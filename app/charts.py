"""Chart generation engine — matplotlib/seaborn → PNG bytes served via /charts/{id}."""
from __future__ import annotations

import io
import uuid
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CHART_DIR = Path("/tmp/charts")

_PALETTES = {
    "default": ["#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3", "#937860", "#DA8BC3", "#CCB974"],
    "blue":    ["#08519c", "#3182bd", "#6baed6", "#9ecae1", "#c6dbef"],
    "green":   ["#006d2c", "#31a354", "#74c476", "#a1d99b", "#c7e9c0"],
    "warm":    ["#d73027", "#f46d43", "#fdae61", "#fee090", "#ffffbf", "#e0f3f8"],
    "pastel":  ["#aec6cf", "#ffb347", "#b5ead7", "#ff9aa2", "#c7ceea", "#ffdac1"],
}


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "axes.grid": True,
        "grid.alpha": 0.3,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })
    return plt, mticker


def _sns():
    import seaborn as sns
    return sns


def _auto_detect(data: Any) -> str:
    if isinstance(data, list) and data:
        if isinstance(data[0], (int, float)):
            return "histogram"
        if isinstance(data[0], dict):
            keys = set(data[0].keys())
            if "x" in keys and "y" in keys and "label" not in keys:
                return "scatter"
            value_keys = keys - {"label", "name", "x", "series", "color"}
            if len(data) <= 7 and len(value_keys) == 1:
                return "pie"
    return "bar"


def generate_chart(
    chart_type: str,
    data: Any,
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    palette: str = "default",
    width: int = 10,
    height: int = 6,
) -> tuple[str, bytes]:
    """Generate a chart. Returns (chart_id, png_bytes).

    data formats:
      bar / line / area  : [{"label": str, "value": float}]
                           or multi-series [{"label": str, "series_a": float, "series_b": float}]
      pie / donut        : [{"label": str, "value": float}]
      scatter            : [{"x": float, "y": float, "label": str?, "series": str?}]
      histogram          : [float, ...] or [{"value": float}, ...]
      heatmap            : {"x_labels": [...], "y_labels": [...], "matrix": [[...]]}
    """
    plt, mticker = _plt()
    colors = _PALETTES.get(palette, _PALETTES["default"])

    if chart_type == "auto":
        chart_type = _auto_detect(data)

    chart_type = chart_type.lower().strip()
    fig, ax = plt.subplots(figsize=(width, height))
    fig.patch.set_facecolor("#F8F9FA")
    ax.set_facecolor("#F8F9FA")

    try:
        dispatch = {
            "bar": _bar, "bar_chart": _bar,
            "line": _line, "line_chart": _line,
            "area": _area, "area_chart": _area,
            "pie": lambda ax, d, c, xl, yl: _pie(ax, d, c, False),
            "donut": lambda ax, d, c, xl, yl: _pie(ax, d, c, True),
            "pie_chart": lambda ax, d, c, xl, yl: _pie(ax, d, c, False),
            "scatter": _scatter, "scatter_plot": _scatter,
            "histogram": _histogram, "hist": _histogram,
            "funnel": _funnel, "funnel_chart": _funnel,
        }

        if chart_type == "heatmap":
            plt.close(fig)
            fig, ax = plt.subplots(figsize=(width, height))
            fig.patch.set_facecolor("#F8F9FA")
            _heatmap(fig, ax, data)
        else:
            fn = dispatch.get(chart_type, _bar)
            fn(ax, data, colors, x_label, y_label)

        if title:
            fig.suptitle(title, fontsize=13, fontweight="bold", y=1.01)

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        buf.seek(0)
        png_bytes = buf.read()
    finally:
        plt.close(fig)

    _CHART_DIR.mkdir(parents=True, exist_ok=True)
    chart_id = uuid.uuid4().hex[:12]
    (_CHART_DIR / f"{chart_id}.png").write_bytes(png_bytes)
    return chart_id, png_bytes


# ── individual draw helpers ──────────────────────────────────────────────────

def _extract_series(data: list[dict]) -> tuple[list[str], list[str], dict[str, list[float]]]:
    """Return (labels, series_keys, {key: [values]})."""
    if not data:
        return [], [], {}
    sample = data[0]
    labels = [str(d.get("label", d.get("name", d.get("x", i)))) for i, d in enumerate(data)]
    series_keys = [k for k in sample if k not in ("label", "name", "x", "color")]
    if not series_keys:
        series_keys = ["value"]
    series = {k: [float(d.get(k, 0)) for d in data] for k in series_keys}
    return labels, series_keys, series


def _bar(ax, data, colors, x_label, y_label):
    import numpy as np
    plt, mticker = _plt()
    labels, keys, series = _extract_series(data)

    if len(keys) == 1:
        vals = series[keys[0]]
        bar_colors = [colors[i % len(colors)] for i in range(len(labels))]
        bars = ax.bar(labels, vals, color=bar_colors, edgecolor="white", linewidth=0.8)
        max_v = max(vals) if vals else 1
        for bar, val in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max_v * 0.01,
                    f"{val:,.0f}", ha="center", va="bottom", fontsize=8.5)
    else:
        x = np.arange(len(labels))
        w = 0.75 / len(keys)
        for i, key in enumerate(keys):
            offset = (i - len(keys) / 2 + 0.5) * w
            ax.bar(x + offset, series[key], w, label=key,
                   color=colors[i % len(colors)], edgecolor="white")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30 if len(labels) > 6 else 0, ha="right")
        ax.legend(framealpha=0.6)

    if len(labels) > 6:
        ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=8)
    if x_label:
        ax.set_xlabel(x_label)
    if y_label:
        ax.set_ylabel(y_label)
    ax.yaxis.set_major_formatter(
        __import__("matplotlib.ticker", fromlist=["FuncFormatter"]).FuncFormatter(
            lambda v, _: f"{v:,.0f}"))


def _line(ax, data, colors, x_label, y_label):
    plt, _ = _plt()
    labels, keys, series = _extract_series(data)
    x_pos = list(range(len(labels)))

    for i, key in enumerate(keys):
        vals = series[key]
        lbl = key if len(keys) > 1 else ""
        ax.plot(x_pos, vals, color=colors[i % len(colors)], marker="o",
                linewidth=2, markersize=5, label=lbl)
        ax.fill_between(x_pos, vals, alpha=0.07, color=colors[i % len(colors)])

    step = max(1, len(labels) // 8)
    ax.set_xticks(x_pos[::step])
    ax.set_xticklabels(labels[::step], rotation=30 if len(labels) > 5 else 0, ha="right")
    if len(keys) > 1:
        ax.legend(framealpha=0.6)
    if x_label:
        ax.set_xlabel(x_label)
    if y_label:
        ax.set_ylabel(y_label)
    ax.yaxis.set_major_formatter(
        __import__("matplotlib.ticker", fromlist=["FuncFormatter"]).FuncFormatter(
            lambda v, _: f"{v:,.0f}"))


def _area(ax, data, colors, x_label, y_label):
    labels, keys, series = _extract_series(data)
    x_pos = list(range(len(labels)))

    baseline = [0.0] * len(labels)
    for i, key in enumerate(keys):
        vals = series[key]
        top = [b + v for b, v in zip(baseline, vals)]
        ax.fill_between(x_pos, baseline, top, alpha=0.65,
                        color=colors[i % len(colors)],
                        label=key if len(keys) > 1 else "")
        ax.plot(x_pos, top, color=colors[i % len(colors)], linewidth=1.5)
        baseline = top

    step = max(1, len(labels) // 8)
    ax.set_xticks(x_pos[::step])
    ax.set_xticklabels(labels[::step], rotation=30 if len(labels) > 5 else 0, ha="right")
    if len(keys) > 1:
        ax.legend(framealpha=0.6)
    if x_label:
        ax.set_xlabel(x_label)
    if y_label:
        ax.set_ylabel(y_label)


def _pie(ax, data, colors, is_donut: bool):
    import matplotlib.patches as mpatches
    if isinstance(data, list) and data and isinstance(data[0], dict):
        labels = [str(d.get("label", d.get("name", i))) for i, d in enumerate(data)]
        vals = [float(d.get("value", d.get("count", 0))) for d in data]
    else:
        labels = [str(i) for i in range(len(data))]
        vals = [float(v) for v in data]

    wedges, texts, autos = ax.pie(
        vals, labels=labels,
        colors=colors[:len(vals)],
        autopct="%1.1f%%",
        startangle=90,
        pctdistance=0.78 if is_donut else 0.62,
        wedgeprops={"linewidth": 2, "edgecolor": "white"},
    )
    for t in autos:
        t.set_fontsize(9)

    if is_donut:
        ax.add_patch(mpatches.Circle((0, 0), 0.5, color="#F8F9FA"))

    ax.set_aspect("equal")


def _scatter(ax, data, colors, x_label, y_label):
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if "series" in data[0]:
            groups: dict = {}
            for d in data:
                s = str(d.get("series", ""))
                groups.setdefault(s, {"x": [], "y": [], "lbl": []})
                groups[s]["x"].append(float(d.get("x", 0)))
                groups[s]["y"].append(float(d.get("y", 0)))
                groups[s]["lbl"].append(str(d.get("label", "")))
            for i, (name, pts) in enumerate(groups.items()):
                ax.scatter(pts["x"], pts["y"], color=colors[i % len(colors)],
                           label=name, alpha=0.75, s=65, edgecolors="white")
            ax.legend(framealpha=0.6)
        else:
            xs = [float(d.get("x", 0)) for d in data]
            ys = [float(d.get("y", 0)) for d in data]
            lbls = [str(d.get("label", "")) for d in data]
            ax.scatter(xs, ys, color=colors[0], alpha=0.75, s=65, edgecolors="white")
            for x, y, lbl in zip(xs, ys, lbls):
                if lbl:
                    ax.annotate(lbl, (x, y), xytext=(5, 5),
                                textcoords="offset points", fontsize=8)
    if x_label:
        ax.set_xlabel(x_label)
    if y_label:
        ax.set_ylabel(y_label)


def _histogram(ax, data, colors, x_label, y_label):
    if isinstance(data, list) and data:
        if isinstance(data[0], dict):
            vals = [float(d.get("value", d.get("x", 0))) for d in data]
        else:
            vals = [float(v) for v in data]
    else:
        vals = []

    ax.hist(vals, bins="auto", color=colors[0], edgecolor="white",
            linewidth=0.8, alpha=0.85)
    if x_label:
        ax.set_xlabel(x_label)
    ax.set_ylabel(y_label or "Số lượng")


def _heatmap(fig, ax, data):
    import numpy as np
    import pandas as pd
    sns = _sns()

    if isinstance(data, dict):
        matrix = np.array(data["matrix"])
        x_labels = data.get("x_labels") or data.get("cols") or list(range(matrix.shape[1]))
        y_labels = data.get("y_labels") or data.get("rows") or list(range(matrix.shape[0]))
    elif isinstance(data, list) and data and isinstance(data[0], list):
        matrix = np.array(data, dtype=float)
        x_labels = list(range(matrix.shape[1]))
        y_labels = list(range(matrix.shape[0]))
    else:
        raise ValueError("Heatmap data must be dict {matrix, x_labels, y_labels}")

    df = pd.DataFrame(matrix, index=y_labels, columns=x_labels)
    sns.heatmap(df, ax=ax, annot=True, fmt=".2g", cmap="YlOrRd",
                linewidths=0.5, cbar=True, annot_kws={"size": 9})
    ax.set_facecolor("#F8F9FA")


def _funnel(ax, data, colors, x_label, y_label):
    """Draw a marketing/product conversion funnel using trapezoid patches."""
    from matplotlib.patches import Polygon as MplPoly

    if isinstance(data, list) and data and isinstance(data[0], dict):
        labels = [str(d.get("label", d.get("name", i))) for i, d in enumerate(data)]
        vals = [float(d.get("value", d.get("count", 0))) for d in data]
    else:
        labels = [str(i) for i in range(len(data))]
        vals = [float(v) for v in data]

    n = len(vals)
    if not n:
        return

    max_val = max(vals) if vals else 1
    stage_h = 1.0   # height of each stage block
    gap = 0.18      # vertical gap between stages

    ax.set_xlim(-1.0, 1.0)
    ax.set_ylim(0, n * (stage_h + gap) + 0.05)
    ax.invert_yaxis()
    ax.axis("off")

    for i, (label, val) in enumerate(zip(labels, vals)):
        # Normalized half-widths: largest stage fills 55% of half-width
        hw_top = (val / max_val) * 0.55
        hw_bot = (vals[i + 1] / max_val) * 0.55 if i + 1 < n else hw_top * 0.5

        y_top = i * (stage_h + gap)
        y_bot = y_top + stage_h

        trap = MplPoly(
            [(-hw_top, y_top), (hw_top, y_top),
             (hw_bot, y_bot), (-hw_bot, y_bot)],
            closed=True,
            facecolor=colors[i % len(colors)],
            edgecolor="white",
            linewidth=2.5,
            alpha=0.9,
        )
        ax.add_patch(trap)

        # Label + value centered inside the stage
        ax.text(
            0, y_top + stage_h * 0.5,
            f"{label}  {val:,.0f}",
            ha="center", va="center",
            fontsize=10, fontweight="bold",
            color="white",
        )

        # Conversion / drop-off annotation on the right
        if i > 0 and vals[i - 1]:
            conv = val / vals[i - 1] * 100
            drop = 100 - conv
            ax.text(
                hw_top + 0.04,
                y_top + stage_h * 0.5,
                f"↓ {drop:.1f}% drop\n{conv:.1f}% conv",
                ha="left", va="center",
                fontsize=8.5, color="#555555",
            )


def get_chart_path(chart_id: str) -> Path | None:
    p = _CHART_DIR / f"{chart_id}.png"
    return p if p.exists() else None
