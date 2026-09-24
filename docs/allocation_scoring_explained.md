# Allocation Scoring — How It Works

> **Audience**: HCA business stakeholders and the HCA analytics team.
> **Purpose**: explain the scoring model without requiring code knowledge.
> **Last rewritten**: 2026-04-21 (pivoted from multiplicative `UTIL^β × size^γ` to
> additive `w_util × UTIL + w_rented × RENTED`).
>
> **Scope note (2026-05-22)**: this document describes the **additive
> scoring mode**, which is the production default and what every
> dashboard run uses unless `scoring_mode="bucket"` is set on the API
> call. A second mode, the **bucket form** per the 2026-05-21 teammate
> email, is in calibration on the `bucket-algorithm` branch and is
> documented separately at `docs/bucket_algorithm.md`. Under bucket
> mode the demand signal is `IN_SERVICE`, not `RENTED`, and the util
> side reduces to a max-anchored range tier multiplier. The two modes
> share the same distance and tax penalty terms but disagree on the
> util-side signal; both narratives are correct within their own mode.

---

## The one formula

Every (vehicle, dealer) pair gets an **allocation score**:

```
alloc_score =  w_util × UTIL_RATE
             + w_rented × RENTED
             − (distance ÷ DISTANCE_NORM)
             − (annual_property_tax ÷ TAX_NORM)
```

Two positive signals and two cost penalties:

| Term | What it captures |
|---|---|
| `w_util × UTIL_RATE` | How efficient is this dealer per-car (the fraction of its existing fleet that's currently rented). |
| `w_rented × RENTED` | How many cars are **currently earning** at this dealer — absolute scale, directly from the client's 2026-04-17 criterion. NOT normalized: a 200-RENTED dealer stays worth 200 × `w_rented` regardless of other dealers. |
| `− distance` | Shipping cost proxy (drivable miles, scaled onto the same magnitude as the positive terms). |
| `− property_tax` | Annualized state property-tax exposure. |

**The total score is a pure ranking signal.** Its absolute value is not
dollars. Its job is to answer "for this vehicle, which dealer is best, which
is second-best, etc."

### Why we show rank, not score, to business users

The UI intentionally displays **Rank 1 / Rank 2 / Rank 3** and hides the raw
score in a small gray subtitle. Two reasons:

1. **Raw scores are not cross-vehicle comparable.** A car in Los Angeles
   might score 2.1 while a car in Miami scores 1.4 — but this does NOT
   mean LA's placement is better. LA just happens to have busier, bigger
   FaaS dealers in driving range. Miami's 1.4 may already be the best it
   could do.
2. **Scores look like dollars, inviting misreading.** A number like `−0.34`
   on a dashboard gets interpreted as a dollar loss even with disclaimers.
   Ranks (`Rank 1`, `Rank 2`) are unambiguous — they communicate "which
   choice the algorithm made for this car" without any unit confusion.

Rank IS comparable at the right level of aggregation: "12 of 17 cars got
their Rank 1 dealer" tells you the batch worked well; "only 4 of 17 were
Rank 1" tells you capacity was tight. Both statements survive the
cross-vehicle-comparison scrutiny that raw scores fail.

---

## Two business knobs, two data-driven normalizers

### Business knobs

Three of the four weights (`w_util`, `w_rented`, `w_tax`) are derived
**jointly** from a 2026-04-21 4-D Pareto-knee sweep; `w_dist` was
subsequently overridden on 2026-04-24 to reflect that per-mile carrier
cost dominates annual property tax by 5–7× per vehicle:

#### Defaults (current calibration)

| Weight | Default | Source | Role |
|---|---|---|---|
| `w_util` | **1.335** | 4-D knee | Per-car efficiency signal |
| `w_rented` | **0.0574** | 4-D knee | Absolute-demand signal (client review 2026-04-17) |
| `w_dist` | **15.0** | **2026-04-24 override** (was 3.827) | Distance penalty, recalibrated so its magnitude matches real carrier cost per vehicle |
| `w_tax` | **1.950** | 4-D knee | Property-tax penalty (verified optimal — lowering it *increases* total cost because solver stops avoiding high-tax states) |

#### How they were derived

- Ran **81,920 ILP simulations** — 1,024 Sobol quasi-random points in
  4-D `(w_util, w_rented, w_dist, w_tax)` space × 80 fleet scenarios
  (8 batch sizes × 10 seeds).
- Four Pareto criteria, kept in natural units (no dollar assumption):
  maximize `avg_rented_at_dest` and `avg_util_at_dest`; minimize
  `avg_distance` and `avg_annual_tax`.
- Each of the 1,024 outcome 4-tuples normalized to `[0, 1]` using
  observed min/max; pick the combination whose normalized 4-D point
  has the shortest Euclidean distance to utopia `(1, 1, 1, 1)`.

**What the weights mean intuitively**: at this balance, one currently-
rented car at the destination (`RENTED = +1`) is worth roughly 4.3
percentage-points of destination utilization (`UTIL_RATE = +0.043`).
Distance and tax penalties have finally been scaled to magnitudes that
can re-order the top candidates when geography disagrees with demand.

### Why this derivation is mathematically defensible

| Step | Why it's not arbitrary |
|---|---|
| Objective criteria | Four natural outcome metrics (rented, util, distance, tax). No dollar conversion assumed. |
| Sample design | 1,024 Sobol points give uniform 4-D coverage; 80 fleet scenarios average out batch-composition noise. |
| Selection rule | Euclidean distance to utopia in normalized outcome space — a closed, deterministic calculation. |

Same data in → same four weights out.

### Normalizers — data-driven, not user-tunable

The distance and tax terms are each divided by a constant so they land on
the same magnitude as the UTIL / RENTED terms and can be added / subtracted
coherently. These constants come from the data:

- **Distance normalizer** ≈ **4,347 mi** — the longest arc in the current
  distance matrix. A trip that long gets a penalty of 1.0; shorter trips
  get proportionally smaller penalties.
- **Tax normalizer** ≈ **$989** — the worst-case annual property-tax
  exposure on the current fleet (highest state rate × highest residual).

They adjust automatically when the data changes. Not calibration parameters —
rescaling scaffolding. (Internally `DISTANCE_NORM`, `TAX_NORM`; the API
also exposes `miles_per_util` and `dollars_per_util` for integrators who
need to override, but the business story only has the two knobs above.)

### Collapse path

When HCA shares:
- `$/car/month` rental revenue → `w_util` and `w_rented` disappear,
  replaced by one pure-dollar revenue term
- `$/mile` carrier rate → `DISTANCE_NORM` scaffolding disappears
- (authoritative tax data) → `TAX_NORM` scaffolding disappears

All four calibration quantities vanish. The algorithm becomes pure
"maximize expected annual profit per vehicle". No more knobs.

### Committed next step (between midterm and final): tier-based pivot

The pure-$ collapse above is the long-term destination. The committed
near-term pivot is to replace the continuous `w_util × UTIL + w_rented
× RENTED` util-side terms with a single tier classification:

```
score = bucket_mult(tier_of(d))  −  w_dist × dist/NORM  −  w_tax × tax/NORM
```

Both `w_util` and `UTIL` are dropped from the bucket form. `bucket_mult`
IS the util-side score, calibrated 2026-05-22 to
`(3.7203, 2.8848, 0.7957, 0.6498)` over 4 IN_SERVICE-tier buckets via a
1024-point Sobol sweep × 80 fleet scenarios (see `docs/bucket_algorithm.md`
for the sweep design and provenance). Within a tier, all dealers tie on
util; distance and tax break ties.

The reason for this pivot is explainability. The current additive
formula's weights cannot answer "+10% UTIL is worth how many miles?"
without HCA's dollar data. The tier form converts that to "Tier 1 vs
Tier 2 is worth how many miles?" — categorical, business-actionable,
defensible without dollar inputs. Trade-offs accepted: loss of
intra-tier discrimination, possible HHI worsening (to be verified
empirically).

---

## The two-stage ILP solver (why and how)

Allocation has two objectives that can conflict:

1. **Ship as many vehicles as possible** (idle vehicles earn nothing).
2. **Send each one to a good dealer** (maximize allocation score).

Combining them in one weighted sum (like `big_M × assignment_count +
total_score`) forces you to pick `big_M`. Too small → algorithm skips
assignments for tiny score gains; too large → score optimization has no
weight.

Instead we do **lexicographic two-stage** optimization:

- **Stage 1**: maximize `n_assigned`. Find the count `N*`.
- **Stage 2**: subject to `n_assigned = N*`, maximize total `alloc_score`.

Analogy: a university first checks whether an applicant is above the
admission line (binary: in or out), then ranks admitted students by
overall strength. No one is rejected because a different admitted student
would have scored 0.5 points higher.

### Known weakness

Stage 1 strictly prefers "assign any dealer" over "leave vehicle
unassigned", even if every feasible dealer has a strongly negative
`alloc_score`. In current data this rarely matters; a future improvement
is to add a floor: accept `x[v,d] = 1` only when `alloc_score(v,d) >
some_threshold`.

---

## Batch-level quality — rank-based, not score-based

Raw scores aren't meaningful to business users, and averaging scores
across vehicles is mathematically questionable (two vehicles' scores come
from different candidate pools — see "Why we show rank, not score"
above). Instead, the dashboard uses two rank-based aggregates:

- **% at Rank 1** (= "Best Choices"): of assigned vehicles, what fraction
  landed at the algorithm's top-ranked dealer. Intuitive, safe to compare
  across batches and across algorithms.
- **Avg Rank**: mean of all assigned-dealer ranks. `1.0` = every car at
  its top pick; higher = some cars were pushed down by capacity or user
  override.

Both KPIs appear on the Weekly Allocation page.

> A legacy `quality_pct` (= `Σ alloc_score / ceiling × 100`) remains in
> the API response for integrators. It's not rendered on the dashboard
> because summing raw scores across vehicles violates our "scores only
> comparable within a single car's candidates" principle.

---

## What's intentionally NOT in the model

- **Revenue per vehicle.** Was in an earlier iteration using synthetic
  data. Removed on 2026-04-17 after stakeholder feedback that it had
  never been discussed with the business. Will be reintroduced as soon
  as HCA shares per-car monthly rental revenue — at that point `w_util`
  and `w_rented` both disappear and revenue becomes the core objective.
- **Transportation cost in dollars.** Was `distance × $1.50/mile`.
  Removed because `$1.50` was a made-up number. Distance is now penalized
  in miles-normalized units. Will be re-converted to dollars when HCA
  shares a real `$/mile` carrier rate.
- **Grounded vehicle holding cost.** A vehicle left at its grounding
  state still accrues property tax there. The solver currently treats
  "unassigned" as zero cost, which slightly under-weights the incentive
  to ship vehicles out. Flagged as a known future improvement.
- **Source-level throttling.** A per-source cap on shipments was
  defaulted to 5 as a defensive setting; now disabled because no
  operational data justified it and it directly fought the business
  goal of reallocating vehicles from low-demand to high-demand
  destinations.

---

## `RENTED` as demand signal (additive mode, 2026-04-21 pivot)

> This section describes the additive mode only. The bucket mode picks
> `IN_SERVICE` as the demand-shape signal per the 2026-05-21 teammate
> email; see `docs/bucket_algorithm.md` for that path.

**Problem** (client review 2026-04-17, confirmed 2026-04-21):
treating utilization as a pure rate (`UTIL_RATE` alone) means a 3-car
dealer at 69% util looks better than a 114-car dealer at 22% util — even
though the 114-car dealer has **25 cars actually earning** vs the small
dealer's **2 cars**. Absolute scale matters.

**First attempt (retired)**: `util_score = UTIL_RATE^β × (IN_SERVICE / median)^γ`.
The 2,048-Sobol-point (β, γ) study found γ→0 to be "optimal" — but that
was wrong for two reasons: `IN_SERVICE` is placement state (cars HCA
has delivered), not demand, and normalization by median made each
dealer's score depend on other dealers' data. Both defects are fixed by
the current additive form.

**Current implementation** (2026-04-21):

```
util_score = w_util × UTIL_RATE + w_rented × RENTED
```

- `RENTED` comes from `dealer_utilization.csv` as-is — it's the dealer's
  own count of cars currently rented out. No derivation.
- Absolute: **not divided by any per-batch max or median**. The 200-RENTED
  dealer stays worth the same no matter what else is in the data.
- Weights derived from 81,920-simulation 4-D Pareto-knee sweep, not
  picked by hand.

**Dashboard work**: `RENTED` is now a column on the Overview Dashboard
Dealer Rankings panel alongside `IN_SERVICE` and `UTIL_RATE`, so business
users can see the absolute scale that drives scoring.

---

## Calibration path

All four weights `(w_util=1.335, w_rented=0.0574, w_dist=15.0, w_tax=1.950)`
are the current calibrated defaults: three (`w_util`, `w_rented`, `w_tax`)
from the 2026-04-21 4-D Pareto knee, plus a 2026-04-24 cost-aware override
on `w_dist` (was `3.827` under the pure knee; raised to `15.0` because
per-mile carrier cost dominates annual property tax by 5–7× per vehicle).
They're scaffolding — they will be replaced by $-denominated coefficients
once HCA shares per-car monthly rental revenue and per-mile carrier rate.
Until then, one interim option exists:

### Inverse-preference from user overrides

If the business wants to override the math:

1. Show them 10–20 ILP-recommended allocations.
2. Have them mark the ones they'd override, and to which dealer.
3. Treat overrides as revealed preference.
4. Run inverse optimization: find `(w_util, w_rented)` that make their
   manual choices optimal under the model.
5. Typically converges after ~15 examples.

---

## Implementation cross-reference

For engineers picking this up:

- **Formula**: `app/engine.py` → `_build_pairs()` + `_enrich()` (additive
  form as of 2026-04-21).
- **Public API**: `app/server.py` → `/api/weekly`, `/api/allocate` accept
  `w_util`, `w_rented`, plus optional business-friendly equivalence rates
  (`miles_per_util`, `dollars_per_util`) and internal weights (`w_dist`,
  `w_tax`). Conversion happens in `_resolve_weights()`.
- **Agent prompt**: `app/agent.py` → `SYSTEM_PROMPT` describes
  the two-knob view to the chat agent.
- **Frontend KPIs**: `app/static/app.js` → `renderBatchKPIs()` shows
  `At Rank 1` (rate + count) and `Avg Rank` per batch. Dealer Rankings
  shows `RENTED`, `IN_SERVICE`, and the composed score.

---

## FAQ

### Q1: How were all four weights chosen?

> Three of the four `(w_util, w_rented, w_tax)` came out of a single
> 2026-04-21 4-D Sobol sweep — 1,024 weight combinations × 80 fleet
> scenarios = 81,920 ILP solves. On the 4-criterion Pareto front
> (maximize avg RENTED + avg UTIL at destination; minimize avg distance
> + avg annual tax, all in natural units — no dollar assumption), the
> point geometrically closest to the "utopia" corner `(1, 1, 1, 1)` in
> normalized outcome space yields `(w_util=1.335, w_rented=0.0574,
> w_dist=3.827, w_tax=1.950)`. The fourth weight, `w_dist`, was
> subsequently overridden on 2026-04-24 from `3.827` to `15.0` because
> the Pareto sweep's equal-weighted "distance vs tax" assumption
> undercounts how much larger per-mile carrier cost ($560–$840/car) is
> than annual property tax ($115/car) in the real business cost
> structure. See `AGENTS.md` for the knee methodology and override rationale,
> and `docs/viz/v5_efficient_frontier.html` for the interactive knee chart.

### Q2: Why is utilization weighted 1.335× vs. 15.0× for distance?

> The weight magnitudes are **not** direct preference intensities —
> they're scaffolding that rescales four mixed-unit terms (a fraction,
> a count, miles, dollars) onto a common normalized scale. `w_util`
> (1.335) is tuned to make utilization signal comparable to the other
> normalized terms; `w_dist` (15.0) is tuned so the normalized distance
> penalty reflects the real carrier cost per vehicle. The absolute
> numbers will change entirely when HCA provides real `$/mile` and
> `$/car/month` — at which point the formula becomes pure dollar-profit
> maximization with no weights.

### Q3: Why divide distance by 4,347?

> That's the longest arc in your current distance matrix. We use it as
> the scale factor so "the worst trip" sits at a distance penalty of 1.0.
> When the data changes, this number updates automatically.

### Q4: ILP returns high quality — isn't that suspiciously perfect?

> Under-bound conditions (650 vehicles vs 200 slots), capacity is not
> binding so every vehicle can go to its top-choice dealer. Greedy only
> hits ~53% of that, meaning blindly picking the nearest dealer leaves
> almost half the possible value on the table. Under supply scarcity
> (100 vehicles vs 160 slots), ILP's advantage over Greedy becomes much
> larger (historical simulation shows +22%).

### Q5: Does normalization hide small distance differences? (Technical)

> No. ILP sees all differences — float precision is far finer than any
> score magnitude you'd notice. What normalization *does* is put distance
> on the same scale as the util/rented terms so weights become
> interpretable. If you think small distance differences are being
> under-weighted, that's a `w_dist` issue — increase `w_dist`
> (equivalently, decrease `miles_per_util`) and distance will dominate more.

### Q6: Batch metrics drift when the dataset changes because DISTANCE_NORM shifts. Problem?

> Within a single batch, rankings are perfectly stable. The dashboard
> KPIs (`% at Rank 1`, `Avg Rank`) are rank-based so they're naturally
> batch-invariant — adding a new dealer changes what "Rank 1" refers to,
> but doesn't break the meaning of the metric. Note that **`RENTED`
> enters the score without normalization**, so absolute demand
> comparisons are stable regardless of fleet expansion.

### Q7: Why did you disable source_limit? Wouldn't that let ILP pile all vehicles on one dealer?

> The dealer side already has `REMAINING_CAPACITY` as a hard constraint,
> so no single dealer gets overloaded. The source-side cap
> (N vehicles per grounding dealer per batch) at N=5 was a defensive
> setting with no data backing it. Disabling it lets ILP reallocate a
> source's entire grounded inventory at once, which is exactly the client's
> stated goal of "move vehicles from low-demand sources to high-demand
> destinations". With real carrier throughput data we can re-enable with
> a data-backed number.

### Q8: The UI shows Rank 1 / Rank 2 — what's that? And can I compare score values between cars?

> The UI displays a **Rank** for each assigned vehicle: Rank 1 means the
> algorithm sent that car to its top-choice dealer, Rank 2 means
> runner-up, etc. The raw score appears as a small gray subtitle for
> audit purposes only.
>
> **You cannot compare raw scores across vehicles.** A car in LA scoring
> 2.1 does not mean its placement is "better" than a car in Miami scoring
> 1.4 — LA simply has a richer set of nearby busy dealers. Miami's 1.4
> may already be the best it could achieve. Comparing absolute scores
> would unfairly punish cars starting in weaker geographic regions.
>
> **Rank IS comparable at the aggregate level**: "12 of 17 cars got
> their Rank 1 dealer" is a meaningful batch-quality statement. That's
> why the KPI shows "Best Choices: 12/17" — it's the count of Rank-1
> placements.
