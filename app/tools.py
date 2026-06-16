"""LangChain tools for the Teams News Agent."""

import io
import os
import re
import time as _time
import datetime
import logging

import requests
from langchain_core.tools import tool

# In-memory Excel DataFrame cache keyed by "drive_id:item_id:sheet_name".
# Populated by analyze_excel_file (Q1), consumed by query_excel_data (Q2+).
_excel_cache: dict = {}
_EXCEL_CACHE_TTL = 7200  # 2 hours


def _format_df_for_llm(df: "Any", filename: str, sheet_name: str) -> str:
    """Return Excel data as CSV text that the LLM can filter/aggregate.

    Hard-caps output at ~6,000 tokens (~24,000 chars) to prevent context overflow.
    Large files get as many rows as fit within the budget.
    """
    _CHAR_BUDGET = 24_000  # ~6k tokens — safe headroom given 32k context + system prompt + history

    total_rows = len(df)
    cols = list(df.columns)

    header_line = ",".join(str(c) for c in cols)
    header = [
        f"📊 **Dữ liệu Excel** — `{filename}` | Sheet: `{sheet_name}` | {total_rows} rows × {len(cols)} cols",
        "",
        "**Columns:** " + ", ".join(f"`{c}`" for c in cols),
        "",
        "**Data (CSV):**",
        "```csv",
        header_line,
    ]
    budget = _CHAR_BUDGET - sum(len(l) for l in header)

    data_lines: list[str] = []
    for _, row in df.iterrows():
        line = ",".join("" if v is None else str(v) for v in row)
        budget -= len(line) + 1  # +1 for newline
        if budget < 0:
            break
        data_lines.append(line)

    lines = header + data_lines
    lines.append("```")
    displayed = len(data_lines)
    truncated = displayed < total_rows
    if truncated:
        lines.append(f"\n_⚠️ Hiển thị {displayed}/{total_rows} rows (giới hạn context). Dùng filter/aggregate để phân tích toàn bộ._")

    # Append quick numeric stats
    try:
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if numeric_cols:
            stats = df[numeric_cols].describe().round(2)
            lines.append("\n**Thống kê nhanh:**")
            lines.append("```")
            lines.append(stats.to_string())
            lines.append("```")
    except Exception:
        pass

    return "\n".join(lines)

from .news import fetch_rss_feeds, fetch_article_text, format_articles_for_llm
from .teams import send_teams_reply, get_access_token
from .scheduler import scheduler_manager
from .excel import download_file_bytes, analyze_excel_bytes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared LLM reference for pandas dataframe sub-agent.
# Set by TeamsAgent.__init__ via set_shared_llm() so all tool calls reuse
# the same ChatOpenAI instance instead of creating a new one per call.
# ---------------------------------------------------------------------------
_shared_llm: "Any" = None


def set_shared_llm(llm: "Any") -> None:
    global _shared_llm
    _shared_llm = llm


def _get_llm() -> "Any":
    global _shared_llm
    if _shared_llm is None:
        from .llm import get_llm_model
        _shared_llm = get_llm_model()
    return _shared_llm


def _check_filter_consistency(question: str, intermediate_steps: list) -> str:
    """Inspect pandas-agent generated code for mixed-filter bugs.

    Extracts every Python code snippet from intermediate_steps, then checks:
    if the question mentions a segment token (e.g. 'TikTok'), that token must
    appear in every line that reads a numeric column (lines with .sum()/.mean()
    /.count()/.value_counts() etc.). If a line aggregates without the segment
    filter we flag it.

    Returns an empty string when no issue is found, or a warning note to append
    to the answer so the main agent (and user) know to verify.
    """
    if not intermediate_steps:
        return ""

    # Collect all code strings from tool calls in intermediate steps
    code_blocks: list[str] = []
    for step in intermediate_steps:
        # intermediate_steps is list of (AgentAction, observation) tuples
        if not isinstance(step, (list, tuple)) or len(step) < 1:
            continue
        action = step[0]
        tool_input = getattr(action, "tool_input", None) or getattr(action, "log", "")
        if isinstance(tool_input, dict):
            code_blocks.append(str(tool_input.get("query", tool_input.get("code", ""))))
        else:
            code_blocks.append(str(tool_input))

    if not code_blocks:
        return ""

    full_code = "\n".join(code_blocks)

    # Find segment tokens: words in the question that appear as string literals
    # in the generated code (quoted values like 'TikTok', "Facebook", ...)
    import re
    quoted_in_code = set(re.findall(r"['\"]([^'\"]+)['\"]", full_code))
    # Only care about tokens that also appear in the question (case-insensitive)
    q_lower = question.lower()
    segments = [v for v in quoted_in_code if v.lower() in q_lower and len(v) > 1]

    if not segments:
        return ""  # no segment filter in play — nothing to check

    # Find numeric aggregation lines that DON'T contain ANY of the segment tokens
    agg_pattern = re.compile(r"\.(sum|mean|count|value_counts|agg|groupby)\s*\(")
    suspicious_lines: list[str] = []
    for line in full_code.splitlines():
        line_stripped = line.strip()
        if not agg_pattern.search(line_stripped):
            continue
        if any(seg in line_stripped for seg in segments):
            continue  # segment filter present on this line — OK
        # Aggregation line with no segment filter — potential bug
        suspicious_lines.append(line_stripped)

    if not suspicious_lines:
        return ""

    logger.warning(
        "[PANDAS_AGENT] filter_consistency: question mentions %s but these lines have no filter: %s",
        segments, suspicious_lines,
    )
    return (
        "\n\n⚠️ _Lưu ý: phát hiện một số phép tính có thể chưa lọc đúng segment "
        f"({', '.join(segments)}). Hãy xác nhận lại kết quả trên._"
    )


def _pandas_agent_answer(df: "Any", question: str, filename: str, sheet_name: str = "") -> str:
    """Use a pandas dataframe agent to compute the answer for a specific question.

    The sub-agent generates Python pandas code, executes it against the full
    DataFrame, and returns only the computed result — the main LLM never sees
    raw CSV, so large files don't cause context overflow.

    Falls back to _format_df_for_llm if langchain-experimental is not installed
    or if the agent fails.
    """
    try:
        from langchain_experimental.agents import create_pandas_dataframe_agent
    except ImportError:
        logger.warning("langchain-experimental not installed — falling back to CSV format")
        return _format_df_for_llm(df, filename, sheet_name)

    llm = _get_llm()
    display = f"`{filename}`" + (f" | sheet `{sheet_name}`" if sheet_name else "")

    # Build column hint so agent knows available values without seeing all rows
    col_hints: list[str] = []
    for col in df.columns:
        if df[col].dtype == object or str(df[col].dtype) == "category":
            uniq = df[col].dropna().unique()
            if 1 < len(uniq) <= 20:
                col_hints.append(f"  - `{col}`: {', '.join(str(v) for v in uniq[:20])}")
    col_hint_block = (
        "\nGiá trị unique của các cột phân loại:\n" + "\n".join(col_hints)
        if col_hints else ""
    )

    prefix = (
        f"Bạn đang phân tích file Excel {display} ({len(df)} rows × {len(df.columns)} cols).\n"
        "Trả lời câu hỏi bằng tiếng Việt, ngắn gọn và chính xác.\n"
        "Dùng pandas để tính toán trực tiếp trên dữ liệu thay vì đoán."
        f"{col_hint_block}\n\n"
        "QUY TẮC BẮT BUỘC:\n"
        "0. Khi filter cột string, LUÔN dùng case-insensitive và strip: "
        "df[df['channel'].str.strip().str.lower() == 'tiktok'] (thay cho exact match)\n"
        "1. Nếu câu hỏi đề cập đến một segment (platform, channel, kênh, nhóm, nguồn...), "
        "PHẢI áp dụng CÙNG filter đó cho TẤT CẢ các giá trị trong cùng phép tính.\n"
        "   ĐÚNG: sub = df[df['channel'].str.strip().str.lower()=='tiktok']; rate = sub['purchase'].sum() / sub['clicks'].sum()\n"
        "   SAI:  tử = df[df['channel']=='TikTok']['clicks'].sum(); mẫu = df['purchase'].sum()  # mix filtered + unfiltered\n"
        "2. Khi tính tỉ lệ A→B, LUÔN lấy cả A và B từ cùng một subset đã filter.\n"
        "3. Không bao giờ mix filtered value với unfiltered value trong cùng một phép tính.\n"
        "4. Nếu không tìm thấy segment (kể cả sau khi strip/lower), thông báo rõ kèm danh sách unique values của cột đó.\n"
    )
    try:
        agent = create_pandas_dataframe_agent(
            llm,
            df,
            agent_type="tool-calling",
            allow_dangerous_code=True,
            verbose=False,
            max_iterations=6,
            prefix=prefix,
            number_of_head_rows=10,
            return_intermediate_steps=True,
        )
        result = agent.invoke({"input": question})
        answer = str(result.get("output", "")).strip()

        # --- Filter-consistency guard (no extra LLM call) ---
        # Extract all Python code blocks from intermediate steps, then check that
        # any segment token present in the question also appears in every numeric
        # extraction line of the generated code — catching the mixed-filter bug.
        warning_suffix = _check_filter_consistency(question, result.get("intermediate_steps", []))

        if answer:
            logger.info("[PANDAS_AGENT] OK filename=%s rows=%d q=%.80s", filename, len(df), question)
            return f"📊 **{filename}** — {answer}{warning_suffix}"
        logger.warning("[PANDAS_AGENT] empty output q=%.80s — falling back to CSV format", question)
        return _format_df_for_llm(df, filename, sheet_name)
    except Exception as e:
        logger.warning("[PANDAS_AGENT] failed (%s) — falling back to CSV format", e)
        return _format_df_for_llm(df, filename, sheet_name)


@tool
def fetch_news(topics: str = "", user_aad_id: str = "") -> str:
    """Fetch the latest news from all configured sources (default + user custom feeds).

    Args:
        topics: Optional comma-separated keywords to filter articles (e.g., "AI, Python, LLM").
                Leave empty to get all recent news.
        user_aad_id: User's AAD object ID (from Teams Context) — used to include
                     any custom RSS sources the user has added.
    """
    from .news import get_all_feeds
    feed_urls = get_all_feeds(user_aad_id)
    articles = fetch_rss_feeds(feed_urls)
    if topics:
        kws = [t.strip().lower() for t in topics.split(",") if t.strip()]
        filtered = [
            a for a in articles
            if any(
                kw in (a["title"] + a.get("summary", "") + a.get("source", "")).lower()
                for kw in kws
            )
        ]
        articles = filtered if filtered else articles

    return format_articles_for_llm(articles, max_articles=10)


@tool
def read_article(url: str) -> str:
    """Fetch and read the full text of a news article from a URL.

    Args:
        url: The URL of the article to read.
    """
    return fetch_article_text(url, max_chars=3000)


@tool
def send_teams_message(conv_id: str, service_url: str, message: str, reply_to_id: str = "") -> str:
    """Send a message to a Microsoft Teams conversation.

    Args:
        conv_id: Teams conversation ID (from current Teams Context).
        service_url: Teams service URL (from current Teams Context).
        message: Message content in markdown format.
        reply_to_id: Optional activity ID to thread the reply to.
    """
    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret_val = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")

    if not all([app_id, client_secret_val, tenant_id]):
        return "Lỗi: Thiếu TEAMS_APP_ID, TEAMS_CLIENT_SECRET hoặc TEAMS_TENANT_ID trong cấu hình."

    token = get_access_token(app_id, client_secret_val, tenant_id)
    if not token:
        return "Lỗi: Không thể lấy access token từ Azure AD. Kiểm tra TEAMS_APP_ID / TEAMS_CLIENT_SECRET / TEAMS_TENANT_ID."

    success, _ = send_teams_reply(
        service_url, conv_id, token, message,
        reply_to_id=reply_to_id or None,
        app_id=app_id, client_secret=client_secret_val, tenant_id=tenant_id,
    )
    return "✅ Đã gửi tin nhắn." if success else "❌ Gửi tin nhắn thất bại."


@tool
def schedule_news(
    time_spec: str,
    conv_id: str,
    service_url: str,
    topics: str = "",
    n_insights: int = 3,
    user_aad_id: str = "",
) -> str:
    """Schedule automatic news summary delivery to a Teams conversation.

    Args:
        time_spec: When to send (Vietnam timezone / ICT). Examples:
                   "08:25", "8:25 AM", "8h25", "8h sáng", "9h chiều",
                   "every 2 hours", "every 10 minutes",
                   "cứ 2 tiếng", "mỗi 30 phút", "10 phút 1 lần", "5 phút", "5p", "5p 1 lần".
        conv_id: Teams conversation ID (from current Teams Context).
        service_url: Teams service URL (from current Teams Context).
        topics: Optional comma-separated topics to focus on (e.g., "AI, startup").
        n_insights: Number of key insights to summarize (default 3).
    """
    try:
        job_id = scheduler_manager.add_news_job(
            conv_id=conv_id,
            service_url=service_url,
            time_spec=time_spec,
            topics=topics,
            n_insights=n_insights,
            user_aad_id=user_aad_id,
        )
        from .job_persist import save_job
        save_job(job_id, "news", {
            "conv_id": conv_id, "service_url": service_url, "time_spec": time_spec,
            "topics": topics, "n_insights": n_insights, "user_aad_id": user_aad_id,
        })
        jobs = scheduler_manager.list_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        next_run = job["next_run"] if job else "unknown"
        topic_info = f"chủ đề: *{topics}*" if topics else "tin tức công nghệ tổng hợp"
        return (
            f"✅ Đã đặt lịch thành công!\n"
            f"- **Job ID**: `{job_id}`\n"
            f"- **Lần chạy tiếp theo**: {next_run}\n"
            f"- **Nội dung**: {n_insights} insights về {topic_info}"
        )
    except ValueError as e:
        return f"❌ Định dạng thời gian không hợp lệ: {e}"
    except Exception as e:
        return f"❌ Không thể đặt lịch: {e}"


@tool
def schedule_email_news(
    time_spec: str,
    user_aad_id: str,
    topics: str = "",
    n_insights: int = 3,
    to_email: str = "",
) -> str:
    """Schedule automatic news summary delivery via email (Outlook / Microsoft Graph).

    Requires the user to have previously authorized Microsoft account access.
    If not yet authorized, returns an OAuth link for the user to click.

    Args:
        time_spec: When to send (Vietnam timezone / ICT). Examples:
                   "08:25", "8h sáng", "every 2 hours", "cứ 2 tiếng", "mỗi 30 phút", "5 phút".
        user_aad_id: User's Azure AD object ID (from Teams Context user_aad_id).
        topics: Optional comma-separated topics to focus on (e.g., "AI, startup, TechCrunch").
        n_insights: Number of key insights to summarize per email (default 3).
        to_email: Recipient email. Leave empty to auto-resolve from the user's Microsoft profile.
    """
    from .onedrive import is_authorized, get_auth_url, get_user_email

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user (thiếu user_aad_id)."

    if not is_authorized(user_aad_id):
        from .onedrive import save_pending_auth_action
        save_pending_auth_action(user_aad_id, "email_news", {
            "time_spec": time_spec,
            "topics": topics,
            "n_insights": n_insights,
            "to_email": to_email,
        })
        auth_url = get_auth_url(user_aad_id)
        return (
            f"🔐 **Cần xác thực Microsoft trước khi đặt lịch email**\n\n"
            f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
            f"_Sau khi xác thực xong, bot sẽ **tự động tạo lịch** và thông báo lại cho bạn qua Teams!_ 🎉"
        )

    # Resolve recipient early so user knows where emails will go
    recipient = to_email.strip() if to_email else (get_user_email(user_aad_id) or "")
    if not recipient:
        return (
            "❌ Không thể xác định địa chỉ email của bạn.\n"
            "Hãy cung cấp email nhận tin (ví dụ: `ban@company.com`)."
        )

    try:
        job_id = scheduler_manager.add_email_news_job(
            user_aad_id=user_aad_id,
            time_spec=time_spec,
            topics=topics,
            n_insights=n_insights,
            to_email=recipient,
        )
        from .job_persist import save_job
        save_job(job_id, "email_news", {
            "user_aad_id": user_aad_id,
            "time_spec": time_spec,
            "topics": topics,
            "n_insights": n_insights,
            "to_email": recipient,
        })
        jobs = scheduler_manager.list_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        next_run = job["next_run"] if job else "unknown"
        topic_info = f"chủ đề: *{topics}*" if topics else "tin tức công nghệ tổng hợp"
        return (
            f"✅ Đã đặt lịch gửi email thành công!\n"
            f"- **Gửi đến**: `{recipient}`\n"
            f"- **Lần chạy tiếp theo**: {next_run}\n"
            f"- **Nội dung**: {n_insights} insights về {topic_info}"
        )
    except ValueError as e:
        return f"❌ Định dạng thời gian không hợp lệ: {e}"
    except Exception as e:
        return f"❌ Không thể đặt lịch: {e}"


def _job_short_id(jid: str) -> str:
    """6-char display ID: skip type prefix, keep alphanumeric (case-insensitive), lowercase."""
    parts = jid.split("_", 1)
    suffix = parts[1] if len(parts) > 1 else jid
    return re.sub(r"[^a-zA-Z0-9]", "", suffix).lower()[:6]


def _fmt_next_run(nr: str) -> str:
    """Convert 'DD/MM/YYYY HH:MM (ICT)' → 'YYYY-MM-DD HH:MM'."""
    if nr == "paused":
        return "—"
    try:
        dt = datetime.datetime.strptime(nr, "%d/%m/%Y %H:%M (ICT)")
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return nr


@tool
def list_schedules() -> str:
    """List all active user-configured scheduled jobs (news, Excel reports, OneDrive watches)."""
    from .onedrive import get_watch

    _USER_PREFIXES = ("news_", "excel_", "excel_wb_", "onedrive_", "email_")
    all_jobs = scheduler_manager.list_jobs()
    jobs = [j for j in all_jobs if j["id"].startswith(_USER_PREFIXES)]

    if not jobs:
        return "📅 Hiện không có lịch tự động nào đang hoạt động."

    rows = []
    for j in jobs:
        jid = j["id"]
        short_id = _job_short_id(jid)
        next_run = _fmt_next_run(j["next_run"])
        kw = j.get("kwargs", {})

        if jid.startswith("news_"):
            desc = "📰 Tin tức Teams"
            topics = kw.get("topics") or "công nghệ tổng hợp"
            n = kw.get("n_insights", 3)
            task = f"{n} tin — {topics}"
        elif jid.startswith("excel_wb_"):
            desc = "📊 Excel Workbook"
            fname = kw.get("filename", "file.xlsx")
            req = (kw.get("analysis_request") or "tổng quan")[:40]
            task = f"{fname}: {req}"
        elif jid.startswith("excel_"):
            desc = "📊 Báo cáo Excel"
            fname = kw.get("filename", "file.xlsx")
            req = (kw.get("analysis_request") or "tổng quan")[:40]
            task = f"{fname}: {req}"
        elif jid.startswith("onedrive_"):
            desc = "📂 OneDrive Watch"
            watch = get_watch(kw.get("sub_id", ""))
            if watch:
                folder = watch["sharepoint_url"].rstrip("/").split("/")[-1]
                req = (watch.get("analysis_request") or "tổng quan")[:40]
                task = f"{folder}: {req}"
            else:
                task = "Theo dõi folder"
        elif jid.startswith("email_news_"):
            desc = "📧 Email tin tức"
            topics = kw.get("topics") or "công nghệ tổng hợp"
            n = kw.get("n_insights", 3)
            recipient = kw.get("to_email", "")
            task = f"{n} tin — {topics}" + (f" → {recipient}" if recipient else "")
        else:  # email_ (các loại email job khác trong tương lai)
            desc = "📧 Email báo cáo"
            task = (kw.get("subject") or kw.get("filename") or "Báo cáo tự động")[:50]

        rows.append((jid, short_id, desc, next_run, task))

    lines = [
        f"📅 **Lịch tự động đang hoạt động ({len(rows)} job):**\n",
        "| # | Job ID | Mô tả | Lần chạy tiếp theo | Yêu cầu |",
        "|---|--------|-------|-------------------|---------|",
    ]
    for i, (jid, short_id, desc, next_run, task) in enumerate(rows, 1):
        lines.append(f"| {i} | `{short_id}` | {desc} | {next_run} | {task} |")
    return "\n".join(lines)


@tool
def cancel_schedule(job_id: str) -> str:
    """Cancel a scheduled job by full job ID or the 6-char short ID shown in list_schedules.

    Args:
        job_id: Full job ID or 6-char short ID from list_schedules table.
    """
    from .job_persist import delete_job

    # Try exact match first
    if scheduler_manager.remove_job(job_id):
        delete_job(job_id)
        return f"✅ Đã hủy lịch: `{job_id}`"

    # Fallback: match by 6-char short ID
    all_jobs = scheduler_manager.list_jobs()
    for j in all_jobs:
        if _job_short_id(j["id"]) == job_id.lower():
            full_id = j["id"]
            if scheduler_manager.remove_job(full_id):
                delete_job(full_id)
                return f"✅ Đã hủy lịch: `{full_id}`"

    return f"❌ Không tìm thấy lịch với ID: `{job_id}`"


@tool
def schedule_excel_report(
    time_spec: str,
    conv_id: str,
    service_url: str,
    content_url: str = "",
    user_aad_id: str = "",
    unique_id: str = "",
    filename: str = "report.xlsx",
    file_source: str = "",
    analysis_request: str = "",
    n_highlights: int = 3,
) -> str:
    """Schedule periodic automatic analysis of an Excel file and delivery report to Teams.

    Use this when user wants the bot to read an Excel file daily/hourly and send analysis.
    Requires the file attachment to be present in the current message context.

    Args:
        time_spec: When to run — e.g. "09:00", "9h sáng", "every 2 hours".
        conv_id: Teams conversation ID (from Teams Context).
        service_url: Teams service URL (from Teams Context).
        content_url: Stable SharePoint contentUrl from [File Attachment] — PREFERRED over download_url for scheduled jobs.
        user_aad_id: Azure AD object ID of file owner from [File Attachment: user_aad_id=...].
        unique_id: SharePoint file uniqueId from [File Attachment: unique_id=...].
        filename: Display name for reports (from [File Attachment: name=...]).
        file_source: Fallback — local path (/app/data/file.xlsx) or download_url if no content_url.
        analysis_request: Specific focus, e.g. "xu hướng doanh thu", "so sánh chi phí".
        n_highlights: Number of insights per report (default 3).
    """
    source = content_url or file_source
    if not source:
        return (
            "❌ Cần cung cấp file để đặt lịch. Hãy đính kèm file Excel vào tin nhắn và thử lại.\n"
            "Ví dụ: gửi file kèm theo và nói 'Đặt lịch phân tích file này mỗi ngày lúc 9h sáng'."
        )

    try:
        job_id = scheduler_manager.add_excel_report_job(
            conv_id=conv_id,
            service_url=service_url,
            file_source=source,
            time_spec=time_spec,
            content_url=content_url,
            user_aad_id=user_aad_id,
            unique_id=unique_id,
            filename=filename,
            analysis_request=analysis_request,
            n_highlights=n_highlights,
        )
        from .job_persist import save_job
        save_job(job_id, "excel", {
            "conv_id": conv_id, "service_url": service_url, "time_spec": time_spec,
            "file_source": source, "content_url": content_url, "user_aad_id": user_aad_id,
            "unique_id": unique_id, "filename": filename,
            "analysis_request": analysis_request, "n_highlights": n_highlights,
        })
        jobs = scheduler_manager.list_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        next_run = job["next_run"] if job else "unknown"
        notes = []
        if user_aad_id and content_url:
            notes.append("🔐 Sẽ dùng Microsoft Graph API để tải file (cần quyền `Files.Read.All`)")
        else:
            notes.append("⚠️ Không có thông tin Graph API — sẽ dùng URL trực tiếp (có thể hết hạn)")
        return (
            f"✅ Đã đặt lịch phân tích Excel thành công!\n"
            f"- **Job ID**: `{job_id}`\n"
            f"- **File**: `{filename}`\n"
            f"- **Lần chạy tiếp theo**: {next_run}\n"
            f"- **Nội dung**: {n_highlights} insights — {analysis_request or 'tổng quan dữ liệu'}\n"
            f"- {notes[0]}"
        )
    except ValueError as e:
        return f"❌ Định dạng thời gian không hợp lệ: {e}"
    except Exception as e:
        return f"❌ Không thể đặt lịch: {e}"


@tool
def analyze_excel_file(
    filename: str,
    content_url: str,
    user_aad_id: str,
    analysis_request: str = "",
    download_url: str = "",
    unique_id: str = "",
) -> str:
    """Analyze an Excel file attached in a Teams message via Microsoft Graph Workbook API.

    Only call this tool when the CURRENT message (the latest user message) contains a
    [File Attachment: ...] block. Do NOT call this tool based on file attachments that
    appear only in earlier messages (conversation history).

    Primary path: resolves the file to Graph driveId + itemId via content_url, then reads
    data through Graph Workbook API — no file download required.
    Fallback: direct download via download_url if Graph path is unavailable.

    Args:
        filename: Original filename, e.g. "data_file.xlsx".
        content_url: Stable SharePoint contentUrl from [File Attachment: content_url=...].
                     Required for Graph API path.
        user_aad_id: User's AAD object ID from [File Attachment: user_aad_id=...].
                     Required for Graph API path.
        analysis_request: Optional specific focus, e.g. "tìm xu hướng doanh thu theo tháng".
        download_url: Pre-authenticated Teams download URL — fallback only when Graph path fails.
        unique_id: SharePoint file uniqueId from [File Attachment: unique_id=...].
                   Speeds up Graph resolution (Strategy 0 — direct item lookup).
    """
    from .onedrive import resolve_file_ids, get_delegated_token, fetch_workbook_range, get_auth_url
    from .excel import analyze_workbook_df
    import pandas as pd

    # Primary path: Graph Workbook API (no file download, stable IDs, no token expiry issues)
    if content_url and user_aad_id:
        token = get_delegated_token(user_aad_id)
        if not token:
            auth_url = get_auth_url(user_aad_id)
            return (
                f"🔐 **Cần liên kết tài khoản Microsoft** để đọc file qua OneDrive.\n\n"
                f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
                f"Sau khi xác thực, bạn hãy gửi lại file để phân tích."
            )

        ids = resolve_file_ids(content_url, user_aad_id, unique_id=unique_id)
        if ids:
            drive_id, item_id = ids
            workbook_data = fetch_workbook_range(drive_id, item_id, token)
            if workbook_data:
                sheet_name = workbook_data["sheet_name"]
                all_sheets = workbook_data.get("all_sheets", [sheet_name])
                try:
                    df = pd.DataFrame(workbook_data["rows"], columns=workbook_data["headers"])
                    # Strip whitespace (including \xa0 non-breaking space) from column names and string values
                    df.columns = [str(c).strip().replace('\xa0', ' ').strip() for c in df.columns]
                    for _col in df.select_dtypes(include='object').columns:
                        df[_col] = df[_col].apply(
                            lambda x: x.strip().replace('\xa0', ' ').strip() if isinstance(x, str) else x
                        )
                except Exception as e:
                    return f"❌ Lỗi xây dựng dữ liệu từ workbook: {e}"

                # Cache DataFrame for follow-up queries (query_excel_data)
                cache_key = f"{drive_id}:{item_id}:{sheet_name}"
                _excel_cache[cache_key] = {
                    "df": df,
                    "filename": filename,
                    "headers": workbook_data["headers"],
                    "loaded_at": _time.time(),
                }
                logger.info(
                    "analyze_excel_file: Graph Workbook API OK drive=%s item=%s sheet=%s rows=%d all_sheets=%s",
                    drive_id[:8], item_id[:8], sheet_name, len(df), all_sheets,
                )

                result = analyze_workbook_df(
                    df, filename=filename, sheet_name=sheet_name,
                    analysis_request=analysis_request,
                )
                other_sheets = [s for s in all_sheets if s != sheet_name]
                if other_sheets:
                    result += (
                        f"\n\n_Workbook còn {len(other_sheets)} sheet khác: "
                        f"{', '.join(f'`{s}`' for s in other_sheets)}_"
                    )
                result += (
                    f"\n\n[EXCEL_REF: drive_id={drive_id}, item_id={item_id}, "
                    f"sheet={sheet_name}, filename={filename}]"
                )
                return result
            else:
                logger.warning(
                    "analyze_excel_file: fetch_workbook_range returned None for drive=%s item=%s — trying download fallback",
                    drive_id[:8], item_id[:8],
                )
        else:
            logger.warning("analyze_excel_file: resolve_file_ids failed for %s — trying download fallback", filename)

    # Fallback: direct download via temporary Teams download URL
    if download_url:
        try:
            file_bytes = download_file_bytes(download_url)
            result = analyze_excel_bytes(file_bytes, filename=filename, analysis_request=analysis_request)
            logger.info("analyze_excel_file: download fallback succeeded for %s", filename)
            return result
        except Exception as e:
            logger.warning("analyze_excel_file: download fallback failed for %s — %s", filename, e)
            return f"❌ Không thể tải file `{filename}`: {e}"

    return (
        f"❌ Không thể đọc file `{filename}`.\n"
        "Hãy liên kết tài khoản Microsoft qua `check_microsoft_auth` "
        "để cho phép bot đọc file OneDrive, rồi gửi lại file."
    )


@tool
def query_excel_data(
    drive_id: str,
    item_id: str,
    question: str,
    user_aad_id: str,
    sheet_name: str = "",
) -> str:
    """Answer a follow-up question about an Excel file using a pandas dataframe agent.

    Call this for EVERY follow-up question about Excel data after analyze_excel_file
    has already been called in this conversation. The drive_id and item_id are in the
    [EXCEL_REF: drive_id=..., item_id=..., sheet=..., filename=...] line of the previous
    analyze_excel_file result.

    This tool uses a pandas dataframe agent to compute the answer directly on the full
    DataFrame — no raw CSV is exposed to the main LLM, so even very large files work
    without context overflow. The agent generates Python pandas code, runs it, and
    returns the computed result (e.g. a sum, filtered table, or trend).

    Args:
        drive_id: From [EXCEL_REF: drive_id=...] in conversation history.
        item_id: From [EXCEL_REF: item_id=...] in conversation history.
        question: The specific question to answer — passed to the pandas agent.
        user_aad_id: User's AAD object ID from [Teams Context: user_aad_id=...].
        sheet_name: Worksheet name. Leave empty to use the first sheet.
    """
    import pandas as pd

    cache_prefix = f"{drive_id}:{item_id}"

    # Try in-memory cache first (populated by analyze_excel_file)
    target_key = None
    if sheet_name:
        target_key = f"{cache_prefix}:{sheet_name}"
        if target_key not in _excel_cache:
            target_key = None  # may be wrong casing; fall through to scan
    if target_key is None:
        for k in _excel_cache:
            if k.startswith(cache_prefix + ":"):
                target_key = k
                break

    cached = _excel_cache.get(target_key) if target_key else None
    if cached and _time.time() - cached.get("loaded_at", 0) < _EXCEL_CACHE_TTL:
        actual_sheet = target_key.split(":", 2)[-1] if target_key else sheet_name
        logger.info(
            "[TOOL] query_excel_data: cache hit drive=%s item=%s sheet=%s q=%.80s",
            drive_id[:8], item_id[:8], actual_sheet, question,
        )
        return _pandas_agent_answer(cached["df"], question, cached["filename"], actual_sheet)
    elif cached:
        del _excel_cache[target_key]  # expired

    # Cache miss — fetch via Graph Workbook API (no file download needed)
    logger.info(
        "[TOOL] query_excel_data: cache miss, calling Graph Workbook API drive=%s item=%s",
        drive_id[:8], item_id[:8],
    )
    from .onedrive import get_delegated_token, fetch_workbook_range

    token = get_delegated_token(user_aad_id)
    if not token:
        return (
            "❌ Không thể tải dữ liệu Excel — chưa có delegated token cho user này.\n"
            "Hãy đảm bảo user đã xác thực Microsoft account (OAuth)."
        )

    data = fetch_workbook_range(drive_id, item_id, token, sheet_name=sheet_name)
    if not data:
        return (
            "❌ Không thể tải dữ liệu từ Excel workbook.\n"
            "Có thể file đã bị xóa, di chuyển, hoặc quyền truy cập bị thu hồi."
        )

    try:
        df = pd.DataFrame(data["rows"], columns=data["headers"])
    except Exception as e:
        return f"❌ Lỗi khi xử lý dữ liệu workbook: {e}"

    actual_sheet = data["sheet_name"]
    new_key = f"{drive_id}:{item_id}:{actual_sheet}"
    _excel_cache[new_key] = {
        "df": df,
        "filename": f"[workbook]",
        "headers": data["headers"],
        "loaded_at": _time.time(),
    }
    logger.info(
        "[TOOL] query_excel_data: Graph Workbook API OK sheet=%s rows=%d",
        actual_sheet, data["total_rows"],
    )
    return _pandas_agent_answer(df, question, "[workbook]", actual_sheet)


@tool
def watch_onedrive_folder(
    sharepoint_url: str,
    schedule: str,
    analysis_request: str,
    conv_id: str,
    service_url: str,
    user_aad_id: str,
) -> str:
    """Watch a OneDrive/SharePoint folder and schedule automatic Excel analysis reports.

    Dynamically scans the folder each run — new files added later are auto-included.
    Only reports when files change or new files appear.
    Requires user to authorize OneDrive access first (delegated OAuth2).

    Args:
        sharepoint_url: Full SharePoint/OneDrive folder URL from user.
        schedule: When to run — e.g. "08:00", "9h sáng", "every 2 hours".
        analysis_request: What to analyze — e.g. "tóm tắt doanh thu theo tuần".
        conv_id: Teams conversation ID (from Teams Context).
        service_url: Teams service URL (from Teams Context).
        user_aad_id: User's AAD object ID (from Teams Context user_aad_id).
    """
    from .onedrive import is_authorized, get_auth_url, add_watch

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user (user_aad_id). Hãy chat trực tiếp trong Teams."

    if not is_authorized(user_aad_id):
        from .onedrive import save_pending_auth_action
        save_pending_auth_action(user_aad_id, "onedrive_watch", {
            "sharepoint_url": sharepoint_url,
            "schedule": schedule,
            "analysis_request": analysis_request,
        })
        auth_url = get_auth_url(user_aad_id)
        return (
            f"🔐 **Cần xác thực OneDrive trước**\n\n"
            f"Nhấn link bên dưới để cấp quyền cho bot đọc file OneDrive của bạn:\n"
            f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
            f"_Sau khi xác thực xong, bot sẽ **tự động đặt lịch theo dõi** và thông báo lại cho bạn qua Teams!_ 🎉"
        )

    try:
        sub_id = add_watch(
            user_aad_id=user_aad_id,
            sharepoint_url=sharepoint_url,
            schedule=schedule,
            analysis_request=analysis_request,
            conv_id=conv_id,
            service_url=service_url,
        )
        job_id = scheduler_manager.add_onedrive_watch_job(sub_id=sub_id, time_spec=schedule)
        from .job_persist import save_job
        save_job(job_id, "onedrive", {"sub_id": sub_id, "time_spec": schedule})
        jobs = scheduler_manager.list_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        next_run = job["next_run"] if job else "unknown"
        folder_name = sharepoint_url.rstrip("/").split("/")[-1]
        return (
            f"✅ **OneDrive Watch đã được đặt lịch!**\n"
            f"- 📂 **Folder**: `{folder_name}`\n"
            f"- ⏰ **Lịch chạy**: {schedule}\n"
            f"- 🔮 **Lần chạy tiếp theo**: {next_run}\n"
            f"- 🔍 **Yêu cầu**: {analysis_request or 'Tổng quan dữ liệu'}\n"
            f"- 🆔 **Watch ID**: `{sub_id}`\n\n"
            f"_Bot sẽ tự quét folder, chỉ báo cáo khi có file thay đổi hoặc file mới._"
        )
    except ValueError as e:
        return f"❌ Định dạng thời gian không hợp lệ: {e}"
    except Exception as e:
        return f"❌ Lỗi khi đặt lịch: {e}"


@tool
def list_onedrive_watches(user_aad_id: str) -> str:
    """List all active OneDrive folder watches for the current user.

    Args:
        user_aad_id: User's AAD object ID (from Teams Context user_aad_id).
    """
    from .onedrive import list_watches

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user."

    watches = list_watches(user_aad_id)
    if not watches:
        return "📂 Bạn chưa đặt lịch theo dõi OneDrive folder nào."

    lines = [f"📂 **OneDrive Watches của bạn ({len(watches)}):**\n"]
    for w in watches:
        folder_name = w["sharepoint_url"].rstrip("/").split("/")[-1]
        lines.append(f"- **`{w['sub_id']}`** — `{folder_name}`")
        lines.append(f"  ⏰ Lịch: `{w['schedule']}` | 🔍 {w.get('analysis_request') or 'tổng quan'}")
    return "\n".join(lines)


@tool
def cancel_onedrive_watch(sub_id: str) -> str:
    """Cancel an active OneDrive folder watch.

    Args:
        sub_id: Watch subscription ID from list_onedrive_watches.
    """
    from .onedrive import remove_watch, get_watch

    watch = get_watch(sub_id)
    if not watch:
        return f"❌ Không tìm thấy watch với ID: `{sub_id}`"

    folder_name = watch["sharepoint_url"].rstrip("/").split("/")[-1]
    scheduler_manager.remove_job(f"onedrive_{sub_id}")
    remove_watch(sub_id)
    return f"✅ Đã hủy theo dõi OneDrive folder `{folder_name}` (ID: `{sub_id}`)"


@tool
def get_ab_test_guide() -> str:
    """Return a guide explaining what data a user needs to prepare for A/B test analysis.

    Call this when user asks questions like:
    - "Tôi cần chuẩn bị dữ liệu gì cho A/B test?"
    - "Format file Excel A/B test như thế nào?"
    - "Tôi muốn thử nghiệm tính năng mới, cần ghi lại gì?"
    """
    return """📋 **Hướng dẫn chuẩn bị dữ liệu A/B Test**

## Cấu trúc file Excel — 7 cột

| Cột | Bắt buộc | Mô tả | Ví dụ |
|-----|:--------:|-------|-------|
| `user_id` | ✅ | ID duy nhất của user | `u001`, `user_12345` |
| `experiment_id` | ✅ | Tên experiment | `checkout_button_v2` |
| `variant` | ✅ | Nhóm của user | `control` hoặc `treatment` |
| `date` | ✅ | Ngày ghi nhận metric | `2024-01-15` |
| `metric_name` | ✅ | Tên metric đang đo | `conversion`, `revenue` |
| `metric_value` | ✅ | Giá trị metric | `0`, `1`, `150000` |
| `platform` | ⚡ Nên có | Nền tảng để phân tích segment | `iOS`, `Android`, `Web` |

## Format dữ liệu — Long format

> Mỗi dòng = **1 metric** của **1 user**. Nếu đo 3 metrics thì 1 user có 3 dòng.

```
user_id | experiment_id | variant   | date       | metric_name      | metric_value | platform
u001    | checkout_v2   | control   | 2024-01-15 | conversion       | 0            | iOS
u001    | checkout_v2   | control   | 2024-01-15 | revenue          | 0            | iOS
u001    | checkout_v2   | control   | 2024-01-15 | session_duration | 145          | iOS
u002    | checkout_v2   | treatment | 2024-01-15 | conversion       | 1            | Android
u002    | checkout_v2   | treatment | 2024-01-15 | revenue          | 150000       | Android
u002    | checkout_v2   | treatment | 2024-01-15 | session_duration | 210          | Android
```

## Hai loại metric_value

| Loại | Giá trị | Ví dụ metric |
|------|---------|-------------|
| **Binary (0/1)** | Chỉ 0 hoặc 1 | `conversion`, `did_click`, `did_register`, `did_purchase` |
| **Continuous (số thực)** | Số bất kỳ | `revenue`, `session_duration`, `page_views`, `cart_value` |

## Quy tắc quan trọng

1. **Đặt tên control group là `control`** (hoặc `ctrl`, `A`, `baseline`) để auto-detect
2. **Tối thiểu 100 users/nhóm** — dưới 30 kết quả không đáng tin
3. **Mỗi user chỉ ở 1 nhóm duy nhất** — không được overlap control ↔ treatment
4. **Chỉ lấy data sau khi experiment bắt đầu** — data trước ngày launch sẽ làm sai kết quả
5. **Nhiều experiment trong 1 file được** — dùng cột `experiment_id` để phân biệt

## Sau khi có file

Gửi file Excel vào chat và nói: _"Phân tích A/B test"_ — tôi sẽ tự động:
- Detect control/treatment group
- Chạy kiểm định thống kê (t-test / chi-square)
- Áp dụng Bonferroni correction nếu nhiều metrics
- Phân tích theo từng platform
- Kết luận **SHIP ✅ / KHÔNG SHIP 🔴 / INCONCLUSIVE 🔵**"""


@tool
def analyze_ab_test(
    filename: str,
    content_url: str = "",
    user_aad_id: str = "",
    unique_id: str = "",
    download_url: str = "",
    control_variant: str = "",
    experiment_id: str = "",
    agg_override: str = "",
) -> str:
    """Analyze A/B test data from an Excel file uploaded in Teams.

    Expects long-format data with columns: user_id, experiment_id, variant,
    date, metric_name, metric_value, platform.
    Auto-detects control group, aggregation strategy, and statistical test type.
    Applies Bonferroni correction when multiple metrics are present.
    Includes per-platform segment breakdown if platform column exists.

    Primary path: Microsoft Graph API via content_url + user_aad_id (no URL expiry).
    Fallback: direct download via download_url (expires in ~1 hour).

    Args:
        filename: Original filename (e.g. "ab_test.xlsx").
        content_url: Stable SharePoint contentUrl from [File Attachment: content_url=...].
        user_aad_id: User's AAD object ID from [File Attachment: user_aad_id=...].
        unique_id: SharePoint file uniqueId from [File Attachment: unique_id=...].
        download_url: Pre-authenticated Teams download URL — fallback only.
        control_variant: Name of control group — auto-detected if empty ("control", "ctrl", "A", ...).
        experiment_id: Filter to one experiment ID — analyzes all experiments if empty.
        agg_override: Override auto-detected aggregation per metric.
                      Format: "metric1:sum,metric2:mean,conversion:max".
    """
    from .abtest import analyze_ab_bytes
    from .onedrive import resolve_file_ids, get_delegated_token, get_auth_url

    agg_dict: dict[str, str] = {}
    if agg_override:
        for part in agg_override.split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                agg_dict[k.strip()] = v.strip()

    def _run(file_bytes: bytes) -> str:
        return analyze_ab_bytes(
            file_bytes,
            filename=filename,
            agg_override=agg_dict or None,
            control_variant=control_variant,
            experiment_filter=experiment_id,
        )

    # Primary path: Graph API download (stable, no URL expiry)
    if content_url and user_aad_id:
        token = get_delegated_token(user_aad_id)
        if not token:
            auth_url = get_auth_url(user_aad_id)
            return (
                f"🔐 **Cần liên kết tài khoản Microsoft** để đọc file qua OneDrive.\n\n"
                f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
                f"Sau khi xác thực, bạn hãy gửi lại file để phân tích."
            )
        ids = resolve_file_ids(content_url, user_aad_id, unique_id=unique_id)
        if ids:
            drive_id, item_id = ids
            try:
                graph_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content"
                resp = requests.get(graph_url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                resp.raise_for_status()
                logger.info("analyze_ab_test: Graph download OK drive=%s item=%s bytes=%d", drive_id[:8], item_id[:8], len(resp.content))
                return _run(resp.content)
            except Exception as e:
                logger.warning("analyze_ab_test: Graph download failed for %s — %s, trying download_url fallback", filename, e)
        else:
            logger.warning("analyze_ab_test: resolve_file_ids failed for %s — trying download_url fallback", filename)

    # Fallback: direct download via temporary Teams URL
    if download_url:
        try:
            file_bytes = download_file_bytes(download_url)
            logger.info("analyze_ab_test: download_url fallback OK for %s", filename)
            return _run(file_bytes)
        except requests.exceptions.HTTPError as e:
            return f"❌ Không thể tải file (HTTP {e.response.status_code}): {e}"
        except Exception as e:
            return f"❌ Lỗi phân tích A/B test: {e}"

    return (
        f"❌ Không thể đọc file `{filename}` — thiếu content_url hoặc download_url.\n"
        "Hãy đính kèm lại file Excel trong tin nhắn."
    )


@tool
def check_microsoft_auth(user_aad_id: str) -> str:
    """Check if the user has linked their Microsoft account (OAuth token status).

    Returns current auth status, linked email, token expiry, and granted permissions.
    If not authorized or token expired, returns an OAuth link to re-authorize.

    Args:
        user_aad_id: User's AAD object ID (from Teams Context user_aad_id).
    """
    import time as _time
    from .onedrive import get_delegated_token, get_user_email, get_auth_url, _OAUTH_SCOPES
    from . import pg_store as _pg_store

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user (user_aad_id)."

    # Thử lấy token thực sự (auto-refresh nếu cần) — chính xác hơn is_authorized()
    token = get_delegated_token(user_aad_id)

    if not token:
        auth_url = get_auth_url(user_aad_id)
        return (
            f"❌ **Chưa liên kết tài khoản Microsoft.**\n\n"
            f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
            f"Sau khi xác thực, bạn có thể:\n"
            f"- 📧 Nhận email tóm tắt tin tức tự động\n"
            f"- 📂 Theo dõi folder OneDrive\n"
            f"- 📊 Nhận báo cáo Excel qua email"
        )

    # Lấy thêm thông tin từ PostgreSQL
    tokens = _pg_store.get_oauth_tokens(user_aad_id) or {}
    expires_at = tokens.get("expires_at", 0)
    email = get_user_email(user_aad_id) or "_(chưa xác định)_"

    # Format expiry time (VN timezone)
    try:
        import datetime as _dt
        import pytz as _pytz
        _vn_tz = _pytz.timezone("Asia/Ho_Chi_Minh")
        exp_str = _dt.datetime.fromtimestamp(expires_at, tz=_vn_tz).strftime("%d/%m/%Y %H:%M (ICT)")
    except Exception:
        exp_str = "_(không xác định)_"

    # Scopes granted
    scopes = [s for s in _OAUTH_SCOPES.split() if s not in ("offline_access",)]
    scope_str = " · ".join(f"`{s}`" for s in scopes)

    return (
        f"✅ **Tài khoản Microsoft đã được liên kết!**\n\n"
        f"👤 **Email**: {email}\n"
        f"🔑 **Quyền đã cấp**: {scope_str}\n"
        f"⏰ **Token hết hạn**: {exp_str} _(tự động gia hạn qua refresh token)_"
    )


@tool
def send_email_report(
    subject: str,
    body: str,
    user_aad_id: str,
    to_email: str = "",
) -> str:
    """Send an analysis report via email using Microsoft Graph API.

    Sends as the authenticated user (delegated Mail.Send permission).
    If to_email is empty, automatically resolves the sender's own email from
    Microsoft Graph /me (using cached profile from OAuth). If user has not
    authorized yet, returns an OAuth link.

    Args:
        subject: Email subject line.
        body: Report content in markdown format.
        user_aad_id: User's AAD object ID (from Teams Context user_aad_id).
        to_email: Recipient email address. Leave empty to send to the user themselves.
    """
    from .onedrive import send_email_via_graph, is_authorized, get_auth_url, get_user_email

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user."

    if not is_authorized(user_aad_id):
        from .onedrive import save_pending_auth_action
        save_pending_auth_action(user_aad_id, "email_send", {
            "subject": subject,
            "body": body,
            "to_email": to_email,
        })
        auth_url = get_auth_url(user_aad_id)
        return (
            f"🔐 **Cần xác thực trước khi gửi email**\n\n"
            f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
            f"_Sau khi xác thực xong, bot sẽ **tự động gửi email** và thông báo lại cho bạn qua Teams!_ 🎉"
        )

    # Auto-resolve recipient: if blank, send to the user themselves
    recipient = to_email.strip()
    if not recipient:
        recipient = get_user_email(user_aad_id) or ""
    if not recipient:
        return (
            "❌ Không thể xác định địa chỉ email của bạn.\n"
            "Hãy cung cấp email nhận báo cáo (ví dụ: `tôi@company.com`)."
        )

    success = send_email_via_graph(user_aad_id, recipient, subject, body)
    if success:
        return f"✅ Đã gửi email đến `{recipient}`"
    auth_url = get_auth_url(user_aad_id)
    return (
        f"❌ Không thể gửi email. Token có thể chưa có quyền `Mail.Send`.\n"
        f"Vui lòng [xác thực lại]({auth_url}) để cấp thêm quyền, rồi thử lại."
    )


@tool
def visualize_data(
    data_json: str,
    chart_type: str = "auto",
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    palette: str = "default",
    conv_id: str = "",
    service_url: str = "",
) -> str:
    """Generate a chart from data and send it as an image to Teams.

    Supported chart_type values:
      "bar"       — so sánh giá trị giữa các nhóm/danh mục
      "line"      — xu hướng theo thời gian (time series)
      "area"      — giống line nhưng vùng bên dưới được tô màu (stacked cũng được)
      "pie"       — tỷ lệ / phần trăm (nên dùng khi <= 7 nhóm)
      "donut"     — giống pie nhưng có lỗ ở giữa
      "scatter"   — tương quan giữa 2 biến số
      "histogram" — phân phối (distribution) của 1 tập giá trị số
      "heatmap"   — ma trận màu sắc thể hiện cường độ
      "funnel"    — phễu chuyển đổi (marketing/product funnel), stages giảm dần
      "auto"      — tự chọn kiểu phù hợp dựa trên dữ liệu

    data_json formats (JSON string):
      bar/line/area (1 series):   '[{"label":"Jan","value":100},{"label":"Feb","value":150}]'
      bar/line/area (multi):      '[{"label":"Jan","north":100,"south":80},{"label":"Feb","north":120,"south":90}]'
      pie/donut:                  '[{"label":"Mobile","value":60},{"label":"Desktop","value":40}]'
      scatter:                    '[{"x":1.5,"y":3.2,"label":"A"},{"x":2.1,"y":4.0}]'
      scatter (grouped):          '[{"x":1,"y":2,"series":"Group A"},{"x":3,"y":1,"series":"Group B"}]'
      histogram:                  '[12,15,14,20,18,22,19,17]'  (raw numbers)
      heatmap:                    '{"x_labels":["Q1","Q2"],"y_labels":["Prod","Test"],"matrix":[[90,85],[70,75]]}'
      funnel:                     '[{"label":"Awareness","value":10000},{"label":"Interest","value":4500},{"label":"Trial","value":2000},{"label":"Purchase","value":800}]'

    palette options: "default" | "blue" | "green" | "warm" | "pastel"

    Args:
        data_json: JSON-encoded chart data (see formats above).
        chart_type: Chart type keyword or "auto".
        title: Chart title shown at the top.
        x_label: Label for the horizontal axis.
        y_label: Label for the vertical axis.
        palette: Color scheme name.
        conv_id: Teams conversation ID (from Teams Context).
        service_url: Teams service URL (from Teams Context).
    """
    import json as _json
    from .charts import generate_chart
    from .teams import send_teams_card_image, get_access_token

    # Parse data
    try:
        data = _json.loads(data_json)
    except Exception as e:
        return f"❌ data_json không hợp lệ (phải là JSON): {e}"

    # Generate chart
    try:
        chart_id, _ = generate_chart(
            chart_type=chart_type,
            data=data,
            title=title,
            x_label=x_label,
            y_label=y_label,
            palette=palette,
        )
    except Exception as e:
        logger.error("Chart generation error: %s", e, exc_info=True)
        return f"❌ Không thể vẽ biểu đồ: {e}"

    # Build public URL
    base_url = os.getenv("BOT_BASE_URL", "").rstrip("/")
    if not base_url:
        return f"❌ BOT_BASE_URL chưa được cấu hình."
    chart_url = f"{base_url}/charts/{chart_id}"

    # Send Adaptive Card with image to Teams
    if conv_id and service_url:
        app_id = os.getenv("TEAMS_APP_ID", "")
        secret = os.getenv("TEAMS_CLIENT_SECRET", "")
        tenant = os.getenv("TEAMS_TENANT_ID", "")
        token = get_access_token(app_id, secret, tenant)
        if token:
            ok, _ = send_teams_card_image(
                service_url=service_url,
                conv_id=conv_id,
                token=token,
                image_url=chart_url,
                caption=title,
                app_id=app_id,
                client_secret=secret,
                tenant_id=tenant,
            )
            if ok:
                return f"✅ Đã gửi biểu đồ vào Teams."
            return f"⚠️ Biểu đồ đã tạo nhưng gửi Teams thất bại. URL: {chart_url}"

    return f"✅ Biểu đồ đã tạo: {chart_url}"


_DEFAULT_FEED_LABELS: dict[str, str] = {
    "techcrunch.com": "TechCrunch",
    "ycombinator.com": "Hacker News",
    "znews.vn/rss/kinh-te": "ZNews Kinh Tế",
    "znews.vn/rss/cong-nghe": "ZNews Công Nghệ",
}


@tool
def add_news_source(url: str, label: str, user_aad_id: str) -> str:
    """Add a custom RSS news feed for the user. Validates the feed before saving.

    Args:
        url: Full RSS feed URL (e.g. "https://vnexpress.net/rss/tin-moi-nhat.rss").
        label: Display name for this source (e.g. "VnExpress").
        user_aad_id: User's AAD object ID (from Teams Context).
    """
    import feedparser as _fp
    from .news import save_user_feed

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user (user_aad_id)."

    try:
        feed = _fp.parse(url)
        if not feed.entries:
            return (
                f"⚠️ RSS feed không có bài viết nào. Hãy kiểm tra URL đúng chưa:\n`{url}`"
            )
        feed_title = feed.feed.get("title", "") or label
    except Exception as e:
        return f"❌ Không thể đọc RSS feed: {e}"

    feed_id = save_user_feed(user_aad_id, url, label or feed_title)
    return (
        f"✅ **Đã thêm nguồn tin: {label or feed_title}**\n"
        f"- URL: `{url}`\n"
        f"- ID: `{feed_id}`\n\n"
        f"_Từ giờ khi bạn yêu cầu tin tức hoặc đặt lịch, nguồn này sẽ tự động được bao gồm._"
    )


@tool
def list_news_sources(user_aad_id: str) -> str:
    """List all active news sources — default feeds and user-added custom feeds.

    Args:
        user_aad_id: User's AAD object ID (from Teams Context).
    """
    from .news import DEFAULT_FEEDS, get_user_feeds

    lines = ["📰 **Nguồn tin đang hoạt động:**\n", "**Mặc định:**"]
    for url in DEFAULT_FEEDS:
        label = next((v for k, v in _DEFAULT_FEED_LABELS.items() if k in url), url)
        lines.append(f"- {label}")

    user_feeds = get_user_feeds(user_aad_id) if user_aad_id else []
    if user_feeds:
        lines.append("\n**Của bạn (custom):**")
        for f in user_feeds:
            lines.append(f"- **{f['label']}** `{f['id']}`")
            lines.append(f"  `{f['url']}`")
    else:
        lines.append("\n_Bạn chưa thêm nguồn tin tùy chỉnh nào._")

    lines.append("\n💡 Dùng `add_news_source` để thêm bất kỳ nguồn RSS nào.")
    return "\n".join(lines)


@tool
def analyze_onedrive_file(
    sharepoint_url: str,
    user_aad_id: str,
    analysis_request: str = "",
) -> str:
    """Analyze an Excel file stored in OneDrive/SharePoint by URL using the user's Microsoft account.

    Use this when the user shares a SharePoint or OneDrive link to an Excel file (not a Teams
    file attachment). Downloads the current version and returns structured data (sheet structure,
    statistics, sample rows) for the LLM to generate insights from.

    After calling this tool, provide key insights based on the data and suggest 2-3 possible
    follow-up analyses the user might find useful.

    Args:
        sharepoint_url: Full OneDrive/SharePoint URL to the Excel file.
        user_aad_id: User's AAD object ID (from Teams Context).
        analysis_request: Specific analysis focus, e.g. "xu hướng doanh thu theo ngày".
    """
    from .onedrive import is_authorized, get_auth_url, download_onedrive_file, save_pending_auth_action
    from .excel import analyze_excel_bytes

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user."

    if not is_authorized(user_aad_id):
        save_pending_auth_action(user_aad_id, "analyze_onedrive", {
            "sharepoint_url": sharepoint_url,
            "analysis_request": analysis_request,
        })
        auth_url = get_auth_url(user_aad_id)
        return (
            f"🔐 **Cần xác thực Microsoft để đọc file OneDrive**\n\n"
            f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
            f"_Sau khi xác thực xong, hãy gửi lại yêu cầu phân tích — bot sẽ đọc file ngay!_"
        )

    try:
        file_bytes = download_onedrive_file(sharepoint_url, user_aad_id)
    except RuntimeError as e:
        return f"❌ Không thể tải file từ OneDrive: {e}"
    except Exception as e:
        return f"❌ Lỗi khi tải file OneDrive: {e}"

    if not file_bytes:
        auth_url = get_auth_url(user_aad_id)
        return (
            f"❌ Không thể tải file từ OneDrive — không có delegated token.\n"
            f"Thử [xác thực lại]({auth_url}) rồi gửi lại yêu cầu."
        )

    filename = sharepoint_url.rstrip("/").split("/")[-1]
    return analyze_excel_bytes(file_bytes, filename=filename, analysis_request=analysis_request)


@tool
def schedule_onedrive_excel(
    sharepoint_url: str,
    time_spec: str,
    conv_id: str,
    service_url: str,
    user_aad_id: str,
    analysis_request: str = "",
    n_highlights: int = 3,
) -> str:
    """Schedule periodic Excel analysis from a OneDrive/SharePoint URL, delivered to Teams.

    Bot will download the latest version of the file on each scheduled run.
    Requires Microsoft account authorization (delegated Files.Read permission).

    Args:
        sharepoint_url: Full OneDrive/SharePoint URL to the Excel file.
        time_spec: Schedule — e.g. "08:00", "9h sáng", "every 2 hours", "cứ 2 tiếng".
        conv_id: Teams conversation ID (from Teams Context).
        service_url: Teams service URL (from Teams Context).
        user_aad_id: User's AAD object ID (from Teams Context).
        analysis_request: What to analyze each run, e.g. "xu hướng doanh thu theo ngày".
        n_highlights: Number of key insights per report (default 3).
    """
    from .onedrive import is_authorized, get_auth_url, save_pending_auth_action

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user."

    if not is_authorized(user_aad_id):
        save_pending_auth_action(user_aad_id, "onedrive_excel_schedule", {
            "sharepoint_url": sharepoint_url,
            "time_spec": time_spec,
            "analysis_request": analysis_request,
            "n_highlights": n_highlights,
        })
        auth_url = get_auth_url(user_aad_id)
        return (
            f"🔐 **Cần xác thực OneDrive trước khi đặt lịch**\n\n"
            f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
            f"_Sau khi xác thực xong, bot sẽ **tự động đặt lịch** và thông báo lại cho bạn!_ 🎉"
        )

    filename = sharepoint_url.rstrip("/").split("/")[-1]
    try:
        job_id = scheduler_manager.add_onedrive_excel_job(
            sharepoint_url=sharepoint_url,
            user_aad_id=user_aad_id,
            conv_id=conv_id,
            service_url=service_url,
            time_spec=time_spec,
            filename=filename,
            analysis_request=analysis_request,
            n_highlights=n_highlights,
        )
        from .job_persist import save_job
        save_job(job_id, "onedrive_excel", {
            "sharepoint_url": sharepoint_url,
            "user_aad_id": user_aad_id,
            "conv_id": conv_id,
            "service_url": service_url,
            "time_spec": time_spec,
            "filename": filename,
            "analysis_request": analysis_request,
            "n_highlights": n_highlights,
        })
        jobs = scheduler_manager.list_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        next_run = job["next_run"] if job else "unknown"
        return (
            f"✅ **Đặt lịch phân tích OneDrive Excel thành công!**\n"
            f"- 📄 **File**: `{filename}`\n"
            f"- ⏰ **Lần chạy tiếp theo**: {next_run}\n"
            f"- 🔍 **Yêu cầu**: {analysis_request or 'Tổng quan dữ liệu'}\n"
            f"- 📊 **Insights mỗi lần**: {n_highlights}\n\n"
            f"_Bot sẽ tự động tải bản mới nhất của file mỗi lần chạy._"
        )
    except ValueError as e:
        return f"❌ Định dạng thời gian không hợp lệ: {e}"
    except Exception as e:
        return f"❌ Không thể đặt lịch: {e}"


@tool
def schedule_excel_workbook(
    time_spec: str,
    conv_id: str,
    service_url: str,
    user_aad_id: str,
    analysis_request: str = "",
    n_highlights: int = 3,
    filename: str = "",
    sheet_name: str = "",
    content_url: str = "",
    sharepoint_url: str = "",
) -> str:
    """Schedule periodic Excel analysis via Microsoft Graph Workbook API, report to Teams.

    Preferred over schedule_excel_report / schedule_onedrive_excel for new scheduled jobs
    because it stores stable drive_id + item_id instead of expiring download URLs.

    Supports two file reference modes (provide exactly ONE):
    - **content_url**: from [File Attachment: content_url=...] when user attaches a file in Teams
    - **sharepoint_url**: a OneDrive/SharePoint URL pasted by the user directly in chat

    The tool resolves the file to a permanent drive_id + item_id at schedule creation time.
    Each scheduled run fetches fresh data via Workbook API — no download_url required.

    Requires the user to have authorized Microsoft account access (delegated OAuth).
    If not yet authorized, returns an OAuth link to authorize first.

    Args:
        time_spec: Schedule — e.g. "08:00", "9h sáng", "every 2 hours", "mỗi ngày".
        conv_id: Teams conversation ID (from Teams Context).
        service_url: Teams service URL (from Teams Context).
        user_aad_id: User's AAD object ID (from Teams Context user_aad_id).
        analysis_request: Specific analysis focus, e.g. "tóm tắt các ý chính doanh số".
        n_highlights: Number of key insights per report (default 3).
        filename: Display name for reports (from [File Attachment: name=...] or user input).
        sheet_name: Specific worksheet name. Leave empty to use the first sheet.
        content_url: Stable SharePoint contentUrl from [File Attachment: content_url=...].
                     Use this when user attached a file in Teams.
        sharepoint_url: Full OneDrive/SharePoint URL pasted by the user directly.
                        Use this when user provides a link instead of attaching.
    """
    from .onedrive import is_authorized, get_auth_url, resolve_file_ids, save_pending_auth_action

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user (user_aad_id)."

    source_url = content_url or sharepoint_url
    if not source_url:
        return (
            "❌ Cần cung cấp file để đặt lịch.\n"
            "Hãy đính kèm file Excel vào tin nhắn HOẶC paste link OneDrive/SharePoint."
        )

    if not is_authorized(user_aad_id):
        save_pending_auth_action(user_aad_id, "excel_workbook_schedule", {
            "source_url": source_url,
            "time_spec": time_spec,
            "filename": filename,
            "analysis_request": analysis_request,
            "n_highlights": n_highlights,
        })
        auth_url = get_auth_url(user_aad_id)
        return (
            f"🔐 **Cần xác thực Microsoft trước khi đặt lịch**\n\n"
            f"[👉 Đăng nhập với Microsoft]({auth_url})\n\n"
            f"_Sau khi xác thực xong, hãy gửi lại yêu cầu — bot sẽ tự động đặt lịch!_ 🎉"
        )

    # Resolve drive_id + item_id once at schedule creation time (permanent IDs)
    ids = resolve_file_ids(source_url, user_aad_id)
    if not ids:
        return (
            "❌ Không thể xác định file từ URL được cung cấp.\n"
            "Hãy đảm bảo URL là link OneDrive/SharePoint hợp lệ và bạn có quyền truy cập."
        )
    drive_id, item_id = ids
    logger.info(
        "schedule_excel_workbook: resolved drive=%s item=%s file=%s",
        drive_id[:8], item_id[:8], filename,
    )

    try:
        job_id = scheduler_manager.add_workbook_job(
            drive_id=drive_id,
            item_id=item_id,
            user_aad_id=user_aad_id,
            conv_id=conv_id,
            service_url=service_url,
            time_spec=time_spec,
            filename=filename,
            sheet_name=sheet_name,
            analysis_request=analysis_request,
            n_highlights=n_highlights,
        )
        from .job_persist import save_job
        save_job(job_id, "excel_workbook", {
            "drive_id": drive_id,
            "item_id": item_id,
            "user_aad_id": user_aad_id,
            "conv_id": conv_id,
            "service_url": service_url,
            "time_spec": time_spec,
            "filename": filename,
            "sheet_name": sheet_name,
            "analysis_request": analysis_request,
            "n_highlights": n_highlights,
        })
        jobs = scheduler_manager.list_jobs()
        job = next((j for j in jobs if j["id"] == job_id), None)
        next_run = job["next_run"] if job else "unknown"
        return (
            f"✅ **Đã đặt lịch phân tích Excel thành công!**\n"
            f"- 📄 **File**: `{filename or 'Excel file'}`\n"
            f"- 🔍 **Sheet**: `{sheet_name or 'Sheet đầu tiên'}`\n"
            f"- ⏰ **Lịch chạy**: `{time_spec}`\n"
            f"- 🔮 **Lần chạy tiếp theo**: {next_run}\n"
            f"- 📊 **Insights mỗi lần**: {n_highlights}\n"
            f"- 🎯 **Yêu cầu**: {analysis_request or 'Tổng quan dữ liệu'}\n\n"
            f"_Bot sẽ tự đọc file qua Microsoft Graph API — không cần đính kèm lại file._"
        )
    except ValueError as e:
        return f"❌ Định dạng thời gian không hợp lệ: {e}"
    except Exception as e:
        return f"❌ Không thể đặt lịch: {e}"


@tool
def remove_news_source(source_id: str, user_aad_id: str) -> str:
    """Remove a user-added custom news source by its ID.

    Args:
        source_id: Feed ID shown in list_news_sources output.
        user_aad_id: User's AAD object ID (from Teams Context).
    """
    from .news import get_user_feeds, remove_user_feed

    if not user_aad_id:
        return "❌ Không tìm thấy thông tin user."

    feeds = get_user_feeds(user_aad_id)
    feed = next((f for f in feeds if f["id"] == source_id), None)
    if not feed:
        return (
            f"❌ Không tìm thấy nguồn tin với ID `{source_id}`.\n"
            f"Dùng `list_news_sources` để xem danh sách."
        )

    remove_user_feed(user_aad_id, source_id)
    return f"✅ Đã xóa nguồn tin: **{feed['label']}**\n`{feed['url']}`"


TOOLS = [
    fetch_news,
    read_article,
    send_teams_message,
    schedule_news,
    schedule_email_news,
    schedule_excel_report,
    schedule_excel_workbook,
    list_schedules,
    cancel_schedule,
    analyze_excel_file,
    query_excel_data,
    analyze_onedrive_file,
    schedule_onedrive_excel,
    watch_onedrive_folder,
    list_onedrive_watches,
    cancel_onedrive_watch,
    get_ab_test_guide,
    analyze_ab_test,
    send_email_report,
    visualize_data,
    add_news_source,
    list_news_sources,
    remove_news_source,
    check_microsoft_auth,
]
