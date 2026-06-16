# ZaloPay Data Agent — Teams AI Agent

Một AI agent cho **Microsoft Teams** chạy trên nền tảng **GreenNode AgentBase**. Agent nhận tin nhắn/file từ Teams, dùng LLM (LangChain ReAct) để gọi các công cụ phân tích dữ liệu Excel, vẽ biểu đồ, chạy A/B test thống kê, đọc tin tức, gửi email, và lập lịch tự động.

> Đây là tài liệu cho **ứng dụng agent** trong thư mục `app/`. README ở thư mục gốc của repo là tài liệu của **bộ skills GreenNode AgentBase** (một thứ khác).

---

## Mục lục

- [Tính năng chính](#tính-năng-chính)
- [Kiến trúc & luồng xử lý](#kiến-trúc--luồng-xử-lý)
- [Cấu trúc thư mục](#cấu-trúc-thư-mục)
- [Bộ công cụ (tools) của agent](#bộ-công-cụ-tools-của-agent)
- [Biến môi trường](#biến-môi-trường)
- [Chạy local](#chạy-local)
- [Chạy bằng Docker](#chạy-bằng-docker)
- [Deploy lên AgentBase Runtime](#deploy-lên-agentbase-runtime)
- [HTTP Endpoints](#http-endpoints)
- [Các khái niệm quan trọng](#các-khái-niệm-quan-trọng)
- [Debug & Troubleshooting](#debug--troubleshooting)

---

## Tính năng chính

| Nhóm | Mô tả |
|---|---|
| 📊 **Phân tích Excel** | Đọc file Excel đính kèm trong Teams qua Microsoft Graph Workbook API (không cần tải file). Trả lời câu hỏi follow-up bằng **pandas dataframe agent** — không đẩy CSV thô vào LLM nên file lớn không tràn context. |
| 📈 **Vẽ biểu đồ** | Sinh chart PNG bằng matplotlib/seaborn: bar, line, area, pie, donut, scatter, histogram, heatmap, **funnel** (phễu chuyển đổi). Gửi vào Teams dưới dạng Adaptive Card. |
| 🧪 **A/B Testing** | Phân tích thống kê bằng scipy (p-value, khoảng tin cậy, significance). |
| 📰 **Tin tức** | Fetch RSS, đọc nội dung bài báo, quản lý nguồn tin tùy chỉnh theo user. |
| ⏰ **Lập lịch tự động** | APScheduler: gửi tin tức / email / báo cáo Excel / theo dõi folder OneDrive theo lịch. Job được persist qua restart. |
| 📧 **Email** | Gửi báo cáo qua Microsoft Graph (delegated). |
| 🔐 **OAuth Microsoft** | Liên kết tài khoản Microsoft của user để đọc OneDrive và gửi email thay mặt user. |
| 🧠 **Memory** | Lịch sử hội thoại (short-term) + trích xuất facts dài hạn (long-term) qua AgentBase Memory, có cơ chế auto-compact để không tràn context. |

---

## Kiến trúc & luồng xử lý

```
                Microsoft Teams
                      │  (Bot Framework Activity, JSON)
                      ▼
       ┌──────────────────────────────────┐
       │  FastAPI  (app/main.py)           │
       │  POST /webhook/teams              │
       │   • parse text + file attachments │
       │   • trả 200 NGAY (fire-and-forget)│
       │   • bật typing indicator          │
       └───────────────┬──────────────────┘
                       │ asyncio.create_task
                       ▼
       ┌──────────────────────────────────┐
       │  TeamsAgent  (app/agent.py)       │
       │  LangChain create_agent (ReAct)   │
       │   • SYSTEM_PROMPT (tiếng Việt)    │
       │   • checkpointer = memory         │
       │   • auto-compact lịch sử          │
       └───────────────┬──────────────────┘
                       │ tool calls
                       ▼
       ┌──────────────────────────────────┐
       │  TOOLS  (app/tools.py — 24 tools) │
       │  Excel · Charts · A/B · News      │
       │  Schedule · Email · OneDrive·Auth │
       └───────────────┬──────────────────┘
                       │ kết quả (text/markdown)
                       ▼
       ┌──────────────────────────────────┐
       │  send_teams_reply (app/teams.py)  │
       │  Bot Framework Connector API      │
       └──────────────────────────────────┘
```

**Điểm cốt lõi:** webhook trả `200 OK` ngay lập tức rồi xử lý bất đồng bộ, vì Teams timeout nhanh. Agent **chủ động** gửi reply ngược lại qua Bot Framework Connector API (không trả trong response của webhook).

---

## Cấu trúc thư mục

```
app/
├── main.py         # FastAPI entrypoint: webhook, OAuth callback, chart serving, health
├── agent.py        # TeamsAgent — LangChain ReAct agent, system prompt, auto-compact lịch sử
├── tools.py        # 24 LangChain tools (Excel, chart, A/B, news, schedule, email, OneDrive, auth)
├── llm.py          # Factory tạo ChatOpenAI từ biến môi trường
├── memory.py       # Checkpointer (MemorySaver / AgentBaseMemoryEvents) + remember/recall tools
├── excel.py        # Parse Excel bytes / DataFrame → markdown phân tích
├── onedrive.py     # Microsoft Graph API: OAuth, delegated token, Workbook range, watch folder
├── teams.py        # Bot Framework: gửi reply, token cache, typing, Adaptive Card, email
├── charts.py       # Engine vẽ chart matplotlib/seaborn (9 loại, gồm funnel)
├── abtest.py       # A/B test thống kê (scipy)
├── scheduler.py    # APScheduler manager: các loại job + persistence
├── job_persist.py  # Lưu/khôi phục job qua diskcache (sống sót qua restart)
├── pg_store.py     # PostgreSQL store (user data, news sources, Teams context)
├── news.py         # RSS feedparser + trích xuất nội dung bài báo
└── README.md       # ← file này
```

---

## Bộ công cụ (tools) của agent

Tất cả đăng ký trong `TOOLS` ở cuối [tools.py](tools.py). LLM tự quyết định gọi tool nào dựa trên `SYSTEM_PROMPT`.

| Nhóm | Tools |
|---|---|
| **Excel / Dữ liệu** | `analyze_excel_file` (đọc file đính kèm qua Graph Workbook API + cache DataFrame), `query_excel_data` (hỏi follow-up bằng pandas agent), `analyze_onedrive_file` |
| **Biểu đồ** | `visualize_data` (sinh PNG → Adaptive Card) |
| **A/B Testing** | `get_ab_test_guide`, `analyze_ab_test` |
| **Tin tức** | `fetch_news`, `read_article`, `add_news_source`, `list_news_sources`, `remove_news_source` |
| **Lập lịch** | `schedule_news`, `schedule_email_news`, `schedule_excel_report`, `schedule_excel_workbook`, `schedule_onedrive_excel`, `watch_onedrive_folder`, `list_onedrive_watches`, `cancel_onedrive_watch`, `list_schedules`, `cancel_schedule` |
| **Email / Teams** | `send_email_report`, `send_teams_message` |
| **Xác thực** | `check_microsoft_auth` (sinh link OAuth Microsoft cho user) |

---

## Biến môi trường

Sao chép `.env.example` → `.env` (file `.env` **không bao giờ commit**). Danh sách biến mà code thực sự đọc:

### LLM (bắt buộc)
| Biến | Mô tả |
|---|---|
| `LLM_API_KEY` *(hoặc `OPENAI_API_KEY`)* | API key của provider OpenAI-compatible. |
| `LLM_BASE_URL` *(hoặc `OPENAI_API_BASE`)* | Endpoint. Mặc định `https://api.openai.com/v1`. GreenNode AIP: `https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1`. |
| `LLM_MODEL` | Tên model. Mặc định `gpt-3.5-turbo`. |

### Microsoft Teams Bot (bắt buộc để gửi reply)
| Biến | Mô tả |
|---|---|
| `TEAMS_APP_ID` | App (client) ID của Azure Bot. |
| `TEAMS_CLIENT_SECRET` | Client secret. |
| `TEAMS_TENANT_ID` | Azure AD tenant ID. |
| `TEAMS_SERVICE_URL` | Fallback serviceUrl cho scheduled jobs (mặc định `https://smba.trafficmanager.net/apac/`). |
| `TEAMS_WEBHOOK_SECRET` | *(tuỳ chọn)* shared secret xác thực webhook inbound. |

### Memory (tuỳ chọn)
| Biến | Mô tả |
|---|---|
| `MEMORY_ID` *(hoặc `AGENTBASE_MEMORY_ID`)* | ID memory store của AgentBase. **Để trống → dùng `MemorySaver` in-process** (reset khi restart). Có giá trị → `AgentBaseMemoryEvents` (persist qua platform). |
| `MEMORY_STRATEGY_ID` | Strategy long-term memory. Mặc định `default`. |

### Hạ tầng & khác
| Biến | Mô tả |
|---|---|
| `BOT_BASE_URL` | URL public của bot — dùng để dựng link OAuth callback và URL ảnh chart. **Bắt buộc** cho OAuth/chart hoạt động. |
| `DATABASE_URL` | PostgreSQL connection string (lưu user, nguồn tin, Teams context). |
| `SCHEDULER_DB_URL` | DB cho APScheduler. Mặc định `sqlite:///data/jobs.sqlite`. |
| `NEWS_RSS_FEEDS` | Danh sách RSS mặc định (CSV). Trống = TechCrunch + Hacker News. |
| `ALLOW_RUN_CMD` | `true` để bật tool chạy shell (NGUY HIỂM). Mặc định `false`. |

> **Trên AgentBase Runtime**, các biến `GREENNODE_CLIENT_ID`, `GREENNODE_CLIENT_SECRET`, `GREENNODE_AGENT_IDENTITY`, `GREENNODE_ENDPOINT_URL` được **runtime tự inject** — KHÔNG đặt thủ công. SDK dùng chúng để truy cập Memory service.

---

## Chạy local

```bash
# 1. Tạo virtualenv + cài deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Cấu hình
cp .env.example .env       # rồi điền LLM_*, TEAMS_* ...

# 3. Chạy (local dùng port 8000; runtime dùng 8080)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Test nhanh không cần Teams (endpoint đồng bộ):

```bash
curl -X POST http://localhost:8000/invocations \
  -H "Content-Type: application/json" \
  -d '{"message":"Cho tôi tin công nghệ mới nhất"}'
```

Mô phỏng payload Teams webhook (bất đồng bộ — reply gửi ngược qua Bot Framework):

```bash
curl -X POST http://localhost:8000/webhook/teams \
  -H "Content-Type: application/json" \
  -d '{"type":"message","text":"phân tích funnel TikTok","from":{"id":"u1","aadObjectId":"..."},"conversation":{"id":"c1"},"serviceUrl":"https://smba.trafficmanager.net/apac/"}'
```

---

## Chạy bằng Docker

```bash
# Build (runtime chạy amd64 — build đúng platform)
docker build --platform linux/amd64 -t teams-agent:local .

# Run
docker run --rm --env-file .env -p 8080:8080 teams-agent:local
```

Hoặc dùng docker-compose (kèm volume `./data` để persist job SQLite):

```bash
cp .env.example .env   # điền giá trị
docker compose up --build
```

**Lưu ý:** Dockerfile chạy bằng user non-root `appuser`, expose **port 8080**, healthcheck `GET /health`. Đây là contract của AgentBase Runtime.

---

## Deploy lên AgentBase Runtime

Dùng skill `/agentbase-deploy` (hoặc `/agentbase-wizard`). Tóm tắt pipeline:

1. **Build** image `linux/amd64`.
2. **Push** lên Container Registry (managed CR hoặc registry ngoài).
3. **Create/Update** runtime với `--env-file .env`, flavor phù hợp.
4. Runtime tự inject IAM credentials → Memory service hoạt động.

Contract bắt buộc: container lắng nghe **port 8080** và `GET /health` trả `200`.

---

## HTTP Endpoints

| Method | Path | Mô tả |
|---|---|---|
| `GET` | `/` | Status đơn giản. |
| `GET` | `/health` | Health check (runtime yêu cầu → 200). |
| `POST` | `/webhook/teams` | Nhận Bot Framework Activity từ Teams. Trả 200 ngay, xử lý + reply bất đồng bộ. |
| `POST` | `/invocations` | Endpoint đồng bộ (AgentBase-style) — trả kết quả trong response. Tiện cho test. |
| `GET` | `/auth/callback` | OAuth2 redirect — đổi authorization code lấy delegated token, rồi chạy pending action. |
| `GET` | `/charts/{chart_id}` | Serve ảnh PNG của chart đã sinh. |

---

## Các khái niệm quan trọng

### Pipeline phân tích Excel
1. User gửi file `.xlsx` trong Teams → `analyze_excel_file` resolve `driveId`/`itemId` qua Graph, đọc `usedRange` bằng Workbook API, build DataFrame và **cache** vào `_excel_cache` (key `drive_id:item_id:sheet_name`, TTL 2h).
2. Câu hỏi follow-up → `query_excel_data` lấy DataFrame từ cache và chạy **pandas dataframe agent** (`_pandas_agent_answer`) để sinh + chạy code pandas, chỉ trả kết quả đã tính. LLM chính không bao giờ thấy CSV thô → không tràn context.
3. Khi cache miss (restart/TTL hết) → tự fetch lại qua Graph.

### Memory & auto-compact
- Checkpointer chọn theo `MEMORY_ID`: trống → `MemorySaver`; có giá trị → `AgentBaseMemoryEvents`.
- `thread_id = "{aad_id}:{conv_id}"` để cô lập lịch sử theo user + hội thoại.
- **Auto-compact 2 pha** trong [agent.py](agent.py): Pha 1 (sync) thu gọn ToolMessage Excel cũ thành stub; Pha 2 (async nền) tóm tắt các exchange cũ bằng LLM khi lịch sử > ngưỡng. Lệnh `/compact` chạy compact thủ công.

### Lập lịch
- `scheduler.py` (APScheduler) + `job_persist.py` (diskcache) → job sống sót qua restart (`restore_from_persist()` lúc startup).
- Các loại job: tin tức, email tin tức, báo cáo Excel, workbook, theo dõi folder OneDrive.

### Luồng OAuth Microsoft
- Khi cần đọc OneDrive/gửi email mà chưa có token → tool trả link đăng nhập (`check_microsoft_auth`).
- User đăng nhập → `/auth/callback` đổi code lấy delegated token → chạy *pending action* đã lưu (vd: đặt lịch email) → thông báo lại user qua Teams.

---

## Debug & Troubleshooting

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| Agent trả **JSON thô** thay vì vẽ chart | LLM nhận CSV thô do pandas agent fail → sinh JSON dạng text. Kiểm tra `langchain-experimental` + `tabulate` đã cài, và `query_excel_data` chạy được pandas agent. |
| `Không tìm thấy kênh "...TikTok"` | Dữ liệu Excel có ký tự `\xa0` (non-breaking space). DataFrame được strip whitespace khi build; filter pandas nên dùng `.str.strip().str.lower()`. |
| Pandas agent trả rỗng | Model yếu (vd `gpt-oss-20b`) sinh code pandas kém → fallback sang CSV (`_format_df_for_llm`). |
| Cột cuối không hiện trong phân tích | Giới hạn **hiển thị** `_MAX_COLS_PREVIEW=10` / `_MAX_ROWS_STATS=8` trong [excel.py](excel.py). Dữ liệu đầy đủ vẫn được cache & tính toán. |
| Reply không gửi được vào Teams | Thiếu `TEAMS_APP_ID/SECRET/TENANT_ID` hoặc `service_url`/`conv_id` rỗng. Xem log `SEND SKIPPED`. |
| Memory không persist qua restart | `MEMORY_ID` để trống → đang dùng `MemorySaver` in-process. Đặt `MEMORY_ID` để dùng AgentBase Memory. |
| Chart/OAuth link sai | `BOT_BASE_URL` chưa đặt hoặc sai → link callback và URL ảnh không tới được. |

**Xem log container:**
```bash
docker logs -f <container>          # local
# hoặc skill /agentbase-monitor cho runtime đã deploy
```
