"""Shared fixtures. Tests run in fake LLM mode and never touch the network."""
from __future__ import annotations

import socket

import pandas as pd
import pytest

SPECIES = [
    ("London planetree", "Platanus x acerifolia"),
    ("honeylocust", "Gleditsia triacanthos var. inermis"),
    ("pin oak", "Quercus palustris"),
    ("'Schubert' chokecherry", "Prunus virginiana"),
]
BOROUGHS = ["Manhattan", "Bronx", "Brooklyn", "Queens", "Staten Island"]
HEALTH = ["Good", "Fair", "Poor"]


def synthetic_census(rows: int = 2000) -> pd.DataFrame:
    """A small census-shaped table, so tests don't need the real download."""
    records = []
    for i in range(rows):
        status = "Stump" if i % 20 == 0 else "Dead" if i % 20 == 1 else "Alive"
        alive = status == "Alive"
        common, latin = SPECIES[i % len(SPECIES)]
        records.append({
            "tree_id": str(100000 + i),
            "created_at": f"2015-{(i % 9) + 1:02d}-{(i % 27) + 1:02d}T00:00:00.000",
            "spc_common": common if alive else "",
            "spc_latin": latin if alive else "",
            "status": status,
            "health": HEALTH[i % 3] if alive else "",
            "tree_dbh": "0" if status == "Stump" else str(3 + i % 30),
            "stump_diam": str(4 + i % 40) if status == "Stump" else "0",
            "boroname": BOROUGHS[i % 5],
            "latitude": f"{40.5 + (i % 400) / 1000:.8f}",
            "longitude": f"{-74.2 + (i % 500) / 1000:.8f}",
            "address": f"{i} 70 AVENUE",
            "steward": "None" if alive else "",
            "guards": "Helpful" if alive else "",
        })
    return pd.DataFrame(records)


@pytest.fixture
def census() -> pd.DataFrame:
    return synthetic_census()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if anything tries to open a real connection."""

    def blocked(*args, **kwargs):
        raise AssertionError("tests must not use the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setenv("PORTER_FAKE_LLM", "1")
