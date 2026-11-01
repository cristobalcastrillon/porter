import json
from collections import Counter

import pandas as pd

from porter.make_dirty import build
from porter.profile import column_stats, compute_stats, infer_type, profile_source, stratified_samples, value_shape


def test_value_shape():
    assert value_shape('Aug 27 2015') == "Aaa 99 9999"
    assert value_shape('12"') == '99"'
    assert value_shape(" 180683 ") == " 999999 "


def test_infer_type():
    assert infer_type(["1", " 2 "]) == "integer"
    assert infer_type(["1", "2.5"]) == "decimal"
    assert infer_type(["12 in", "3"]) == "text"
    assert infer_type(["2015-08-27", "08/27/2015", "Aug 27 2015"]) == "date"
    assert infer_type(["Yes", "no"]) == "boolean"
    assert infer_type([]) == "empty"


def test_stats_counts_and_none_stays_text():
    stats = column_stats("Guards", pd.Series(["None", "None", "Helpful", "", "  "]))
    assert stats["null_count"] == 2
    assert stats["distinct_count"] == 2
    assert stats["top_values"][0] == {"value": "None", "count": 2}
    assert stats["inferred_type"] == "text"


def test_top_values_capped_and_samples_stratified():
    counts = Counter({f"v{i}": 100 - i for i in range(60)})
    samples = stratified_samples(counts)
    assert len(samples) == 20 and samples[0] == "v0" and samples[-1] == "v59"
    assert len(set(samples)) == 20
    stats = column_stats("x", pd.Series(list(counts.elements())))
    assert len(stats["top_values"]) == 15


def test_all_spellings_reach_the_model(census):
    legacy, _ = build(census, 1000, 7)
    stats = {c["column"]: c for c in compute_stats(legacy)}
    boro = stats["Boro"]
    seen = {v["value"] for v in boro["top_values"]} | set(boro["samples"])
    assert seen == set(legacy["Boro"])


def test_profile_source_in_fake_mode(census, tmp_path):
    legacy, _ = build(census, 500, 7)
    source = tmp_path / "legacy.csv"
    legacy.to_csv(source, index=False)
    profile = profile_source(source, tmp_path)
    assert [c["column"] for c in profile["columns"]] == list(legacy.columns)
    assert json.loads((tmp_path / "stats.json").read_text())["row_count"] == 500
    assert (tmp_path / "profile.json").exists()
    assert (tmp_path / "calls.jsonl").exists()
