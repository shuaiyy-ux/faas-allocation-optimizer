"""Paired-sample benchmark: additive ILP vs bucket ILP vs Greedy.

Same (seed, batch_size=50) fed to all three solvers per iteration so the
three method runs on row i are apples-to-apples.

Output: scripts/benchmark_three_methods.csv
Reports: per-method describe() to stdout. No business judgments.
"""

from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# `app.engine` does `from constraints import ...`, which only resolves when
# `app/` itself is on sys.path (constraints is a sibling package inside app/).
# We also chdir into app/ so that engine's relative CSV paths resolve.
REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
os.chdir(APP_DIR)

from engine import solve_weekly_batch, solve_weekly_greedy  # noqa: E402
from bucket_pipeline import solve_weekly_bucket  # noqa: E402


BATCH_SIZE = 50
N_SEEDS = 100
RNG = np.random.default_rng(42)
SEEDS = [int(s) for s in RNG.integers(low=0, high=10**9, size=N_SEEDS)]

CSV_PATH = REPO_ROOT / "scripts" / "benchmark_three_methods.csv"


def _extract_metrics(result: dict) -> dict:
    """Pull raw KPIs from a solver result dict. Returns NaN where the
    underlying field isn't present (no fabrication).
    """
    vehicles = result.get("vehicles", []) or []
    assigned = [v for v in vehicles if v.get("assigned")]

    n_assigned = result.get("n_assigned", float("nan"))
    total_distance = result.get("total_distance", float("nan"))

    # prop_tax on each assigned vehicle is rate * residual * stay_months/12;
    # with the default stay=12 this is already the annualised tax for the
    # stay period. Sum across assigned vehicles.
    if assigned:
        tax_vals = [v["assigned"].get("prop_tax") for v in assigned]
        if all(t is not None for t in tax_vals):
            total_annual_tax = float(sum(tax_vals))
        else:
            total_annual_tax = float("nan")
        util_vals = [v["assigned"].get("utilization") for v in assigned]
        if all(u is not None for u in util_vals):
            avg_util = float(sum(util_vals) / len(util_vals))
        else:
            avg_util = float("nan")
    else:
        total_annual_tax = float("nan")
        avg_util = float("nan")

    return {
        "n_assigned": n_assigned,
        "total_distance": total_distance,
        "total_annual_tax_dollars": total_annual_tax,
        "avg_util_at_destination": avg_util,
    }


def _run_one(method_name: str, fn, seed: int) -> dict:
    t0 = time.time()
    row = {
        "seed": seed,
        "method": method_name,
        "n_assigned": float("nan"),
        "total_distance": float("nan"),
        "total_annual_tax_dollars": float("nan"),
        "avg_util_at_destination": float("nan"),
        "runtime_seconds": float("nan"),
    }
    try:
        result = fn(n_batch=BATCH_SIZE, seed=seed)
        metrics = _extract_metrics(result)
        row.update(metrics)
    except Exception as exc:  # solver crash, infeasible, etc.
        print(f"  [warn] {method_name} seed={seed} raised {type(exc).__name__}: {exc}",
              file=sys.stderr)
    row["runtime_seconds"] = round(time.time() - t0, 3)
    return row


METHODS = [
    ("additive_ilp", solve_weekly_batch),
    ("bucket_ilp", solve_weekly_bucket),
    ("greedy", solve_weekly_greedy),
]


def _timing_probe(n_probe: int = 5) -> float:
    """Run n_probe seeds across all three methods and report per-call timing.
    Returns estimated total runtime in seconds for N_SEEDS * 3 methods.
    """
    print(f"[probe] Running {n_probe} seeds across {len(METHODS)} methods to estimate timing...")
    probe_times = {name: [] for name, _ in METHODS}
    for seed in SEEDS[:n_probe]:
        for name, fn in METHODS:
            t0 = time.time()
            try:
                fn(n_batch=BATCH_SIZE, seed=seed)
            except Exception as exc:
                print(f"  [probe-warn] {name} seed={seed}: {exc}", file=sys.stderr)
            dt = time.time() - t0
            probe_times[name].append(dt)
            if dt > 10.0:
                print(f"[probe-abort] {name} seed={seed} took {dt:.2f}s > 10s — stopping.",
                      file=sys.stderr)
                sys.exit(2)
    avg = {name: sum(ts) / len(ts) for name, ts in probe_times.items()}
    print("[probe] Per-call mean seconds:")
    for name, mean_t in avg.items():
        print(f"  {name:<14}{mean_t:.2f}s  (samples: {probe_times[name]})")
    est = sum(avg.values()) * N_SEEDS
    print(f"[probe] Estimated total runtime for {N_SEEDS} seeds x 3 methods: ~{est:.1f}s ({est/60:.1f} min)")
    return est


def main():
    est = _timing_probe(n_probe=5)
    # Honour the 30-min budget guidance; warn but proceed unless catastrophic.
    if est > 30 * 60:
        print(f"[abort] Estimated runtime {est/60:.1f} min exceeds 30 min budget. Stopping.",
              file=sys.stderr)
        sys.exit(3)

    rows = []
    t_total = time.time()
    for i, seed in enumerate(SEEDS, start=1):
        for name, fn in METHODS:
            row = _run_one(name, fn, seed)
            rows.append(row)
        if i % 10 == 0 or i == 1:
            elapsed = time.time() - t_total
            print(f"[progress] {i}/{N_SEEDS} seeds done, elapsed {elapsed:.1f}s")

    df = pd.DataFrame(rows, columns=[
        "seed", "method", "n_assigned", "total_distance",
        "total_annual_tax_dollars", "avg_util_at_destination", "runtime_seconds",
    ])
    df.to_csv(CSV_PATH, index=False)
    print(f"\n[done] Wrote {len(df)} rows -> {CSV_PATH}")

    print("\n=== Describe() by method (raw, no judgments) ===")
    numeric_cols = [
        "n_assigned", "total_distance",
        "total_annual_tax_dollars", "avg_util_at_destination", "runtime_seconds",
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
