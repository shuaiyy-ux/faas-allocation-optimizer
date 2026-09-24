"""Unit tests for the bucket scoring module.

Coverage
--------
* `tier_of` boundary semantics under the max-anchored range rule.
* `compute_dealer_util_scores` end-to-end mapping from dataframe.
* `score_pair` arithmetic and norm fallback.
* `tier_breakpoints` for debug/UI use.
* Pipeline smoke test on a synthetic 3-dealer / 2-vehicle dataset.

The pipeline test exercises `bucket_pipeline.solve_bucket` end-to-end
through a monkey-patched `_data_for_session` so it runs without the CSV
fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scoring import (  # noqa: E402
    BucketParams,
    DEFAULT_BUCKET_MULTS,
    compute_dealer_util_scores,
    score_pair,
    tier_of,
)
from scoring.bucket import tier_breakpoints  # noqa: E402


# ── tier_of ──────────────────────────────────────────────────────────


def test_tier_of_email_example_max_100():
    """Spec example from teammate email 2026-05-21."""
    assert tier_of(100, 100) == 0  # A
    assert tier_of(76, 100) == 0   # A
    assert tier_of(75, 100) == 1   # B (boundary closed-upper)
    assert tier_of(51, 100) == 1   # B
    assert tier_of(50, 100) == 2   # C (boundary)
    assert tier_of(26, 100) == 2   # C
    assert tier_of(25, 100) == 3   # D (boundary)
    assert tier_of(0, 100) == 3    # D


def test_tier_of_extremes():
    assert tier_of(0, 0) == 3                # max=0 -> bottom tier
    assert tier_of(-5, 100) == 3             # negative input -> bottom tier
    assert tier_of(200, 100) == 0            # above max -> top tier
    assert tier_of(50, 0) == 3               # max=0 dominates


def test_tier_of_three_bucket_variant():
    """Number of buckets is configurable via n_buckets."""
    assert tier_of(100, 100, n_buckets=3) == 0
    assert tier_of(67, 100, n_buckets=3) == 0  # 67/100 > 2/3 -> tier 0
    assert tier_of(66, 100, n_buckets=3) == 1
    assert tier_of(34, 100, n_buckets=3) == 1
    assert tier_of(33, 100, n_buckets=3) == 2
    assert tier_of(0, 100, n_buckets=3) == 2


# ── tier_breakpoints ─────────────────────────────────────────────────


def test_tier_breakpoints_email_example():
    assert tier_breakpoints(100, 4) == [75.0, 50.0, 25.0, 0.0]
    assert tier_breakpoints(40, 4) == [30.0, 20.0, 10.0, 0.0]


def test_tier_breakpoints_zero_max():
    assert tier_breakpoints(0, 4) == [0.0, 0.0, 0.0, 0.0]


# ── compute_dealer_util_scores ───────────────────────────────────────


def _dealer_df():
    return pd.DataFrame({
        "DEALER_CODE": ["FD01", "FD02", "FD03", "FD04"],
        "IN_SERVICE": [100, 60, 30, 5],
        "RENTED": [80, 20, 10, 2],
        "PROP_TAX_RATE": [0.02, 0.025, 0.015, 0.03],
    })


def test_compute_dealer_scores_default_signal_is_in_service():
    """Each dealer's util score must equal the bucket mult of its tier under
    default params. The expected multipliers come from `DEFAULT_BUCKET_MULTS`
    so a calibration update is a one-line constant change, not a test rewrite.
    """
    params = BucketParams()
    scores = compute_dealer_util_scores(_dealer_df(), params)
    mults = DEFAULT_BUCKET_MULTS
    # max IN_SERVICE = 100 → cuts at 75 / 50 / 25.
    assert scores["FD01"] == mults[0]   # 100 → tier 0
    assert scores["FD02"] == mults[1]   #  60 → ratio 0.60 → tier 1
    assert scores["FD03"] == mults[2]   #  30 → ratio 0.30 → tier 2
    assert scores["FD04"] == mults[3]   #   5 → tier 3


def test_compute_dealer_scores_alternate_signal_rented():
    """Signal swap should require only a params change, not code change."""
    params = BucketParams(signal_field="RENTED")
    scores = compute_dealer_util_scores(_dealer_df(), params)
    mults = DEFAULT_BUCKET_MULTS
    # max RENTED = 80. Cuts at 60, 40, 20.
    # FD01 RENTED=80 → top tier              → mults[0]
    # FD02 RENTED=20 → ratio 0.25 (boundary) → tier 3 (closed-upper) → mults[3]
    # FD03 RENTED=10 → ratio 0.125 → tier 3 → mults[3]
    # FD04 RENTED=2  → tier 3 → mults[3]
    assert scores["FD01"] == mults[0]
    assert scores["FD02"] == mults[3]
    assert scores["FD03"] == mults[3]
    assert scores["FD04"] == mults[3]


def test_compute_dealer_scores_missing_signal_raises():
    params = BucketParams(signal_field="NOT_A_COLUMN")
    with pytest.raises(KeyError):
        compute_dealer_util_scores(_dealer_df(), params)


def test_compute_dealer_scores_handles_nan():
    df = _dealer_df()
    df.loc[df["DEALER_CODE"] == "FD03", "IN_SERVICE"] = pd.NA
    params = BucketParams()
    scores = compute_dealer_util_scores(df, params)
    assert scores["FD03"] == DEFAULT_BUCKET_MULTS[3]  # NaN → 0 → bottom tier


# ── score_pair ───────────────────────────────────────────────────────


def test_score_pair_arithmetic():
    params = BucketParams(w_dist=10.0, w_tax=2.0)
    out = score_pair(
        util_score=2.0,
        distance=1000.0,
        prop_tax=500.0,
        params=params,
        distance_norm=4000.0,
        tax_norm=1000.0,
    )
    # 2.0 - 10 * 1000/4000 - 2 * 500/1000 = 2 - 2.5 - 1 = -1.5
    assert out == pytest.approx(-1.5)


def test_score_pair_norm_fallback():
    """A zero norm must not divide-by-zero; the fallback should keep the math finite."""
    params = BucketParams(w_dist=10.0, w_tax=2.0)
    out = score_pair(2.0, 100.0, 50.0, params, distance_norm=0.0, tax_norm=0.0)
    assert out == pytest.approx(2.0 - 10 * 100 - 2 * 50)


# ── pipeline smoke test ──────────────────────────────────────────────


def _synthetic_data():
    """Hand-built mini dataset that bypasses the CSV loader."""
    veh = pd.DataFrame({
        "VIN": ["VIN001", "VIN002"],
        "SOURCE": ["D01", "D01"],
        "RESIDUAL": [20000.0, 25000.0],
    })
    dealer = pd.DataFrame({
        "DEALER_CODE": ["FD01", "FD02", "FD03"],
        "DEALER_NAME": ["A", "B", "C"],
        "STATE": ["CA", "TX", "FL"],
        "TRUE_CAPACITY": [50, 50, 50],
        "DELIVERED_COUNT": [10, 10, 10],
        "REMAINING_CAPACITY": [40, 40, 40],
        "IN_TRANSIT_COUNT": [0, 0, 0],
        "LATITUDE": [34.0, 30.0, 28.0],
        "LONGITUDE": [-118.0, -95.0, -82.0],
        "IN_SERVICE": [100.0, 50.0, 10.0],
        "RENTED": [80.0, 20.0, 5.0],
        "UTIL_RATE": [0.8, 0.4, 0.5],
        "PROP_TAX_RATE": [0.02, 0.025, 0.015],
    })
    grounding = pd.DataFrame({
        "SOURCE": ["D01"],
        "CITY": ["LA"],
        "STATE": ["CA"],
        "ZIPCODE": ["90001"],
        "SOURCE_LAT": [34.0],
        "SOURCE_LON": [-118.0],
        "LOCATION": ["Los Angeles, CA"],
    })
    arc_dist = {
        ("D01", "FD01"): 50.0,
        ("D01", "FD02"): 1500.0,
        ("D01", "FD03"): 2500.0,
    }
    source_to_dealers = {"D01": ["FD01", "FD02", "FD03"]}
    return {
        "veh": veh, "dealer": dealer, "grounding": grounding,
        "arc_dist": arc_dist, "source_to_dealers": source_to_dealers,
        "distance_norm": 2500.0, "tax_norm": 1000.0,
    }


def test_pipeline_end_to_end_assigns_to_top_bucket_when_close(monkeypatch):
    """With FD01 in bucket A and only 50 miles away, both vehicles land there."""
    import bucket_pipeline  # noqa: E402

    data = _synthetic_data()
    monkeypatch.setattr(bucket_pipeline, "_data_for_session", lambda _session: data)
    result = bucket_pipeline.solve_bucket(params=BucketParams())

    assert result["method"] == "bucket_v2"
    assert result["n_assigned"] == 2
    assigned_dealers = {row["DEALER_CODE"] for row in result["alloc"]}
    assert assigned_dealers == {"FD01"}
    # All assignments should have BUCKET_TIER == 0
    assert all(row["BUCKET_TIER"] == 0 for row in result["alloc"])


def test_pipeline_distance_can_break_tier_preference(monkeypatch):
    """If the top-tier dealer is far enough, the distance penalty can flip the choice.

    Uses an explicit params override rather than the defaults so the test
    keeps a single-purpose hypothesis: distance can outweigh tier.
    With mults [2.0, 1.4, 1.0, 0.6] and w_dist=15:
      score(FD01) = 2.0 - 15 * 4000/4000     = -13.0
      score(FD02) = 1.0 - 15 *  100/4000     =   0.625   (FD02 in tier 2)
    FD02 wins.
    """
    import bucket_pipeline  # noqa: E402

    data = _synthetic_data()
    data["arc_dist"] = {
        ("D01", "FD01"): 4000.0,
        ("D01", "FD02"): 100.0,
        ("D01", "FD03"): 2500.0,
    }
    data["distance_norm"] = 4000.0
    monkeypatch.setattr(bucket_pipeline, "_data_for_session", lambda _session: data)
    # Pin params so the hypothesis is independent of calibrated defaults.
    params = BucketParams(bucket_mults=(2.0, 1.4, 1.0, 0.6), w_dist=15.0, w_tax=1.95)
    result = bucket_pipeline.solve_bucket(params=params)

    assigned_dealers = {row["DEALER_CODE"] for row in result["alloc"]}
    assert "FD02" in assigned_dealers


# ── parameter integrity ──────────────────────────────────────────────


def test_default_params_match_spec():
    """BucketParams() must reflect the live `DEFAULT_BUCKET_MULTS` exactly,
    have 4 buckets, target IN_SERVICE as the signal, and be monotone
    non-increasing. The literal calibration vector lives in the module
    constant so a recalibration is one constant edit, not a test rewrite.
    """
    params = BucketParams()
    assert tuple(params.bucket_mults) == tuple(DEFAULT_BUCKET_MULTS)
    assert params.signal_field == "IN_SERVICE"
    assert params.n_buckets == 4
    for i in range(len(params.bucket_mults) - 1):
        assert params.bucket_mults[i] >= params.bucket_mults[i + 1], (
            f"bucket_mults must be monotone non-increasing; "
            f"got {params.bucket_mults}"
        )


def test_params_are_frozen():
    """`@dataclass(frozen=True)` should reject mutation."""
    params = BucketParams()
    with pytest.raises(Exception):
        params.signal_field = "RENTED"  # type: ignore[misc]


# ── solve_weekly_bucket response contract ────────────────────────────


def test_solve_weekly_bucket_response_has_frontend_fields():
    """Frontend assumes scoring_mode, params.bucket_mults, and per-vehicle
    in_service + rented are present. Lock these in so a refactor that drops
    any of them gets caught by the test suite, not by a broken Batch Overview.
    """
    import bucket_pipeline  # noqa: E402

    r = bucket_pipeline.solve_weekly_bucket(n_batch=15, seed=42)
    assert r["scoring_mode"] == "bucket"
    assert r["method"] == "bucket_ilp"
    assert "bucket_mults" in r["params"]
    assert r["params"]["signal_field"] == "IN_SERVICE"
    assert len(r["vehicles"]) > 0

    assigned = next((v["assigned"] for v in r["vehicles"] if v["assigned"]), None)
    assert assigned is not None, "expected at least one assigned vehicle"
    for field in ("in_service", "rented", "utilization", "distance",
                  "alloc_score", "rank", "dealer_code"):
        assert field in assigned, f"bucket response.assigned missing '{field}'"


def test_bucket_tier_assignment_matches_email_rule_on_real_data():
    """A dealer with IN_SERVICE in the top 25% of the dataset's max should
    score 2.0 on the util side under default mults. This is an integration
    check that the production data path produces the boundary the email
    actually specified.
    """
    import bucket_pipeline  # noqa: E402
    import engine  # noqa: E402
    from scoring import BucketParams, compute_dealer_util_scores

    data = engine._data_for_session(None)
    scores = compute_dealer_util_scores(data["dealer"], BucketParams())
    max_in_service = float(data["dealer"]["IN_SERVICE"].max())
    cut_a = 0.75 * max_in_service

    expected_top = DEFAULT_BUCKET_MULTS[0]
    for _, row in data["dealer"].iterrows():
        code = row["DEALER_CODE"]
        in_service = float(row["IN_SERVICE"] or 0)
        if in_service > cut_a:
            assert scores[code] == expected_top, (
                f"{code} IN_SERVICE={in_service} > {cut_a} (top tier cut) "
                f"should score {expected_top}, got {scores[code]}"
            )
