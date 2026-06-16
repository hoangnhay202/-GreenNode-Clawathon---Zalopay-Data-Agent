"""A/B Test statistical analysis — long-format Excel input."""
from __future__ import annotations

import io
import logging
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_ab_excel(file_bytes: bytes, filename: str = "data.xlsx") -> pd.DataFrame:
    ext = filename.lower().rsplit(".", 1)[-1]
    engine = "openpyxl" if ext == "xlsx" else "xlrd"
    try:
        df = pd.read_excel(io.BytesIO(file_bytes), engine=engine)
    except Exception as e:
        raise ValueError(f"Không đọc được file Excel: {e}") from e
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    return df


def _detect_columns(df: pd.DataFrame) -> dict[str, Any]:
    """Auto-map logical field names → actual column names.

    Supports two formats:
    - Long format: must have metric_name + metric_value columns
    - Wide format: each metric is its own numeric column (e.g. conversion, retention_7d)
    """
    cols = list(df.columns)
    patterns: dict[str, list[str]] = {
        "user_id":       ["user_id", "userid", "user", "id", "member_id"],
        "variant":       ["variant", "group", "experiment_group", "treatment", "arm", "bucket"],
        "metric_name":   ["metric_name", "metric", "kpi", "event", "event_name"],
        "metric_value":  ["metric_value", "value", "metric_val", "kpi_value"],
        "experiment_id": ["experiment_id", "exp_id", "experiment", "exp"],
        "platform":      ["platform", "os", "device_type", "channel"],
        "segment":       ["segment", "user_type", "cohort", "user_segment"],
        "date":          ["date", "event_date", "created_at", "timestamp"],
    }
    col_map: dict[str, Any] = {}
    for field, candidates in patterns.items():
        for c in candidates:
            if c in cols:
                col_map[field] = c
                break

    has_long = "metric_name" in col_map and "metric_value" in col_map
    if has_long:
        col_map["format"] = "long"
        required = ("user_id", "variant", "metric_name", "metric_value")
        missing = [k for k in required if k not in col_map]
        if missing:
            raise ValueError(f"Không tìm thấy cột bắt buộc: {missing}. Các cột có: {cols}")
    else:
        # Wide format: treat remaining numeric columns as metrics
        known = set(v for v in col_map.values() if isinstance(v, str))
        metric_cols = [
            c for c in cols
            if c not in known and pd.api.types.is_numeric_dtype(df[c])
        ]
        if not metric_cols:
            raise ValueError(
                "Không tìm thấy metric columns. Cần có cột metric_name + metric_value "
                "(long format) hoặc các cột số liệu như conversion, retention_7d (wide format). "
                f"Các cột có: {cols}"
            )
        col_map["format"] = "wide"
        col_map["metric_cols_wide"] = metric_cols
        missing = [k for k in ("user_id", "variant") if k not in col_map]
        if missing:
            raise ValueError(f"Không tìm thấy cột bắt buộc: {missing}. Các cột có: {cols}")

    return col_map


# ── Aggregation ───────────────────────────────────────────────────────────────

def _is_binary(series: pd.Series) -> bool:
    vals = set(series.dropna().unique())
    return vals.issubset({0, 1, 0.0, 1.0, True, False})


def aggregate_per_user(
    df: pd.DataFrame,
    col_map: dict[str, str],
    agg_override: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Pivot long format → wide: (user_id, variant, metric_1, metric_2, ...)."""
    user_col = col_map["user_id"]
    variant_col = col_map["variant"]
    mname_col = col_map["metric_name"]
    mval_col = col_map["metric_value"]

    df = df.copy()
    df[mval_col] = pd.to_numeric(df[mval_col], errors="coerce")

    # Determine agg function per metric
    agg_funcs: dict[str, str] = {}
    for m in df[mname_col].dropna().unique():
        if agg_override and m in agg_override:
            agg_funcs[m] = agg_override[m]
        else:
            agg_funcs[m] = "max" if _is_binary(df[df[mname_col] == m][mval_col]) else "sum"

    # Aggregate per (user, variant, metric)
    def _agg(x: pd.Series) -> float:
        m = x.name if hasattr(x, "name") else ""
        return x.max() if agg_funcs.get(m) == "max" else x.sum()

    grouped = (
        df.groupby([user_col, variant_col, mname_col])[mval_col]
        .agg(lambda x: x.max() if agg_funcs.get(x.name, "sum") == "max" else x.sum())
        .reset_index()
    )

    wide = grouped.pivot_table(
        index=[user_col, variant_col],
        columns=mname_col,
        values=mval_col,
        aggfunc="first",
    ).reset_index()
    wide.columns.name = None
    wide.columns = [str(c).strip() for c in wide.columns]
    return wide, agg_funcs


# ── Statistical Tests ─────────────────────────────────────────────────────────

def _cohens_d(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2 or len(b) < 2:
        return 0.0
    pooled = np.sqrt(
        ((len(a) - 1) * a.std(ddof=1) ** 2 + (len(b) - 1) * b.std(ddof=1) ** 2)
        / (len(a) + len(b) - 2)
    )
    return float((a.mean() - b.mean()) / pooled) if pooled else 0.0


def _uplift(ctrl: float, trt: float) -> float:
    if ctrl == 0:
        return float("inf") if trt > 0 else 0.0
    return (trt - ctrl) / abs(ctrl)


def test_one_metric(
    control: np.ndarray,
    treatment: np.ndarray,
    metric: str,
    alpha: float = 0.05,
) -> dict[str, Any]:
    c = control[~np.isnan(control)]
    t = treatment[~np.isnan(treatment)]

    r: dict[str, Any] = {
        "metric": metric,
        "n_control": len(c),
        "n_treatment": len(t),
        "mean_control": float(np.mean(c)) if len(c) else 0.0,
        "mean_treatment": float(np.mean(t)) if len(t) else 0.0,
        "uplift_pct": 0.0,
        "p_value": 1.0,
        "significant": False,
        "significant_bonferroni": False,
        "adjusted_alpha": alpha,
        "test_type": "",
        "effect_size": 0.0,
        "ci_lower": 0.0,
        "ci_upper": 0.0,
        "warning": "",
    }

    if len(c) < 2 or len(t) < 2:
        r["warning"] = "Không đủ dữ liệu"
        return r
    if len(c) < 30 or len(t) < 30:
        r["warning"] = f"Cỡ mẫu nhỏ (n_ctrl={len(c)}, n_trt={len(t)}) — kết quả có thể không ổn định"

    r["uplift_pct"] = _uplift(r["mean_control"], r["mean_treatment"])

    binary = set(np.unique(np.concatenate([c, t]))).issubset({0.0, 1.0})

    if binary:
        r["test_type"] = "chi-square (binary)"
        cont = np.array([
            [int(c.sum()), len(c) - int(c.sum())],
            [int(t.sum()), len(t) - int(t.sum())],
        ])
        try:
            chi2, p, _, _ = stats.chi2_contingency(cont, correction=False)
            r["p_value"] = float(p)
            r["effect_size"] = float(np.sqrt(chi2 / (len(c) + len(t))))  # Cramér's V
        except Exception:
            r["warning"] = "Không thể tính chi-square"
        # Wilson CI for proportion difference
        p_c, p_t = r["mean_control"], r["mean_treatment"]
        se = np.sqrt(p_c * (1 - p_c) / len(c) + p_t * (1 - p_t) / len(t))
        diff = p_t - p_c
        r["ci_lower"] = float(diff - 1.96 * se)
        r["ci_upper"] = float(diff + 1.96 * se)
    else:
        r["test_type"] = "Welch t-test (continuous)"
        _, p = stats.ttest_ind(t, c, equal_var=False)
        r["p_value"] = float(p)
        r["effect_size"] = _cohens_d(t, c)
        diff = r["mean_treatment"] - r["mean_control"]
        var_t, var_c = np.var(t, ddof=1), np.var(c, ddof=1)
        se = np.sqrt(var_t / len(t) + var_c / len(c))
        denom = (var_t / len(t)) ** 2 / (len(t) - 1) + (var_c / len(c)) ** 2 / (len(c) - 1)
        df_w = ((var_t / len(t) + var_c / len(c)) ** 2) / denom if denom else 30
        t_crit = float(stats.t.ppf(1 - alpha / 2, df=df_w))
        r["ci_lower"] = float(diff - t_crit * se)
        r["ci_upper"] = float(diff + t_crit * se)

    r["significant"] = r["p_value"] < alpha
    return r


def run_ab_analysis(
    wide_df: pd.DataFrame,
    col_map: dict[str, str],
    control_variant: str = "",
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Run full A/B analysis supporting 2+ variants with Bonferroni correction."""
    variant_col = col_map["variant"]
    user_col = col_map["user_id"]

    variants = sorted(wide_df[variant_col].dropna().unique().tolist(), key=str)

    if not control_variant:
        for candidate in ("control", "ctrl", "baseline", "a", "0"):
            match = next((v for v in variants if str(v).lower() == candidate), None)
            if match:
                control_variant = match
                break
        if not control_variant:
            control_variant = variants[0]

    treatment_variants = [v for v in variants if v != control_variant]
    metric_cols = [
        c for c in wide_df.columns
        if c not in (user_col, variant_col)
        and pd.api.types.is_numeric_dtype(wide_df[c])
    ]
    ctrl_df = wide_df[wide_df[variant_col] == control_variant]

    results_per_variant: dict[str, list[dict]] = {}
    for tv in treatment_variants:
        trt_df = wide_df[wide_df[variant_col] == tv]
        metric_results = [
            test_one_metric(
                ctrl_df[m].values.astype(float),
                trt_df[m].values.astype(float),
                m,
                alpha=alpha,
            )
            for m in metric_cols
        ]
        # Bonferroni correction across metrics
        n_tests = len(metric_results)
        adj_alpha = alpha / n_tests if n_tests > 1 else alpha
        for r in metric_results:
            r["adjusted_alpha"] = adj_alpha
            r["significant_bonferroni"] = r["p_value"] < adj_alpha
        results_per_variant[str(tv)] = metric_results

    return {
        "control_variant": str(control_variant),
        "treatment_variants": [str(v) for v in treatment_variants],
        "variants": [str(v) for v in variants],
        "n_control": len(ctrl_df),
        "results_per_variant": results_per_variant,
        "metric_cols": metric_cols,
        "alpha": alpha,
        "bonferroni_applied": len(metric_cols) > 1,
    }


# ── Segment Analysis ──────────────────────────────────────────────────────────

def run_segment_analysis(
    wide_df: pd.DataFrame,
    col_map: dict[str, str],
    segment_col: str,
    control_variant: str,
    alpha: float = 0.05,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for seg in wide_df[segment_col].dropna().unique():
        seg_df = wide_df[wide_df[segment_col] == seg]
        try:
            results[str(seg)] = run_ab_analysis(
                seg_df, col_map, control_variant=control_variant, alpha=alpha
            )
        except Exception as e:
            results[str(seg)] = {"error": str(e)}
    return results


# ── Report Helpers ────────────────────────────────────────────────────────────

_PRIMARY_KEYWORDS = [
    "conversion", "cvr", "convert", "purchase", "revenue", "order", "sale",
    "gmv", "booking", "transaction", "click", "ctr", "activation", "signup",
    "register", "retain", "dau", "mau", "session", "engagement",
]


def _pick_primary_metric(metric_cols: list[str]) -> str:
    """Auto-pick primary metric by name heuristics, fallback to first column."""
    for m in metric_cols:
        m_lower = m.lower()
        for kw in _PRIMARY_KEYWORDS:
            if kw in m_lower:
                return m
    return metric_cols[0] if metric_cols else ""


def _format_key_numbers_binary(r: dict) -> list[str]:
    n_c, n_t = r["n_control"], r["n_treatment"]
    conv_c = int(round(r["mean_control"] * n_c))
    conv_t = int(round(r["mean_treatment"] * n_t))
    abs_pp = (r["mean_treatment"] - r["mean_control"]) * 100
    ci_lo = r["ci_lower"] * 100
    ci_hi = r["ci_upper"] * 100
    return [
        f"- Control: {conv_c}/{n_c} users converted = {r['mean_control']:.1%}",
        f"- Treatment: {conv_t}/{n_t} users converted = {r['mean_treatment']:.1%}",
        f"- Absolute uplift: {abs_pp:+.1f} percentage points",
        f"- Relative uplift: {r['uplift_pct']:+.1%}",
        f"- p-value: {r['p_value']:.4f}",
        f"- 95% CI: {ci_lo:+.2f}pp to {ci_hi:+.2f}pp",
    ]


def _format_key_numbers_continuous(r: dict) -> list[str]:
    abs_diff = r["mean_treatment"] - r["mean_control"]
    return [
        f"- Control mean: {r['mean_control']:.4f} (n={r['n_control']:,})",
        f"- Treatment mean: {r['mean_treatment']:.4f} (n={r['n_treatment']:,})",
        f"- Absolute change: {abs_diff:+.4f}",
        f"- Relative uplift: {r['uplift_pct']:+.1%}",
        f"- p-value: {r['p_value']:.4f}",
        f"- 95% CI: {r['ci_lower']:+.4f} to {r['ci_upper']:+.4f}",
    ]


def _what_this_means(r: dict, metric: str, n_total: int, is_binary: bool) -> list[str]:
    lines: list[str] = []
    sig = r["significant_bonferroni"]
    uplift_pos = r["uplift_pct"] > 0
    ci_all_positive = r["ci_lower"] > 0
    ci_all_negative = r["ci_upper"] < 0

    if sig and uplift_pos:
        if is_binary:
            abs_pp = (r["mean_treatment"] - r["mean_control"]) * 100
            lines.append(f"Treatment có {metric} cao hơn rõ rệt so với control (+{abs_pp:.1f}pp).")
        else:
            lines.append(f"Treatment cải thiện {metric} có ý nghĩa thống kê ({r['uplift_pct']:+.1%}).")
        if ci_all_positive:
            lines.append("Khoảng tin cậy không chứa 0 — uplift có khả năng là thật, không phải nhiễu.")
        conf = (1 - r["p_value"]) * 100
        lines.append(f"Confidence: {conf:.1f}% tin tưởng đây là cải thiện thật.")
    elif sig and not uplift_pos:
        lines.append(f"Treatment làm **giảm** {metric} một cách có ý nghĩa thống kê ({r['uplift_pct']:+.1%}).")
        if ci_all_negative:
            lines.append("Khoảng tin cậy hoàn toàn phía âm — mức độ tổn hại là nhất quán.")
    else:
        lines.append(f"Chưa đủ bằng chứng để kết luận treatment cải thiện hay làm xấu {metric}.")
        lines.append("Có thể cần thêm dữ liệu hoặc chạy experiment lâu hơn.")

    if n_total < 200:
        lines.append(
            f"Tuy nhiên sample chỉ có {n_total:,} users, nên kết quả nên được xem là "
            "tín hiệu mạnh cho rollout có kiểm soát, không phải rollout toàn bộ ngay lập tức."
        )
    elif n_total < 1000:
        lines.append(f"Sample {n_total:,} users — tạm đủ để ra quyết định nhưng nên monitor kỹ sau rollout.")

    return lines


def _format_risks(metric_results: list[dict], primary_metric: str, n_total: int) -> list[str]:
    lines: list[str] = []
    secondary = [r for r in metric_results if r["metric"] != primary_metric]

    hard_violations = [r for r in secondary if r["significant_bonferroni"] and r["uplift_pct"] < 0]
    soft_concerns = [r for r in secondary if not r["significant_bonferroni"] and r["uplift_pct"] < 0]

    if hard_violations:
        lines.append("🔴 **Guardrail violations** (significant negative):")
        for r in hard_violations:
            lines.append(
                f"  - `{r['metric']}` giảm {r['uplift_pct']:.1%} (p={r['p_value']:.4f})"
                " — cần đánh giá kỹ trade-off trước khi release tính năng"
            )

    if soft_concerns:
        lines.append("⚠️ **Metrics có xu hướng giảm** (chưa significant):")
        for r in soft_concerns:
            lines.append(f"  - `{r['metric']}` {r['uplift_pct']:+.1%} — cần monitor sau rollout")

    if not hard_violations and not soft_concerns:
        lines.append("- Không phát hiện guardrail violation nào trong dữ liệu hiện có.")

    if n_total < 100:
        lines.append(f"⚠️ Sample rất nhỏ ({n_total:,} users) — kết quả có thể không ổn định.")

    lines.append("- Chưa có dữ liệu về crash rate, refund rate, complaint rate — cần kiểm tra riêng trước khi rollout lớn.")
    lines.append("- Cần xác nhận việc chia nhóm là random và không có user trùng giữa các variants.")

    return lines


def _format_recommendation(primary_result: dict, n_total: int, has_hard_violation: bool) -> str:
    sig = primary_result["significant_bonferroni"]
    uplift_pos = primary_result["uplift_pct"] > 0

    if sig and uplift_pos and not has_hard_violation:
        if n_total < 500:
            return (
                "Rollout dần treatment lên **25-50% traffic** và tiếp tục monitor guardrails. "
                "Sample hiện tại còn nhỏ, nên theo dõi thêm 3-5 ngày trước khi rollout toàn bộ."
            )
        return (
            "Rollout treatment lên **50% traffic** trong tuần đầu, monitor guardrails 3-5 ngày. "
            "Nếu ổn thì rollout 100%."
        )
    elif sig and uplift_pos and has_hard_violation:
        return (
            "**Chưa nên release tính năng toàn bộ.** Primary metric tăng nhưng có guardrail bị vi phạm. "
            "Cần resolve trade-off với team trước, sau đó cân nhắc rollout có kiểm soát."
        )
    elif sig and not uplift_pos:
        return "**Không nên release tính năng này.** Treatment đang làm hại primary metric. Cần rethink design và chạy lại experiment."
    else:
        return (
            "**Hold.** Kết quả chưa đủ rõ ràng. Chạy thêm với sample lớn hơn hoặc kéo dài "
            "thời gian test để đạt statistical power cần thiết."
        )


# ── Report Formatting ─────────────────────────────────────────────────────────

def format_ab_report(
    analysis: dict[str, Any],
    experiment_id: str = "",
    agg_funcs: dict[str, str] | None = None,
    segment_analysis: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    label = f" — `{experiment_id}`" if experiment_id else ""
    lines += [f"# 🧪 A/B Test{label}", ""]

    primary = _pick_primary_metric(analysis["metric_cols"])

    for tv, metric_results in analysis["results_per_variant"].items():
        if not metric_results:
            continue

        primary_result = next((r for r in metric_results if r["metric"] == primary), metric_results[0])
        secondary_results = [r for r in metric_results if r["metric"] != primary_result["metric"]]
        n_total = primary_result["n_control"] + primary_result["n_treatment"]
        is_binary = "chi-square" in primary_result.get("test_type", "")

        sig = primary_result["significant_bonferroni"]
        uplift_pos = primary_result["uplift_pct"] > 0
        hard_violations = [r for r in secondary_results if r["significant_bonferroni"] and r["uplift_pct"] < 0]

        # ── Decision ──────────────────────────────────────────────────────────
        if sig and uplift_pos and not hard_violations:
            verdict_icon, verdict_label = "✅", "Winner"
            verdict_desc = (
                f"Treatment đang outperform control trên primary metric **{primary_result['metric']}**. "
                "Kết quả có ý nghĩa thống kê và confidence interval nằm hoàn toàn phía dương."
            )
        elif sig and uplift_pos and hard_violations:
            verdict_icon, verdict_label = "⚠️", "Mixed — Cần xem xét"
            verdict_desc = (
                f"Primary metric **{primary_result['metric']}** tăng có ý nghĩa, "
                f"nhưng {len(hard_violations)} guardrail metric bị vi phạm. Cần đánh giá trade-off."
            )
        elif sig and not uplift_pos:
            verdict_icon, verdict_label = "🔴", "Loser"
            verdict_desc = (
                f"Treatment đang làm **giảm** primary metric **{primary_result['metric']}** "
                "một cách có ý nghĩa thống kê."
            )
        else:
            verdict_icon, verdict_label = "🟡", "Inconclusive"
            verdict_desc = (
                f"Chưa đủ bằng chứng để kết luận về primary metric **{primary_result['metric']}**. "
                "Kết quả chưa đạt ý nghĩa thống kê."
            )

        lines += ["## Decision", f"{verdict_icon} {verdict_label}", "", verdict_desc, ""]

        # Metric overview table (only when multiple metrics)
        if len(metric_results) > 1:
            lines.append("| Metric | Uplift | Significant | Status |")
            lines.append("|--------|--------|-------------|--------|")
            for r in metric_results:
                star = " ★" if r["metric"] == primary_result["metric"] else ""
                sig_r = r["significant_bonferroni"]
                up_r = r["uplift_pct"] > 0
                if sig_r and up_r:
                    status = "✅ Positive"
                elif sig_r and not up_r:
                    status = "🔴 Negative"
                elif r["uplift_pct"] < 0:
                    status = "⚠️ Trending down"
                else:
                    status = "🔵 Neutral"
                lines.append(
                    f"| **{r['metric']}{star}** | {r['uplift_pct']:+.1%} "
                    f"| {'Yes' if sig_r else 'No'} | {status} |"
                )
            lines += ["", "_★ = primary metric_", ""]

        # ── Key Numbers ───────────────────────────────────────────────────────
        lines += [f"## Key Numbers — `{primary_result['metric']}`", ""]
        if is_binary:
            lines += _format_key_numbers_binary(primary_result)
        else:
            lines += _format_key_numbers_continuous(primary_result)
        lines.append("")

        # ── What This Means ───────────────────────────────────────────────────
        lines += ["## What This Means", ""]
        for bullet in _what_this_means(primary_result, primary_result["metric"], n_total, is_binary):
            lines.append(f"- {bullet}")
        lines.append("")

        # ── Other Metrics ─────────────────────────────────────────────────────
        if secondary_results:
            lines += ["## Other Metrics", ""]
            lines.append("| Metric | Control | Treatment | Uplift | p-value | |")
            lines.append("|--------|---------|-----------|--------|---------|---|")
            for r in secondary_results:
                sig_r = r["significant_bonferroni"]
                up_r = r["uplift_pct"] > 0
                if sig_r and up_r:
                    icon = "✅"
                elif sig_r and not up_r:
                    icon = "🔴"
                elif r["uplift_pct"] < 0:
                    icon = "⚠️"
                else:
                    icon = "🔵"
                lines.append(
                    f"| {r['metric']} | {r['mean_control']:.4f} | {r['mean_treatment']:.4f} "
                    f"| {r['uplift_pct']:+.1%} | {r['p_value']:.4f} | {icon} |"
                )
            lines.append("")

        # ── Risks and Checks ──────────────────────────────────────────────────
        lines += ["## Risks and Checks", ""]
        for risk in _format_risks(metric_results, primary_result["metric"], n_total):
            lines.append(risk)
        lines.append("")

        # ── Recommendation ────────────────────────────────────────────────────
        lines += ["## Recommendation", ""]
        lines.append(_format_recommendation(primary_result, n_total, bool(hard_violations)))
        lines.append("")

    # ── Segment Breakdown ─────────────────────────────────────────────────────
    if segment_analysis:
        lines += ["---", "## 📱 Segment Breakdown (Platform)", ""]
        lines.append("| Platform | n | Primary metric uplift | Verdict |")
        lines.append("|----------|---|----------------------|---------|")
        for seg, seg_res in segment_analysis.items():
            if "error" in seg_res:
                lines.append(f"| {seg} | — | Lỗi | — |")
                continue
            n_ctrl_seg = seg_res["n_control"]
            for tv, mresults in seg_res["results_per_variant"].items():
                n_trt_seg = mresults[0]["n_treatment"] if mresults else 0
                primary_seg = next((r for r in mresults if r["metric"] == primary), mresults[0] if mresults else None)
                if not primary_seg:
                    continue
                n_seg = n_ctrl_seg + n_trt_seg
                sig_seg = primary_seg["significant_bonferroni"]
                up_seg = primary_seg["uplift_pct"] > 0
                if sig_seg and up_seg:
                    seg_rec = "✅ Winner"
                elif sig_seg and not up_seg:
                    seg_rec = "🔴 Loser"
                else:
                    seg_rec = "🟡 Inconclusive"
                lines.append(f"| {seg} | {n_seg:,} | {primary_seg['uplift_pct']:+.1%} | {seg_rec} |")
        lines.append("")

    return "\n".join(lines)


# ── Main Entry Point ──────────────────────────────────────────────────────────

def analyze_ab_bytes(
    file_bytes: bytes,
    filename: str = "ab_test.xlsx",
    agg_override: dict[str, str] | None = None,
    control_variant: str = "",
    experiment_filter: str = "",
    include_segment_breakdown: bool = True,
) -> str:
    """Full pipeline: Excel bytes → statistical analysis → markdown report."""
    df = parse_ab_excel(file_bytes, filename)
    col_map = _detect_columns(df)

    # List experiments to process
    has_exp_col = "experiment_id" in col_map
    if has_exp_col:
        exp_col = col_map["experiment_id"]
        if experiment_filter:
            exp_ids: list[Any] = [experiment_filter]
        else:
            exp_ids = df[exp_col].dropna().unique().tolist()
    else:
        exp_ids = [None]

    reports: list[str] = []
    for exp_id in exp_ids:
        if has_exp_col and exp_id is not None:
            exp_df = df[df[col_map["experiment_id"]] == exp_id].copy()
        else:
            exp_df = df.copy()

        if exp_df.empty:
            reports.append(f"❌ Không có dữ liệu cho experiment `{exp_id}`.")
            continue

        try:
            user_col = col_map["user_id"]
            variant_col = col_map["variant"]

            if col_map.get("format") == "wide":
                metric_cols = col_map["metric_cols_wide"]
                agg_dict: dict[str, str] = {}
                for m in metric_cols:
                    vals = set(exp_df[m].dropna().unique())
                    agg_dict[m] = "max" if vals.issubset({0, 1, 0.0, 1.0, True, False}) else "sum"
                wide_df = (
                    exp_df.groupby([user_col, variant_col])[metric_cols]
                    .agg(agg_dict)
                    .reset_index()
                )
                agg_funcs = agg_dict
            else:
                wide_df, agg_funcs = aggregate_per_user(exp_df, col_map, agg_override)

            analysis = run_ab_analysis(wide_df, col_map, control_variant=control_variant)

            # Platform segment breakdown
            seg_analysis: dict | None = None
            seg_dim = col_map.get("platform") or col_map.get("segment")
            if include_segment_breakdown and seg_dim:
                dim_per_user = (
                    exp_df.groupby(user_col)[seg_dim]
                    .first()
                    .reset_index()
                )
                dim_per_user.columns = [user_col, "_seg_dim"]
                wide_with_seg = wide_df.merge(dim_per_user, on=user_col, how="left")
                seg_analysis = run_segment_analysis(
                    wide_with_seg, col_map, "_seg_dim",
                    control_variant=analysis["control_variant"],
                )

            reports.append(
                format_ab_report(analysis, str(exp_id) if exp_id else "", agg_funcs, seg_analysis)
            )

        except Exception as e:
            reports.append(f"❌ Lỗi phân tích experiment `{exp_id}`: {e}")
            logger.error("AB analysis error for exp %s: %s", exp_id, e, exc_info=True)

    return "\n\n---\n\n".join(reports)
