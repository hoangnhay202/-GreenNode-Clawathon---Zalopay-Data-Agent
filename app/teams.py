"""Microsoft Teams Bot Framework API client."""
from __future__ import annotations

import os
import logging
from urllib.parse import urlparse, unquote

import requests

from . import pg_store

logger = logging.getLogger(__name__)

# Refresh 5 minutes before expiry; MS tokens last 60 min → 55 min TTL
_TOKEN_TTL = 55 * 60

# Teams Bot Framework hard limit is 28,000 chars; stay below with a safe buffer
_TEAMS_MAX_CHARS = 27_000


def _split_message(msg: str) -> list[str]:
    """Split a message into chunks ≤ _TEAMS_MAX_CHARS, breaking at paragraph then line boundaries."""
    if len(msg) <= _TEAMS_MAX_CHARS:
        return [msg]
    chunks: list[str] = []
    remaining = msg
    while len(remaining) > _TEAMS_MAX_CHARS:
        cut = remaining.rfind("\n\n", 0, _TEAMS_MAX_CHARS)
        if cut == -1:
            cut = remaining.rfind("\n", 0, _TEAMS_MAX_CHARS)
        if cut == -1:
            cut = _TEAMS_MAX_CHARS
        else:
            cut += 1
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks




def _get_token(app_id: str, client_secret: str, tenant_id: str, scope: str) -> str | None:
    """Generic Azure AD OAuth2 client_credentials token fetch (no cache)."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    try:
        resp = requests.post(url, data={
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": client_secret,
            "scope": scope,
        }, timeout=10)
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as e:
        logger.error("Failed to get token (scope=%s): %s", scope, e)
        return None


def _get_token_cached(app_id: str, client_secret: str, tenant_id: str, scope: str) -> str | None:
    """Get token with pg_store layer — calls Microsoft API only on cache miss or near-expiry."""
    cached = pg_store.get_app_token(scope)
    if cached:
        logger.info("⚡ [TOKEN] Cache hit (scope=...%s)", scope[-20:])
        return cached

    logger.info("🌐 [TOKEN] Cache miss — fetching from Microsoft (scope=...%s)", scope[-20:])
    token = _get_token(app_id, client_secret, tenant_id, scope)
    if token:
        pg_store.set_app_token(scope, token, _TOKEN_TTL)
        logger.info("✅ [TOKEN] Cached for %ds (scope=...%s)", _TOKEN_TTL, scope[-20:])
    return token


def get_access_token(app_id: str, client_secret: str, tenant_id: str) -> str | None:
    """Get cached Bot Framework Connector API token."""
    return _get_token_cached(app_id, client_secret, tenant_id, "https://api.botframework.com/.default")


def warm_bot_token() -> None:
    """Proactively fetch and cache the bot token. Call at startup and on a schedule."""
    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")
    if not all([app_id, client_secret, tenant_id]):
        return
    token = get_access_token(app_id, client_secret, tenant_id)
    if token:
        print("🔑 TOKEN: warmed bot token in cache")
    else:
        print("⚠️  TOKEN: warm-up failed — check TEAMS_APP_ID / TEAMS_CLIENT_SECRET / TEAMS_TENANT_ID")


# ── User info cache ──────────────────────────────────────────────────────────

def save_user(aad_id: str, name: str) -> None:
    """Lưu thông tin user vào PostgreSQL (không hết hạn)."""
    if not aad_id:
        return
    pg_store.upsert_user(aad_id, user_name=name)
    print(f"👤 USER CACHED: {aad_id} → {name}")


def get_user(aad_id: str) -> dict | None:
    """Lấy thông tin user từ PostgreSQL."""
    return pg_store.get_user(aad_id)


def get_user_details(
    service_url: str,
    conversation_id: str,
    member_id: str,
    access_token: str,
) -> dict | None:
    """Fetch user details (email, name, aad_id) from Bot Framework Connector API.

    member_id must be the Teams user ID from activity 'from.id' (e.g. '29:xxx...').
    Returns dict with keys: email, name, aad_id — or None on failure.
    """
    base_url = service_url.rstrip("/")
    endpoint = f"{base_url}/v3/conversations/{conversation_id}/members/{member_id}"
    try:
        resp = requests.get(
            endpoint,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "email": data.get("userPrincipalName"),
                "name": data.get("name"),
                "aad_id": data.get("aadObjectId"),
            }
        logger.warning("get_user_details: %d %s", resp.status_code, resp.text[:100])
        return None
    except Exception as e:
        logger.error("get_user_details exception: %s", e)
        return None


def fetch_and_save_email(
    user_aad_id: str,
    service_url: str,
    conv_id: str,
    user_id: str,
) -> None:
    """Fetch email via Bot Framework members API and save to DB if not already cached.

    Only calls the API when the user's email is missing — no-op on cache hit.
    """
    if not (user_aad_id and service_url and conv_id and user_id):
        return
    user = pg_store.get_user(user_aad_id)
    if user and user.get("email"):
        return

    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")
    if not all([app_id, client_secret, tenant_id]):
        return

    token = get_access_token(app_id, client_secret, tenant_id)
    if not token:
        return

    details = get_user_details(service_url, conv_id, user_id, token)
    if details and details.get("email"):
        pg_store.upsert_user(user_aad_id, email=details["email"])
        print(f"📧 EMAIL: fetched for {user_aad_id[:8]} → {details['email']}")
    else:
        logger.warning("fetch_and_save_email: no email returned for user %s", user_aad_id[:8])


def download_sharepoint_file(
    content_url: str,
    user_aad_id: str,
    app_id: str,
    client_secret: str,
    tenant_id: str,
) -> bytes | None:
    """Download a SharePoint/OneDrive file using Microsoft Graph API.

    Uses the file owner's AAD object ID + file path parsed from contentUrl.
    App registration must have Files.Read.All application permission.

    Args:
        content_url: Stable SharePoint URL, e.g.
            https://tenant-my.sharepoint.com/personal/user_company_com/Documents/report.xlsx
        user_aad_id: Azure AD object ID of the file owner (from Teams activity "from.aadObjectId").
        app_id, client_secret, tenant_id: Bot registration credentials.
    """
    # Parse relative path from the /personal/{user}/ portion of the URL
    parsed = urlparse(content_url)
    parts = parsed.path.split("/personal/", 1)
    if len(parts) != 2:
        logger.warning("download_sharepoint_file: unexpected contentUrl format: %s", content_url[:80])
        return None

    # Path after the user folder, e.g. "Documents/Teams Chat Files/report.xlsx"
    after_user = parts[1]
    slash = after_user.find("/")
    if slash < 0:
        return None
    relative_path = unquote(after_user[slash + 1:])

    token = _get_token_cached(app_id, client_secret, tenant_id, "https://graph.microsoft.com/.default")
    if not token:
        return None

    graph_url = (
        f"https://graph.microsoft.com/v1.0/users/{user_aad_id}"
        f"/drive/root:/{relative_path}:/content"
    )
    try:
        resp = requests.get(
            graph_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            allow_redirects=True,
        )
        if resp.ok:
            logger.info("Graph download OK: %s (%d bytes)", relative_path, len(resp.content))
            return resp.content
        logger.warning(
            "Graph download failed %s for user %s path '%s'",
            resp.status_code, user_aad_id[:8], relative_path,
        )
        return None
    except Exception as e:
        logger.error("Graph download exception: %s", e)
        return None


def fetch_message_via_graph(
    conv_id: str,
    message_id: str,
    app_id: str,
    client_secret: str,
    tenant_id: str,
    user_aad_id: str = "",
) -> list[dict]:
    """Fallback cho mobile (iOS/Android): lấy attachments từ Graph API khi webhook thiếu metadata.

    Teams Bot API không gửi attachment metadata cho file từ OneDrive/SharePoint trên mobile.
    Graph API GET /chats/{chatId}/messages/{messageId} trả về đầy đủ.
    Cần quyền Chat.Read (Application) trong Azure AD app registration.
    """
    token = _get_token_cached(app_id, client_secret, tenant_id, "https://graph.microsoft.com/.default")
    if not token:
        logger.warning("fetch_message_via_graph: no Graph token available")
        return []

    url = f"https://graph.microsoft.com/v1.0/chats/{conv_id}/messages/{message_id}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("fetch_message_via_graph: %d %s (conv=%s msg=%s)", resp.status_code, resp.text[:120], conv_id[:20], message_id)
            return []

        data = resp.json()
        attachments: list[dict] = []
        for att in data.get("attachments", []):
            content_url = att.get("contentUrl", "")
            name = att.get("name", "attachment")
            if not content_url:
                continue
            file_type = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            attachments.append({
                "name": name,
                "file_type": file_type,
                "download_url": "",
                "content_url": content_url,
                "unique_id": att.get("id", ""),
                "user_aad_id": user_aad_id,
            })
        logger.info("fetch_message_via_graph: found %d attachments for msg %s", len(attachments), message_id)
        return attachments
    except Exception as e:
        logger.error("fetch_message_via_graph exception: %s", e)
        return []


def send_typing_indicator(service_url: str, conv_id: str, token: str) -> bool:
    """Send a typing indicator activity to Teams conversation."""
    base_url = service_url.rstrip("/")
    endpoint = f"{base_url}/v3/conversations/{conv_id}/activities"
    try:
        resp = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"type": "typing"},
            timeout=5,
        )
        return resp.status_code in (200, 201, 202)
    except Exception as e:
        logger.debug("send_typing_indicator failed: %s", e)
        return False


def send_teams_reply(
    service_url: str,
    conv_id: str,
    token: str,
    msg: str,
    reply_to_id: str | None = None,
    app_id: str | None = None,
    client_secret: str | None = None,
    tenant_id: str | None = None,
) -> tuple[bool, str]:
    """Send message via Microsoft Bot Framework Connector API.

    service_url is taken dynamically from the incoming Teams activity payload
    to ensure correct regional routing. Messages longer than Teams' 28,000-char
    limit are automatically split into consecutive posts.
    """
    base_url = service_url.rstrip("/")
    endpoint_reply = (
        f"{base_url}/v3/conversations/{conv_id}/activities/{reply_to_id}"
        if reply_to_id else None
    )
    endpoint_new = f"{base_url}/v3/conversations/{conv_id}/activities"

    def _post_chunk(chunk: str, use_reply: bool, current_token: str) -> requests.Response:
        endpoint = endpoint_reply if use_reply else endpoint_new
        payload: dict = {
            "type": "message",
            "text": chunk,
            "textFormat": "markdown",
            "locale": "en-US",
        }
        if use_reply and reply_to_id:
            payload["replyToId"] = reply_to_id
        return requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {current_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )

    if not msg or not msg.strip():
        logger.warning("send_teams_reply called with empty message — skipping")
        return True, token

    chunks = _split_message(msg)
    all_ok = True
    current_token = token

    for i, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        use_reply = (i == 0) and bool(endpoint_reply)
        try:
            response = _post_chunk(chunk, use_reply, current_token)
            if response.status_code == 401 and all([app_id, client_secret, tenant_id]):
                pg_store.delete_app_token("https://api.botframework.com/.default")
                new_token = get_access_token(app_id, client_secret, tenant_id)
                if new_token:
                    current_token = new_token
                    response = _post_chunk(chunk, use_reply, current_token)
            if response.status_code not in (200, 201, 202):
                logger.error(
                    "Teams send failed (chunk %d/%d): %s %s",
                    i + 1, len(chunks), response.status_code, response.text[:200],
                )
                all_ok = False
        except Exception as e:
            logger.error("Teams send exception (chunk %d/%d): %s", i + 1, len(chunks), e)
            all_ok = False

    return all_ok, current_token


def send_teams_card_image(
    service_url: str,
    conv_id: str,
    token: str,
    image_url: str,
    caption: str = "",
    reply_to_id: str | None = None,
    app_id: str | None = None,
    client_secret: str | None = None,
    tenant_id: str | None = None,
) -> tuple[bool, str]:
    """Send an Adaptive Card with an inline image to a Teams conversation."""
    base_url = service_url.rstrip("/")
    if reply_to_id:
        endpoint = f"{base_url}/v3/conversations/{conv_id}/activities/{reply_to_id}"
    else:
        endpoint = f"{base_url}/v3/conversations/{conv_id}/activities"

    card_body = []
    if caption:
        card_body.append({
            "type": "TextBlock",
            "text": caption,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        })
    card_body.append({
        "type": "Image",
        "url": image_url,
        "size": "Stretch",
        "altText": caption or "chart",
    })

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.2",
                "body": card_body,
            },
        }],
    }
    if reply_to_id:
        payload["replyToId"] = reply_to_id

    def _post(current_token: str) -> requests.Response:
        return requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {current_token}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )

    try:
        response = _post(token)
        if response.status_code == 401 and all([app_id, client_secret, tenant_id]):
            pg_store.delete_app_token("https://api.botframework.com/.default")
            new_token = get_access_token(app_id, client_secret, tenant_id)
            if new_token:
                response = _post(new_token)
                token = new_token
        if response.status_code in (200, 201, 202):
            return True, token
        logger.error("Teams card send failed: %s %s", response.status_code, response.text[:200])
        return False, token
    except Exception as e:
        logger.error("Teams card send exception: %s", e)
        return False, token
