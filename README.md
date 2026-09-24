> **Anonymized sample data. Values perturbed; not actual HCA figures.**

# FaaS Vehicle Allocation Optimizer

A decision-support app that recommends a destination dealer for every vehicle in a weekly batch of Hyundai Capital America's Fleet-as-a-Service (FaaS) program. It solves the assignment as a two-stage integer linear program, compares it side by side with the distance-only baseline HCA uses today, and adds an AI advisor that answers questions, runs what-if scenarios and drafts allocation rules through 14 tools. The operator approves every change.

Built for the UCI MSBA capstone with Hyundai Capital America, January to June 2026, by team ZotLogic.

A hosted demo is available on request.

## Case study

**User problem.** HCA redeploys off-lease vehicles from 50 grounding locations to 30 FaaS dealers, where rideshare drivers rent them. Each week the Central Data Office sends each new vehicle to the nearest dealer with open capacity. That rule ignores how busy the destination already is, whether its cars are actually being rented, and whether the destination state charges property tax on inventory. Cars sat idle at saturated dealers and picked up avoidable tax, and the manual process did not scale or leave an audit trail.

**Workflow.** Load the week's grounded vehicles, dealer capacity, utilization, the drivable-distance matrix and state tax tables from CSV. Score every vehicle and dealer pair on utilization, rented demand, distance and property tax. Solve the whole batch at once with an ILP, which lets it send one car a little farther so that the rest of the batch lands better. Show the result next to the greedy baseline with rank-based KPIs. The operator reviews it, overrides any row, and confirms. The advisor explains single assignments ("why did this VIN go to Harbor?"), tests what-ifs such as removing slots from a dealer before anything is committed, and turns plain-English rules into constraints that need operator approval.

**Tools.** Python, FastAPI, PuLP with CBC, pandas, a vanilla HTML, CSS and JavaScript dashboard with Leaflet maps, Server-Sent Events for streaming, an MCP server exposing 14 tools, and the Claude CLI as the agent runtime. The client's target platform was Claude on AWS Bedrock, so the agent layer talks only to the application's own tools and never to a database.

**Testing.**
- 168 pytest tests in `app/tests/`, run in CI on every push. They cover the solver, scoring, the SSE and agent event contract, the REST contract, frontend static contracts, and security, including malicious constraint and query payloads.
- A benchmark of 5,000 vehicles, 100 batches of 50, run through all three methods with the same inputs (`scripts/benchmark_three_methods.py`).
- Weight calibration from an 81,920-run sweep, picking the 4-D Pareto knee (`sensitivity_analysis/bucket/`, `docs/viz/`).
- Biweekly working sessions with the HCA Central Data Office to check the results against business intent.

**Result.** From the final client report, measured on HCA's development dataset:

| Metric | Greedy baseline | Additive ILP | Bucket ILP |
|---|---|---|---|
| Average destination utilization | 67.1% | 82.7% | 82.6% |
| Vehicles placed at high-utilization dealers | 67% | 83% | 83% |
| Annual tax exposure vs Greedy | baseline | −47%, about $5,800 per 50-car batch | −45%, about $5,600 per 50-car batch |
| Solve time per 50-car batch | 0.07 s | about 0.2 s | about 0.2 s |

The cost is distance. The ILP methods drive roughly 9,300 to 9,800 miles per batch against 5,200 for greedy, which is why the distance weight is calibrated to carrier cost per mile rather than left at the sweep's optimum. The HCA team gave positive feedback on letting non-technical staff configure constraints through conversation.

**What I learned.**
- A baseline that mirrors the current process is what makes the result credible. Clients compare against what they do today, not against a theoretical optimum.
- Show the tradeoff, do not hide it. Presenting the extra miles next to the tax and utilization gains turned the calibration into a business decision the client could own.
- Keep the agent on a short leash. The advisor recommends and explains; the optimizer computes; the operator decides. For this public demo the agent runs with no shell or file tools at all, only the 14 application tools, and every agent-written constraint passes an AST whitelist before it can touch the solver.
- Tests are the acceptance gate for AI-written code. Every behavior the client relied on has a contract test, so refactors and agent-generated changes cannot silently change it.

## My contribution

This was a five-person team project. I was project manager and technical lead. I designed and built everything in this repository: the scoring model and its calibration, the ILP and greedy solvers, the FastAPI backend, the dashboard, the MCP tool layer and AI advisor, the watchlist, the test suite and CI. Every commit in the original repository is mine. The other four members covered the non-technical side of the engagement.

## How it was built

I wrote the specifications and the architecture, and implemented them with coding agents, Claude Code and Codex, using the test suite as acceptance. `AGENTS.md` is the working brief those agents followed, and `docs/` holds the specs behind each feature.

## Run it locally

Requires Python 3.11 or newer.

```bash
python bootstrap.py
```

The launcher creates `.venv`, installs `app/requirements.txt`, and opens `http://localhost:8000`. The chat advisor is optional and needs the Claude CLI installed and logged in (`claude --version`). Without it the rest of the app works and the chat panel shows a disabled state.

Tests:

```bash
.venv/bin/python -m pytest app/tests/ -q
```

## About the data

`data_csv/` is an anonymized sample derived from the development dataset HCA provided. Dealer names and codes were replaced, coordinates moved a few miles with distances rescaled to match, VINs renumbered, and capacity, residual value and odometer perturbed by up to 15%. The headline numbers above come from the final report on the original dataset; rerunning the benchmark on this sample gives similar but not identical figures. State tax rates and ZIP centroids are public data.

---

## Using the app

The UI has **4 tabs** (Home is the default landing page):

1. **Home** — fleet-wide brief. Hero card with this week's estimated savings
   vs Greedy baseline, 8-week sparkline trends, fleet state donut + pipeline
   by week, dealer performance (geographic map, util-band heatmap, util×rented
   scatter), top moves narrative cards, and a watchlist. Click **↻ Refresh**
   in the watchlist section to compute up to 5 anomaly alerts.
2. **Fleet Inventory** — browse grounded vehicles. Select ~15 cars to allocate,
   then click **Allocate Selected** → jumps to Weekly Allocation.
3. **Weekly Allocation** — review the KPI cards (Vehicles / Assigned /
   At ≥80% Util Dealer / Avg Dest Util / Total Distance) and the per-vehicle
   table. The **Choice** column shows the algorithm's rank (Rank 1 = top
   pick). Override any assignment from the dropdown — the KPIs update live.
   **Confirm Allocation** persists the decisions inside the current browser
   session's in-memory fleet snapshot.
4. **Batch Overview** — per-batch dashboard with the ILP vs Greedy banner,
   side-by-side comparison, allocation routes map, rank distribution
   histogram, and per-dealer load.

**Reset** restores the current browser session's in-memory fleet snapshot from
the baseline fleet data for another demo run. It does not write to disk.

**Chat panel** (right side) — ask the agent questions in plain English.
Visible on Fleet Inventory / Weekly Allocation / Batch Overview; hidden on
Home (read-only by design).

---

## How the allocation works

The score for assigning vehicle *v* to dealer *d*:

```
alloc_score = w_util × UTIL_RATE_d
            + w_rented × RENTED_d
            − distance(v,d) / DISTANCE_NORM
            − annual_property_tax(v,d) / TAX_NORM
```

Two additive positive signals (destination utilization rate, absolute count
of currently-rented cars at the destination) and two cost penalties
(transportation distance, state property tax). `RENTED` enters
**without normalization** so absolute demand is preserved regardless of
other dealers' data.

**Four weights** — three from an 81,920-simulation 4-D Pareto knee;
`w_dist` overridden to reflect that per-mile carrier cost dominates per-car
annual property tax by 5–7× in the real business cost structure:

| Param | Code default | What it weighs |
|---|---|---|
| `w_util` | 1.335 | UTIL_RATE signal (destination dealer's per-car efficiency) |
| `w_rented` | 0.0574 | RENTED signal (absolute earning cars at destination) |
| `w_dist` | 15.0 | Distance penalty, scaled to actual carrier cost |
| `w_tax` | 1.950 | Property-tax penalty |

Business users see **rank-based** metrics: **% at Rank 1** (how often the
algorithm's top pick was used) and **Avg Rank** (1.0 = every car at its
top choice). Raw scores are not comparable across vehicles and are kept
internal — see [`docs/allocation_scoring_explained.md`](docs/allocation_scoring_explained.md)
for the methodology in detail.

All weights and normalizers will collapse into pure $-denominated
coefficients once HCA shares per-car monthly rental revenue and per-mile
carrier rate.

---

## Repository layout

```
faas-allocation-optimizer/
├── bootstrap.py            # cross-platform launcher (Windows-compatible)
├── README.md               # this file
├── app/                    # application code
│   ├── server.py           # FastAPI backend, SSE chat, REST + MCP endpoints
│   ├── engine.py           # ILP + Greedy solvers, additive scoring
│   ├── agent.py            # Claude CLI subprocess driver (stream-json + MCP)
│   ├── mcp_server.py       # 14 agent-callable tools mounted at /mcp
│   ├── state.py            # browser-session state isolation
│   ├── watchlist/          # fleet anomaly detection (deterministic aggregators + agent)
│   ├── constraints/        # user-defined constraint plugins (loader)
│   ├── static/             # frontend: index.html, app.js, style.css
│   ├── registry.json       # CSV schemas for agent tools
│   ├── requirements.txt    # Python deps
│   └── tests/              # pytest contract tests
├── data_csv/               # anonymized sample, 9 CSVs (8 read by engine, 1 auxiliary)
│   ├── faas_eligible_vehicles.csv     # 650 grounded vehicles
│   ├── dealer_inventory.csv           # 30 FaaS dealers + lat/lon
│   ├── dealer_utilization.csv         # UTILIZATION, IN_SERVICE, RENTED
│   ├── dealer_distance_matrix.csv     # 1,500 (source→FaaS) drivable miles
│   ├── property_tax_by_state.csv      # 51 state rates
│   ├── sales_tax_by_state.csv         # aux (agent queries only)
│   ├── zip_centroids.csv              # 41k ZIP→lat/lon lookup
│   ├── fleet_inventory.csv            # baseline fleet state loaded into sessions
│   └── fleet_inventory_original.csv   # immutable reset baseline
├── docs/                   # business-facing methodology docs
│   ├── API.md                         # REST API contract + OpenAPI entrypoints
│   ├── DATA_SOURCE_SPEC.md           # CSV schema contract
│   ├── allocation_scoring_explained.md  # methodology in detail
│   └── bucket_algorithm.md           # bucket scoring follow-up design
└── scripts/
    ├── data_migration.py             # read-only CSV validator
    └── benchmark_*.py / *.csv        # validation evidence for the deck
```

---

## Testing details


Run the pytest contract tests:

```bash
.venv/bin/python -m pytest app/tests/ -q
```

Current collection: 168 tests.

API documentation is generated by FastAPI at `http://localhost:8000/docs` when running locally. The hosted demo sets `DEMO_MODE=1`, which turns these pages off.

See [`docs/API.md`](docs/API.md) for the session header contract, endpoint
groups, and future export adapter note.

Quick engine smoke test:

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, 'app')
import engine
r = engine.solve_weekly_batch(n_batch=17, seed=3)
print(f'avg_rank={r[\"avg_rank\"]}, rank1={r[\"rank1_pct\"]}%, assigned={r[\"n_assigned\"]}/17')
"
# Expected on the sample data: avg_rank 1.12, rank1 88.2%, assigned 17/17
```
