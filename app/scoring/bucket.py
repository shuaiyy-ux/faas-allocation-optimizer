"""Bucket-based dealer scoring (max-anchored range tiers).

Spec source: HCA email 2026-05-21 from teammate.

    Bucket A: top 25% range of `cars_assigned` across all dealers.
    The cut is anchored to the dealer with the highest count,
    NOT to the top 25% of dealers by rank.

    Example: if max(cars_assigned) = 100, then
        A: 76 <= n <= 100
        B: 51 <= n <=  75
        C: 26 <= n <=  50
        D:  0 <= n <=  25

`cars_assigned` is interpreted as IN_SERVICE (cars HCA has delivered to
the dealer). This overrides the prior `RENTED` demand-signal narrative
in `docs/allocation_scoring_explained.md`; see `docs/bucket_algorithm.md`
for the conflict resolution.

The module is intentionally standalone — it depends only on `pandas` and
the standard library. It does not import from `engine.py`. Callers wire
it into a pipeline (see `app/bucket_pipeline.py`).

Public surface
--------------
    BucketParams              dataclass holding all knobs
    DEFAULT_BUCKET_MULTS      calibrated multipliers (3.7203, 2.8848, 0.7957, 0.6498)
                              from the 2026-05-22 Sobol-knee sweep — see the
                              comment block above the constant for provenance
    tier_of(value, max_value, n_buckets) -> int
        Pure function. Returns the bucket index (0 = top tier).
    compute_dealer_util_scores(dealer_df, params) -> dict[str, float]
        Maps DEALER_CODE -> util-side score (bucket_mult only, no distance/tax).
    score_pair(util_score, distance, prop_tax, params, distance_norm, tax_norm)
        Pure function. Returns the full alloc_score for one (vehicle, dealer) pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Sequence

import pandas as pd

# Calibrated 2026-05-22 from a 1024-point Sobol sweep × 80 fleet scenarios
# (81,920 ILP solves). Knee = the 4-D-utopia-nearest point under normalized
# (avg_in_service_at_dest, avg_util_at_dest, avg_distance, avg_annual_tax).
# Raw output: sensitivity_analysis/bucket/output/sweep_v1.parquet.
# Knee row + top-10 archived in sensitivity_analysis/bucket/output/knee_v1.txt.
DEFAULT_BUCKET_MULTS: Sequence[float] = (3.7203, 2.8848, 0.7957, 0.6498)
DEFAULT_SIGNAL_FIELD: str = "IN_SERVICE"
# w_dist and w_tax intentionally do NOT use the sweep's knee values
# (5.23, 2.19). The 2026-04-24 cost-aware override for additive mode raised
# w_dist from 3.83 to 15.0 to reflect that per-mile carrier cost ($560–$840
# per vehicle) dominates annual property tax (~$115 per vehicle) by 5 to 7
# times. The cost calculus is identical under bucket mode, so the override
# carries forward. Re-run the sweep with a wider w_dist range if HCA provides
# a real $/mile rate that contradicts this.
DEFAULT_W_DIST: float = 15.0
DEFAULT_W_TAX: float = 1.950


@dataclass(frozen=True)
class BucketParams:
    """Immutable knob bundle for the bucket scorer.

    Parameters
    ----------
    bucket_mults
        Per-tier multipliers ordered top to bottom. Length determines the
        number of buckets. The email spec implies 4 buckets at 25% steps.
    signal_field
        Column on the dealer dataframe used to assign tiers. The email
        spec calls this "cars assigned"; per 2026-05-22 user clarification
        this is `IN_SERVICE`.
    w_dist
        Coefficient on the normalized distance penalty term. Reused from
        the prior additive form because the cost-aware override on
        2026-04-24 is independent of the util-side scoring.
    w_tax
        Coefficient on the normalized property-tax penalty term.
    """

    bucket_mults: Sequence[float] = field(default_factory=lambda: tuple(DEFAULT_BUCKET_MULTS))
    signal_field: str = DEFAULT_SIGNAL_FIELD
    w_dist: float = DEFAULT_W_DIST
    w_tax: float = DEFAULT_W_TAX

    @property
    def n_buckets(self) -> int:
        return len(self.bucket_mults)


def tier_of(value: float, max_value: float, n_buckets: int = 4) -> int:
    """Return the bucket index for `value` under the max-anchored range rule.

    Tier 0 is the top bucket. If `max_value <= 0` everyone falls into the
    bottom tier (`n_buckets - 1`) since there is no positive scale to anchor.

    Boundary convention: each cut is closed on the upper side, so
    `value == max_value * (k / n_buckets)` lands in tier `n_buckets - k`.

    Examples (n_buckets=4, max_value=100)
    -------------------------------------
        tier_of(100, 100) -> 0   # A
        tier_of( 76, 100) -> 0   # A (cut at 75)
        tier_of( 75, 100) -> 1   # B (boundary)
        tier_of( 51, 100) -> 1   # B
        tier_of( 50, 100) -> 2   # C (boundary)
        tier_of(  0, 100) -> 3   # D
    """
    if max_value <= 0:
        return n_buckets - 1
    if value >= max_value:
        return 0
    if value <= 0:
        return n_buckets - 1
    ratio = value / max_value
    # The k-th cut from the top sits at (n_buckets - k) / n_buckets.
    # A value at ratio r lands in tier ceil(n_buckets * (1 - r)) - 1 in
    # the closed-upper convention. Compute directly for clarity:
    tier = 0
    for k in range(1, n_buckets):
        cut = (n_buckets - k) / n_buckets
        if ratio > cut:
            return tier
        tier += 1
    return tier  # n_buckets - 1


def compute_dealer_util_scores(
    dealer_df: pd.DataFrame,
    params: BucketParams,
) -> Dict[str, float]:
    """Return DEALER_CODE -> util-side score under the bucket rule.

    Util-side score is `bucket_mults[tier_of(IN_SERVICE)]`. Distance and
    tax penalties are NOT included here; they enter at pair time via
    `score_pair`.

    The signal column must exist on `dealer_df`. Missing values are
    treated as zero (which puts them in the bottom tier).
    """
    if params.signal_field not in dealer_df.columns:
        raise KeyError(
            f"BucketParams.signal_field={params.signal_field!r} "
            f"not found on dealer dataframe; columns are {list(dealer_df.columns)}"
        )
    signal = dealer_df[params.signal_field].fillna(0).astype(float)
    max_signal = float(signal.max()) if len(signal) else 0.0
    mults = params.bucket_mults
    n = params.n_buckets

    scores: Dict[str, float] = {}
    for code, value in zip(dealer_df["DEALER_CODE"], signal):
        tier = tier_of(float(value), max_signal, n)
        scores[code] = float(mults[tier])
    return scores


def score_pair(
    util_score: float,
    distance: float,
    prop_tax: float,
    params: BucketParams,
    distance_norm: float,
    tax_norm: float,
) -> float:
    """Full alloc_score for one (vehicle, dealer) pair under bucket form.

        alloc_score = bucket_mult(tier)
                    - w_dist * distance / distance_norm
                    - w_tax  * prop_tax / tax_norm
    """
    return (
        float(util_score)
        - params.w_dist * float(distance) / float(distance_norm or 1.0)
        - params.w_tax * float(prop_tax) / float(tax_norm or 1.0)
    )


def tier_breakpoints(max_value: float, n_buckets: int = 4) -> list:
    """Return the lower edges of each tier (top to bottom) for debugging.

    Example
    -------
        tier_breakpoints(100, 4) -> [75.0, 50.0, 25.0, 0.0]
    """
    if max_value <= 0:
        return [0.0] * n_buckets
    return [max_value * (n_buckets - k - 1) / n_buckets for k in range(n_buckets)]
