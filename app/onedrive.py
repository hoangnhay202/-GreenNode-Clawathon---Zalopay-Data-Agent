"""OneDrive delegated access — OAuth2 Authorization Code flow + Microsoft Graph API."""
from __future__ import annotations

import os
import re
import time
import uuid
import base64
import logging
from urllib.parse import urlencode, unquote

import requests

from . import pg_store

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_OAUTH_SCOPES = "Files.Read offline_access User.Read Mail.Send"


def _app_id() -> str:
    return os.getenv("TEAMS_APP_ID", "")


def _app_secret() -> str:
    return os.getenv("TEAMS_CLIENT_SECRET", "")


def _tenant_id() -> str:
    return os.getenv("TEAMS_TENANT_ID", "")


def _redirect_uri() -> str:
    base = os.getenv("BOT_BASE_URL", "").rstrip("/")
    return f"{base}/auth/callback"


# ── OAuth2 Authorization Code Flow ──────────────────────────────────────────

def get_auth_url(user_aad_id: str) -> str:
    """Generate Microsoft OAuth2 authorization URL for the user."""
    params = {
        "client_id": _app_id(),
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "scope": _OAUTH_SCOPES,
        "state": user_aad_id,
        "response_mode": "query",
        "prompt": "consent",
    }
    return (
        f"https://login.microsoftonline.com/{_tenant_id()}/oauth2/v2.0/authorize"
        f"?{urlencode(params)}"
    )


def exchange_code(code: str, user_aad_id: str) -> bool:
    """Exchange authorization code for tokens and cache them. Returns True on success."""
    url = f"https://login.microsoftonline.com/{_tenant_id()}/oauth2/v2.0/token"
    try:
        resp = requests.post(url, data={
            "grant_type": "authorization_code",
            "client_id": _app_id(),
            "client_secret": _app_secret(),
            "code": code,
            "redirect_uri": _redirect_uri(),
            "scope": _OAUTH_SCOPES,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _save_tokens(user_aad_id, data)
        print(f"✅ OAUTH: tokens saved for user {user_aad_id[:8]}")
        # Eagerly cache user email so agent never needs to ask for it
        _fetch_and_cache_email(user_aad_id, data["access_token"])
        return True
    except Exception as e:
        logger.error("❌ [OAUTH] Code exchange failed: %s", e)
        return False


def _save_tokens(user_aad_id: str, data: dict) -> None:
    pg_store.set_oauth_tokens(user_aad_id, {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": time.time() + data.get("expires_in", 3600) - 60,
    })


def _refresh_access_token(user_aad_id: str, refresh_token: str) -> str | None:
    url = f"https://login.microsoftonline.com/{_tenant_id()}/oauth2/v2.0/token"
    try:
        resp = requests.post(url, data={
            "grant_type": "refresh_token",
            "client_id": _app_id(),
            "client_secret": _app_secret(),
            "refresh_token": refresh_token,
            "scope": _OAUTH_SCOPES,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        _save_tokens(user_aad_id, data)
        print(f"🔄 OAUTH: token refreshed for user {user_aad_id[:8]}")
        return data["access_token"]
    except Exception as e:
        logger.error("❌ [OAUTH] Refresh failed for %s: %s", user_aad_id[:8], e)
        return None


def _fetch_and_cache_email(user_aad_id: str, access_token: str) -> str | None:
    """Call Graph /me to get user email and store it in cache. Returns email or None."""
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params={"$select": "mail,userPrincipalName"},
            timeout=10,
        )
        if resp.ok:
            profile = resp.json()
            email = profile.get("mail") or profile.get("userPrincipalName") or ""
            if email:
                pg_store.upsert_user(user_aad_id, email=email)
                print(f"📧 OAUTH: email cached for user {user_aad_id[:8]} → {email}")
                return email
    except Exception as e:
        logger.error("❌ [OAUTH] Failed to fetch /me email: %s", e)
    return None


def get_user_email(user_aad_id: str) -> str | None:
    """Return the user's email address — from db or Graph /me (lazy fetch).

    Tries db first. If not cached but a valid token exists, calls Graph /me
    to retrieve and cache the email. Returns None if token is unavailable.
    """
    user = pg_store.get_user(user_aad_id)
    if user and user.get("email"):
        return user["email"]
    token = get_delegated_token(user_aad_id)
    if not token:
        return None
    return _fetch_and_cache_email(user_aad_id, token)


def get_delegated_token(user_aad_id: str) -> str | None:
    """Return a valid delegated access token, auto-refreshing if needed."""
    tokens = pg_store.get_oauth_tokens(user_aad_id)
    if not tokens:
        return None
    if time.time() < tokens["expires_at"]:
        return tokens["access_token"]
    if tokens.get("refresh_token"):
        return _refresh_access_token(user_aad_id, tokens["refresh_token"])
    return None


def is_authorized(user_aad_id: str) -> bool:
    """Check if the user has stored OAuth tokens (access or refresh)."""
    tokens = pg_store.get_oauth_tokens(user_aad_id)
    return bool(tokens and (tokens.get("access_token") or tokens.get("refresh_token")))


# ── Microsoft Graph API ──────────────────────────────────────────────────────

def _extract_drive_path(sharepoint_url: str) -> str | None:
    """Extract the drive-root-relative path from a personal OneDrive URL.

    Handles both direct paths and SharePoint view/sharing links (/:x:/r/ format).
    Returns e.g. '/Documents/folder/file.xlsx', or None if pattern not recognized.

    Supported formats:
      - https://tenant-my.sharepoint.com/personal/{upn}/Documents/file.xlsx
      - https://tenant-my.sharepoint.com/:x:/r/personal/{upn}/Documents/file.xlsx?d=...
    """
    # Strip query string, then decode %20 → space etc.
    clean = unquote(sharepoint_url.split("?")[0])
    # Remove SharePoint view prefix /:X:/r (Excel=:x:, Folder=:f:, Word=:w:, etc.)
    clean = re.sub(r"/:[a-z]:/r", "", clean, flags=re.IGNORECASE)
    # Extract path after /personal/{upn}/Documents — "Documents" is the document library
    # root which maps to the drive root, so we strip it from the returned path.
    # e.g. .../personal/user/Documents/Microsoft Teams Chat Files/f.xlsx → /Microsoft Teams Chat Files/f.xlsx
    m = re.search(r"/personal/[^/]+/Documents(.+)$", clean, re.IGNORECASE)
    return m.group(1) if m else None


def _encode_sharing_url(url: str) -> str:
    """Encode a SharePoint URL for use with Graph /shares/{id}/driveItem."""
    return base64.urlsafe_b64encode(f"u!{url}".encode()).decode().rstrip("=")


def _graph_get(path: str, token: str, params: dict | None = None) -> dict | None:
    url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            timeout=20,
        )
        if resp.ok:
            return resp.json()
        logger.error("Graph GET %s → %d %s", path[:80], resp.status_code, resp.text[:200])
        return None
    except Exception as e:
        logger.error("Graph GET exception: %s", e)
        return None


def resolve_sharepoint_url(sharepoint_url: str, token: str) -> dict | None:
    """Resolve any SharePoint/OneDrive URL → Graph driveItem metadata.

    Returns dict: {id, driveId, name, type ("file"|"folder"), lastModified, eTag, size}
    """
    encoded = _encode_sharing_url(sharepoint_url)
    item = _graph_get(f"{GRAPH_BASE}/shares/{encoded}/driveItem", token)
    if not item:
        return None
    is_folder = "folder" in item
    return {
        "id": item["id"],
        "driveId": item.get("parentReference", {}).get("driveId", ""),
        "name": item.get("name", ""),
        "type": "folder" if is_folder else "file",
        "childCount": item.get("folder", {}).get("childCount") if is_folder else None,
        "lastModified": item.get("lastModifiedDateTime", ""),
        "eTag": item.get("eTag", ""),
        "size": item.get("size", 0),
    }


def resolve_file_ids(
    content_url: str,
    user_aad_id: str,
    unique_id: str = "",
) -> tuple[str, str] | None:
    """Resolve a SharePoint URL to (drive_id, item_id) using the user's delegated token.

    Tries strategies in order:
    0. UniqueId direct: GET /me/drive/items/{unique_id} — fastest, requires Teams uniqueId.
    1. Path-based: GET /me/drive/root:{path} — works for personal OneDrive direct paths.
    2. Search by filename: GET /me/drive/search(q=...) — fallback when path resolution fails.
    3. Shares API: GET /shares/{encoded}/driveItem — works for sharing links.

    Returns (drive_id, item_id) or None on failure.
    """
    from urllib.parse import quote as _quote, unquote as _unquote

    token = get_delegated_token(user_aad_id)
    if not token:
        return None

    # Strategy 0: direct UniqueId lookup — only available from Teams attachment metadata
    if unique_id:
        item = _graph_get(f"/me/drive/items/{unique_id}", token)
        if item and item.get("id"):
            drive_id = item.get("parentReference", {}).get("driveId", "")
            item_id = item.get("id", "")
            if drive_id and item_id:
                logger.info(
                    "resolve_file_ids: uniqueId OK uid=%s drive=%s item=%s",
                    unique_id[:8], drive_id[:8], item_id[:8],
                )
                return (drive_id, item_id)
        logger.warning("resolve_file_ids: uniqueId lookup failed for %s", unique_id)

    # Strategy 1: path-based resolution — preferred for Teams attachment content_url
    drive_path = _extract_drive_path(content_url)
    if drive_path:
        encoded_path = _quote(drive_path, safe="/")
        item = _graph_get(f"/me/drive/root:{encoded_path}", token)
        if item and item.get("id"):
            drive_id = item.get("parentReference", {}).get("driveId", "")
            item_id = item.get("id", "")
            if drive_id and item_id:
                logger.info(
                    "resolve_file_ids: path-based OK drive=%s item=%s", drive_id[:8], item_id[:8]
                )
                return (drive_id, item_id)
        logger.warning("resolve_file_ids: path-based failed for %s", content_url[:80])

    # Strategy 2: search by filename — handles cases where path-based resolution fails
    # (e.g., Teams Chat Files stored at unexpected path, or encoding differences)
    raw_filename = _unquote(content_url.rstrip("/").split("/")[-1])
    if raw_filename:
        search_data = _graph_get(f"/me/drive/search(q='{_quote(raw_filename)}')", token)
        if search_data and search_data.get("value"):
            for hit in search_data["value"]:
                if hit.get("name", "").lower() == raw_filename.lower() and not hit.get("folder"):
                    drive_id = hit.get("parentReference", {}).get("driveId", "")
                    item_id = hit.get("id", "")
                    if drive_id and item_id:
                        logger.info(
                            "resolve_file_ids: search OK file=%s drive=%s item=%s",
                            raw_filename, drive_id[:8], item_id[:8],
                        )
                        return (drive_id, item_id)
        logger.warning("resolve_file_ids: search failed for filename=%s", raw_filename)

    # Strategy 3: shares API — works for explicit sharing links
    item = resolve_sharepoint_url(content_url, token)
    if not item or item.get("type") != "file":
        return None
    drive_id = item.get("driveId", "")
    item_id = item.get("id", "")
    if not drive_id or not item_id:
        return None
    logger.info("resolve_file_ids: shares API OK drive=%s item=%s", drive_id[:8], item_id[:8])
    return (drive_id, item_id)


def fetch_workbook_range(
    drive_id: str,
    item_id: str,
    token: str,
    sheet_name: str = "",
) -> dict | None:
    """Fetch all cell data from an Excel worksheet via Microsoft Graph Workbook API.

    Does NOT require downloading the Excel file — reads data directly as JSON.
    Returns dict with keys: sheet_name, headers, rows, total_rows, all_sheets.
    Returns None on any failure.
    """
    # 1. List worksheets
    ws_data = _graph_get(f"/drives/{drive_id}/items/{item_id}/workbook/worksheets", token)
    if not ws_data or not ws_data.get("value"):
        logger.error("fetch_workbook_range: no worksheets found drive=%s item=%s", drive_id[:8], item_id[:8])
        return None

    worksheets = ws_data["value"]
    all_sheet_names = [w["name"] for w in worksheets]

    # Select target worksheet
    if sheet_name:
        ws = next((w for w in worksheets if w["name"].lower() == sheet_name.lower()), worksheets[0])
    else:
        ws = worksheets[0]

    ws_id = ws["id"]   # use the opaque ID, not the name, to avoid URL-encoding issues
    ws_name = ws["name"]

    # 2. Fetch usedRange (values only — no formulas, no formatting)
    range_data = _graph_get(
        f"/drives/{drive_id}/items/{item_id}/workbook/worksheets/{ws_id}/usedRange(valuesOnly=true)",
        token,
    )
    if not range_data or not range_data.get("values"):
        logger.error("fetch_workbook_range: empty usedRange for sheet=%s", ws_name)
        return None

    values = range_data["values"]
    if not values:
        return None

    headers = [str(h).strip() for h in values[0]]
    rows = [list(r) for r in values[1:]]

    logger.info(
        "fetch_workbook_range: sheet=%s rows=%d cols=%d",
        ws_name, len(rows), len(headers),
    )
    return {
        "sheet_name": ws_name,
        "headers": headers,
        "rows": rows,
        "total_rows": len(rows),
        "all_sheets": all_sheet_names,
    }


def list_excel_files(folder_sharepoint_url: str, token: str) -> list[dict]:
    """List all Excel (.xlsx/.xls) files directly inside a SharePoint folder.

    Returns list of dicts: {id, driveId, name, lastModified, eTag, size}
    """
    encoded = _encode_sharing_url(folder_sharepoint_url)
    folder_item = _graph_get(f"{GRAPH_BASE}/shares/{encoded}/driveItem", token)
    if not folder_item:
        return []

    drive_id = folder_item.get("parentReference", {}).get("driveId", "")
    item_id = folder_item["id"]
    if not drive_id:
        return []

    data = _graph_get(
        f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/children",
        token,
        params={"$select": "id,name,lastModifiedDateTime,eTag,size,file,folder"},
    )
    if not data:
        return []

    results = []
    for child in data.get("value", []):
        if "folder" in child:
            continue
        name = child.get("name", "")
        if not (name.lower().endswith(".xlsx") or name.lower().endswith(".xls")):
            continue
        results.append({
            "id": child["id"],
            "driveId": drive_id,
            "name": name,
            "lastModified": child.get("lastModifiedDateTime", ""),
            "eTag": child.get("eTag", ""),
            "size": child.get("size", 0),
        })

    print(f"📂 Found {len(results)} Excel file(s) in folder")
    return results


def download_excel_by_id(item_id: str, drive_id: str, token: str) -> bytes | None:
    """Download an Excel file by its Graph driveItem ID."""
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            allow_redirects=True,
        )
        if resp.ok:
            print(f"📥 Downloaded {item_id[:8]} ({len(resp.content)} bytes)")
            return resp.content
        logger.error("Download failed: %d %s", resp.status_code, resp.text[:100])
        return None
    except Exception as e:
        logger.error("Download exception: %s", e)
        return None


def download_onedrive_file(sharepoint_url: str, user_aad_id: str) -> bytes | None:
    """Download any file from OneDrive/SharePoint by URL using the user's delegated token.

    Tries three strategies in order:
    0. /me/drive/root:{path}:/content — most reliable for personal OneDrive files;
       works with /:x:/r/ view links and direct paths; no sharing-token expiry issues.
    1. /shares/{encoded}/driveItem/content — fallback for non-personal URLs.
    2. Resolve driveItem metadata → download by drive/item ID (last resort).

    Returns bytes on success.
    Returns None if the user has no delegated token (caller should prompt re-auth).
    Raises RuntimeError with details if token exists but all download strategies fail.
    """
    token = get_delegated_token(user_aad_id)
    if not token:
        logger.warning("download_onedrive_file: no delegated token for user %s", user_aad_id[:8])
        return None

    errors: list[str] = []

    # Strategy 0: personal OneDrive path via /me/drive/root:{path}:
    # Handles /:x:/r/ view links and direct paths; avoids sharing-token expiry.
    drive_path = _extract_drive_path(sharepoint_url)
    if drive_path:
        try:
            resp = requests.get(
                f"{GRAPH_BASE}/me/drive/root:{drive_path}:/content",
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
                allow_redirects=True,
            )
            if resp.ok:
                print(f"📥 OneDrive /me/drive download OK ({len(resp.content)} bytes)")
                return resp.content
            errors.append(f"me/drive:{resp.status_code}:{resp.text[:200].replace(chr(10), ' ')}")
            logger.warning(
                "download_onedrive_file: /me/drive → %d %s",
                resp.status_code, resp.text[:120],
            )
        except Exception as e:
            errors.append(f"me/drive:exc:{e}")
            logger.warning("download_onedrive_file: /me/drive exception: %s", e)
    else:
        errors.append("me/drive:skip:URL pattern not matched")

    encoded = _encode_sharing_url(sharepoint_url)

    # Strategy 1: direct Shares content endpoint
    try:
        resp = requests.get(
            f"{GRAPH_BASE}/shares/{encoded}/driveItem/content",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            allow_redirects=True,
        )
        if resp.ok:
            print(f"📥 OneDrive Shares download OK ({len(resp.content)} bytes)")
            return resp.content
        errors.append(f"shares:{resp.status_code}:{resp.text[:200].replace(chr(10), ' ')}")
        logger.warning(
            "download_onedrive_file: Shares content → %d %s",
            resp.status_code, resp.text[:120],
        )
    except Exception as e:
        errors.append(f"shares:exc:{e}")
        logger.warning("download_onedrive_file: Shares content exception: %s", e)

    # Strategy 2: resolve driveItem metadata → download by drive/item ID
    item = resolve_sharepoint_url(sharepoint_url, token)
    if not item or item.get("type") != "file":
        errors.append("resolve:URL did not resolve to a file")
        logger.warning("download_onedrive_file: URL did not resolve to a file: %s", sharepoint_url[:80])
        logger.error(
            "download_onedrive_file: all strategies failed — user=%s url=%.80s errors=%s",
            user_aad_id[:8], sharepoint_url, errors,
        )
        raise RuntimeError(f"Tải file OneDrive thất bại ({'; '.join(errors)})")
    if not item.get("driveId"):
        errors.append(f"resolve:driveId missing for item {item.get('id', '?')[:8]}")
        logger.warning("download_onedrive_file: driveId missing for item %s", item.get("id", "?"))
        logger.error(
            "download_onedrive_file: all strategies failed — user=%s url=%.80s errors=%s",
            user_aad_id[:8], sharepoint_url, errors,
        )
        raise RuntimeError(f"Tải file OneDrive thất bại ({'; '.join(errors)})")

    result = download_excel_by_id(item["id"], item["driveId"], token)
    if result:
        return result

    errors.append(f"download_by_id:failed:item={item['id'][:8]}")
    logger.error(
        "download_onedrive_file: all strategies failed — user=%s url=%.80s errors=%s",
        user_aad_id[:8], sharepoint_url, errors,
    )
    raise RuntimeError(f"Tải file OneDrive thất bại ({'; '.join(errors)})")


# ── Subscription (Watch) management ─────────────────────────────────────────

def add_watch(
    user_aad_id: str,
    sharepoint_url: str,
    schedule: str,
    analysis_request: str,
    conv_id: str,
    service_url: str,
) -> str:
    """Create a new OneDrive folder watch. Returns sub_id."""
    sub_id = f"watch_{uuid.uuid4().hex[:10]}"
    pg_store.kv_set(f"onedrive_watch:{sub_id}", {
        "sub_id": sub_id,
        "user_aad_id": user_aad_id,
        "sharepoint_url": sharepoint_url,
        "schedule": schedule,
        "analysis_request": analysis_request,
        "conv_id": conv_id,
        "service_url": service_url,
        "created_at": time.time(),
        "last_checksums": {},
    })
    idx = pg_store.kv_get(f"onedrive_watches:{user_aad_id}") or []
    if sub_id not in idx:
        idx.append(sub_id)
    pg_store.kv_set(f"onedrive_watches:{user_aad_id}", idx)
    print(f"📌 Watch added: {sub_id} for user {user_aad_id[:8]}")
    return sub_id


def get_watch(sub_id: str) -> dict | None:
    return pg_store.kv_get(f"onedrive_watch:{sub_id}")


def update_watch_checksums(sub_id: str, checksums: dict) -> None:
    watch = get_watch(sub_id)
    if watch:
        watch["last_checksums"] = checksums
        pg_store.kv_set(f"onedrive_watch:{sub_id}", watch)


def list_watches(user_aad_id: str) -> list[dict]:
    return [
        w for sid in (pg_store.kv_get(f"onedrive_watches:{user_aad_id}") or [])
        if (w := pg_store.kv_get(f"onedrive_watch:{sid}"))
    ]


def remove_watch(sub_id: str) -> bool:
    watch = pg_store.kv_get(f"onedrive_watch:{sub_id}")
    if not watch:
        return False
    pg_store.kv_delete(f"onedrive_watch:{sub_id}")
    user_aad_id = watch.get("user_aad_id", "")
    if user_aad_id:
        idx = [s for s in (pg_store.kv_get(f"onedrive_watches:{user_aad_id}") or []) if s != sub_id]
        pg_store.kv_set(f"onedrive_watches:{user_aad_id}", idx)
    return True


# ── Email via Microsoft Graph ─────────────────────────────────────────────────

def send_email_via_graph(
    user_aad_id: str,
    to_email: str,
    subject: str,
    body_markdown: str,
) -> bool:
    """Send email as the authenticated user via Graph /me/sendMail (delegated Mail.Send)."""
    import re

    token = get_delegated_token(user_aad_id)
    if not token:
        return False

    # Minimal markdown → HTML conversion
    html = body_markdown
    html = re.sub(r"^#{1,3} (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    html = re.sub(r"\n\|(.+)", r"<br>|\1", html)  # rough table rows
    html = html.replace("\n\n", "<br><br>").replace("\n", "<br>")

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": f"<div style='font-family:sans-serif'>{html}</div>"},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        },
        "saveToSentItems": "false",
    }
    try:
        resp = requests.post(
            f"{GRAPH_BASE}/me/sendMail",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        if resp.status_code == 202:
            print(f"✅ EMAIL: sent to {to_email}")
            return True
        logger.error("Email send failed: %d %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.error("Email send exception: %s", e)
        return False


# ── Teams context + Pending auth action ─────────────────────────────────────


def save_teams_ctx(user_aad_id: str, conv_id: str, service_url: str) -> None:
    """Cache the user's latest Teams conv_id + service_url for use in post-auth callbacks."""
    if not user_aad_id:
        return
    pg_store.kv_set(f"teams_ctx:{user_aad_id}", {
        "conv_id": conv_id,
        "service_url": service_url,
    })


def get_teams_ctx(user_aad_id: str) -> dict | None:
    """Retrieve the last-seen Teams context for a user (conv_id + service_url)."""
    return pg_store.kv_get(f"teams_ctx:{user_aad_id}")


def save_pending_auth_action(
    user_aad_id: str,
    action_type: str,
    payload: dict,
) -> None:
    """Save an intent that should be executed automatically after OAuth completes.

    action_type values: "email_news" | "onedrive_watch" | "email_send"
    Teams context (conv_id + service_url) is resolved from the teams_ctx cache
    so callers don't need to pass it explicitly.
    Expires after 15 minutes — same window as Microsoft OAuth state parameter.
    """
    ctx = get_teams_ctx(user_aad_id) or {}
    pg_store.kv_set(
        f"pending_auth_action:{user_aad_id}",
        {
            "type": action_type,
            "conv_id": ctx.get("conv_id", ""),
            "service_url": ctx.get("service_url", ""),
            "payload": payload,
        },
        expire_seconds=900,
    )
    logger.info("Pending auth action saved: user=%s type=%s", user_aad_id[:8], action_type)


def get_pending_auth_action(user_aad_id: str) -> dict | None:
    """Return the pending post-auth action for a user, or None if none/expired."""
    return pg_store.kv_get(f"pending_auth_action:{user_aad_id}")


def clear_pending_auth_action(user_aad_id: str) -> None:
    pg_store.kv_delete(f"pending_auth_action:{user_aad_id}")
