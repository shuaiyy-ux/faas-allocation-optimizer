"""Watchlist agent — Claude CLI subprocess wrapper.

Uses the local Claude CLI in one-shot non-streaming mode (`--output-format
json`). No tools, no MCP, no settings — pure text ranking of pre-computed
signals. Falls back to deterministic template rendering if the CLI is
unavailable or returns invalid output.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .templates import TEMPLATES, render_fallback

MAX_ITEMS = 5
TIMEOUT_SEC = 60
PROJECT_DIR = Path(__file__).resolve().parents[2]

# Dedicated empty working directory — never the repo root (no CLAUDE.md /
# settings / project files must reach the CLI). FAAS_VAR_DIR relocates it.
_VAR_DIR = Path(os.environ.get("FAAS_VAR_DIR") or Path(__file__).resolve().parents[1] / "var")
_AGENT_CWD = _VAR_DIR / "watchlist_cwd"


def _agent_cwd() -> str:
    try:
        _AGENT_CWD.mkdir(parents=True, exist_ok=True)
    except OSError:
        return tempfile.gettempdir()
    return str(_AGENT_CWD)


def _model() -> str:
    return os.environ.get("FAAS_CLAUDE_MODEL_WATCHLIST", "sonnet").strip() or "sonnet"


def _build_prompt(signals: List[Dict[str, Any]]) -> str:
    """Compose the one-shot prompt: ranking + template selection + filling, all in one.

    The hard constraint: agent must use values present in the signal JSON only —
    no invention. Templates are a shape menu; the agent's judgment goes into
    *which* template fits and *how* to phrase the body within the template's grammar.
    """
    return (
        "You are a fleet operations watchlist agent for the HCA FaaS Vehicle "
        "Allocation system.\n\n"
        "Input: a list of structured signals computed from REAL data + a menu of "
        "template shapes.\n\n"
        "Task:\n"
        "1. Rank signals by (severity, business impact). Pick AT MOST {max_items}.\n"
        "2. For each chosen signal, pick the best matching template shape from the menu.\n"
        "3. Write a short alert using ONLY values from that signal's context/metric.\n"
        "4. Body: 1–2 sentences, factual, no speculation, no invented numbers.\n\n"
        "Return a JSON array (no markdown fences, no preamble, ONLY the array) of\n"
        "at most {max_items} items in this exact shape:\n"
        '[\n'
        '  {{\n'
        '    "signal_id": "<id from input signal>",\n'
        '    "template_id": "<id from templates>",\n'
        '    "severity": "low|medium|high|critical",\n'
        '    "title": "<concise 1-line headline, <80 chars>",\n'
        '    "body": "<1-2 sentences with numbers from signal context, <200 chars>",\n'
        '    "action_label": "<imperative 1-3 word button, e.g. Raise cap / Investigate / Review>"\n'
        '  }}\n'
        ']\n\n'
        "Rules:\n"
        "- Use ONLY values present in the signal JSON. Missing field → omit it; do not invent.\n"
        "- severity must equal or be ≤ the signal's severity.\n"
        "- Skip signals whose template_id field menu doesn't have a fit — better to return\n"
        "  fewer than {max_items} than to force a bad fit.\n"
        "- Numbers in body must come from signal.context or signal.metric.\n\n"
        "SIGNALS (JSON):\n{signals_json}\n\n"
        "TEMPLATES (JSON):\n{templates_json}\n\n"
        "Return ONLY the JSON array. No explanation, no markdown fences."
    ).format(
        max_items=MAX_ITEMS,
        signals_json=json.dumps(signals, indent=2),
        templates_json=json.dumps(TEMPLATES, indent=2),
    )


def _usage_from_result(event: Dict[str, Any]) -> Dict[str, Any]:
    """Compact usage payload for the `X-Demo-Usage` header (no answer field)."""
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    return {
        "model": event.get("model") or _model(),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "total_cost_usd": event.get("total_cost_usd"),
        "duration_ms": event.get("duration_ms"),
        "num_turns": event.get("num_turns"),
        "tools_used": [],
    }


def _parse_result_text(text: str) -> List[Dict[str, Any]]:
    """Strip code fences from the CLI `result` text and parse the JSON array."""
    text = (text or "").strip()
    if not text:
        raise ValueError("agent returned empty result")
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    items = json.loads(text)
    if not isinstance(items, list):
        raise ValueError("agent did not return a list, got: " + type(items).__name__)
    return items


def _fallback(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deterministic ranking + rendering when the agent is unavailable.

    Sort by severity (critical → low), then pick top MAX_ITEMS, render via
    `templates.render_fallback`. Output shape matches the agent's output."""
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sorted_sigs = sorted(
        signals,
        key=lambda s: severity_rank.get(s.get("severity"), 9),
    )
    return [
        dict(signal_id=s.get("id"), **render_fallback(s))
        for s in sorted_sigs[:MAX_ITEMS]
    ]


def narrate(signals: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Hand structured signals to the Claude CLI agent, get ranked alerts back.

    Returns `(alerts, usage)` — up to MAX_ITEMS alerts and the usage payload
    for the `X-Demo-Usage` header. On any failure, returns the deterministic
    fallback alerts and `None` usage (no model call was billable).
    """
    if not signals:
        return [], None

    prompt = _build_prompt(signals)

    try:
        result = subprocess.run(
            [
                "claude", "-p",
                "--output-format", "json",
                "--model", _model(),
                "--tools", "",
                "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "",
                "--disable-slash-commands",
                "--permission-mode", "dontAsk",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            cwd=_agent_cwd(),
        )
    except FileNotFoundError:
        print("[watchlist] claude CLI not in PATH — using fallback")
        return _fallback(signals), None
    except subprocess.TimeoutExpired:
        print("[watchlist] claude CLI timed out after {}s — using fallback".format(TIMEOUT_SEC))
        return _fallback(signals), None

    if result.returncode != 0:
        print("[watchlist] claude CLI exit {}: {}".format(
            result.returncode, (result.stderr or "")[:200]))
        return _fallback(signals), None

    try:
        event = json.loads(result.stdout)
        items = _parse_result_text(event.get("result", ""))
        usage = _usage_from_result(event)
    except (ValueError, json.JSONDecodeError, TypeError) as exc:
        print("[watchlist] agent output parse failed: {}".format(exc))
        return _fallback(signals), None

    # Defense in depth: cap at MAX_ITEMS even if agent returns more
    return items[:MAX_ITEMS], usage
