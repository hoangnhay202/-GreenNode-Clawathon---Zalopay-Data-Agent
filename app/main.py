"""FastAPI entrypoint — Teams webhook + health check."""
from __future__ import annotations

import os
import json
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, Response
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from .agent import TeamsAgent
from .memory import purge_bloated_sessions
from .scheduler import scheduler_manager
from .teams import save_user, warm_bot_token, get_access_token, send_teams_reply, send_typing_indicator, fetch_and_save_email, fetch_message_via_graph
from .onedrive import save_teams_ctx, get_teams_ctx, get_pending_auth_action, clear_pending_auth_action

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_TEAMS_APP_ID = os.getenv("TEAMS_APP_ID", "")
_TEAMS_SECRET = os.getenv("TEAMS_CLIENT_SECRET", "")
_TEAMS_TENANT = os.getenv("TEAMS_TENANT_ID", "")

_MOBILE_PLATFORMS = frozenset({"iOS", "Android"})

# Keywords báo hiệu user đang muốn phân tích file/dữ liệu
_FILE_INTENT_KEYWORDS = (
    "file", "excel", "xlsx", "xls", "csv",
    "phân tích", "dữ liệu", "data", "báo cáo", "bảng", "sheet",
    "đính kèm", "attachment",
)


def _has_file_intent(text: str) -> bool:
    """Trả về True nếu message có dấu hiệu user muốn phân tích file/dữ liệu."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in _FILE_INTENT_KEYWORDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_manager.start()
    restored = scheduler_manager.restore_from_persist()
    if restored:
        print(f"♻️  SCHEDULER: restored {restored} job(s) from diskcache")
    asyncio.get_running_loop().run_in_executor(None, purge_bloated_sessions)
    asyncio.get_running_loop().run_in_executor(None, warm_bot_token)
    scheduler_manager.scheduler.add_job(
        warm_bot_token, "interval", minutes=50, id="token_refresh", replace_existing=True
    )
    yield
    scheduler_manager.shutdown()


app = FastAPI(title="Teams News Agent", lifespan=lifespan)
agent = TeamsAgent()


async def _typing_loop(service_url: str, conv_id: str, stop_event: asyncio.Event) -> None:
    """Gửi typing indicator mỗi 2s cho đến khi stop_event được set."""
    while not stop_event.is_set():
        token = await asyncio.to_thread(get_access_token, _TEAMS_APP_ID, _TEAMS_SECRET, _TEAMS_TENANT)
        if token:
            await asyncio.to_thread(send_typing_indicator, service_url, conv_id, token)
        try:
            await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=2.0)
        except asyncio.TimeoutError:
            pass


async def _process_and_reply(
    raw_text: str,
    service_url: str,
    conv_id: str,
    activity_id: str,
    user_id: str,
    file_attachments: list,
    user_aad_id: str = "",
    client_platform: str = "",
) -> None:
    """Xử lý message và chủ động gửi reply vào Teams qua Bot Framework API."""
    # Bắt đầu typing indicator ngay lập tức — user thấy feedback sớm nhất có thể
    stop_typing = asyncio.Event()
    typing_task = None
    if service_url and conv_id:
        typing_task = asyncio.create_task(_typing_loop(service_url, conv_id, stop_typing))

    try:
        # Mobile fallback: iOS/Android không gửi attachment metadata trong webhook,
        # phải lấy lại qua Graph API GET /chats/{chatId}/messages/{messageId}.
        # Chỉ trigger khi user có intent phân tích file (keyword match) HOẶC gửi không có text
        # (user gửi file không kèm chữ → raw_text rỗng).
        if (
            not file_attachments
            and client_platform in _MOBILE_PLATFORMS
            and conv_id and activity_id
            and (not raw_text.strip() or _has_file_intent(raw_text))
        ):
            graph_attachments = await asyncio.to_thread(
                fetch_message_via_graph, conv_id, activity_id, _TEAMS_APP_ID, _TEAMS_SECRET, _TEAMS_TENANT, user_aad_id,
            )
            if graph_attachments:
                file_attachments = graph_attachments
                logger.info("[MOBILE FALLBACK] Graph fetched %d attachment(s) (platform=%s)", len(file_attachments), client_platform)
            else:
                logger.info("[MOBILE FALLBACK] No attachments found via Graph (platform=%s conv=%s)", client_platform, conv_id[:20])

        response_text = await agent.handle_message(
            raw_text,
            service_url=service_url,
            conv_id=conv_id,
            activity_id=activity_id,
            user_id=user_id,
            user_aad_id=user_aad_id,
            file_attachments=file_attachments,
        )
    except Exception as e:
        logger.error("Agent error: %s", e, exc_info=True)
        response_text = f"❌ Lỗi nội bộ: {str(e)[:200]}"
    finally:
        stop_typing.set()
        if typing_task:
            typing_task.cancel()
            try:
                await asyncio.wait_for(typing_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    print("🤖 RESPONSE:", response_text)

    # Gửi reply vào Teams qua Bot Framework Connector API
    if not response_text or not response_text.strip():
        print("⚠️  SEND SKIPPED: empty response from agent")
        return

    if service_url and conv_id:
        token = get_access_token(_TEAMS_APP_ID, _TEAMS_SECRET, _TEAMS_TENANT)
        if token:
            ok, _ = send_teams_reply(
                service_url=service_url,
                conv_id=conv_id,
                token=token,
                msg=response_text,
                reply_to_id=activity_id or None,
                app_id=_TEAMS_APP_ID,
                client_secret=_TEAMS_SECRET,
                tenant_id=_TEAMS_TENANT,
            )
            print(f"📤 SENT TO TEAMS: {'✅ ok' if ok else '❌ failed'}")
        else:
            print("⚠️  SEND SKIPPED: no bot token available")
    else:
        print("⚠️  SEND SKIPPED: missing service_url or conv_id")


@app.get("/")
async def root():
    return {"status": "ok", "service": "teams-news-agent"}


@app.post("/webhook/teams")
async def teams_webhook(request: Request):
    """Receive activities from Microsoft Teams Bot Framework."""
    body = await request.json()

    print("📥 BODY:", body)

    activity_type: str = body.get("type", "")

    # Bỏ qua các event không phải message (conversationUpdate, typing...)
    if activity_type != "message":
        print(f"⏭️  SKIP activity type: {activity_type}")
        return JSONResponse(content={})

    # Extract Bot Framework Activity fields
    service_url: str = body.get("serviceUrl", os.getenv("TEAMS_SERVICE_URL", ""))
    conv = body.get("conversation", {})
    conv_id: str = conv.get("id", "") if isinstance(conv, dict) else ""
    activity_id: str = body.get("id", "")

    # Extract text (Teams sometimes wraps text in HTML)
    raw_text: str = (
        body.get("text")
        or body.get("message")
        or (body.get("activity", {}).get("text") if isinstance(body.get("activity"), dict) else None)
        or ""
    )
    if raw_text and "<" in raw_text:
        raw_text = BeautifulSoup(raw_text, "lxml").get_text(strip=True)

    # Extract user identity
    from_info = body.get("from", {})
    user_id: str = from_info.get("id", "default") if isinstance(from_info, dict) else "default"
    user_aad_id: str = from_info.get("aadObjectId", "") if isinstance(from_info, dict) else ""
    user_name: str = from_info.get("name", "") if isinstance(from_info, dict) else ""
    save_user(user_aad_id, user_name)
    if user_aad_id:
        save_teams_ctx(user_aad_id, conv_id, service_url)
        fetch_and_save_email(user_aad_id, service_url, conv_id, user_id)

    # Extract client platform (iOS, Android, etc.) from clientInfo entity
    client_platform: str = ""
    for entity in body.get("entities", []):
        if isinstance(entity, dict) and entity.get("type") == "clientInfo":
            client_platform = entity.get("platform", "")
            break

    # Extract file attachments
    file_attachments: list[dict] = []
    for att in body.get("attachments", []):
        if att.get("contentType") == "application/vnd.microsoft.teams.file.download.info":
            content = att.get("content") or {}
            if not isinstance(content, dict):
                continue
            download_url = content.get("downloadUrl", "")
            file_type = content.get("fileType", "").lower()
            name = att.get("name", "attachment")
            if download_url or att.get("contentUrl"):
                file_attachments.append({
                    "name": name,
                    "file_type": file_type,
                    "download_url": download_url,
                    "content_url": att.get("contentUrl", ""),
                    "unique_id": content.get("uniqueId", ""),
                    "user_aad_id": user_aad_id,
                })

    print("💬 PARSED:", json.dumps({
        "conv_id": conv_id, "user_id": user_id,
        "text": raw_text, "files": len(file_attachments),
    }, ensure_ascii=False))

    # Fire-and-forget: trả 200 ngay cho Teams, xử lý + reply async
    asyncio.create_task(_process_and_reply(
        raw_text, service_url, conv_id, activity_id, user_id, file_attachments, user_aad_id, client_platform,
    ))

    return JSONResponse(content={})  # 200 OK ngay lập tức


def _send_teams_notification(conv_id: str, service_url: str, msg: str) -> bool:
    """Send a Teams message. Returns True on success."""
    if not (conv_id and service_url and msg):
        return False
    token = get_access_token(_TEAMS_APP_ID, _TEAMS_SECRET, _TEAMS_TENANT)
    if not token:
        logger.warning("_send_teams_notification: no bot token available")
        return False
    ok, _ = send_teams_reply(
        service_url=service_url,
        conv_id=conv_id,
        token=token,
        msg=msg,
        app_id=_TEAMS_APP_ID,
        client_secret=_TEAMS_SECRET,
        tenant_id=_TEAMS_TENANT,
    )
    return ok


async def _handle_post_auth(user_aad_id: str) -> None:
    """Execute any pending action saved before OAuth and notify user via Teams."""
    pending = get_pending_auth_action(user_aad_id)

    if not pending:
        # No pending action — still notify the user using their last known Teams context.
        # This happens when the user clicks an auth link generated by check_microsoft_auth
        # (which doesn't save a pending action) or when the pending action expired.
        logger.info("Post-auth: no pending action for user %s — sending generic notification", user_aad_id[:8])
        ctx = get_teams_ctx(user_aad_id) or {}
        conv_id = ctx.get("conv_id", "")
        service_url = ctx.get("service_url", "")
        if conv_id and service_url:
            _send_teams_notification(
                conv_id, service_url,
                "✅ **Tài khoản Microsoft đã được liên kết thành công!**\n\n"
                "Bot có thể đọc file OneDrive và gửi email cho bạn. "
                "Hãy thử lại yêu cầu ban đầu! 😊",
            )
        else:
            logger.warning("Post-auth: no Teams context for user %s — cannot notify", user_aad_id[:8])
        return

    action_type = pending.get("type", "")
    conv_id = pending.get("conv_id", "")
    service_url = pending.get("service_url", "")
    payload = pending.get("payload", {})
    msg = ""

    # Fallback: if pending action was saved without Teams context, resolve it now.
    if not conv_id or not service_url:
        ctx = get_teams_ctx(user_aad_id) or {}
        conv_id = conv_id or ctx.get("conv_id", "")
        service_url = service_url or ctx.get("service_url", "")
        if conv_id or service_url:
            logger.info("Post-auth: resolved Teams context from fallback for user %s", user_aad_id[:8])

    logger.info("Post-auth: processing type=%s for user %s (conv=%s)", action_type, user_aad_id[:8], conv_id[:12] if conv_id else "none")

    try:
        if action_type == "email_news":
            from .onedrive import get_user_email
            to_email = payload.get("to_email", "") or (get_user_email(user_aad_id) or "")
            if not to_email:
                msg = "✅ Xác thực Microsoft thành công!\n⚠️ Không thể xác định email của bạn — hãy chat lại để đặt lịch."
            else:
                job_id = scheduler_manager.add_email_news_job(
                    user_aad_id=user_aad_id,
                    time_spec=payload["time_spec"],
                    topics=payload.get("topics", ""),
                    n_insights=payload.get("n_insights", 3),
                    to_email=to_email,
                )
                from .job_persist import save_job
                save_job(job_id, "email_news", {
                    "user_aad_id": user_aad_id,
                    "time_spec": payload["time_spec"],
                    "topics": payload.get("topics", ""),
                    "n_insights": payload.get("n_insights", 3),
                    "to_email": to_email,
                })
                jobs = scheduler_manager.list_jobs()
                job = next((j for j in jobs if j["id"] == job_id), None)
                next_run = job["next_run"] if job else "unknown"
                topics = payload.get("topics", "")
                n = payload.get("n_insights", 3)
                topic_info = f"về *{topics}*" if topics else "tin tức công nghệ tổng hợp"
                msg = (
                    f"✅ **Xác thực Microsoft thành công!**\n\n"
                    f"Đã tự động tạo lịch gửi email **{payload['time_spec']}** "
                    f"với **{n} tin** {topic_info}.\n"
                    f"📧 Gửi đến: `{to_email}`\n"
                    f"⏰ Lần chạy tiếp theo: {next_run}"
                )

        elif action_type == "onedrive_watch":
            from .onedrive import add_watch
            sub_id = add_watch(
                user_aad_id=user_aad_id,
                sharepoint_url=payload["sharepoint_url"],
                schedule=payload["schedule"],
                analysis_request=payload.get("analysis_request", ""),
                conv_id=conv_id,
                service_url=service_url,
            )
            job_id = scheduler_manager.add_onedrive_watch_job(
                sub_id=sub_id, time_spec=payload["schedule"]
            )
            from .job_persist import save_job
            save_job(job_id, "onedrive", {"sub_id": sub_id, "time_spec": payload["schedule"]})
            jobs = scheduler_manager.list_jobs()
            job = next((j for j in jobs if j["id"] == job_id), None)
            next_run = job["next_run"] if job else "unknown"
            folder_name = payload["sharepoint_url"].rstrip("/").split("/")[-1]
            msg = (
                f"✅ **Xác thực Microsoft thành công!**\n\n"
                f"Đã tự động đặt lịch theo dõi OneDrive folder **`{folder_name}`** "
                f"theo lịch `{payload['schedule']}`.\n"
                f"⏰ Lần chạy tiếp theo: {next_run}"
            )

        elif action_type == "onedrive_excel_schedule":
            filename = payload["sharepoint_url"].rstrip("/").split("/")[-1]
            job_id = scheduler_manager.add_onedrive_excel_job(
                sharepoint_url=payload["sharepoint_url"],
                user_aad_id=user_aad_id,
                conv_id=conv_id,
                service_url=service_url,
                time_spec=payload["time_spec"],
                filename=filename,
                analysis_request=payload.get("analysis_request", ""),
                n_highlights=payload.get("n_highlights", 3),
            )
            from .job_persist import save_job as _save_job
            _save_job(job_id, "onedrive_excel", {
                "sharepoint_url": payload["sharepoint_url"],
                "user_aad_id": user_aad_id,
                "conv_id": conv_id,
                "service_url": service_url,
                "time_spec": payload["time_spec"],
                "filename": filename,
                "analysis_request": payload.get("analysis_request", ""),
                "n_highlights": payload.get("n_highlights", 3),
            })
            jobs = scheduler_manager.list_jobs()
            job = next((j for j in jobs if j["id"] == job_id), None)
            next_run = job["next_run"] if job else "unknown"
            req = payload.get("analysis_request", "") or "tổng quan dữ liệu"
            msg = (
                f"✅ **Xác thực Microsoft thành công!**\n\n"
                f"Đã tự động đặt lịch phân tích OneDrive Excel:\n"
                f"📄 **File**: `{filename}`\n"
                f"⏰ **Lịch**: {payload['time_spec']}\n"
                f"🔍 **Yêu cầu**: {req}\n"
                f"🔮 **Lần chạy tiếp theo**: {next_run}"
            )

        elif action_type == "analyze_onedrive":
            sharepoint_url = payload.get("sharepoint_url", "")
            analysis_req = payload.get("analysis_request", "")
            filename = sharepoint_url.rstrip("/").split("/")[-1] if sharepoint_url else "file"
            hint = f" về *{analysis_req}*" if analysis_req else ""
            msg = (
                f"✅ **Xác thực Microsoft thành công!**\n\n"
                f"Bot đã có quyền đọc file OneDrive của bạn. "
                f"Hãy gửi lại yêu cầu phân tích file **`{filename}`**{hint} để bắt đầu! 📊"
            )

        elif action_type == "email_send":
            from .onedrive import send_email_via_graph, get_user_email
            recipient = payload.get("to_email") or get_user_email(user_aad_id) or ""
            if not recipient:
                msg = "✅ Xác thực thành công!\n⚠️ Không thể xác định email — hãy chat lại để gửi báo cáo."
            else:
                success = send_email_via_graph(
                    user_aad_id,
                    recipient,
                    payload.get("subject", ""),
                    payload.get("body", ""),
                )
                if success:
                    msg = f"✅ **Xác thực thành công!** Đã tự động gửi email đến `{recipient}`."
                else:
                    msg = "✅ Xác thực thành công nhưng gửi email thất bại. Vui lòng thử lại."

    except Exception as e:
        logger.error("_handle_post_auth error (type=%s): %s", action_type, e)
        msg = f"✅ Xác thực Microsoft thành công!\n⚠️ Không thể tự động tạo job: {str(e)[:120]}"
    finally:
        clear_pending_auth_action(user_aad_id)

    if msg:
        ok = _send_teams_notification(conv_id, service_url, msg)
        if ok:
            logger.info("Post-auth Teams notification sent (type=%s user=%s)", action_type, user_aad_id[:8])
        else:
            logger.warning("Post-auth: failed to send notification (type=%s user=%s conv=%s)", action_type, user_aad_id[:8], conv_id[:12] if conv_id else "none")


@app.get("/auth/callback")
async def auth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    """OAuth2 redirect callback — exchange authorization code for delegated tokens."""
    if error:
        print(f"❌ OAUTH CALLBACK ERROR: {error} — {error_description}")
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
            f"<h2>❌ Xác thực thất bại</h2><p>{error_description}</p></body></html>",
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
            "<h2>❌ Thiếu tham số</h2></body></html>",
            status_code=400,
        )

    from .onedrive import exchange_code
    success = exchange_code(code, user_aad_id=state)
    print(f"{'✅' if success else '❌'} OAUTH CALLBACK: user {state[:8]} — {'ok' if success else 'failed'}")

    if success:
        asyncio.create_task(_handle_post_auth(state))
        return HTMLResponse("""
            <html><head><meta charset="utf-8"></head>
            <body style="font-family:sans-serif;text-align:center;padding:40px">
            <h2>✅ Xác thực thành công!</h2>
            <p>Bot đã được cấp quyền. Đang chuyển bạn về Microsoft Teams...</p>
            <p><a href="https://teams.microsoft.com">Nhấn vào đây nếu không tự chuyển</a></p>
            <script>
              // Try Teams desktop app first, fallback to web
              window.location.href = "msteams://";
              setTimeout(function() {
                window.location.href = "https://teams.microsoft.com";
              }, 1500);
            </script>
            </body></html>
        """)
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
        "<h2>❌ Xác thực thất bại</h2><p>Không thể lấy token. Vui lòng thử lại.</p>"
        "</body></html>",
        status_code=500,
    )


@app.post("/invocations")
async def invocations(request: Request):
    """AgentBase-compatible endpoint — xử lý đồng bộ, trả kết quả trong response."""
    body = await request.json()
    message: str = body.get("message", body.get("text", ""))
    response_text = await agent.handle_message(message)
    return JSONResponse(content={"status": "success", "response": response_text})


@app.get("/charts/{chart_id}")
async def serve_chart(chart_id: str):
    """Serve a generated chart PNG by ID."""
    from .charts import get_chart_path
    path = get_chart_path(chart_id)
    if not path:
        return JSONResponse({"error": "chart not found"}, status_code=404)
    return Response(content=path.read_bytes(), media_type="image/png")


@app.get("/health")
async def health():
    return {"status": "healthy"}
