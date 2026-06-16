"""APScheduler manager for scheduled news delivery to Teams."""
from __future__ import annotations

import os
import re
import logging

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_DB_URL = os.getenv("SCHEDULER_DB_URL", "sqlite:///data/jobs.sqlite")
_VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")


def _execute_news_job(
    conv_id: str,
    service_url: str,
    topics: str,
    n_insights: int,
    user_aad_id: str = "",
) -> None:
    """APScheduler job function: fetch news, summarize with LLM, send to Teams.

    Must be a module-level function so APScheduler can serialize/deserialize it.
    """
    import datetime as _dt
    from .news import get_all_feeds, fetch_rss_feeds, format_articles_for_llm
    from .teams import send_teams_reply, get_access_token
    from .llm import get_llm_model
    from langchain_core.messages import SystemMessage, HumanMessage

    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")

    def _notify_error(msg: str) -> None:
        if not all([app_id, client_secret, tenant_id]):
            return
        token = get_access_token(app_id, client_secret, tenant_id)
        if token:
            send_teams_reply(
                service_url, conv_id, token, msg,
                app_id=app_id, client_secret=client_secret, tenant_id=tenant_id,
            )

    try:
        token = get_access_token(app_id, client_secret, tenant_id)
        if not token:
            logger.error("Scheduled job %s: failed to get Teams token", conv_id)
            _notify_error("⚠️ **Lịch tin tức tự động**: Không thể lấy bot token. Kiểm tra cấu hình TEAMS_APP_ID / TEAMS_CLIENT_SECRET.")
            return

        feed_urls = get_all_feeds(user_aad_id)
        articles = fetch_rss_feeds(feed_urls)
        if topics:
            topic_list = [t.strip().lower() for t in topics.split(",")]
            filtered = [
                a for a in articles
                if any(
                    kw in (a["title"] + a.get("summary", "") + a.get("source", "")).lower()
                    for kw in topic_list
                )
            ]
            articles = filtered if filtered else articles

        articles_text = format_articles_for_llm(articles, max_articles=12)

        llm = get_llm_model()
        messages = [
            SystemMessage(content="You are a concise tech news summarizer. Always respond in Vietnamese."),
            HumanMessage(
                content=(
                    f"Tóm tắt các bài báo sau thành {n_insights} insights quan trọng nhất. "
                    f"Format: đánh số, tiêu đề in đậm, 1-2 câu mô tả, include URL.\n\n{articles_text}"
                )
            ),
        ]
        response = llm.invoke(messages)
        summary = response.content

        run_time = _dt.datetime.now(_VN_TZ).strftime("%d/%m/%Y %H:%M")
        message = f"📰 **Tin tức công nghệ** _{run_time}_\n\n{summary}"
        success, _ = send_teams_reply(
            service_url, conv_id, token, message,
            app_id=app_id, client_secret=client_secret, tenant_id=tenant_id,
        )
        if not success:
            logger.error("Scheduled job: failed to send Teams message to %s", conv_id)

    except Exception as e:
        logger.error("Scheduled news job error for conv %s: %s", conv_id, e, exc_info=True)
        _notify_error(f"❌ **Lịch tin tức tự động gặp lỗi**:\n`{str(e)[:200]}`")


def _execute_excel_report_job(
    conv_id: str,
    service_url: str,
    file_source: str,
    content_url: str,
    user_aad_id: str,
    unique_id: str,
    filename: str,
    analysis_request: str,
    n_highlights: int = 3,
) -> None:
    """APScheduler job: load Excel, analyze with pandas, summarize with LLM, send to Teams.

    Module-level function required for APScheduler serialization.
    """
    import datetime
    from .excel import load_excel_from_source, analyze_excel_bytes
    from .teams import send_teams_reply, get_access_token
    from .llm import get_llm_model
    from langchain_core.messages import SystemMessage, HumanMessage

    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")

    def _notify_error(msg: str) -> None:
        if not all([app_id, client_secret, tenant_id]):
            return
        token = get_access_token(app_id, client_secret, tenant_id)
        if token:
            send_teams_reply(
                service_url, conv_id, token, msg,
                app_id=app_id, client_secret=client_secret, tenant_id=tenant_id,
            )

    try:
        # 1. Load file
        file_bytes = load_excel_from_source(
            file_source=file_source,
            content_url=content_url,
            user_aad_id=user_aad_id,
            unique_id=unique_id,
        )

        # 2. Pandas analysis
        raw_analysis = analyze_excel_bytes(file_bytes, filename=filename, analysis_request=analysis_request)

        # 3. LLM summarize into N insights
        llm = get_llm_model()
        messages = [
            SystemMessage(content="You are a data analyst. Always respond in Vietnamese."),
            HumanMessage(
                content=(
                    f"Từ phân tích dữ liệu Excel dưới đây, hãy đưa ra {n_highlights} insights quan trọng nhất.\n"
                    f"Format: đánh số, tiêu đề in đậm, 1-2 câu diễn giải ý nghĩa thực tế.\n"
                    f"Yêu cầu phân tích: {analysis_request or 'Tổng quan dữ liệu'}\n\n"
                    f"{raw_analysis}"
                )
            ),
        ]
        summary = llm.invoke(messages).content

        # 4. Send to Teams
        report_date = datetime.datetime.now(_VN_TZ).strftime("%d/%m/%Y %H:%M")
        message = f"📊 **Báo cáo định kỳ: {filename}**\n_{report_date}_\n\n{summary}"

        token = get_access_token(app_id, client_secret, tenant_id)
        if not token:
            logger.error("Excel report job: failed to get Teams token for conv %s", conv_id)
            return
        success, _ = send_teams_reply(
            service_url, conv_id, token, message,
            app_id=app_id, client_secret=client_secret, tenant_id=tenant_id,
        )
        if not success:
            logger.error("Excel report job: failed to send to conv %s", conv_id)

    except FileNotFoundError:
        _notify_error(f"❌ **Lỗi lịch phân tích Excel**: Không tìm thấy file `{filename}`.\n"
                      f"Hãy kiểm tra đường dẫn hoặc cập nhật lại lịch.")
        logger.error("Excel report job: file not found: %s", file_source)
    except Exception as e:
        _notify_error(f"❌ **Lỗi lịch phân tích Excel** (`{filename}`):\n{str(e)[:300]}")
        logger.error("Excel report job error for conv %s: %s", conv_id, e, exc_info=True)


def _execute_onedrive_excel_job(
    sharepoint_url: str,
    user_aad_id: str,
    conv_id: str,
    service_url: str,
    filename: str = "",
    analysis_request: str = "",
    n_highlights: int = 3,
) -> None:
    """APScheduler job: download Excel file from OneDrive URL, analyze, send Teams report.

    Uses delegated token — if expired, notifies user to re-auth.
    Always downloads the latest version of the file each run.
    """
    import datetime
    from .onedrive import download_onedrive_file, get_delegated_token, get_auth_url
    from .excel import analyze_excel_bytes
    from .teams import send_teams_reply, get_access_token
    from .llm import get_llm_model
    from langchain_core.messages import SystemMessage, HumanMessage

    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")

    def _send(msg: str) -> None:
        token = get_access_token(app_id, client_secret, tenant_id)
        if token:
            send_teams_reply(
                service_url, conv_id, token, msg,
                app_id=app_id, client_secret=client_secret, tenant_id=tenant_id,
            )

    if not get_delegated_token(user_aad_id):
        auth_url = get_auth_url(user_aad_id)
        _send(
            f"⚠️ **Lịch phân tích Excel OneDrive** — Token xác thực đã hết hạn.\n"
            f"Vui lòng [xác thực lại tại đây]({auth_url}) để tiếp tục nhận báo cáo."
        )
        return

    try:
        file_bytes = download_onedrive_file(sharepoint_url, user_aad_id)
        if not file_bytes:
            _send(
                f"❌ **Lỗi lịch phân tích Excel**: Không thể tải file từ OneDrive.\n"
                f"Kiểm tra quyền truy cập hoặc cập nhật lại lịch."
            )
            return

        display_name = filename or sharepoint_url.rstrip("/").split("/")[-1]
        raw_analysis = analyze_excel_bytes(file_bytes, filename=display_name, analysis_request=analysis_request)

        llm = get_llm_model()
        messages = [
            SystemMessage(content="You are a data analyst. Always respond in Vietnamese."),
            HumanMessage(
                content=(
                    f"Từ phân tích dữ liệu Excel dưới đây, hãy đưa ra {n_highlights} insights quan trọng nhất.\n"
                    f"Format: đánh số, tiêu đề in đậm, 1-2 câu diễn giải ý nghĩa thực tế.\n"
                    f"Yêu cầu phân tích: {analysis_request or 'Tổng quan dữ liệu'}\n\n"
                    f"{raw_analysis[:3500]}"
                )
            ),
        ]
        summary = llm.invoke(messages).content

        report_date = datetime.datetime.now(_VN_TZ).strftime("%d/%m/%Y %H:%M")
        _send(f"📊 **Báo cáo định kỳ: {display_name}**\n_{report_date}_\n\n{summary}")

    except Exception as e:
        _send(f"❌ **Lỗi lịch phân tích Excel** (`{filename or 'file'}`):\n{str(e)[:300]}")
        logger.error("OneDrive excel job error for conv %s: %s", conv_id, e, exc_info=True)


def _execute_workbook_job(
    drive_id: str,
    item_id: str,
    user_aad_id: str,
    conv_id: str,
    service_url: str,
    filename: str = "",
    sheet_name: str = "",
    analysis_request: str = "",
    n_highlights: int = 3,
) -> None:
    """APScheduler job: fetch Excel data via Graph Workbook API, analyze, send Teams report.

    Uses stable drive_id + item_id (never-expiring) instead of download URLs.
    Auto-refreshes delegated OAuth token via refresh_token stored in diskcache.
    """
    import datetime
    import pandas as pd
    from .onedrive import get_delegated_token, get_auth_url, fetch_workbook_range
    from .excel import analyze_workbook_df
    from .teams import send_teams_reply, get_access_token
    from .llm import get_llm_model
    from langchain_core.messages import SystemMessage, HumanMessage

    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")

    def _send(msg: str) -> None:
        token = get_access_token(app_id, client_secret, tenant_id)
        if token:
            send_teams_reply(
                service_url, conv_id, token, msg,
                app_id=app_id, client_secret=client_secret, tenant_id=tenant_id,
            )

    # 1. Delegated token — auto-refreshes via stored refresh_token
    delegated_token = get_delegated_token(user_aad_id)
    if not delegated_token:
        auth_url = get_auth_url(user_aad_id)
        _send(
            f"⚠️ **Lịch phân tích Excel** — Token xác thực đã hết hạn.\n"
            f"Vui lòng [xác thực lại tại đây]({auth_url}) để tiếp tục nhận báo cáo."
        )
        return

    # 2. Fetch data via Workbook API (no file download needed)
    data = fetch_workbook_range(drive_id, item_id, delegated_token, sheet_name=sheet_name)
    if not data:
        _send(
            f"❌ **Lỗi lịch phân tích Excel** (`{filename or 'file'}`): "
            f"Không thể đọc dữ liệu workbook.\n"
            f"Có thể file đã bị xóa, di chuyển hoặc quyền truy cập bị thu hồi."
        )
        return

    try:
        df = pd.DataFrame(data["rows"], columns=data["headers"])
    except Exception as e:
        _send(f"❌ **Lỗi lịch phân tích Excel**: Không thể xử lý dữ liệu — {e}")
        return

    display_name = filename or data["sheet_name"]
    actual_sheet = data["sheet_name"]

    # 3. Structured analysis
    raw_analysis = analyze_workbook_df(
        df, filename=display_name, sheet_name=actual_sheet,
        analysis_request=analysis_request,
    )

    # 4. LLM summarize → N highlights
    try:
        llm = get_llm_model()
        messages = [
            SystemMessage(content="You are a data analyst. Always respond in Vietnamese."),
            HumanMessage(
                content=(
                    f"Từ phân tích dữ liệu Excel dưới đây, hãy đưa ra {n_highlights} insights quan trọng nhất.\n"
                    f"Format: đánh số, tiêu đề in đậm, 1-2 câu diễn giải ý nghĩa thực tế.\n"
                    f"Yêu cầu phân tích: {analysis_request or 'Tổng quan dữ liệu'}\n\n"
                    f"{raw_analysis[:3500]}"
                )
            ),
        ]
        summary = llm.invoke(messages).content
    except Exception as e:
        logger.error("Workbook job: LLM summarize failed: %s", e)
        summary = raw_analysis[:2000]

    # 5. Send report to Teams
    report_date = datetime.datetime.now(_VN_TZ).strftime("%d/%m/%Y %H:%M")
    _send(f"📊 **Báo cáo định kỳ: {display_name}**\n_{report_date}_\n\n{summary}")
    logger.info(
        "Workbook job completed: file=%s sheet=%s rows=%d",
        display_name, actual_sheet, data["total_rows"],
    )


def _execute_email_news_job(
    user_aad_id: str,
    topics: str,
    n_insights: int,
    to_email: str = "",
) -> None:
    """APScheduler job: fetch news, summarize with LLM, send via email.

    Module-level function required for APScheduler serialization.
    """
    import datetime as _dt
    from .news import fetch_rss_feeds, format_articles_for_llm
    from .onedrive import send_email_via_graph, get_delegated_token, get_user_email, get_auth_url
    from .llm import get_llm_model
    from langchain_core.messages import SystemMessage, HumanMessage

    if not user_aad_id:
        logger.error("Email news job: missing user_aad_id — cannot send email")
        return

    token = get_delegated_token(user_aad_id)
    if not token:
        logger.warning(
            "Email news job: OAuth token expired or missing for user %s — skipping",
            user_aad_id,
        )
        return

    recipient = to_email.strip() if to_email else (get_user_email(user_aad_id) or "")
    if not recipient:
        logger.error("Email news job: cannot resolve recipient email for user %s", user_aad_id)
        return

    try:
        from .news import get_all_feeds
        feed_urls = get_all_feeds(user_aad_id)
        articles = fetch_rss_feeds(feed_urls)
        if topics:
            topic_list = [t.strip().lower() for t in topics.split(",")]
            filtered = [
                a for a in articles
                if any(
                    kw in (a["title"] + a.get("summary", "") + a.get("source", "")).lower()
                    for kw in topic_list
                )
            ]
            articles = filtered if filtered else articles

        articles_text = format_articles_for_llm(articles, max_articles=12)

        llm = get_llm_model()
        messages = [
            SystemMessage(content="You are a concise tech news summarizer. Always respond in Vietnamese."),
            HumanMessage(
                content=(
                    f"Tóm tắt các bài báo sau thành {n_insights} insights quan trọng nhất. "
                    f"Format: đánh số, tiêu đề in đậm, 1-2 câu mô tả, include URL.\n\n{articles_text}"
                )
            ),
        ]
        summary = llm.invoke(messages).content

        run_time = _dt.datetime.now(_VN_TZ).strftime("%d/%m/%Y %H:%M")
        subject = f"📰 Tin tức công nghệ — {run_time}"
        body = f"# Tin tức công nghệ\n_{run_time}_\n\n{summary}"

        ok = send_email_via_graph(user_aad_id, recipient, subject, body)
        if not ok:
            logger.error("Email news job: send failed for user %s → %s", user_aad_id, recipient)
        else:
            logger.info("Email news job: sent to %s", recipient)

    except Exception as e:
        logger.error("Email news job error for user %s: %s", user_aad_id, e, exc_info=True)


def _execute_onedrive_watch_job(sub_id: str) -> None:
    """APScheduler job: scan OneDrive folder, detect changes, analyze, send Teams report."""
    from .onedrive import (
        get_watch, update_watch_checksums,
        get_delegated_token, get_auth_url,
        list_excel_files, download_excel_by_id,
    )
    from .excel import analyze_excel_bytes
    from .teams import send_teams_reply, get_access_token
    from .llm import get_llm_model
    from langchain_core.messages import SystemMessage, HumanMessage
    import datetime

    watch = get_watch(sub_id)
    if not watch:
        logger.warning("OneDrive watch job %s: subscription not found", sub_id)
        return

    user_aad_id = watch["user_aad_id"]
    sharepoint_url = watch["sharepoint_url"]
    analysis_request = watch.get("analysis_request", "")
    conv_id = watch["conv_id"]
    service_url = watch["service_url"]
    last_checksums = watch.get("last_checksums", {})

    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")

    def _send(msg: str) -> None:
        token = get_access_token(app_id, client_secret, tenant_id)
        if token:
            send_teams_reply(
                service_url, conv_id, token, msg,
                app_id=app_id, client_secret=client_secret, tenant_id=tenant_id,
            )

    # 1. Get delegated token — if expired/missing, ask user to re-auth
    delegated_token = get_delegated_token(user_aad_id)
    if not delegated_token:
        auth_url = get_auth_url(user_aad_id)
        _send(
            f"⚠️ **OneDrive Watch** — Token của bạn đã hết hạn.\n"
            f"Vui lòng [xác thực lại tại đây]({auth_url}) để tiếp tục nhận báo cáo."
        )
        return

    # 2. Dynamic folder scan
    excel_files = list_excel_files(sharepoint_url, delegated_token)
    if not excel_files:
        logger.info("OneDrive watch %s: no Excel files found in folder", sub_id)
        return

    # 3. Change detection via lastModified timestamp
    changed = []
    new_checksums = dict(last_checksums)
    for f in excel_files:
        key = f["id"]
        new_checksums[key] = f["lastModified"]
        if last_checksums.get(key) != f["lastModified"]:
            changed.append(f)

    update_watch_checksums(sub_id, new_checksums)

    if not changed:
        logger.info("OneDrive watch %s: no changes detected (%d files)", sub_id, len(excel_files))
        return

    print(f"📊 Watch {sub_id}: {len(changed)} file(s) changed — analyzing")

    # 4. Download + analyze changed files
    all_analyses = []
    for f in changed:
        file_bytes = download_excel_by_id(f["id"], f["driveId"], delegated_token)
        if not file_bytes:
            continue
        try:
            analysis = analyze_excel_bytes(
                file_bytes, filename=f["name"], analysis_request=analysis_request
            )
            all_analyses.append({"name": f["name"], "analysis": analysis})
        except Exception as e:
            logger.error("Watch %s: analysis error for %s: %s", sub_id, f["name"], e)

    if not all_analyses:
        return

    # 5. LLM summarize all analyses into one report
    llm = get_llm_model()
    combined = "\n\n---\n\n".join(
        f"## File: {a['name']}\n{a['analysis']}" for a in all_analyses
    )
    messages = [
        SystemMessage(content="You are a data analyst. Always respond in Vietnamese."),
        HumanMessage(
            content=(
                f"Từ phân tích dữ liệu Excel dưới đây ({len(all_analyses)} file có thay đổi), "
                f"hãy tóm tắt những insights quan trọng nhất.\n"
                f"Yêu cầu: {analysis_request or 'Tổng quan dữ liệu'}\n\n"
                f"{combined[:4000]}"
            )
        ),
    ]
    summary = llm.invoke(messages).content

    # 6. Send report to Teams
    report_date = datetime.datetime.now(_VN_TZ).strftime("%d/%m/%Y %H:%M")
    changed_names = ", ".join(f"`{f['name']}`" for f in changed)
    _send(
        f"📊 **OneDrive Report** — {report_date}\n"
        f"📝 File cập nhật: {changed_names}\n\n"
        f"{summary}"
    )


def _canonical_time_key(trigger: str, kw: dict) -> str:
    """Stable key from parsed trigger params — independent of raw time_spec wording.

    "8h sáng", "8:00", "08:00", "8h00" all map to "c0800".
    "every 30 minutes", "cứ 30 phút 1 lần", "30p" all map to "i00h30m".
    This prevents duplicate jobs when user reschedules with different phrasing.
    """
    if trigger == "cron":
        return f"c{kw.get('hour', 0):02d}{kw.get('minute', 0):02d}"
    h = int(kw.get("hours", 0))
    m = int(kw.get("minutes", 0))
    return f"i{h:02d}h{m:02d}m"


def _parse_time_spec(time_spec: str) -> dict:
    """Parse a human-readable time spec into APScheduler trigger kwargs.

    Supported formats:
    - "08:25", "8:25", "8:25 AM", "8h25", "8 giờ 25" → daily cron (UTC+7)
    - "8h sáng", "9h chiều", "8 giờ sáng" → daily cron (no minutes)
    - "every N hours" → interval
    - "every N minutes" → interval
    - "cứ N tiếng", "mỗi N tiếng", "cứ N giờ" → interval (Vietnamese)
    - "mỗi tiếng", "mỗi giờ" → interval every 1 hour
    - "cứ N phút", "mỗi N phút" → interval (Vietnamese)
    - "N tiếng" (bare, e.g. "1 tiếng") → interval
    - "Np", "5p 1 lần" (shorthand for phút) → interval minutes
    """
    ts = time_spec.lower().strip()

    # ── Interval patterns ─────────────────────────────────────────────────────

    # English: "every N hours"
    m = re.match(r"every\s+(\d+)\s+hours?", ts)
    if m:
        return {"trigger": "interval", "hours": int(m.group(1))}

    # English: "every N minutes"
    m = re.match(r"every\s+(\d+)\s+minutes?", ts)
    if m:
        return {"trigger": "interval", "minutes": int(m.group(1))}

    # Vietnamese: "cứ/mỗi N tiếng/giờ"  →  interval hours
    m = re.match(r"(?:cứ|mỗi)\s+(\d+)\s*(?:tiếng|giờ)", ts)
    if m:
        return {"trigger": "interval", "hours": int(m.group(1))}

    # Vietnamese: "mỗi tiếng" / "mỗi giờ" (no number)  →  every 1 hour
    if re.match(r"(?:cứ|mỗi)\s+(?:tiếng|giờ)\b", ts):
        return {"trigger": "interval", "hours": 1}

    # Vietnamese: "cứ/mỗi N phút"  →  interval minutes
    m = re.match(r"(?:cứ|mỗi)\s+(\d+)\s*phút", ts)
    if m:
        return {"trigger": "interval", "minutes": int(m.group(1))}

    # Bare Vietnamese: "1 tiếng", "2 tiếng 1 lần"  →  interval hours
    m = re.match(r"(\d+)\s*tiếng", ts)
    if m:
        return {"trigger": "interval", "hours": int(m.group(1))}

    # Bare Vietnamese: "10 phút 1 lần", "10 phút một lần", "10 phút"  →  interval minutes
    m = re.match(r"(\d+)\s*phút(?:\s+(?:1|một)\s+lần)?$", ts)
    if m:
        return {"trigger": "interval", "minutes": int(m.group(1))}

    # Shorthand: "5p", "5p 1 lần", "5p một lần"  →  interval minutes
    m = re.match(r"(\d+)\s*p(?:\s+(?:1|một)\s+lần)?$", ts)
    if m:
        return {"trigger": "interval", "minutes": int(m.group(1))}

    # ── Daily cron patterns ───────────────────────────────────────────────────

    # With minutes: "H:MM", "HH:MM", "8h25", "8h25 sáng", "8 giờ 25"
    m = re.search(r"(\d{1,2})\s*[h:giờ]\s*(\d{2})\s*(am|pm|sáng|chiều|tối)?", ts)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        period = m.group(3) or ""
        if period in ("pm", "chiều", "tối") and hour < 12:
            hour += 12
        elif period in ("am", "sáng") and hour == 12:
            hour = 0
        return {"trigger": "cron", "hour": hour, "minute": minute, "timezone": _VN_TZ}

    # Without minutes: "8h sáng", "9 giờ chiều", "8h" (alone at end of string)
    m = re.search(r"(\d{1,2})\s*(?:h|giờ)\s*(sáng|chiều|tối|am|pm)?\b", ts)
    if m:
        hour = int(m.group(1))
        period = (m.group(2) or "").lower()
        if period in ("pm", "chiều", "tối") and hour < 12:
            hour += 12
        elif period in ("am", "sáng") and hour == 12:
            hour = 0
        return {"trigger": "cron", "hour": hour, "minute": 0, "timezone": _VN_TZ}

    raise ValueError(
        f"Cannot parse '{time_spec}'. "
        "Use: '08:25', '8h sáng', 'every 2 hours', 'cứ 2 tiếng', 'mỗi 30 phút', '5p', '10 phút 1 lần'."
    )


class SchedulerManager:
    def __init__(self) -> None:
        jobstores = {"default": SQLAlchemyJobStore(url=_DB_URL)}
        executors = {"default": ThreadPoolExecutor(max_workers=5)}
        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults={"coalesce": True, "max_instances": 1},
            timezone=_VN_TZ,
        )

    def start(self) -> None:
        self.scheduler.start()
        count = len(self.scheduler.get_jobs())
        logger.info("Scheduler started — %d existing jobs loaded", count)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def add_news_job(
        self,
        conv_id: str,
        service_url: str,
        time_spec: str,
        topics: str = "",
        n_insights: int = 3,
        user_aad_id: str = "",
    ) -> str:
        trigger_kwargs = _parse_time_spec(time_spec)
        trigger = trigger_kwargs.pop("trigger")
        time_key = _canonical_time_key(trigger, trigger_kwargs)
        job_id = f"news_{conv_id[:20]}_{time_key}"

        kwargs = {
            "conv_id": conv_id,
            "service_url": service_url,
            "topics": topics,
            "n_insights": n_insights,
            "user_aad_id": user_aad_id,
        }
        self.scheduler.add_job(
            _execute_news_job,
            trigger=trigger,
            id=job_id,
            kwargs=kwargs,
            replace_existing=True,
            **trigger_kwargs,
        )
        return job_id

    def add_excel_report_job(
        self,
        conv_id: str,
        service_url: str,
        file_source: str,
        time_spec: str,
        content_url: str = "",
        user_aad_id: str = "",
        unique_id: str = "",
        filename: str = "report.xlsx",
        analysis_request: str = "",
        n_highlights: int = 3,
    ) -> str:
        trigger_kwargs = _parse_time_spec(time_spec)
        trigger = trigger_kwargs.pop("trigger")
        time_key = _canonical_time_key(trigger, trigger_kwargs)
        safe_file = re.sub(r"[^a-z0-9]", "_", filename.lower())[:15]
        job_id = f"excel_{conv_id[:20]}_{safe_file}_{time_key}"

        self.scheduler.add_job(
            _execute_excel_report_job,
            trigger=trigger,
            id=job_id,
            kwargs={
                "conv_id": conv_id,
                "service_url": service_url,
                "file_source": file_source,
                "content_url": content_url,
                "user_aad_id": user_aad_id,
                "unique_id": unique_id,
                "filename": filename,
                "analysis_request": analysis_request,
                "n_highlights": n_highlights,
            },
            replace_existing=True,
            **trigger_kwargs,
        )
        return job_id

    def list_jobs(self) -> list[dict]:
        jobs = []
        for j in self.scheduler.get_jobs():
            nr = j.next_run_time
            if nr:
                next_run = nr.astimezone(_VN_TZ).strftime("%d/%m/%Y %H:%M (ICT)")
            else:
                next_run = "paused"
            jobs.append({
                "id": j.id,
                "next_run": next_run,
                "trigger": str(j.trigger),
                "kwargs": j.kwargs or {},
            })
        return jobs

    def add_email_news_job(
        self,
        user_aad_id: str,
        time_spec: str,
        topics: str = "",
        n_insights: int = 3,
        to_email: str = "",
    ) -> str:
        trigger_kwargs = _parse_time_spec(time_spec)
        trigger = trigger_kwargs.pop("trigger")
        time_key = _canonical_time_key(trigger, trigger_kwargs)
        job_id = f"email_news_{user_aad_id[:20]}_{time_key}"

        self.scheduler.add_job(
            _execute_email_news_job,
            trigger=trigger,
            id=job_id,
            kwargs={
                "user_aad_id": user_aad_id,
                "topics": topics,
                "n_insights": n_insights,
                "to_email": to_email,
            },
            replace_existing=True,
            **trigger_kwargs,
        )
        return job_id

    def add_onedrive_excel_job(
        self,
        sharepoint_url: str,
        user_aad_id: str,
        conv_id: str,
        service_url: str,
        time_spec: str,
        filename: str = "",
        analysis_request: str = "",
        n_highlights: int = 3,
    ) -> str:
        trigger_kwargs = _parse_time_spec(time_spec)
        trigger = trigger_kwargs.pop("trigger")
        time_key = _canonical_time_key(trigger, trigger_kwargs)
        safe_file = re.sub(r"[^a-z0-9]", "_", (filename or sharepoint_url.split("/")[-1]).lower())[:15]
        job_id = f"excel_od_{user_aad_id[:12]}_{safe_file}_{time_key}"

        self.scheduler.add_job(
            _execute_onedrive_excel_job,
            trigger=trigger,
            id=job_id,
            kwargs={
                "sharepoint_url": sharepoint_url,
                "user_aad_id": user_aad_id,
                "conv_id": conv_id,
                "service_url": service_url,
                "filename": filename,
                "analysis_request": analysis_request,
                "n_highlights": n_highlights,
            },
            replace_existing=True,
            **trigger_kwargs,
        )
        return job_id

    def add_workbook_job(
        self,
        drive_id: str,
        item_id: str,
        user_aad_id: str,
        conv_id: str,
        service_url: str,
        time_spec: str,
        filename: str = "",
        sheet_name: str = "",
        analysis_request: str = "",
        n_highlights: int = 3,
    ) -> str:
        trigger_kwargs = _parse_time_spec(time_spec)
        trigger = trigger_kwargs.pop("trigger")
        time_key = _canonical_time_key(trigger, trigger_kwargs)
        safe_file = re.sub(r"[^a-z0-9]", "_", filename.lower())[:15]
        job_id = f"excel_wb_{user_aad_id[:12]}_{safe_file}_{time_key}"

        self.scheduler.add_job(
            _execute_workbook_job,
            trigger=trigger,
            id=job_id,
            kwargs={
                "drive_id": drive_id,
                "item_id": item_id,
                "user_aad_id": user_aad_id,
                "conv_id": conv_id,
                "service_url": service_url,
                "filename": filename,
                "sheet_name": sheet_name,
                "analysis_request": analysis_request,
                "n_highlights": n_highlights,
            },
            replace_existing=True,
            **trigger_kwargs,
        )
        return job_id

    def add_onedrive_watch_job(self, sub_id: str, time_spec: str) -> str:
        trigger_kwargs = _parse_time_spec(time_spec)
        trigger = trigger_kwargs.pop("trigger")
        job_id = f"onedrive_{sub_id}"
        self.scheduler.add_job(
            _execute_onedrive_watch_job,
            trigger=trigger,
            id=job_id,
            kwargs={"sub_id": sub_id},
            replace_existing=True,
            **trigger_kwargs,
        )
        return job_id

    def remove_job(self, job_id: str) -> bool:
        try:
            self.scheduler.remove_job(job_id)
            return True
        except Exception:
            return False

    def restore_from_persist(self) -> int:
        """Recreate all user jobs from PostgreSQL. Called once at startup.

        Returns the number of jobs successfully restored.
        Also:
        - Migrates old non-canonical job_ids in PostgreSQL to canonical form.
        - Removes stale APScheduler jobs not backed by any PostgreSQL record (zombie cleanup).
        """
        from .job_persist import get_all_jobs, save_job, delete_job
        restored = 0
        expected_ids: set = set()

        for cfg in get_all_jobs():
            old_job_id = cfg.get("job_id", "")
            job_type = cfg.get("job_type", "")
            try:
                new_job_id = None
                if job_type == "news":
                    new_job_id = self.add_news_job(
                        conv_id=cfg["conv_id"],
                        service_url=cfg["service_url"],
                        time_spec=cfg["time_spec"],
                        topics=cfg.get("topics", ""),
                        n_insights=cfg.get("n_insights", 3),
                        user_aad_id=cfg.get("user_aad_id", ""),
                    )
                elif job_type == "excel":
                    new_job_id = self.add_excel_report_job(
                        conv_id=cfg["conv_id"],
                        service_url=cfg["service_url"],
                        file_source=cfg.get("file_source", ""),
                        time_spec=cfg["time_spec"],
                        content_url=cfg.get("content_url", ""),
                        user_aad_id=cfg.get("user_aad_id", ""),
                        unique_id=cfg.get("unique_id", ""),
                        filename=cfg.get("filename", "report.xlsx"),
                        analysis_request=cfg.get("analysis_request", ""),
                        n_highlights=cfg.get("n_highlights", 3),
                    )
                elif job_type == "email_news":
                    new_job_id = self.add_email_news_job(
                        user_aad_id=cfg["user_aad_id"],
                        time_spec=cfg["time_spec"],
                        topics=cfg.get("topics", ""),
                        n_insights=cfg.get("n_insights", 3),
                        to_email=cfg.get("to_email", ""),
                    )
                elif job_type == "onedrive_excel":
                    new_job_id = self.add_onedrive_excel_job(
                        sharepoint_url=cfg["sharepoint_url"],
                        user_aad_id=cfg["user_aad_id"],
                        conv_id=cfg["conv_id"],
                        service_url=cfg["service_url"],
                        time_spec=cfg["time_spec"],
                        filename=cfg.get("filename", ""),
                        analysis_request=cfg.get("analysis_request", ""),
                        n_highlights=cfg.get("n_highlights", 3),
                    )
                elif job_type == "excel_workbook":
                    new_job_id = self.add_workbook_job(
                        drive_id=cfg["drive_id"],
                        item_id=cfg["item_id"],
                        user_aad_id=cfg["user_aad_id"],
                        conv_id=cfg["conv_id"],
                        service_url=cfg["service_url"],
                        time_spec=cfg["time_spec"],
                        filename=cfg.get("filename", ""),
                        sheet_name=cfg.get("sheet_name", ""),
                        analysis_request=cfg.get("analysis_request", ""),
                        n_highlights=cfg.get("n_highlights", 3),
                    )
                elif job_type == "onedrive":
                    new_job_id = self.add_onedrive_watch_job(
                        sub_id=cfg["sub_id"],
                        time_spec=cfg["time_spec"],
                    )
                else:
                    logger.warning("restore_from_persist: unknown job_type '%s' for %s", job_type, old_job_id)
                    continue

                expected_ids.add(new_job_id)
                restored += 1

                # Migrate non-canonical job_id in PostgreSQL if it changed
                if new_job_id != old_job_id:
                    config = {k: v for k, v in cfg.items() if k not in ("job_id", "job_type")}
                    save_job(new_job_id, job_type, config)
                    delete_job(old_job_id)
                    logger.info(
                        "restore_from_persist: migrated job_id %s → %s (%s)",
                        old_job_id, new_job_id, job_type,
                    )
                else:
                    logger.info("restore_from_persist: restored %s (%s)", new_job_id, job_type)

            except Exception as e:
                logger.warning("restore_from_persist: skipped %s — %s", old_job_id, e)

        # Remove stale APScheduler jobs not backed by any PostgreSQL record
        _protected = {"token_refresh"}
        for job in self.scheduler.get_jobs():
            if job.id not in expected_ids and job.id not in _protected:
                try:
                    self.scheduler.remove_job(job.id)
                    logger.info("restore_from_persist: removed stale APScheduler job %s", job.id)
                except Exception:
                    pass

        if restored:
            logger.info("restore_from_persist: %d job(s) active after restore", restored)
        return restored


# Module-level singleton used by tools and main
scheduler_manager = SchedulerManager()
