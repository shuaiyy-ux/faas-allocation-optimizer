"""End-to-end allocation pipeline using the bucket scorer.

This module is the user-facing entry point for the bucket algorithm. It
imports the algorithm block from `app/scoring/bucket.py` and the data
plumbing from `app/engine.py`, but it does NOT modify `engine.py`. The
additive scoring path in `engine.solve_both` is preserved unchanged so
the two algorithms can run side by side for A/B comparison.

Design notes
------------
* The ILP solver (lexicographic two-stage) is replicated here rather
  than imported from `engine._solve_v2` to keep `engine.py` untouched.
  The duplication is intentional and scoped to ~60 lines.
* Dynamic constraints loaded by `engine._solve_v2` via
  `constraints.load_all` are intentionally NOT applied in this pipeline.
  Bucket scoring is in calibration mode, surfacing custom constraint
  side effects would muddy the A/B comparison. Re-enable once the bucket
  scorer is the production path.
* No CSV writes, no session mutation. This is a pure read path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

import pandas as pd
from pulp import (
    LpBinary,
    LpMaximize,
    LpProblem,
    LpVariable,
    lpSum,
    value,
)

from engine import (
    DEFAULT_EXPECTED_STAY_MONTHS,
    SOLVER,
    _data_for_session,
    _build_reasoning,
    load_baseline_fleet,
)
import numpy as np
from scoring import (
    BucketParams,
    DEFAULT_BUCKET_MULTS,
    compute_dealer_util_scores,
    score_pair,
    tier_of,
)

if TYPE_CHECKING:
    from state import SessionState


def _build_pairs_bucket(
    veh: pd.DataFrame,
    dealer: pd.DataFrame,
    arc_dist: Dict,
    source_to_dealers: Dict,
    params: BucketParams,
    expected_stay_months: int,
    distance_norm: float,
    tax_norm: float,
):
    """Build (v, d) alloc_score map under the bucket form.

    Returns
    -------
    alloc_score      dict[(VIN, DEALER_CODE)] -> float
    vin_to_dealers   dict[VIN] -> list[DEALER_CODE]
    dealer_to_vins   dict[DEALER_CODE] -> list[VIN]
    rem_cap          dict[DEALER_CODE] -> int     (slot constraint, unchanged)
    vin_source       dict[VIN] -> SOURCE
    dealer_util      dict[DEALER_CODE] -> bucket_mult  (per-dealer util score)
    dealer_ptax      dict[DEALER_CODE] -> property tax rate
    """
    dealer_codes = set(dealer["DEALER_CODE"])
    dealer_ptax = dealer.set_index("DEALER_CODE")["PROP_TAX_RATE"].to_dict()
    dealer_util = compute_dealer_util_scores(dealer, params)
    rem_cap = dealer.set_index("DEALER_CODE")["REMAINING_CAPACITY"].to_dict()

    alloc_score: Dict = {}
    vin_to_dealers: Dict = {}
    dealer_to_vins: Dict = {d: [] for d in dealer_codes}
    vin_source = veh.set_index("VIN")["SOURCE"].to_dict()

    for _, row in veh.iterrows():
        v, src, res = row["VIN"], row["SOURCE"], row["RESIDUAL"]
        vd = []
        for d in source_to_dealers.get(src, []):
            if d in dealer_codes:
                dist = arc_dist.get((src, d), 0)
                ptax = dealer_ptax.get(d, 0) * res * expected_stay_months / 12
                alloc_score[(v, d)] = score_pair(
                    util_score=dealer_util[d],
                    distance=dist,
                    prop_tax=ptax,
                    params=params,
                    distance_norm=distance_norm,
                    tax_norm=tax_norm,
                )
                vd.append(d)
                dealer_to_vins[d].append(v)
        vin_to_dealers[v] = vd

    return alloc_score, vin_to_dealers, dealer_to_vins, rem_cap, vin_source, dealer_util, dealer_ptax


def _solve_v2_bucket(
    veh: pd.DataFrame,
    data: Dict[str, Any],
    params: BucketParams,
    expected_stay_months: int,
):
    """Lexicographic two-stage ILP using bucket-scored alloc values.

    Stage 1 maximizes the number of assignments. Stage 2 maximizes total
    alloc_score subject to assignment count = Stage 1 optimum. Replicates
    `engine._solve_v2` minus the dynamic-constraint loader.
    """
    dealer = data["dealer"]
    arc_dist = data["arc_dist"]
    source_to_dealers = data["source_to_dealers"]

    nv, v2d, d2v, rem_cap, _vin_source, _dealer_util, _dealer_ptax = _build_pairs_bucket(
        veh, dealer, arc_dist, source_to_dealers, params,
        expected_stay_months, data["distance_norm"], data["tax_norm"],
    )
    dealer_codes = set(dealer["DEALER_CODE"])

    # Stage 1: maximize assignments
    m1 = LpProblem("S1_bucket", LpMaximize)
    x1 = {k: LpVariable(f"x1_{k[0]}_{k[1]}", cat=LpBinary) for k in nv}
    u1 = {v: LpVariable(f"u1_{v}", cat=LpBinary) for v in veh["VIN"]}
    m1 += lpSum(x1.values())
    for v in veh["VIN"]:
        m1 += lpSum(x1[v, d] for d in v2d[v]) + u1[v] == 1
    for d in dealer_codes:
        if d2v[d]:
            m1 += lpSum(x1[v, d] for v in d2v[d]) <= rem_cap[d]
    m1.solve(SOLVER)
    max_assigned = int(round(value(m1.objective)))

    # Stage 2: maximize total alloc_score given assignment count
    m2 = LpProblem("S2_bucket", LpMaximize)
    x2 = {k: LpVariable(f"x2_{k[0]}_{k[1]}", cat=LpBinary) for k in nv}
    u2 = {v: LpVariable(f"u2_{v}", cat=LpBinary) for v in veh["VIN"]}
    m2 += lpSum(nv[k] * x2[k] for k in nv)
    for v in veh["VIN"]:
        m2 += lpSum(x2[v, d] for d in v2d[v]) + u2[v] == 1
    for d in dealer_codes:
        if d2v[d]:
            m2 += lpSum(x2[v, d] for v in d2v[d]) <= rem_cap[d]
    m2 += lpSum(x2.values()) == max_assigned
    m2.solve(SOLVER)

    allocs = [
        {"VIN": v, "DEALER_CODE": d, "NET_VALUE": nv[(v, d)]}
        for (v, d), var in x2.items()
        if var.varValue and var.varValue > 0.5
    ]
    return pd.DataFrame(allocs)


def _enrich_bucket(
    alloc_df: pd.DataFrame,
    veh: pd.DataFrame,
    data: Dict[str, Any],
    params: BucketParams,
    expected_stay_months: int,
) -> pd.DataFrame:
    """Attach per-row score components for downstream reporting.

    Adds DISTANCE, PROP_TAX, UTILIZATION_SCORE, ALLOC_SCORE, BUCKET_TIER,
    plus the usual descriptive dealer fields. Matches the column set
    produced by `engine._enrich` so existing reporting helpers can be
    pointed at this output without changes.
    """
    if alloc_df.empty:
        return alloc_df
    dealer = data["dealer"]
    arc_dist = data["arc_dist"]
    distance_norm = data["distance_norm"]
    tax_norm = data["tax_norm"]
    di = dealer.set_index("DEALER_CODE")
    vr = veh.set_index("VIN")

    alloc_df["SOURCE"] = alloc_df["VIN"].map(vr["SOURCE"].to_dict())
    alloc_df["RESIDUAL"] = alloc_df["VIN"].map(vr["RESIDUAL"].to_dict())
    if "DISTANCE" not in alloc_df.columns:
        alloc_df["DISTANCE"] = alloc_df.apply(
            lambda r: arc_dist.get((r["SOURCE"], r["DEALER_CODE"]), 0), axis=1
        )
    alloc_df["DEALER_NAME"] = alloc_df["DEALER_CODE"].map(di["DEALER_NAME"])
    alloc_df["DEALER_STATE"] = alloc_df["DEALER_CODE"].map(di["STATE"])
    alloc_df["UTIL_RATE"] = alloc_df["DEALER_CODE"].map(di["UTIL_RATE"])
    alloc_df["IN_SERVICE"] = alloc_df["DEALER_CODE"].map(di["IN_SERVICE"]).fillna(0.0)
    alloc_df["RENTED"] = alloc_df["DEALER_CODE"].map(di["RENTED"]).fillna(0.0)
    alloc_df["PROP_TAX_RATE"] = alloc_df["DEALER_CODE"].map(di["PROP_TAX_RATE"])
    alloc_df["PROP_TAX"] = (
        alloc_df["PROP_TAX_RATE"] * alloc_df["RESIDUAL"] * expected_stay_months / 12
    )

    dealer_util = compute_dealer_util_scores(dealer, params)
    max_signal = float(dealer[params.signal_field].fillna(0).max())
    signal_by_code = dealer.set_index("DEALER_CODE")[params.signal_field].fillna(0).to_dict()

    alloc_df["UTILIZATION_SCORE"] = alloc_df["DEALER_CODE"].map(dealer_util)
    alloc_df["BUCKET_TIER"] = alloc_df["DEALER_CODE"].map(
        lambda c: tier_of(float(signal_by_code.get(c, 0.0)), max_signal, params.n_buckets)
    )
    alloc_df["ALLOC_SCORE"] = (
        alloc_df["UTILIZATION_SCORE"]
        - params.w_dist * alloc_df["DISTANCE"] / distance_norm
        - params.w_tax * alloc_df["PROP_TAX"] / tax_norm
    )
    alloc_df["DEALER_LAT"] = alloc_df["DEALER_CODE"].map(di["LATITUDE"])
    alloc_df["DEALER_LON"] = alloc_df["DEALER_CODE"].map(di["LONGITUDE"])
    return alloc_df


def solve_bucket(
    n_vehicles: Optional[int] = None,
    params: Optional[BucketParams] = None,
    expected_stay_months: int = DEFAULT_EXPECTED_STAY_MONTHS,
    session: Optional["SessionState"] = None,
) -> Dict[str, Any]:
    """Run V2 ILP under bucket scoring and return a JSON-safe result.

    Parameters
    ----------
    n_vehicles
        Optional subsample size, deterministic via seed=42 to match
        `engine.solve_both`.
    params
        Bucket knobs. Defaults to `BucketParams()` (signal=IN_SERVICE,
        mults=DEFAULT_BUCKET_MULTS, i.e. the calibrated
        (3.7203, 2.8848, 0.7957, 0.6498) from the 2026-05-22 Sobol-knee sweep).
    expected_stay_months
        Forwarded to the property-tax accrual computation.
    session
        Optional `SessionState` for capacity overlay. Constraint plugins
        are intentionally not applied; see module docstring.
    """
    params = params or BucketParams()
    data = _data_for_session(session)
    veh = data["veh"].copy()
    if n_vehicles and n_vehicles < len(veh):
        veh = veh.sample(n=n_vehicles, random_state=42).reset_index(drop=True)

    raw = _solve_v2_bucket(veh, data, params, expected_stay_months)
    enriched = _enrich_bucket(raw, veh, data, params, expected_stay_months)

    n_assigned = int(len(enriched))
    total_alloc = float(enriched["ALLOC_SCORE"].sum()) if n_assigned else 0.0
    total_distance = float(enriched["DISTANCE"].sum()) if n_assigned else 0.0

    return {
        "method": "bucket_v2",
        "params": {
            "n_vehicles": len(veh),
            "bucket_mults": list(params.bucket_mults),
            "signal_field": params.signal_field,
            "w_dist": params.w_dist,
            "w_tax": params.w_tax,
            "distance_norm": round(data["distance_norm"], 2),
            "tax_norm": round(data["tax_norm"], 2),
            "expected_stay_months": expected_stay_months,
        },
        "n_assigned": n_assigned,
        "total_alloc_score": round(total_alloc, 4),
        "total_distance": round(total_distance, 1),
        "alloc": enriched.round(4).to_dict("records") if n_assigned else [],
    }


def solve_weekly_bucket(
    n_batch: int = 10,
    seed: Optional[int] = None,
    bucket_mults: Optional[Sequence[float]] = None,
    signal_field: str = "IN_SERVICE",
    w_dist: Optional[float] = None,
    w_tax: Optional[float] = None,
    vin_list: Optional[list] = None,
    expected_stay_months: int = DEFAULT_EXPECTED_STAY_MONTHS,
    session: Optional["SessionState"] = None,
) -> Dict[str, Any]:
    """Weekly batch allocation under bucket scoring.

    Mirrors `engine.solve_weekly_batch`'s output shape so the frontend
    weekly view can render bucket results without code changes. The
    differences are:

    * `method` is `"bucket_ilp"`.
    * `scoring_mode` is `"bucket"`.
    * `params` carries `bucket_mults` and `signal_field` instead of
      `w_util` and `w_rented`. `w_dist` and `w_tax` are still present.
    """
    params = BucketParams(
        bucket_mults=tuple(bucket_mults) if bucket_mults else tuple(DEFAULT_BUCKET_MULTS),
        signal_field=signal_field,
        w_dist=15.0 if w_dist is None else float(w_dist),
        w_tax=1.950 if w_tax is None else float(w_tax),
    )

    data = _data_for_session(session)
    veh = data["veh"]
    dealer = data["dealer"]
    arc_dist = data["arc_dist"]
    source_to_dealers = data["source_to_dealers"]
    grounding = data["grounding"]

    if vin_list and len(vin_list) > 0:
        unknown = [v for v in vin_list if v not in set(veh["VIN"])]
        if unknown:
            preview = ", ".join(unknown[:5]) + ("..." if len(unknown) > 5 else "")
            return _empty_bucket_batch(
                error=f"Unknown VINs (not in faas_eligible_vehicles): {preview}",
                params=params, expected_stay_months=expected_stay_months,
                distance_norm=data["distance_norm"], tax_norm=data["tax_norm"], seed=seed,
            )
        fleet = session.fleet_df if session is not None else load_baseline_fleet()
        fleet_sel = fleet[fleet["VIN"].isin(vin_list)].reset_index(drop=True)
        if fleet_sel.empty:
            return _empty_bucket_batch(
                error="No matching VINs found in fleet snapshot",
                params=params, expected_stay_months=expected_stay_months,
                distance_norm=data["distance_norm"], tax_norm=data["tax_norm"], seed=seed,
            )
        batch = fleet_sel[["VIN", "SOURCE", "RESIDUAL"]].copy()
    else:
        rng = np.random.RandomState(seed)
        batch = veh.sample(n=min(n_batch, len(veh)), random_state=rng).reset_index(drop=True)

    raw = _solve_v2_bucket(batch, data, params, expected_stay_months)
    enriched = _enrich_bucket(raw, batch, data, params, expected_stay_months)

    nv, v2d, _, _, _, dealer_util_score, dealer_ptax = _build_pairs_bucket(
        batch, dealer, arc_dist, source_to_dealers, params,
        expected_stay_months, data["distance_norm"], data["tax_norm"],
    )

    di = dealer.set_index("DEALER_CODE")
    src_loc = grounding.set_index("SOURCE")[["SOURCE_LAT", "SOURCE_LON", "CITY", "STATE"]].to_dict("index")
    assigned_map = enriched.set_index("VIN").to_dict("index") if not enriched.empty else {}

    vehicles = []
    for _, row in batch.iterrows():
        vin = row["VIN"]
        src = row["SOURCE"]
        residual = row["RESIDUAL"]
        src_info = src_loc.get(src, {})

        candidates = []
        for d in v2d.get(vin, []):
            dist = arc_dist.get((src, d), 0)
            prop_tax = dealer_ptax.get(d, 0) * residual * expected_stay_months / 12
            util_score = dealer_util_score.get(d, 0)
            net = nv.get((vin, d), 0)
            dr = di.loc[d] if d in di.index else None
            candidates.append({
                "dealer_code": d,
                "dealer_name": dr["DEALER_NAME"] if dr is not None else d,
                "state": dr["STATE"] if dr is not None else "",
                "lat": float(dr["LATITUDE"]) if dr is not None else 0,
                "lon": float(dr["LONGITUDE"]) if dr is not None else 0,
                "distance": round(dist, 1),
                "prop_tax": round(prop_tax, 2),
                "prop_tax_rate": round(dealer_ptax.get(d, 0) * 100, 2),
                "utilization_score": round(util_score, 4),
                "alloc_score": round(net, 4),
                "utilization": round(float(dr["UTIL_RATE"]) * 100, 1) if dr is not None else 0,
                "rented": int(dr["RENTED"]) if dr is not None else 0,
                "in_service": int(dr["IN_SERVICE"]) if dr is not None else 0,
                "remaining_capacity": int(dr["REMAINING_CAPACITY"]) if dr is not None else 0,
            })
        candidates.sort(key=lambda c: c["alloc_score"], reverse=True)
        for i, c in enumerate(candidates, start=1):
            c["rank"] = i

        asgn = assigned_map.get(vin)
        assigned_dealer = asgn["DEALER_CODE"] if asgn else None
        assigned_info = next((c for c in candidates if c["dealer_code"] == assigned_dealer), None)
        reasoning = _build_reasoning(assigned_info, candidates) if assigned_info else "Unassigned — no feasible dealer with remaining capacity."

        vehicles.append({
            "vin": vin,
            "source": src,
            "source_city": src_info.get("CITY", ""),
            "source_state": src_info.get("STATE", ""),
            "source_lat": src_info.get("SOURCE_LAT", 39.0),
            "source_lon": src_info.get("SOURCE_LON", -98.0),
            "residual": round(residual, 2),
            "assigned": assigned_info,
            "alternatives": candidates,
            "reasoning": reasoning,
            "assigned_dealer": assigned_dealer,
        })

    n_assigned = sum(1 for v in vehicles if v["assigned"])
    total_alloc_score = sum(v["assigned"]["alloc_score"] for v in vehicles if v["assigned"])
    unique_arcs = {}
    for v in vehicles:
        if v["assigned"]:
            key = (v["source"], v["assigned"]["dealer_code"])
            unique_arcs[key] = v["assigned"]["distance"]
    total_distance = sum(unique_arcs.values())

    ceiling = sum(
        max((nv[(vin, d)] for d in v2d[vin]), default=0)
        for vin in (row["VIN"] for _, row in batch.iterrows())
        if v2d.get(vin)
    )
    quality_pct = round(max(0.0, min(100.0, total_alloc_score / ceiling * 100)), 1) if ceiling > 0 else 0.0

    assigned_ranks = [v["assigned"]["rank"] for v in vehicles if v["assigned"]]
    avg_rank = round(sum(assigned_ranks) / len(assigned_ranks), 2) if assigned_ranks else 0.0
    rank1_count = sum(1 for r in assigned_ranks if r == 1)
    rank1_pct = round(rank1_count / len(assigned_ranks) * 100, 1) if assigned_ranks else 0.0

    return {
        "batch_size": len(batch),
        "seed": seed,
        "n_assigned": n_assigned,
        "total_alloc_score": round(total_alloc_score, 4),
        "total_distance": round(total_distance, 1),
        "ceiling": round(ceiling, 4),
        "quality_pct": quality_pct,
        "avg_rank": avg_rank,
        "rank1_count": rank1_count,
        "rank1_pct": rank1_pct,
        "scoring_mode": "bucket",
        "params": {
            "bucket_mults": list(params.bucket_mults),
            "signal_field": params.signal_field,
            "w_dist": params.w_dist,
            "w_tax": params.w_tax,
            "distance_norm": round(data["distance_norm"], 2),
            "tax_norm": round(data["tax_norm"], 2),
            "expected_stay_months": expected_stay_months,
        },
        "vehicles": vehicles,
        "method": "bucket_ilp",
    }


def _empty_bucket_batch(error: str, params: BucketParams, expected_stay_months: int,
                        distance_norm: float, tax_norm: float, seed: Optional[int]) -> Dict[str, Any]:
    """Bucket-mode counterpart to engine._empty_batch_result. Same keys, scoring_mode tag."""
    return {
        "batch_size": 0,
        "seed": seed,
        "n_assigned": 0,
        "total_alloc_score": 0.0,
        "total_distance": 0.0,
        "ceiling": 0.0,
        "quality_pct": 0.0,
        "avg_rank": 0.0,
        "rank1_count": 0,
        "rank1_pct": 0.0,
        "scoring_mode": "bucket",
        "params": {
            "bucket_mults": list(params.bucket_mults),
            "signal_field": params.signal_field,
            "w_dist": params.w_dist,
            "w_tax": params.w_tax,
            "distance_norm": round(distance_norm, 2),
            "tax_norm": round(tax_norm, 2),
            "expected_stay_months": expected_stay_months,
        },
        "vehicles": [],
        "method": "bucket_ilp",
        "error": error,
    }
