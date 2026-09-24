"""
FaaS Vehicle Allocation — FastAPI Backend (multi-session)
Run:  python app/server.py    (or: uvicorn app.server:app --reload)

Per-user state lives in `SessionState` keyed by the `X-Session-Id`
request header. Every browser tab generates its own UUID, sends it on
every API call, and gets an isolated fleet snapshot + allocation cache.
A TTL reaper evicts idle sessions every 5 minutes.
"""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

PORT = int(os.environ.get("PORT", "8000"))

# Public demo hardening. DEMO_MODE gates docs/diagnostic exposure and the
# annotation banner; quota is enforced by the gateway (this app never counts).
DEMO_MODE = os.environ.get("DEMO_MODE", "") == "1"

# Hard upper bound on batch / fleet-level requests: the fleet is 650 vehicles,
# so no legitimate request asks for more. Caps user-supplied counts so a demo
# visitor can't force an oversized solve. Weights/params get generous but
# finite bounds so pathological values can't be injected.
FLEET_SIZE = 650
MAX_WEIGHT = 1000.0
MAX_EQUIV_RATE = 1_000_000.0
MAX_STAY_MONTHS = 120

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

# Chat rate limit per browser session. Set CHAT_LIMIT_PER_SESSION=0 to disable
# the hosted demo agent while keeping the rest of the dashboard available.
CHAT_LIMIT_PER_SESSION = int(os.environ.get("CHAT_LIMIT_PER_SESSION", "3"))

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import (
    DEFAULT_W_UTIL,
    DEFAULT_W_RENTED,
    DEFAULT_W_DIST,
    DEFAULT_W_TAX,
    DEFAULT_EXPECTED_STAY_MONTHS,
    load_baseline,
    load_baseline_fleet,
    get_overview,
    solve_both,
    solve_weekly_batch,
    solve_weekly_greedy,
    get_fleet_inventory,
    confirm_allocation,
    reset_fleet,
)
from agent import cleanup_stale_mcp_configs, run_agent_stream
from bucket_pipeline import solve_bucket, solve_weekly_bucket
import state
from state import SessionState, SessionStore
import mcp_server
import watchlist


# Materialize the MCP ASGI app first so its session_manager is created;
# its lifespan is wired into FastAPI's so the task group starts/stops
# alongside the FastAPI app.
_mcp_app = mcp_server.get_asgi_app()


_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-"
    r"[0-9a-fA-F]{12}$"
)


# ── Session reaper background task ───────────────────────────────────


async def _reaper_loop(store: SessionStore, interval: int = 300):
    """Evict idle sessions every `interval` seconds. Idempotent — running
    a fresh server uses an empty store, so the first iteration finds
    nothing and the loop just sleeps."""
    try:
        while True:
            await asyncio.sleep(interval)
            evicted = store.reap()
            if evicted:
                print("[reaper] evicted {} idle session(s)".format(evicted))
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Load baseline ONCE at startup (immutable for the rest of the process).
    baseline = load_baseline()
    baseline_fleet = load_baseline_fleet()
    store = SessionStore(baseline_fleet_df=baseline_fleet)
    app.state.store = store
    # Hand the store to the MCP layer so MCP tools can resolve their session.
    mcp_server.set_store(store)
    # One-shot janitor: wipe stale per-session .mcp.json files left in /tmp
    # by prior runs. The CLI keeps these alive only for the duration of one
    # subprocess invocation; anything older than an hour is orphaned.
    cleanup_stale_mcp_configs()
    # Hand the watchlist module a no-arg overview/fleet view (baseline).
    # Watchlist is fleet-wide / not per-session, so it always reads baseline.
    reaper = asyncio.create_task(_reaper_loop(store))
    try:
        async with mcp_server.mcp.session_manager.run():
            yield
    finally:
        reaper.cancel()


app = FastAPI(
    title="FaaS Allocator",
    description=(
        "HCA FaaS vehicle allocation API. Most REST endpoints are scoped to "
        "one browser tab or API client by the X-Session-Id header."
    ),
    version="0.1.0",
    lifespan=_lifespan,
    # In the public demo, hide the interactive API docs and the OpenAPI
    # schema (and every internal/diagnostic surface) from the outside world.
    docs_url=None if DEMO_MODE else "/docs",
    redoc_url=None if DEMO_MODE else "/redoc",
    openapi_url=None if DEMO_MODE else "/openapi.json",
)

# Mount the MCP streamable-http transport. The MCP layer handles its
# own session resolution via URL path (see mcp_server.py).
app.mount("/mcp", _mcp_app)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Attach minimal security headers for browser-facing pages and JSON APIs."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "fullscreen=(), geolocation=(), microphone=(), camera=()")
    return response


# ── Session middleware + dependency ──────────────────────────────────


@app.middleware("http")
async def session_mw(request: Request, call_next):
    """Resolve `X-Session-Id` header → SessionState on `request.state.session`.

    Skips `/mcp/*` (MCP path-routing handles its own session resolution)
    and static / index routes (no state needed). Endpoints that require
    a session use the `session_dep` dependency, which raises 400 if the
    middleware did not attach one.
    """
    path = request.url.path
    if path.startswith("/mcp/") or path.startswith("/static/") or path == "/":
        return await call_next(request)
    cid = request.headers.get("X-Session-Id")
    if cid:
        if not _SESSION_ID_RE.fullmatch(cid):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "Invalid X-Session-Id format. Send a stable UUID-like string."
                },
            )
        store: SessionStore = request.app.state.store
        request.state.session = store.get_or_create(cid)
    return await call_next(request)


def session_dep(
    request: Request,
    x_session_id: Optional[str] = Header(
        default=None,
        alias="X-Session-Id",
        description=(
            "Stable UUID generated by the browser tab or API client. "
            "Required for all session-scoped endpoints."
        ),
    ),
) -> SessionState:
    sess = getattr(request.state, "session", None)
    if sess is None:
        raise HTTPException(
            status_code=400,
            detail="Missing X-Session-Id header — generate a UUID per browser tab and send it on every request.",
        )
    return sess


# ── OpenAPI models ───────────────────────────────────────────────────


class FlexibleModel(BaseModel):
    """Schema helper for payloads whose solver-specific fields may evolve."""

    model_config = ConfigDict(extra="allow")


class ErrorResponse(BaseModel):
    detail: Any


class DefaultsResponse(BaseModel):
    w_util: float
    w_rented: float
    w_dist: float
    w_tax: float
    expected_stay_months: int
    # Frontend toggles the demo annotation banner + chart watermarks on this.
    demo_mode: bool = False


class SessionSummary(BaseModel):
    client_id: str
    last_touched: float
    has_weekly: bool
    has_allocate: bool
    n_constraints: int


class SessionsResponse(BaseModel):
    sessions: List[SessionSummary]


class DealerOverview(FlexibleModel):
    DEALER_CODE: str
    DEALER_NAME: str
    STATE: str
    TRUE_CAPACITY: int
    DELIVERED_COUNT: int
    IN_TRANSIT_COUNT: int
    REMAINING_CAPACITY: int
    UTIL_RATE: float
    IN_SERVICE: float
    RENTED: float
    PROP_TAX_RATE: float
    LATITUDE: float
    LONGITUDE: float


class SourceOverview(FlexibleModel):
    SOURCE: str
    CITY: str
    STATE: str
    SOURCE_LAT: float
    SOURCE_LON: float


class OverviewResponse(BaseModel):
    total_vehicles: int
    total_dealers: int
    total_capacity: int
    dealers: List[DealerOverview]
    sources: List[SourceOverview]


class FleetVehicle(FlexibleModel):
    vin: str
    year: int
    make: str
    model: str
    source: str
    source_city: str
    source_state: str
    residual: float
    status: Literal["Incoming", "Grounded", "Transporting", "Delivered"]
    week: str
    assigned_dealer: Optional[str] = None
    assigned_dealer_name: Optional[str] = None
    assigned_state: Optional[str] = None
    distance: Optional[float] = None


class FleetCounts(BaseModel):
    total: int
    incoming: int
    grounded: int
    transporting: int
    delivered: int


class FleetResponse(BaseModel):
    vehicles: List[FleetVehicle]
    weeks: List[str]
    counts: FleetCounts


class AllocationResultResponse(FlexibleModel):
    params: Optional[Dict[str, Any]] = None
    vehicles: Optional[List[Dict[str, Any]]] = None
    scoring_mode: Optional[Literal["additive", "bucket"]] = None


class ConfirmResponse(BaseModel):
    updated: int
    skipped: int
    total: int


class ResetResponse(BaseModel):
    status: str


class TrendResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    placeholder: bool = Field(alias="_placeholder")
    note: str = Field(alias="_note")
    placement_rate: List[int]
    rank1_pct: List[int]
    annual_tax_saved: List[int]
    hhi: List[int]


class PipelineWeek(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    week: str
    grounded: int
    transporting: int
    delivered: int
    synthetic: bool = Field(alias="_synthetic")


class FleetStateResponse(BaseModel):
    grounded: int
    transporting: int
    delivered: int
    incoming: int
    total: int
    pipeline_by_week: List[PipelineWeek]


class HeatmapCell(FlexibleModel):
    code: str
    full_name: str
    dealer_code: str
    util: float
    rented: int
    in_service: int
    band: str


class TopMove(FlexibleModel):
    tag: str
    tag_label: str
    title: str
    body: str
    metrics: List[Dict[str, str]]


class WatchlistMeta(BaseModel):
    refreshed_at: Optional[str] = None
    count: int
    max_items: Optional[int] = None


class HomeResponse(BaseModel):
    trends: TrendResponse
    fleet_state: FleetStateResponse
    dealer_heatmap: List[HeatmapCell]
    top_moves: List[TopMove]
    watchlist: List[Dict[str, Any]]
    watchlist_meta: WatchlistMeta


class AllocationCacheSummary(BaseModel):
    batch_size: Optional[int] = None
    n_assigned: Optional[int] = None
    total_alloc_score: Optional[float] = None
    total_transport: Optional[float] = None
    params: Optional[Dict[str, Any]] = None


class AllocationCacheResponse(BaseModel):
    summary: Optional[AllocationCacheSummary] = None
    vehicles: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None


class ApprovalTokenResponse(BaseModel):
    approval_token: str


class ChatStatusResponse(BaseModel):
    limit: int
    remaining: int
    disabled: bool


class ChatQuotaError(BaseModel):
    error: Literal["chat_quota_exhausted"]
    message: str
    limit: int
    remaining: int


class ConfirmedExportVehicle(FlexibleModel):
    vin: str
    status: Literal["Transporting"]
    assigned_dealer: str
    assigned_dealer_name: Optional[str] = None
    assigned_state: Optional[str] = None
    source: str
    source_city: str
    source_state: str
    residual: float
    week: str
    distance: Optional[float] = None


class ConfirmedExportResponse(BaseModel):
    status: Literal["stub"]
    integration_status: Literal["not_connected"]
    format: Literal["json"]
    count: int
    vehicles: List[ConfirmedExportVehicle]
    note: str


# ── Static + diagnostics ─────────────────────────────────────────────


@app.get("/")
def index():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get(
    "/api/defaults",
    response_model=DefaultsResponse,
    summary="Get canonical scoring defaults",
)
def defaults():
    """Canonical scoring defaults. Single source of truth is engine.DEFAULT_*;
    frontend fetches this at boot to avoid hardcoding 1.5 / 0.031 in app.js."""
    return {
        "w_util": DEFAULT_W_UTIL,
        "w_rented": DEFAULT_W_RENTED,
        "w_dist": DEFAULT_W_DIST,
        "w_tax": DEFAULT_W_TAX,
        "expected_stay_months": DEFAULT_EXPECTED_STAY_MONTHS,
        "demo_mode": DEMO_MODE,
    }


@app.get(
    "/api/sessions",
    response_model=SessionsResponse,
    summary="List active in-memory sessions",
    description="Diagnostic endpoint for the current session snapshot only.",
)
def sessions_diag(session: SessionState = Depends(session_dep)):
    """Diagnostic: dump current session only to avoid cross-session identifier leakage."""
    if DEMO_MODE:
        # Diagnostic endpoint is not exposed in the public demo.
        raise HTTPException(status_code=404, detail="Not found.")
    return {"sessions": [
        {
            "client_id": session.client_id,
            "last_touched": session.last_touched,
            "has_weekly": session.last_weekly is not None,
            "has_allocate": session.last_allocate is not None,
            "n_constraints": len(session.constraints),
        },
    ]}


def _resolve_weights(w_dist, w_tax, miles_per_util, dollars_per_util):
    """Accept either the business-friendly equivalence rates (miles_per_util,
    dollars_per_util) or the internal weights (w_dist, w_tax). If both are
    provided, the business-friendly names win. Converts equivalence rates to
    internal weights using the engine's data-driven normalization constants."""
    if miles_per_util is None and dollars_per_util is None:
        return w_dist, w_tax
    data = load_baseline()
    if miles_per_util is not None:
        w_dist = data["distance_norm"] / float(miles_per_util)
    if dollars_per_util is not None:
        w_tax = data["tax_norm"] / float(dollars_per_util)
    return w_dist, w_tax


# ── Per-session read endpoints ───────────────────────────────────────


@app.get(
    "/api/overview",
    response_model=OverviewResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Get dealer network overview",
    description="Session-scoped fleet and dealer overview. Requires X-Session-Id.",
)
def overview(session: SessionState = Depends(session_dep)):
    return get_overview(session=session)


@app.get(
    "/api/fleet",
    response_model=FleetResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Get fleet inventory",
    description=(
        "Session-scoped fleet inventory. After /api/confirm, confirmed vehicles "
        "appear here as status=Transporting with assigned dealer fields."
    ),
)
def fleet(session: SessionState = Depends(session_dep)):
    return get_fleet_inventory(session=session)


# ── Allocation endpoints ─────────────────────────────────────────────


class AllocateRequest(BaseModel):
    n_vehicles: Optional[int] = Field(default=None, gt=0, le=FLEET_SIZE)
    w_util: float = Field(default=DEFAULT_W_UTIL, ge=0, le=MAX_WEIGHT)
    w_rented: float = Field(default=DEFAULT_W_RENTED, ge=0, le=MAX_WEIGHT)
    # Business-friendly equivalence rates (preferred). If provided, override w_dist/w_tax.
    miles_per_util: Optional[float] = Field(default=None, gt=0, le=MAX_EQUIV_RATE)
    dollars_per_util: Optional[float] = Field(default=None, gt=0, le=MAX_EQUIV_RATE)
    # Internal weights (fallback / legacy). Ignored if the above two are set.
    w_dist: float = Field(default=DEFAULT_W_DIST, ge=0, le=MAX_WEIGHT)
    w_tax: float = Field(default=DEFAULT_W_TAX, ge=0, le=MAX_WEIGHT)
    source_limit: Optional[int] = Field(default=None, gt=0, le=FLEET_SIZE)
    expected_stay_months: int = Field(default=DEFAULT_EXPECTED_STAY_MONTHS, gt=0, le=MAX_STAY_MONTHS)
    # Bucket-form opt-in. When "bucket", w_util/w_rented are ignored and
    # the response carries the bucket-pipeline native shape (no greedy
    # comparison block). See bucket_pipeline.solve_bucket.
    scoring_mode: Literal["additive", "bucket"] = "additive"
    bucket_mults: Optional[List[float]] = None
    bucket_signal_field: str = "IN_SERVICE"


@app.post(
    "/api/allocate",
    response_model=AllocationResultResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Run full-fleet allocation",
    description=(
        "Runs the allocation solver for the current session and caches the result. "
        "Use scoring_mode=additive for the production additive formula or "
        "scoring_mode=bucket for the tier-based bucket path."
    ),
)
async def allocate(req: AllocateRequest, session: SessionState = Depends(session_dep)):
    w_dist, w_tax = _resolve_weights(req.w_dist, req.w_tax,
                                     req.miles_per_util, req.dollars_per_util)
    if req.scoring_mode == "bucket":
        from scoring import BucketParams, DEFAULT_BUCKET_MULTS
        params = BucketParams(
            bucket_mults=tuple(req.bucket_mults) if req.bucket_mults else tuple(DEFAULT_BUCKET_MULTS),
            signal_field=req.bucket_signal_field,
            w_dist=w_dist,
            w_tax=w_tax,
        )
        result = await asyncio.to_thread(
            solve_bucket,
            n_vehicles=req.n_vehicles,
            params=params,
            expected_stay_months=req.expected_stay_months,
            session=session,
        )
        result["scoring_mode"] = "bucket"
        session.last_allocate = result
        return result

    result = await asyncio.to_thread(
        solve_both,
        n_vehicles=req.n_vehicles, w_util=req.w_util, w_rented=req.w_rented,
        w_dist=w_dist, w_tax=w_tax,
        source_limit=req.source_limit,
        expected_stay_months=req.expected_stay_months,
        session=session,
    )
    result["scoring_mode"] = "additive"
    session.last_allocate = result
    return result


class WeeklyRequest(BaseModel):
    n_batch: int = Field(default=10, gt=0, le=FLEET_SIZE)
    seed: Optional[int] = Field(default=None, ge=0, le=2**32 - 1)
    w_util: float = Field(default=DEFAULT_W_UTIL, ge=0, le=MAX_WEIGHT)
    w_rented: float = Field(default=DEFAULT_W_RENTED, ge=0, le=MAX_WEIGHT)
    miles_per_util: Optional[float] = Field(default=None, gt=0, le=MAX_EQUIV_RATE)
    dollars_per_util: Optional[float] = Field(default=None, gt=0, le=MAX_EQUIV_RATE)
    w_dist: float = Field(default=DEFAULT_W_DIST, ge=0, le=MAX_WEIGHT)
    w_tax: float = Field(default=DEFAULT_W_TAX, ge=0, le=MAX_WEIGHT)
    source_limit: Optional[int] = Field(default=None, gt=0, le=FLEET_SIZE)
    vin_list: Optional[List[str]] = Field(default=None, max_length=FLEET_SIZE)
    expected_stay_months: int = Field(default=DEFAULT_EXPECTED_STAY_MONTHS, gt=0, le=MAX_STAY_MONTHS)
    # Bucket-form opt-in. When "bucket", w_util/w_rented/source_limit are
    # ignored. Response shape stays compatible with the additive path so
    # the frontend's weekly view renders both transparently.
    scoring_mode: Literal["additive", "bucket"] = "additive"
    bucket_mults: Optional[List[float]] = None
    bucket_signal_field: str = "IN_SERVICE"


@app.post(
    "/api/weekly",
    response_model=AllocationResultResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Run weekly ILP allocation",
    description=(
        "Runs a session-scoped weekly ILP batch. If vin_list is supplied, the "
        "batch is built from those VINs in the current session fleet snapshot."
    ),
)
async def weekly(req: WeeklyRequest, session: SessionState = Depends(session_dep)):
    w_dist, w_tax = _resolve_weights(req.w_dist, req.w_tax,
                                     req.miles_per_util, req.dollars_per_util)
    if req.scoring_mode == "bucket":
        result = await asyncio.to_thread(
            solve_weekly_bucket,
            n_batch=req.n_batch, seed=req.seed,
            bucket_mults=req.bucket_mults,
            signal_field=req.bucket_signal_field,
            w_dist=w_dist, w_tax=w_tax,
            vin_list=req.vin_list,
            expected_stay_months=req.expected_stay_months,
            session=session,
        )
        session.last_weekly = result
        return result

    result = await asyncio.to_thread(
        solve_weekly_batch,
        n_batch=req.n_batch, seed=req.seed,
        w_util=req.w_util, w_rented=req.w_rented,
        w_dist=w_dist, w_tax=w_tax,
        source_limit=req.source_limit,
        vin_list=req.vin_list,
        expected_stay_months=req.expected_stay_months,
        session=session,
    )
    result["scoring_mode"] = "additive"
    session.last_weekly = result
    return result


@app.post(
    "/api/weekly_greedy",
    response_model=AllocationResultResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Run weekly Greedy baseline",
    description="Runs the nearest-feasible-dealer Greedy baseline for comparison.",
)
async def weekly_greedy(req: WeeklyRequest, session: SessionState = Depends(session_dep)):
    w_dist, w_tax = _resolve_weights(req.w_dist, req.w_tax,
                                     req.miles_per_util, req.dollars_per_util)
    return await asyncio.to_thread(
        solve_weekly_greedy,
        n_batch=req.n_batch, seed=req.seed,
        w_util=req.w_util, w_rented=req.w_rented,
        w_dist=w_dist, w_tax=w_tax,
        source_limit=req.source_limit,
        vin_list=req.vin_list,
        expected_stay_months=req.expected_stay_months,
        session=session,
    )


# ── Mutation endpoints ───────────────────────────────────────────────


class Assignment(BaseModel):
    vin: str
    dealer_code: str = ""
    dealer_name: str = ""


class ConfirmRequest(BaseModel):
    assignments: List[Assignment]


@app.post(
    "/api/confirm",
    response_model=ConfirmResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Confirm allocation assignments",
    description=(
        "Mutates only the current session's in-memory fleet snapshot. Confirmed "
        "Grounded vehicles become Transporting and receive ASSIGNED_DEALER. "
        "This endpoint does not write CSV files and clears the allocation cache."
    ),
)
async def confirm(req: ConfirmRequest, session: SessionState = Depends(session_dep)):
    result = await asyncio.to_thread(
        confirm_allocation,
        [a.model_dump() for a in req.assignments],
        session,
    )
    # Per the watchlist UX contract, allocation caches are wiped on confirm
    # but watchlist is preserved (handled at module level — `last_watchlist`
    # is not touched here).
    session.last_weekly = None
    session.last_allocate = None
    return result


@app.post(
    "/api/reset",
    response_model=ResetResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Reset session fleet state",
    description="Restores the current session's in-memory fleet snapshot from the immutable baseline.",
)
async def reset(request: Request, session: SessionState = Depends(session_dep)):
    store: SessionStore = request.app.state.store
    return await asyncio.to_thread(reset_fleet, session, store)


# ── Home + cache + watchlist ─────────────────────────────────────────


@app.get(
    "/api/home",
    response_model=HomeResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Get Home dashboard data",
    description=(
        "Returns the session-scoped Home dashboard payload plus the fleet-wide "
        "watchlist cache. Some trend series are explicitly marked synthetic."
    ),
)
async def home(session: SessionState = Depends(session_dep)):
    """Home page data — per-session fleet + cached batch + fleet-wide watchlist.

    Hero stats (savings vs Greedy) are computed client-side from cached weeklyData
    + weeklyGreedyData since backend does not persist greedy separately.

    8wk trends are synthetic for v1 (no allocation history persistence yet);
    everything else derives from session-aware /api/overview + /api/fleet and
    `session.cached()`.
    """
    from collections import Counter
    overview_data = await asyncio.to_thread(get_overview, session)
    fleet_data = await asyncio.to_thread(get_fleet_inventory, session)
    cached = session.cached()
    counts = fleet_data.get("counts", {})

    # ── 8wk trends — SYNTHETIC PLACEHOLDER ──
    # No allocation history persisted yet; replace with real time-series once
    # data_csv/allocation_history.csv is built. Frontend renders with a
    # "synthetic" badge based on the _placeholder flag.
    trends = {
        "_placeholder": True,
        "_note": "Synthetic placeholder until allocation history is persisted",
        "placement_rate":    [80, 82, 81, 84, 87, 90, 93, 94],
        "rank1_pct":         [60, 63, 65, 70, 72, 78, 82, 85],
        # ILP-vs-Greedy annual property-tax savings ($/batch). Trends
        # upward as calibration matured; real values will come from
        # allocation_history once persisted.
        "annual_tax_saved":  [850, 920, 1080, 1250, 1410, 1560, 1720, 1861],
        "hhi":               [1200, 1180, 1150, 1100, 1050, 990, 900, 821],
    }

    # ── Fleet pipeline (4 weeks — current is REAL; prior 3 weeks SYNTHETIC) ──
    g = int(counts.get("grounded", 0))
    t = int(counts.get("transporting", 0))
    d = int(counts.get("delivered", 0))
    inc = int(counts.get("incoming", 0))
    pipeline = [
        {"week": "W-3", "grounded": int(g * 0.92), "transporting": int(t * 0.85), "delivered": int(d * 0.88), "_synthetic": True},
        {"week": "W-2", "grounded": int(g * 0.96), "transporting": int(t * 0.90), "delivered": int(d * 0.91), "_synthetic": True},
        {"week": "W-1", "grounded": int(g * 0.98), "transporting": int(t * 0.95), "delivered": int(d * 0.95), "_synthetic": True},
        {"week": "this", "grounded": g, "transporting": t, "delivered": d, "_synthetic": False},
    ]

    # ── Dealer heatmap from /api/overview destinations ──
    destinations = overview_data.get("dealers", [])
    heatmap = []
    for dst in destinations:
        util = float(dst.get("UTIL_RATE", 0) or 0)
        if util >= 0.90: band = "vhigh"
        elif util >= 0.75: band = "high"
        elif util >= 0.65: band = "good"
        elif util >= 0.50: band = "mid"
        elif util >= 0.40: band = "low"
        else: band = "vlow"
        name = dst.get("DEALER_NAME", "") or ""
        short = name.replace("FaaS_Dealer_", "").upper() if name.startswith("FaaS_Dealer_") else (name.upper() or dst.get("DEALER_CODE", ""))
        heatmap.append({
            "code": short,
            "full_name": name,
            "dealer_code": dst.get("DEALER_CODE", ""),
            "util": round(util, 4),
            "rented": int(dst.get("RENTED", 0) or 0),
            "in_service": int(dst.get("IN_SERVICE", 0) or 0),
            "band": band,
        })
    heatmap.sort(key=lambda x: -x["util"])
    heatmap = heatmap[:30]

    # ── Top moves (rule-based from cached batch) ──
    top_moves = []
    if cached and cached.get("vehicles"):
        dealer_counts = Counter()
        # Distance dedup: multiple cars on the same (source, dealer) arc
        # ride one carrier trip, so count each arc's miles ONCE. Mirrors
        # engine._solve_*'s unique_arcs accounting and keeps the home
        # narrative consistent with `cached["total_distance"]`.
        unique_arcs = {}
        n_assigned = 0
        for v in cached["vehicles"]:
            a = v.get("assigned")
            if a:
                dealer_counts[a.get("dealer_name", "?")] += 1
                unique_arcs[(v.get("source"), a.get("dealer_code"))] = float(a.get("distance", 0) or 0)
                n_assigned += 1
        total_dist = sum(unique_arcs.values())
        n_arcs = len(unique_arcs)

        if dealer_counts:
            top_dealer, top_count = dealer_counts.most_common(1)[0]
            top_moves.append({
                "tag": "demand",
                "tag_label": "Demand catch",
                "title": "{} vehicles routed to {}".format(top_count, top_dealer.replace("FaaS_Dealer_", "")),
                "body": "This batch's largest single-dealer placement. Concentrated demand absorbed by one well-utilized dealer.",
                "metrics": [
                    {"label": "Placed", "value": str(top_count)},
                    {"label": "Dealer", "value": top_dealer.replace("FaaS_Dealer_", "")[:16]},
                ],
            })

        unique_dealers = len(dealer_counts)
        if unique_dealers >= 10:
            top_moves.append({
                "tag": "spread",
                "tag_label": "Concentration ease",
                "title": "Used {} of 30 dealers this batch".format(unique_dealers),
                "body": "Distribution stays broad, avoiding concentration risk on a few saturating dealers.",
                "metrics": [
                    {"label": "Dealers used", "value": "{} of 30".format(unique_dealers)},
                    {"label": "Top dealer share", "value": "{}%".format(int(dealer_counts.most_common(1)[0][1] * 100 / max(n_assigned, 1)))},
                ],
            })

        if n_arcs > 0:
            # Avg per unique carrier trip (not per vehicle) — matches the
            # joint trucking cost the ILP actually minimizes.
            avg_d = total_dist / n_arcs
            top_moves.append({
                "tag": "win",
                "tag_label": "Routing efficiency",
                "title": "Avg trucking distance: {:.0f} mi/trip".format(avg_d),
                "body": "Cost-aware w_dist keeps each carrier route tight. {} cars across {} unique trips.".format(n_assigned, n_arcs),
                "metrics": [
                    {"label": "Avg / trip", "value": "{:.0f} mi".format(avg_d)},
                    {"label": "Total", "value": "{:,.0f} mi".format(total_dist)},
                ],
            })

    # ── Watchlist (fleet-wide singleton — not per-session) ──
    if state.last_watchlist:
        wl_alerts = state.last_watchlist.get("alerts", []) or []
        wl_meta = {
            "refreshed_at": state.last_watchlist.get("refreshed_at"),
            "count": state.last_watchlist.get("count", len(wl_alerts)),
            "max_items": state.last_watchlist.get("max_items"),
        }
    else:
        wl_alerts = []
        wl_meta = {"refreshed_at": None, "count": 0, "max_items": watchlist.MAX_ITEMS if hasattr(watchlist, "MAX_ITEMS") else 5}

    return {
        "trends": trends,
        "fleet_state": {
            "grounded": g,
            "transporting": t,
            "delivered": d,
            "incoming": inc,
            "total": int(counts.get("total", g + t + d + inc)),
            "pipeline_by_week": pipeline,
        },
        "dealer_heatmap": heatmap,
        "top_moves": top_moves,
        "watchlist": wl_alerts,
        "watchlist_meta": wl_meta,
    }


# Lock around the watchlist refresh so concurrent clicks don't fan out
# parallel Claude CLI subprocesses (each takes 30-60s and they all touch
# the same singleton). The first caller runs the refresh; later callers
# wait and return the same fresh cache.
_watchlist_lock = asyncio.Lock()


@app.post(
    "/api/watchlist/refresh",
    response_model=Dict[str, Any],
    summary="Refresh fleet watchlist",
    description=(
        "Recomputes fleet-wide watchlist signals and returns up to five alerts. "
        "The result is cached globally, not per session."
    ),
)
async def watchlist_refresh(session: SessionState = Depends(session_dep)):
    """Recompute watchlist signals + agent narration. Cached in
    state.last_watchlist (fleet-wide singleton). Concurrent refresh clicks
    are serialized — only one Claude CLI subprocess runs at a time."""
    async with _watchlist_lock:
        result = await asyncio.to_thread(watchlist.refresh)
        # Usage goes to the gateway via a response header, never into the body
        # or the cache.
        usage = result.pop("_usage", None)
        state.set_watchlist(result)
        headers = {}
        if usage is not None:
            headers["X-Demo-Usage"] = json.dumps(usage, separators=(",", ":"), default=str)
        return JSONResponse(content=result, headers=headers)


@app.get(
    "/api/allocation_cache",
    response_model=AllocationCacheResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Get cached allocation result",
    description=(
        "Returns the current session's latest allocation result, optionally "
        "filtered by VIN substring or dealer code. Confirm clears this cache."
    ),
)
def allocation_cache(vin: str = "", dealer: str = "",
                     session: SessionState = Depends(session_dep)):
    """Return cached allocation results for THIS session, optionally filtered."""
    data = session.cached()
    if not data:
        return {"error": "No allocation has been run yet."}

    vehicles = data.get("vehicles")
    if vehicles is None:
        raw_vins = data.get("vins")
        if raw_vins is None:
            return {"error": "No vehicle-level data in cached results."}
        vehicles = []
        for v in raw_vins:
            vehicles.append({
                "vin": v.get("VIN", ""),
                "assigned_dealer": v.get("DEALER_CODE", ""),
                "dealer_name": v.get("DEALER_NAME", ""),
                "source": v.get("SOURCE", ""),
                "dealer_state": v.get("DEALER_STATE", ""),
                "distance": v.get("DISTANCE", 0),
                "alloc_score": v.get("ALLOC_SCORE", 0),
                "utilization_score": v.get("UTILIZATION_SCORE", 0),
                "prop_tax": v.get("PROP_TAX", 0),
                "alternatives": [],
            })

    if vin:
        vin_up = vin.upper()
        vehicles = [v for v in vehicles if vin_up in v["vin"].upper()]
    if dealer:
        dealer_up = dealer.upper()
        vehicles = [v for v in vehicles
                    if (v.get("assigned_dealer") or "").upper() == dealer_up
                    or any(a["dealer_code"].upper() == dealer_up for a in v.get("alternatives", []))]

    summary = {
        "batch_size": data.get("batch_size"),
        "n_assigned": data.get("n_assigned"),
        "total_alloc_score": data.get("total_alloc_score"),
        "total_transport": data.get("total_transport"),
        "params": data.get("params"),
    }
    return {"summary": summary, "vehicles": vehicles}


@app.get(
    "/api/export/confirmed",
    response_model=ConfirmedExportResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Export confirmed allocation snapshot",
    description=(
        "Future integration hook. Returns confirmed Transporting vehicles from "
        "the current session as JSON, but does not connect to an external system, "
        "write CSV files, or run automatically after /api/confirm."
    ),
)
def export_confirmed(session: SessionState = Depends(session_dep)):
    fleet_data = get_fleet_inventory(session=session)
    vehicles = [
        v for v in fleet_data.get("vehicles", [])
        if v.get("status") == "Transporting" and v.get("assigned_dealer")
    ]
    return {
        "status": "stub",
        "integration_status": "not_connected",
        "format": "json",
        "count": len(vehicles),
        "vehicles": vehicles,
        "note": (
            "Future export adapter only. This endpoint reads the current "
            "session snapshot and does not persist data or call external systems."
        ),
    }


@app.post(
    "/api/approve_constraint",
    response_model=ApprovalTokenResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Mint mutation approval token",
    description="Mints a single-use token for MCP mutation tools after user approval.",
)
def approve_constraint(session: SessionState = Depends(session_dep)):
    """Mint a single-use approval token for a mutation tool (add_constraint /
    remove_constraint)."""
    return {"approval_token": mcp_server.mint_approval_token(session.client_id)}


@app.get(
    "/api/chat_status",
    responses={200: {"model": ChatStatusResponse}, 400: {"model": ErrorResponse}},
    summary="Get chat quota status",
    description="Returns the current session's remaining server-enforced chat turns.",
)
def chat_status(request: Request, session: SessionState = Depends(session_dep)):
    """Return the chat budget.

    In DEMO_MODE the gateway owns the quota, so this app does not enforce a
    per-session cap; it echoes the gateway's rolling-24h counters from the
    `X-Demo-Quota-Limit` / `X-Demo-Quota-Remaining` request headers (the owner
    token carries `unlimited`) for display only.
    """
    if DEMO_MODE:
        q_limit = request.headers.get("X-Demo-Quota-Limit")
        q_remaining = request.headers.get("X-Demo-Quota-Remaining")
        return {
            # App does not cap chat in demo mode — the gateway does.
            "limit": -1,
            "remaining": -1,
            "disabled": False,
            "demo": True,
            "quota_limit": q_limit,
            "quota_remaining": q_remaining,
        }
    remaining = max(CHAT_LIMIT_PER_SESSION - session.chat_count, 0)
    return {
        "limit": CHAT_LIMIT_PER_SESSION,
        "remaining": remaining,
        "disabled": CHAT_LIMIT_PER_SESSION <= 0,
    }


# ── Chat / Agent ─────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str
    context: str = ""
    # client_id: stable UUID per browser tab. Used as the app-side key for
    # the Claude CLI session id, so multi-turn fidelity is handled by
    # `claude -p --resume` rather than replaying history every request.
    client_id: str


# Tracks first-turn behavior by session within this request cycle via
# session.chat_count. Frontend treats "Clear chat" as a new client_id, which
# naturally starts a fresh chat state.


async def _chat_event_stream(req: ChatRequest, is_first_turn: bool):
    """Async generator yielding SSE events. Runs the agent in an
    asyncio.to_thread so subprocess I/O doesn't block the event loop —
    other HTTP requests (including MCP tool dispatch) stay responsive."""
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _runner():
        try:
            for event in run_agent_stream(
                message=req.message,
                context=req.context,
                session_id=req.client_id,
                is_first_turn=is_first_turn,
            ):
                asyncio.run_coroutine_threadsafe(q.put(event), loop)
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "error", "text": str(e)}), loop,
            )
        finally:
            asyncio.run_coroutine_threadsafe(q.put(None), loop)

    asyncio.create_task(asyncio.to_thread(_runner))

    while True:
        event = await q.get()
        if event is None:
            break
        yield "data: " + json.dumps(event, default=str) + "\n\n"

    yield "data: [DONE]\n\n"


@app.post(
    "/api/chat",
    responses={
        200: {"content": {"text/event-stream": {}}},
        400: {"model": ErrorResponse},
        429: {"model": ChatQuotaError},
    },
    summary="Stream chat agent response",
    description=(
        "Streams Server-Sent Events from the Claude CLI-backed FaaS agent. The "
        "X-Session-Id header must match request body client_id."
    ),
)
async def chat(req: ChatRequest, session: SessionState = Depends(session_dep)):
    # Sanity: the X-Session-Id header MUST match the body's client_id —
    # otherwise the MCP layer would resolve a different session for the
    # tool callbacks than what the chat itself is keyed on.
    if session.client_id != req.client_id:
        raise HTTPException(
            status_code=400,
            detail="X-Session-Id header does not match request body client_id.",
        )

    # In DEMO_MODE the gateway enforces quota (rolling 24h) and returns 429
    # itself before forwarding, so this app must not double-count or cap.
    if DEMO_MODE:
        is_first_turn = session.chat_count == 0
        session.chat_count += 1
        return StreamingResponse(
            _chat_event_stream(req, is_first_turn=is_first_turn),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Shared-quota rate limit. Count the request BEFORE streaming so an
    # aborted SSE still consumes a turn (otherwise an attacker could just
    # close the socket to bypass the cap).
    remaining_before = CHAT_LIMIT_PER_SESSION - session.chat_count
    if remaining_before <= 0:
        return JSONResponse(
            status_code=429,
            headers={"X-Chat-Remaining": "0", "X-Chat-Limit": str(CHAT_LIMIT_PER_SESSION)},
            content={
                "error": "chat_quota_exhausted",
                "message": (
                    "Agent chat is disabled for this deployment."
                    if CHAT_LIMIT_PER_SESSION <= 0 else
                    "You've used your {} messages for this session — refresh the "
                    "page to start a new session.".format(CHAT_LIMIT_PER_SESSION)
                ),
                "limit": CHAT_LIMIT_PER_SESSION,
                "remaining": 0,
            },
        )
    is_first_turn = session.chat_count == 0
    session.chat_count += 1

    return StreamingResponse(
        _chat_event_stream(req, is_first_turn=is_first_turn),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Chat-Remaining": str(CHAT_LIMIT_PER_SESSION - session.chat_count),
            "X-Chat-Limit": str(CHAT_LIMIT_PER_SESSION),
        },
    )


if __name__ == "__main__":
    load_baseline()
    print(f"Data loaded. Starting server on http://localhost:{PORT} ...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
