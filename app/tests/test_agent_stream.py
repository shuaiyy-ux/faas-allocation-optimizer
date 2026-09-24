#!/usr/bin/env python3
"""
Unit tests for app/agent.py — SSE event translation from Claude CLI events.

Architecture (Claude CLI migration):
  - run_agent_stream() spawns `claude -p --output-format stream-json ...`.
  - The CLI dispatches MCP tools internally and emits stream-json events.
  - agent.py translates those into SSE events:
      status | tool_start | tool_done | token | focus | answer | demo_usage | error

Test approach:
  - Mock subprocess.Popen to return a fake process whose stdout yields
    stream-json lines. No real subprocess or MCP server required.
  - Assert that agent.py emits the correct SSE event sequence, captures the
    Claude session id for --resume, and emits the demo_usage line.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# app/tests/this.py -> parent = app/tests -> parent.parent = app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import (  # noqa: E402
    _ALLOWED_TOOLS,
    _CLAUDE_SESSIONS,
    _build_cli_args,
    _build_user_message,
    _claude_model,
    _mcp_config_json,
    _remember_claude_session,
    _stored_claude_session,
    run_agent_stream,
    SYSTEM_PROMPT,
)


# ── Helpers ───────────────────────────────────────────────────────────

def _make_fake_proc(stream_lines: list, returncode: int = 0):
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = io.StringIO("".join(json.dumps(line) + "\n" for line in stream_lines))
    proc.stderr = io.StringIO("")
    proc.returncode = returncode
    proc.wait.return_value = returncode
    proc.kill.return_value = None
    proc.terminate.return_value = None
    return proc


def _stream_events(message: str, session_id: str = "test-session-001",
                   context: str = "", is_first_turn: bool = True,
                   fake_lines: list = None):
    fake_lines = fake_lines or []
    fake_proc = _make_fake_proc(fake_lines)
    with patch("agent.subprocess.Popen", return_value=fake_proc):
        return list(run_agent_stream(
            message=message,
            context=context,
            session_id=session_id,
            is_first_turn=is_first_turn,
        ))


# ── Claude CLI stream-json builders ───────────────────────────────────

def _init_event(session_id="claude-sess-abc", tools=None, model="claude-opus"):
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "model": model,
        "tools": tools if tools is not None else [f"mcp__faas__{t}" for t in (
            "query_data", "run_allocation")],
        "mcp_servers": [{"name": "faas", "status": "connected"}],
    }


def _text_delta(text, idx=0):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": idx,
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _tool_use_start(tool_id, raw_name, idx=0):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": "tool_use", "id": tool_id, "name": raw_name},
        },
    }


def _tool_use_stop(idx=0):
    return {
        "type": "stream_event",
        "event": {"type": "content_block_stop", "index": idx},
    }


def _tool_result(tool_use_id, output, is_error=False):
    return {
        "type": "user",
        "message": {
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "is_error": is_error,
                "content": [{"type": "text", "text": output}],
            }],
        },
    }


def _result_event(text, session_id="claude-sess-abc", cost=0.012):
    return {
        "type": "result",
        "subtype": "success",
        "session_id": session_id,
        "result": text,
        "total_cost_usd": cost,
        "duration_ms": 4200,
        "num_turns": 2,
        "usage": {
            "input_tokens": 1200, "output_tokens": 340,
            "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100,
        },
        "model": "claude-opus",
    }


# ═══════════════════════════════════════════════════════════════════════
# CLI argument construction — the safety flags are the whole point.
# ═══════════════════════════════════════════════════════════════════════
class TestCLIArgs:
    def setup_method(self):
        _CLAUDE_SESSIONS.clear()

    def teardown_method(self):
        _CLAUDE_SESSIONS.clear()

    def test_first_turn_has_all_safety_flags(self):
        args = _build_cli_args("11111111-1111-4111-8111-111111111111", True)
        assert args[:2] == ["claude", "-p"]
        assert "--output-format" in args and args[args.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in args
        assert "--include-partial-messages" in args
        # --tools "" disables all built-in tools.
        assert args[args.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in args
        assert "--setting-sources" in args and args[args.index("--setting-sources") + 1] == ""
        assert "--disable-slash-commands" in args
        assert args[args.index("--permission-mode") + 1] == "dontAsk"
        assert args[args.index("--system-prompt") + 1] == SYSTEM_PROMPT
        # bypassPermissions must NOT be used anywhere.
        assert "bypassPermissions" not in args

    def test_allowed_tools_are_only_faas_mcp(self):
        args = _build_cli_args("11111111-1111-4111-8111-111111111111", True)
        i = args.index("--allowedTools")
        # every allowed tool token must be an mcp__faas__ name
        allowed = []
        for tok in args[i + 1:]:
            if tok.startswith("--"):
                break
            allowed.append(tok)
        assert allowed == _ALLOWED_TOOLS
        assert allowed and all(t.startswith("mcp__faas__") for t in allowed)

    def test_first_turn_has_no_resume(self):
        args = _build_cli_args("11111111-1111-4111-8111-111111111111", True)
        assert "--resume" not in args

    def test_followup_turn_resumes_stored_claude_session(self):
        _remember_claude_session("faas-1", "claude-sess-xyz")
        args = _build_cli_args("faas-1", False)
        assert args[args.index("--resume") + 1] == "claude-sess-xyz"

    def test_model_default_is_opus(self):
        with patch.dict("agent.os.environ", {}, clear=True):
            assert _claude_model() == "opus"

    def test_model_env_override(self):
        with patch.dict("agent.os.environ", {"FAAS_CLAUDE_MODEL": "sonnet"}, clear=True):
            assert _claude_model() == "sonnet"


# ═══════════════════════════════════════════════════════════════════════
# MCP config — inline http server with the internal key header.
# ═══════════════════════════════════════════════════════════════════════
class TestMcpConfig:
    def test_config_is_http_with_session_and_internal_key(self):
        cfg = json.loads(_mcp_config_json("11111111-1111-4111-8111-111111111111"))
        faas = cfg["mcpServers"]["faas"]
        assert faas["type"] == "http"
        assert "faas_session_id=11111111-1111-4111-8111-111111111111" in faas["url"]
        assert faas["url"].startswith("http://127.0.0.1:")
        assert "X-Internal-Key" in faas["headers"]
        assert faas["headers"]["X-Internal-Key"]


# ═══════════════════════════════════════════════════════════════════════
# Session capture / resume bookkeeping
# ═══════════════════════════════════════════════════════════════════════
class TestSessionCapture:
    def setup_method(self):
        _CLAUDE_SESSIONS.clear()

    def teardown_method(self):
        _CLAUDE_SESSIONS.clear()

    def test_first_turn_returns_none(self):
        assert _stored_claude_session("s1", True) is None

    def test_captures_session_id_from_init_event(self):
        lines = [
            _init_event(session_id="captured-sess-1"),
            _text_delta("hi"),
            _result_event("hi", session_id="captured-sess-1"),
        ]
        _stream_events("hello", session_id="faas-x", fake_lines=lines)
        assert _CLAUDE_SESSIONS.get("faas-x") == "captured-sess-1"


# ═══════════════════════════════════════════════════════════════════════
# Prompt contract — SYSTEM_PROMPT keeps the business behavior
# ═══════════════════════════════════════════════════════════════════════
class TestPromptContract:
    def test_system_prompt_has_business_answering_rules(self):
        prompt = SYSTEM_PROMPT
        assert "HCA's FaaS optimizer" in prompt
        assert "business decision-support analyst" in prompt
        assert "Always answer in English" in prompt
        assert "even if the user writes in another language" in prompt
        assert "[Current UI state]" in prompt
        assert "**What I can do**" in prompt
        assert "**What I cannot do**" in prompt
        assert "Do not wrap the entire answer in a code block" in prompt

    def test_user_message_wraps_context_and_question(self):
        msg = _build_user_message("Current batch: 31 assigned", "introduce yourself")
        assert "[Current UI state]" in msg
        assert "Current batch: 31 assigned" in msg
        assert "[User question]" in msg
        assert "introduce yourself" in msg


# ═══════════════════════════════════════════════════════════════════════
# Missing session_id
# ═══════════════════════════════════════════════════════════════════════
class TestMissingSessionId:
    def test_yields_error_when_session_id_none(self):
        with patch("agent.subprocess.Popen") as mock_popen:
            events = list(run_agent_stream("hello", session_id=None))
        mock_popen.assert_not_called()
        assert events == [{"type": "error", "text": "missing session_id"}]


# ═══════════════════════════════════════════════════════════════════════
# CLI not found on PATH
# ═══════════════════════════════════════════════════════════════════════
class TestCLINotFound:
    def test_yields_error_when_claude_not_on_path(self):
        with patch("agent.subprocess.Popen", side_effect=FileNotFoundError()):
            events = list(run_agent_stream("hello", session_id="s1"))
        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) == 1
        assert "Claude CLI not found" in error_events[0]["text"]


# ═══════════════════════════════════════════════════════════════════════
# run_allocation tool_use flow
# ═══════════════════════════════════════════════════════════════════════
class TestRunAllocationToolUseFlow:
    _TOOL_ID = "toolu_run_alloc_001"
    _ANSWER = "Allocation complete. 49 vehicles assigned."

    @pytest.fixture(autouse=True)
    def _clear(self):
        _CLAUDE_SESSIONS.clear()
        yield
        _CLAUDE_SESSIONS.clear()

    @pytest.fixture
    def events(self):
        lines = [
            _init_event(),
            _tool_use_start(self._TOOL_ID, "mcp__faas__run_allocation", idx=0),
            _tool_use_stop(idx=0),
            _tool_result(self._TOOL_ID, '{"assigned": 49, "delta": 33.0}'),
            _text_delta("Allocation complete. "),
            _text_delta("49 vehicles assigned."),
            _result_event(self._ANSWER),
        ]
        return _stream_events("run allocation for 50 vehicles", fake_lines=lines)

    def test_tool_start_emitted(self, events):
        tool_starts = [e for e in events if e["type"] == "tool_start"]
        assert any(e["name"] == "run_allocation" for e in tool_starts)

    def test_tool_start_has_label(self, events):
        tool_starts = [e for e in events if e["type"] == "tool_start"]
        assert tool_starts[0]["label"] == "Running allocation"

    def test_tool_done_emitted(self, events):
        tool_dones = [e for e in events if e["type"] == "tool_done"]
        assert len(tool_dones) == 1
        assert tool_dones[0]["name"] == "run_allocation"
        assert tool_dones[0]["error"] is None

    def test_tool_start_precedes_tool_done(self, events):
        types = [e["type"] for e in events]
        assert types.index("tool_start") < types.index("tool_done")

    def test_token_events_emitted_for_answer_text(self, events):
        tokens = [e for e in events if e["type"] == "token"]
        combined = "".join(e["text"] for e in tokens)
        assert self._ANSWER in combined

    def test_answer_event_present(self, events):
        answers = [e for e in events if e["type"] == "answer"]
        assert len(answers) == 1
        assert answers[0]["dashboard_refresh"] is True
        assert answers[0]["session_id"] == "test-session-001"

    def test_demo_usage_is_last_event(self, events):
        assert events[-1]["type"] == "demo_usage"
        u = events[-1]
        assert u["total_cost_usd"] == 0.012
        assert u["input_tokens"] == 1200
        assert u["output_tokens"] == 340
        assert u["cache_read_input_tokens"] == 900
        assert u["num_turns"] == 2
        assert u["duration_ms"] == 4200
        assert "mcp__faas__run_allocation" in u["tools_used"]
        assert self._ANSWER in u["answer"]


# ═══════════════════════════════════════════════════════════════════════
# Plain text answer (no tool use)
# ═══════════════════════════════════════════════════════════════════════
class TestPlainTextAnswer:
    _ANSWER = "The allocation formula uses four weights."

    @pytest.fixture(autouse=True)
    def _clear(self):
        _CLAUDE_SESSIONS.clear()
        yield
        _CLAUDE_SESSIONS.clear()

    @pytest.fixture
    def events(self):
        lines = [
            _init_event(),
            _text_delta("The allocation formula "),
            _text_delta("uses four weights."),
            _result_event(self._ANSWER),
        ]
        return _stream_events("explain the formula", fake_lines=lines)

    def test_no_tool_events(self, events):
        assert not any(e["type"] in ("tool_start", "tool_done") for e in events)

    def test_token_events_contain_answer_text(self, events):
        combined = "".join(e["text"] for e in events if e["type"] == "token")
        assert self._ANSWER in combined

    def test_answer_then_demo_usage(self, events):
        types = [e["type"] for e in events]
        assert types[-2] == "answer"
        assert types[-1] == "demo_usage"
        answer = [e for e in events if e["type"] == "answer"][0]
        assert answer["dashboard_refresh"] is False

    def test_no_error_events(self, events):
        assert not any(e["type"] == "error" for e in events)
