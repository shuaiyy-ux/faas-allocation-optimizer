"""Knee picker for the bucket Sobol sweep.

Reads the long-form parquet emitted by `sweep_bucket.py`, averages each
point_id's outcomes over its 80 scenarios, normalizes the four Pareto
axes to [0, 1] using observed min/max, and reports the points geometrically
closest to utopia (1, 1, 1, 1).

The four Pareto axes (no dollar conversion assumed)
---------------------------------------------------
    maximize  avg_in_service_at_dest       (primary positive, bucket form)
    maximize  avg_util_at_dest             (per-car earning efficiency)
    minimize  avg_distance                 (carrier cost proxy)
    minimize  avg_annual_tax               (property-tax cost proxy)

HHI is reported alongside the candidates so a concentration spike can
be flagged. The script does NOT auto-pick; it prints the top-5 nearest
points and lets the operator choose.

Usage
-----
    .venv/bin/python sensitivity_analysis/bucket/analysis.py \\
        sensitivity_analysis/bucket/output/sweep_v1.parquet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


PARETO_AXES = {
    "avg_in_service_at_dest": "max",
    "avg_util_at_dest": "max",
    "avg_distance": "min",
    "avg_annual_tax": "min",
}


def aggregate_per_point(df: pd.DataFrame) -> pd.DataFrame:
    """Mean each Pareto axis (and HHI, rank1_pct) per point_id over scenarios."""
    agg_cols = list(PARETO_AXES.keys()) + ["hhi_concentration", "rank1_pct", "n_assigned"]
    grouped = df.groupby("point_id")[agg_cols].mean().reset_index()
    # Carry the params (constant per point_id) through the join.
    param_cols = ["bucket_A", "bucket_B", "bucket_C", "bucket_D", "w_dist", "w_tax"]
    params = df.groupby("point_id")[param_cols].first().reset_index()
    return grouped.merge(params, on="point_id")


def utopia_distance(agg: pd.DataFrame) -> pd.DataFrame:
    """Append a `dist_to_utopia` column under normalized 4-D Pareto axes."""
    norm = agg.copy()
    for axis, direction in PARETO_AXES.items():
        lo, hi = float(agg[axis].min()), float(agg[axis].max())
        span = (hi - lo) or 1.0
        # After normalization, 1.0 always = best (maximize axes: higher = 1;
        # minimize axes: lower = 1).
        if direction == "max":
            norm[axis + "_n"] = (agg[axis] - lo) / span
        else:
            norm[axis + "_n"] = (hi - agg[axis]) / span

    diffs = np.column_stack([
        1.0 - norm[axis + "_n"].to_numpy()
        for axis in PARETO_AXES
    ])
    norm["dist_to_utopia"] = np.sqrt((diffs ** 2).sum(axis=1))
    return norm.sort_values("dist_to_utopia", ascending=True).reset_index(drop=True)


def report(parquet_path: Path, top_k: int = 5) -> None:
    df = pd.read_parquet(parquet_path)
    print(f"Read {len(df):,} rows from {parquet_path}")
    print(f"Unique points: {df['point_id'].nunique()}  |  "
          f"Scenarios per point: {len(df) // df['point_id'].nunique()}")

    agg = aggregate_per_point(df)
    ranked = utopia_distance(agg)

    print("\n=== TOP CANDIDATES (closest to 4-D utopia) ===")
    cols = ["point_id", "bucket_A", "bucket_B", "bucket_C", "bucket_D",
            "w_dist", "w_tax",
            "avg_in_service_at_dest", "avg_util_at_dest",
            "avg_distance", "avg_annual_tax",
            "hhi_concentration", "rank1_pct", "dist_to_utopia"]
    print(ranked.head(top_k)[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n=== AXIS RANGES (across all sweep points, after per-point averaging) ===")
    for axis in PARETO_AXES:
        print(f"  {axis:28s}  min={agg[axis].min():.4f}  max={agg[axis].max():.4f}  "
              f"mean={agg[axis].mean():.4f}")
    print(f"  {'hhi_concentration':28s}  min={agg['hhi_concentration'].min():.4f}  "
          f"max={agg['hhi_concentration'].max():.4f}  mean={agg['hhi_concentration'].mean():.4f}")

    knee = ranked.iloc[0]
    print("\n=== KNEE PICK ===")
    print(f"  bucket_mults = [{knee['bucket_A']:.4f}, {knee['bucket_B']:.4f}, "
          f"{knee['bucket_C']:.4f}, {knee['bucket_D']:.4f}]")
    print(f"  w_dist       = {knee['w_dist']:.4f}")
    print(f"  w_tax        = {knee['w_tax']:.4f}")
    print(f"  avg_in_service_at_dest = {knee['avg_in_service_at_dest']:.4f}")
    print(f"  avg_util_at_dest       = {knee['avg_util_at_dest']:.4f}")
    print(f"  avg_distance           = {knee['avg_distance']:.4f}")
    print(f"  avg_annual_tax         = {knee['avg_annual_tax']:.4f}")
    print(f"  hhi_concentration      = {knee['hhi_concentration']:.4f}")
    print(f"  rank1_pct              = {knee['rank1_pct']:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("parquet", type=str)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    report(Path(args.parquet), top_k=args.top_k)


if __name__ == "__main__":
    main()
