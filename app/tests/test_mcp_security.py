"""Security regression tests for MCP sandbox helpers."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine
import mcp_server
from state import SessionStore


def test_query_data_blocks_forbidden_pd_readers():
    with pytest.raises(ValueError, match="Disallowed pd"):
        mcp_server.query_data("pd.read_json('app/data_csv/fleet_inventory.csv')")


def test_query_data_blocks_forbidden_df_exports():
    with pytest.raises(ValueError, match="Disallowed DataFrame method call"):
        mcp_server.query_data("faas_eligible_vehicles.to_csv('/tmp/output.csv')")


def test_query_data_blocks_builtin_file_ops():
    with pytest.raises(ValueError, match="Disallowed builtin call: open"):
        mcp_server.query_data("open('app/server.py').read()")


# ── query_data: dunder / private attribute + dangerous callable blocks ──

def test_query_data_blocks_dunder_attribute_on_dataframe():
    with pytest.raises(ValueError, match="attribute"):
        mcp_server.query_data("faas_eligible_vehicles.__class__")


def test_query_data_blocks_subclasses_escape():
    with pytest.raises(ValueError, match="attribute"):
        mcp_server.query_data("().__class__.__bases__")


def test_query_data_blocks_getattr():
    with pytest.raises(ValueError, match="Disallowed builtin call: getattr"):
        mcp_server.query_data("getattr(pd, 'read_json')('x')")


def test_query_data_blocks_globals():
    with pytest.raises(ValueError, match="Disallowed builtin call: globals"):
        mcp_server.query_data("globals()")


def test_query_data_blocks_dunder_import_name():
    with pytest.raises(ValueError):
        mcp_server.query_data("__import__('os')")


def test_query_data_blocks_imports():
    with pytest.raises(ValueError, match="imports"):
        mcp_server.query_data("import os")


def test_query_data_still_runs_legitimate_query():
    out = mcp_server.query_data("len(faas_eligible_vehicles)")
    assert "result" in out
    assert isinstance(out["result"], int) and out["result"] > 0


# ── analyze_capacity_change: shared baseline must stay immutable ────────

_CAP_SESSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_CAP_SESSION_B_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _some_grounded_vins(n=6):
    fleet = engine.load_baseline_fleet()
    grounded = fleet[fleet["STATUS"] == "Grounded"]["VIN"].tolist()
    return grounded[:n] or fleet["VIN"].tolist()[:n]


def test_analyze_capacity_change_does_not_mutate_baseline():
    store = SessionStore(baseline_fleet_df=engine.load_baseline_fleet())
    mcp_server.set_store(store)
    dealer_df = engine.load_baseline()["dealer"]
    code = dealer_df.iloc[0]["DEALER_CODE"]
    before = int(dealer_df.loc[dealer_df["DEALER_CODE"] == code, "REMAINING_CAPACITY"].iloc[0])

    tok = mcp_server._current_session_id.set(_CAP_SESSION_ID)
    try:
        result = mcp_server.analyze_capacity_change(
            dealer_code=code, new_capacity=before + 25, vin_list=_some_grounded_vins(),
        )
    finally:
        mcp_server._current_session_id.reset(tok)

    assert result["status"] == "ok"
    after = int(
        engine.load_baseline()["dealer"]
        .loc[dealer_df["DEALER_CODE"] == code, "REMAINING_CAPACITY"].iloc[0]
    )
    assert after == before, "shared baseline REMAINING_CAPACITY changed"


def test_analyze_capacity_change_baseline_stable_under_concurrency():
    store = SessionStore(baseline_fleet_df=engine.load_baseline_fleet())
    mcp_server.set_store(store)
    dealer_df = engine.load_baseline()["dealer"]
    code = dealer_df.iloc[0]["DEALER_CODE"]
    before = int(dealer_df.loc[dealer_df["DEALER_CODE"] == code, "REMAINING_CAPACITY"].iloc[0])
    vins = _some_grounded_vins()
    errors = []

    def _run(session_id, new_cap):
        tok = mcp_server._current_session_id.set(session_id)
        try:
            r = mcp_server.analyze_capacity_change(
                dealer_code=code, new_capacity=new_cap, vin_list=vins,
            )
            if r.get("status") != "ok":
                errors.append(r)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            mcp_server._current_session_id.reset(tok)

    t1 = threading.Thread(target=_run, args=(_CAP_SESSION_ID, before + 40))
    t2 = threading.Thread(target=_run, args=(_CAP_SESSION_B_ID, 0))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert not errors, errors
    after = int(
        engine.load_baseline()["dealer"]
        .loc[dealer_df["DEALER_CODE"] == code, "REMAINING_CAPACITY"].iloc[0]
    )
    assert after == before


def test_analyze_new_file_blocks_outside_project_path(tmp_path):
    outside = tmp_path / "outside.csv"
    outside.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="inside the repository directory"):
        mcp_server.analyze_new_file(str(outside))


def test_analyze_new_file_allows_only_csv():
    # Create a repository-local non-CSV file so the code reaches the suffix
    # guard before the repository-boundary guard.
    csv_like = mcp_server.PROJECT_DIR / "tmp-not-csv-for-test.txt"
    csv_like.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="Only CSV files are supported"):
        try:
            mcp_server.analyze_new_file(str(csv_like))
        finally:
            if csv_like.exists():
                csv_like.unlink()
