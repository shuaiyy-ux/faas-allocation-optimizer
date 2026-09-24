"""Watchlist module — fleet operations anomaly detection.

Public API: `refresh()` runs the 6 signal aggregators (deterministic, real data only),
then hands them to a Claude CLI-based agent that picks templates and writes alerts.
Returns dict with `signals`, `alerts`, `refreshed_at`, `count`. Max 5 alerts per refresh.
"""
from datetime import datetime
from .signals import all_signals
from .agent import narrate, MAX_ITEMS


def refresh():
    """Compute fresh signals + agent narration. Caller is responsible for caching.

    The `_usage` key (usage payload, or None on fallback) is for the caller to
    surface via the `X-Demo-Usage` header; it should be stripped before caching
    / returning in the body.
    """
    sigs = all_signals()
    alerts, usage = narrate(sigs)
    return {
        "signals": sigs,
        "alerts": alerts,
        "refreshed_at": datetime.now().isoformat(),
        "count": len(alerts),
        "max_items": MAX_ITEMS,
        "_usage": usage,
    }
