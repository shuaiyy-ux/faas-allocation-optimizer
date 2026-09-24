# Bucket Allocation Algorithm

> **Status**: implemented on branch `bucket-algorithm`, not yet wired
> into production. Standalone module `app/scoring/bucket.py`, runner
> `app/bucket_pipeline.py`. The additive scorer in `engine.py` is
> untouched and remains the production path until a swap is approved.
> **Spec source**: HCA teammate email 2026-05-21, clarified by user
> 2026-05-22 (cars_assigned = IN_SERVICE).

---

## Formula

For every (vehicle, dealer) pair:

```
alloc_score = bucket_mult(tier_of(d))
            - w_dist * distance(v, d) / DISTANCE_NORM
            - w_tax  * prop_tax(v, d) / TAX_NORM
```

`bucket_mult(tier)` is the util-side score by itself. `w_util` and the
continuous `UTIL_RATE` term are no longer in the formula. Within a tier
all dealers share the same util-side score, and the distance and tax
penalties break ties.

## Tier definition, max-anchored range rule

Let `M = max(IN_SERVICE)` over all dealers in the current dataset. The
four tiers are equal-width slices of `(0, M]`, top to bottom.

| Tier | Range (closed upper) | Default mult |
|---|---|---|
| A | `0.75 M < IN_SERVICE <= M` | 2.0 |
| B | `0.50 M < IN_SERVICE <= 0.75 M` | 1.4 |
| C | `0.25 M < IN_SERVICE <= 0.50 M` | 1.0 |
| D | `0 <= IN_SERVICE <= 0.25 M` | 0.6 |

Example, `M = 100`:

```
A: 76..100      B: 51..75       C: 26..50       D: 0..25
```

This is a **range-based** cut, not a quantile-based cut. The email is
explicit: anchor to the dealer with the highest count, not the top 25%
of dealers by rank. A long-tail distribution can leave tier A with one
dealer and tier D with most of the network, which is the intended
"absolute giants versus long tail" semantics.

## Signal field

`cars_assigned` is `IN_SERVICE`, per user clarification 2026-05-22. This
overrides three pieces of prior copy that must be updated before the
bucket scorer becomes the production path:

1. `docs/allocation_scoring_explained.md`, the "RENTED as demand signal"
   section, which argues RENTED is the demand signal and IN_SERVICE is
   placement state only.
2. `app/registry.json` `IN_SERVICE.semantic`, which currently reads
   "IN_SERVICE is NOT a scoring input".
3. `app/agent.py` `SYSTEM_PROMPT`, which tells
   the agent that IN_SERVICE is not a scoring signal.

The `BucketParams.signal_field` knob keeps the choice configurable, so a
later revisit to RENTED is one line in the call site.

## Bucket multipliers

Calibrated 2026-05-22 from a 1024-point Sobol sweep × 80 fleet scenarios,
81,920 ILP solves total. The 6-D search space was `(A, B, C, D, w_dist,
w_tax)` with the four multipliers sorted descending per sample to enforce
monotonicity. The four Pareto axes were

| Direction | Axis |
|---|---|
| maximize | `avg_in_service_at_dest` |
| maximize | `avg_util_at_dest` |
| minimize | `avg_distance` |
| minimize | `avg_annual_tax` |

Each axis was normalized to `[0, 1]` against its observed min and max.
The knee is the point geometrically closest to utopia `(1, 1, 1, 1)`
in normalized 4-D space.

Calibrated knee, point #460:

| Param | Value |
|---|---|
| `bucket_mults` | `(3.7203, 2.8848, 0.7957, 0.6498)` |
| `w_dist` (knee) | `5.2334` |
| `w_tax` (knee) | `2.1896` |

Knee-axis values at this point: avg IN_SERVICE 35.13, avg UTIL 0.8494,
avg distance 373.2 mi, avg annual tax $74.29, HHI 0.1160, rank1_pct 60.7%.

The top-10 candidates cluster within 0.03 of utopia distance; the
calibration is somewhat under-determined and any of the top-10 are
defensible. Raw sweep output and the top-10 table live at
`sensitivity_analysis/bucket/output/sweep_v1.parquet` and
`sensitivity_analysis/bucket/output/knee_v1.txt`.

The shape of the calibrated vector: tier A and B are clearly separated
from C and D (3.72 vs 2.88 small drop, 2.88 vs 0.80 large drop, 0.80
vs 0.65 small drop). Read: "Tier A and B are the dealers worth
preferring; Tier C and D are essentially the long tail with a small
floor." Compare against the working-assumption vector `[2.0, 1.4, 1.0,
0.6]`, which spaced all four tiers roughly evenly.

## Distance and tax penalties

`w_dist` defaults to 15.0 and `w_tax` to 1.950, both inherited from the
2026-04-24 cost-aware override under the additive form. The justification
is unchanged, per-mile carrier cost still dominates annual property tax
by 5 to 7 times per vehicle.

The 2026-05-22 bucket sweep reported a knee at `w_dist=5.23` and
`w_tax=2.19`. The bucket form is NOT adopting these because the same
Sobol-knee under-weighting of distance that drove the 2026-04-24 override
for the additive form applies here. Distance and tax penalty terms are
identical across the two scoring modes, and the cost calculus has not
changed since 2026-04-24. The override carries forward. If HCA shares a
real `$/mile` rate that contradicts the 5-to-7-times-tax-cost ratio, the
override should be revisited for both modes simultaneously.

## Code layout

```
app/
  scoring/
    __init__.py            re-exports BucketParams, tier_of, etc.
    bucket.py              pure functions, no engine import
  bucket_pipeline.py       orchestrator, imports engine + scoring
  tests/
    test_bucket_scoring.py unit and pipeline tests
docs/
  bucket_algorithm.md      this file
```

`engine.py`, `server.py`, `agent.py`, and `static/app.js` are unchanged
on this branch. The bucket pipeline is reachable only via
`app.bucket_pipeline.solve_bucket()`. To ship, add a `scoring_mode`
parameter on the relevant API endpoints and dispatch into this module.

## Maintainability choices

* `BucketParams` is a frozen dataclass, so accidental mutation across
  threads or solver calls is impossible.
* `tier_of`, `score_pair`, and `compute_dealer_util_scores` are pure
  functions, each independently unit-testable.
* The signal field, number of buckets, multiplier vector, and penalty
  weights are all parameters. Switching to 3 buckets or to RENTED as the
  signal is a parameter change, not a code change.
* The bucket pipeline imports the algorithm module, not the other way
  around. The algorithm module has zero dependency on the engine.
* The dynamic-constraint plugin loader in `engine._solve_v2` is
  intentionally not invoked by the bucket pipeline, since constraint
  side effects would muddy A/B comparison during calibration.

## Calibration validation, 2026-05-22

After picking the knee from the Sobol sweep and replacing the working-assumption multipliers with the calibrated values, a sanity-check pass ran the production-default bucket configuration alongside two alternatives on the same batch (n=50, seed=42, all three runs share the same vehicle sample).

### Configurations tested

| Configuration | bucket_mults | Source |
|---|---|---|
| Manual bucket | (2.0, 1.5, 1.0, 0.5) | Hand-picked, even spacing |
| Calibrated bucket (production) | (3.7203, 2.8848, 0.7957, 0.6498) | 1024-point Sobol sweep, point #460 |
| Additive (ILP, baseline) | n/a, continuous `w_util × UTIL + w_rented × RENTED` form | 2026-04-21 production |

All three runs used the same `w_dist = 15.0` and `w_tax = 1.95` from the 2026-04-24 cost-aware override.

### Headline numbers, n=50 seed=42

| Metric | Manual | Calibrated | Additive |
|---|---|---|---|
| Assigned | 50 | 50 | 50 |
| Dest UTIL > 80% rate | 40% | 52% | 58% |
| Avg Dest UTIL | 75.4% | 78.4% | 82.3% |
| Avg Dist / VIN | 231 mi | 287 mi | 312 mi |
| HHI | 0.0696 | 0.0816 | 0.0776 |

Intra-mode Rank-1 is intentionally omitted because the comparison is not meaningful across formulas.

### What the numbers say

1. UTIL preference gradient is monotone across the three configurations, from highest (additive, 58% routed to high-UTIL dealers) through calibrated bucket (52%) to manual bucket (40%). This confirms the bucket form's structural intent: collapsing the continuous UTIL signal into a categorical tier preference de-emphasizes UTIL by design. The calibrated vector retains more of the additive UTIL bias than the manual vector does, because Tier A dealers in this dataset are also the high-UTIL dealers.
2. Distance ordering inverts the UTIL ordering. Manual ships shortest (231 mi per VIN), additive longest (312 mi per VIN). The bucket form's flat util-side gradient lets distance compete more aggressively against tier preference, especially under the manual vector's smoother spacing.
3. HHI ordering is non-monotone. Manual has the lowest concentration; calibrated has the highest. The Sobol-calibrated multipliers are aggressive enough at the top (3.72 vs Tier B's 2.88) that the algorithm pushes more flow toward fewer Tier A dealers than the additive form does on this batch.
4. The calibrated weights are not strictly optimal on these axes. The manual vector outperforms calibrated on distance and HHI for this batch. The Sobol calibration's top-10 candidates were within 0.03 of utopia distance, so the choice between them is within noise.

### Caveats

- All numbers come from one batch (n=50, seed=42). The pattern may not hold on different sample sizes or fleet seeds.
- The Pareto metric used to select the Sobol knee included two axes (`avg_in_service_at_dest`, `avg_util_at_dest`) that are partially circular under the bucket form. A corrected metric (drop those two, add `n_unique_dealers_used` and `HHI`) would likely shift the knee toward a less aggressive multiplier vector closer to the manual one.
- Rank-1 is intra-mode and not cross-mode comparable; it is excluded from the headline numbers.

### Decision

Keep the calibrated multipliers `(3.7203, 2.8848, 0.7957, 0.6498)` as the production default. Reasons:

- Stronger tier preference (sharper A vs C/D gap) is closer to the categorical "prefer bigger dealers" pitch the business team wants to defend.
- The Dest UTIL > 80% gap between calibrated (52%) and additive (58%) is six percentage points, smaller than the 18-point gap to manual (40%). Calibrated retains more of the additive form's high-UTIL bias, which the business may still value until HCA shares per-car revenue data.
- The Sobol calibration is documented and reproducible. The manual vector would need its own justification.

The manual vector remains a defensible fallback if HCA later prefers a less aggressive tier preference. Switching is a one-constant edit thanks to the test refactor that already references `DEFAULT_BUCKET_MULTS` rather than literal numbers.

### Reproducibility

```bash
.venv/bin/python sensitivity_analysis/bucket/compare_weights.py \
    --manual 2,1.5,1,0.5 \
    --calibrated 3.7203,2.8848,0.7957,0.6498 \
    --n-batch 50 --seed 42 \
    --output docs/viz/bucket_weights_comparison.html
```

The committed HTML at `docs/viz/bucket_weights_comparison.html` is the output of this command and is the canonical sanity-check artifact for the calibration.

## Open items before production swap

1. ~~Calibrate `bucket_mults` against a Sobol sweep, not the
   working-assumption vector.~~ Done 2026-05-22.
2. Validate HHI and Rank-1 rate against the additive baseline. The doc
   flags HHI risk explicitly. Knee HHI = 0.1160 (vs sweep mean 0.1014),
   well below additive baseline. Knee rank1_pct = 60.7% (vs additive's
   typical 70-90%); intra-tier ties under bucket form reduce rank1_pct
   by construction, see note below.
3. ~~Resolve the three narrative conflicts above (registry, agent prompt,
   primary scoring doc).~~ Done 2026-05-22 in commit `5b96b14`.
4. ~~Decide on API surface for the swap.~~ Done 2026-05-22 in commit
   `6479e85` — `scoring_mode` field on `/api/weekly` and `/api/allocate`.
5. ~~Update the frontend's "scoring formula" copy where it explains the
   util-side terms.~~ Done 2026-05-22 in commit `9b9e15e` — Batch
   Overview banner and demand-depth axis switch by `scoring_mode`.
6. **Rank-1 reporting under bucket form**: with the calibrated mults,
   tier C and tier D are nearly tied (0.7957 vs 0.6498), and tier A
   and tier B are widely separated from C/D. This compresses the
   intra-batch rank-1 rate because many vehicles see two or more equally
   ranked dealers (any tie inside the same tier breaks only on distance
   and tax). Consider reporting "tier-1 placement rate" (% landing in
   bucket A) alongside the existing rank-1 metric on the Batch Overview.
7. Sweep is not auto-re-run when the data changes. If the dealer
   network grows substantially or new states with very different tax
   rates are added, re-run `sweep_bucket.py` and inspect the new knee.
