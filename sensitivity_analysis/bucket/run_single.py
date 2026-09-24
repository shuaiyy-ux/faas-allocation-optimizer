"""One ILP solve under the bucket form with given (mults, w_dist, w_tax, scenario).

Calls `app.bucket_pipeline._solve_v2_bucket` directly so we skip the
JSON-shaping overhead of `solve_bucket()`. Returns a metrics dict plus
the params used, ready to be batched into the sweep parquet.

The bucket multiplier vector is passed as a 4-tuple (A, B, C, D) and
must be monotone non-increasing. Non-monotone samples are rejected at
the sweep level, not here, so this function does not validate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "app"))

import bucket_pipeline  # noqa: E402
import engine           # noqa: E402
from scoring import BucketParams  # noqa: E402

from metrics import compute_metrics  # noqa: E402


def _sample_batch(veh_df: pd.DataFrame, n_batch: int, seed: int) -> pd.DataFrame:
    """Without-replacement random sample, reproducible."""
    rng = np.random.RandomState(seed)
    n = len(veh_df)
    if n_batch > n:
        raise ValueError(f"requested batch size {n_batch} exceeds pool size {n}")
    idx = rng.choice(n, size=n_batch, replace=False)
    return veh_df.iloc[idx].reset_index(drop=True).copy()


def run_one(
    bucket_mults: Sequence[float],
    w_dist: float,
    w_tax: float,
    fleet_seed: int,
    n_batch: int,
    signal_field: str = "IN_SERVICE",
    expected_stay_months: int = engine.DEFAULT_EXPECTED_STAY_MONTHS,
) -> dict:
    """Single ILP solve under bucket scoring. Returns flat metrics dict."""
    params = BucketParams(
        bucket_mults=tuple(float(m) for m in bucket_mults),
        signal_field=signal_field,
        w_dist=float(w_dist),
        w_tax=float(w_tax),
    )

    data = engine._data_for_session(None)
    veh = _sample_batch(data["veh"], n_batch, fleet_seed)

    raw = bucket_pipeline._solve_v2_bucket(veh, data, params, expected_stay_months)
    enriched = bucket_pipeline._enrich_bucket(raw, veh, data, params, expected_stay_months)

    # Per-VIN rank within its candidate pool (rank 1 = highest alloc_score among candidates).
    rank_lookup = _compute_ranks(veh, data, params, expected_stay_months)

    assignments = []
    for _, row in enriched.iterrows():
        assignments.append({
            "vin": row["VIN"],
            "dealer": row["DEALER_CODE"],
            "alloc_score": float(row["ALLOC_SCORE"]),
            "distance": float(row["DISTANCE"]),
            "annual_tax": float(row["PROP_TAX"]),
            "dest_util": float(row["UTIL_RATE"]),
            "dest_in_service": float(row["IN_SERVICE"]),
            "dest_rented": float(row["RENTED"]),
            "rank": int(rank_lookup.get((row["VIN"], row["DEALER_CODE"]), 99)),
        })

    metrics = compute_metrics(assignments, n_batch)
    metrics.update({
        "bucket_A": params.bucket_mults[0],
        "bucket_B": params.bucket_mults[1],
        "bucket_C": params.bucket_mults[2],
        "bucket_D": params.bucket_mults[3],
        "w_dist": params.w_dist,
        "w_tax": params.w_tax,
        "fleet_seed": fleet_seed,
        "signal_field": signal_field,
    })
    return metrics


def _compute_ranks(veh, data, params, expected_stay_months) -> dict:
    """For each VIN, rank its candidate dealers by alloc_score descending.

    Returns dict[(VIN, DEALER_CODE)] -> int rank (1-based).
    """
    alloc_score, vin_to_dealers, _, _, _, _, _ = bucket_pipeline._build_pairs_bucket(
        veh, data["dealer"], data["arc_dist"], data["source_to_dealers"],
        params, expected_stay_months, data["distance_norm"], data["tax_norm"],
    )
    out = {}
    for vin, dealers in vin_to_dealers.items():
        scored = [(d, alloc_score.get((vin, d), float("-inf"))) for d in dealers]
        scored.sort(key=lambda x: x[1], reverse=True)
        for rank, (d, _) in enumerate(scored, start=1):
            out[(vin, d)] = rank
    return out


if __name__ == "__main__":
    # Smoke run with the working-assumption multipliers.
    out = run_one(
        bucket_mults=(2.0, 1.4, 1.0, 0.6),
        w_dist=15.0,
        w_tax=1.95,
        fleet_seed=42,
        n_batch=40,
    )
    for k, v in out.items():
        print(f"  {k:24s} = {v}")
