"""AST allow-list and restricted execution for model-written transform functions.

This limits what approved code can reach; a human still reads every function first.
"""
from __future__ import annotations

import __future__
import ast
import builtins
import datetime
import decimal
import math
import re
import types
from typing import Callable

ALLOWED_IMPORTS = {"re": re, "datetime": datetime, "math": math, "decimal": decimal}

FORBIDDEN_NAMES = {
    "exec", "eval", "open", "compile", "__import__", "getattr", "setattr", "delattr", "hasattr",
    "globals", "locals", "vars", "dir", "input", "breakpoint", "memoryview", "type", "object",
    "super", "classmethod", "staticmethod", "property", "print", "help", "exit", "quit",
}
# str.format can reach attributes from inside a string, where the AST can't see them.
FORBIDDEN_ATTRIBUTES = {"format", "format_map"}
FORBIDDEN_NODES = (
    ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith, ast.Global,
    ast.Nonlocal, ast.Yield, ast.YieldFrom, ast.While, ast.With,
)
SAFE_BUILTINS = [
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "frozenset", "int",
    "isinstance", "len", "list", "map", "max", "min", "range", "repr", "reversed", "round",
    "set", "sorted", "str", "sum", "tuple", "zip", "ValueError", "TypeError", "KeyError",
    "IndexError", "ArithmeticError", "Exception",
]


def _module_attributes() -> set[str]:
    """Names like re.enum or decimal.sys: a module reached through an allowed module."""
    return {
        name
        for module in ALLOWED_IMPORTS.values()
        for name in dir(module)
        if isinstance(getattr(module, name), types.ModuleType)
    }


MODULE_ATTRIBUTES = _module_attributes()


def _check_import(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        roots = [alias.name for alias in node.names]
        names: list[str] = []
    else:
        roots = [node.module or ""]
        names = [alias.name for alias in node.names]
        if node.level:
            return ["relative imports are not allowed"]
    errors = [f"import of '{r}' is not allowed" for r in roots if r not in ALLOWED_IMPORTS]
    for name in names:
        if name == "*" or name.startswith("_") or name in MODULE_ATTRIBUTES:
            errors.append(f"importing '{name}' is not allowed")
    return errors


def check_code(code: str) -> list[str]:
    """Return every rule the code breaks. An empty list means it may be compiled."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]

    errors: list[str] = []
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if [f.name for f in functions] != ["f"]:
        errors.append("code must define exactly one top-level function, named f")
    elif len(functions[0].args.args) != 1 or functions[0].args.vararg or functions[0].args.kwarg:
        errors.append("f must take exactly one argument")
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
                errors.append(f"line {node.lineno}: only imports, constants and f are allowed at the top level")

    # The signature's annotations (-> object) are never evaluated, so they need no checking.
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            function.returns = None
            for argument in function.args.args:
                argument.annotation = None

    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, FORBIDDEN_NODES):
            errors.append(f"line {line}: {type(node).__name__} is not allowed")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            errors.extend(f"line {line}: {e}" for e in _check_import(node))
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES or node.id.startswith("_"):
                errors.append(f"line {line}: name '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in FORBIDDEN_ATTRIBUTES or node.attr in MODULE_ATTRIBUTES:
                errors.append(f"line {line}: attribute '{node.attr}' is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            errors.append(f"line {line}: strings containing '__' are not allowed")
    return errors


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level or name not in ALLOWED_IMPORTS:
        raise ImportError(f"import of '{name}' is not allowed")
    return ALLOWED_IMPORTS[name]


def compile_function(code: str) -> Callable[[str | None], object]:
    """Check the code, then build f with restricted builtins. Raises ValueError if refused."""
    errors = check_code(code)
    if errors:
        raise ValueError("; ".join(errors))
    allowed = {name: getattr(builtins, name) for name in SAFE_BUILTINS}
    allowed["__import__"] = _restricted_import
    # The only exec in Porter; check_code ran above.
    namespace: dict = {"__builtins__": allowed, "__name__": "porter_generated"}
    flags = __future__.annotations.compiler_flag
    exec(compile(code, "<generated>", "exec", flags=flags, dont_inherit=True), namespace)
    return namespace["f"]
