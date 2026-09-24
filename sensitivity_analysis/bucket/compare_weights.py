"""Side-by-side comparison: manual bucket weights vs calibrated weights.

Runs bucket mode twice with different `bucket_mults` on the same vehicle
batch, then emits an HTML report with embedded Plotly charts.

The motivation is to see how much the calibrated `[3.72, 2.88, 0.80, 0.65]`
actually differs in practice from a hand-picked monotone vector like
`[2.0, 1.5, 1.0, 0.5]`. If allocations are nearly identical, the
calibration is mostly cosmetic and a simpler manual vector is a fine
substitute.

Charts
------
1. KPI bar chart
2. Per-dealer load (top 15 by either)
3. Per-vehicle distance distribution
4. Bucket tier breakdown (side by side)
5. VIN-level diff table

Output
------
    docs/viz/bucket_weights_comparison.html
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "app"))

import engine  # noqa: E402
import bucket_pipeline  # noqa: E402
from scoring import BucketParams, tier_of, tier_breakpoints  # noqa: E402


# ── Chart helpers (mirror compare_modes.py style) ─────────────────────

DARK_LAYOUT = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(13,17,23,0.6)",
    "font": {"family": "Inter, system-ui, sans-serif", "size": 12, "color": "#e0e4ea"},
    "margin": {"l": 56, "r": 24, "t": 44, "b": 56},
    "xaxis": {"gridcolor": "rgba(255,255,255,0.06)", "zerolinecolor": "rgba(255,255,255,0.1)"},
    "yaxis": {"gridcolor": "rgba(255,255,255,0.06)", "zerolinecolor": "rgba(255,255,255,0.1)"},
    "legend": {"bgcolor": "rgba(0,0,0,0)", "bordercolor": "rgba(255,255,255,0.1)"},
}
COLOR_MANUAL = "#ffb74d"       # amber for the simple hand-picked vector
COLOR_CALIB = "#66bb6a"        # green for the Sobol-calibrated vector
COLOR_ADDITIVE = "#00d4ff"     # cyan for the additive (production-baseline) form


def chart_div(div_id: str, height: int = 360) -> str:
    return f'<div id="{div_id}" style="width:100%;height:{height}px;"></div>'


def chart_script(div_id: str, data: list, layout: dict) -> str:
    merged_layout = {**DARK_LAYOUT, **layout}
    payload = json.dumps({"data": data, "layout": merged_layout},
                         default=lambda o: float(o) if hasattr(o, "__float__") else str(o))
    return (
        f"<script>(function(){{var p={payload};"
        f"Plotly.newPlot('{div_id}', p.data, p.layout, "
        "{displayModeBar:false, responsive:true});})();</script>"
    )


# ── Aggregations ──────────────────────────────────────────────────────


def per_vehicle_rows(result: dict, label: str, dealer_in_service: dict | None = None) -> pd.DataFrame:
    rows = []
    for v in result.get("vehicles", []):
        a = v.get("assigned")
        if not a:
            continue
        dealer_code = a.get("dealer_code")
        in_service = a.get("in_service")
        if in_service is None and dealer_in_service is not None:
            in_service = dealer_in_service.get(dealer_code, 0.0)
        rows.append({
            "label": label,
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


def kpis(result: dict, df: pd.DataFrame) -> dict:
    n = int(len(df))
    n_high_util = int((df["utilization"] > 80).sum()) if not df.empty else 0
    return {
        "n_assigned": result.get("n_assigned", 0),
        "batch_size": result.get("batch_size", 0),
        "rank1_pct": result.get("rank1_pct", 0.0),
        "avg_rank": result.get("avg_rank", 0.0),
        "total_distance": result.get("total_distance", 0.0),
        "avg_util_at_dest": float(df["utilization"].mean()) if not df.empty else 0.0,
        "avg_in_service_at_dest": float(df["in_service"].mean()) if not df.empty else 0.0,
        "avg_rented_at_dest": float(df["rented"].mean()) if not df.empty else 0.0,
        "avg_distance_per_assigned": float(df["distance"].mean()) if not df.empty else 0.0,
        "hhi": _hhi(df),
        "n_high_util_dest": n_high_util,
        "pct_high_util_dest": (n_high_util / n * 100) if n else 0.0,
    }


def _hhi(df: pd.DataFrame) -> float:
    n = len(df)
    if n == 0:
        return 0.0
    counts = df["dealer_code"].value_counts()
    shares = counts / n
    return float((shares ** 2).sum())


def rank_counts(df: pd.DataFrame, total_n: int) -> dict:
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


# ── Chart builders ────────────────────────────────────────────────────


def kpi_chart(series: list) -> str:
    """series = [(label, color, kpis_dict), ...] — supports N runs."""
    metrics = [
        ("Rank-1 %", "rank1_pct"),
        ("Avg Rank", "avg_rank"),
        ("Dest UTIL > 80% rate", "pct_high_util_dest"),
        ("Avg Dest UTIL %", "avg_util_at_dest"),
        ("Avg Dest IN_SERVICE", "avg_in_service_at_dest"),
        ("Avg Dest RENTED", "avg_rented_at_dest"),
        ("Avg Dist / VIN (mi)", "avg_distance_per_assigned"),
        ("HHI × 100", None),
    ]
    labels = [m[0] for m in metrics]

    def value(k, name, label):
        return k["hhi"] * 100 if label == "HHI × 100" else k.get(name, 0.0)

    data = []
    for label, color, k in series:
        vals = [value(k, m[1], m[0]) for m in metrics]
        data.append({"type": "bar", "name": label, "x": labels, "y": vals,
                     "marker": {"color": color},
                     "text": [f"{v:.1f}" for v in vals], "textposition": "outside"})
    layout = {
        "title": f"KPI side-by-side · same batch, {len(series)} configurations",
        "barmode": "group",
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "value"},
        "xaxis": {**DARK_LAYOUT["xaxis"], "tickangle": -22},
        "height": 460,
    }
    return chart_div("chart-kpi", 460) + chart_script("chart-kpi", data, layout)


def rank_dist_chart(series: list) -> str:
    """series = [(label, color, rank_counts_dict), ...]"""
    cats = ["Rank 1", "Rank 2", "Rank 3", "Rank 4+", "Deferred"]
    keys = ["r1", "r2", "r3", "r4_plus", "deferred"]
    data = []
    for label, color, rc in series:
        vals = [rc[k] for k in keys]
        data.append({"type": "bar", "name": label, "x": cats, "y": vals,
                     "marker": {"color": color}, "text": vals, "textposition": "outside"})
    layout = {
        "title": "Rank distribution (intra-mode — each algorithm's own ranking of its candidates)",
        "barmode": "group",
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "vehicles"},
        "height": 360,
    }
    return chart_div("chart-rank", 360) + chart_script("chart-rank", data, layout)


def per_dealer_chart(series: list) -> str:
    """series = [(label, color, df), ...]"""
    top_n = 15
    all_dealers = set()
    counts_by_label = {}
    for label, _color, df in series:
        c = df["dealer_code"].value_counts().head(top_n)
        counts_by_label[label] = c
        all_dealers |= set(c.index)
    dealers = sorted(all_dealers)
    data = []
    for label, color, _df in series:
        c = counts_by_label[label]
        data.append({"type": "bar", "name": label, "x": dealers,
                     "y": [int(c.get(d, 0)) for d in dealers],
                     "marker": {"color": color}})
    layout = {
        "title": "Per-dealer load",
        "barmode": "group",
        "xaxis": {**DARK_LAYOUT["xaxis"], "tickangle": -45},
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "vehicles routed"},
        "height": 440,
    }
    return chart_div("chart-dealer", 440) + chart_script("chart-dealer", data, layout)


def distance_chart(series: list) -> str:
    """series = [(label, color, df), ...]"""
    data = []
    for label, color, df in series:
        data.append({"type": "box", "name": label, "y": df["distance"].tolist(),
                     "marker": {"color": color}, "boxpoints": "outliers"})
    layout = {
        "title": "Per-vehicle shipping distance",
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "miles"},
        "height": 360,
    }
    return chart_div("chart-dist", 360) + chart_script("chart-dist", data, layout)


def tier_chart(series: list, max_in_service) -> str:
    """series = [(label, color, df), ...]"""
    def by_tier(df):
        if df.empty:
            return [0, 0, 0, 0]
        t = df["in_service"].apply(lambda x: tier_of(float(x), max_in_service, 4))
        return [int((t == i).sum()) for i in range(4)]
    tiers = ["A", "B", "C", "D"]
    data = []
    for label, color, df in series:
        vals = by_tier(df)
        data.append({"type": "bar", "name": label, "x": tiers, "y": vals,
                     "marker": {"color": color}, "text": vals, "textposition": "outside"})
    layout = {
        "title": "Destination tier breakdown (where assigned vehicles landed by IN_SERVICE tier)",
        "barmode": "group",
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "vehicles"},
        "height": 360,
    }
    return chart_div("chart-tier", 360) + chart_script("chart-tier", data, layout)


def score_chart(series: list) -> str:
    """series = [(label, color, df), ...] — note: alloc_scores are NOT cross-comparable
    between configurations because each mode has its own scoring formula."""
    data = []
    for label, color, df in series:
        data.append({"type": "histogram", "name": label, "x": df["alloc_score"].tolist(),
                     "marker": {"color": color}, "opacity": 0.55, "nbinsx": 24})
    layout = {
        "title": "Per-vehicle alloc_score (NOT cross-comparable — each mode has its own formula)",
        "barmode": "overlay",
        "xaxis": {**DARK_LAYOUT["xaxis"], "title": "alloc_score"},
        "yaxis": {**DARK_LAYOUT["yaxis"], "title": "vehicles"},
        "height": 360,
    }
    return chart_div("chart-score", 360) + chart_script("chart-score", data, layout)


def diff_table(series: list, max_in_service: float) -> str:
    """series = [(label, color, df), ...] with len >= 2.

    Shows VINs where at least two configs disagreed on the destination dealer.
    """
    if len(series) < 2:
        return ""
    # Build a wide table: one row per VIN, one column per config
    by_vin = {}
    for label, _color, df in series:
        for _, r in df.iterrows():
            by_vin.setdefault(r["vin"], {"source": r["source"]})
            by_vin[r["vin"]][label] = {
                "dealer": r["dealer_code"],
                "in_service": r["in_service"],
                "distance": r["distance"],
            }
    diff_vins = []
    labels = [s[0] for s in series]
    for vin, row in by_vin.items():
        dealers = {row.get(l, {}).get("dealer") for l in labels}
        if len(dealers) > 1:
            diff_vins.append(vin)
    if not diff_vins:
        return ('<p style="color:#6b7b8d;font-size:0.85rem;padding:12px">'
                'No VIN-level disagreements — all configurations routed every vehicle to the same dealer.</p>')

    header = "<thead><tr><th>VIN</th><th>Source</th>"
    for label, _color, _df in series:
        header += f"<th>{label} →</th><th>Tier</th><th>Dist</th>"
    header += "</tr></thead><tbody>"

    body_rows = []
    cls_map = {0: "dealer-m", 1: "dealer-c", 2: "dealer-a"}
    for vin in diff_vins:
        row = by_vin[vin]
        cells = [f"<td>{vin[-6:]}</td><td>{row['source']}</td>"]
        for idx, (label, _color, _df) in enumerate(series):
            entry = row.get(label, {})
            d = entry.get("dealer", "—")
            tier = chr(65 + tier_of(float(entry.get("in_service", 0)), max_in_service, 4)) if entry else "—"
            dist = f"{entry.get('distance', 0):.0f}" if entry else "—"
            cls = cls_map.get(idx, "")
            cells.append(f"<td class='{cls}'>{d}</td><td>{tier}</td><td>{dist}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    body = "".join(body_rows) + "</tbody>"

    n = len(diff_vins); total = len(by_vin); pct = n / max(total, 1) * 100
    return (
        f'<p style="color:#aab4c0;font-size:0.86rem;margin:6px 0 12px">'
        f'{n} of {total} assigned VINs ({pct:.0f}%) routed to different dealers under at least one configuration.</p>'
        f'<table class="diff-table">{header}{body}</table>'
    )


# ── Top-level renderer ────────────────────────────────────────────────


_KPI_FIELDS = [
    ("Assigned", "n_assigned", "{:.0f}", ""),
    ("Rank-1 %", "rank1_pct", "{:.1f}", "%"),
    ("Avg Rank", "avg_rank", "{:.2f}", ""),
    ("Dest UTIL > 80% count", "n_high_util_dest", "{:.0f}", ""),
    ("Dest UTIL > 80% rate", "pct_high_util_dest", "{:.1f}", "%"),
    ("Avg Dest UTIL", "avg_util_at_dest", "{:.1f}", "%"),
    ("Avg Dest IN_SERVICE", "avg_in_service_at_dest", "{:.1f}", ""),
    ("Avg Dist / VIN", "avg_distance_per_assigned", "{:.0f}", " mi"),
    ("Total Distance", "total_distance", "{:.0f}", " mi"),
    ("HHI", "hhi", "{:.4f}", ""),
]


def render_summary(series: list) -> str:
    """series = [(label, color_class, kpis_dict), ...] — one column per config."""
    blocks = []
    for field_label, key, fmt, unit in _KPI_FIELDS:
        rows = ""
        for label, color_class, k in series:
            v = k.get(key, 0.0)
            rows += (f'<div class="kpi-row">'
                     f'<span class="kpi-sub {color_class}">{label}</span>'
                     f'<span class="kpi-val">{fmt.format(v)}{unit}</span>'
                     f'</div>')
        blocks.append(
            f'<div class="kpi-block">'
            f'<div class="kpi-label">{field_label}</div>'
            f'{rows}'
            f'</div>'
        )
    return '<div class="kpi-grid">' + "".join(blocks) + '</div>'


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
    --manual: #ffb74d; --calib: #66bb6a; --additive: #00d4ff;
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
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
               gap: 12px; }}
  .kpi-block {{ background: var(--card); border: 1px solid var(--border);
                border-radius: 10px; padding: 12px 14px; }}
  .kpi-label {{ font-size: 0.75rem; text-transform: uppercase;
                letter-spacing: 0.5px; color: var(--muted); margin-bottom: 8px; }}
  .kpi-row {{ display: flex; justify-content: space-between; align-items: baseline; margin: 4px 0; }}
  .kpi-sub {{ font-size: 0.72rem; }}
  .kpi-sub.manual {{ color: var(--manual); }}
  .kpi-sub.calib {{ color: var(--calib); }}
  .kpi-sub.additive {{ color: var(--additive); }}
  .kpi-val {{ font-weight: 600; font-size: 0.95rem; }}
  table.diff-table {{ border-collapse: collapse; font-size: 0.78rem; width: 100%; }}
  table.diff-table th, table.diff-table td {{ padding: 6px 10px; text-align: left;
                                              border-bottom: 1px solid var(--border); }}
  table.diff-table th {{ color: var(--muted); font-weight: 500;
                         text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.4px; }}
  .dealer-m {{ color: var(--manual); }}
  .dealer-c {{ color: var(--calib); }}
  .dealer-a {{ color: var(--additive); }}
  .legend {{ font-size: 0.78rem; color: var(--muted); margin-top: 6px;
             display: flex; flex-wrap: wrap; gap: 16px; }}
  .legend .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%;
                  vertical-align: -1px; margin-right: 4px; }}
  .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  @media (max-width: 1100px) {{ .row {{ grid-template-columns: 1fr; }} }}
  .mults {{ font-family: ui-monospace, SFMono-Regular, monospace; padding: 2px 6px;
            background: rgba(255,255,255,0.05); border-radius: 4px; }}
</style>
</head>
<body>
  <h1>{title}</h1>
  <div class="sub">
    Generated {timestamp} · same batch, three configurations · w_dist = 15.0 · w_tax = 1.95
    <div class="legend">{legend}</div>
  </div>

  <h2>Headline KPIs</h2>
  <div class="panel">{summary}</div>

  <h2>Headline Charts</h2>
  <div class="row">{chart1}{chart2}</div>

  <h2>Where Did the Cars Go?</h2>
  <div class="row">{chart3}{chart4}</div>

  <h2>Tier Mix &amp; Score Distribution</h2>
  <div class="row">{chart5}{chart6}</div>

  <h2>VIN-Level Disagreement</h2>
  <div class="panel">{diff_table}</div>
</body>
</html>
"""


def render(mults_m, mults_c, n_batch, seed, out_path: Path) -> None:
    label_m = "Manual bucket"
    label_c = "Calibrated bucket"
    label_a = "Additive (ILP)"
    pm = BucketParams(bucket_mults=tuple(mults_m))
    pc = BucketParams(bucket_mults=tuple(mults_c))

    print(f"[{datetime.now():%H:%M:%S}] running 3 configs on n_batch={n_batch}, seed={seed} ...")

    rm = bucket_pipeline.solve_weekly_bucket(
        n_batch=n_batch, seed=seed,
        bucket_mults=list(mults_m), w_dist=pm.w_dist, w_tax=pm.w_tax,
    )
    rc = bucket_pipeline.solve_weekly_bucket(
        n_batch=n_batch, seed=seed,
        bucket_mults=list(mults_c), w_dist=pc.w_dist, w_tax=pc.w_tax,
    )
    ra = engine.solve_weekly_batch(n_batch=n_batch, seed=seed)

    # Sanity: same VIN set across the three runs (same seed + same n)
    vins = [sorted(v["vin"] for v in r["vehicles"]) for r in (rm, rc, ra)]
    if not (vins[0] == vins[1] == vins[2]):
        raise RuntimeError("vehicle sets differ across the three runs")

    # Additive's `assigned` block omits IN_SERVICE; fall back to the dealer master.
    baseline = engine.load_baseline()
    dealer_in_service = baseline["dealer"].set_index("DEALER_CODE")["IN_SERVICE"].fillna(0).to_dict()

    dm = per_vehicle_rows(rm, label_m)
    dc = per_vehicle_rows(rc, label_c)
    da = per_vehicle_rows(ra, label_a, dealer_in_service=dealer_in_service)
    km = kpis(rm, dm)
    kc = kpis(rc, dc)
    ka = kpis(ra, da)
    rcm = rank_counts(dm, rm["batch_size"])
    rcc = rank_counts(dc, rc["batch_size"])
    rca = rank_counts(da, ra["batch_size"])

    M = float(max(*(df["in_service"].max() if not df.empty else 0.0 for df in (dm, dc, da)), 1.0))

    chart_series_df = [
        (label_m, COLOR_MANUAL, dm),
        (label_c, COLOR_CALIB, dc),
        (label_a, COLOR_ADDITIVE, da),
    ]
    chart_series_kpi = [
        (label_m, COLOR_MANUAL, km),
        (label_c, COLOR_CALIB, kc),
        (label_a, COLOR_ADDITIVE, ka),
    ]
    chart_series_rank = [
        (label_m, COLOR_MANUAL, rcm),
        (label_c, COLOR_CALIB, rcc),
        (label_a, COLOR_ADDITIVE, rca),
    ]
    summary_series = [
        (label_m, "manual", km),
        (label_c, "calib", kc),
        (label_a, "additive", ka),
    ]

    legend = (
        f'<span><span class="dot" style="background:#ffb74d"></span>'
        f'{label_m}: <span class="mults">[{", ".join(f"{m:.4f}" for m in mults_m)}]</span></span>'
        f'<span><span class="dot" style="background:#66bb6a"></span>'
        f'{label_c}: <span class="mults">[{", ".join(f"{m:.4f}" for m in mults_c)}]</span></span>'
        f'<span><span class="dot" style="background:#00d4ff"></span>'
        f'{label_a}: <span class="mults">w_util=1.335, w_rented=0.0574, w_dist=15.0, w_tax=1.95</span></span>'
    )

    html = HTML_TEMPLATE.format(
        title=f"Manual vs Calibrated Bucket vs Additive ILP · n={n_batch}, seed={seed}",
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M PT"),
        legend=legend,
        summary=render_summary(summary_series),
        chart1=kpi_chart(chart_series_kpi),
        chart2=rank_dist_chart(chart_series_rank),
        chart3=per_dealer_chart(chart_series_df),
        chart4=distance_chart(chart_series_df),
        chart5=tier_chart(chart_series_df, M),
        chart6=score_chart(chart_series_df),
        diff_table=diff_table(chart_series_df, M),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[{datetime.now():%H:%M:%S}] wrote {out_path}")
    for label, k in ((label_m, km), (label_c, kc), (label_a, ka)):
        print(f"  {label:20s}  n_assigned={k['n_assigned']}  rank1={k['rank1_pct']:.0f}%  "
              f"avg_dist={k['avg_distance_per_assigned']:.0f}mi  HHI={k['hhi']:.4f}  "
              f"UTIL>80%: {k['n_high_util_dest']}/{k['n_assigned']} ({k['pct_high_util_dest']:.0f}%)")


def parse_mults(s: str):
    parts = [float(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"need 4 comma-separated floats, got {parts}")
    return parts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual", type=parse_mults, default=[2.0, 1.5, 1.0, 0.5],
                        help="manual bucket_mults, default 2,1.5,1,0.5")
    parser.add_argument("--calibrated", type=parse_mults, default=[3.7203, 2.8848, 0.7957, 0.6498],
                        help="calibrated bucket_mults, default = current production defaults")
    parser.add_argument("--n-batch", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=str,
                        default="docs/viz/bucket_weights_comparison.html")
    args = parser.parse_args()
    render(args.manual, args.calibrated, args.n_batch, args.seed, ROOT / args.output)


if __name__ == "__main__":
    main()
