# Porter benchmark

| Metric | Result |
| --- | --- |
| Target fields mapped correctly | 7.7 / 9 |
| Cell values matching ground truth after transform | 99.85 % |
| Rows rejected by the dry run, each with a reason | 0.0 |
| Model cost per migration | 0.70 USD |

Results are averaged over 3 seeded variants of the file.

## Per seed

| Seed | Fields (strict) | Fields (copy = cast) | Cells | Rejected rows | Cost (USD) | Fields differing from ground truth |
| --- | --- | --- | --- | --- | --- | --- |
| 7 | 7/9 | 8/9 | 99.65% | 0 | 0.74 | external_id, diameter_cm |
| 8 | 8/9 | 9/9 | 99.96% | 0 | 0.75 | external_id |
| 9 | 8/9 | 9/9 | 99.95% | 0 | 0.62 | external_id |

Wrong cells by field: diameter_cm 141, species 51.
