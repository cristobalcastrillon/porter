import json

from porter.make_dirty import GROUND_TRUTH, LEGACY_COLUMNS, build, write_outputs

PROFILE = json.load(open("examples/profile.example.json"))
TARGET = json.load(open("examples/target.assets.json"))


def variants(column: str) -> set[str]:
    col = next(c for c in PROFILE["columns"] if c["column"] == column)
    return {v for g in col["canonical_values"] for v in g["variants"]}


def test_same_seed_is_byte_identical(census, tmp_path):
    for name in ("a", "b"):
        write_outputs(*build(census, 500, 7), tmp_path / name)
    for file in ("legacy_tree_inventory.csv", "expected_assets.csv", "ground_truth.json"):
        assert (tmp_path / "a" / file).read_bytes() == (tmp_path / "b" / file).read_bytes()


def test_different_seed_differs(census):
    assert not build(census, 500, 7)[0].equals(build(census, 500, 8)[0])


def test_shape_and_strata(census):
    legacy, expected = build(census, 1000, 7)
    assert list(legacy.columns) == LEGACY_COLUMNS
    assert len(legacy) == 1000
    assert expected["external_id"].is_unique
    assert legacy["TreeID"].str.strip().nunique() == len(expected)
    assert legacy.duplicated().sum() == 1000 - len(expected)
    assert (expected["status"] == "stump").sum() == 28
    assert (expected["status"] == "dead").sum() == 14


def test_only_spellings_from_the_example_profile(census):
    legacy, _ = build(census, 1000, 7)
    for column in ("Status", "Cond.", "Boro"):
        assert set(legacy[column]) <= variants(column)


def test_corruptions_present(census):
    legacy, _ = build(census, 1000, 7)
    assert legacy["DBH"].str.endswith(" in").any()
    assert legacy["DBH"].str.endswith('"').any()
    assert legacy["Location"].str.startswith("(").any()
    assert (legacy["TreeID"] != legacy["TreeID"].str.strip()).any()
    shapes = legacy["Inspection Date"].str.replace(r"[A-Za-z]", "a", regex=True).str.replace(r"\d", "9", regex=True)
    assert set(shapes) == {"99/99/9999", "9999-99-99", "aaa 99 9999"}
    assert legacy["Species (common)"].str.isupper().any()


def test_stump_ground_truth_uses_stump_diameter(census):
    _, expected = build(census, 1000, 7)
    row = census[census["tree_id"] == expected[expected["status"] == "stump"].iloc[0]["external_id"]].iloc[0]
    stump = expected[expected["external_id"] == row["tree_id"]].iloc[0]
    assert stump["diameter_cm"] == round(float(row["stump_diam"]) * 2.54, 1)
    assert row["tree_dbh"] == "0"


def test_ground_truth_covers_target():
    assert list(GROUND_TRUTH) == [f["name"] for f in TARGET["fields"]]
