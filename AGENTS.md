# Capstone Project — HCA FaaS Vehicle Allocation

<!-- Repo-local showcase copy. The chat + watchlist agents run on the Claude CLI. -->

This file is the architectural source of truth for the HCA FaaS allocator.
When anything here conflicts with docs, this file wins — update the docs.
**`AGENTS.md` is this showcase copy's source of truth.** Do not propagate this
file back to legacy branches unless the user explicitly asks.

---

## Architecture

Five layers, all under `app/` except the calibration infrastructure:

| Layer | Files | What it does |
|---|---|---|
| **Engine** | `app/engine.py` | ILP + Greedy solvers, data loading, allocation scoring. Public entry points: `load_data()`, `get_overview()`, `solve_both()`, `solve_weekly_batch()`, `solve_weekly_greedy()`, `get_fleet_inventory()`, `confirm_allocation()`, `reset_fleet()`. All solver entry points accept the four weights `(w_util, w_rented, w_dist, w_tax)` derived jointly from the 4-D Pareto knee. |
| **Shared state** | `app/state.py` | `SessionStore` keeps per-browser-session fleet snapshots, approved in-memory constraints, and allocation caches keyed by the frontend `client_id` / `X-Session-Id`. Confirm and Reset mutate only the session snapshot; they do not write CSVs. Legacy `last_weekly` / `last_allocate` globals remain only for migration fallback. `last_watchlist` is fleet-wide and persists via `last_watchlist.json`. |
| **Server** | `app/server.py` | FastAPI backend. 16 REST endpoints plus the `/mcp` mount. Pydantic request models accept all four weights where applicable (bounded: counts capped at the 650-vehicle fleet, weights/params sanity-bounded). `ChatRequest` carries `client_id` (UUID), which binds the Claude CLI session and MCP tool state to the same browser session. `PORT` env var (default 8000) drives the listen port. `DEMO_MODE=1` hides `/docs` / `/redoc` / `/openapi.json` and `/api/sessions`, and defers chat quota to the gateway. |
| **MCP server + Agent** | `app/mcp_server.py`, `app/agent.py`, `app/registry.json` | `mcp_server.py` exposes the 14 tools via FastMCP (streamable-http). It is mounted into the same FastAPI process at `/mcp`, so tools call engine functions in-process (no urllib self-calls). The `/mcp` mount is gated by a random `X-Internal-Key` header (403 without it). `agent.py` runs one `claude -p` subprocess per turn (`--output-format stream-json --verbose --include-partial-messages`) and translates its stream-json events into the existing SSE event shape (`token`, `tool_start`, `tool_done`, `answer`, `focus`), plus a final `demo_usage` line for the gateway. Safety flags: `--tools ""`, `--allowedTools mcp__faas__*`, inline `--mcp-config` + `--strict-mcp-config` pointing at `/mcp/?faas_session_id=<client_id>` with the internal key, `--setting-sources ""`, `--disable-slash-commands`, `--permission-mode dontAsk`, cwd is a dedicated empty dir. Model from `FAAS_CLAUDE_MODEL` (default `opus`). Multi-turn via `--resume <session_id>` captured from the CLI `system/init` event. Mutation tools (`add_constraint`, `remove_constraint`) are gated by a one-shot approval token minted by `POST /api/approve_constraint`; constraint code is AST-whitelisted before it is ever stored or run. |
| **Frontend** | `app/static/index.html`, `app.js`, `style.css`, `favicon.png` | **Four-view dashboard** (Home → Fleet Inventory → Weekly Allocation → Batch Overview). **Home** is the default landing — fleet-wide brief with hero $-savings, 8wk sparkline trends, status donut + pipeline-by-week, dealer util heatmap, dealer scatter, top moves narrative cards, watchlist. **Batch Overview** is per-batch only — 5 batch KPIs (`Vehicles`, `Assigned`, `Rank-1`, `Avg Util`, `Total Distance`), ILP-vs-Greedy banner + comparison, allocation routes map, rank distribution histogram, per-dealer load. Chat panel on the right; each tab has a `chatClientId` UUID that also scopes backend session state. AI overlay during allocation is a CSS particle system (replaces the earlier canvas physics on 2026-05-14 — see `ui-redesign-v2` branch). Tool errors surface as the red ✗ tooltip + an inline `.tool-error` row showing the structured error string. |

Separate from the runtime stack:

- **Sensitivity analysis** (`sensitivity_analysis/`) — the original 81,920-run additive 4-D Sobol sweep is summarized in this file, while the current branch packages the bucket-calibration infrastructure under `sensitivity_analysis/bucket/` plus static visualizations under `docs/viz/`. Extra analysis dependencies remain outside `app/requirements.txt` to preserve the runtime/tooling boundary.

---

## Allocation Scoring

### The formula (2026-04-21 additive pivot)

For every `(vehicle, dealer)` candidate pair:

```
alloc_score = w_util × UTIL_RATE_d
            + w_rented × RENTED_d
            − distance_miles(v, d) / DISTANCE_NORM
            − property_tax_over_stay(v, d) / TAX_NORM
```

Two additive positive signals, two subtractive cost terms.
**RENTED enters without normalization** — absolute scale preserved per
client review 2026-04-17 (a 200-RENTED dealer scores independently of other dealers'
data). Distance and tax are scaffolding-normalized (divided by their
data-observed max) so they stay on a comparable numeric scale to the
util/rented terms.

**Only the relative ordering of candidates matters.** The absolute score
is not dollars, is not comparable across vehicles (different source
dealers reach different candidate pools), and must not be surfaced to
business users as a primary metric.

### Four weights — 4-D Pareto knee + 2026-04-24 cost-aware `w_dist` override

| Knob | Code default | What it does |
|---|---|---|
| `w_util` | `1.335` | UTIL_RATE signal (destination's per-car efficiency). From 4-D knee. |
| `w_rented` | `0.0574` | RENTED signal (absolute demand count — client review 2026-04-17). Un-normalized so absolute scale is preserved. From 4-D knee. |
| `w_dist` | `15.0` | Distance penalty. **Override** on 2026-04-24 from the 4-D knee value of `3.827`, because carrier cost ($/mile) dominates the per-vehicle $ calculus by ~5–7× over property tax. See "Cost-aware `w_dist` override" below. |
| `w_tax` | `1.950` | Property-tax penalty. From 4-D knee (verified optimal — lowering it routes to high-tax states and *increases* total cost). |

Three of the four weights (`w_util`, `w_rented`, `w_tax`) come from the
**4-D Sobol sweep over 1,024 weight combinations × 80 fleet scenarios =
81,920 ILP solves** on 2026-04-21. Selection rule: minimum Euclidean
distance to the utopia point `(1, 1, 1, 1)` after normalizing each of the
four outcome criteria (avg_rented_at_dest, avg_util_at_dest, avg_distance,
avg_annual_tax) to `[0, 1]`. The fourth weight, `w_dist`, was overridden
on 2026-04-24 from the knee value of `3.827` to `15.0` because per-vehicle
carrier cost ($2–$3/mi × ~280 mi/car ≈ $560–$850) is an order of magnitude
larger than annual property tax ($115/car at worst), so the knee's
distance weight was undercalibrated to the actual business cost structure.
Current branch reference: the rationale in this file plus the interactive
efficient-frontier chart at `docs/viz/v5_efficient_frontier.html`. The original
full additive-sweep report is not packaged in this branch.

There used to be `beta` (UTIL curvature) and `gamma` (IN_SERVICE size
exponent) parameters. Both are retired. The multiplicative form
`UTIL^β × (IN_SERVICE/median)^γ` was wrong on two counts: IN_SERVICE is
placement state (not demand — RENTED is demand), and normalization by
median made dealer scores dependent on other dealers' data. An earlier
1-D sweep (2026-04-21 morning) calibrated only `w_rented` while keeping
`w_dist = w_tax = 1.0`; that result (`w_rented = 0.031`) is superseded
by the 4-D sweep above.

### IN_SERVICE is NOT a scoring input

`IN_SERVICE` = cars HCA has delivered to a FaaS dealer (placement state).
It's surfaced in the UI for context but **does not enter `alloc_score`**.
The client's 2026-04-17 criterion was about RENTED (cars currently earning), which
the identity `UTIL × IN_SERVICE = RENTED` already gives us as its own
column in `dealer_utilization.csv`.

### Rank-primary UI discipline

User-facing surfaces (UI, agent, business docs) display **rank** — the
ordinal position of the assigned dealer within the vehicle's own candidate
list (Rank 1 = algorithm's top pick). Two vehicles' raw `alloc_score`
values **cannot** be compared because they come from different candidate
pools (different grounding dealers reach different sets of FaaS dealers).
Raw scores remain in API payloads as a gray secondary — never quoted to
users as a primary metric.

This is enforced at three layers:

- **Engine** — every candidate carries a `rank` field; every assigned
  vehicle reports its assigned rank; batch response includes `rank1_pct`,
  `rank1_count`, `avg_rank`.
- **Frontend** (`app.js` `renderBatchKPIs`, weekly table) — Choice column
  shows `★ Rank 1` prominently; raw score is a small gray subtitle.
- **Agent prompt** (`agent.py`) — Rule 7 forbids quoting raw `alloc_score`;
  agent must lead with rank when explaining placements.

### Batch-level rank metrics (what business users see)

- **`rank1_pct`** = % of assigned vehicles that landed at their Rank 1
  dealer. Typical ILP with defaults: 70–90%. Greedy (nearest-dealer):
  10–30%. User override to a non-rank-1 dealer drops this.
- **`avg_rank`** = mean of assigned ranks (1.0 = perfect). Typical ILP
  with defaults: 1.1–1.5. Greedy: 5–12.

Both are safe to compare across batches and methods because rank is
intra-vehicle. A **legacy `quality_pct`** field remains in the API payload
for back-compat but is NOT rendered in the UI — it aggregates raw scores
across vehicles, which violates the "scores only comparable within a
single vehicle's pool" principle. Do not quote it.

### Normalization constants (data-driven scaffolding)

- **`DISTANCE_NORM`** ≈ `4,347 mi` on current data. Set to the longest
  arc in `dealer_distance_matrix.csv` at load time (actually 4346.8;
  rounded throughout the docs).
- **`TAX_NORM`** ≈ `$989` on current data. Set to the worst-case annual
  tax (highest state rate × highest residual × `expected_stay_months/12`).

These exist only to put distance and tax on the same scale as utilization.
They auto-adapt when the data changes — they are **not** business
trade-off rates. The API exposes them as `miles_per_util` and
`dollars_per_util` for integrators who need to override (equivalent to
`DISTANCE_NORM / w_dist` and `TAX_NORM / w_tax`), but the business-facing
surface treats them as fixed.

### Property-tax horizon

- **`expected_stay_months`** (default `12`): how long a vehicle is assumed
  to sit at its FaaS dealer and accrue tax. State tax is strictly lumpy
  (annual assessment), but for allocation ranking we linearize. Retune
  when HCA provides real turnover data.
- **`property_tax_over_stay`** = `state_rate × residual × stay/12`.

### Operational parameter

- **`source_limit`** (default `None` = off): optional per-grounding-dealer
  shipment cap. Off by default — no carrier-throughput data supports a
  numeric cap, and an arbitrary limit fights the core goal of moving
  vehicles off low-util sources. Left as a hook for real carrier data.

### Internal implementation (do NOT expose to users)

Code implements the cost terms as
`w_dist × dist/DISTANCE_NORM` and `w_tax × tax/TAX_NORM`. The four
internal symbols (`w_dist`, `w_tax`, `DISTANCE_NORM`, `TAX_NORM`) are
mathematically redundant with two integrator-facing aliases:
`miles_per_util = DISTANCE_NORM / w_dist`, `dollars_per_util = TAX_NORM /
w_tax`. The redundancy is kept for back-compat with earlier integrations.

**Business-facing surfaces (UI, agent, docs, presentation) quote the four
weights as a single calibration result — not four independent knobs**.
Three of them (`w_util`, `w_rented`, `w_tax`) were derived jointly from
one 4-D Pareto knee; `w_dist` was subsequently overridden on 2026-04-24
to reflect the carrier-cost reality. The business-facing narrative is
still "a single set of calibrated weights" — the override is tech-team
methodology detail, not a user-facing distinction.
`miles_per_util` / `dollars_per_util` are integrator-only aliases — never
quote them as business trade-off rates.

---

## Calibration — 4-D Pareto knee (2026-04-21) + cost-aware `w_dist` override (2026-04-24)

**Current weights in code** (final values):

| Weight | Value | Source |
|---|---|---|
| `w_util` | `1.335` | 2026-04-21 4-D Pareto knee |
| `w_rented` | `0.0574` | 2026-04-21 4-D Pareto knee |
| `w_dist` | `15.0` | **2026-04-24 override** (was `3.827` from knee) |
| `w_tax` | `1.950` | 2026-04-21 4-D Pareto knee |

### How the 4-D knee was derived

- **81,920 ILP simulations** in the 4-D weight space:
  - 1,024 Sobol quasi-random points in `(w_util ∈ [0.5, 5], w_rented ∈ [0, 0.2], w_dist ∈ [0.5, 5], w_tax ∈ [0.5, 5])`
  - × 8 fleet sizes (25, 30, ..., 60)
  - × 10 seeds per size
  - = 1,024 × 80 = 81,920 runs
- Objective — 4 Pareto criteria, no dollar-conversion assumed:
  - maximize `avg_rented_at_dest` — client review 2026-04-17 (cars currently rented at destination)
  - maximize `avg_util_at_dest` — destination utilization rate
  - minimize `avg_distance` — trucking miles per assignment
  - minimize `avg_annual_tax` — property-tax dollars per assignment
- Each of the 1,024 outcome 4-tuples normalized to `[0, 1]` using
  observed min/max. Selection: minimum Euclidean distance to utopia
  point `(1, 1, 1, 1)` in the normalized 4-D space.
- Historical reproducer: the original additive 4-D sweep script is not packaged
  in this branch. Re-run current calibration work through the bucket
  infrastructure under `sensitivity_analysis/bucket/`.
- Interactive visualization: `docs/viz/v5_efficient_frontier.html`.

### Cost-aware `w_dist` override (2026-04-24)

The 4-D knee's `w_dist=3.827` was derived from a **4-Pareto-objective
space** where `avg_distance` and `avg_annual_tax` were treated as
equally weighted (after min-max normalization). This is correct under
"no-dollar-information" assumptions — but implicitly treats a 1-σ shift
in distance as comparable to a 1-σ shift in annual tax.

Once realistic business cost assumptions are considered:

| Cost term | Typical per-vehicle magnitude | Source |
|---|---|---|
| Distance × carrier rate | ~280 mi × $2–$3/mi = **$560–$840/car/trip** | Industry-typical $/mi range; HCA to confirm |
| Annual property tax | ~$115/car/year (50-state WalletHub 2026 synthetic) | Synthetic benchmark documented in this section; HCA to replace with authoritative tax data |

Per-vehicle, distance cost is **5–7× larger than tax cost**. The knee's
`w_dist=3.827` therefore under-weighted distance relative to the real
$ calculus. Raising `w_dist` to `15.0` (holding the other three weights
at the knee) produces on a 50-car batch:

- **Total distance**: 14,114 → 10,215 mi (−3,899 mi, −28%)
- **Total tax**: $5,756 → $5,995 (+$239, statistically flat)
- **Cars at ≥80% util dealer**: 45 → 40 (−5 cars)
- **HHI concentration**: 1,050 → 821 (−22% — also addresses Jen's concern)

At $2/mi carrier cost, this nets ~+$7,300 savings per 50-car batch vs
the knee-default ILP. Derivation script: `scripts/explore_low_tax_weight.py`.
Median-distance-vs-Greedy verification: `scripts/median_distance_vs_greedy.py`.

**Why only `w_dist` was overridden and not `w_tax`**: reducing `w_tax`
counterintuitively *increases* total cost on this dataset, because the
solver stops avoiding the CO/MS/VA high-tax-rate dealers (1.8–4.0%) and
total tax rises by more than the weight shift saves in routing
flexibility. Empirically verified in `scripts/explore_low_tax_weight.py`:
at `w_dist=15`, `w_tax=0` produces $5,361 *more* tax per batch than
`w_dist=15`, `w_tax=1.95`. The 4-D knee's `w_tax=1.950` sits at the right
optimum already.

### Client stress test

`scripts/client_stress_test.py` verifies the calibrated weights pass the client's
2026-04-17 example: a hypothetical `114-car × 22% util` dealer must score
higher than a `3-car × 69% util` dealer. Under the 4-D-knee defaults:
**1.190 vs 0.497 — big dealer wins by +0.693** (vs +0.008 under the
earlier 1-D sweep; the 4-D knee restores a comfortable margin). The
`w_dist` override does not affect the client check because it's a
same-distance comparison.

### Collapse to $

All four weights plus both normalizers are unit-less scaffolding on top
of a mixed-dimensional objective. Once HCA shares:
- `$/car/month` rental revenue → replaces `w_util` and `w_rented` with
  true dollar coefficients;
- `$/mile` carrier rate → replaces `DISTANCE_NORM` / `w_dist`;
- authoritative vehicle property-tax data → firms up `TAX_NORM` / `w_tax`.

…the entire formula becomes pure "maximize expected annual profit" and
no calibration knobs remain.

---

## Data Layer

9 CSVs in `data_csv/` (8 read by the engine, 1 auxiliary for the agent).
Schema in `docs/DATA_SOURCE_SPEC.md`. Validate candidate data with
`python scripts/data_migration.py validate <dir>` — it's the only
subcommand; there is no `install` / `rollback` despite older doc versions
suggesting otherwise.

### Engine-read (8)
- `faas_eligible_vehicles.csv` — 650 grounded vehicles
- `dealer_inventory.csv` — 30 FaaS dealers + capacity + lat/lon
- `dealer_utilization.csv` — UTILIZATION (%), IN_SERVICE, RENTED per dealer
- `dealer_distance_matrix.csv` — 1,500 (source→FaaS) drivable-miles arcs
- `property_tax_by_state.csv` — 51 state rates, "%"-suffix strings
- `zip_centroids.csv` — 41,489 ZIP→lat/lon
- `fleet_inventory.csv` — baseline fleet state loaded into per-session snapshots
- `fleet_inventory_original.csv` — immutable Reset baseline

### Agent-only aux (1)
- `sales_tax_by_state.csv` — registered in `app/registry.json` for natural-language tax queries; not read by the engine.

### Distance data provenance
- `dealer_distance_matrix.csv` — drivable miles (verified ±1 mi vs Apple Maps).
- `dealer_distance_matrix_haversine.csv.bak` — original straight-line version kept for reference (haversine underestimates drivable by ~20%).

### `REMAINING_CAPACITY` definition (canonical)

```
REMAINING_CAPACITY = TRUE_CAPACITY − (every car accounted to this dealer)
```

"Every car accounted to this dealer" means **all of**: in-transit
(`fleet_inventory.STATUS = Transporting`), at the lot but not rented
(part of `IN_SERVICE`), and rented out to customers (`RENTED`). RENTED
cars **still consume a slot** even though they are physically with a
customer — the slot is the dealer's allocation cap, not their physical
parking footprint.

Current implementation (`engine.py:72, 82-88`):
`TRUE_CAPACITY − DELIVERED_COUNT − Transporting_count` where
`DELIVERED_COUNT` is `dealer_inventory.DELIVERED_COUNT` (the dealer's
total post-delivery count, includes RENTED) and `Transporting_count` is
counted live from `fleet_inventory.csv`. This matches the rule above.

**Do not "fix" REMAINING by subtracting RENTED — that interpretation
is wrong.** A rented car is still that dealer's car and still occupies
their allocation slot.

### User-facing "Capacity" terminology (canonical)

Whenever a user-facing surface (UI tooltip, table column, chat reply,
agent response, presentation slide, doc) uses the word **"Capacity"**
for a dealer, it means **`IN_SERVICE − RENTED`** — the dealer's idle
on-lot inventory (cars currently at the dealer not yet rented out).

This is non-negotiable. Examples:
- FaaS_Dealer_Gamma: IN_SERVICE=42, RENTED=40 → Capacity = 2
- FaaS_Dealer_Tango: IN_SERVICE=20, RENTED=12 → Capacity = 8

Mapping of UI labels → underlying engine fields:

| UI label | Computed from |
|---|---|
| `Utilization` | `UTIL_RATE` (= `RENTED / IN_SERVICE`) |
| `Rented` | `RENTED` |
| `In Service` | `IN_SERVICE` (= `DELIVERED_COUNT`) |
| `Capacity` | `IN_SERVICE − RENTED` |

The ILP's per-dealer slot constraint is a separate concept. Code-side
it's `REMAINING_CAPACITY = TRUE_CAPACITY − DELIVERED_COUNT − in_transit`;
in user-facing text refer to it as **"slot limit"** / **"slots remaining"**
/ **"dealer can take N more cars"** — never reuse the word "Capacity"
for it.

`TRUE_CAPACITY` is dealer-master scaffolding — never surface it on its
own. `IN_SERVICE` and `DELIVERED_COUNT` are numerically identical;
prefer "In Service" as the user-facing label.

**In-transit handling**: the engine's `REMAINING_CAPACITY` includes an
`in_transit` subtraction, but the in-transit feature is a future-task
scope item — current UI/agent surfaces should NOT lean on or display
in-transit math.

**Latent gap**: only `STATUS=Transporting` is subtracted from
the session fleet snapshot (`engine.py:82`). `STATUS=Delivered` is not
subtracted, and `dealer_inventory.DELIVERED_COUNT` is never written to
by the engine. If any future feature transitions Transporting →
Delivered (no such code today), those cars will silently disappear from
the capacity ledger and REMAINING will overstate. Either also subtract
`Delivered` in `load_data()`, or write `DELIVERED_COUNT` back to the
CSV at the transition point.

---

## API Contract

`app/server.py` exposes 16 REST routes plus the mounted MCP transport at
`/mcp`. All POST endpoints accept JSON bodies matching Pydantic models.
Every allocation-producing endpoint accepts all four weights (threaded
end-to-end to the solvers).

| Method | Path | Purpose | Accepts weights |
|---|---|---|---|
| GET | `/` | Serves `index.html` | — |
| GET | `/api/overview` | Fleet + dealers dump (incl. per-dealer `RENTED` and `IN_SERVICE`) | — |
| GET | `/api/home` | Home page data — 8wk trends (synthetic, flagged `_placeholder=true` until allocation history is persisted), fleet pipeline-by-week (4 weeks, prior 3 flagged `_synthetic=true`), dealer util heatmap (30 cells), top moves narrative (rule-based from cached batch), `watchlist` (alerts from `state.last_watchlist`, empty until user clicks Refresh) + `watchlist_meta` (`refreshed_at`, `count`, `max_items`). Hero `$` savings computed client-side from cached `weeklyData` + `weeklyGreedyData`. | — |
| POST | `/api/watchlist/refresh` | Recompute watchlist signals (6 deterministic aggregators in `app/watchlist/signals.py`: saturation / deferred_batch / capacity_mismatch / stuck_vehicles / underutilized / source_pressure — all real data, no fabrication) and run the watchlist agent (one-shot `claude -p --output-format json` subprocess, no tools/MCP; falls back to deterministic template rendering on CLI failure; usage returned in the `X-Demo-Usage` response header). Returns up to `MAX_ITEMS=5` ranked alerts. Caches result in `state.last_watchlist`. | — |
| GET | `/api/defaults` | Canonical scoring defaults (`engine.DEFAULT_W_*`) for frontend boot | — |
| GET | `/api/sessions` | Diagnostic session-store snapshot; not used by business users | — |
| GET | `/api/fleet` | Fleet inventory with filters | — |
| POST | `/api/allocate` | Run Greedy + V2 ILP (full fleet-level) | ✓ |
| POST | `/api/weekly` | Weekly batch ILP (user-selected VINs) | ✓ |
| POST | `/api/weekly_greedy` | Weekly batch Greedy (comparison) | ✓ |
| POST | `/api/confirm` | Apply assignments to the current session's in-memory fleet snapshot | — |
| POST | `/api/reset` | Restore the current session's fleet snapshot from the immutable baseline | — |
| GET | `/api/allocation_cache` | Last allocation result, filterable | — |
| GET | `/api/export/confirmed` | Future export adapter stub: returns confirmed `Transporting` vehicles from the current session as JSON; does not write files or call external systems | — |
| POST | `/api/approve_constraint` | Mint a one-shot approval token for `add_constraint` / `remove_constraint` (Rule 9 gate) | — |
| POST | `/api/chat` | Agent streaming SSE response. `ChatRequest.client_id` (UUID) binds the browser session, Claude CLI session, and FaaS MCP tool state; no `history` field on the request. Emits a final `demo_usage` SSE line for the gateway. | — |
| ANY | `/mcp/*` | MCP streamable-http transport (the Claude CLI agent talks to this via an inline `--mcp-config`; requires the `X-Internal-Key` header, 403 otherwise) | — |

The older `POST /api/sensitivity` (supply-scarcity sweep) was removed on
2026-04-21. If that analysis is needed again, add a small endpoint wrapping a
fresh analysis function rather than resurrecting the old engine-level path.

Every Pydantic request model defaults all four weights to
`engine.DEFAULT_W_*` (currently `w_util=1.335, w_rented=0.0574,
w_dist=15.0, w_tax=1.950`). The frontend fetches these from
`GET /api/defaults` at boot and echoes them on every weekly request,
so changing `engine.DEFAULT_*` is the only place to tune — the
frontend/API/agent stay in sync automatically.

---

## Agent + Tools Layer

### MCP server (`app/mcp_server.py`)

14 agent-callable tools registered via FastMCP. Tool registration is
**native, not prompt-injected**: the Claude CLI receives the repo-local FaaS
MCP server through an inline `--mcp-config` and dispatches calls itself;
agent.py never parses tool calls out of model text. The MCP app is mounted
on the FastAPI process at `/mcp` (streamable-http transport), so tool
functions call `engine.solve_*` / `state.cached()` directly in-process —
no urllib self-calls, no deadlock surface. The mount requires the internal
`X-Internal-Key` header (403 otherwise) so only the app's own agent
subprocess can reach it.

**Read-only / analysis tools (12)** — safe to call without user gate:

1. `list_datasets` — list `registry.json` entries
2. `inspect_data(dataset_name)` — schema + stats + 3 sample rows
3. `query_data(code)` — pandas eval in a restricted-builtins namespace; 5 s timeout. AST-validated: imports, `_`-prefixed / dunder attribute access, and the dangerous callables (`open` / `eval` / `exec` / `getattr` / `globals` …) are rejected; `pd.read_csv` is gated to registered datasets.
4. `run_allocation(w_util? / w_rented? / w_dist? / w_tax? / n_vehicles? / miles_per_util? / dollars_per_util?)`
   — KPI summary under the 2026-04-21 additive formula. All weights default to `engine.DEFAULT_*` (4-D knee for three weights + cost-aware `w_dist=15.0` override). Populates `state.last_allocate`.
5. `list_constraints`
6. `analyze_new_file(file_path)` — schema + semantic-conflict detection
7. `get_solver_code` — engine source code for explanation
8. `get_allocation_result(vin? / dealer?)` — filter cached result. Returns `{"status":"no_allocation_yet"}` (NOT an error) when nothing is cached.
9. `analyze_weekly_batch(...)` — what-if weekly batch under custom weights. Engine validates VINs against `faas_eligible_vehicles.csv` upfront and returns `{"status":"error","error":"Unknown VINs: ..."}` on bad input.
10. `analyze_override_impact(vin, alt_dealer_code)` — single-vehicle override simulation. Reads `state.cached()` directly; returns `no_allocation_yet` cleanly if cache empty.
11. `compare_ilp_vs_greedy(...)` — method comparison
12. `analyze_capacity_change(dealer_code, new_capacity, vin_list)` — perturb one dealer's `REMAINING_CAPACITY` on a **private copy** (a per-call `dealer_capacity_overrides` passed to `solve_weekly_batch`), rerun ILP, report delta KPIs. The shared baseline is never mutated, so concurrent sessions are unaffected.

**Mutating tools (2)** — gated by a one-shot `approval_token`:

13. `add_constraint(name, description, code, approval_token?)` — registers an in-session plugin (`SessionState.constraints`), loaded by `_solve_v2`. The code is AST-whitelisted (`constraints.validate_constraint_source`) and rejected before it is stored if unsafe; it later runs with an empty builtins map + a small safe namespace (`lpSum` + pure helpers). Without a valid token, returns `{"status":"needs_approval","code_preview": ...}`. The frontend displays the preview, user clicks Accept, frontend calls `POST /api/approve_constraint` to mint a token, agent re-invokes with the token.
14. `remove_constraint(name, approval_token?)` — same gating pattern.

This replaces the previous prompt-discipline-only Rule 9 enforcement
with a real per-tool approval gate. The token mint endpoint is the only
trust boundary the agent cannot bypass.

### Agent driver (`app/agent.py`)

Runs one `claude -p` subprocess per turn:
- Command: `claude -p --output-format stream-json --verbose
  --include-partial-messages --tools "" --mcp-config <inline JSON>
  --strict-mcp-config --allowedTools mcp__faas__* --permission-mode dontAsk
  --setting-sources "" --disable-slash-commands --system-prompt <SYSTEM_PROMPT>
  --model <FAAS_CLAUDE_MODEL|opus>`.
- The inline MCP config is an `http` server at
  `/mcp/?faas_session_id=<client_id>` carrying the internal `X-Internal-Key`
  header. cwd is a dedicated empty scratch dir (`app/var/agent_cwd`), never the
  repo.
- First turn: the CLI mints a session id (captured from the `system/init`
  event and stored per browser session). Follow-up turns pass
  `--resume <session_id>`.
- Never uses `bypassPermissions` and never exposes any built-in tool — only the
  faas MCP tools are allow-listed.

The CLI internalizes the agent loop. `agent.py` is a thin translator: it parses
stream-json events and emits the existing SSE event shape (`token`,
`tool_start`, `tool_done`, `answer`, `focus`), then a final `demo_usage` line
built from the CLI `result` event for the gateway (frontend ignores it). Only
`mcp__faas__*` tool calls are surfaced to the UI.

### System prompt (`app/agent.py` `SYSTEM_PROMPT`)

- Documents the additive formula `w_util × UTIL + w_rented × RENTED − w_dist × dist/NORM − w_tax × tax/NORM`.
- Positions the agent as HCA's FaaS optimizer and a business decision-support analyst, not a tool catalog. It always answers in English. Capability replies start with "I am HCA's FaaS optimizer," then use Markdown section labels such as **What I can do** and **What I cannot do** to explain capabilities (assignment explanation, manual-review flags, dealer alternatives, override impact, ILP-vs-Greedy comparison, slot-limit analysis, data inspection, allocation-logic explanation) and user-approval boundaries (confirm allocations, reset fleet state, apply overrides, add/remove constraints).
- Explains the joint calibration: `w_util / w_rented / w_tax` from the 4-D Pareto knee; `w_dist` is the 2026-04-24 cost-aware override.
- Emphasizes `IN_SERVICE` is NOT a scoring input (only RENTED is).
- Enforces rank-primary discipline (agent Rules 6–7 in `SYSTEM_PROMPT`; distinct from the "Rules for the coding agent" at the end of this file).
- Forbids surfacing `miles_per_util` / `dollars_per_util` / `DISTANCE_NORM` / `TAX_NORM` / `beta` / `gamma` to users (agent Rule 5). High-level four-weight vocabulary is allowed.
- Distinguishes ILP-chosen non-rank-1 (capacity/optimization) from user
  overrides (explicit dropdown click).
- **Rule 8 — analyst, not operator.** Agent must draft constraint code in
  plain text first; only call `add_constraint` after the user approves
  in chat. The MCP server's `approval_token` gate enforces this at
  runtime, but the prompt rule keeps the conversational discipline.
- **Rule 10 — narrow-scope deferral.** Agent KNOWS the architecture, formula
  shape, algorithm, data layer, and UI workflow. Agent DOES NOT act as the
  authoritative source for unpublished HCA-specific parameter tables or future
  production calibration decisions; those defer to Tech Team handoff materials.

### Registry (`app/registry.json`)

Describes every CSV (including aux `sales_tax_by_state`) with per-column
`semantic` strings. `dealer_utilization.RENTED`'s semantic marks it the
primary demand signal (enters `alloc_score` via `w_rented × RENTED`
without normalization). `dealer_utilization.IN_SERVICE`'s semantic
marks it operational state, explicitly NOT a scoring input (2026-04-21
pivot).

### Watchlist module (`app/watchlist/`)

Real-time fleet anomaly detection — separate from the MCP chat agent. Surfaces
on the Home tab's Watchlist section. User-triggered (Refresh button → `POST
/api/watchlist/refresh`); no cron.

Three layers, four files:

1. **`signals.py`** — 6 deterministic aggregators, all real data (no fabrication):
   - `saturation(min_util=0.85)` — dealers at high util. Severity bands: critical ≥0.95, high ≥0.90, medium ≥0.85.
   - `deferred_batch()` — vehicles in `state.cached()` with `assigned=None`. Aggregated by source state.
   - `capacity_mismatch()` — deferred vehicles in states where dealer idle capacity exists (routing puzzle).
   - `stuck_vehicles(weeks_threshold=4)` — `STATUS=Grounded` for > 4 weeks based on `fleet_inventory.csv.WEEK`.
   - `underutilized(max_util=0.55, min_in_service=10)` — dealers with significant idle capacity.
   - `source_pressure(min_count=8)` — source dealers with high count of grounded vehicles awaiting allocation.

   Each returns `Signal` dicts (`id`, `type`, `severity`, `metric`, `context`,
   `recommended_action`). `all_signals()` aggregates across all six. Thresholds
   tuned 2026-05-14 so ≥5 signals exist on real fleet data so the agent has
   enough material to rank to `MAX_ITEMS=5`.

2. **`templates.py`** — 9 alert-shape templates the agent picks from:
   `saturation_single` / `saturation_with_nearby_idle` / `deferred_simple` /
   `deferred_multi_region` / `capacity_mismatch` / `stuck_vehicles_concentrated` /
   `stuck_vehicles_distributed` / `underutilized_dealer` / `source_pressure`.
   Each has `description`, `fields`, `example_format`. `render_fallback(signal)`
   produces a deterministic alert per signal type when the agent is unavailable.

3. **`agent.py`** — Claude CLI subprocess wrapper. Spawns
   `claude -p --output-format json --tools "" --strict-mcp-config
   --mcp-config '{"mcpServers":{}}' --setting-sources "" --disable-slash-commands
   --permission-mode dontAsk --model ${FAAS_CLAUDE_MODEL_WATCHLIST:-sonnet}` as a
   one-shot (non-streaming, no tools, no MCP), prompt on stdin, empty cwd.
   One-shot prompt: signals + template menu → "rank by severity × impact, pick
   templates, fill placeholders with values FROM signal context only — no
   invention". Returns up to `MAX_ITEMS=5` alerts (`signal_id`, `template_id`,
   `severity`, `title`, `body`, `action_label`) plus a usage payload (surfaced
   by the endpoint as the `X-Demo-Usage` header). 60s timeout. Falls back to
   deterministic `render_fallback()` on every failure mode (CLI not in PATH,
   timeout, non-zero exit, JSON parse failure).

The watchlist agent is NOT registered as an MCP tool and is not in the main
chat agent's tool menu — it's a separate subprocess invoked only via the
`/api/watchlist/refresh` endpoint.

---

## Sensitivity Analysis Infrastructure

`sensitivity_analysis/` — lives outside `app/`, not imported at runtime.

Current branch contents are bucket-calibration artifacts, not the full archived
additive-sweep tree:

- `bucket/README.md` — bucket calibration overview.
- `bucket/sweep_bucket.py` — Sobol sweep for the bucket scoring design.
- `bucket/compare_modes.py` / `bucket/compare_weights.py` — comparison helpers.
- `bucket/output/sweep_v1.parquet` and `bucket/output/knee_v1.txt` — current
  bucket sweep outputs.
- Interactive charts: `docs/viz/v5_efficient_frontier.html`,
  `docs/viz/bucket_vs_additive.html`, and
  `docs/viz/bucket_weights_comparison.html`.

**When to re-run**: if HCA swaps in new fleet data, re-run the current
calibration path for the active scoring mode. For the bucket track, use
`sensitivity_analysis/bucket/sweep_bucket.py` and update the bucket defaults,
docs, and visualizations if the knee shifts materially.

---

## Calibration Paths

Code defaults: `w_util = 1.335`, `w_rented = 0.0574`, `w_dist = 15.0`,
`w_tax = 1.950`. Three of these (`w_util`, `w_rented`, `w_tax`) come from
the 2026-04-21 4-D Pareto knee. `w_dist` was overridden on 2026-04-24
from the knee value of `3.827` to `15.0` to reflect the business cost
structure (per-mile carrier cost dominates per-car annual property tax
by 5–7×; see "Cost-aware `w_dist` override" section above). All four are
scaffolding to replace when HCA shares `$/car/month` rental revenue and
`$/mile` carrier rate — at which point the override also becomes moot
and all four weights plus the `DISTANCE_NORM` / `TAX_NORM` scaffolding
collapse into dollar coefficients, and the problem becomes pure profit
maximization.

**Interim calibration option** (if business wants to override the math):
inverse-optimize the four weights from 10–20 user-marked override
examples. Typically converges after ~15 examples.

---

## Roadmap

### Next milestone (committed): Tier-based scoring pivot

Replaces the continuous additive `w_util × UTIL + w_rented × RENTED`
util-side terms with a single tier classification. Final working form:

```
score = bucket_mult(tier_of(d))  −  w_dist × dist/NORM  −  w_tax × tax/NORM
```

Both `w_util` and `UTIL` are dropped from the bucket form. `bucket_mult`
IS the util-side score (e.g. `[2.0, 1.4, 1.0, 0.6]` over 4 demand-rank
tiers as the working assumption); within a tier, all dealers tie on
util and distance/tax break ties.

**Why pivot.** The current additive formula's weights are derived from
a 4-D Pareto knee, but they cannot answer the natural business
question "+10% UTIL is worth how many miles?" without HCA's
`$/car/month` and `$/mile` data. The tier form converts the trade-off
question to **"Tier 1 vs Tier 2 is worth how many miles?"** —
categorical, business-actionable, defensible without dollar inputs.

**Trade-offs accepted.**
- Loss of intra-tier discrimination (200-RENTED dealer ties with
  80-RENTED dealer if both top-tier).
- Boundary-cliff at quartile cuts.
- Possible HHI worsening (within-tier driving by distance + tax may
  concentrate at geographically convenient top-tier dealers — needs
  empirical test).
- New calibration debt: `bucket_mult` set, bucket sizes, bucketing
  signal (RENTED vs UTIL_RATE), and joint cost weights need a Pareto
  sweep before the form is shippable.

**Plan to final.** (1) Reframe scoring (engine + agent + UI); (2)
calibrate via Pareto sweep over `(mult set × bucket sizes × bucketing
signal × w_dist × w_tax)` using the same methodology as the current
4-D sweep; (3) A/B against the current additive ILP on identical
batches — ship only if equal-or-better on every KPI.

This pivot does **not** retire the long-term pure-$ objective. Once
HCA shares `$/car/month` rental revenue and `$/mile` carrier rate, the
bucket form collapses the same way the current form does — both are
scaffolding.

### Planned: Historical Allocation Review

Allow fleet managers to view and modify past allocation records:
- Browse allocation history by date/week
- View details of each past allocation (vehicles, dealers, scores, costs)
- Modify/override past decisions and track changes
- Compare current allocation against historical patterns

Requires persisting allocation results beyond the current in-memory
`state.last_weekly` / `state.last_allocate` cache (`app/state.py`) —
likely RDS Postgres when the deployment track picks it up.

### Optional follow-up

- **Per-mile carrier rate integration** — when HCA shares $/mile, swap
  `cost_proxy = miles + $tax` for pure-dollar cost; `DISTANCE_NORM`
  scaffolding retires.
- **Per-car monthly rental revenue** — replaces `w_util × UTIL +
  w_rented × RENTED` with a single $-denominated revenue term. Both
  `w_util` and `w_rented` retire entirely.
- **Re-enable `source_limit` with real throughput data** — currently
  disabled because no carrier data supports a specific cap.
- **New-dealer seeding** — for new FaaS dealers with `RENTED = 0`, the
  additive form gives them `w_util × UTIL` contribution (no hard
  zero-out). A seeding policy — e.g. give new dealers 5 cars in week 1
  so they can build RENTED history — is roadmap-level post-midterm.

---

## Git Workflow

See [GIT_WORKFLOW.md](GIT_WORKFLOW.md) for details.

- **Main = core code only.** Docs, validation files, meeting notes,
  utility scripts stay out of `main`.
- **One branch per purpose.** Never mix MVP work with validation on the
  same branch.
- Enforcement is **discipline-based**: `.gitignore` only excludes
  user-local state (venv, caches, secrets). Stage files explicitly
  (`git add <specific-files>`) rather than `git add .`.
- Active branches: `presentation-ready` (default), `main` (archival),
  `deployment` (HCA environment), `manual-validation` (legacy QA).

---

## Rules for the coding agent

1. **Before editing any file**, run `git branch` to confirm which branch
   you are on. State the branch name before proceeding.
2. **AGENTS.md is local to this showcase copy.** Do not copy it
   back to legacy branches unless the user explicitly asks.
3. **Thread new knobs through all layers.** If you add a new business
   parameter to `engine._build_pairs`, you MUST also thread it through
   `_solve_v2`, `_enrich`, `solve_both`, `solve_weekly_batch`,
   `solve_weekly_greedy`; then the Pydantic models (`AllocateRequest`,
   `WeeklyRequest`) in `server.py`; then `tools.py run_allocation` spec
   + signature; then `app.js` API calls; then the system prompt in
   `agent.py`. All layers see it or none do.
4. **Rank is primary; raw `alloc_score` is secondary.** Never design
   new UI or agent responses that lead with raw score. Rank is the
   only per-vehicle quality metric safe to compare; `rank1_pct` /
   `avg_rank` are the only batch-level metrics to show business users.
5. **Never surface internal implementation details** to business users:
   `w_dist`, `w_tax`, `DISTANCE_NORM`, `TAX_NORM`, `miles_per_util`,
   `dollars_per_util`, `alloc_score` (as a quantity), `quality_pct`
   (as a quality metric), or retired symbols (`beta`, `gamma`,
   Conservative/Balanced/Aggressive). These belong in integrator docs only.
6. **`IN_SERVICE` is NOT a scoring input.** The 2026-04-21 pivot moved
   size-awareness onto `RENTED` (absolute demand), not `IN_SERVICE`
   (placement state). Surface IN_SERVICE in the UI for operational
   context, but never include it in score-explanation language.
7. **Docs and code comments must be in English.** Chat responses may
   be in Chinese when the user writes Chinese, but persistent artifacts
   (markdown, prompts, comments) are English-only.
8. **Before asserting a trade-off direction** ("A beats B when
   condition X"), derive the breakeven inequality from the underlying
   deltas. Do not echo the user's sign without verifying.
9. **Session logs are append-only.** `docs/session_log_YYYY-MM-DD.md`
   captures discussion / decisions / fixes per session. Don't edit
   history; start a new log instead.
10. **Validation scripts output raw data.** The user (non-technical HCA
    stakeholders included) does the verification. Don't write validation
    logic — dump results to CSV/Excel + a plain-English "how to check"
    guide.
