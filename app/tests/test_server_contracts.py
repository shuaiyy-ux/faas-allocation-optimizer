"""
Contract tests for app/server.py — FastAPI endpoint shape verification.

Uses FastAPI TestClient (backed by httpx). All engine calls are patched at
server module level so tests run without invoking the ILP solver or reading
CSV files. These are contract tests, not end-to-end integration tests.

Endpoint coverage
-----------------
  GET  /                       → serves dashboard HTML
  GET  /api/defaults           → canonical scoring weight defaults
  GET  /api/chat_status        → current per-session chat budget
  GET  /api/overview           → dealer network overview
  POST /api/allocate           → Greedy + V2 ILP comparison
  GET  /api/fleet              → fleet inventory
  POST /api/weekly             → weekly ILP batch
  POST /api/weekly_greedy      → weekly greedy batch
  POST /api/confirm            → confirm allocation to session snapshot
  POST /api/reset              → restore fleet backup
  GET  /api/allocation_cache   → cached allocation (state-based)
  GET  /api/export/confirmed   → future confirmed-allocation export stub
  POST /api/approve_constraint → mint single-use approval token
  POST /api/chat               → SSE event stream (smoke)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# app/tests/this.py -> parent = app/tests -> parent.parent = app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
import server                                # noqa: E402
import state                                 # noqa: E402


# ── Shared mock payloads ──────────────────────────────────────────────

# solve_both return value — additive-scoring schema
MOCK_ALLOCATE = {
    "params": {
        "n_vehicles": 50,
        "w_util": 1.335, "w_rented": 0.0574,
        "miles_per_util": 289.79, "dollars_per_util": 506.94,
        "w_dist": 15.0, "w_tax": 1.95,
        "distance_norm": 4346.8, "tax_norm": 988.53,
        "source_limit": None, "expected_stay_months": 12,
    },
    "greedy": {
        "assigned": 49, "util_score": 92.28, "distance": 9324.3,
        "tax": 10499.34, "net": 39.39,
    },
    "v2": {
        "assigned": 49, "util_score": 135.83, "distance": 15594.8,
        "tax": 4874.69, "net": 72.39,
    },
    "delta": 33.0, "delta_pct": 83.8,
    "dealers_v2": [], "dealers_greedy": [], "arcs": [], "vins": [],
    "sankey": {"labels": [], "sources": [], "targets": [], "values": [],
               "link_colors": [], "node_colors": []},
}

MOCK_FLEET = {
    "vehicles": [
        {
            "vin": "VIN_XXXXXXXXXX001",
            "year": 2023,
            "make": "HYUNDAI",
            "model": "IONIQ 5 SEL",
            "source": "D001",
            "source_city": "Los Angeles",
            "source_state": "CA",
            "residual": 22500.0,
            "status": "Grounded",
            "week": "2025-12-15",
        }
    ],
    "weeks": ["2025-12-15"],
    "counts": {"total": 1, "incoming": 0, "grounded": 1, "transporting": 0, "delivered": 0},
}

MOCK_OVERVIEW = {
    "total_vehicles": 650,
    "total_dealers": 1,
    "total_capacity": 201,
    "dealers": [
        {
            "DEALER_CODE": "FD01", "DEALER_NAME": "FaaS_Dealer_Alpha",
            "STATE": "CA", "TRUE_CAPACITY": 15, "DELIVERED_COUNT": 7,
            "IN_TRANSIT_COUNT": 0, "REMAINING_CAPACITY": 8,
            "UTIL_RATE": 0.85, "IN_SERVICE": 42, "RENTED": 36,
            "PROP_TAX_RATE": 0.0, "LATITUDE": 34.05, "LONGITUDE": -118.24,
        }
    ],
    "sources": [
        {"SOURCE": "D001", "CITY": "Los Angeles", "STATE": "CA",
         "SOURCE_LAT": 34.05, "SOURCE_LON": -118.24}
    ],
}

MOCK_WEEKLY = {
    "batch_size": 10,
    "seed": 42,
    "n_assigned": 9,
    "total_alloc_score": 15.23,
    "total_distance": 3200.0,
    "ceiling": 18.5,
    "quality_pct": 82.3,
    "avg_rank": 1.2,
    "rank1_count": 7,
    "rank1_pct": 77.8,
    "params": {
        "w_util": 1.335, "w_rented": 0.0574,
        "miles_per_util": 289.79, "dollars_per_util": 506.94,
        "w_dist": 15.0, "w_tax": 1.95,
        "distance_norm": 4346.8, "tax_norm": 988.53,
        "source_limit": None, "expected_stay_months": 12,
    },
    "vehicles": [
        {
            "vin": "VIN_XXXXXXXXXX001",
            "source": "D001",
            "source_city": "Los Angeles",
            "source_state": "CA",
            "source_lat": 34.05,
            "source_lon": -118.24,
            "residual": 22500.0,
            "assigned": {
                "dealer_code": "FD01", "dealer_name": "FaaS_Dealer_Alpha",
                "state": "CA", "lat": 34.05, "lon": -118.24,
                "distance": 228.5, "prop_tax": 402.75,
                "prop_tax_rate": 1.79, "utilization_score": 3.57,
                "alloc_score": 1.25, "utilization": 85.0,
                "rented": 36, "remaining_capacity": 8, "rank": 1,
            },
            "alternatives": [],
            "reasoning": "Rank 1 of 5 feasible dealers.",
            "assigned_dealer": "FD01",
        }
    ],
    "method": "ilp",
}

MOCK_WEEKLY_GREEDY = {**MOCK_WEEKLY, "method": "greedy"}
MOCK_CONFIRM = {"updated": 1, "skipped": 0, "total": 1}
MOCK_RESET   = {"status": "ok"}


TEST_SESSION_ID = "47d8e1f4-ef27-4d4c-8f8d-4b2c8b8a9c8a"
CHAT_STATUS_FRESH_SESSION_ID = "11111111-1111-4111-8111-111111111111"
CHAT_STATUS_DISABLED_SESSION_ID = "22222222-2222-4222-8222-222222222222"
EXPORT_EMPTY_SESSION_ID = "33333333-3333-4333-8333-333333333333"
EXPORT_CONFIRMED_SESSION_ID = "44444444-4444-4444-8444-444444444444"
CONFIRM_SESSION_ID = "55555555-5555-4555-8555-555555555555"
CHAT_SESSION_ID = "66666666-6666-4666-8666-666666666666"
MCP_SESSION_A_ID = "77777777-7777-4777-8777-777777777777"
MCP_SESSION_B_ID = "88888888-8888-4888-8888-888888888888"


@pytest.fixture(scope="module")
def client():
    """TestClient wrapping the FastAPI app — no live server needed.

    Skips the MCP lifespan and manually attaches a `SessionStore` to
    `app.state.store` so the per-session middleware can resolve sessions
    without spinning up the real MCP session manager. Every request
    automatically gets `X-Session-Id` set to `TEST_SESSION_ID` so the tests don't
    need to thread the header through every call site.
    """
    import engine
    from state import SessionStore
    baseline_fleet = engine.load_baseline_fleet()
    server.app.state.store = SessionStore(baseline_fleet_df=baseline_fleet)
    return TestClient(
        server.app,
        raise_server_exceptions=True,
        headers={"X-Session-Id": TEST_SESSION_ID},
    )


# ── GET / ─────────────────────────────────────────────────────────────

class TestRootEndpoint:
    def test_returns_200(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_content_type_is_html(self, client):
        response = client.get("/")
        assert "text/html" in response.headers["content-type"]

    def test_body_is_not_empty(self, client):
        response = client.get("/")
        assert len(response.content) > 0


# ── GET /api/defaults ─────────────────────────────────────────────────

class TestDefaultsEndpoint:
    def test_returns_200(self, client):
        response = client.get("/api/defaults")
        assert response.status_code == 200

    def test_response_has_all_four_weights(self, client):
        data = client.get("/api/defaults").json()
        for field in ("w_util", "w_rented", "w_dist", "w_tax", "expected_stay_months"):
            assert field in data, f"defaults missing '{field}'"

    def test_defaults_match_engine_constants(self, client):
        from engine import (
            DEFAULT_W_UTIL, DEFAULT_W_RENTED, DEFAULT_W_DIST, DEFAULT_W_TAX,
        )
        data = client.get("/api/defaults").json()
        assert data["w_util"]   == DEFAULT_W_UTIL
        assert data["w_rented"] == DEFAULT_W_RENTED
        assert data["w_dist"]   == DEFAULT_W_DIST
        assert data["w_tax"]    == DEFAULT_W_TAX


# ── GET /api/chat_status ──────────────────────────────────────────────

class TestChatStatusEndpoint:
    def test_returns_current_budget(self, client):
        data = client.get(
            "/api/chat_status",
            headers={"X-Session-Id": CHAT_STATUS_FRESH_SESSION_ID},
        ).json()
        assert set(data) == {"limit", "remaining", "disabled"}
        assert data["limit"] == server.CHAT_LIMIT_PER_SESSION
        assert data["remaining"] == server.CHAT_LIMIT_PER_SESSION
        assert data["disabled"] is (server.CHAT_LIMIT_PER_SESSION <= 0)

    def test_zero_limit_disables_chat(self, client, monkeypatch):
        monkeypatch.setattr(server, "CHAT_LIMIT_PER_SESSION", 0)
        data = client.get(
            "/api/chat_status",
            headers={"X-Session-Id": CHAT_STATUS_DISABLED_SESSION_ID},
        ).json()
        assert data == {"limit": 0, "remaining": 0, "disabled": True}


# ── GET /openapi.json ─────────────────────────────────────────────────

class TestOpenAPIContract:
    EXPECTED_PATHS = {
        "/api/defaults",
        "/api/sessions",
        "/api/overview",
        "/api/fleet",
        "/api/allocate",
        "/api/weekly",
        "/api/weekly_greedy",
        "/api/confirm",
        "/api/reset",
        "/api/home",
        "/api/watchlist/refresh",
        "/api/allocation_cache",
        "/api/export/confirmed",
        "/api/approve_constraint",
        "/api/chat_status",
        "/api/chat",
    }

    def test_openapi_schema_generates(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        assert data["info"]["title"] == "FaaS Allocator"
        assert "paths" in data

    def test_openapi_contains_all_rest_api_paths(self, client):
        paths = set(client.get("/openapi.json").json()["paths"])
        missing = self.EXPECTED_PATHS - paths
        assert not missing, "OpenAPI missing paths: {}".format(sorted(missing))

    def test_session_scoped_paths_document_session_header(self, client):
        schema = client.get("/openapi.json").json()
        documented_headers = {}
        for path, methods in schema["paths"].items():
            for method, spec in methods.items():
                params = spec.get("parameters", [])
                has_header = any(
                    p.get("name") == "X-Session-Id" and p.get("in") == "header"
                    for p in params
                )
                documented_headers[(method.upper(), path)] = has_header

        post_paths = {
            "/api/allocate", "/api/weekly", "/api/weekly_greedy", "/api/confirm",
            "/api/reset", "/api/watchlist/refresh", "/api/approve_constraint",
            "/api/chat",
        }
        for path in (
            "/api/overview", "/api/fleet", "/api/allocate", "/api/weekly",
            "/api/weekly_greedy", "/api/confirm", "/api/reset", "/api/home",
            "/api/watchlist/refresh", "/api/sessions", "/api/allocation_cache", "/api/export/confirmed",
            "/api/approve_constraint", "/api/chat_status", "/api/chat",
        ):
            assert documented_headers[("GET" if path not in post_paths else "POST", path)]


class TestSessionsEndpoint:
    def test_sessions_requires_session_header(self, client):
        plain_client = TestClient(server.app)
        response = plain_client.get("/api/sessions")
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "Missing X-Session-Id header — generate a UUID per browser tab and send it on every request."
        )

    def test_rejects_invalid_session_header(self, client):
        response = client.get(
            "/api/overview",
            headers={"X-Session-Id": "not-a-uuid"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "Invalid X-Session-Id format. Send a stable UUID-like string."
        )

    def test_sessions_returns_only_current_session(self, client):
        store = client.app.state.store
        store.get_or_create(EXPORT_CONFIRMED_SESSION_ID)
        response = client.get("/api/sessions")
        sessions = response.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["client_id"] == TEST_SESSION_ID


class TestWatchlistEndpoint:
    def test_refresh_requires_session_header(self, client):
        plain_client = TestClient(server.app)
        response = plain_client.post("/api/watchlist/refresh")
        assert response.status_code == 400
        assert response.json()["detail"] == (
            "Missing X-Session-Id header — generate a UUID per browser tab and send it on every request."
        )


# ── POST /api/allocate ────────────────────────────────────────────────

class TestAllocateEndpoint:
    def test_returns_200(self, client):
        with patch("server.solve_both", return_value=MOCK_ALLOCATE):
            response = client.post("/api/allocate", json={})
        assert response.status_code == 200

    def test_response_contains_greedy_and_v2(self, client):
        with patch("server.solve_both", return_value=MOCK_ALLOCATE):
            data = client.post("/api/allocate", json={}).json()
        assert "greedy" in data
        assert "v2" in data

    def test_greedy_block_has_required_fields(self, client):
        with patch("server.solve_both", return_value=MOCK_ALLOCATE):
            data = client.post("/api/allocate", json={}).json()
        for field in ("assigned", "util_score", "distance", "tax", "net"):
            assert field in data["greedy"], f"greedy missing '{field}'"

    def test_v2_block_has_required_fields(self, client):
        with patch("server.solve_both", return_value=MOCK_ALLOCATE):
            data = client.post("/api/allocate", json={}).json()
        for field in ("assigned", "util_score", "distance", "tax", "net"):
            assert field in data["v2"], f"v2 missing '{field}'"

    def test_delta_field_present(self, client):
        with patch("server.solve_both", return_value=MOCK_ALLOCATE):
            data = client.post("/api/allocate", json={}).json()
        assert "delta" in data and "delta_pct" in data

    def test_params_block_has_weight_fields(self, client):
        with patch("server.solve_both", return_value=MOCK_ALLOCATE):
            data = client.post("/api/allocate", json={}).json()
        assert "params" in data
        for field in ("w_util", "w_rented", "w_dist", "w_tax"):
            assert field in data["params"], f"params missing '{field}'"

    def test_rejects_non_positive_vehicle_count(self, client):
        response = client.post("/api/allocate", json={"n_vehicles": 0})
        assert response.status_code == 422

    def test_rejects_non_positive_expected_stay_months(self, client):
        response = client.post("/api/allocate", json={"expected_stay_months": 0})
        assert response.status_code == 422

    def test_custom_weights_forwarded_to_engine(self, client):
        mock_fn = MagicMock(return_value=MOCK_ALLOCATE)
        with patch("server.solve_both", mock_fn):
            client.post(
                "/api/allocate",
                json={"n_vehicles": 50, "w_util": 2.0, "w_rented": 0.1,
                      "w_dist": 10.0, "w_tax": 1.5},
            )
        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["w_util"] == 2.0
        assert kwargs["w_rented"] == 0.1

    def test_default_weights_applied_when_omitted(self, client):
        from engine import DEFAULT_W_UTIL, DEFAULT_W_RENTED
        mock_fn = MagicMock(return_value=MOCK_ALLOCATE)
        with patch("server.solve_both", mock_fn):
            client.post("/api/allocate", json={})
        _, kwargs = mock_fn.call_args
        assert kwargs["w_util"]   == DEFAULT_W_UTIL
        assert kwargs["w_rented"] == DEFAULT_W_RENTED


# ── GET /api/fleet ────────────────────────────────────────────────────

class TestFleetEndpoint:
    def test_returns_200(self, client):
        with patch("server.get_fleet_inventory", return_value=MOCK_FLEET):
            response = client.get("/api/fleet")
        assert response.status_code == 200

    def test_response_contains_vehicles_key(self, client):
        with patch("server.get_fleet_inventory", return_value=MOCK_FLEET):
            data = client.get("/api/fleet").json()
        assert "vehicles" in data and isinstance(data["vehicles"], list)

    def test_response_contains_counts(self, client):
        with patch("server.get_fleet_inventory", return_value=MOCK_FLEET):
            data = client.get("/api/fleet").json()
        for field in ("total", "grounded", "transporting"):
            assert field in data["counts"], f"counts missing '{field}'"

    def test_response_contains_weeks(self, client):
        with patch("server.get_fleet_inventory", return_value=MOCK_FLEET):
            data = client.get("/api/fleet").json()
        assert "weeks" in data and isinstance(data["weeks"], list)

    def test_vehicle_has_required_fields(self, client):
        with patch("server.get_fleet_inventory", return_value=MOCK_FLEET):
            data = client.get("/api/fleet").json()
        vehicle = data["vehicles"][0]
        for field in ("vin", "make", "model", "source", "status", "residual"):
            assert field in vehicle, f"vehicle missing '{field}'"


# ── GET /api/overview ─────────────────────────────────────────────────

class TestOverviewEndpoint:
    def test_returns_200(self, client):
        with patch("server.get_overview", return_value=MOCK_OVERVIEW):
            response = client.get("/api/overview")
        assert response.status_code == 200

    def test_response_contains_dealers_list(self, client):
        with patch("server.get_overview", return_value=MOCK_OVERVIEW):
            data = client.get("/api/overview").json()
        assert "dealers" in data and len(data["dealers"]) > 0

    def test_total_dealers_matches_list_length(self, client):
        with patch("server.get_overview", return_value=MOCK_OVERVIEW):
            data = client.get("/api/overview").json()
        assert data["total_dealers"] == len(data["dealers"])

    def test_dealer_has_required_fields(self, client):
        with patch("server.get_overview", return_value=MOCK_OVERVIEW):
            data = client.get("/api/overview").json()
        dealer = data["dealers"][0]
        for field in ("DEALER_CODE", "DEALER_NAME", "STATE", "REMAINING_CAPACITY", "RENTED"):
            assert field in dealer, f"dealer missing '{field}'"

    def test_response_contains_sources(self, client):
        with patch("server.get_overview", return_value=MOCK_OVERVIEW):
            data = client.get("/api/overview").json()
        assert "sources" in data and isinstance(data["sources"], list)


# ── POST /api/weekly ──────────────────────────────────────────────────

class TestWeeklyEndpoint:
    def test_returns_200(self, client):
        with patch("server.solve_weekly_batch", return_value=MOCK_WEEKLY):
            response = client.post("/api/weekly", json={})
        assert response.status_code == 200

    def test_response_has_batch_fields(self, client):
        with patch("server.solve_weekly_batch", return_value=MOCK_WEEKLY):
            data = client.post("/api/weekly", json={}).json()
        for field in ("batch_size", "n_assigned", "total_alloc_score",
                      "total_distance", "avg_rank", "rank1_pct", "vehicles"):
            assert field in data, f"weekly response missing '{field}'"

    def test_vehicle_assigned_block_has_rank_and_score(self, client):
        with patch("server.solve_weekly_batch", return_value=MOCK_WEEKLY):
            data = client.post("/api/weekly", json={}).json()
        assigned = data["vehicles"][0]["assigned"]
        assert "alloc_score" in assigned
        assert "rank" in assigned

    def test_custom_weights_forwarded(self, client):
        mock_fn = MagicMock(return_value=MOCK_WEEKLY)
        with patch("server.solve_weekly_batch", mock_fn):
            client.post("/api/weekly", json={"w_util": 2.0, "n_batch": 5})
        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs["w_util"]  == 2.0
        assert kwargs["n_batch"] == 5

    def test_rejects_non_positive_batch_size(self, client):
        response = client.post("/api/weekly", json={"n_batch": 0})
        assert response.status_code == 422


# ── POST /api/weekly  (scoring_mode = "bucket") ───────────────────────

MOCK_WEEKLY_BUCKET = {
    "batch_size": 10, "seed": None, "n_assigned": 10,
    "total_alloc_score": 14.5, "total_distance": 800.0,
    "ceiling": 18.0, "quality_pct": 80.6,
    "avg_rank": 1.2, "rank1_count": 8, "rank1_pct": 80.0,
    "scoring_mode": "bucket",
    "params": {
        "bucket_mults": [3.7203, 2.8848, 0.7957, 0.6498],
        "signal_field": "IN_SERVICE",
        "w_dist": 15.0, "w_tax": 1.95,
        "distance_norm": 4346.8, "tax_norm": 988.53,
        "expected_stay_months": 12,
    },
    "vehicles": [
        {
            "vin": "VIN_XXXXXXXXXX001",
            "source": "D001",
            "source_city": "Los Angeles",
            "source_state": "CA",
            "source_lat": 34.05,
            "source_lon": -118.24,
            "residual": 22500.0,
            "assigned": {
                "dealer_code": "FD01", "dealer_name": "FaaS_Dealer_Alpha",
                "state": "CA", "lat": 34.05, "lon": -118.24,
                "distance": 228.5, "prop_tax": 402.75,
                "prop_tax_rate": 1.79, "utilization_score": 3.7203,
                "alloc_score": 2.85, "utilization": 85.0,
                "rented": 36, "in_service": 42,
                "remaining_capacity": 8, "rank": 1,
            },
            "alternatives": [],
            "reasoning": "Rank 1 of 5 feasible dealers — Tier A on IN_SERVICE.",
            "assigned_dealer": "FD01",
        }
    ],
    "method": "bucket_ilp",
}


class TestWeeklyEndpointBucketMode:
    def test_bucket_mode_returns_200(self, client):
        with patch("server.solve_weekly_bucket", return_value=MOCK_WEEKLY_BUCKET):
            response = client.post("/api/weekly", json={"scoring_mode": "bucket"})
        assert response.status_code == 200

    def test_bucket_mode_dispatches_to_bucket_pipeline(self, client):
        """Verify scoring_mode=bucket routes to solve_weekly_bucket, not solve_weekly_batch."""
        bucket_fn = MagicMock(return_value=MOCK_WEEKLY_BUCKET)
        additive_fn = MagicMock()
        with patch("server.solve_weekly_bucket", bucket_fn), \
             patch("server.solve_weekly_batch", additive_fn):
            client.post("/api/weekly", json={"scoring_mode": "bucket", "n_batch": 7})
        bucket_fn.assert_called_once()
        additive_fn.assert_not_called()
        _, kwargs = bucket_fn.call_args
        assert kwargs["n_batch"] == 7
        assert kwargs["signal_field"] == "IN_SERVICE"

    def test_bucket_mode_response_has_scoring_mode_tag(self, client):
        with patch("server.solve_weekly_bucket", return_value=MOCK_WEEKLY_BUCKET):
            data = client.post("/api/weekly", json={"scoring_mode": "bucket"}).json()
        assert data["scoring_mode"] == "bucket"
        assert "bucket_mults" in data["params"]
        assert data["method"] == "bucket_ilp"

    def test_bucket_mults_forwarded_when_provided(self, client):
        mock_fn = MagicMock(return_value=MOCK_WEEKLY_BUCKET)
        with patch("server.solve_weekly_bucket", mock_fn):
            client.post("/api/weekly", json={
                "scoring_mode": "bucket",
                "bucket_mults": [3.0, 2.0, 1.0, 0.5],
            })
        _, kwargs = mock_fn.call_args
        assert kwargs["bucket_mults"] == [3.0, 2.0, 1.0, 0.5]

    def test_additive_path_default_when_mode_omitted(self, client):
        """Backward compat: omitting scoring_mode hits the existing additive path."""
        additive_fn = MagicMock(return_value=MOCK_WEEKLY)
        bucket_fn = MagicMock()
        with patch("server.solve_weekly_batch", additive_fn), \
             patch("server.solve_weekly_bucket", bucket_fn):
            client.post("/api/weekly", json={"n_batch": 5})
        additive_fn.assert_called_once()
        bucket_fn.assert_not_called()


# ── POST /api/allocate  (scoring_mode = "bucket") ─────────────────────

MOCK_ALLOCATE_BUCKET = {
    "method": "bucket_v2",
    "params": {
        "n_vehicles": 50,
        "bucket_mults": [2.0, 1.4, 1.0, 0.6],
        "signal_field": "IN_SERVICE",
        "w_dist": 15.0, "w_tax": 1.95,
        "distance_norm": 4346.8, "tax_norm": 988.53,
        "expected_stay_months": 12,
    },
    "n_assigned": 49,
    "total_alloc_score": 78.5,
    "total_distance": 15200.0,
    "alloc": [],
}


class TestAllocateEndpointBucketMode:
    def test_bucket_mode_dispatches_to_solve_bucket(self, client):
        bucket_fn = MagicMock(return_value=MOCK_ALLOCATE_BUCKET)
        additive_fn = MagicMock()
        with patch("server.solve_bucket", bucket_fn), \
             patch("server.solve_both", additive_fn):
            response = client.post("/api/allocate", json={"scoring_mode": "bucket"})
        assert response.status_code == 200
        bucket_fn.assert_called_once()
        additive_fn.assert_not_called()

    def test_bucket_mode_response_tagged(self, client):
        with patch("server.solve_bucket", return_value=MOCK_ALLOCATE_BUCKET):
            data = client.post("/api/allocate", json={"scoring_mode": "bucket"}).json()
        assert data["scoring_mode"] == "bucket"
        assert data["method"] == "bucket_v2"


# ── POST /api/weekly_greedy ───────────────────────────────────────────

class TestWeeklyGreedyEndpoint:
    def test_returns_200(self, client):
        with patch("server.solve_weekly_greedy", return_value=MOCK_WEEKLY_GREEDY):
            response = client.post("/api/weekly_greedy", json={})
        assert response.status_code == 200

    def test_response_has_method_field(self, client):
        with patch("server.solve_weekly_greedy", return_value=MOCK_WEEKLY_GREEDY):
            data = client.post("/api/weekly_greedy", json={}).json()
        assert data.get("method") == "greedy"

    def test_response_has_vehicles_list(self, client):
        with patch("server.solve_weekly_greedy", return_value=MOCK_WEEKLY_GREEDY):
            data = client.post("/api/weekly_greedy", json={}).json()
        assert isinstance(data["vehicles"], list)

    def test_scoring_mode_is_ignored_by_greedy(self, client):
        """Greedy is distance-only — passing scoring_mode='bucket' must NOT
        change behavior and must NOT propagate to the greedy solver."""
        mock_fn = MagicMock(return_value=MOCK_WEEKLY_GREEDY)
        with patch("server.solve_weekly_greedy", mock_fn):
            client.post("/api/weekly_greedy", json={
                "scoring_mode": "bucket",
                "bucket_mults": [3.0, 2.0, 1.0, 0.5],
            })
        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        # The greedy handler must never forward bucket params downstream.
        assert "scoring_mode" not in kwargs
        assert "bucket_mults" not in kwargs


# ── Bucket round-trip: confirm + allocation_cache ─────────────────────

class TestBucketRoundTrip:
    """Bucket-shaped cached batches must survive the confirm / cache
    endpoints intact so the rest of the dashboard can render them.
    """

    def test_confirm_clears_bucket_weekly_cache(self, client):
        with patch("server.solve_weekly_bucket", return_value=MOCK_WEEKLY_BUCKET):
            client.post("/api/weekly", json={"scoring_mode": "bucket"})
        with patch("server.confirm_allocation", return_value=MOCK_CONFIRM):
            response = client.post(
                "/api/confirm",
                json={"assignments": [
                    {"vin": "VIN_XXXXXXXXXX001", "dealer_code": "FD01"}
                ]},
            )
        assert response.status_code == 200
        store = server.app.state.store
        sess = store.get_or_create(TEST_SESSION_ID)
        # last_weekly is wiped on confirm regardless of which mode populated it.
        assert sess.last_weekly is None

    def test_allocation_cache_returns_bucket_payload(self, client):
        with patch("server.solve_weekly_bucket", return_value=MOCK_WEEKLY_BUCKET):
            client.post("/api/weekly", json={"scoring_mode": "bucket"})
        data = client.get("/api/allocation_cache").json()
        assert "vehicles" in data
        assert data["vehicles"][0]["vin"] == "VIN_XXXXXXXXXX001"
        # Summary must carry bucket-shape params for downstream consumers.
        assert "bucket_mults" in data["summary"]["params"]
        assert data["summary"]["params"]["signal_field"] == "IN_SERVICE"


# ── POST /api/confirm ─────────────────────────────────────────────────

class TestConfirmEndpoint:
    def test_returns_200(self, client):
        with patch("server.confirm_allocation", return_value=MOCK_CONFIRM):
            response = client.post(
                "/api/confirm",
                json={"assignments": [{"vin": "VIN_XXXXXXXXXX001", "dealer_code": "FD01"}]},
            )
        assert response.status_code == 200

    def test_response_has_updated_and_total(self, client):
        with patch("server.confirm_allocation", return_value=MOCK_CONFIRM):
            data = client.post(
                "/api/confirm",
                json={"assignments": [{"vin": "VIN_XXXXXXXXXX001", "dealer_code": "FD01"}]},
            ).json()
        assert "updated" in data
        assert "total" in data

    def test_confirm_clears_state(self, client):
        # Per-session state: the cleared cache lives on the SessionState
        # for the fixture's default session id.
        store = server.app.state.store
        session = store.get_or_create(TEST_SESSION_ID)
        session.last_weekly = {"batch_size": 10}
        with patch("server.confirm_allocation", return_value=MOCK_CONFIRM):
            client.post(
                "/api/confirm",
                json={"assignments": [{"vin": "VIN_XXXXXXXXXX001", "dealer_code": "FD01"}]},
            )
        assert session.cached() is None


# ── GET /api/export/confirmed ─────────────────────────────────────────

class TestConfirmedExportEndpoint:
    def test_returns_stub_status_for_empty_session(self, client):
        data = client.get(
            "/api/export/confirmed",
            headers={"X-Session-Id": EXPORT_EMPTY_SESSION_ID},
        ).json()
        assert data["status"] == "stub"
        assert data["integration_status"] == "not_connected"
        assert data["count"] == 0
        assert data["vehicles"] == []

    def test_returns_confirmed_transporting_vehicles(self, client):
        sid = EXPORT_CONFIRMED_SESSION_ID
        confirm_response = client.post(
            "/api/confirm",
            json={"assignments": [
                {
                    "vin": "VIN_XXXXXXXXXX001",
                    "dealer_code": "FD01",
                    "dealer_name": "FaaS_Dealer_Alpha",
                }
            ]},
            headers={"X-Session-Id": sid},
        )
        assert confirm_response.status_code == 200

        data = client.get("/api/export/confirmed", headers={"X-Session-Id": sid}).json()
        assert data["status"] == "stub"
        assert data["integration_status"] == "not_connected"
        assert data["count"] == 1
        vehicle = data["vehicles"][0]
        assert vehicle["vin"] == "VIN_XXXXXXXXXX001"
        assert vehicle["status"] == "Transporting"
        assert vehicle["assigned_dealer"] == "FD01"


# ── POST /api/reset ───────────────────────────────────────────────────

class TestResetEndpoint:
    def test_returns_200(self, client):
        with patch("server.reset_fleet", return_value=MOCK_RESET):
            response = client.post("/api/reset")
        assert response.status_code == 200

    def test_response_has_status_key(self, client):
        with patch("server.reset_fleet", return_value=MOCK_RESET):
            data = client.post("/api/reset").json()
        assert "status" in data


# ── GET /api/allocation_cache ─────────────────────────────────────────

class TestAllocationCacheEndpoint:
    def _session(self, client):
        return client.app.state.store.get_or_create(TEST_SESSION_ID)

    def test_returns_error_when_no_cache(self, client):
        s = self._session(client)
        s.last_weekly = None
        s.last_allocate = None
        data = client.get("/api/allocation_cache").json()
        assert "error" in data

    def test_returns_summary_when_cache_exists(self, client):
        s = self._session(client)
        s.last_weekly = MOCK_WEEKLY
        try:
            data = client.get("/api/allocation_cache").json()
            assert "summary" in data
        finally:
            s.last_weekly = None

    def test_vehicles_list_present_in_cache_response(self, client):
        s = self._session(client)
        s.last_weekly = MOCK_WEEKLY
        try:
            data = client.get("/api/allocation_cache").json()
            assert "vehicles" in data
        finally:
            s.last_weekly = None


# ── POST /api/approve_constraint ─────────────────────────────────────

class TestApproveConstraintEndpoint:
    def test_returns_200(self, client):
        response = client.post("/api/approve_constraint")
        assert response.status_code == 200

    def test_response_has_approval_token(self, client):
        data = client.post("/api/approve_constraint").json()
        assert "approval_token" in data
        assert isinstance(data["approval_token"], str)
        assert len(data["approval_token"]) > 0


# ── MCP approval token scoping ───────────────────────────────────────

class TestApprovalTokenScope:
    def test_constraint_tokens_are_session_scoped(self):
        import mcp_server
        import engine
        from state import SessionStore
        import contextvars

        store = SessionStore( baseline_fleet_df=engine.load_baseline_fleet())
        mcp_server.set_store(store)

        tok_sess_a = mcp_server.mint_approval_token(MCP_SESSION_A_ID)
        tok_sess_b = mcp_server.mint_approval_token(MCP_SESSION_B_ID)
        assert tok_sess_a != tok_sess_b

        constraint_src = """
def apply(model, x, veh, dealer, **ctx):
    return True
"""

        token_a = mcp_server._current_session_id.set(MCP_SESSION_A_ID)
        try:
            # Same-session token should register immediately.
            result = mcp_server.add_constraint(
                "scope_test", "scoped test", constraint_src, approval_token=tok_sess_a,
            )
            assert result["status"] == "registered"

            # Token from other session must not authorize this one.
            result = mcp_server.remove_constraint("scope_test", approval_token=tok_sess_b)
            assert result["status"] == "needs_approval"

            # Single-use tokens cannot be reused, even in the same session.
            result = mcp_server.remove_constraint("scope_test", approval_token=tok_sess_a)
            assert result["status"] == "needs_approval"
        finally:
            mcp_server._current_session_id.reset(token_a)


# ── POST /api/chat (SSE smoke) ────────────────────────────────────────

class TestChatEndpoint:
    """Minimal smoke: agent stream is mocked, we verify the endpoint sets
    the SSE content-type and emits the [DONE] sentinel."""

    def _fake_stream(self, *args, **kwargs):
        yield {"type": "status", "icon": "🔍", "text": "Analyzing..."}
        yield {"type": "answer", "answer": "ok", "tool_calls": [],
               "cost": 0.0, "dashboard_refresh": False,
               "new_params": None, "session_id": CHAT_SESSION_ID}

    def test_returns_200_and_sse_content_type(self, client):
        with patch("server.run_agent_stream", side_effect=self._fake_stream):
            response = client.post(
                "/api/chat",
                json={"message": "hi", "context": "", "client_id": TEST_SESSION_ID},
            )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

    def test_stream_terminates_with_done_sentinel(self, client):
        # X-Session-Id must match body.client_id (the chat handler enforces
        # this so the MCP tool callbacks land on the same SessionState the
        # chat is keyed on). Override the fixture header for this case.
        with patch("server.run_agent_stream", side_effect=self._fake_stream):
            response = client.post(
                "/api/chat",
                json={"message": "hi", "context": "", "client_id": CONFIRM_SESSION_ID},
                headers={"X-Session-Id": CONFIRM_SESSION_ID},
            )
        assert "[DONE]" in response.text


# ── Input bounds (n_batch / n_vehicles / weights / params) ────────────

class TestInputBounds:
    def test_n_vehicles_capped_at_fleet_size(self, client):
        # 650 is the fleet size — one more is rejected.
        response = client.post("/api/allocate", json={"n_vehicles": 651})
        assert response.status_code == 422

    def test_n_vehicles_at_fleet_size_ok(self, client):
        with patch("server.solve_both", return_value=MOCK_ALLOCATE):
            response = client.post("/api/allocate", json={"n_vehicles": 650})
        assert response.status_code == 200

    def test_n_batch_capped_at_fleet_size(self, client):
        response = client.post("/api/weekly", json={"n_batch": 651})
        assert response.status_code == 422

    def test_n_batch_negative_rejected(self, client):
        response = client.post("/api/weekly", json={"n_batch": -5})
        assert response.status_code == 422

    def test_negative_weight_rejected(self, client):
        response = client.post("/api/weekly", json={"w_util": -1.0})
        assert response.status_code == 422

    def test_absurd_weight_rejected(self, client):
        response = client.post("/api/allocate", json={"w_dist": 10_000.0})
        assert response.status_code == 422

    def test_expected_stay_months_upper_bound(self, client):
        response = client.post("/api/weekly", json={"expected_stay_months": 500})
        assert response.status_code == 422

    def test_source_limit_capped(self, client):
        response = client.post("/api/weekly", json={"source_limit": 10_000})
        assert response.status_code == 422


# ── DEMO_MODE runtime behavior (request-time checks) ──────────────────

class TestDemoModeRuntime:
    def test_sessions_returns_404_in_demo_mode(self, client, monkeypatch):
        monkeypatch.setattr(server, "DEMO_MODE", True)
        response = client.get(
            "/api/sessions",
            headers={"X-Session-Id": "99999999-9999-4999-8999-999999999999"},
        )
        assert response.status_code == 404

    def test_chat_status_echoes_gateway_quota_headers(self, client, monkeypatch):
        monkeypatch.setattr(server, "DEMO_MODE", True)
        data = client.get(
            "/api/chat_status",
            headers={
                "X-Session-Id": "a9999999-9999-4999-8999-999999999999",
                "X-Demo-Quota-Limit": "50",
                "X-Demo-Quota-Remaining": "42",
            },
        ).json()
        assert data["demo"] is True
        assert data["quota_limit"] == "50"
        assert data["quota_remaining"] == "42"
        assert data["disabled"] is False

    def test_chat_not_capped_in_demo_mode(self, client, monkeypatch):
        monkeypatch.setattr(server, "DEMO_MODE", True)
        sid = "b9999999-9999-4999-8999-999999999999"
        # Pre-exhaust the per-session counter — demo mode must still stream.
        store = server.app.state.store
        sess = store.get_or_create(sid)
        sess.chat_count = 99

        def _fake_stream(*args, **kwargs):
            yield {"type": "answer", "answer": "ok", "tool_calls": [],
                   "cost": 0.0, "dashboard_refresh": False,
                   "new_params": None, "session_id": sid}

        with patch("server.run_agent_stream", side_effect=_fake_stream):
            response = client.post(
                "/api/chat",
                json={"message": "hi", "context": "", "client_id": sid},
                headers={"X-Session-Id": sid},
            )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert "[DONE]" in response.text
