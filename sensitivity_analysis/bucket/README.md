# Bucket scoring calibration

Sobol-Pareto-knee infrastructure for calibrating `BucketParams.bucket_mults`,
`w_dist`, and `w_tax`. Methodology mirrors the prior 4-D additive sweep
(commit `ffd6af4`), with two adaptations.

1. Search space is 6-D, the four bucket multipliers plus the two penalty
   weights. The four multipliers are sorted descending per Sobol sample
   so `(A, B, C, D)` is monotone.
2. Primary positive Pareto axis is `avg_in_service_at_dest` instead of
   `avg_rented_at_dest`, since the bucket axis is `IN_SERVICE` per email
   spec. `avg_rented_at_dest` is kept for cross-form comparison.

## Files

| File | Role |
|---|---|
| `run_single.py` | One ILP solve under bucket form, returns metrics dict |
| `sweep_bucket.py` | Parallel Sobol sweep, writes long-form parquet |
| `metrics.py` | Pareto axes + diagnostics including HHI |
| `output/` | Parquet outputs, gitignored |

## Smoke run

```bash
.venv/bin/python sensitivity_analysis/bucket/sweep_bucket.py \
    --points 32 --sizes 30 40 --seeds 3 --workers 4 \
    --output sensitivity_analysis/bucket/output/smoke.parquet
```

32 points × 2 sizes × 3 seeds = 192 ILP solves. Confirms the pipeline
runs end-to-end and produces a non-empty parquet. Expect roughly two
to four minutes on a 2024 Mac.

## Full sweep

```bash
.venv/bin/python sensitivity_analysis/bucket/sweep_bucket.py \
    --points 1024 --workers 8 \
    --output sensitivity_analysis/bucket/output/sweep_v1.parquet
```

1024 points × 8 sizes × 10 seeds = 81,920 ILP solves. Roughly 30 to
60 minutes wall clock. Matches the additive sweep's run-count target.

## Knee selection is manual

The sweep deliberately does not auto-select a knee. Inspect the parquet,
plot the Pareto front in `(avg_in_service_at_dest, avg_distance, hhi)`
space, and choose. The HHI axis is the bucket form's known risk per
`docs/allocation_scoring_explained.md`, so a knee that improves
`avg_in_service_at_dest` while degrading HHI by more than the additive
baseline should be rejected.

## Dependencies

Beyond `app/requirements.txt`, the sweep adds `scipy` for the Sobol
sampler and `pyarrow` for parquet writes. Both are calibration-only,
not production runtime. Install via

```bash
.venv/bin/pip install scipy pyarrow
```
