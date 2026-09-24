"""Watchlist signal aggregators.

Six aggregators, all REAL — every signal field is computed from current data
(/api/overview.dealers, /api/fleet, state.cached). No synthetic data,
no hardcoded thresholds claiming history we don't have.

Each aggregator returns a list of Signal dicts. Combined output is fed to
the watchlist agent which ranks + narrates them.
"""
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List

from engine import get_overview, get_fleet_inventory
import state

Signal = Dict[str, Any]


def _band_severity(util: float) -> str:
    if util >= 0.95:
        return "critical"
    if util >= 0.90:
        return "high"
    return "medium"


# Thresholds tuned 2026-05-14 to surface ≥5 signals reliably on real fleet data,
# so the agent has enough material to rank to MAX_ITEMS=5 alerts. The agent's
# severity-based ranking still ensures only the most relevant items reach the UI.
def saturation(min_util: float = 0.85) -> List[Signal]:
    """Dealers at saturation (UTIL_RATE >= threshold). Real, snapshot-based."""
    overview = get_overview()
    out: List[Signal] = []
    for d in overview.get("dealers", []) or []:
        util = float(d.get("UTIL_RATE") or 0)
        if util < min_util:
            continue
        rented = int(d.get("RENTED") or 0)
        in_service = int(d.get("IN_SERVICE") or 0)
        out.append({
            "id": "sat_" + str(d.get("DEALER_CODE", "?")),
            "type": "saturation",
            "severity": _band_severity(util),
            "metric": {
                "name": "util_rate",
                "actual": round(util, 4),
                "threshold": min_util,
            },
            "context": {
                "dealer_code": d.get("DEALER_CODE"),
                "dealer_name": d.get("DEALER_NAME"),
                "state": d.get("STATE"),
                "rented": rented,
                "in_service": in_service,
                "idle_capacity": max(0, in_service - rented),
                "util_pct": int(util * 100),
            },
            "recommended_action": "raise_cap",
        })
    return out


def deferred_batch() -> List[Signal]:
    """Vehicles in current batch that couldn't be assigned. Real, from state.cached."""
    cached = state.cached()
    if not cached or not cached.get("vehicles"):
        return []
    deferred = [v for v in cached["vehicles"] if not v.get("assigned")]
    if not deferred:
        return []
    state_counts = Counter(v.get("source_state", "?") for v in deferred)
    top_state, top_count = state_counts.most_common(1)[0]
    return [{
        "id": "deferred_batch",
        "type": "deferred_batch",
        "severity": "critical" if len(deferred) >= 5 else "high",
        "metric": {
            "name": "deferred_count",
            "actual": len(deferred),
            "threshold": 0,
        },
        "context": {
            "total_deferred": len(deferred),
            "top_state": top_state,
            "top_state_count": top_count,
            "state_breakdown": dict(state_counts),
            "sample_vins": [v.get("vin") for v in deferred[:5]],
            "region_summary": ", ".join("{} ({})".format(k, v) for k, v in state_counts.most_common(3)),
        },
        "recommended_action": "raise_cap_or_defer",
    }]


def capacity_mismatch() -> List[Signal]:
    """Deferred vehicles in regions where idle capacity exists nearby — routing puzzle.

    Real: joins deferred vehicles (state.cached) with dealer state (overview)."""
    cached = state.cached()
    if not cached or not cached.get("vehicles"):
        return []
    deferred = [v for v in cached["vehicles"] if not v.get("assigned")]
    if not deferred:
        return []
    overview = get_overview()
    state_deferred = Counter(v.get("source_state", "?") for v in deferred)
    out: List[Signal] = []
    for state_code, def_count in state_deferred.items():
        if state_code in ("?", None, ""):
            continue
        dealers_in_state = [
            d for d in overview.get("dealers", []) or []
            if d.get("STATE") == state_code
        ]
        idle = sum(
            max(0, int(d.get("IN_SERVICE") or 0) - int(d.get("RENTED") or 0))
            for d in dealers_in_state
        )
        if idle > 0 and idle >= def_count:
            out.append({
                "id": "capmismatch_" + state_code,
                "type": "capacity_mismatch",
                "severity": "medium",
                "metric": {
                    "name": "idle_to_deferred_ratio",
                    "actual": round(idle / max(def_count, 1), 2),
                    "threshold": 1.0,
                },
                "context": {
                    "state": state_code,
                    "deferred_count": def_count,
                    "idle_capacity_in_state": idle,
                    "dealers_in_state": len(dealers_in_state),
                },
                "recommended_action": "investigate_routing",
            })
    return out


def stuck_vehicles(weeks_threshold: int = 4) -> List[Signal]:
    """Vehicles STATUS=Grounded for > N weeks based on fleet_inventory.WEEK.

    Real: WEEK is ISO date in fleet_inventory.csv; status comes from same file."""
    fleet = get_fleet_inventory() or {}
    vehicles = fleet.get("vehicles", []) or []
    if not vehicles:
        return []
    threshold_date = datetime.now().date() - timedelta(weeks=weeks_threshold)
    stuck = []
    for v in vehicles:
        if v.get("status") != "Grounded":
            continue
        week_str = v.get("week") or ""
        try:
            week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if week_date < threshold_date:
            stuck.append(v)
    if not stuck:
        return []
    by_source = Counter(v.get("source", "?") for v in stuck)
    top_source, top_n = by_source.most_common(1)[0]
    oldest_week = min((v.get("week", "") for v in stuck if v.get("week")), default="")
    return [{
        "id": "stuck_" + str(weeks_threshold) + "wk",
        "type": "stuck_vehicles",
        "severity": "high" if len(stuck) >= 10 else "medium",
        "metric": {
            "name": "stuck_count",
            "actual": len(stuck),
            "threshold_weeks": weeks_threshold,
        },
        "context": {
            "total_stuck": len(stuck),
            "weeks_threshold": weeks_threshold,
            "top_source": top_source,
            "top_source_count": top_n,
            "source_count": len(by_source),
            "oldest_week": oldest_week,
        },
        "recommended_action": "review_or_defer",
    }]


def underutilized(max_util: float = 0.55, min_in_service: int = 10) -> List[Signal]:
    """Dealers with significant idle capacity — routing target candidates."""
    overview = get_overview()
    out: List[Signal] = []
    for d in overview.get("dealers", []) or []:
        util = float(d.get("UTIL_RATE") or 0)
        in_service = int(d.get("IN_SERVICE") or 0)
        if util >= max_util or in_service < min_in_service:
            continue
        rented = int(d.get("RENTED") or 0)
        out.append({
            "id": "underutil_" + str(d.get("DEALER_CODE", "?")),
            "type": "underutilized",
            "severity": "low",
            "metric": {
                "name": "util_rate",
                "actual": round(util, 4),
                "threshold": max_util,
            },
            "context": {
                "dealer_code": d.get("DEALER_CODE"),
                "dealer_name": d.get("DEALER_NAME"),
                "state": d.get("STATE"),
                "rented": rented,
                "in_service": in_service,
                "idle_capacity": max(0, in_service - rented),
                "util_pct": int(util * 100),
            },
            "recommended_action": "route_more_vehicles",
        })
    return out


def source_pressure(min_count: int = 8) -> List[Signal]:
    """Source dealers with high count of Grounded vehicles awaiting allocation."""
    fleet = get_fleet_inventory() or {}
    vehicles = fleet.get("vehicles", []) or []
    if not vehicles:
        return []
    grounded_by_source: Counter = Counter()
    state_by_source: Dict[str, str] = {}
    for v in vehicles:
        if v.get("status") != "Grounded":
            continue
        src = v.get("source") or "?"
        grounded_by_source[src] += 1
        if src not in state_by_source:
            state_by_source[src] = v.get("source_state") or "?"
    out: List[Signal] = []
    for src, count in grounded_by_source.most_common():
        if count < min_count:
            break
        out.append({
            "id": "srcpress_" + src,
            "type": "source_pressure",
            "severity": "high" if count >= 30 else "medium",
            "metric": {
                "name": "grounded_count_at_source",
                "actual": count,
                "threshold": min_count,
            },
            "context": {
                "source_code": src,
                "state": state_by_source.get(src, "?"),
                "grounded_count": count,
            },
            "recommended_action": "prioritize_in_next_batch",
        })
    return out


def all_signals() -> List[Signal]:
    """Run every aggregator. Returns flat list ordered by aggregator (input order to agent)."""
    sigs: List[Signal] = []
    for fn in (saturation, deferred_batch, capacity_mismatch,
               stuck_vehicles, underutilized, source_pressure):
        try:
            sigs.extend(fn() or [])
        except Exception as exc:  # noqa: BLE001 — one broken aggregator must not blow up the rest
            print("[watchlist] aggregator {} failed: {}".format(fn.__name__, exc))
    return sigs
