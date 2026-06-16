"""Excel file download and analysis for Teams attachments."""

import io
import os
import logging

import requests
import pandas as pd

logger = logging.getLogger(__name__)

_MAX_ROWS_STATS = 8    # max numeric columns for describe()
_MAX_COLS_PREVIEW = 10 # max columns to show in head() preview

_MAX_UPLOAD_MB = 200
_MAX_UPLOAD_BYTES = _MAX_UPLOAD_MB * 1024 * 1024

# Sheet names that are treated as column-definition dictionaries, not data
_README_NAMES = frozenset({"readme", "definitions", "data_dict", "legend", "column_def", "mô tả", "giải thích"})


def _is_readme_sheet(name: str) -> bool:
    n = name.lower().strip()
    return n in _README_NAMES or "readme" in n or "definition" in n or "mô tả" in n


def download_file_bytes(download_url: str, timeout: int = 60) -> bytes:
    """Download raw bytes from a Teams pre-authenticated downloadUrl. Max 50MB."""
    resp = requests.get(download_url, timeout=timeout, stream=True)
    resp.raise_for_status()

    # Fast-fail if Content-Length header is present and clearly oversized
    cl = resp.headers.get("Content-Length")
    if cl and int(cl) > _MAX_UPLOAD_BYTES:
        resp.close()
        raise ValueError(
            f"File quá lớn ({int(cl) // 1024 // 1024}MB). "
            f"Giới hạn tối đa là {_MAX_UPLOAD_MB}MB."
        )

    # Stream-read in 1MB chunks with running size check
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=1024 * 1024):
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            resp.close()
            raise ValueError(
                f"File quá lớn (>{_MAX_UPLOAD_MB}MB). "
                f"Giới hạn tối đa là {_MAX_UPLOAD_MB}MB."
            )
    return b"".join(chunks)


def load_excel_from_source(
    file_source: str,
    content_url: str = "",
    user_aad_id: str = "",
    unique_id: str = "",
) -> bytes:
    """Load Excel bytes from local path, SharePoint (Graph API), or direct URL.

    Resolution order:
    1. Local filesystem path (starts with / or ./)
    2. Microsoft Graph API — if content_url + user_aad_id + TEAMS_* env vars present
    3. Direct HTTP GET of file_source (works for temp-auth URLs or plain HTTP)

    Args:
        file_source: Local path or URL to fetch.
        content_url: Stable SharePoint contentUrl (for Graph API download on scheduled runs).
        user_aad_id: Azure AD object ID of the file owner.
        unique_id: SharePoint file uniqueId (for logging/reference only).
    """
    # 1. Local path
    if file_source.startswith("/") or file_source.startswith("./"):
        with open(file_source, "rb") as f:
            return f.read()

    # 2. Graph API — preferred for scheduled jobs (stable, no token expiry)
    app_id = os.getenv("TEAMS_APP_ID", "")
    client_secret = os.getenv("TEAMS_CLIENT_SECRET", "")
    tenant_id = os.getenv("TEAMS_TENANT_ID", "")

    if content_url and user_aad_id and all([app_id, client_secret, tenant_id]):
        from .teams import download_sharepoint_file
        data = download_sharepoint_file(content_url, user_aad_id, app_id, client_secret, tenant_id)
        if data:
            return data
        logger.warning("Graph download failed, falling back to direct URL")

    # 3. Direct HTTP download (temp-auth URL or any plain URL)
    url = file_source or content_url
    if not url:
        raise ValueError("No file_source or content_url provided")
    return download_file_bytes(url)


def analyze_workbook_df(
    df: pd.DataFrame,
    filename: str = "workbook",
    sheet_name: str = "",
    analysis_request: str = "",
) -> str:
    """Analyze a pandas DataFrame (loaded from Graph Workbook API) and return structured markdown.

    Produces the same output format as analyze_excel_bytes but takes a DataFrame directly —
    used by scheduled workbook jobs that fetch data via Workbook API instead of downloading bytes.
    """
    rows, cols_count = len(df), len(df.columns)
    lines = [
        f"## 📊 Phân tích file: **{filename}**",
        f"- Sheet: `{sheet_name}`" if sheet_name else "",
        f"- **Kích thước**: {rows} hàng × {cols_count} cột",
        f"- **Cột**: {', '.join(f'`{c}`' for c in df.columns)}",
    ]
    if analysis_request:
        lines.append(f"- Yêu cầu phân tích: *{analysis_request}*")
    lines.append("")

    # Null / missing values
    null_counts = df.isnull().sum()
    missing = null_counts[null_counts > 0]
    if not missing.empty:
        null_info = ", ".join(f"`{col}`: {cnt}" for col, cnt in missing.items())
        lines.append(f"- **Giá trị thiếu**: {null_info}")
    else:
        lines.append("- **Giá trị thiếu**: Không có")

    # Data types summary
    dtype_map: dict[str, list] = {}
    for col, dtype in df.dtypes.items():
        dtype_map.setdefault(str(dtype), []).append(str(col))
    dtype_summary = "; ".join(f"{k}: {v[:3]}" for k, v in dtype_map.items())
    lines.append(f"- **Kiểu dữ liệu**: {dtype_summary}")
    lines.append("")

    # Numeric statistics
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if numeric_cols:
        cols_to_show = numeric_cols[:_MAX_ROWS_STATS]
        lines.append(f"**Thống kê mô tả** ({', '.join(f'`{c}`' for c in cols_to_show)}):")
        stats = df[cols_to_show].describe().round(2)
        lines.append("```")
        lines.append(stats.to_string())
        lines.append("```")
        lines.append("")

    # 5 rows preview
    preview_df = df.head(5).iloc[:, :_MAX_COLS_PREVIEW]
    lines.append("**5 hàng đầu tiên:**")
    lines.append("```")
    lines.append(preview_df.to_string(index=False))
    lines.append("```")

    return "\n".join(l for l in lines if l is not None)


def analyze_excel_bytes(
    file_bytes: bytes,
    filename: str = "file.xlsx",
    analysis_request: str = "",
) -> str:
    """Parse Excel bytes and return a structured markdown analysis."""
    try:
        xls = pd.ExcelFile(io.BytesIO(file_bytes))
    except Exception as e:
        return f"❌ Không thể đọc file Excel: {e}"

    sheet_names = xls.sheet_names
    lines = [
        f"## 📊 Phân tích file: **{filename}**",
        f"- Số sheet: {len(sheet_names)}",
        f"- Tên sheet: {', '.join(f'`{s}`' for s in sheet_names)}",
    ]
    if analysis_request:
        lines.append(f"- Yêu cầu phân tích: *{analysis_request}*")
    lines.append("")

    # First pass: extract full column definitions from README-like sheets.
    # These definitions are injected before data-sheet analysis so the LLM
    # knows the semantic meaning of each column when generating insights.
    col_defs: dict[str, str] = {}
    readme_sheets: set[str] = set()
    for sname in sheet_names:
        if not _is_readme_sheet(sname):
            continue
        try:
            df_r = xls.parse(sname)
            if len(df_r.columns) >= 2:
                for _, row in df_r.iterrows():
                    k = str(row.iloc[0]).strip()
                    v = str(row.iloc[1]).strip()
                    if k and k.lower() not in ("nan", "column", "cột", "field", "tên cột", "col"):
                        col_defs[k] = v
            readme_sheets.add(sname)
        except Exception:
            pass

    if col_defs:
        lines.append("## 📋 Định nghĩa cột (trích từ sheet README)")
        lines.append("```")
        for col_name, col_desc in col_defs.items():
            lines.append(f"{col_name}: {col_desc}")
        lines.append("```")
        lines.append("")

    # Second pass: analyze each sheet
    for sheet in sheet_names:
        # README sheets: already fully extracted above — skip heavy stats
        if sheet in readme_sheets:
            lines.append(f"### Sheet: `{sheet}` _(định nghĩa cột — đã trích xuất đầy đủ ở trên)_\n")
            continue

        try:
            df = xls.parse(sheet)
        except Exception as e:
            lines.append(f"### Sheet `{sheet}` — ⚠️ Không đọc được: {e}\n")
            continue

        lines.append(f"### Sheet: `{sheet}`")
        lines.append(f"- **Kích thước**: {len(df)} hàng × {len(df.columns)} cột")
        lines.append(f"- **Cột**: {', '.join(f'`{c}`' for c in df.columns)}")

        # Annotate columns that have definitions from README
        if col_defs:
            annotated = [f"`{c}` ({col_defs[c][:40]})" for c in df.columns if str(c) in col_defs]
            if annotated:
                lines.append(f"- **Ngữ nghĩa cột**: {', '.join(annotated)}")

        # Null / missing values
        null_counts = df.isnull().sum()
        missing = null_counts[null_counts > 0]
        if not missing.empty:
            null_info = ", ".join(f"`{col}`: {cnt}" for col, cnt in missing.items())
            lines.append(f"- **Giá trị thiếu**: {null_info}")
        else:
            lines.append("- **Giá trị thiếu**: Không có")

        # Data types summary
        dtype_map: dict[str, list] = {}
        for col, dtype in df.dtypes.items():
            key = str(dtype)
            dtype_map.setdefault(key, []).append(str(col))
        dtype_summary = "; ".join(f"{k}: {v[:3]}" for k, v in dtype_map.items())
        lines.append(f"- **Kiểu dữ liệu**: {dtype_summary}")

        # Numeric statistics
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if numeric_cols:
            cols_to_show = numeric_cols[:_MAX_ROWS_STATS]
            lines.append(f"\n**Thống kê mô tả** ({', '.join(f'`{c}`' for c in cols_to_show)}):")
            stats = df[cols_to_show].describe().round(2)
            lines.append("```")
            lines.append(stats.to_string())
            lines.append("```")

        # Top 5 rows preview
        preview_df = df.head(5).iloc[:, :_MAX_COLS_PREVIEW]
        lines.append("\n**5 hàng đầu tiên:**")
        lines.append("```")
        lines.append(preview_df.to_string(index=False))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
