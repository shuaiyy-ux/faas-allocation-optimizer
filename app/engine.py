"""
FaaS Vehicle Allocation Engine
Data loading + Greedy / V2 ILP solvers + JSON-serializable outputs

Session model: the baseline is loaded once at startup (`load_baseline()`)
and is immutable for the process lifetime. Mutable state (a per-session
fleet snapshot and per-session constraint plugins) lives on a
`SessionState` from `app/state.py`. Every solver entry point accepts an
optional `session: SessionState | None` keyword; when supplied, the
function overlays the session's `fleet_df` to compute IN_TRANSIT and
applies the session's compiled constraints. When `None`, the baseline
is used unchanged — useful for tests and read-only callers.
"""

from typing import TYPE_CHECKING, Any, Dict, Optional

import pandas as pd
import numpy as np
from pathlib import Path
from pulp import (
    LpProblem, LpMaximize, LpVariable, LpBinary,
    lpSum, value, PULP_CBC_CMD,
)
from constraints import load_all as load_constraints

if TYPE_CHECKING:
    from state import SessionState, SessionStore

DATA_DIR = Path(__file__).resolve().parent.parent / "data_csv"
SOLVER = PULP_CBC_CMD(msg=0)

# Default hyperparameters for the allocation score.
#
# Formula (2026-04-21 pivot — additive two-signal form):
#   alloc_score = w_util × UTIL_RATE
#               + w_rented × RENTED
#               − w_dist × distance(v,d) / DISTANCE_NORM
#               − w_tax  × tax_over_stay(v,d) / TAX_NORM
#
# UTIL and RENTED enter additively — RENTED is NOT normalized because the client
# 4/17 explicitly said absolute demand is invariant (a 200-RENTED dealer is a
# 200-RENTED dealer regardless of other dealers' data). DISTANCE_NORM and
# TAX_NORM are scaffolding — they'll collapse once HCA shares $/mile and
# per-car monthly revenue.
DEFAULT_W_UTIL = 1.335     # 4-D Pareto knee (2026-04-21 4-D sweep)
DEFAULT_W_RENTED = 0.0574  # 4-D Pareto knee (was 0.031 under the earlier 1-D sweep)
DEFAULT_W_DIST = 15.0      # 2026-04-24 cost-aware override — distance dominates the $ calculus (carrier costs >> tax costs per vehicle). Was 3.827 (4-D knee) before the override.
DEFAULT_W_TAX = 1.950      # 4-D Pareto knee (was 1.0 scaffolding)
DEFAULT_EXPECTED_STAY_MONTHS = 12


def _empty_batch_result(error_msg, method,
                        w_util=DEFAULT_W_UTIL, w_rented=DEFAULT_W_RENTED,
                        w_dist=DEFAULT_W_DIST, w_tax=DEFAULT_W_TAX,
                        source_limit=None,
                        expected_stay_months=DEFAULT_EXPECTED_STAY_MONTHS,
                        seed=None):
    """Empty batch result that matches the success-path schema.
    Tools (compare_ilp_vs_greedy, analyze_capacity_change) read fields like
    n_assigned, total_distance, etc. unconditionally — the early-return path
    must carry the same keys to avoid KeyError."""
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
        "params": {
            "w_util": w_util, "w_rented": w_rented,
            "miles_per_util": None,
            "dollars_per_util": None,
            "w_dist": w_dist, "w_tax": w_tax,
            "distance_norm": None,
            "tax_norm": None,
            "source_limit": source_limit,
            "expected_stay_months": expected_stay_months,
        },
        "vehicles": [],
        "method": method,
        "error": error_msg,
    }


# ── City Coordinates ────────────────────────────────────────────────

def _load_zip_centroids():
    """ZIP -> (lat, lon) from GeoNames US postal + manually-added entries.
    Exact lookup only; missing zips raise — no silent fallback."""
    path = DATA_DIR / "zip_centroids.csv"
    df = pd.read_csv(path, dtype={"ZIPCODE": str})
    df["ZIPCODE"] = df["ZIPCODE"].str.zfill(5)
    return {z: (lat, lon) for z, lat, lon in zip(df["ZIPCODE"], df["LATITUDE"], df["LONGITUDE"])}

# ── Data Loading ────────────────────────────────────────────────────

_baseline_cache: Dict[str, Any] = {}


def load_baseline() -> Dict[str, Any]:
    """Read all static CSVs once and cache. The returned dict is **immutable**
    in spirit — callers must not mutate any of its dataframes in place.
    `IN_TRANSIT_COUNT` on the dealer df is always 0 here; per-session capacity
    is computed lazily via :func:`compute_session_dealer`.
    """
    if _baseline_cache:
        return _baseline_cache

    vehicles_raw = pd.read_csv(DATA_DIR / "faas_eligible_vehicles.csv", dtype={"ZIPCODE": str})
    vehicles_raw["ZIPCODE"] = vehicles_raw["ZIPCODE"].str.zfill(5)
    inventory_raw = pd.read_csv(DATA_DIR / "dealer_inventory.csv", dtype={"ZIPCODE": str})
    inventory_raw["ZIPCODE"] = inventory_raw["ZIPCODE"].str.zfill(5)
    utilization_raw = pd.read_csv(DATA_DIR / "dealer_utilization.csv")
    distance_raw = pd.read_csv(DATA_DIR / "dealer_distance_matrix.csv")
    prop_tax_raw = pd.read_csv(DATA_DIR / "property_tax_by_state.csv")
    zip_centroids = _load_zip_centroids()

    # Vehicles
    veh = vehicles_raw[["VIN", "DEALER_CODE", "RESIDUAL"]].copy()
    veh.rename(columns={"DEALER_CODE": "SOURCE"}, inplace=True)

    # Dealers — baseline IN_TRANSIT_COUNT = 0; the overlay is per-session.
    dealer = inventory_raw[["DEALER_CODE", "DEALER_NAME", "STATE", "ZIPCODE",
                            "TRUE_CAPACITY", "DELIVERED_COUNT",
                            "LATITUDE", "LONGITUDE"]].copy()
    dealer["REMAINING_CAPACITY"] = dealer["TRUE_CAPACITY"] - dealer["DELIVERED_COUNT"]
    dealer["IN_TRANSIT_COUNT"] = 0

    util = utilization_raw[["DEALER_ID", "UTILIZATION", "IN_SERVICE", "RENTED"]].copy()
    util.rename(columns={"DEALER_ID": "DEALER_CODE"}, inplace=True)
    util["UTIL_RATE"] = util["UTILIZATION"] / 100.0
    dealer = dealer.merge(
        util[["DEALER_CODE", "UTIL_RATE", "IN_SERVICE", "RENTED"]], on="DEALER_CODE", how="left"
    )
    dealer["IN_SERVICE"] = dealer["IN_SERVICE"].fillna(0).astype(float)
    # RENTED is the primary demand signal in the scoring formula (2026-04-21).
    # IN_SERVICE is retained purely for operational display / reporting.
    dealer["RENTED"] = dealer["RENTED"].fillna(0).astype(float)

    # Property tax
    ptax = prop_tax_raw.copy()
    ptax["PROP_TAX_RATE"] = ptax["PROPERTY_TAX_RATE"].str.replace("%", "").astype(float) / 100.0
    ptax_map = ptax.set_index("STATE")["PROP_TAX_RATE"].to_dict()
    dealer["PROP_TAX_RATE"] = dealer["STATE"].map(ptax_map).fillna(0)

    # Grounding dealer locations — lat/lon from ZIP centroid (Census 2020 ZCTA)
    grounding = vehicles_raw[["DEALER_CODE", "LOCATION", "STATE", "ZIPCODE"]].drop_duplicates("DEALER_CODE")
    grounding.rename(columns={"DEALER_CODE": "SOURCE"}, inplace=True)
    grounding["CITY"] = grounding["LOCATION"].str.split(",").str[0].str.strip()
    _missing = grounding.loc[~grounding["ZIPCODE"].isin(zip_centroids), ["SOURCE", "ZIPCODE", "CITY", "STATE"]]
    if len(_missing):
        raise ValueError(
            f"{len(_missing)} grounding zip(s) not in data_csv/zip_centroids.csv. "
            f"Look them up manually and append to the CSV:\n{_missing.to_string(index=False)}"
        )
    grounding["SOURCE_LAT"] = grounding["ZIPCODE"].map(lambda z: zip_centroids[z][0])
    grounding["SOURCE_LON"] = grounding["ZIPCODE"].map(lambda z: zip_centroids[z][1])

    # Distance arcs
    arcs = distance_raw.rename(columns={
        "GROUNDING_DEALER_CODE": "SOURCE", "FAAS_DEALER_CODE": "DEALER_CODE",
    })
    arc_dist = arcs.set_index(["SOURCE", "DEALER_CODE"])["DISTANCE_MILES"].to_dict()
    source_to_dealers = arcs.groupby("SOURCE")["DEALER_CODE"].apply(sorted).apply(list).to_dict()

    # Normalization constants. Each cost term is divided by its worst observed
    # value so the three score components live on [0, 1]. Both are computed
    # against the current dataset — if the data changes shape, these constants
    # shift accordingly, which is fine because only relative rankings matter.
    distance_norm = max(arc_dist.values()) if arc_dist else 1.0
    max_ptax = float(dealer["PROP_TAX_RATE"].max())
    max_residual = float(veh["RESIDUAL"].max())
    tax_norm = (max_ptax * max_residual * DEFAULT_EXPECTED_STAY_MONTHS / 12) or 1.0

    _baseline_cache.update({
        "veh": veh, "dealer": dealer, "grounding": grounding,
        "arc_dist": arc_dist, "source_to_dealers": source_to_dealers,
        "distance_norm": distance_norm, "tax_norm": tax_norm,
    })
    return _baseline_cache


def load_baseline_fleet() -> pd.DataFrame:
    """Read `fleet_inventory_original.csv` — the immutable per-session seed."""
    original = DATA_DIR / "fleet_inventory_original.csv"
    return pd.read_csv(original)


def compute_session_dealer(baseline_dealer: pd.DataFrame,
                           session_fleet_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return a copy of `baseline_dealer` with IN_TRANSIT_COUNT and
    REMAINING_CAPACITY overlaid from this session's fleet snapshot."""
    dealer = baseline_dealer.copy()
    if session_fleet_df is None or len(session_fleet_df) == 0:
        return dealer
    fleet = session_fleet_df.copy()
    fleet["ASSIGNED_DEALER"] = fleet["ASSIGNED_DEALER"].fillna("").astype(str)
    in_transit = fleet[
        (fleet["ASSIGNED_DEALER"] != "")
        & fleet["ASSIGNED_DEALER"].str.startswith("FD")
        & fleet["STATUS"].isin(["Transporting"])
    ]
    transit_counts = in_transit.groupby("ASSIGNED_DEALER").size()
    dealer["IN_TRANSIT_COUNT"] = dealer["DEALER_CODE"].map(transit_counts).fillna(0).astype(int)
    dealer["REMAINING_CAPACITY"] = (
        dealer["REMAINING_CAPACITY"] - dealer["IN_TRANSIT_COUNT"]
    ).clip(lower=0)
    return dealer


def _data_for_session(session: Optional["SessionState"]) -> Dict[str, Any]:
    """Assemble the `data` dict every solver expects, with session overlay."""
    baseline = load_baseline()
    fleet_df = session.fleet_df if session is not None else None
    dealer = compute_session_dealer(baseline["dealer"], fleet_df)
    return {**baseline, "dealer": dealer}


# Back-compat alias for callers that still use the old `load_data` name.
# Returns the *baseline* — no per-session capacity overlay. Prefer
# `_data_for_session(session)` in new code.
def load_data() -> Dict[str, Any]:
    return load_baseline()


def get_overview(session: Optional["SessionState"] = None):
    d = _data_for_session(session)
    dealer = d["dealer"]
    return {
        "total_vehicles": len(d["veh"]),
        "total_dealers": len(dealer),
        "total_capacity": int(dealer["REMAINING_CAPACITY"].sum()),
        "dealers": dealer[["DEALER_CODE", "DEALER_NAME", "STATE",
                           "TRUE_CAPACITY", "DELIVERED_COUNT", "IN_TRANSIT_COUNT",
                           "REMAINING_CAPACITY", "UTIL_RATE", "IN_SERVICE", "RENTED",
                           "PROP_TAX_RATE", "LATITUDE", "LONGITUDE"]].to_dict("records"),
        "sources": d["grounding"][["SOURCE", "CITY", "STATE",
                                    "SOURCE_LAT", "SOURCE_LON"]].to_dict("records"),
    }


# ── Solvers ─────────────────────────────────────────────────────────

def _build_pairs(veh, dealer, arc_dist, source_to_dealers, w_util, w_dist, w_tax,
                 expected_stay_months, distance_norm, tax_norm,
                 w_rented=DEFAULT_W_RENTED):
    """Build (v, d) alloc_score map under the 2026-04-21 additive formula.

      util_score(d) = w_util × UTIL_RATE(d) + w_rented × RENTED(d)
      alloc_score(v, d) = util_score(d) − w_dist × dist / DISTANCE_NORM
                                        − w_tax  × tax  / TAX_NORM

    RENTED is NOT normalized — absolute count preserved per client review 2026-04-17
    (a 200-RENTED dealer's score is invariant to other dealers' data).
    """
    dealer_codes = set(dealer["DEALER_CODE"])
    dealer_ptax = dealer.set_index("DEALER_CODE")["PROP_TAX_RATE"].to_dict()
    dealer_util_score = {
        r["DEALER_CODE"]: (
            w_util * float(r["UTIL_RATE"]) + w_rented * float(r["RENTED"])
        )
        for _, r in dealer.iterrows()
    }
    rem_cap = dealer.set_index("DEALER_CODE")["REMAINING_CAPACITY"].to_dict()

    alloc_score, vin_to_dealers, dealer_to_vins = {}, {}, {d: [] for d in dealer_codes}
    vin_source = veh.set_index("VIN")["SOURCE"].to_dict()

    for _, row in veh.iterrows():
        v, src, res = row["VIN"], row["SOURCE"], row["RESIDUAL"]
        vd = []
        for d in source_to_dealers.get(src, []):
            if d in dealer_codes:
                dist = arc_dist.get((src, d), 0)
                ptax = dealer_ptax.get(d, 0) * res * expected_stay_months / 12
                alloc_score[(v, d)] = (
                    dealer_util_score[d]
                    - w_dist * dist / distance_norm
                    - w_tax * ptax / tax_norm
                )
                vd.append(d)
                dealer_to_vins[d].append(v)
        vin_to_dealers[v] = vd

    return alloc_score, vin_to_dealers, dealer_to_vins, rem_cap, vin_source, dealer_util_score, dealer_ptax


def _solve_greedy(veh, data, w_util, w_dist, w_tax, source_limit, expected_stay_months,
                  session: Optional["SessionState"] = None):
    dealer = data["dealer"]
    arc_dist = data["arc_dist"]
    source_to_dealers = data["source_to_dealers"]
    dealer_codes = set(dealer["DEALER_CODE"])
    rem_cap = dealer.set_index("DEALER_CODE")["REMAINING_CAPACITY"].to_dict()
    dealer_ptax = dealer.set_index("DEALER_CODE")["PROP_TAX_RATE"].to_dict()
    vin_source = veh.set_index("VIN")["SOURCE"].to_dict()

    pairs = []
    for _, row in veh.iterrows():
        v, src = row["VIN"], row["SOURCE"]
        for d in sorted(source_to_dealers.get(src, [])):
            if d in dealer_codes:
                pairs.append((v, d, arc_dist.get((src, d), 0)))
    pairs.sort(key=lambda x: (x[2], x[0], x[1]))

    source_shipped = {src: 0 for src in veh["SOURCE"].unique()}
    cap_left = dict(rem_cap)
    assigned, allocs = set(), []

    for v, d, dist in pairs:
        if v in assigned or cap_left.get(d, 0) <= 0:
            continue
        if source_limit and source_shipped.get(vin_source[v], 0) >= source_limit:
            continue
        cap_left[d] -= 1
        assigned.add(v)
        source_shipped[vin_source[v]] = source_shipped.get(vin_source[v], 0) + 1
        allocs.append({"VIN": v, "DEALER_CODE": d, "DISTANCE": dist})

    return pd.DataFrame(allocs)


def _solve_v2(veh, data, w_util, w_dist, w_tax, source_limit, expected_stay_months,
              w_rented=DEFAULT_W_RENTED, session: Optional["SessionState"] = None):
    dealer = data["dealer"]
    arc_dist = data["arc_dist"]
    source_to_dealers = data["source_to_dealers"]
    session_constraints = session.constraints if session is not None else None

    nv, v2d, d2v, rem_cap, vin_source, _, _ = _build_pairs(
        veh, dealer, arc_dist, source_to_dealers, w_util, w_dist, w_tax,
        expected_stay_months, data["distance_norm"], data["tax_norm"],
        w_rented=w_rented,
    )
    dealer_codes = set(dealer["DEALER_CODE"])

    # Stage 1: maximize assignments
    m1 = LpProblem("S1", LpMaximize)
    x1 = {k: LpVariable(f"x1_{k[0]}_{k[1]}", cat=LpBinary) for k in nv}
    u1 = {v: LpVariable(f"u1_{v}", cat=LpBinary) for v in veh["VIN"]}
    m1 += lpSum(x1.values())
    for v in veh["VIN"]:
        m1 += lpSum(x1[v, d] for d in v2d[v]) + u1[v] == 1
    for d in dealer_codes:
        if d2v[d]:
            m1 += lpSum(x1[v, d] for v in d2v[d]) <= rem_cap[d]
    if source_limit:
        for src in source_to_dealers:
            sv = veh[veh["SOURCE"] == src]["VIN"].tolist()
            if sv:
                m1 += lpSum(x1[v, d] for v in sv for d in v2d.get(v, [])) <= source_limit
    # Dynamic constraints (Stage 1)
    for cname, cfn in load_constraints(session_constraints):
        try:
            cfn(m1, x1, veh, dealer, stage=1, rem_cap=rem_cap,
                source_to_dealers=source_to_dealers, arc_dist=arc_dist)
        except Exception as e:
            print(f"[constraint:{cname}] S1 error: {e}")

    m1.solve(SOLVER)
    max_assigned = int(round(value(m1.objective)))

    # Stage 2: maximize allocation score
    m2 = LpProblem("S2", LpMaximize)
    x2 = {k: LpVariable(f"x2_{k[0]}_{k[1]}", cat=LpBinary) for k in nv}
    u2 = {v: LpVariable(f"u2_{v}", cat=LpBinary) for v in veh["VIN"]}
    m2 += lpSum(nv[k] * x2[k] for k in nv)
    for v in veh["VIN"]:
        m2 += lpSum(x2[v, d] for d in v2d[v]) + u2[v] == 1
    for d in dealer_codes:
        if d2v[d]:
            m2 += lpSum(x2[v, d] for v in d2v[d]) <= rem_cap[d]
    m2 += lpSum(x2.values()) == max_assigned
    if source_limit:
        for src in source_to_dealers:
            sv = veh[veh["SOURCE"] == src]["VIN"].tolist()
            if sv:
                m2 += lpSum(x2[v, d] for v in sv for d in v2d.get(v, [])) <= source_limit
    # Dynamic constraints (Stage 2)
    for cname, cfn in load_constraints(session_constraints):
        try:
            cfn(m2, x2, veh, dealer, stage=2, rem_cap=rem_cap,
                source_to_dealers=source_to_dealers, arc_dist=arc_dist)
        except Exception as e:
            print(f"[constraint:{cname}] S2 error: {e}")

    m2.solve(SOLVER)

    allocs = [{"VIN": v, "DEALER_CODE": d, "NET_VALUE": nv[(v, d)]}
              for (v, d), var in x2.items() if var.varValue and var.varValue > 0.5]
    return pd.DataFrame(allocs)


def _enrich(alloc_df, veh, data, w_util, w_dist, w_tax, expected_stay_months,
            w_rented=DEFAULT_W_RENTED):
    """Enrich an alloc df with per-row score components under the 2026-04-21 formula."""
    if alloc_df.empty:
        return alloc_df
    dealer = data["dealer"]
    arc_dist = data["arc_dist"]
    distance_norm = data["distance_norm"]
    tax_norm = data["tax_norm"]
    di = dealer.set_index("DEALER_CODE")
    vr = veh.set_index("VIN")
    vs = vr["SOURCE"].to_dict()
    vres = vr["RESIDUAL"].to_dict()

    alloc_df["SOURCE"] = alloc_df["VIN"].map(vs)
    alloc_df["RESIDUAL"] = alloc_df["VIN"].map(vres)
    if "DISTANCE" not in alloc_df.columns:
        alloc_df["DISTANCE"] = alloc_df.apply(
            lambda r: arc_dist.get((r["SOURCE"], r["DEALER_CODE"]), 0), axis=1)
    alloc_df["DEALER_NAME"] = alloc_df["DEALER_CODE"].map(di["DEALER_NAME"])
    alloc_df["DEALER_STATE"] = alloc_df["DEALER_CODE"].map(di["STATE"])
    alloc_df["UTIL_RATE"] = alloc_df["DEALER_CODE"].map(di["UTIL_RATE"])
    alloc_df["IN_SERVICE"] = alloc_df["DEALER_CODE"].map(di["IN_SERVICE"]).fillna(0.0)
    alloc_df["RENTED"] = alloc_df["DEALER_CODE"].map(di["RENTED"]).fillna(0.0)
    alloc_df["PROP_TAX_RATE"] = alloc_df["DEALER_CODE"].map(di["PROP_TAX_RATE"])
    alloc_df["PROP_TAX"] = alloc_df["PROP_TAX_RATE"] * alloc_df["RESIDUAL"] * expected_stay_months / 12
    # Additive two-signal util score (2026-04-21 pivot):
    # UTIL_RATE and RENTED combined with independent weights; no normalization.
    alloc_df["UTILIZATION_SCORE"] = (
        w_util * alloc_df["UTIL_RATE"] + w_rented * alloc_df["RENTED"]
    )
    alloc_df["ALLOC_SCORE"] = (
        alloc_df["UTILIZATION_SCORE"]
        - w_dist * alloc_df["DISTANCE"] / distance_norm
        - w_tax * alloc_df["PROP_TAX"] / tax_norm
    )
    alloc_df["DEALER_LAT"] = alloc_df["DEALER_CODE"].map(di["LATITUDE"])
    alloc_df["DEALER_LON"] = alloc_df["DEALER_CODE"].map(di["LONGITUDE"])
    return alloc_df


def _score(df):
    if df.empty:
        return {"assigned": 0, "util_score": 0, "distance": 0, "tax": 0, "net": 0}
    return {
        "assigned": int(len(df)),
        "util_score": round(float(df["UTILIZATION_SCORE"].sum()), 4),
        "distance": round(float(df["DISTANCE"].sum()), 1),
        "tax": round(float(df["PROP_TAX"].sum()), 2),
        "net": round(float(df["ALLOC_SCORE"].sum()), 4),
    }


def _dealer_summary(df):
    if df.empty:
        return []
    s = df.groupby("DEALER_CODE").agg(
        vehicles=("VIN", "count"),
        util_score=("UTILIZATION_SCORE", "sum"),
        distance=("DISTANCE", "sum"),
        tax=("PROP_TAX", "sum"),
        net=("ALLOC_SCORE", "sum"),
        state=("DEALER_STATE", "first"),
        name=("DEALER_NAME", "first"),
        util=("UTIL_RATE", "first"),
        lat=("DEALER_LAT", "first"),
        lon=("DEALER_LON", "first"),
    ).reset_index()
    s = s.sort_values("net", ascending=False)
    return s.round(4).to_dict("records")


def _arc_data(df, grounding):
    if df.empty:
        return []
    src_loc = grounding.set_index("SOURCE")[["SOURCE_LAT", "SOURCE_LON"]].to_dict("index")
    arcs = df.groupby(["SOURCE", "DEALER_CODE"]).agg(
        count=("VIN", "count"), net=("ALLOC_SCORE", "sum"),
        dlat=("DEALER_LAT", "first"), dlon=("DEALER_LON", "first"),
    ).reset_index()
    arcs["slat"] = arcs["SOURCE"].map(lambda s: src_loc.get(s, {}).get("SOURCE_LAT", 39.0))
    arcs["slon"] = arcs["SOURCE"].map(lambda s: src_loc.get(s, {}).get("SOURCE_LON", -98.0))
    return arcs.round(4).to_dict("records")


def _vin_table(df):
    if df.empty:
        return []
    cols = ["VIN", "SOURCE", "DEALER_CODE", "DEALER_NAME", "DEALER_STATE",
            "DISTANCE", "UTILIZATION_SCORE", "PROP_TAX", "ALLOC_SCORE"]
    return df[cols].sort_values("ALLOC_SCORE", ascending=False).round(4).to_dict("records")


# ── Public API ──────────────────────────────────────────────────────

def solve_both(n_vehicles=None, w_util=DEFAULT_W_UTIL,
               w_dist=DEFAULT_W_DIST, w_tax=DEFAULT_W_TAX,
               source_limit=None, expected_stay_months=DEFAULT_EXPECTED_STAY_MONTHS,
               w_rented=DEFAULT_W_RENTED, session: Optional["SessionState"] = None):
    """Run Greedy + V2, return JSON-safe dict with all results."""
    data = _data_for_session(session)
    veh = data["veh"].copy()
    if n_vehicles and n_vehicles < len(veh):
        veh = veh.sample(n=n_vehicles, random_state=42).reset_index(drop=True)

    g_raw = _solve_greedy(veh, data, w_util, w_dist, w_tax, source_limit, expected_stay_months,
                          session=session)
    v_raw = _solve_v2(veh, data, w_util, w_dist, w_tax, source_limit, expected_stay_months,
                      w_rented=w_rented, session=session)
    g = _enrich(g_raw, veh, data, w_util, w_dist, w_tax, expected_stay_months, w_rented=w_rented)
    v = _enrich(v_raw, veh, data, w_util, w_dist, w_tax, expected_stay_months, w_rented=w_rented)

    gs, vs = _score(g), _score(v)
    delta = round(vs["net"] - gs["net"], 4)
    delta_pct = round(delta / gs["net"] * 100, 1) if gs["net"] else 0

    return {
        "params": {"n_vehicles": len(veh),
                    "w_util": w_util, "w_rented": w_rented,
                    "miles_per_util": round(data["distance_norm"] / w_dist, 2) if w_dist else None,
                    "dollars_per_util": round(data["tax_norm"] / w_tax, 2) if w_tax else None,
                    "w_dist": w_dist, "w_tax": w_tax,
                    "distance_norm": round(data["distance_norm"], 2),
                    "tax_norm": round(data["tax_norm"], 2),
                    "source_limit": source_limit,
                    "expected_stay_months": expected_stay_months},
        "greedy": gs,
        "v2": vs,
        "delta": delta,
        "delta_pct": delta_pct,
        "dealers_v2": _dealer_summary(v),
        "dealers_greedy": _dealer_summary(g),
        "arcs": _arc_data(v, data["grounding"]),
        "vins": _vin_table(v),
        "sankey": _sankey_data(v),
    }


def _sankey_data(df):
    if df.empty:
        return {"labels": [], "sources": [], "targets": [], "values": [], "link_colors": [], "node_colors": []}
    flow = df.groupby(["SOURCE", "DEALER_CODE"]).agg(count=("VIN", "count"), net=("ALLOC_SCORE", "sum")).reset_index()
    top_src = flow.groupby("SOURCE")["count"].sum().nlargest(12).index.tolist()
    top_dlr = flow.groupby("DEALER_CODE")["count"].sum().nlargest(15).index.tolist()
    ff = flow[(flow["SOURCE"].isin(top_src)) & (flow["DEALER_CODE"].isin(top_dlr))]
    if ff.empty:
        return {"labels": [], "sources": [], "targets": [], "values": [], "link_colors": [], "node_colors": []}
    labels = top_src + top_dlr
    idx = {l: i for i, l in enumerate(labels)}
    return {
        "labels": labels,
        "sources": [idx[s] for s in ff["SOURCE"]],
        "targets": [idx[d] for d in ff["DEALER_CODE"]],
        "values": ff["count"].tolist(),
        "link_colors": ["rgba(0,212,255,0.2)" if n > 0 else "rgba(255,90,90,0.2)" for n in ff["net"]],
        "node_colors": ["rgba(255,107,107,0.7)"] * len(top_src) + ["rgba(0,212,255,0.7)"] * len(top_dlr),
    }


# ── Fleet Inventory ────────────────────────────────────────────────

def get_fleet_inventory(session: Optional["SessionState"] = None):
    """
    Return fleet inventory derived from the session's fleet snapshot
    (or the immutable baseline when no session is provided).
    Statuses: Incoming, Grounded, Transporting, Delivered.
    """
    data = _data_for_session(session)
    dealer = data["dealer"]
    arc_dist = data["arc_dist"]
    di = dealer.set_index("DEALER_CODE")

    fleet = session.fleet_df if session is not None else load_baseline_fleet()

    vehicles = []
    for _, row in fleet.iterrows():
        entry = {
            "vin": row["VIN"],
            "year": int(row["YEAR"]),
            "make": row["MAKE"],
            "model": row["MODEL"],
            "source": row["SOURCE"],
            "source_city": row["SOURCE_CITY"],
            "source_state": row["SOURCE_STATE"],
            "residual": round(float(row["RESIDUAL"]), 2),
            "status": row["STATUS"],
            "week": row["WEEK"],
        }
        ad = row.get("ASSIGNED_DEALER")
        if pd.notna(ad) and ad and ad in di.index:
            dr = di.loc[ad]
            entry["assigned_dealer"] = ad
            entry["assigned_dealer_name"] = dr["DEALER_NAME"]
            entry["assigned_state"] = dr["STATE"]
            dist = arc_dist.get((row["SOURCE"], ad), 0)
            entry["distance"] = round(dist, 1)
        vehicles.append(entry)

    counts = fleet["STATUS"].value_counts().to_dict()
    weeks = sorted(fleet["WEEK"].unique().tolist())

    return {
        "vehicles": vehicles,
        "weeks": weeks,
        "counts": {
            "total": len(fleet),
            "incoming": counts.get("Incoming", 0),
            "grounded": counts.get("Grounded", 0),
            "transporting": counts.get("Transporting", 0),
            "delivered": counts.get("Delivered", 0),
        },
    }


# ── Confirm Allocation ─────────────────────────────────────────────

def confirm_allocation(assignments, session: "SessionState"):
    """
    Mutate the session's in-memory fleet snapshot to reflect confirmed
    assignments. assignments: list of {"vin", "dealer_code", "dealer_name"}.
    Updates STATUS=Transporting, sets ASSIGNED_DEALER and NOTES for VINs
    currently in `Grounded` status. NO disk write.
    """
    if session is None:
        raise ValueError("confirm_allocation requires a session")
    fleet = session.fleet_df
    fleet["ASSIGNED_DEALER"] = fleet["ASSIGNED_DEALER"].fillna("").astype(str)

    assign_map = {a["vin"]: a for a in assignments}
    updated = 0
    skipped = 0
    for idx, row in fleet.iterrows():
        vin = row["VIN"]
        if vin in assign_map:
            a = assign_map[vin]
            dealer_code = a.get("dealer_code", "")
            dealer_name = a.get("dealer_name", dealer_code)
            if row["STATUS"] not in ("Grounded",):
                skipped += 1
                continue
            if dealer_code:
                fleet.at[idx, "STATUS"] = "Transporting"
                fleet.at[idx, "ASSIGNED_DEALER"] = dealer_code
                fleet.at[idx, "NOTES"] = f"En route to {dealer_name}"
            else:
                fleet.at[idx, "ASSIGNED_DEALER"] = ""
                fleet.at[idx, "NOTES"] = "Awaiting allocation"
            updated += 1

    session.touch()
    return {"updated": updated, "skipped": skipped, "total": len(assignments)}


def reset_fleet(session: "SessionState", store: "SessionStore"):
    """Restore the session's fleet snapshot from the baseline. NO disk write."""
    if session is None or store is None:
        raise ValueError("reset_fleet requires a session and store")
    store.reset_session_fleet(session)
    return {"status": "ok"}


# ── Weekly Batch Allocation ────────────────────────────────────────

def solve_weekly_batch(n_batch=10, seed=None,
                       w_util=DEFAULT_W_UTIL,
                       w_dist=DEFAULT_W_DIST, w_tax=DEFAULT_W_TAX,
                       source_limit=None, vin_list=None,
                       expected_stay_months=DEFAULT_EXPECTED_STAY_MONTHS,
                       w_rented=DEFAULT_W_RENTED,
                       session: Optional["SessionState"] = None,
                       dealer_capacity_overrides: Optional[Dict[str, int]] = None):
    """
    Run weekly batch allocation. If vin_list is provided (from fleet inventory),
    builds a temporary vehicle dataframe from the session's fleet snapshot
    (or the baseline if no session) and runs V2 ILP.

    `dealer_capacity_overrides` ({DEALER_CODE: REMAINING_CAPACITY}) is a
    what-if hook used by `analyze_capacity_change`. It is applied to a private
    copy of the dealer frame for this call only — the shared baseline is never
    mutated — so concurrent solves are unaffected.
    """
    data = _data_for_session(session)
    veh = data["veh"].copy()
    dealer = data["dealer"]
    if dealer_capacity_overrides:
        # `data["dealer"]` from `_data_for_session` is already a fresh copy, but
        # copy again defensively so no caller-visible frame is ever mutated.
        dealer = dealer.copy()
        for code, cap in dealer_capacity_overrides.items():
            mask = dealer["DEALER_CODE"] == code
            dealer.loc[mask, "REMAINING_CAPACITY"] = int(cap)
        data = {**data, "dealer": dealer}
    arc_dist = data["arc_dist"]
    source_to_dealers = data["source_to_dealers"]
    grounding = data["grounding"]

    # Pick vehicles from the session's fleet snapshot — single source of truth per user
    if vin_list and len(vin_list) > 0:
        unknown = [v for v in vin_list if v not in set(veh["VIN"])]
        if unknown:
            preview = ", ".join(unknown[:5]) + ("..." if len(unknown) > 5 else "")
            return _empty_batch_result(
                f"Unknown VINs (not in faas_eligible_vehicles): {preview}",
                method="ilp", w_util=w_util, w_rented=w_rented,
                w_dist=w_dist, w_tax=w_tax, source_limit=source_limit,
                expected_stay_months=expected_stay_months, seed=seed,
            )
        fleet = session.fleet_df if session is not None else load_baseline_fleet()
        fleet_sel = fleet[fleet["VIN"].isin(vin_list)].reset_index(drop=True)
        if fleet_sel.empty:
            return _empty_batch_result(
                "No matching VINs found in fleet snapshot",
                method="ilp", w_util=w_util, w_rented=w_rented,
                w_dist=w_dist, w_tax=w_tax, source_limit=source_limit,
                expected_stay_months=expected_stay_months, seed=seed,
            )
        batch = fleet_sel[["VIN", "SOURCE", "RESIDUAL"]].copy()
    else:
        rng = np.random.RandomState(seed)
        batch = veh.sample(n=min(n_batch, len(veh)), random_state=rng).reset_index(drop=True)

    # Run V2 solver on this batch
    v2_raw = _solve_v2(batch, data, w_util, w_dist, w_tax, source_limit, expected_stay_months,
                       w_rented=w_rented, session=session)
    v2 = _enrich(v2_raw, batch, data, w_util, w_dist, w_tax, expected_stay_months, w_rented=w_rented)

    # Build per-vehicle scoring for ALL feasible dealers (for reasoning)
    di = dealer.set_index("DEALER_CODE")
    src_loc = grounding.set_index("SOURCE")[["SOURCE_LAT", "SOURCE_LON", "CITY", "STATE"]].to_dict("index")

    nv, v2d, _, _, _, dealer_util_score, dealer_ptax = _build_pairs(
        batch, dealer, arc_dist, source_to_dealers, w_util, w_dist, w_tax,
        expected_stay_months, data["distance_norm"], data["tax_norm"],
        w_rented=w_rented,
    )

    # Build assignment map from V2 results
    assigned_map = {}
    if not v2.empty:
        assigned_map = v2.set_index("VIN").to_dict("index")

    vehicles = []
    for _, row in batch.iterrows():
        vin = row["VIN"]
        src = row["SOURCE"]
        residual = row["RESIDUAL"]
        src_info = src_loc.get(src, {})

        # Score all feasible dealers for this vehicle
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
                "remaining_capacity": int(dr["REMAINING_CAPACITY"]) if dr is not None else 0,
            })
        candidates.sort(key=lambda c: c["alloc_score"], reverse=True)
        for i, c in enumerate(candidates, start=1):
            c["rank"] = i

        # Determine assignment
        asgn = assigned_map.get(vin)
        assigned_dealer = asgn["DEALER_CODE"] if asgn else None
        assigned_info = next((c for c in candidates if c["dealer_code"] == assigned_dealer), None)

        # Build reasoning: why this dealer over alternatives
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
            "alternatives": candidates,  # all feasible dealers
            "reasoning": reasoning,
            "assigned_dealer": assigned_dealer,
        })

    # Summary stats
    n_assigned = sum(1 for v in vehicles if v["assigned"])
    total_alloc_score = sum(v["assigned"]["alloc_score"] for v in vehicles if v["assigned"])
    # Distance is counted per unique (source → destination) arc, not per vehicle:
    # a single carrier trip can transport multiple vehicles sharing the same route,
    # so we only pay that mileage once. Duplicate (source, dest) pairs collapse.
    unique_arcs = {}
    for v in vehicles:
        if v["assigned"]:
            key = (v["source"], v["assigned"]["dealer_code"])
            unique_arcs[key] = v["assigned"]["distance"]
    total_distance = sum(unique_arcs.values())

    # Unconstrained ceiling: if every vehicle could go to its best feasible dealer
    # ignoring capacity and source_limit. Per-VIN argmax over nv. This is a
    # theoretical upper bound used to express allocation quality as a percentage.
    ceiling = sum(
        max((nv[(vin, d)] for d in v2d[vin]), default=0)
        for vin in (row["VIN"] for _, row in batch.iterrows())
        if v2d.get(vin)
    )
    # Clamp to [0, 100]: a negative score (destinations with high prop tax dominating)
    # means the allocation is worse than doing nothing, so 0% reads more honestly than
    # a huge negative percentage.
    quality_pct = round(max(0.0, min(100.0, total_alloc_score / ceiling * 100)), 1) if ceiling > 0 else 0.0

    # Rank-based batch quality: average rank (1 = perfect) and rank-1 rate.
    # These are the UI-facing metrics. Raw quality_pct is kept in the response
    # for API back-compat but no longer rendered in the dashboard.
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
        "params": {"w_util": w_util, "w_rented": w_rented,
                   # Business-friendly equivalence rates (what 1 util-unit equals)
                   "miles_per_util": round(data["distance_norm"] / w_dist, 2) if w_dist else None,
                   "dollars_per_util": round(data["tax_norm"] / w_tax, 2) if w_tax else None,
                   # Internal implementation details (kept for reproducibility)
                   "w_dist": w_dist, "w_tax": w_tax,
                   "distance_norm": round(data["distance_norm"], 2),
                   "tax_norm": round(data["tax_norm"], 2),
                   "source_limit": source_limit,
                   "expected_stay_months": expected_stay_months},
        "vehicles": vehicles,
        "method": "ilp",
    }


def _build_reasoning(assigned, candidates):
    """Generate a concise reasoning string for why this dealer was chosen."""
    if not assigned or not candidates:
        return "No alternatives available."

    a = assigned
    n = len(candidates)
    rank = a.get("rank", 1)
    headline = (f"Rank {rank} of {n} feasible dealers"
                if n > 1 else "Only feasible dealer")
    parts = [headline]

    others = [c for c in candidates if c["dealer_code"] != a["dealer_code"]]
    if not others:
        return headline + "."

    runner_up = others[0]

    # Explain why this rank — compare to the next best option
    if a["utilization"] > runner_up["utilization"] + 5:
        parts.append(f"higher utilization ({a['utilization']}% vs {runner_up['utilization']}%)")
    if a["distance"] < runner_up["distance"] - 50:
        parts.append(f"shorter distance ({a['distance']} mi vs {runner_up['distance']} mi)")
    if a["prop_tax"] < runner_up["prop_tax"] - 10:
        parts.append(f"lower property tax (${a['prop_tax']} vs ${runner_up['prop_tax']})")

    return ". ".join(parts) + "."


def solve_weekly_greedy(n_batch=10, seed=None,
                        w_util=DEFAULT_W_UTIL,
                        w_dist=DEFAULT_W_DIST, w_tax=DEFAULT_W_TAX,
                        source_limit=None, vin_list=None,
                        expected_stay_months=DEFAULT_EXPECTED_STAY_MONTHS,
                        w_rented=DEFAULT_W_RENTED,
                        session: Optional["SessionState"] = None):
    """
    Same as solve_weekly_batch but uses Greedy solver instead of V2 ILP.
    Returns identical per-vehicle structure for UI consistency.
    """
    data = _data_for_session(session)
    veh = data["veh"].copy()
    dealer = data["dealer"]
    arc_dist = data["arc_dist"]
    source_to_dealers = data["source_to_dealers"]
    grounding = data["grounding"]

    if vin_list and len(vin_list) > 0:
        unknown = [v for v in vin_list if v not in set(veh["VIN"])]
        if unknown:
            preview = ", ".join(unknown[:5]) + ("..." if len(unknown) > 5 else "")
            return _empty_batch_result(
                f"Unknown VINs (not in faas_eligible_vehicles): {preview}",
                method="greedy", w_util=w_util, w_rented=w_rented,
                w_dist=w_dist, w_tax=w_tax, source_limit=source_limit,
                expected_stay_months=expected_stay_months, seed=seed,
            )
        fleet = session.fleet_df if session is not None else load_baseline_fleet()
        fleet_sel = fleet[fleet["VIN"].isin(vin_list)].reset_index(drop=True)
        if fleet_sel.empty:
            return _empty_batch_result(
                "No matching VINs found in fleet snapshot",
                method="greedy", w_util=w_util, w_rented=w_rented,
                w_dist=w_dist, w_tax=w_tax, source_limit=source_limit,
                expected_stay_months=expected_stay_months, seed=seed,
            )
        batch = fleet_sel[["VIN", "SOURCE", "RESIDUAL"]].copy()
    else:
        rng = np.random.RandomState(seed)
        batch = veh.sample(n=min(n_batch, len(veh)), random_state=rng).reset_index(drop=True)

    # Run Greedy solver
    g_raw = _solve_greedy(batch, data, w_util, w_dist, w_tax, source_limit, expected_stay_months,
                          session=session)
    g = _enrich(g_raw, batch, data, w_util, w_dist, w_tax, expected_stay_months, w_rented=w_rented)

    # Build per-vehicle scoring (same as solve_weekly_batch)
    di = dealer.set_index("DEALER_CODE")
    src_loc = grounding.set_index("SOURCE")[["SOURCE_LAT", "SOURCE_LON", "CITY", "STATE"]].to_dict("index")
    nv, v2d, _, _, _, dealer_util_score, dealer_ptax = _build_pairs(
        batch, dealer, arc_dist, source_to_dealers, w_util, w_dist, w_tax,
        expected_stay_months, data["distance_norm"], data["tax_norm"],
        w_rented=w_rented,
    )

    assigned_map = {}
    if not g.empty:
        assigned_map = g.set_index("VIN").to_dict("index")

    vehicles = []
    for _, row in batch.iterrows():
        vin, src, residual = row["VIN"], row["SOURCE"], row["RESIDUAL"]
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
                "remaining_capacity": int(dr["REMAINING_CAPACITY"]) if dr is not None else 0,
            })
        candidates.sort(key=lambda c: c["alloc_score"], reverse=True)
        for i, c in enumerate(candidates, start=1):
            c["rank"] = i

        asgn = assigned_map.get(vin)
        assigned_dealer = asgn["DEALER_CODE"] if asgn else None
        assigned_info = next((c for c in candidates if c["dealer_code"] == assigned_dealer), None)
        reasoning = _build_reasoning(assigned_info, candidates) if assigned_info else "Unassigned — greedy could not place this vehicle."

        vehicles.append({
            "vin": vin, "source": src,
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
    # Unique-arc distance (see solve_weekly_batch for rationale).
    unique_arcs = {}
    for v in vehicles:
        if v["assigned"]:
            unique_arcs[(v["source"], v["assigned"]["dealer_code"])] = v["assigned"]["distance"]
    total_distance = sum(unique_arcs.values())

    # Unconstrained ceiling (same definition as solve_weekly_batch)
    ceiling = sum(
        max((nv[(vin, d)] for d in v2d[vin]), default=0)
        for vin in (row["VIN"] for _, row in batch.iterrows())
        if v2d.get(vin)
    )
    # Clamp to [0, 100]: a negative score (destinations with high prop tax dominating)
    # means the allocation is worse than doing nothing, so 0% reads more honestly than
    # a huge negative percentage.
    quality_pct = round(max(0.0, min(100.0, total_alloc_score / ceiling * 100)), 1) if ceiling > 0 else 0.0

    # Rank-based batch quality — same treatment as solve_weekly_batch.
    assigned_ranks = [v["assigned"]["rank"] for v in vehicles if v["assigned"]]
    avg_rank = round(sum(assigned_ranks) / len(assigned_ranks), 2) if assigned_ranks else 0.0
    rank1_count = sum(1 for r in assigned_ranks if r == 1)
    rank1_pct = round(rank1_count / len(assigned_ranks) * 100, 1) if assigned_ranks else 0.0

    return {
        "batch_size": len(batch), "seed": seed,
        "n_assigned": n_assigned,
        "total_alloc_score": round(total_alloc_score, 4),
        "total_distance": round(total_distance, 1),
        "ceiling": round(ceiling, 4),
        "quality_pct": quality_pct,
        "avg_rank": avg_rank,
        "rank1_count": rank1_count,
        "rank1_pct": rank1_pct,
        "params": {"w_util": w_util, "w_rented": w_rented,
                   # Business-friendly equivalence rates (what 1 util-unit equals)
                   "miles_per_util": round(data["distance_norm"] / w_dist, 2) if w_dist else None,
                   "dollars_per_util": round(data["tax_norm"] / w_tax, 2) if w_tax else None,
                   # Internal implementation details (kept for reproducibility)
                   "w_dist": w_dist, "w_tax": w_tax,
                   "distance_norm": round(data["distance_norm"], 2),
                   "tax_norm": round(data["tax_norm"], 2),
                   "source_limit": source_limit,
                   "expected_stay_months": expected_stay_months},
        "vehicles": vehicles,
        "method": "greedy",
    }
