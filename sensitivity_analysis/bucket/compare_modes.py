"""Side-by-side bucket vs additive comparison HTML.

Runs both scoring modes on the same vehicle batch (n=50, seed=42), then
emits a self-contained HTML report with embedded Plotly charts. The
script is read-only and does not write anything to data CSVs.

Charts
------
1. KPI bar comparison (n_assigned, rank1_pct, avg_rank, total_distance,
   total_alloc_score, quality_pct, HHI).
2. Rank distribution histograms, side-by-side.
3. Per-dealer load — top 15 destinations under each mode.
4. Distance distribution (per-vehicle CDF + means).
5. Destination quality scatter: IN_SERVICE vs UTIL_RATE per assignment,
   colored by mode.
6. Bucket tier breakdown for bucket mode (counts in A / B / C / D).
7. VIN-level diff table — which dealers changed.

Output
------
    docs/viz/bucket_vs_additive.html

Usage
-----
    .venv/bin/python sensitivity_analysis/bucket/compare_modes.py \\
        --n-batch 50 --seed 42 \\
        --output docs/viz/bucket_vs_additive.html
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "app"))

import engine  # noqa: E402
import bucket_pipeline  # noqa: E402
from scoring import BucketParams, DEFAULT_BUCKET_MULTS, tier_breakpoints, tier_of  # noqa: E402


# ── HTML / chart helpers ──────────────────────────────────────────────

DARK_LAYOUT = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(13,17,23,0.6)",
    "font": {"family": "Inter, system-ui, sans-serif", "size": 12, "color": "#e0e4ea"},
    "margin": {"l": 56, "r": 24, "t": 44, "b": 56},
    "xaxis": {"gridcolor": "rgba(255,255,255,0.06)", "zerolinecolor": "rgba(255,255,255,0.1)"},
    "yaxis": {"gridcolor": "rgba(255,255,255,0.06)", "zerolinecolor": "rgba(255,255,255,0.1)"},
    "legend": {"bgcolor": "rgba(0,0,0,0)", "bordercolor": "rgba(255,255,255,0.1)"},
}

COLOR_ADDITIVE = "#00d4ff"  # cyan, matches frontend
COLOR_BUCKET = "#66bb6a"    # green
COLOR_DELTA_POS = "#00e676"
COLOR_DELTA_NEG = "#ff5252"


def chart_div(div_id: str, height: int = 360) -> str:
    return f'<div id="{div_id}" style="width:100%;height:{height}px;"></div>'


def chart_script(div_id: str, data: list, layout: dict) -> str:
    """Emit a Plotly.newPlot call as inline script. Each chart is independent."""
    merged_layout = {**DARK_LAYOUT, **layout}
    payload = json.dumps({"data": data, "layout": merged_layout},
                         default=lambda o: float(o) if hasattr(o, "__float__") else str(o))
    return (
        f"<script>(function(){{var p={payload};"
        f"Plotly.newPlot('{div_id}', p.data, p.layout, "
        "{displayModeBar:false, responsive:true});})();</script>"
    )


# ── Aggregations on the result dicts ──────────────────────────────────


def per_vehicle_rows(result: dict, mode: str, dealer_in_service: dict) -> pd.DataFrame:
    """Flatten the API response into one row per assigned vehicle.

    `dealer_in_service` is a DEALER_CODE -> IN_SERVICE map used as a fallback
    when the response itself does not carry the field (additive mode's
    `assigned` block omits IN_SERVICE because additive scoring does not
    reference it — the comparison needs it for the destination-quality chart).
    """
    rows = []
    for v in result.get("vehicles", []):
        a = v.get("assigned")
        if not a:
            continue
        dealer_code = a.get("dealer_code")
        in_service = a.get("in_service")
        if in_service is None:
            in_service = dealer_in_service.get(dealer_code, 0.0)
        rows.append({
            "mode": mode,
            "vin": v["vin"],
            "source": v.get("source"),
            "dealer_code": dealer_code,
            "dealer_name": a.get("dealer_name"),
            "state": a.get("state"),
            "distance": float(a.get("distance", 0) or 0),
            "alloc_score": float(a.get("alloc_score", 0) or 0),
            "utilization": float(a.get("utilization", 0) or 0),
            "rented": float(a.get("rented", 0) or 0),
            "in_service": float(in_service or 0),
            "rank": int(a.get("rank", 0) or 0),
        })
    return pd.DataFrame(rows)


def rank_counts(df: pd.DataFrame, total_n: int) -> dict:
    """Counts in r1 / r2 / r3 / r4+ / deferred."""
    if df.empty:
        return {"r1": 0, "r2": 0, "r3": 0, "r4_plus": 0, "deferred": total_n}
    by = df["rank"].value_counts().to_dict()
    return {
        "r1": int(by.get(1, 0)),
        "r2": int(by.get(2, 0)),
        "r3": int(by.get(3, 0)),
        "r4_plus": int(sum(c for r, c in by.items() if r >= 4)),
        "deferred": int(total_n - len(df)),
    }


def kpis_from_result(result: dict, per_vin: pd.DataFrame) -> dict:
    return {
        "n_assigned": result.get("n_assigned", 0),
        "batch_size": result.get("batch_size", 0),
        "rank1_pct": result.get("rank1_pct", 0.0),
        "avg_rank": result.get("avg_rank", 0.0),
        "total_alloc_score": result.get("total_alloc_score", 0.0),
        "total_distance": result.get("total_distance", 0.0),
        "quality_pct": result.get("quality_pct", 0.0),
        "avg_util_at_dest": float(per_vin["utilization"].mean()) if not per_vin.empty else 0.0,
        "avg_in_service_at_dest": float(per_vin["in_service"].mean()) if not per_vin.empty else 0.0,
        "avg_rented_at_dest": float(per_vin["rented"].mean()) if not per_vin.empty else 0.0,
        "avg_distance_per_assigned": float(per_vin["distance"].mean()) if not per_vin.empty else 0.0,
        "hhi": _hhi(per_vin),
    }


def _hhi(per_vin: pd.DataFrame) -> float:
    n = len(per_vin)
    if n == 0:
        return 0.0
    counts = per_vin["dealer_code"].value_counts()
    shares = counts / n
    return float((shares ** 2).sum())


# ── Chart builders ────────────────────────────────────────────────────


def kpi_chart(kpis_a: dict, kpis_b: dict) -> str:
    """Bar chart: additive vs bucket on key KPIs. Two y-axes are not needed
    because each metric is on its own row inside a grouped bar."""
    metrics = [
        ("Rank-1 %", "rank1_pct"),
        ("Avg Rank", "avg_rank"),
        ("Quality %", "quality_pct"),
        ("Avg Dest UTIL %", "avg_util_at_dest"),
        ("Avg Dest IN_SERVICE", "avg_in_service_at_dest"),
        ("Avg Dest RENTED", "avg_rented_at_dest"),
        ("Avg Dist / VIN (mi)", "avg_distance_per_assigned"),
        ("HHI × 100", None),  # rescaled for chart readability
    ]
    labels = [m[0] for m in metrics]

    def value(kpis, name, label):
        if label == "HHI × 100":
            return kpis["hhi"] * 100
        return kpis.get(name, 0.0)

    vals_a = [value(kpis_a, m[1], m[0]) for m in metrics]
    vals_b = [value(kpis_b, m[1], m[0]) for m in metrics]

    data = [
        {"type": "bar", "name": "Additive", "x": labels, "y": vals_a,
         "marker": {"color": COLOR_ADDITIVE},
         "text": [f"{v:.1f}" for v in vals_a], "textposition": "outside"},
        {"type": "bar", "name": "Bucket", "x": labels, "y": vals_b,
         "marker": {"color": COLOR_BUCKET},
         "text": [f"{v:.1f}" for v in vals_b], "textposition": "outside"},
    ]
    layout = {
        "title": "KPI side-by-side · same 50-vehicle batch, seed 42",
        "barmode": "group",
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "value"},
        "xaxis": {**DARK_LAYOUT["xaxis"], "tickangle": -22},
        "height": 420,
    }
    return chart_div("chart-kpi", 420) + chart_script("chart-kpi", data, layout)


def rank_dist_chart(rc_a: dict, rc_b: dict) -> str:
    cats = ["Rank 1", "Rank 2", "Rank 3", "Rank 4+", "Deferred"]
    keys = ["r1", "r2", "r3", "r4_plus", "deferred"]
    a = [rc_a[k] for k in keys]
    b = [rc_b[k] for k in keys]
    data = [
        {"type": "bar", "name": "Additive", "x": cats, "y": a,
         "marker": {"color": COLOR_ADDITIVE},
         "text": a, "textposition": "outside"},
        {"type": "bar", "name": "Bucket", "x": cats, "y": b,
         "marker": {"color": COLOR_BUCKET},
         "text": b, "textposition": "outside"},
    ]
    layout = {
        "title": "Rank distribution · how many cars landed at each rank",
        "barmode": "group",
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "vehicles"},
        "height": 360,
    }
    return chart_div("chart-rank", 360) + chart_script("chart-rank", data, layout)


def per_dealer_chart(df_a: pd.DataFrame, df_b: pd.DataFrame) -> str:
    """Top-15 dealers by count under each mode, displayed side by side."""
    top_n = 15
    counts_a = df_a["dealer_code"].value_counts().head(top_n)
    counts_b = df_b["dealer_code"].value_counts().head(top_n)
    dealers = sorted(set(counts_a.index) | set(counts_b.index))
    a = [int(counts_a.get(d, 0)) for d in dealers]
    b = [int(counts_b.get(d, 0)) for d in dealers]
    data = [
        {"type": "bar", "name": "Additive", "x": dealers, "y": a,
         "marker": {"color": COLOR_ADDITIVE}},
        {"type": "bar", "name": "Bucket", "x": dealers, "y": b,
         "marker": {"color": COLOR_BUCKET}},
    ]
    layout = {
        "title": "Per-dealer load · counts by destination",
        "barmode": "group",
        "xaxis": {**DARK_LAYOUT["xaxis"], "tickangle": -45},
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "vehicles routed"},
        "height": 420,
    }
    return chart_div("chart-dealer", 420) + chart_script("chart-dealer", data, layout)


def distance_chart(df_a: pd.DataFrame, df_b: pd.DataFrame) -> str:
    """Per-vehicle distance distribution. Box plot reveals tail and median."""
    data = [
        {"type": "box", "name": "Additive", "y": df_a["distance"].tolist(),
         "marker": {"color": COLOR_ADDITIVE}, "boxpoints": "outliers"},
        {"type": "box", "name": "Bucket", "y": df_b["distance"].tolist(),
         "marker": {"color": COLOR_BUCKET}, "boxpoints": "outliers"},
    ]
    layout = {
        "title": "Per-vehicle shipping distance · box + outliers",
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "miles"},
        "height": 360,
    }
    return chart_div("chart-dist", 360) + chart_script("chart-dist", data, layout)


def quality_scatter(df_a: pd.DataFrame, df_b: pd.DataFrame) -> str:
    """Destination IN_SERVICE × UTIL_RATE scatter, one mark per assignment."""
    data = [
        {"type": "scatter", "mode": "markers", "name": "Additive",
         "x": df_a["in_service"].tolist(),
         "y": df_a["utilization"].tolist(),
         "marker": {"color": COLOR_ADDITIVE, "size": 9, "opacity": 0.7,
                    "line": {"color": "#0d1117", "width": 1}},
         "text": [f"{c} · {s}" for c, s in zip(df_a["dealer_code"], df_a["state"])],
         "hovertemplate": "Additive<br>%{text}<br>IN_SERVICE=%{x}<br>UTIL=%{y:.1f}%<extra></extra>"},
        {"type": "scatter", "mode": "markers", "name": "Bucket",
         "x": df_b["in_service"].tolist(),
         "y": df_b["utilization"].tolist(),
         "marker": {"color": COLOR_BUCKET, "size": 9, "opacity": 0.7,
                    "line": {"color": "#0d1117", "width": 1}},
         "text": [f"{c} · {s}" for c, s in zip(df_b["dealer_code"], df_b["state"])],
         "hovertemplate": "Bucket<br>%{text}<br>IN_SERVICE=%{x}<br>UTIL=%{y:.1f}%<extra></extra>"},
    ]
    layout = {
        "title": "Destination quality · IN_SERVICE × UTIL_RATE per assignment",
        "xaxis": {**DARK_LAYOUT["xaxis"], "title": "Dealer IN_SERVICE (cars on lot)"},
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "Dealer UTIL %"},
        "height": 420,
    }
    return chart_div("chart-quality", 420) + chart_script("chart-quality", data, layout)


def tier_chart(df_b: pd.DataFrame, breakpoints) -> str:
    """For bucket mode, count cars landing in each tier."""
    # Tier is recomputed from the destination's IN_SERVICE relative to global max.
    max_in_service = max(df_b["in_service"].max(), 1.0)
    df_b = df_b.copy()
    df_b["tier"] = df_b["in_service"].apply(lambda x: tier_of(float(x), max_in_service, 4))
    by_tier = df_b["tier"].value_counts().sort_index()
    labels = [f"Bucket {chr(65+i)}\n({breakpoints[i]:.0f}-{breakpoints[i-1] if i>0 else max_in_service:.0f})"
              if i > 0 else f"Bucket A\n(>{breakpoints[0]:.0f})"
              for i in range(4)]
    counts = [int(by_tier.get(i, 0)) for i in range(4)]
    colors = ["#66bb6a", "#26c6da", "#ffb74d", "#ef5350"]
    data = [{
        "type": "bar",
        "x": labels,
        "y": counts,
        "marker": {"color": colors},
        "text": counts,
        "textposition": "outside",
    }]
    layout = {
        "title": "Bucket mode · destination tier breakdown",
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "vehicles routed"},
        "height": 380,
        "showlegend": False,
    }
    return chart_div("chart-tier", 380) + chart_script("chart-tier", data, layout)


def diff_table(df_a: pd.DataFrame, df_b: pd.DataFrame) -> str:
    """Rows where additive and bucket picked different dealers."""
    merged = df_a.merge(df_b, on="vin", suffixes=("_a", "_b"))
    diffs = merged[merged["dealer_code_a"] != merged["dealer_code_b"]]
    if diffs.empty:
        return ('<p style="color:#6b7b8d;font-size:0.85rem;padding:12px">'
                'No VIN-level disagreements — both modes picked the same dealer for every assigned vehicle.</p>')
    rows = []
    rows.append("<thead><tr>"
                "<th>VIN</th><th>Source</th>"
                "<th>Additive →</th><th>IN_SERVICE</th><th>Dist</th>"
                "<th>Bucket →</th><th>IN_SERVICE</th><th>Dist</th>"
                "<th>Δ Dist (mi)</th>"
                "</tr></thead><tbody>")
    for _, r in diffs.iterrows():
        d_diff = r["distance_b"] - r["distance_a"]
        cls = "delta-neg" if d_diff > 50 else ("delta-pos" if d_diff < -50 else "")
        rows.append(
            f"<tr><td>{r['vin'][-6:]}</td><td>{r['source_a']}</td>"
            f"<td class='dealer-a'>{r['dealer_code_a']}</td>"
            f"<td>{int(r['in_service_a'])}</td>"
            f"<td>{r['distance_a']:.0f}</td>"
            f"<td class='dealer-b'>{r['dealer_code_b']}</td>"
            f"<td>{int(r['in_service_b'])}</td>"
            f"<td>{r['distance_b']:.0f}</td>"
            f"<td class='{cls}'>{'+' if d_diff >= 0 else ''}{d_diff:.0f}</td></tr>"
        )
    rows.append("</tbody>")
    n = len(diffs)
    pct = n / max(len(merged), 1) * 100
    return (
        f'<p style="color:#aab4c0;font-size:0.86rem;margin:6px 0 12px">'
        f'{n} of {len(merged)} assigned VINs ({pct:.0f}%) routed to different dealers under the two modes.</p>'
        f'<table class="diff-table">{"".join(rows)}</table>'
    )


# ── Top-level renderer ────────────────────────────────────────────────


def render_html(n_batch: int, seed: int, out_path: Path) -> None:
    # Run both modes on the same VIN list. solve_weekly_batch and
    # solve_weekly_bucket internally use np.random.RandomState(seed), so a
    # bare seed gives matching sampling — verified by comparing VIN sets.
    print(f"[{datetime.now():%H:%M:%S}] running additive on n_batch={n_batch}, seed={seed} ...")
    additive = engine.solve_weekly_batch(n_batch=n_batch, seed=seed)
    print(f"[{datetime.now():%H:%M:%S}] running bucket on n_batch={n_batch}, seed={seed} ...")
    bucket = bucket_pipeline.solve_weekly_bucket(n_batch=n_batch, seed=seed)

    # Sanity: the two runs must operate on the same vehicle set.
    vins_a = sorted(v["vin"] for v in additive["vehicles"])
    vins_b = sorted(v["vin"] for v in bucket["vehicles"])
    if vins_a != vins_b:
        raise RuntimeError(
            f"Vehicle sets differ between modes — "
            f"additive {len(vins_a)} vs bucket {len(vins_b)} VINs"
        )

    # Build a dealer IN_SERVICE lookup so additive rows can be enriched
    # with the field they need for the destination-quality scatter, even
    # though additive scoring itself does not surface IN_SERVICE.
    baseline = engine.load_baseline()
    dealer_in_service = baseline["dealer"].set_index("DEALER_CODE")["IN_SERVICE"].fillna(0).to_dict()

    df_a = per_vehicle_rows(additive, "additive", dealer_in_service)
    df_b = per_vehicle_rows(bucket, "bucket", dealer_in_service)
    kpis_a = kpis_from_result(additive, df_a)
    kpis_b = kpis_from_result(bucket, df_b)
    rc_a = rank_counts(df_a, additive["batch_size"])
    rc_b = rank_counts(df_b, bucket["batch_size"])

    # Bucket tier cutoffs from the dealer master (max IN_SERVICE drives the tier
    # boundaries). For reporting we recompute against the destinations seen.
    max_in_service = float(max(df_a["in_service"].max() if not df_a.empty else 0.0,
                                df_b["in_service"].max() if not df_b.empty else 0.0,
                                1.0))
    bps = tier_breakpoints(max_in_service, 4)

    summary_card = render_summary_card(kpis_a, kpis_b, additive, bucket)

    headline_charts = (
        kpi_chart(kpis_a, kpis_b) +
        rank_dist_chart(rc_a, rc_b)
    )
    middle_charts = (
        per_dealer_chart(df_a, df_b) +
        distance_chart(df_a, df_b)
    )
    deep_charts = (
        quality_scatter(df_a, df_b) +
        tier_chart(df_b, bps)
    )
    diffs_html = diff_table(df_a, df_b)

    html = HTML_TEMPLATE.format(
        title=f"Bucket vs Additive · n={n_batch}, seed={seed}",
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M PT"),
        mults=", ".join(f"{m:.4f}" for m in DEFAULT_BUCKET_MULTS),
        summary=summary_card,
        headline_charts=headline_charts,
        middle_charts=middle_charts,
        deep_charts=deep_charts,
        diff_table=diffs_html,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[{datetime.now():%H:%M:%S}] wrote {out_path}")
    print(f"  additive n_assigned = {kpis_a['n_assigned']} · rank1 = {kpis_a['rank1_pct']:.1f}%")
    print(f"  bucket   n_assigned = {kpis_b['n_assigned']} · rank1 = {kpis_b['rank1_pct']:.1f}%")


def render_summary_card(kpis_a, kpis_b, result_a, result_b) -> str:
    """Compact top-row summary so the headline numbers are visible without scrolling."""
    def kpi_block(label, val_a, val_b, fmt="{:.1f}", unit="", higher_better=True):
        delta = val_b - val_a
        cls = ("delta-pos" if (delta > 0) == higher_better else "delta-neg") if delta != 0 else ""
        delta_str = ("+" if delta > 0 else "") + fmt.format(delta) + unit if delta != 0 else "—"
        return (
            f'<div class="kpi-block">'
            f'<div class="kpi-label">{label}</div>'
            f'<div class="kpi-row"><span class="kpi-sub additive">Additive</span><span class="kpi-val">{fmt.format(val_a)}{unit}</span></div>'
            f'<div class="kpi-row"><span class="kpi-sub bucket">Bucket</span><span class="kpi-val">{fmt.format(val_b)}{unit}</span></div>'
            f'<div class="kpi-delta {cls}">Δ {delta_str}</div>'
            f'</div>'
        )

    return (
        '<div class="kpi-grid">'
        + kpi_block("Assigned", kpis_a["n_assigned"], kpis_b["n_assigned"], "{:.0f}", "", True)
        + kpi_block("Rank-1 %", kpis_a["rank1_pct"], kpis_b["rank1_pct"], "{:.1f}", "%", True)
        + kpi_block("Avg Rank", kpis_a["avg_rank"], kpis_b["avg_rank"], "{:.2f}", "", False)
        + kpi_block("Avg Dest UTIL", kpis_a["avg_util_at_dest"], kpis_b["avg_util_at_dest"], "{:.1f}", "%", True)
        + kpi_block("Avg Dest IN_SERVICE", kpis_a["avg_in_service_at_dest"], kpis_b["avg_in_service_at_dest"], "{:.1f}", "", True)
        + kpi_block("Avg Dist / VIN", kpis_a["avg_distance_per_assigned"], kpis_b["avg_distance_per_assigned"], "{:.0f}", " mi", False)
        + kpi_block("Total Distance", kpis_a["total_distance"], kpis_b["total_distance"], "{:.0f}", " mi", False)
        + kpi_block("HHI", kpis_a["hhi"], kpis_b["hhi"], "{:.4f}", "", False)
        + '</div>'
    )


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {{
    --bg: #06080c; --surface: #0d1117; --card: rgba(255,255,255,0.03);
    --border: rgba(255,255,255,0.08); --text: #e0e4ea; --muted: #8a97a8;
    --additive: #00d4ff; --bucket: #66bb6a;
    --pos: #00e676; --neg: #ff5252;
  }}
  body {{ margin: 0; padding: 24px 40px; background: var(--bg); color: var(--text);
         font: 14px/1.5 Inter, system-ui, sans-serif; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px 0; font-weight: 700; }}
  h2 {{ font-size: 1.05rem; margin: 32px 0 8px 0; font-weight: 600;
        color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }}
  .sub {{ color: var(--muted); font-size: 0.86rem; margin-bottom: 24px; }}
  .panel {{ background: var(--card); border: 1px solid var(--border);
            border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
               gap: 12px; }}
  .kpi-block {{ background: var(--card); border: 1px solid var(--border);
                border-radius: 10px; padding: 12px 14px; }}
  .kpi-label {{ font-size: 0.75rem; text-transform: uppercase;
                letter-spacing: 0.5px; color: var(--muted); margin-bottom: 8px; }}
  .kpi-row {{ display: flex; justify-content: space-between; align-items: baseline;
              margin: 4px 0; }}
  .kpi-sub {{ font-size: 0.72rem; }}
  .kpi-sub.additive {{ color: var(--additive); }}
  .kpi-sub.bucket {{ color: var(--bucket); }}
  .kpi-val {{ font-weight: 600; font-size: 0.95rem; }}
  .kpi-delta {{ font-size: 0.78rem; margin-top: 8px; color: var(--muted); }}
  .delta-pos {{ color: var(--pos) !important; }}
  .delta-neg {{ color: var(--neg) !important; }}
  table.diff-table {{ border-collapse: collapse; font-size: 0.78rem; width: 100%; }}
  table.diff-table th, table.diff-table td {{ padding: 6px 10px; text-align: left;
                                              border-bottom: 1px solid var(--border); }}
  table.diff-table th {{ color: var(--muted); font-weight: 500;
                         text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.4px; }}
  .dealer-a {{ color: var(--additive); }}
  .dealer-b {{ color: var(--bucket); }}
  .legend {{ font-size: 0.78rem; color: var(--muted); margin-top: 6px; }}
  .legend .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                  vertical-align: -1px; margin-right: 4px; }}
  .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media (max-width: 1100px) {{ .row {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="sub">
    Generated {timestamp} · same batch, same seed, two scoring strategies ·
    bucket_mults = [{mults}] · w_dist = 15.0 · w_tax = 1.95
    <div class="legend">
      <span><span class="dot" style="background:#00d4ff"></span>Additive (production default)</span>
      <span style="margin-left:18px"><span class="dot" style="background:#66bb6a"></span>Bucket (2026-05-22 calibration)</span>
    </div>
  </div>

  <h2>Headline KPIs</h2>
  <div class="panel">{summary}</div>

  <h2>Headline Charts</h2>
  <div class="row">{headline_charts}</div>

  <h2>Where Did the Cars Go?</h2>
  <div class="row">{middle_charts}</div>

  <h2>Destination Quality &amp; Bucket Tier Breakdown</h2>
  <div class="row">{deep_charts}</div>

  <h2>VIN-Level Disagreement</h2>
  <div class="panel">{diff_table}</div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-batch", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=str, default="docs/viz/bucket_vs_additive.html")
    args = parser.parse_args()
    render_html(args.n_batch, args.seed, ROOT / args.output)


if __name__ == "__main__":
    main()
