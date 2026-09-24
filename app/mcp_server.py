"""FaaS Optimizer — MCP server.

Replaces the prompt-injected tool layer (app/tools.py) with native MCP
tool registration. Tools call engine functions in-process (no HTTP
self-calls — that was the deadlock root cause). State is shared with
the FastAPI server via app/state.py.

Mounted on the FastAPI app at /mcp (streamable-http transport). The
Claude CLI agent connects to it through an inline --mcp-config.
"""

from __future__ import annotations

import ast
import contextvars
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

import pandas as pd
from mcp.server.fastmcp import FastMCP

from engine import (
    DEFAULT_W_DIST,
    DEFAULT_W_RENTED,
    DEFAULT_W_TAX,
    DEFAULT_W_UTIL,
    load_baseline,
    solve_both,
    solve_weekly_batch,
    solve_weekly_greedy,
)
import state

MVP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = MVP_DIR.parent
REGISTRY_PATH = MVP_DIR / "registry.json"
ENGINE_PATH = MVP_DIR / "engine.py"
CONSTRAINTS_DIR = MVP_DIR / "constraints"
CONSTRAINTS_DIR.mkdir(exist_ok=True)


# ── Per-session resolution ───────────────────────────────────────────
# The ASGI middleware (see :func:`_strip_session_path` below) parses the
# session id from `?faas_session_id=<sid>` or the legacy `/<sid>/...` path
# shape and stashes it in this contextvar before forwarding to FastMCP. Tool
# handlers call :func:`_session` to resolve the matching SessionState via the
# store injected by server.py's lifespan.

_current_session_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "faas_session_id", default=None,
)

# Hex + dash, 8+ chars. Matches crypto.randomUUID() output ("a1b2c3d4-...")
# but NOT the MCP transport's own paths ("messages", "sse", etc. — those
# contain non-hex characters and are passed through unchanged).
_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-"
    r"[0-9a-fA-F]{12}$"
)

# File uploads analysed through MCP are expected to be local CSVs only.
MAX_ANALYZE_FILE_SIZE_BYTES = 10 * 1024 * 1024


def _validate_user_csv_path(raw_path: str) -> Path:
    """Resolve and validate user-provided CSV paths for analysis.

    The path must stay within PROJECT_DIR, point to an existing .csv file, and
    stay within the analysis size budget.
    """
    fp = Path(raw_path)
    if not fp.is_absolute():
        fp = PROJECT_DIR / fp
    try:
        fp = fp.resolve()
    except OSError:
        raise ValueError("Invalid file path.")

    try:
        fp.relative_to(PROJECT_DIR)
    except ValueError:
        raise ValueError("File path must be inside the repository directory.")

    if fp.suffix.lower() != ".csv":
        raise ValueError("Only CSV files are supported.")

    if not fp.is_file():
        raise FileNotFoundError(f"File not found: {raw_path}")

    if fp.stat().st_size > MAX_ANALYZE_FILE_SIZE_BYTES:
        raise ValueError("Uploaded file is too large for analysis.")

    return fp


# Internal key guarding the /mcp mount. The agent subprocess sends it as the
# `X-Internal-Key` header on every MCP request; the mount rejects anything
# without the correct value (403), so nothing else on the box can reach the
# MCP tools. Generated once at process start unless pinned via env.
_INTERNAL_KEY = os.environ.get("FAAS_MCP_KEY") or secrets.token_urlsafe(32)


def get_internal_key() -> str:
    return _INTERNAL_KEY


# Mutation-tool approval tokens — minted by /api/approve_constraint after
# the user clicks Accept on the proposed code. Single-use.
_APPROVAL_TOKENS: dict[str, set[str]] = {}
_APPROVAL_TOKEN_LOCK = threading.Lock()

# Per-session store reference, set during FastAPI lifespan via set_store().
_STORE = None


def set_store(store) -> None:
    """Inject the SessionStore. Called from server.py's lifespan so the
    MCP tool handlers can resolve per-session state."""
    global _STORE
    _STORE = store


def _session():
    """Resolve the current request's SessionState.

    Reads the session id stashed in `_current_session_id` by the
    path-strip middleware, then looks the session up in the global
    SessionStore. Raises if either layer is missing — callers should
    treat that as a programming error (MCP tool was invoked without the
    `/mcp/?faas_session_id=<sid>` URL).
    """
    sid = _current_session_id.get()
    if not sid:
        raise RuntimeError(
            "MCP tool called without session context — agent must use "
            "the per-session URL `/mcp/?faas_session_id=<client_id>`."
        )
    if _STORE is None:
        raise RuntimeError(
            "MCP session store not initialised; server lifespan must "
            "call mcp_server.set_store(store)."
        )
    return _STORE.get_or_create(sid)


def mint_approval_token(session_id: str) -> str:
    tok = secrets.token_urlsafe(16)
    with _APPROVAL_TOKEN_LOCK:
        _APPROVAL_TOKENS.setdefault(session_id, set()).add(tok)
    return tok


def _consume_approval_token(session_id: str, tok: str) -> bool:
    if not tok:
        return False
    with _APPROVAL_TOKEN_LOCK:
        tokens = _APPROVAL_TOKENS.get(session_id)
        if not tokens or tok not in tokens:
            return False
        tokens.discard(tok)
        if not tokens:
            _APPROVAL_TOKENS.pop(session_id, None)
        return True


# ── Registry helpers (ported from tools.py without HTTP) ────────────


def _load_registry() -> Dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {"datasets": {}}
    with open(REGISTRY_PATH, "r") as f:
        return json.load(f)


def _iter_datasets() -> List[tuple]:
    raw = _load_registry().get("datasets", {})
    if isinstance(raw, dict):
        return list(raw.items())
    return [(d["name"], d) for d in raw]


def _resolve_dataset_path(rel: str) -> Path:
    return (PROJECT_DIR / rel).resolve()


_SANDBOX_RENAMES = {
    "dealer_utilization": {"DEALER_ID": "DEALER_CODE", "NAME": "DEALER_NAME"},
}


_DATAFRAME_CACHE: Optional[Dict[str, pd.DataFrame]] = None

_FORBIDDEN_PD_IO = {
    "read_json", "read_xml", "read_html", "read_clipboard", "read_excel", "read_fwf",
    "read_pickle", "read_feather", "read_parquet", "read_orc", "read_sas", "read_spss",
    "read_stata", "read_sql", "read_sql_query", "read_sql_table", "read_hdf",
}

_FORBIDDEN_DF_IO_METHODS = {
    "to_csv", "to_json", "to_excel", "to_parquet", "to_pickle", "to_hdf",
    "to_feather", "to_orc", "to_sql",
}

_FORBIDDEN_NP_IO = {
    "load", "loadtxt", "fromfile", "genfromtxt", "save", "savez", "savez_compressed",
    "savetxt", "savez", "save", "dump",
}


def _extract_call_root(node: ast.AST) -> Optional[str]:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


# Callables that can reach the filesystem, import machinery, or object
# internals — blocked by name regardless of how they are used in query_data.
_FORBIDDEN_QUERY_NAMES = {
    "open", "exec", "eval", "compile", "__import__",
    "getattr", "setattr", "delattr", "hasattr", "vars", "globals", "locals",
    "input", "breakpoint", "help", "exit", "quit", "memoryview",
}


def _assert_query_code_safe(tree: ast.AST) -> None:
    for n in ast.walk(tree):
        # No imports inside the sandbox.
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            raise ValueError("Disallowed: imports are not permitted in query_data.")
        # Block dunder / private attribute access (e.g. __globals__,
        # __class__, __subclasses__) which is the usual sandbox-escape route.
        if isinstance(n, ast.Attribute) and n.attr.startswith("_"):
            raise ValueError(
                f"Disallowed attribute access: {n.attr}. "
                "Underscore-prefixed / dunder attributes are blocked in query_data."
            )
        # Block reads of dunder names (e.g. bare __builtins__ references).
        if isinstance(n, ast.Name) and n.id.startswith("__") and n.id.endswith("__"):
            raise ValueError(f"Disallowed name: {n.id}.")
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        if isinstance(func, ast.Attribute):
            root = _extract_call_root(func)
            if root == "pd" and func.attr in _FORBIDDEN_PD_IO:
                raise ValueError(
                    f"Disallowed pd call: pd.{func.attr}. Use pre-loaded DataFrame views "
                    "or ask the tool maintainers to expose a specific accessor."
                )
            if root == "np" and func.attr in _FORBIDDEN_NP_IO:
                raise ValueError(
                    f"Disallowed np call: np.{func.attr}. File/network operations are blocked in query_data."
                )
            # Block common data-frame write/export methods, even when called on
            # derived pandas objects, not only on pd.<method>.
            if func.attr in _FORBIDDEN_DF_IO_METHODS:
                raise ValueError(
                    f"Disallowed DataFrame method call: {func.attr}. "
                    "query_data is read-only for security."
                )
        elif isinstance(func, ast.Name) and func.id in _FORBIDDEN_QUERY_NAMES:
            raise ValueError(f"Disallowed builtin call: {func.id}.")


def _load_all_dataframes() -> Dict[str, pd.DataFrame]:
    """Return all registered CSVs as DataFrames. Read once at first call,
    cached module-side. Returns fresh shallow copies so the query_data
    sandbox can rebind / drop columns without poisoning the cache; this
    is ~10x cheaper than re-parsing the CSVs (zip_centroids alone is 41k
    rows)."""
    global _DATAFRAME_CACHE
    if _DATAFRAME_CACHE is None:
        loaded: Dict[str, pd.DataFrame] = {}
        for name, info in _iter_datasets():
            p = _resolve_dataset_path(info["path"])
            if p.exists():
                try:
                    df = pd.read_csv(p)
                    rename = _SANDBOX_RENAMES.get(name)
                    if rename:
                        df = df.rename(columns=rename)
                    loaded[name] = df
                except Exception:
                    pass
        _DATAFRAME_CACHE = loaded
    return {name: df.copy(deep=False) for name, df in _DATAFRAME_CACHE.items()}


# ── FastMCP instance ─────────────────────────────────────────────────

mcp = FastMCP(
    "faas",
    instructions=(
        "FaaS Vehicle Allocation tools. Two scoring modes coexist; check the "
        "response's `scoring_mode` field before reasoning about which formula "
        "applies. Additive (production default): "
        "alloc_score = w_util*UTIL + w_rented*RENTED - w_dist*dist/NORM "
        "- w_tax*tax/NORM, with the four weights jointly calibrated via the "
        "2026-04-21 4-D Pareto knee plus the 2026-04-24 cost-aware w_dist "
        "override. Bucket (2026-05-21 spec): "
        "alloc_score = bucket_mult(tier_of(IN_SERVICE)) - w_dist*dist/NORM "
        "- w_tax*tax/NORM, with bucket_mults calibrated via the 2026-05-22 "
        "6-D Sobol sweep. Tools that run or analyze allocations accept a "
        "`scoring_mode` parameter to choose between them. Rank is the "
        "per-vehicle quality metric; raw alloc_score is not comparable "
        "across vehicles or across modes."
    ),
    # Inner app serves at "/" so when FastAPI mounts at "/mcp", the public
    # URL is /mcp (not /mcp/mcp).
    streamable_http_path="/",
)


# ── 1. list_datasets ─────────────────────────────────────────────────


@mcp.tool(description="List all registered datasets in registry.json.")
def list_datasets() -> Dict[str, Any]:
    """List all registered datasets with names, paths, descriptions, key columns."""
    summaries = []
    for name, info in _iter_datasets():
        abs_path = _resolve_dataset_path(info["path"])
        rows = info.get("row_count")
        if rows is None and abs_path.exists():
            try:
                rows = len(pd.read_csv(abs_path))
            except Exception:
                pass
        keys = info.get("key_columns") or list((info.get("columns") or {}).keys())
        summaries.append({
            "name": name,
            "path": info["path"],
            "description": info.get("description", ""),
            "key_columns": keys,
            "rows": rows,
        })
    return {"datasets": summaries}


# ── 2. inspect_data ──────────────────────────────────────────────────


@mcp.tool(description="Show schema, per-column stats, and 3 sample rows for a registered dataset.")
def inspect_data(dataset_name: str) -> Dict[str, Any]:
    entry = next((info for n, info in _iter_datasets() if n == dataset_name), None)
    if entry is None:
        raise ValueError(f"Dataset '{dataset_name}' not found in registry.")
    p = _resolve_dataset_path(entry["path"])
    if not p.exists():
        raise FileNotFoundError(f"File not found: {entry['path']}")
    df = pd.read_csv(p)
    rename = _SANDBOX_RENAMES.get(dataset_name)
    if rename:
        df = df.rename(columns=rename)

    schema = []
    for col in df.columns:
        s = {
            "column": col,
            "dtype": str(df[col].dtype),
            "nulls": int(df[col].isna().sum()),
            "unique": int(df[col].nunique()),
        }
        if pd.api.types.is_numeric_dtype(df[col]):
            s["min"] = float(df[col].min()) if not df[col].isna().all() else None
            s["max"] = float(df[col].max()) if not df[col].isna().all() else None
        else:
            non_null = df[col].dropna()
            s["min"] = str(non_null.min()) if len(non_null) else None
            s["max"] = str(non_null.max()) if len(non_null) else None
        schema.append(s)
    return {
        "name": dataset_name,
        "path": entry["path"],
        "shape": list(df.shape),
        "columns": schema,
        "sample_rows": df.head(3).fillna("").to_dict("records"),
    }


# ── 3. query_data ────────────────────────────────────────────────────

_QUERY_BUILTINS = {
    "len": len, "range": range, "list": list, "dict": dict,
    "tuple": tuple, "set": set, "str": str, "int": int, "float": float,
    "bool": bool, "sorted": sorted, "enumerate": enumerate, "zip": zip,
    "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
    "print": print, "isinstance": isinstance,
    "True": True, "False": False, "None": None,
}


def _safe_read_csv(path, *args, **kwargs):
    """`pd.read_csv` replacement gated to registry-listed datasets only.
    Closes the gap where the query_data sandbox could otherwise read
    `~/.ssh/id_rsa` etc. through the otherwise-exposed pandas object."""
    target = Path(str(path)).resolve()
    allowed = {
        _resolve_dataset_path(info["path"]).resolve()
        for _, info in _iter_datasets()
    }
    if target not in allowed:
        raise PermissionError(
            "pd.read_csv is restricted to registered datasets. Use the "
            "pre-loaded DataFrame variables (e.g. faas_eligible_vehicles) "
            "in this sandbox instead of calling read_csv directly."
        )
    return pd.read_csv(target, *args, **kwargs)


class _SafePandas:
    """Proxy that forwards attribute access to the real `pd` module,
    except `read_csv` which is replaced by the path-gated wrapper above.
    Lets queries use `pd.DataFrame`, `pd.concat`, `pd.merge`, etc. as
    before without exposing arbitrary file I/O."""

    read_csv = staticmethod(_safe_read_csv)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in _FORBIDDEN_PD_IO:
            raise AttributeError(
                f"pandas.{name} is not exposed in query_data to keep file/network operations blocked."
            )
        if name in _FORBIDDEN_DF_IO_METHODS:
            raise AttributeError(
                f"pandas.{name} is not exposed in query_data because it mutates or exports data."
            )
        return getattr(pd, name)


@mcp.tool(
    description=(
        "Execute a pandas expression in a sandbox. All registered datasets are "
        "pre-loaded as DataFrames keyed by their registry name (e.g. "
        "'faas_eligible_vehicles', 'dealer_utilization'). The last expression "
        "in the code is captured as the result. Timeout: 5s."
    ),
)
def query_data(code: str) -> Dict[str, Any]:
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

    namespace: Dict[str, Any] = {"pd": _SafePandas(), "np": np, "__builtins__": _QUERY_BUILTINS}
    namespace.update(_load_all_dataframes())

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"SyntaxError: {e}")

    _assert_query_code_safe(tree)

    last_expr = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last_expr = tree.body.pop()

    def _run():
        if tree.body:
            exec(compile(tree, "<query>", "exec"), namespace)
        if last_expr is not None:
            return eval(compile(ast.Expression(body=last_expr.value), "<query>", "eval"), namespace)
        return None

    with ThreadPoolExecutor(max_workers=1) as ex:
        try:
            result = ex.submit(_run).result(timeout=5)
        except FuturesTimeoutError:
            raise TimeoutError("Code execution timed out after 5s")

    if isinstance(result, pd.DataFrame):
        total = len(result)
        truncated = result.head(50).to_dict("records")
        return {"result": truncated, "rows": total, "truncated": total > 50}
    if isinstance(result, pd.Series):
        total = len(result)
        return {"result": result.head(50).to_dict(), "items": total, "truncated": total > 50}
    if isinstance(result, (dict, list, int, float, str, bool, type(None))):
        return {"result": result}
    return {"result": str(result)}


# ── 4. run_allocation ────────────────────────────────────────────────


@mcp.tool(
    description=(
        "Run the FaaS allocation engine (Greedy + V2 ILP) and return a KPI "
        "summary. Populates the in-process cache so get_allocation_result can "
        "fetch the per-vehicle details afterwards. Two scoring modes available "
        "via `scoring_mode`: 'additive' (default — continuous w_util*UTIL + "
        "w_rented*RENTED util-side) or 'bucket' (2026-05-21 spec — dealers "
        "tiered into 4 IN_SERVICE-size buckets, bucket_mult is the util-side "
        "score, distance/tax break ties). Defaults track engine.DEFAULT_W_* "
        "for additive and scoring.DEFAULT_BUCKET_MULTS for bucket."
    ),
)
def run_allocation(
    n_vehicles: Optional[int] = None,
    w_util: Optional[float] = None,
    w_rented: Optional[float] = None,
    w_dist: Optional[float] = None,
    w_tax: Optional[float] = None,
    miles_per_util: Optional[float] = None,
    dollars_per_util: Optional[float] = None,
    scoring_mode: str = "additive",
    bucket_mults: Optional[list] = None,
    bucket_signal_field: str = "IN_SERVICE",
) -> Dict[str, Any]:
    session = _session()
    data = load_baseline()
    if w_dist is None:
        w_dist = DEFAULT_W_DIST
    if w_tax is None:
        w_tax = DEFAULT_W_TAX
    if miles_per_util is not None:
        w_dist = data["distance_norm"] / float(miles_per_util)
    if dollars_per_util is not None:
        w_tax = data["tax_norm"] / float(dollars_per_util)

    if scoring_mode == "bucket":
        from bucket_pipeline import solve_bucket
        from scoring import BucketParams, DEFAULT_BUCKET_MULTS
        params = BucketParams(
            bucket_mults=tuple(bucket_mults) if bucket_mults else tuple(DEFAULT_BUCKET_MULTS),
            signal_field=bucket_signal_field,
            w_dist=w_dist,
            w_tax=w_tax,
        )
        full = solve_bucket(
            n_vehicles=n_vehicles,
            params=params,
            session=session,
        )
        full["scoring_mode"] = "bucket"
        session.last_allocate = full
        return {
            "params": full["params"],
            "scoring_mode": "bucket",
            "n_assigned": full["n_assigned"],
            "total_alloc_score": full["total_alloc_score"],
            "total_distance": full["total_distance"],
        }

    if w_util is None:
        w_util = DEFAULT_W_UTIL
    if w_rented is None:
        w_rented = DEFAULT_W_RENTED

    full = solve_both(
        n_vehicles=n_vehicles, w_util=w_util, w_rented=w_rented,
        w_dist=w_dist, w_tax=w_tax,
        session=session,
    )
    full["scoring_mode"] = "additive"
    session.last_allocate = full

    return {
        "params": full["params"],
        "scoring_mode": "additive",
        "greedy": full["greedy"],
        "v2": full["v2"],
        "delta": full["delta"],
        "delta_pct": full["delta_pct"],
        "num_dealers_v2": len(full.get("dealers_v2", [])),
        "num_dealers_greedy": len(full.get("dealers_greedy", [])),
    }


# ── 5. add_constraint (gated by approval_token) ──────────────────────


@mcp.tool(
    description=(
        "Register a constraint plugin against THIS session (in-memory, "
        "not persisted to disk). Requires an approval_token minted by the "
        "UI after the user accepts the proposed code (Rule 9 — agent never "
        "mutates state without explicit user approval). Without a valid "
        "token, returns the proposed code as a preview."
    ),
)
def add_constraint(
    name: str, description: str, code: str,
    approval_token: Optional[str] = None,
) -> Dict[str, Any]:
    session = _session()
    safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    if not safe_name:
        raise ValueError("Invalid constraint name.")
    # Reject unsafe constraint code up front — before it is ever stored or
    # approved — using the AST whitelist. This is enforced again at solve time
    # by constraints.load_all (defense in depth). Arbitrary code never runs.
    from constraints import validate_constraint_source
    try:
        validate_constraint_source(code)
    except ValueError as e:
        return {
            "status": "rejected",
            "name": safe_name,
            "error": str(e),
            "message": (
                "Constraint code failed the safety validator and was not stored. "
                "Constraints may only add PuLP constraints to the model using the "
                "passed model / x / veh / dealer objects and lpSum."
            ),
        }

    if not approval_token or not _consume_approval_token(session.client_id, approval_token):
        return {
            "status": "needs_approval",
            "name": safe_name,
            "description": description,
            "code_preview": code,
            "message": (
                "Mutation requires user approval. The UI should show the "
                "proposed code and call POST /api/approve_constraint to mint "
                "an approval_token, then re-invoke this tool with it."
            ),
        }

    header = f'"""\nConstraint: {safe_name}\n{description}\n"""\n\n'
    session.constraints[safe_name] = header + code + "\n"
    return {
        "status": "registered",
        "name": safe_name,
        "session_constraint_count": len(session.constraints),
    }


# ── 6. list_constraints ──────────────────────────────────────────────


@mcp.tool(description="List constraint plugins registered to THIS session with their docstrings.")
def list_constraints() -> Dict[str, Any]:
    session = _session()
    plugins = []
    for name, source in session.constraints.items():
        try:
            doc = ast.get_docstring(ast.parse(source)) or ""
        except Exception:
            doc = "(could not parse)"
        plugins.append({"name": name, "docstring": doc.strip()})
    return {"plugins": plugins}


# ── 7. remove_constraint (gated by approval_token) ───────────────────


@mcp.tool(
    description=(
        "Deregister a constraint plugin from THIS session. Requires an "
        "approval_token (Rule 9). Without a valid token, returns the name "
        "that would be removed."
    ),
)
def remove_constraint(
    name: str,
    approval_token: Optional[str] = None,
) -> Dict[str, Any]:
    safe_name = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    session = _session()
    if safe_name not in session.constraints:
        raise FileNotFoundError(f"Constraint '{safe_name}' not registered in this session.")

    if not approval_token or not _consume_approval_token(session.client_id, approval_token):
        return {
            "status": "needs_approval",
            "name": safe_name,
            "message": (
                "Deletion requires user approval. The UI should call "
                "POST /api/approve_constraint, then re-invoke with the token."
            ),
        }

    session.constraints.pop(safe_name, None)
    return {"status": "deleted", "name": safe_name}


# ── 8. analyze_new_file ──────────────────────────────────────────────


@mcp.tool(
    description=(
        "Read a CSV, generate schema info, and compare columns against the "
        "registered datasets to flag semantic conflicts (same column name, "
        "different dtype / range / value vocabulary)."
    ),
)
def analyze_new_file(file_path: str) -> Dict[str, Any]:
    fp = _validate_user_csv_path(file_path)
    try:
        df = pd.read_csv(fp)
    except Exception:
        raise ValueError(f"Unable to parse CSV file: {fp.name}")
    schema = []
    for col in df.columns:
        info: Dict[str, Any] = {
            "column": col,
            "dtype": str(df[col].dtype),
            "nulls": int(df[col].isna().sum()),
            "unique": int(df[col].nunique()),
        }
        if pd.api.types.is_numeric_dtype(df[col]):
            if not df[col].isna().all():
                info["mean"] = round(float(df[col].mean()), 4)
                info["std"] = round(float(df[col].std()), 4)
                info["min"] = float(df[col].min())
                info["max"] = float(df[col].max())
        else:
            non_null = df[col].dropna().astype(str)
            if len(non_null):
                info["sample_values"] = non_null.head(5).tolist()
        schema.append(info)

    conflicts = []
    new_cols = set(df.columns)
    for ds_name, ds_info in _iter_datasets():
        ds_path = _resolve_dataset_path(ds_info["path"])
        if not ds_path.exists():
            continue
        try:
            existing = pd.read_csv(ds_path)
        except Exception:
            continue
        for col in new_cols & set(existing.columns):
            new_dt = str(df[col].dtype)
            old_dt = str(existing[col].dtype)
            dtype_mismatch = new_dt != old_dt
            range_conflict = False
            if pd.api.types.is_numeric_dtype(df[col]) and pd.api.types.is_numeric_dtype(existing[col]):
                ns, os_ = df[col].std(), existing[col].std()
                combined = max(ns, os_, 0.001)
                if abs(df[col].mean() - existing[col].mean()) > 3 * combined:
                    range_conflict = True
            value_conflict = False
            if not pd.api.types.is_numeric_dtype(df[col]) and not pd.api.types.is_numeric_dtype(existing[col]):
                nv = set(df[col].dropna().astype(str).unique()[:100])
                ov = set(existing[col].dropna().astype(str).unique()[:100])
                if nv and ov:
                    overlap = len(nv & ov) / max(len(nv | ov), 1)
                    if overlap < 0.1:
                        value_conflict = True
            if dtype_mismatch or range_conflict or value_conflict:
                reasons = []
                if dtype_mismatch:
                    reasons.append(f"dtype mismatch ({new_dt} vs {old_dt})")
                if range_conflict:
                    reasons.append("numeric range differs significantly")
                if value_conflict:
                    reasons.append("very low value overlap")
                conflicts.append({
                    "column": col,
                    "existing_dataset": ds_name,
                    "reasons": reasons,
                })
    return {
        "file": str(fp),
        "shape": list(df.shape),
        "columns": schema,
        "sample_rows": df.head(3).fillna("").to_dict("records"),
        "semantic_conflicts": conflicts,
        "conflict_count": len(conflicts),
    }


# ── 9. get_solver_code ───────────────────────────────────────────────


@mcp.tool(description="Return the source code of app/engine.py.")
def get_solver_code() -> Dict[str, Any]:
    if not ENGINE_PATH.exists():
        raise FileNotFoundError("engine.py not found.")
    return {"source": ENGINE_PATH.read_text(encoding="utf-8")}


# ── 10. get_allocation_result (cache-only, in-process) ───────────────


@mcp.tool(
    description=(
        "Retrieve the most recent cached allocation result (the one shown in "
        "the UI). Filter by VIN substring or dealer code. Returns "
        "{status: 'no_allocation_yet'} if nothing is cached — that is the "
        "expected response before the user runs an allocation, NOT an error."
    ),
)
def get_allocation_result(vin: str = "", dealer: str = "") -> Dict[str, Any]:
    session = _session()
    data = session.cached()
    if data is None:
        return {
            "status": "no_allocation_yet",
            "message": (
                "No allocation has been run yet. Ask the user to run a weekly "
                "batch or full allocation first, then call this tool."
            ),
        }

    vehicles = data.get("vehicles")
    if vehicles is None:
        raw = data.get("vins") or []
        vehicles = [{
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
        } for v in raw]

    if vin:
        u = vin.upper()
        vehicles = [v for v in vehicles if u in v["vin"].upper()]
    if dealer:
        u = dealer.upper()
        vehicles = [
            v for v in vehicles
            if (v.get("assigned_dealer") or "").upper() == u
            or any((a.get("dealer_code") or "").upper() == u for a in v.get("alternatives", []))
        ]

    return {
        "status": "ok",
        "summary": {
            "scoring_mode": data.get("scoring_mode", "additive"),
            "batch_size": data.get("batch_size"),
            "n_assigned": data.get("n_assigned"),
            "total_alloc_score": data.get("total_alloc_score"),
            "total_distance": data.get("total_distance"),
            "params": data.get("params"),
        },
        "vehicles": vehicles,
    }


# ── 11. analyze_weekly_batch ─────────────────────────────────────────


@mcp.tool(
    description=(
        "Run the weekly ILP on a specific list of VINs and return the KPI "
        "summary. What-if analysis only — does NOT mutate the UI's allocation "
        "cache. If any VIN is unknown, returns a structured error listing the "
        "unknown VINs (engine validates against faas_eligible_vehicles.csv)."
    ),
)
def analyze_weekly_batch(
    vin_list: List[str],
    w_util: Optional[float] = None,
    w_rented: Optional[float] = None,
    w_dist: Optional[float] = None,
    w_tax: Optional[float] = None,
    scoring_mode: str = "additive",
    bucket_mults: Optional[list] = None,
    bucket_signal_field: str = "IN_SERVICE",
) -> Dict[str, Any]:
    if not vin_list:
        raise ValueError("vin_list must be a non-empty list")
    session = _session()
    if scoring_mode == "bucket":
        from bucket_pipeline import solve_weekly_bucket
        r = solve_weekly_bucket(
            n_batch=len(vin_list), vin_list=list(vin_list),
            bucket_mults=bucket_mults,
            signal_field=bucket_signal_field,
            w_dist=w_dist, w_tax=w_tax,
            session=session,
        )
    else:
        r = solve_weekly_batch(
            n_batch=len(vin_list), vin_list=list(vin_list),
            w_util=w_util if w_util is not None else DEFAULT_W_UTIL,
            w_rented=w_rented if w_rented is not None else DEFAULT_W_RENTED,
            w_dist=w_dist if w_dist is not None else DEFAULT_W_DIST,
            w_tax=w_tax if w_tax is not None else DEFAULT_W_TAX,
            session=session,
        )
    if r.get("error"):
        return {"status": "error", "error": r["error"], "n_assigned": 0}

    assigned = [v for v in r["vehicles"] if v["assigned"]]
    out = {
        "status": "ok",
        "scoring_mode": r.get("scoring_mode", scoring_mode),
        "params": r["params"],
        "batch_size": r["batch_size"],
        "n_assigned": r["n_assigned"],
        "rank1_pct": r.get("rank1_pct", 0.0),
        "avg_rank": r.get("avg_rank", 0.0),
        "total_distance_mi": r.get("total_distance", 0),
        "total_tax_usd": round(sum(v["assigned"]["prop_tax"] for v in assigned), 2),
        "avg_rented_at_dest": round(
            sum(v["assigned"]["rented"] for v in assigned) / len(assigned), 2,
        ) if assigned else 0,
        "unique_dealers_used": len({v["assigned"]["dealer_code"] for v in assigned}),
        "top_assignments_sample": [
            {
                "vin": v["vin"],
                "dealer": v["assigned"]["dealer_code"],
                "rank": v["assigned"]["rank"],
                "distance_mi": v["assigned"]["distance"],
                "rented_at_dest": v["assigned"]["rented"],
            }
            for v in assigned[:5]
        ],
    }
    if scoring_mode == "bucket":
        out["avg_in_service_at_dest"] = round(
            sum(v["assigned"].get("in_service", 0) for v in assigned) / len(assigned), 2,
        ) if assigned else 0
    return out


# ── 12. analyze_override_impact (cache-only, in-process) ─────────────


@mcp.tool(
    description=(
        "Simulate moving a single VIN from its currently-assigned dealer to "
        "an alternative dealer WITHOUT making the change. Pure read against "
        "the most recent cached weekly allocation. Returns no_allocation_yet "
        "if no allocation has been run."
    ),
)
def analyze_override_impact(vin: str, alt_dealer_code: str) -> Dict[str, Any]:
    session = _session()
    data = session.cached()
    if data is None:
        return {
            "status": "no_allocation_yet",
            "message": "No allocation cached. Ask user to run an allocation first.",
        }

    vehicles = data.get("vehicles") or []
    target = next((v for v in vehicles if v.get("vin") == vin), None)
    if target is None:
        return {
            "status": "vin_not_in_batch",
            "vin": vin,
            "message": f"VIN {vin!r} not found in the current cached allocation.",
        }

    current = target.get("assigned") or {}
    alts = target.get("alternatives") or []
    candidate = next((a for a in alts if a.get("dealer_code") == alt_dealer_code), None)
    if candidate is None:
        return {
            "status": "infeasible",
            "vin": vin,
            "alt_dealer_code": alt_dealer_code,
            "feasible_alternatives": [a["dealer_code"] for a in alts[:10]],
        }

    alt_assigned = sum(
        1 for v in vehicles
        if v.get("vin") != vin
        and (v.get("assigned") or {}).get("dealer_code") == alt_dealer_code
    )
    alt_capacity = candidate.get("remaining_capacity", 0)
    overflow = alt_assigned + 1 > alt_capacity

    cur_score = current.get("alloc_score", 0)
    new_score = candidate.get("alloc_score", 0)
    cur_rank = current.get("rank", 0)
    new_rank = candidate.get("rank", 0)

    report = {
        "status": "ok",
        "feasible": not overflow,
        "vin": vin,
        "current": {
            "dealer": current.get("dealer_code"),
            "rank": cur_rank,
            "alloc_score": round(cur_score, 4),
            "distance_mi": current.get("distance"),
            "rented_at_dest": current.get("rented"),
            "utilization_pct": current.get("utilization"),
        },
        "proposed": {
            "dealer": alt_dealer_code,
            "rank": new_rank,
            "alloc_score": round(new_score, 4),
            "distance_mi": candidate.get("distance"),
            "rented_at_dest": candidate.get("rented"),
            "utilization_pct": candidate.get("utilization"),
        },
        "delta": {
            "rank_change": new_rank - cur_rank,
            "alloc_score_change": round(new_score - cur_score, 4),
            "distance_change_mi": (candidate.get("distance", 0) - current.get("distance", 0)),
        },
        "capacity": {
            "alt_dealer_remaining_before": alt_capacity,
            "alt_dealer_already_assigned_in_batch": alt_assigned,
            "would_overflow": overflow,
        },
    }
    if overflow:
        report["warning"] = (
            f"{alt_dealer_code} is at or above capacity in the current batch; "
            f"moving {vin} there would force at least one other vehicle out."
        )
    return report


# ── 13. compare_ilp_vs_greedy ────────────────────────────────────────


def _kpis(r: Dict[str, Any]) -> Dict[str, Any]:
    """Compact KPI extractor that handles both success and early-return shapes."""
    assigned = [v for v in r.get("vehicles", []) if v.get("assigned")]
    return {
        "n_assigned": r.get("n_assigned", 0),
        "rank1_pct": r.get("rank1_pct", 0.0),
        "avg_rank": r.get("avg_rank", 0.0),
        "total_distance_mi": r.get("total_distance", 0),
        "total_tax_usd": round(sum(v["assigned"]["prop_tax"] for v in assigned), 2),
        "avg_rented_at_dest": round(
            sum(v["assigned"]["rented"] for v in assigned) / len(assigned), 2,
        ) if assigned else 0,
        "unique_dealers_used": len({v["assigned"]["dealer_code"] for v in assigned}),
    }


@mcp.tool(
    description=(
        "Run both the ILP optimizer and the distance-only Greedy baseline on "
        "the same VIN list, return a side-by-side KPI comparison with deltas. "
        "ILP scoring is additive by default; pass scoring_mode='bucket' to "
        "use the bucket (max-anchored IN_SERVICE-tier) formula instead. "
        "Greedy is distance-only and ignores scoring_mode."
    ),
)
def compare_ilp_vs_greedy(
    vin_list: List[str],
    w_util: Optional[float] = None,
    w_rented: Optional[float] = None,
    w_dist: Optional[float] = None,
    w_tax: Optional[float] = None,
    scoring_mode: str = "additive",
    bucket_mults: Optional[list] = None,
    bucket_signal_field: str = "IN_SERVICE",
) -> Dict[str, Any]:
    if not vin_list:
        raise ValueError("vin_list must be a non-empty list")
    session = _session()
    weights = dict(
        w_util=w_util if w_util is not None else DEFAULT_W_UTIL,
        w_rented=w_rented if w_rented is not None else DEFAULT_W_RENTED,
        w_dist=w_dist if w_dist is not None else DEFAULT_W_DIST,
        w_tax=w_tax if w_tax is not None else DEFAULT_W_TAX,
    )
    if scoring_mode == "bucket":
        from bucket_pipeline import solve_weekly_bucket
        ilp = solve_weekly_bucket(
            n_batch=len(vin_list), vin_list=list(vin_list),
            bucket_mults=bucket_mults,
            signal_field=bucket_signal_field,
            w_dist=weights["w_dist"], w_tax=weights["w_tax"],
            session=session,
        )
    else:
        ilp = solve_weekly_batch(n_batch=len(vin_list), vin_list=list(vin_list),
                                  session=session, **weights)
    greedy = solve_weekly_greedy(n_batch=len(vin_list), vin_list=list(vin_list),
                                  session=session, **weights)
    if ilp.get("error") or greedy.get("error"):
        return {
            "status": "error",
            "error": ilp.get("error") or greedy.get("error"),
            "ilp_n_assigned": ilp.get("n_assigned", 0),
            "greedy_n_assigned": greedy.get("n_assigned", 0),
        }
    ilp_k = _kpis(ilp)
    gr_k = _kpis(greedy)
    delta = {
        k: round(ilp_k[k] - gr_k[k], 2)
        for k in ilp_k
        if isinstance(ilp_k[k], (int, float))
    }
    return {
        "status": "ok",
        "scoring_mode": scoring_mode,
        "batch_size": len(vin_list),
        "weights": weights if scoring_mode == "additive" else {
            "bucket_mults": list(bucket_mults) if bucket_mults else None,
            "signal_field": bucket_signal_field,
            "w_dist": weights["w_dist"], "w_tax": weights["w_tax"],
        },
        "ilp": ilp_k,
        "greedy": gr_k,
        "delta_ilp_minus_greedy": delta,
    }


# ── 14. analyze_capacity_change ──────────────────────────────────────


@mcp.tool(
    description=(
        "Temporarily perturb one dealer's REMAINING_CAPACITY in memory, rerun "
        "the weekly ILP on a VIN list, and report the delta vs. baseline. "
        "Reverts on every exit path (success or failure). dealer_inventory.csv "
        "is NEVER written."
    ),
)
def analyze_capacity_change(
    dealer_code: str, new_capacity: int, vin_list: List[str],
) -> Dict[str, Any]:
    if not vin_list:
        raise ValueError("vin_list must be a non-empty list")
    session = _session()
    # The perturbation is passed as a per-call dealer override that
    # solve_weekly_batch applies to a private copy — the shared baseline is
    # never written, so concurrent sessions never see this dealer's capacity
    # change and no lock is required for correctness.
    dealer_df = load_baseline()["dealer"]
    row_idx = dealer_df.index[dealer_df["DEALER_CODE"] == dealer_code]
    if len(row_idx) == 0:
        raise ValueError(f"Dealer {dealer_code!r} not found.")
    orig = int(dealer_df.at[row_idx[0], "REMAINING_CAPACITY"])

    baseline = solve_weekly_batch(n_batch=len(vin_list), vin_list=list(vin_list),
                                   session=session)
    if baseline.get("error"):
        return {"status": "error", "error": baseline["error"]}

    perturbed = solve_weekly_batch(
        n_batch=len(vin_list), vin_list=list(vin_list), session=session,
        dealer_capacity_overrides={dealer_code: int(new_capacity)},
    )

    if perturbed.get("error"):
        return {"status": "error", "error": perturbed["error"]}

    base_k = _kpis(baseline)
    pert_k = _kpis(perturbed)
    # Per-dealer assignment counts
    def _count_to(r, code):
        return sum(
            1 for v in r.get("vehicles", [])
            if v.get("assigned") and v["assigned"]["dealer_code"] == code
        )
    base_k[f"cars_to_{dealer_code}"] = _count_to(baseline, dealer_code)
    pert_k[f"cars_to_{dealer_code}"] = _count_to(perturbed, dealer_code)
    delta = {k: round(pert_k[k] - base_k[k], 2) for k in base_k}
    return {
        "status": "ok",
        "dealer_code": dealer_code,
        "original_capacity": orig,
        "new_capacity": int(new_capacity),
        "batch_size": len(vin_list),
        "baseline": base_k,
        "perturbed": pert_k,
        "delta_perturbed_minus_baseline": delta,
        "note": "In-memory perturbation only; dealer_inventory.csv unchanged.",
    }


# ── ASGI app for mounting on FastAPI ─────────────────────────────────

# Cache the app so the session manager is created exactly once. Subsequent
# calls return the same instance — both server.py's mount and its lifespan
# wrapper need to reference the same session manager.
_asgi_app = None


def _strip_session_path(inner_app):
    """ASGI middleware that stashes the FaaS session id in a contextvar.

    The agent connects to the real FastMCP transport path, `/mcp/`, and includes
    the app session as `?faas_session_id=<client_id>`. The older
    `/mcp/<session_id>/...` path shape is kept for compatibility, but query
    params are the preferred route because some streamable-HTTP clients
    treat path suffixes as transport paths.
    """

    async def _reject_403(send, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})

    async def app(scope, receive, send):
        if scope["type"] == "http":
            # Gate the whole MCP surface on the internal key. The agent
            # subprocess sends it as `X-Internal-Key`; anything else (a stray
            # curl, another local process, a leaked URL) is refused.
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            supplied = headers.get(b"x-internal-key", b"").decode("utf-8", "ignore")
            if not secrets.compare_digest(supplied, _INTERNAL_KEY):
                await _reject_403(send, "Missing or invalid X-Internal-Key.")
                return
            query = parse_qs((scope.get("query_string") or b"").decode("utf-8"))
            query_sid = (query.get("faas_session_id") or [""])[0]
            if query_sid and _SESSION_ID_RE.match(query_sid):
                token = _current_session_id.set(query_sid)
                try:
                    await inner_app(scope, receive, send)
                finally:
                    _current_session_id.reset(token)
                return

            path = scope.get("path", "")
            head, _slash, tail = path.lstrip("/").partition("/")
            # Only strip when there's an actual `/<head>/...` shape AND
            # the head looks like a session id — otherwise the MCP
            # transport's own path segments (`/messages`, `/sse`) get
            # misinterpreted as session ids.
            if head and _slash and _SESSION_ID_RE.match(head):
                token = _current_session_id.set(head)
                new_scope = dict(scope)
                new_scope["path"] = "/" + tail
                # `raw_path` is the bytes path that some ASGI clients
                # consult — keep it consistent with `path`.
                if "raw_path" in scope:
                    new_scope["raw_path"] = new_scope["path"].encode("utf-8")
                try:
                    await inner_app(new_scope, receive, send)
                finally:
                    _current_session_id.reset(token)
                return
        await inner_app(scope, receive, send)

    return app


def get_asgi_app():
    """Return the streamable-http ASGI app for mounting on FastAPI."""
    global _asgi_app
    if _asgi_app is None:
        _asgi_app = _strip_session_path(mcp.streamable_http_app())
    return _asgi_app
