"""Structural-metrics companion to scripts/benchmark_three_methods.py.

Same experimental design (100 seeds x 3 solvers x batch_size=50, same seed
paired across methods, np.random.default_rng(42).integers(0, 1e9, size=100)).
Does NOT alter the original benchmark — collects four extra structural
metrics per (seed, method) run and dumps them to a CSV.

Metrics (all derived from result["vehicles"], with one CSV join):

  1. dealers_used        — count of distinct assigned.dealer_code over
                            assigned vehicles.
  2. avg_dest_in_service — mean IN_SERVICE of the destination dealer for
                            each assigned vehicle. Joins on dealer_code
                            from data_csv/dealer_utilization.csv (column
                            IN_SERVICE). bucket_ilp vehicles already carry
                            assigned.in_service inline; we still re-derive
                            from the same join for cross-method comparability.
  3. unique_routes       — count of distinct (source_dealer, dest_dealer)
                            pairs. source_dealer = vehicle["source"];
                            dest_dealer = vehicle["assigned"]["dealer_code"].
  4. cross_state_trips   — number of assigned vehicles with
                            vehicle["source_state"] != assigned["state"].

No business judgments are printed — just per-method describe() blocks.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Match the original benchmark's import discipline: add app/ to sys.path
# and chdir into it so engine's relative CSV paths resolve.
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
DATA_DIR = REPO_ROOT / "data_csv"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
os.chdir(APP_DIR)

from engine import solve_weekly_batch, solve_weekly_greedy  # noqa: E402
from bucket_pipeline import solve_weekly_bucket  # noqa: E402


BATCH_SIZE = 50
N_SEEDS = 100
RNG = np.random.default_rng(42)
SEEDS = [int(s) for s in RNG.integers(low=0, high=10**9, size=N_SEEDS)]

CSV_PATH = REPO_ROOT / "scripts" / "benchmark_structural_metrics.csv"


# --- One-time join tables --------------------------------------------------
# dealer_utilization.csv columns: DEALER_ID, NAME, RENTED, IN_SERVICE, UTILIZATION
# Map dealer_code -> IN_SERVICE.
_util = pd.read_csv(DATA_DIR / "dealer_utilization.csv")
DEALER_IN_SERVICE: dict[str, float] = {
    str(row["DEALER_ID"]): float(row["IN_SERVICE"])
    for _, row in _util.iterrows()
}
# Fallback: dealer_inventory.csv DELIVERED_COUNT (per AGENTS.md, numerically
# identical to IN_SERVICE). Used only if a dealer is somehow missing from
# the utilization CSV.
_inv = pd.read_csv(DATA_DIR / "dealer_inventory.csv")
DEALER_DELIVERED: dict[str, float] = {
    str(row["DEALER_CODE"]): float(row["DELIVERED_COUNT"])
    for _, row in _inv.iterrows()
}


def _dest_in_service(dealer_code: str) -> float:
    """Look up destination dealer IN_SERVICE. NaN if unknown."""
    if dealer_code in DEALER_IN_SERVICE:
        return DEALER_IN_SERVICE[dealer_code]
    if dealer_code in DEALER_DELIVERED:
        return DEALER_DELIVERED[dealer_code]
    return float("nan")


def _extract_structural(result: dict) -> dict:
    """Pull the four structural metrics from a solver result dict.
    NaN for any column we can't derive (no fabrication, no approximations).
    """
    vehicles = result.get("vehicles", []) or []
    assigned = [v for v in vehicles if v.get("assigned")]
    n_assigned = len(assigned)

    # 2. dealers_used
    try:
        dest_codes = [v["assigned"].get("dealer_code") for v in assigned]
        if any(dc is None for dc in dest_codes):
            dealers_used = float("nan")
        else:
            dealers_used = float(len(set(dest_codes)))
    except Exception:
        dealers_used = float("nan")

    # 3. avg_dest_in_service (join via DEALER_IN_SERVICE)
    try:
        ins_vals: list[float] = []
        for v in assigned:
            dc = v["assigned"].get("dealer_code")
            ins = _dest_in_service(dc) if dc is not None else float("nan")
            ins_vals.append(ins)
        if not ins_vals or any(math.isnan(x) for x in ins_vals):
            # If even one assigned vehicle has no IN_SERVICE join match,
            # treat the whole metric as NaN rather than partial-average.
            avg_dest_in_service = float("nan") if any(math.isnan(x) for x in ins_vals) else float("nan")
        else:
            avg_dest_in_service = float(sum(ins_vals) / len(ins_vals))
    except Exception:
        avg_dest_in_service = float("nan")

    # 4. unique_routes
    try:
        pairs = []
        for v in assigned:
            src = v.get("source")
            dst = v["assigned"].get("dealer_code")
            if src is None or dst is None:
                pairs = None
                break
            pairs.append((src, dst))
        if pairs is None:
            unique_routes = float("nan")
        else:
            unique_routes = float(len(set(pairs)))
    except Exception:
        unique_routes = float("nan")

    # 5. cross_state_trips
    try:
        cnt = 0
        bad = False
        for v in assigned:
            src_state = v.get("source_state")
            dst_state = v["assigned"].get("state")
            if src_state is None or dst_state is None:
                bad = True
                break
            if src_state != dst_state:
                cnt += 1
        cross_state_trips = float("nan") if bad else float(cnt)
    except Exception:
        cross_state_trips = float("nan")

    return {
        "n_assigned": float(n_assigned),
        "dealers_used": dealers_used,
        "avg_dest_in_service": avg_dest_in_service,
        "unique_routes": unique_routes,
        "cross_state_trips": cross_state_trips,
    }


def _run_one(method_name: str, fn, seed: int) -> dict:
    t0 = time.time()
    row = {
        "seed": seed,
        "method": method_name,
        "n_assigned": float("nan"),
        "dealers_used": float("nan"),
        "avg_dest_in_service": float("nan"),
        "unique_routes": float("nan"),
        "cross_state_trips": float("nan"),
        "runtime_seconds": float("nan"),
    }
    try:
        result = fn(n_batch=BATCH_SIZE, seed=seed)
        row.update(_extract_structural(result))
    except Exception as exc:
        print(f"  [warn] {method_name} seed={seed} raised {type(exc).__name__}: {exc}",
              file=sys.stderr)
    row["runtime_seconds"] = round(time.time() - t0, 3)
    return row


METHODS = [
    ("additive_ilp", solve_weekly_batch),
    ("bucket_ilp", solve_weekly_bucket),
    ("greedy", solve_weekly_greedy),
]


def main():
    rows = []
    t_total = time.time()
    for i, seed in enumerate(SEEDS, start=1):
        for name, fn in METHODS:
            rows.append(_run_one(name, fn, seed))
        if i % 10 == 0 or i == 1:
            elapsed = time.time() - t_total
            print(f"[progress] {i}/{N_SEEDS} seeds done, elapsed {elapsed:.1f}s")

    df = pd.DataFrame(rows, columns=[
        "seed", "method", "n_assigned", "dealers_used",
        "avg_dest_in_service", "unique_routes", "cross_state_trips",
        "runtime_seconds",
    ])
    df.to_csv(CSV_PATH, index=False)
    print(f"\n[done] Wrote {len(df)} rows -> {CSV_PATH}")

    print("\n=== Describe() by method (raw, no judgments) ===")
    numeric_cols = [
        "n_assigned", "dealers_used", "avg_dest_in_service",
        "unique_routes", "cross_state_trips", "runtime_seconds",
    ]
    with pd.option_context("display.float_format", lambda v: f"{v:,.3f}",
                           "display.width", 160,
                           "display.max_columns", 20):
        for name, _ in METHODS:
            sub = df[df["method"] == name][numeric_cols]
            print(f"\n--- method = {name} (n={len(sub)}) ---")
            print(sub.describe())


if __name__ == "__main__":
    main()
