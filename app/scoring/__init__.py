"""Scoring strategies for vehicle-to-dealer allocation.

Each scoring strategy lives in its own submodule and exposes the same
two-call surface, see `bucket.py` for the reference implementation:

    compute_dealer_util_scores(dealer_df, params) -> dict[DEALER_CODE, float]
    score_pair(util_score, distance, prop_tax, params,
               distance_norm, tax_norm)              -> float

The engine and bucket pipeline import strategies from here, not directly
from submodules, so scoring swaps stay a single-line change at call sites.
"""

from .bucket import (
    BucketParams,
    DEFAULT_BUCKET_MULTS,
    compute_dealer_util_scores,
    score_pair,
    tier_breakpoints,
    tier_of,
)

__all__ = [
    "BucketParams",
    "DEFAULT_BUCKET_MULTS",
    "compute_dealer_util_scores",
    "score_pair",
    "tier_breakpoints",
    "tier_of",
]
