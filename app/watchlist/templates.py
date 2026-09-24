"""Watchlist alert templates — shapes the agent picks from.

Templates are the "vocabulary" of alert types. The agent picks which template
fits each signal, then fills the placeholders using values FROM THE SIGNAL'S
context/metric — no invention. If the agent fails, `render_fallback` produces
a deterministic alert from the same template menu (sensible default per type).
"""

# Each template documents its shape; the agent uses `description` + `fields` to
# decide which template suits which signal. The `example_format` is illustrative
# but the agent writes its own prose, not literal string-format.
TEMPLATES = {
    "saturation_single": {
        "description": "Single dealer at critical util — saturation risk",
        "fields": ["dealer_name", "util_pct", "state", "idle_capacity", "rented", "in_service"],
        "example_format": "{dealer_name} at {util_pct}% util in {state} · {idle_capacity} idle slots",
    },
    "saturation_with_nearby_idle": {
        "description": "Saturated dealer in a region where other dealers have idle capacity",
        "fields": ["dealer_name", "util_pct", "state", "nearby_idle_summary"],
        "example_format": "{dealer_name} at {util_pct}% · nearby dealers in {state} have idle capacity",
    },
    "deferred_simple": {
        "description": "Batch has deferred vehicles, single dominant region",
        "fields": ["count", "top_state", "top_state_count"],
        "example_format": "{count} vehicles deferred · {top_state_count} from {top_state}",
    },
    "deferred_multi_region": {
        "description": "Batch deferred across multiple regions (no single dominant source)",
        "fields": ["count", "region_summary"],
        "example_format": "{count} vehicles deferred across {region_summary}",
    },
    "capacity_mismatch": {
        "description": "Idle capacity exists in same state as deferred vehicles — routing puzzle",
        "fields": ["state", "deferred_count", "idle_capacity_in_state", "dealers_in_state"],
        "example_format": "{state}: {deferred_count} deferred but {idle_capacity_in_state} idle slots across {dealers_in_state} dealers",
    },
    "stuck_vehicles_concentrated": {
        "description": "Stuck-grounded vehicles concentrated at one source dealer",
        "fields": ["count", "weeks_threshold", "top_source", "top_source_count", "oldest_week"],
        "example_format": "{count} vehicles grounded >{weeks_threshold} weeks · {top_source_count} from {top_source}",
    },
    "stuck_vehicles_distributed": {
        "description": "Stuck-grounded vehicles spread across many source dealers",
        "fields": ["count", "weeks_threshold", "source_count"],
        "example_format": "{count} vehicles grounded >{weeks_threshold} weeks across {source_count} sources",
    },
    "underutilized_dealer": {
        "description": "Dealer with significant idle capacity — routing opportunity",
        "fields": ["dealer_name", "util_pct", "idle_capacity", "state"],
        "example_format": "{dealer_name} at {util_pct}% util · {idle_capacity} idle slots in {state}",
    },
    "source_pressure": {
        "description": "Source dealer accumulating grounded vehicles — next-batch priority",
        "fields": ["source_code", "state", "grounded_count"],
        "example_format": "{source_code} ({state}) has {grounded_count} grounded vehicles waiting",
    },
}


def render_fallback(signal):
    """Deterministic per-signal-type rendering. Used when the Claude CLI agent fails
    so the endpoint always returns something usable.

    No judgment, no ranking — just picks a sensible default template per type."""
    sig_type = signal.get("type")
    ctx = signal.get("context", {}) or {}
    metric = signal.get("metric", {}) or {}
    sev = signal.get("severity", "medium")

    if sig_type == "saturation":
        return {
            "template_id": "saturation_single",
            "severity": sev,
            "title": "{} at {}% util".format(ctx.get("dealer_name", "?"), ctx.get("util_pct", "?")),
            "body": "In {}. {} of {} rented · {} idle slots remaining.".format(
                ctx.get("state", "?"), ctx.get("rented", "?"),
                ctx.get("in_service", "?"), ctx.get("idle_capacity", "?"),
            ),
            "action_label": "Raise cap",
        }
    if sig_type == "deferred_batch":
        n = ctx.get("total_deferred", "?")
        return {
            "template_id": "deferred_simple",
            "severity": sev,
            "title": "{} vehicles deferred this batch".format(n),
            "body": "Top region: {} ({} of {}). All candidate dealers at slot cap.".format(
                ctx.get("top_state", "?"), ctx.get("top_state_count", "?"), n,
            ),
            "action_label": "Resolve",
        }
    if sig_type == "capacity_mismatch":
        return {
            "template_id": "capacity_mismatch",
            "severity": sev,
            "title": "{}: routing puzzle".format(ctx.get("state", "?")),
            "body": "{} deferred but {} idle slots across {} dealers in {}.".format(
                ctx.get("deferred_count", "?"), ctx.get("idle_capacity_in_state", "?"),
                ctx.get("dealers_in_state", "?"), ctx.get("state", "?"),
            ),
            "action_label": "Investigate",
        }
    if sig_type == "stuck_vehicles":
        return {
            "template_id": "stuck_vehicles_concentrated",
            "severity": sev,
            "title": "{} vehicles grounded >{} weeks".format(
                ctx.get("total_stuck", "?"), ctx.get("weeks_threshold", "?"),
            ),
            "body": "Top source: {} with {} of them. Oldest grounded {}.".format(
                ctx.get("top_source", "?"), ctx.get("top_source_count", "?"),
                ctx.get("oldest_week", "?"),
            ),
            "action_label": "Review",
        }
    if sig_type == "underutilized":
        return {
            "template_id": "underutilized_dealer",
            "severity": sev,
            "title": "{} at {}% util".format(ctx.get("dealer_name", "?"), ctx.get("util_pct", "?")),
            "body": "{} idle slots in {}. Candidate for next-batch routing target.".format(
                ctx.get("idle_capacity", "?"), ctx.get("state", "?"),
            ),
            "action_label": "Route more",
        }
    if sig_type == "source_pressure":
        return {
            "template_id": "source_pressure",
            "severity": sev,
            "title": "{}: {} grounded".format(
                ctx.get("source_code", "?"), ctx.get("grounded_count", "?"),
            ),
            "body": "Source dealer in {}. Prioritize in next batch.".format(ctx.get("state", "?")),
            "action_label": "Prioritize",
        }
    return {
        "template_id": "unknown",
        "severity": "low",
        "title": str(signal.get("id", "Unknown signal")),
        "body": "No template registered for type {}.".format(sig_type),
        "action_label": "Review",
    }
