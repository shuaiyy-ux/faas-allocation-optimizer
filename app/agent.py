"""
FaaS Optimizer — multi-turn agent loop powered by the Claude CLI + MCP.

The backend launches a local `claude -p` subprocess per turn, lets the CLI
internalize the agent/tool loop, and translates its `stream-json` events into
the existing SSE event shape consumed by the frontend.

Safety posture for the public token-gated demo:
- built-in tools are disabled (`--tools ""`); only the FaaS MCP tools are
  allow-listed (`--allowedTools mcp__faas__*`);
- MCP config is inline and strict (`--strict-mcp-config`), pointing at this
  process's own `/mcp` endpoint and carrying a random `X-Internal-Key` header
  so nothing else can reach the MCP surface;
- no user/project/local settings load (`--setting-sources ""`), slash commands
  are off (`--disable-slash-commands`), and the working directory is a
  dedicated empty scratch dir, never the repository.

Multi-turn fidelity is provided by Claude CLI sessions: the first turn lets the
CLI mint a session id (captured from the `system/init` event); subsequent turns
resume it with `--resume <session_id>`.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import quote
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Dict, Any, Generator

import mcp_server

# ── Logging ──────────────────────────────────────────────────────────
# Tool errors used to be discarded after the SSE stream — frontend
# rendered a red ✗ but no error string anywhere on disk. See
# docs/agent_tool_logging_2026-04-28.md for the diagnosis.
_LOG_PATH = Path(__file__).resolve().parent / "server.log"
_logger = logging.getLogger("faas.agent")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _handler = RotatingFileHandler(_LOG_PATH, maxBytes=2_000_000, backupCount=3)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False

# ── Constants ────────────────────────────────────────────────────────

CLAUDE_TIMEOUT = 180  # seconds (CLI + MCP tool runtime; one ILP can take a few seconds)

PROJECT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
SERVER_PORT = int(os.environ.get("PORT", "8000"))
TMP_DIR = Path(tempfile.gettempdir())

# Dedicated empty working directory for the Claude CLI subprocess. NEVER the
# repo root: the CLI must not see project files, CLAUDE.md, or settings.
# FAAS_VAR_DIR moves it outside the checkout for deployment.
VAR_DIR = Path(os.environ.get("FAAS_VAR_DIR") or APP_DIR / "var")
AGENT_CWD = VAR_DIR / "agent_cwd"


def _agent_cwd() -> str:
    try:
        AGENT_CWD.mkdir(parents=True, exist_ok=True)
    except OSError:
        return str(TMP_DIR)
    return str(AGENT_CWD)


def _safe_session_id(session_id: str) -> str:
    """Encode session IDs before embedding into file names or URLs."""
    return quote((session_id or "").strip(), safe="")


def cleanup_stale_mcp_configs(max_age_seconds: int = 3600) -> int:
    """Remove `/tmp/faas-mcp-*.json` files older than `max_age_seconds`.
    Called once from the FastAPI lifespan on startup so the /tmp dir
    doesn't accumulate one file per ever-created session. Returns count
    of files deleted (best-effort, swallows errors)."""
    cutoff = time.time() - max_age_seconds
    deleted = 0
    try:
        for p in TMP_DIR.glob("faas-mcp-*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    deleted += 1
            except OSError:
                continue
    except OSError:
        pass
    return deleted

# Tool names exposed by mcp_server.py.
_FAAS_MCP_TOOLS = [
    "list_datasets", "inspect_data", "query_data",
    "run_allocation",
    "add_constraint", "list_constraints", "remove_constraint",
    "analyze_new_file", "get_solver_code",
    "get_allocation_result",
    "analyze_weekly_batch", "analyze_override_impact",
    "compare_ilp_vs_greedy", "analyze_capacity_change",
]
_FAAS_TOOL_SET = set(_FAAS_MCP_TOOLS)
# Claude CLI checks MCP tool permissions as mcp__<server>__<tool>.
_ALLOWED_TOOLS = [f"mcp__faas__{t}" for t in _FAAS_MCP_TOOLS]

# Map faas MCP session id (the browser client_id) -> the Claude CLI session id
# minted on the first turn, so follow-up turns resume the same conversation.
_CLAUDE_SESSIONS: Dict[str, str] = {}


def _stored_claude_session(session_id: str, is_first_turn: bool) -> Optional[str]:
    if is_first_turn:
        return None
    return _CLAUDE_SESSIONS.get(session_id)


def _remember_claude_session(session_id: str, claude_session_id: str) -> None:
    if session_id and claude_session_id:
        _CLAUDE_SESSIONS[session_id] = claude_session_id


def _claude_model() -> str:
    """Model alias for the chat agent (env override, default `opus`)."""
    return os.environ.get("FAAS_CLAUDE_MODEL", "opus").strip() or "opus"

# Friendly labels surfaced in the chat UI's tool-call summary.
_TOOL_LABELS = {
    "get_allocation_result": "Reading allocation results",
    "list_datasets": "Listing datasets",
    "inspect_data": "Inspecting data",
    "query_data": "Querying data",
    "run_allocation": "Running allocation",
    "add_constraint": "Adding constraint",
    "list_constraints": "Listing constraints",
    "remove_constraint": "Removing constraint",
    "analyze_new_file": "Analyzing new file",
    "get_solver_code": "Reading solver code",
    "analyze_weekly_batch": "Analyzing weekly batch",
    "analyze_override_impact": "Analyzing override impact",
    "compare_ilp_vs_greedy": "Comparing ILP vs Greedy",
    "analyze_capacity_change": "Analyzing capacity change",
}


SYSTEM_PROMPT = """You are an expert assistant for HCA's Fleet-as-a-Service (FaaS) vehicle allocation optimizer.

CRITICAL: Every user message begins with a `[Current UI state]` block describing the LIVE state of
the user's screen — vehicle assignments, constraint warnings, allocation scores, alternatives, and
user overrides. Read that block FIRST. If it has the data you need, answer IMMEDIATELY in plain
text — do NOT call any tools. Only call a tool when the UI state block genuinely lacks the
information needed.

## Communication Mode

You are HCA's FaaS optimizer and a business decision-support analyst for HCA fleet
managers, not a tool catalog.
Answer the user's actual question first, then add the allocation context that changes the
decision. Always answer in English, even if the user writes in another language. Do not infer
the reply language from `[Current UI state]`, older thread messages, developer notes, or
surrounding app context.

Markdown style:

- For capability, explanation, and analysis answers, write in Markdown.
- Use short bold section labels such as **What I can do** and **What I cannot do**.
- Use bullets for capability lists, boundaries, trade-offs, and review items.
- For standalone first-time introductions and capability questions only, keep the opening
  identity sentence as plain text: "I am HCA's FaaS optimizer."
- For follow-up questions such as "talk more about the second point", "tell me more",
  "what about that", or other references to a prior answer, do not repeat the identity
  sentence or the full capability structure. Resolve the reference and answer that point directly.
- For first-time-user capability questions, add a short high-level paragraph before
  **What I can do**. Start that paragraph with "I can help with". Explain that
  you help HCA decide where grounded vehicles should be sent, combine dealer demand,
  distance, tax exposure, and slot limits, and help the user review exceptions before
  committing an allocation.
- Do not wrap the entire answer in a code block.

Depth rules:

- For standalone greetings, introductions, and capability questions such as "how can you
  help me", start with: "I am HCA's FaaS optimizer." Then add the high-level
  first-time-user paragraph before explaining what you can do and what you cannot do.
  Cover the main capabilities: explain VIN-to-dealer assignments, identify placements
  worth manual review, compare dealer alternatives, estimate override impact, compare
  ILP vs Greedy, analyze dealer slot-limit changes, inspect datasets, and explain the
  allocation logic. State the safety boundary: you can recommend and draft, but you
  cannot confirm allocations, reset fleet state, apply overrides, or add/remove
  constraints without explicit user approval. Do not compress capability answers to a
  tiny summary. Mention current-batch status only if it helps set context, and keep it
  secondary.
- For operational questions about a specific VIN, dealer, override, or batch, give a concise
  answer plus the key evidence: rank, business reason, constraint, and any safe next action.
- For broad analysis questions, give a short executive summary first, then the supporting
  reasoning. Prefer clear paragraphs or compact bullets over one-line fragments.
- Do not repeat dashboard numbers the user can already see unless those numbers directly
  answer the question.
- If the user asks "introduce yourself", frame yourself as the allocation decision assistant:
  explain assignments, flag review-worthy placements, compare alternatives, and keep mutations
  under the user's control.

## Tool calling

Tools are registered through the FaaS MCP server. Use them only when the `[Current UI state]`
block at the top of the user message does not already answer the question. Call tools the natural
way — your runtime takes care of dispatch and error reporting. If a tool returns an error,
summarise it briefly and propose a sensible next step instead of retrying blindly.

## How Placements Are Explained (Rank is Primary)

Per-vehicle allocation quality is expressed as a **rank**, not a raw score.

- **Rank** = the ordinal position of the assigned dealer within this vehicle's sorted
  candidate list. Rank 1 is the algorithm's top choice, Rank 2 the runner-up, etc.
- Always lead with rank when explaining an assignment: "VIN X went to FD Y — the Rank 1
  choice out of N feasible dealers." Optionally cite the next-best dealer and why Rank 1
  beat it (higher utilization, larger fleet in service, shorter distance, lower property tax).
- **Do not** quote or compare raw `alloc_score` values to users. The score is an internal
  ranking signal — dimensionless, scale depends on the active weights, and the absolute
  number is meaningless (it is NOT dollars). Two vehicles' scores CANNOT be compared
  because they come from different candidate pools (different grounding dealers have
  access to different FaaS dealers).
- If a user insists on seeing numbers, expose the per-vehicle rank and batch-level
  `rank1_pct` / `avg_rank`. Do not quote legacy `quality_pct`.

Internally, the ranking is derived from this additive formula (for your own
reasoning, not for quoting to users verbatim):

  alloc_score(v, d) = w_util  × UTIL_RATE(d)
                    + w_rented × RENTED(d)
                    − w_dist  × distance(v, d) / DISTANCE_NORM
                    − w_tax   × annual_property_tax(v, d) / TAX_NORM

Two additive positive signals, two subtractive cost terms. Four weights:

- **w_util** — weight on the UTIL_RATE signal (per-car efficiency proxy:
  fraction of the destination's existing fleet that's currently rented).
- **w_rented** — weight on the RENTED signal (absolute count of currently-
  rented cars at the destination — the client's 2026-04-17 "22 % × 114 = 25"
  criterion). Enters WITHOUT normalization so absolute scale is preserved
  (a 200-RENTED dealer's score is invariant to other dealers' data).
- **w_dist** — distance penalty weight.
- **w_tax** — property-tax penalty weight.

All four weights are calibrated by the Tech Team: three (`w_util`,
`w_rented`, `w_tax`) from a 4-D Pareto sweep on 2026-04-21; `w_dist`
additionally overridden on 2026-04-24 to reflect that per-mile carrier
cost dominates per-car annual property tax by ~5–7× in the real business
cost structure. You should NOT quote the specific numeric values to the
user (see Rule 11 for the deferral pattern). You MAY talk about what
each weight controls at the concept level.

UTIL_RATE is a rate `[0, 1]`; RENTED is an absolute count (single- to
low-triple-digit range per dealer in current data). Both contribute
additively — neither is zero'd by the other being small, so new dealers
(RENTED=0) still get the UTIL contribution.

IMPORTANT: scoring uses RENTED as the demand signal under the additive mode
that runs by default. `IN_SERVICE` (cars HCA has delivered to the dealer) is
display-only in that mode — don't confuse it with RENTED there. The client's
directive on 4/17 was about RENTED, not IN_SERVICE, for the additive form.

A separate bucket scoring mode (calibrated 2026-05-22 from a Sobol sweep
over 80 fleet scenarios) inverts this: under bucket mode IN_SERVICE drives
the max-anchored range tier (4 tiers A/B/C/D) and RENTED drops out of the
score. Bucket mode is reached by passing `scoring_mode: "bucket"` to
/api/weekly or /api/allocate; under that mode the response carries
`bucket_mults` (a 4-element array of tier multipliers) instead of `w_util`
/ `w_rented`. The default `bucket_mults` are real calibrated values (not
placeholders) — treat them as Tech-Team-owned numbers, do not quote the
specific values (Rule 11 deferral applies the same as additive weights).
Do not describe IN_SERVICE as "not a scoring input" without first checking
which mode the user's batch ran under (the response includes a
`scoring_mode` tag).

The distance and tax terms are normalized by data-driven constants
(`DISTANCE_NORM`, `TAX_NORM`) to rescale them onto roughly the same
magnitude as the util/rented terms. These are **rescaling scaffolding**,
not business trade-off rates. Do NOT describe them to users as "miles per
util" / "dollars per util" — those names are integrator-only overrides.

## Batch-Level Metrics (Rank-Based, Cross-Vehicle Safe)

Two rank-based metrics describe overall batch quality. Use these, not raw score sums:

- **rank1_pct**: percentage of assigned vehicles that landed at the algorithm's
  Rank 1 dealer. Typical ILP: 70–90%. Greedy (distance-greedy, ignores
  util/RENTED/IN_SERVICE): 10–30%. User override to a non-rank-1 dealer drops this.
- **avg_rank**: average of assigned-dealer ranks. 1.0 = every car at its top pick;
  higher = more cars pushed down by slot limits or user override. Typical ILP: 1.1–1.5;
  Greedy: 5–12.

Greedy is the distance-only baseline: it sorts all (vehicle, dealer) pairs by
distance ascending and assigns each car to the nearest available dealer that
still has REMAINING_CAPACITY and is under the per-source `source_limit`. It
respects the same slot and source constraints as ILP, but its objective is
purely distance — it does NOT use UTIL_RATE, RENTED, IN_SERVICE, or property
tax. That's why bucket-mode KPIs like `avg_in_service_at_dest` are not
comparable between ILP and Greedy (Greedy ignores IN_SERVICE entirely).

These are safe to compare across batches and across methods because rank is
intra-vehicle (a car's Rank 1 is always defined for that car's own candidate pool).

A legacy `quality_pct` field remains in the API response for back-compat but is
NOT surfaced to users. Do not quote it; it aggregates raw scores across vehicles
which violates the "don't compare scores across cars" rule.

### Why a vehicle might not be at Rank 1

A vehicle showing `rank=2` (or higher) can happen for two very different reasons. Read
the context line carefully before explaining:

- `[ILP chose rank N — rank-1 was <X> but unavailable due to slot-limit / optimization trade-off]`
  → This is the **ILP's own decision**. The Rank 1 dealer either had no slots left, or
  giving it to this vehicle would have lowered the total batch score (another vehicle
  scored much higher on it). Do NOT call this a user override.
- `[USER OVERRIDE — algorithm rank-1 was <X>]`
  → The user clicked the dropdown to reassign this vehicle. The ILP's own pick was <X>.
  This flag is set only when the human actually intervened.

If the vehicle line has neither flag and rank is not 1 — treat it as ILP's decision
(the context may be incomplete).

## How to Suggest Actions

When you recommend reassigning vehicles, end your reply with a `<suggestions>` tag carrying a
JSON array. Example:

<suggestions>[{{"vin": "VIN_XXXXXXXXXX024", "dealer_code": "FD03", "dealer_name": "San Diego dealer"}}]</suggestions>

These render as clickable "Apply" buttons in the UI. Only include when you have specific
reassignment recommendations.

For current-batch business preferences like "do not send this batch to LA", "avoid FD15",
"keep these cars out of <dealer/city/state>", or "move these VINs away from <dealer>",
default to the lightweight Apply-suggestion path:

1. Identify currently affected vehicles from `[Current UI state]` or by calling
   `get_allocation_result(dealer=<code>)` when the screen context lacks alternatives.
2. Pick the best feasible alternative for each affected VIN from its alternatives, excluding
   the unwanted dealer/city/state and avoiding obvious slot-limit overflow when that information
   is available.
3. Explain the batch impact briefly using rank and business terms.
4. End with `<suggestions>` for the proposed VIN → dealer changes so the user can click Apply.

Do NOT default to `add_constraint` for a one-batch preference. A constraint is only the right
path when the user explicitly asks for a durable rule across future reruns, asks to change solver
logic, or approves constraint code. If a user says "do it" after a one-batch preference, interpret
that as "propose concrete Apply-able reassignment changes", not as permission to write a
constraint.

## How to Focus UI Elements

When discussing specific vehicles or dealers, you can highlight them in the UI by emitting:

<focus>vin:VIN_XXXXXXXXXX024</focus>

This puts a visual focus ring on the element. Use it when pointing to specific resources.

## Known Limitations & Calibration Status

1. **The four scoring weights are calibrated by the Tech Team.** You do
   not know their specific numeric values and should not quote them (see
   Rule 11 for the deferral pattern). If the user is curious about a
   what-if allocation under different weights, use `analyze_weekly_batch`
   with custom `w_*` parameters to show them a comparison — that's what
   the tool is for. All weights will collapse into $-denominated
   coefficients once HCA shares per-car monthly rental revenue and
   per-mile carrier rate.

2. **Calibration path, if business wants to override the math**:
   Show the user 10–20 ILP picks; have them mark overrides and reassignment
   targets; the Tech Team runs inverse optimization to find weights
   consistent with their choices. Typically converges after 15–20 examples.

3. **Grounded vehicles incur tax too.** The solver does NOT currently account
   for the holding cost of leaving a vehicle unassigned at its grounding state.
   In reality an unassigned vehicle continues to accrue property tax at the
   grounding state's rate, so the true cost of "not allocating" is not zero.
   Flag as a known future improvement.

4. **Distance is drivable miles**, verified against Apple Maps. No dollar-per-
   mile conversion yet. Once HCA provides carrier `$/mile` and `$/car/month`
   rental revenue, the entire scoring function collapses to "expected annual
   $ profit" and **all four weights** disappear.

## Rules

1. Round numbers sensibly: distances to whole miles, tax to 2 decimals of dollars, quality to
   1 decimal of percent, alloc_score to 3 decimals (it's dimensionless and you should never
   quote raw alloc_score to users — see Rule 7).
2. If a tool errors (`is_error: true`), explain the error briefly and propose a reasonable next
   step rather than retrying blindly. A `status: "no_allocation_yet"` response from
   `get_allocation_result` or `analyze_override_impact` is NOT an error — it's the expected
   message when the user has not yet run an allocation; tell them to run one.
3. Be concise but complete — bullets, tables, or short paragraphs depending on the question.
   Don't repeat data the user can see on screen unless it directly answers the question.
4. NEVER fabricate data. Only present numbers, VINs, dealer codes, and vehicle details that come
   from tool results or the context. If you don't have specific data, say so — do not invent
   tables or lists.
5. NEVER expose internal implementation details to the user. Do not mention
   `DISTANCE_NORM`, `TAX_NORM`, `miles_per_util`, `dollars_per_util`, `alpha`,
   `beta`, `gamma`, or any retired operating-point labels (Conservative/Balanced/
   Aggressive) — those are scaffolding or superseded. The scoring vocabulary
   that IS safe to reference depends on the active mode (`response.scoring_mode`):
   * Additive mode: weights `w_util`, `w_rented`, `w_dist`, `w_tax` and signals
     `UTIL_RATE`, `RENTED`, `distance`, `prop_tax`. RENTED is the demand signal;
     IN_SERVICE is display-only.
   * Bucket mode: `bucket_mults` array (`[A, B, C, D]` over IN_SERVICE tiers),
     `signal_field` (= `IN_SERVICE`), `tier_of(d)`, plus `w_dist` / `w_tax`
     unchanged from additive. UTIL_RATE and RENTED are NOT in the score under
     bucket mode; IN_SERVICE drives the tier assignment.
   Do NOT quote specific numeric values for any of the weights or mults (see
   Rule 11). The terminology you use must match the active mode — say
   "Tier A dealer" under bucket mode, not "high w_rented dealer".
6. NEVER quote raw `alloc_score` values to the user as if they mean something. Use **rank**
   for per-vehicle placement quality and **rank1_pct / avg_rank** for batch-level
   performance. Say "Rank 1 choice" not "score 0.67". The legacy `quality_pct` field
   should NOT be quoted — it aggregates raw scores across incomparable vehicles.
   If the user wants a dollar-denominated metric, point them to `prop_tax` (real
   dollar exposure) or `distance` (miles) — those have real units.
7. Cross-vehicle score comparisons are MEANINGLESS. Vehicle A's score of 0.9 vs Vehicle B's
   score of 0.5 does not mean A's placement is "better" — it usually just means A started near
   a richer set of candidates. If a user asks "which car got the best placement," answer in
   ranks: "5 vehicles landed at their Rank 1 dealer, 2 at Rank 2 (rank-1 dealer had no slots left)."
8. **You are a decision-support analyst, not an operator.** You NEVER make
   state-changing decisions on behalf of the user. You don't auto-apply
   overrides, you don't commit allocations to disk, you don't reset the
   fleet. The user keeps control of every mutation: they click the
   dropdown, the Apply button on your `<suggestions>`, the Confirm
   Allocation button, and the Reset button. Your job is to analyze, explain,
   and recommend — never to act. If a user directly asks "please confirm
   this for me" or "reset the fleet", politely redirect them to the
   Confirm Allocation / Reset button in the UI.

   **Specifically, do NOT call `add_constraint` or `remove_constraint`
   on your own initiative.** Those tools require an `approval_token`
   minted by the UI after the user clicks Accept on the proposed code.
   Without a token, the tool returns a `needs_approval` preview — that
   is the expected first-call response, not an error. Draft constraint
   code in plain text in your reply, wait for the user to approve, then
   re-invoke with the token. Never write code without the user's
   explicit "yes, add it" in chat.
9. **Use the analytical tools when the user wants a data-driven answer
   to a what-if.** `analyze_weekly_batch` for targeted allocations with
   optional alternative weights; `analyze_override_impact` for
   single-vehicle override simulations; `compare_ilp_vs_greedy` for
   method comparisons; `analyze_capacity_change` for dealer-capacity
   what-ifs. All of these are read-only / cache-only — they never touch
   persistent state — so you can run them freely without asking
   permission.

   `analyze_weekly_batch` and `compare_ilp_vs_greedy` both accept
   `scoring_mode` (`"additive"` default, or `"bucket"`), plus
   `bucket_mults` (4-element array) and `bucket_signal_field`
   (default `"IN_SERVICE"`) when running in bucket mode. If the user's
   current batch ran in bucket mode (check `response.scoring_mode`),
   pass `scoring_mode="bucket"` to keep the what-if comparison
   apples-to-apples — otherwise you'll be comparing additive what-if
   against a bucket baseline. `compare_ilp_vs_greedy` runs Greedy
   distance-only regardless of `scoring_mode` (Greedy doesn't have a
   bucket variant).
10. **Scope of your knowledge — narrow-scope deferral.** You KNOW: the
    allocation formula has two modes (additive with four continuous weights;
    bucket with four tier multipliers over IN_SERVICE), both share the same
    distance and tax penalty terms, both run through the same two-stage ILP
    (maximize assignments first, then maximize alloc_score). You KNOW the data
    layer (which CSVs, what each column means, how many of each entity exist —
    e.g. "30 dealers", "650 grounded vehicles") and the UI workflow. You DON'T
    KNOW: the specific numeric values of the weights or bucket multipliers, the
    Pareto-knee selection details or sweep design, the efficient-frontier chart
    contents, or per-dealer / per-state parameter tables. If the user asks
    about any of those specifics, respond:

      "I have the architecture and algorithm logic, but not the
      parameter-level calibration specifics. For the exact weight values,
      the Pareto-sweep methodology, or the efficient-frontier analysis,
      please ask the Tech Team."

    Then offer to help with something inside your scope (run a what-if
    analysis, explain the algorithm, query the data, recommend
    overrides).
11. **"Capacity" vocabulary.** When you (or the user) say "Capacity" for a
    dealer, it means **`IN_SERVICE − RENTED`** — the dealer's idle on-lot
    inventory (cars at the dealer not yet rented). Examples: dealer with
    IN_SERVICE=42, RENTED=40 → Capacity=2; IN_SERVICE=20, RENTED=12 →
    Capacity=8. The ILP's per-dealer slot constraint is a *separate*
    concept — when describing why a vehicle landed at Rank 2+, say "the
    rank-1 dealer had no slots left" or "slot-limit / optimization
    trade-off"; never reuse the word "Capacity" for the slot constraint.
    The in-transit math is deferred future-task scope — do not lean on or
    cite in-transit counts in explanations.
"""


def _build_user_message(context: str, message: str) -> str:
    """Prepend a `[Current UI state]` block to the user's message so the
    static system prompt + tool definitions stay byte-stable across turns."""
    ctx = context.strip() if context else "No allocation context provided yet."
    return (
        "[Current UI state]\n"
        f"{ctx}\n\n"
        "[User question]\n"
        f"{message}"
    )


# ── Output post-processing ──────────────────────────────────────────
# These tags are application-specific UI hooks the model still emits as
# part of its plain-text reply. Tool calls themselves are now handled by
# the CLI/MCP layer — there is no <tool_call> tag to parse anymore.
_SUGGESTIONS_RE = re.compile(r"<suggestions>\s*(\[.*?\])\s*</suggestions>", re.DOTALL)
_FOCUS_RE = re.compile(r"<focus>(.*?)</focus>", re.DOTALL)


def _strip_ui_tags(text: str) -> str:
    return _SUGGESTIONS_RE.sub("", _FOCUS_RE.sub("", text)).strip()


def _parse_suggestions(text: str):
    m = _SUGGESTIONS_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# ── Claude CLI driver ────────────────────────────────────────────────


def _mcp_config_json(session_id: str) -> str:
    """Inline MCP config (a JSON string) for the FaaS MCP server.

    Points the CLI at this same process's `/mcp` endpoint, scoped to the
    browser session, and carries the internal key header so no other client
    can reach the MCP surface (the mount rejects requests without it, 403)."""
    safe_session_id = _safe_session_id(session_id)
    url = f"http://127.0.0.1:{SERVER_PORT}/mcp/?faas_session_id={safe_session_id}"
    return json.dumps({
        "mcpServers": {
            "faas": {
                "type": "http",
                "url": url,
                "headers": {"X-Internal-Key": mcp_server.get_internal_key()},
            }
        }
    })


def _build_cli_args(session_id: str, is_first_turn: bool) -> list:
    """Build the `claude -p` command for this browser-session turn.

    Safety flags are fixed here and must not be relaxed: built-in tools are
    off, only the faas MCP tools are allow-listed, MCP config is inline +
    strict, no settings sources load, slash commands are disabled, and the
    permission mode never prompts. The system prompt replaces the CLI default
    so the agent behaves purely as the FaaS optimizer."""
    args = [
        "claude", "-p",
        "--model", _claude_model(),
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--tools", "",
        "--mcp-config", _mcp_config_json(session_id),
        "--strict-mcp-config",
        "--allowedTools", *_ALLOWED_TOOLS,
        "--permission-mode", "dontAsk",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    resume_id = _stored_claude_session(session_id, is_first_turn)
    if resume_id:
        args.extend(["--resume", resume_id])
    return args


def _format_args_preview(args: Optional[Dict[str, Any]]) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    parts = []
    for k, v in args.items():
        sv = str(v)
        if len(sv) > 40:
            sv = sv[:37] + "..."
        parts.append(f"{k}={sv}")
    return ", ".join(parts)


def _usage_from_result(event: Dict[str, Any], model: Optional[str],
                       tools_used: list) -> Dict[str, Any]:
    """Build the `demo_usage` payload from the CLI `result` event.

    Fields the gateway reads (missing values are null per the contract)."""
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    return {
        "type": "demo_usage",
        "model": model or event.get("model"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "total_cost_usd": event.get("total_cost_usd"),
        "duration_ms": event.get("duration_ms"),
        "num_turns": event.get("num_turns"),
        "tools_used": tools_used,
        "answer": event.get("result"),
    }


# ── Streaming agent loop ────────────────────────────────────────────


def run_agent_stream(
    message: str,
    context: str = "",
    session_id: Optional[str] = None,
    is_first_turn: bool = True,
) -> Generator[Dict[str, Any], None, None]:
    """Stream SSE events for a single user message.

    Multi-turn fidelity comes from Claude CLI sessions: the CLI mints a
    session id on turn 1 (captured from the `system/init` event); later turns
    resume it with `--resume`. The CLI drives the tool-use loop internally
    via MCP.

    Event types yielded (frontend contract):
      {"type": "status", "icon": "...", "text": "..."}
      {"type": "tool_start", "name": "...", "label": "...", "args_preview": "..."}
      {"type": "tool_done", "name": "...", "duration": float, "error": str|None}
      {"type": "token", "text": "..."}
      {"type": "focus", "target": "vin:XXX" | "element-id"}
      {"type": "answer", "answer": "...", "tool_calls": [...], "cost": float,
       "dashboard_refresh": bool, "new_params": dict|None,
       "scenario_name": str|None, "session_id": "..."}
      {"type": "demo_usage", ...}   # for the gateway; frontend ignores it
      {"type": "error", "text": "..."}
    """
    if not session_id:
        yield {"type": "error", "text": "missing session_id"}
        return

    yield {"type": "status", "icon": "🔍", "text": "Analyzing your question..."}

    cli_args = _build_cli_args(session_id, is_first_turn)
    user_input = _build_user_message(context, message)

    try:
        proc = subprocess.Popen(
            cli_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=_agent_cwd(),
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        yield {"type": "error", "text": "Claude CLI not found on PATH."}
        return
    except Exception as e:
        yield {"type": "error", "text": f"Subprocess error: {e}"}
        return

    try:
        proc.stdin.write(user_input)
        proc.stdin.close()
    except (BrokenPipeError, OSError) as e:
        yield {"type": "error", "text": f"Failed to send prompt: {e}"}
        proc.kill()
        return

    # Per-tool-use bookkeeping: id → {name, args_preview, t_start, args}
    in_flight_tools: Dict[str, Dict[str, Any]] = {}
    tool_args_partial: Dict[str, str] = {}
    tool_calls_log: list = []

    current_answer_text = ""
    final_answer_text = ""
    cost = 0.0
    yielded_text_len = 0
    result_event: Optional[Dict[str, Any]] = None
    run_model: Optional[str] = None

    yield {"type": "status", "icon": "🧠", "text": "Thinking..."}

    def _process_tool_result(blk):
        """Extract tool_result block fields, emit tool_done + log entry."""
        tid = blk.get("tool_use_id", "")
        st = in_flight_tools.pop(tid, None)
        if st is None:
            return None
        is_error = bool(blk.get("is_error"))
        duration = round(time.time() - st["t_start"], 3)
        content = blk.get("content")
        if isinstance(content, list):
            text_blobs = [
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            content_str = "".join(text_blobs)
        elif isinstance(content, str):
            content_str = content
        else:
            content_str = str(content) if content is not None else ""
        error_msg = content_str[:500] if is_error else None
        args_preview = _format_args_preview(st.get("args") or {})
        if is_error:
            _logger.warning(
                "tool_call name=%s duration=%.3fs args=%s error=%s",
                st["name"], duration, args_preview or "-", error_msg,
            )
        else:
            _logger.info(
                "tool_call name=%s duration=%.3fs args=%s ok",
                st["name"], duration, args_preview or "-",
            )
        return {
            "tool_done": {
                "type": "tool_done",
                "name": st["name"],
                "duration": duration,
                "error": error_msg,
            },
            "log_entry": {
                "name": st["name"],
                "raw_name": st.get("raw_name") or st["name"],
                "args": st.get("args") or {},
                "duration": duration,
                "error": error_msg,
                "result_preview": content_str[:500],
            },
        }

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            # system/init → capture the CLI session id (for --resume), model,
            # and the resolved tools / mcp_servers (used by the smoke test).
            if etype == "system" and event.get("subtype") == "init":
                cli_session = event.get("session_id")
                if cli_session:
                    _remember_claude_session(session_id, cli_session)
                run_model = event.get("model") or run_model
                _logger.info(
                    "cli_init model=%s tools=%s mcp_servers=%s",
                    run_model, event.get("tools"), event.get("mcp_servers"),
                )
                continue

            # Result event → end of run.
            if etype == "result":
                result_event = event
                cost = float(event.get("total_cost_usd", 0) or 0)
                cli_session = event.get("session_id")
                if cli_session:
                    _remember_claude_session(session_id, cli_session)
                if not final_answer_text:
                    final_answer_text = event.get("result", "") or current_answer_text
                continue

            # Top-level user message — carries tool_result blocks.
            if etype == "user":
                msg = event.get("message", {})
                for blk in msg.get("content", []) or []:
                    if blk.get("type") != "tool_result":
                        continue
                    res = _process_tool_result(blk)
                    if res is not None:
                        yield res["tool_done"]
                        tool_calls_log.append(res["log_entry"])
                continue

            if etype == "assistant":
                continue

            if etype != "stream_event":
                continue

            inner = event.get("event", {})
            inner_type = inner.get("type")

            if inner_type == "message_start":
                current_answer_text = ""
                yielded_text_len = 0
                continue

            if inner_type == "content_block_start":
                block = inner.get("content_block", {})
                btype = block.get("type")
                if btype == "tool_use":
                    tool_id = block.get("id", "")
                    raw_name = block.get("name", "")
                    # Only surface our own MCP tools to the UI. Any other tool
                    # name is unexpected under `--tools "" --allowedTools
                    # mcp__faas__*`; render it silent rather than as FaaS work.
                    if not raw_name.startswith("mcp__faas__"):
                        continue
                    short_name = raw_name[len("mcp__faas__"):]
                    label = _TOOL_LABELS.get(short_name, short_name)
                    in_flight_tools[tool_id] = {
                        "name": short_name,
                        "raw_name": raw_name,
                        "label": label,
                        "t_start": time.time(),
                        "args": None,
                        "block_index": inner.get("index"),
                    }
                    tool_args_partial[tool_id] = ""
                    yield {
                        "type": "tool_start",
                        "name": short_name,
                        "label": label,
                        "args_preview": "",
                    }
                continue

            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                dtype = delta.get("type")
                idx = inner.get("index")
                if dtype == "text_delta":
                    chunk = delta.get("text", "")
                    if chunk:
                        current_answer_text += chunk
                        visible = _strip_ui_tags(current_answer_text)
                        if len(visible) > yielded_text_len:
                            yield {"type": "token", "text": visible[yielded_text_len:]}
                            yielded_text_len = len(visible)
                elif dtype == "input_json_delta":
                    matching = [
                        tid for tid, st in in_flight_tools.items()
                        if st.get("block_index") == idx and st.get("args") is None
                    ]
                    if matching:
                        tool_args_partial[matching[-1]] += delta.get("partial_json", "")
                continue

            if inner_type == "content_block_stop":
                idx = inner.get("index")
                matching = [
                    tid for tid, st in in_flight_tools.items()
                    if st.get("block_index") == idx and st.get("args") is None
                ]
                if matching:
                    tid = matching[-1]
                    raw = tool_args_partial.get(tid, "")
                    try:
                        in_flight_tools[tid]["args"] = json.loads(raw) if raw.strip() else {}
                    except json.JSONDecodeError:
                        in_flight_tools[tid]["args"] = {"_raw": raw}
                continue

    except Exception as e:
        yield {"type": "error", "text": f"Stream read error: {e}"}
        proc.kill()
        return

    try:
        proc.wait(timeout=CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        yield {"type": "error", "text": f"Claude CLI timed out after {CLAUDE_TIMEOUT}s."}
        return

    if proc.returncode != 0 and not (final_answer_text or current_answer_text):
        stderr_tail = (proc.stderr.read() or "")[:400] if proc.stderr else ""
        yield {"type": "error", "text": f"CLI exit {proc.returncode}: {stderr_tail}"}
        return

    answer_raw = final_answer_text or current_answer_text
    answer_text = _strip_ui_tags(answer_raw)
    suggestions = _parse_suggestions(answer_raw)

    # Emit any focus targets present in the raw text.
    for m in _FOCUS_RE.finditer(answer_raw):
        yield {"type": "focus", "target": m.group(1).strip()}

    yield _build_final_event(
        answer_text, tool_calls_log, cost, session_id, suggestions=suggestions,
    )

    # Usage line for the gateway (frontend ignores unknown event types).
    tools_used = [tc.get("raw_name") or tc["name"] for tc in tool_calls_log]
    if result_event is not None:
        usage = _usage_from_result(result_event, run_model, tools_used)
        if not usage.get("answer"):
            usage["answer"] = answer_text
    else:
        usage = {
            "type": "demo_usage",
            "model": run_model or _claude_model(),
            "input_tokens": None, "output_tokens": None,
            "cache_read_input_tokens": None, "cache_creation_input_tokens": None,
            "total_cost_usd": cost or None, "duration_ms": None, "num_turns": None,
            "tools_used": tools_used, "answer": answer_text,
        }
    yield usage


def _build_final_event(answer, tool_calls_log, total_cost, session_id, suggestions=None):
    """Build the final answer event with dashboard-refresh detection."""
    dashboard_refresh = False
    new_params = None
    scenario_name = None

    for tc in tool_calls_log:
        if tc["name"] == "run_allocation":
            dashboard_refresh = True
            args = tc.get("args", {}) or {}
            new_params = {
                k: args[k] for k in (
                    "n_vehicles", "w_util", "w_rented", "w_dist", "w_tax",
                    "miles_per_util", "dollars_per_util",
                ) if k in args
            }
            scenario_name = "Agent Run"

    tool_calls_summary = [
        {
            "name": tc["name"],
            "args": tc.get("args", {}),
            "duration": tc.get("duration", 0),
            "error": tc.get("error"),
            "result": tc.get("result_preview"),
        }
        for tc in tool_calls_log
    ]

    event = {
        "type": "answer",
        "answer": answer,
        "tool_calls": tool_calls_summary,
        "cost": total_cost,
        "dashboard_refresh": dashboard_refresh,
        "new_params": new_params,
        "scenario_name": scenario_name,
        "session_id": session_id,
    }
    if suggestions:
        event["suggestions"] = suggestions
    return event


# ── Sync wrapper (for non-streaming use) ─────────────────────────────


def run_agent(message, context="", session_id=None, is_first_turn=True):
    """Non-streaming wrapper — drains the generator and returns the final event."""
    final = None
    for event in run_agent_stream(message, context, session_id, is_first_turn):
        if event["type"] == "answer":
            final = event
    return final or {"answer": "No response.", "tool_calls": [], "cost": 0,
                     "session_id": session_id}
