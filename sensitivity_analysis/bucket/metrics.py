"""Outcome metrics for one bucket-form ILP solve.

Mirrors the additive form's metrics.py but adds `avg_in_service_at_dest`
since the bucket axis is IN_SERVICE rather than RENTED. HHI is preserved
because the doc flags HHI worsening as the bucket form's known risk;
the calibration knee must land on a point that doesn't blow HHI up.
"""

from __future__ import annotations

from typing import Sequence, Mapping


def compute_metrics(
    assignments: Sequence[Mapping],
    batch_size: int,
) -> dict:
    """
    assignments
        list of dicts with keys vin, dealer, alloc_score, distance,
        annual_tax, dest_util, dest_in_service, dest_rented, rank.
    batch_size
        total VINs attempted (assigned + unassigned).

    Returns the Pareto axes plus diagnostics.
        avg_in_service_at_dest  primary positive axis (bucket form)
        avg_rented_at_dest      kept for cross-form comparison
        avg_util_at_dest        per-car efficiency at destination
        avg_distance            cost axis
        avg_annual_tax          cost axis
        rank1_pct               quality diagnostic
        hhi_concentration       known-risk diagnostic
        n_assigned              feasibility check
    """
    n_assigned = len(assignments)

    if n_assigned == 0:
        return {
            "n_assigned": 0,
            "batch_size": batch_size,
            "avg_in_service_at_dest": 0.0,
            "avg_rented_at_dest": 0.0,
            "avg_util_at_dest": 0.0,
            "avg_distance": 0.0,
            "avg_annual_tax": 0.0,
            "rank1_pct": 0.0,
            "hhi_concentration": 0.0,
        }

    avg_in_service = sum(a.get("dest_in_service", 0.0) for a in assignments) / n_assigned
    avg_rented = sum(a.get("dest_rented", 0.0) for a in assignments) / n_assigned
    avg_util = sum(a.get("dest_util", 0.0) for a in assignments) / n_assigned
    avg_distance = sum(a["distance"] for a in assignments) / n_assigned
    avg_annual_tax = sum(a["annual_tax"] for a in assignments) / n_assigned

    dealer_counts: dict = {}
    for a in assignments:
        dealer_counts[a["dealer"]] = dealer_counts.get(a["dealer"], 0) + 1
    shares = [c / n_assigned for c in dealer_counts.values()]
    hhi = sum(s * s for s in shares)

    rank1_count = sum(1 for a in assignments if a.get("rank", 99) == 1)
    rank1_pct = rank1_count / n_assigned * 100.0

    return {
        "n_assigned": n_assigned,
        "batch_size": batch_size,
        "avg_in_service_at_dest": round(avg_in_service, 4),
        "avg_rented_at_dest": round(avg_rented, 4),
        "avg_util_at_dest": round(avg_util, 4),
        "avg_distance": round(avg_distance, 2),
        "avg_annual_tax": round(avg_annual_tax, 2),
        "rank1_pct": round(rank1_pct, 2),
        "hhi_concentration": round(hhi, 4),
    }
