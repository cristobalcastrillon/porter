"""Turn the 2015 NYC Street Tree Census into a messy legacy export with known answers.

Usage: python -m porter.make_dirty --rows 5000 --seed 7

Writes the corrupted CSV plus the ground truth used by porter.evaluate. The same
rows and seed always produce byte-identical files.
"""
from __future__ import annotations

import argparse
import io
import json
import random
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_CSV = DATA_DIR / "raw" / "street_tree_census_2015.csv"
SOCRATA_URL = "https://data.cityofnewyork.us/resource/uvpi-gqnh.csv"
SOURCE_COLUMNS = [
    "tree_id", "created_at", "spc_common", "spc_latin", "status", "health", "tree_dbh",
    "stump_diam", "boroname", "latitude", "longitude", "address", "steward", "guards",
]
PAGE_SIZE = 100_000

LEGACY_COLUMNS = [
    "TreeID", "Inspection Date", "Species (common)", "Latin Name", "Status", "Cond.", "DBH",
    "Stump Dia", "Boro", "Location", "Address", "Steward", "Guards",
]
TARGET_COLUMNS = [
    "external_id", "species", "status", "condition", "diameter_cm",
    "latitude", "longitude", "borough", "inspected_on",
]

# Share of the file, chosen so 5,000 rows gives 141 stumps, 71 dead trees and 97 repeats.
STUMP_SHARE = 0.0282
DEAD_SHARE = 0.0142
DUPLICATE_SHARE = 0.0194

# (value, weight) pairs. The spellings are the ones listed in examples/profile.example.json.
DATE_FORMATS = [("%m/%d/%Y", 61), ("%Y-%m-%d", 27), ("%b %d %Y", 12)]
STATUS_VARIANTS = {
    "Alive": [("Alive", 87), ("alive", 9), ("A", 4)],
    "Stump": [("Stump", 90), ("STUMP", 10)],
    "Dead": [("Dead", 85), ("dead", 7), ("D", 8)],
}
CONDITION_VARIANTS = {
    "Good": [("Good", 80), ("good", 11), ("G", 9)],
    "Fair": [("Fair", 80), ("fair", 11), ("F", 9)],
    "Poor": [("Poor", 80), ("poor", 11), ("P", 9)],
}
BOROUGH_VARIANTS = {
    "Manhattan": [("Manhattan", 75), ("manhattan", 10), ("MN", 15)],
    "Bronx": [("Bronx", 75), ("The Bronx", 10), ("BX", 15)],
    "Brooklyn": [("Brooklyn", 75), ("Bklyn", 10), ("BK", 15)],
    "Queens": [("Queens", 75), ("queens", 10), ("QN", 15)],
    "Staten Island": [("Staten Island", 70), ("Staten Is", 8), ("S.I.", 8), ("SI", 14)],
}
LEADING_SPACE_SHARE = 0.008
PARENTHESES_SHARE = 0.22

GROUND_TRUTH = {
    "external_id": {"source_columns": ["TreeID"], "kind": "cast", "also_accept": ["copy"]},
    "species": {"source_columns": ["Species (common)"], "kind": "expression"},
    "status": {"source_columns": ["Status"], "kind": "map_values"},
    "condition": {"source_columns": ["Cond."], "kind": "map_values"},
    "diameter_cm": {"source_columns": ["DBH", "Stump Dia"], "kind": "conditional"},
    "latitude": {"source_columns": ["Location"], "kind": "split"},
    "longitude": {"source_columns": ["Location"], "kind": "split"},
    "borough": {"source_columns": ["Boro"], "kind": "map_values"},
    "inspected_on": {"source_columns": ["Inspection Date"], "kind": "parse_date"},
}


def download_census(path: Path = RAW_CSV) -> Path:
    """Download the census columns Porter needs and cache them. Skips if already cached."""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    pages: list[pd.DataFrame] = []
    with httpx.Client(timeout=180) as client:
        while True:
            params = {
                "$select": ",".join(SOURCE_COLUMNS),
                "$order": "tree_id",
                "$limit": PAGE_SIZE,
                "$offset": PAGE_SIZE * len(pages),
            }
            response = client.get(SOCRATA_URL, params=params)
            response.raise_for_status()
            page = pd.read_csv(io.StringIO(response.text), dtype=str, keep_default_na=False)
            if page.empty:
                break
            pages.append(page)
            print(f"downloaded {sum(len(p) for p in pages):,} rows")
            if len(page) < PAGE_SIZE:
                break
    tmp = path.with_suffix(".tmp")
    pd.concat(pages).to_csv(tmp, index=False)
    tmp.replace(path)
    return path


def load_census(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def stump_dbh_report(census: pd.DataFrame) -> tuple[int, int]:
    """Return (stumps, stumps whose tree_dbh is not 0)."""
    stumps = census[census["status"] == "Stump"]
    return len(stumps), int((stumps["tree_dbh"].astype(float) != 0).sum())


def sample_census(census: pd.DataFrame, rows: int, seed: int) -> tuple[pd.DataFrame, int]:
    """Stratified sample of unique trees. Returns the sample and how many repeats to add."""
    repeats = round(rows * DUPLICATE_SHARE)
    unique = rows - repeats
    stumps = round(rows * STUMP_SHARE)
    dead = round(rows * DEAD_SHARE)
    wanted = {"Stump": stumps, "Dead": dead, "Alive": unique - stumps - dead}
    census = census.drop_duplicates("tree_id").sort_values("tree_id", kind="stable")
    parts = [
        census[census["status"] == status].sample(n=n, random_state=seed)
        for status, n in wanted.items()
    ]
    return pd.concat(parts), repeats


def pick(rng: random.Random, options: list[tuple[str, int]]) -> str:
    values, weights = zip(*options)
    return rng.choices(values, weights=weights)[0]


def recase(rng: random.Random, text: str) -> str:
    return pick(rng, [(text, 78), (text.upper(), 11), (text.lower(), 7), (text.title(), 4)])


def number_text(raw: str) -> str:
    return str(int(float(raw)))


def corrupt_row(row: dict, rng: random.Random) -> dict:
    """One census row written the way the legacy system would have written it."""
    tree_id = row["tree_id"]
    if rng.random() < LEADING_SPACE_SHARE:
        tree_id = f" {tree_id} "

    inspected = datetime.strptime(row["created_at"][:10], "%Y-%m-%d")
    dbh = number_text(row["tree_dbh"])
    dbh_style = pick(rng, [("{}", 82), ("{} in", 11), ('{}"', 7)])
    location = f"{row['latitude']}, {row['longitude']}"
    if rng.random() < PARENTHESES_SHARE:
        location = f"({location})"

    return {
        "TreeID": tree_id,
        "Inspection Date": inspected.strftime(pick(rng, DATE_FORMATS)),
        "Species (common)": recase(rng, row["spc_common"]) if row["spc_common"] else "",
        "Latin Name": row["spc_latin"],
        "Status": pick(rng, STATUS_VARIANTS[row["status"]]),
        "Cond.": pick(rng, CONDITION_VARIANTS[row["health"]]) if row["health"] else "",
        "DBH": dbh_style.format(dbh),
        "Stump Dia": number_text(row["stump_diam"]),
        "Boro": pick(rng, BOROUGH_VARIANTS[row["boroname"]]),
        "Location": location,
        "Address": row["address"],
        "Steward": row["steward"],
        "Guards": row["guards"],
    }


def expected_row(row: dict) -> dict:
    """The correct target row. Stumps take their stump diameter (the recommended answer to q1)."""
    is_stump = row["status"] == "Stump"
    inches = float(row["stump_diam"] if is_stump else row["tree_dbh"])
    return {
        "external_id": row["tree_id"],
        "species": row["spc_common"],
        "status": row["status"].lower(),
        "condition": row["health"].lower(),
        "diameter_cm": round(inches * 2.54, 1),
        "latitude": float(row["latitude"]),
        "longitude": float(row["longitude"]),
        "borough": row["boroname"],
        "inspected_on": row["created_at"][:10],
    }


def build(census: pd.DataFrame, rows: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (legacy export, expected target rows)."""
    sample, repeats = sample_census(census, rows, seed)
    rng = random.Random(seed)
    records = sample.to_dict("records")
    legacy = [corrupt_row(r, rng) for r in records]
    legacy += rng.sample(legacy, repeats)
    rng.shuffle(legacy)
    expected = pd.DataFrame([expected_row(r) for r in records], columns=TARGET_COLUMNS)
    return (
        pd.DataFrame(legacy, columns=LEGACY_COLUMNS),
        expected.sort_values("external_id", kind="stable").reset_index(drop=True),
    )


def write_outputs(legacy: pd.DataFrame, expected: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    legacy.to_csv(out_dir / "legacy_tree_inventory.csv", index=False)
    expected.to_csv(out_dir / "expected_assets.csv", index=False)
    (out_dir / "ground_truth.json").write_text(json.dumps(GROUND_TRUTH, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=DATA_DIR)
    args = parser.parse_args()

    census = load_census(download_census())
    stumps, nonzero = stump_dbh_report(census)
    print(f"census: {len(census):,} rows; {stumps:,} stumps, {nonzero} with tree_dbh != 0 (nothing forced)"
          if nonzero == 0 else f"census: {nonzero} stumps have tree_dbh != 0")
    if nonzero:
        census.loc[census["status"] == "Stump", "tree_dbh"] = "0"
        print("forced tree_dbh = 0 on those stumps")

    legacy, expected = build(census, args.rows, args.seed)
    write_outputs(legacy, expected, args.out)
    status = expected["status"].value_counts().to_dict()
    print(f"wrote {args.out}/legacy_tree_inventory.csv: {len(legacy):,} rows, "
          f"{len(legacy) - len(expected)} exact repeats, {status}")


if __name__ == "__main__":
    main()
