"""Sobol quasi-random sweep over (bucket_mults, w_dist, w_tax).

Sample space
------------
    bucket_mults = (A, B, C, D) ∈ [0.1, 5.0]⁴   sampled then sorted desc
    w_dist                       ∈ [0.5, 30.0]
    w_tax                        ∈ [0.5, 5.0]

6-D Sobol. The first four dimensions are sorted descending per sample
so the bucket vector is monotone, A >= B >= C >= D. Strict equality is
allowed.

Per-point evaluation
--------------------
Each (mults, w_dist, w_tax) is evaluated against FLEET_SIZES x SEEDS
scenarios. Default: 8 sizes × 10 seeds = 80 scenarios per point.

Output
------
Long-form parquet with one row per (point_id, fleet_seed, batch_size)
cell. Knee selection is deliberately NOT performed here; the user
inspects raw output and picks the knee manually (or via a separate
analysis script).

Usage
-----
    .venv/bin/python sensitivity_analysis/bucket/sweep_bucket.py \\
        --points 64 --sizes 30 40 --seeds 3 \\
        --output sensitivity_analysis/bucket/output/sweep_smoke.parquet

For a full production sweep, use --points 1024 --workers 8 and the
default sizes/seeds. Expect ~30-60 min wall clock on a 2024 Mac.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import qmc

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

# Sample ranges
MULT_RANGE = (0.1, 5.0)
W_DIST_RANGE = (0.5, 30.0)   # current production w_dist=15.0 sits in the middle
W_TAX_RANGE = (0.5, 5.0)

# Default scenario grid
DEFAULT_FLEET_SIZES = [25, 30, 35, 40, 45, 50, 55, 60]
DEFAULT_SEEDS_PER_SIZE = 10


def build_sobol_points(n_points: int, seed: int = 42) -> np.ndarray:
    """6-D Sobol points. Returns shape (n_points, 6)."""
    m = int(math.ceil(math.log2(max(n_points, 2))))
    sampler = qmc.Sobol(d=6, scramble=True, seed=seed)
    unit = sampler.random_base2(m=m)[:n_points]

    lo = np.array([
        MULT_RANGE[0], MULT_RANGE[0], MULT_RANGE[0], MULT_RANGE[0],
        W_DIST_RANGE[0], W_TAX_RANGE[0],
    ])
    hi = np.array([
        MULT_RANGE[1], MULT_RANGE[1], MULT_RANGE[1], MULT_RANGE[1],
        W_DIST_RANGE[1], W_TAX_RANGE[1],
    ])
    raw = lo + unit * (hi - lo)

    # Sort the first 4 dims descending per row so (A, B, C, D) is monotone.
    sorted_mults = -np.sort(-raw[:, :4], axis=1)
    raw[:, :4] = sorted_mults
    return raw


def _worker(args):
    """Process-pool target. Lazy-imports the bucket runner."""
    from run_single import run_one
    (A, B, C, D, w_dist, w_tax,
     fleet_seed, n_batch, point_id) = args
    t0 = time.time()
    r = run_one(
        bucket_mults=(float(A), float(B), float(C), float(D)),
        w_dist=float(w_dist),
        w_tax=float(w_tax),
        fleet_seed=int(fleet_seed),
        n_batch=int(n_batch),
    )
    r["point_id"] = int(point_id)
    r["batch_size"] = int(n_batch)
    r["runtime_sec"] = round(time.time() - t0, 3)
    return r


def build_task_grid(points: np.ndarray, sizes, seeds_per_size):
    tasks = []
    for pid, (A, B, C, D, wd, wt) in enumerate(points):
        for size in sizes:
            for s in range(seeds_per_size):
                fleet_seed = size * 1000 + s
                tasks.append((A, B, C, D, wd, wt, fleet_seed, size, pid))
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--points", type=int, default=64, help="Sobol points (power of 2)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sizes", type=int, nargs="+", default=DEFAULT_FLEET_SIZES)
    parser.add_argument("--seeds", type=int, default=DEFAULT_SEEDS_PER_SIZE,
                        help="seeds per size")
    parser.add_argument("--output", type=str,
                        default="sensitivity_analysis/bucket/output/sweep_bucket.parquet")
    args = parser.parse_args()

    points = build_sobol_points(args.points)
    tasks = build_task_grid(points, args.sizes, args.seeds)
    total = len(tasks)
    print(f"[{datetime.now():%H:%M:%S}] sweep starting "
          f"points={args.points} sizes={args.sizes} seeds={args.seeds} "
          f"tasks={total} workers={args.workers}")

    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(_worker, t) for t in tasks]
        for i, fut in enumerate(as_completed(futs), start=1):
            rows.append(fut.result())
            if i % 50 == 0 or i == total:
                print(f"[{datetime.now():%H:%M:%S}] {i}/{total} done")

    df = pd.DataFrame(rows)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    print(f"[{datetime.now():%H:%M:%S}] wrote {len(df)} rows -> {out_path}")
    print(f"unique points: {df['point_id'].nunique()}")
    print(f"mean rank1_pct: {df['rank1_pct'].mean():.2f}")
    print(f"mean hhi:        {df['hhi_concentration'].mean():.4f}")


if __name__ == "__main__":
    main()
