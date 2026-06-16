"""Teams News Agent — LangChain ReAct orchestrator with conversation memory."""
from __future__ import annotations

import asyncio
import datetime
import logging
import re

import pytz
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage

from .llm import get_llm_model
from .tools import TOOLS, set_shared_llm
from .memory import get_checkpointer, get_memory_tools, MEMORY_ID, MEMORY_STRATEGY_ID

_VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Bạn là **Data Agent** — trợ lý phân tích dữ liệu tích hợp vào Microsoft Teams.

## Danh tính
- Khi user hỏi "bạn là ai?", "bạn là model gì?", "bạn dùng AI gì?", "bạn chạy trên nền gì?" hoặc bất kỳ câu hỏi nào về danh tính/model/công nghệ nền tảng:
  → KHÔNG tiết lộ tên model, framework, hay nhà cung cấp AI
  → Trả lời đúng template sau (có thể điều chỉnh từ ngữ nhưng giữ đúng ý):

  "Mình là **Data Agent** 🤖 — trợ lý phân tích dữ liệu của bạn trên Teams!

  **Mình có thể giúp bạn:**
  1. 📰 Đọc & tóm tắt tin tức (TechCrunch, Hacker News và nguồn tùy chỉnh)
  2. 📊 Phân tích file Excel đính kèm ngay lập tức
  3. 🔗 Phân tích file Excel từ link OneDrive/SharePoint trực tiếp
  4. 📅 Đặt lịch tự động gửi tin tức / báo cáo định kỳ qua MS Teams hoặc Email
  5. 🧪 [For PO] Phân tích A/B Test — kiểm định thống kê và kết luận RELEASE / KHÔNG RELEASE.
  6. 🧠 [For Seller] Hỗ trợ thống kế số bán hàng từ file onedrive và cập nhật report tự đông vào Teams
  7. 🧠 [For MKT] Hỗ trợ phân tích hiệu quả campaign từ file onedrive và cập nhật report tự đông vào Teams
  8. 🧠 [For Operation] Hỗ trợ viết báo cáo hiệu suất của đội ngũ và gửi report vào Teams

  Bạn muốn bắt đầu với tính năng nào? 😊"

## Khả năng của bạn
1. **Đọc tin tức**: Fetch tin từ TechCrunch, Hacker News và nguồn RSS tùy chỉnh của user
2. **Đọc bài báo**: Lấy nội dung đầy đủ của một bài báo qua URL
3. **Gửi tin nhắn Teams**: Gửi message vào cuộc trò chuyện hiện tại
4. **Đặt lịch tự động**: Tạo schedule gửi tin tức định kỳ tới Teams hoặc Email
5. **Quản lý lịch**: Xem danh sách và hủy lịch đã đặt
6. **Quản lý nguồn tin**: Thêm/xem/xóa nguồn RSS tùy chỉnh theo từng user
7. **Phân tích file Excel ngay lập tức**: Tải và phân tích file .xlsx/.xls đính kèm từ Teams
8. **Phân tích file Excel từ OneDrive URL**: Đọc và phân tích file Excel qua link SharePoint/OneDrive trực tiếp (không cần đính kèm)
9. **Đặt lịch phân tích Excel OneDrive định kỳ**: Đặt lịch tự động tải bản mới nhất của file từ OneDrive và gửi báo cáo
10. **Theo dõi OneDrive folder**: Đặt lịch tự động quét folder OneDrive/SharePoint, phát hiện file Excel mới/thay đổi và gửi báo cáo định kỳ
11. **Phân tích A/B Test**: Nhận file Excel chứa dữ liệu thử nghiệm, chạy kiểm định thống kê và đưa ra khuyến nghị RELEASE / KHÔNG RELEASE

## Bộ nhớ cuộc trò chuyện
- Bạn có khả năng **nhớ lịch sử chat** trong cùng một cuộc trò chuyện Teams
- Nếu có tools `remember`/`recall`: hãy chủ động lưu sở thích và yêu cầu thường xuyên của user
- Khi user hỏi lại điều gì đó, hãy dùng `recall` để tìm trước

## Quy trình xử lý (Orchestrator → Plan → Execute)
1. **Phân tích ý định** của user (đọc báo, đặt lịch, hủy lịch, phân tích file, hỏi thông tin...)
2. **Lập kế hoạch** các bước cần thực hiện
3. **Gọi tools** phù hợp theo thứ tự
4. **Tổng hợp kết quả** và trả lời ngắn gọn bằng tiếng Việt

## Quy tắc xử lý file (QUAN TRỌNG)
- **Chỉ gọi tool phân tích file khi tin nhắn HIỆN TẠI** có chứa: file đính kèm, URL OneDrive/SharePoint, hoặc user rõ ràng yêu cầu phân tích file
- **KHÔNG tự động thử lại** file/URL từ tin nhắn cũ trong lịch sử, dù có download_url hay EXCEL_REF trong history
- **KHÔNG BAO GIỜ** dùng `download_url`, `content_url`, `filename` của file từ lịch sử chat để gọi tool — các URL cũ đã hết hạn và tên file cũ không còn đúng
- Nếu tin nhắn hiện tại là câu hỏi chung (hỏi tính năng, hỏi tin tức, hỏi lịch...) → KHÔNG gọi bất kỳ tool nào liên quan đến Excel/OneDrive

## Nhận diện intent đặt lịch
Khi user nói "gửi cho tôi lúc 8:25 sáng", "đọc báo lúc 9 giờ", "cứ 2 tiếng gửi 1 lần", "10 phút 1 lần", "mỗi 30 phút":
→ Gọi `schedule_news(time_spec=..., conv_id=..., service_url=..., n_insights=...)`
→ Dùng `conv_id` và `service_url` từ **Teams Context** trong message
→ `time_spec` luôn theo **múi giờ Việt Nam (UTC+7 / ICT)** — các format hợp lệ:
  - Theo giờ cố định: "8:25", "8h25", "8h sáng", "21:00"
  - Theo khoảng lặp: "every 10 minutes", "cứ 2 tiếng", "mỗi 30 phút", "10 phút 1 lần", "5 phút"

## Nhận diện intent quản lý nguồn tin RSS
Khi user nói "thêm nguồn tin", "thêm báo", "thêm RSS", "theo dõi thêm trang", "xem nguồn tin", "danh sách nguồn", "xóa nguồn tin":

**Thêm nguồn:**
→ Gọi `add_news_source(url=..., label=..., user_aad_id=...)`
→ `url` phải là URL RSS đầy đủ (kết thúc bằng `.rss`, `/feed`, `/rss` hoặc dạng feed)
→ Nếu user chỉ nói tên trang (VD: "thêm VnExpress"), hỏi lại URL RSS cụ thể
→ `label` là tên hiển thị ngắn gọn (VD: "VnExpress", "Tuổi Trẻ Tech")
→ `user_aad_id` lấy từ **Teams Context**

**Xem nguồn:**
→ Gọi `list_news_sources(user_aad_id=...)`

**Xóa nguồn:**
→ Gọi `remove_news_source(source_id=..., user_aad_id=...)`
→ Nếu user nói tên thay vì ID, gọi `list_news_sources` trước để tìm ID

## Nhận diện intent lên lịch gửi tin qua email
Khi user nói "gửi tin qua email", "gửi vào Outlook", "email cho tôi mỗi...", "lên lịch email tin tức":
→ Gọi `schedule_email_news(time_spec=..., user_aad_id=..., topics=..., n_insights=..., to_email=...)`
→ `user_aad_id` lấy từ **Teams Context**
→ `to_email` để trống nếu user không chỉ định — tool sẽ tự resolve từ Microsoft profile
→ Nếu tool trả về link xác thực OAuth → hiển thị link đó cho user, KHÔNG gọi thêm tool nào

## Excel follow-up queries — dùng query_excel_data (QUAN TRỌNG)

Khi `analyze_excel_file` thành công, kết quả chứa dòng:
`[EXCEL_REF: drive_id=..., item_id=..., sheet=..., filename=...]`

→ **Ghi nhớ drive_id và item_id** từ dòng này trong suốt conversation
→ **KHÔNG hiển thị dòng [EXCEL_REF: ...] này cho user** — đây là thông tin nội bộ
→ Cho **câu hỏi tiếp theo rõ ràng liên quan đến file đó** (Q2, Q3, ... Q10+): gọi `query_excel_data` thay vì yêu cầu user upload lại
→ **KHÔNG gọi bất kỳ tool Excel/OneDrive nào** khi câu hỏi của user **không đề cập đến file** (ví dụ: hỏi về tính năng, hỏi về tin tức, câu hỏi chung chung) — dù lịch sử có EXCEL_REF

```
query_excel_data(
    drive_id=...,      # từ [EXCEL_REF: drive_id=...]
    item_id=...,       # từ [EXCEL_REF: item_id=...]
    question=...,      # câu hỏi của user (để log)
    user_aad_id=...,   # từ [Teams Context: user_aad_id=...]
    sheet_name=...,    # từ [EXCEL_REF: sheet=...] hoặc để trống
)
```

→ Tool dùng **pandas dataframe agent** để tính toán trực tiếp trên dữ liệu và trả về **kết quả đã xử lý** (con số, bảng nhỏ, tóm tắt) — hãy đọc kết quả đó và trình bày cho user, KHÔNG cần filter thêm
→ **KHÔNG BAO GIỜ** nói "mình không có dữ liệu" hay "bạn cần đính kèm lại file" nếu đã có EXCEL_REF trong lịch sử **và user đang hỏi về file đó**

**Ví dụ flow đúng:**
- Q1: user upload file → gọi `analyze_excel_file` → nhận kết quả + EXCEL_REF
- Q2: "cho tôi số POS bán thành công 3 ngày gần nhất" → gọi `query_excel_data(drive_id=..., item_id=..., question=...)` → filter dữ liệu → trả lời
- Q3-Q10: tiếp tục dùng `query_excel_data` với cùng drive_id/item_id

## Nhận diện intent phân tích file Excel (ngay lập tức)
Khi **TIN NHẮN HIỆN TẠI** của user chứa `[File Attachment: ...]` và user hỏi về phân tích / kiểm tra / xem dữ liệu:
→ **LUÔN gọi `analyze_excel_file`** (KHÔNG gọi `analyze_onedrive_file` — dù `content_url` có chứa sharepoint.com)
→ Gọi:
   ```
   analyze_excel_file(
       filename=...,        # từ [File Attachment: name=...]
       content_url=...,     # từ [File Attachment: content_url=...] — BẮT BUỘC
       user_aad_id=...,     # từ [File Attachment: user_aad_id=...] — BẮT BUỘC
       unique_id=...,       # từ [File Attachment: unique_id=...] — nếu có, LUÔN truyền vào
       analysis_request=... # mô tả yêu cầu của user
   )
   ```
→ Tool đọc file trực tiếp qua **Microsoft Graph Workbook API** — KHÔNG dùng `download_url`
→ Nếu user chưa liên kết tài khoản Microsoft, tool sẽ tự trả về link OAuth → hiển thị link cho user, KHÔNG gọi thêm tool nào
→ **KHÔNG GỌI LẠI tool** nếu tool trả về lỗi — báo lỗi trực tiếp cho user
→ Sau khi có kết quả, hãy diễn giải và tóm tắt những insight quan trọng nhất

**QUAN TRỌNG — phân biệt current message vs lịch sử:**
- Chỉ gọi `analyze_excel_file` khi `[File Attachment: ...]` xuất hiện trong **TIN NHẮN HIỆN TẠI** (message mới nhất của user, bắt đầu bằng nội dung trước `[Current Time: ...]`)
- Nếu `[File Attachment: ...]` **chỉ có trong lịch sử chat** (từ tin nhắn trước đó), KHÔNG gọi lại `analyze_excel_file` — file đã được phân tích, kết quả đang có trong lịch sử
- Câu hỏi follow-up về file đã phân tích (phân tích sâu hơn, so sánh, vẽ biểu đồ...) → dùng kết quả trong lịch sử để trả lời, KHÔNG gọi lại tool
- KHÔNG BAO GIỜ output raw JSON của file metadata (filename, download_url...) vào reply cho user

**QUAN TRỌNG — phân biệt 2 tool:**
- `analyze_excel_file`: dùng khi có `[File Attachment: ...]` trong **TIN NHẮN HIỆN TẠI** (file đính kèm từ Teams chat)
- `analyze_onedrive_file`: chỉ dùng khi user **tự gõ/paste** một URL SharePoint/OneDrive vào tin nhắn (KHÔNG phải từ file attachment)

## Nhận diện intent đặt lịch phân tích Excel định kỳ — GỬI TEAMS
Khi user muốn bot **tự động đọc file Excel và gửi báo cáo định kỳ vào Teams** ("mỗi ngày 8h", "hàng ngày lúc 9h sáng"):

**Flow 1 — User đính kèm file (có `[File Attachment: ...]` trong tin nhắn hiện tại):**
→ Gọi `schedule_excel_workbook(
      content_url=...,           # từ [File Attachment: content_url=...] — BẮT BUỘC
      user_aad_id=...,           # từ [File Attachment: user_aad_id=...]
      filename=...,              # từ [File Attachment: name=...]
      time_spec=...,             # thời gian từ yêu cầu user ("08:00", "9h sáng")
      conv_id=...,               # từ [Teams Context: conv_id=...]
      service_url=...,           # từ [Teams Context: service_url=...]
      analysis_request=...,      # mô tả yêu cầu phân tích
      n_highlights=3
   )`

**Flow 2 — User paste link OneDrive/SharePoint (URL chứa `sharepoint.com` hoặc `onedrive.com`):**
→ Gọi `schedule_excel_workbook(
      sharepoint_url=...,        # URL do user paste trực tiếp trong tin nhắn
      user_aad_id=...,           # từ [Teams Context: user_aad_id=...]
      filename=...,              # tên file từ URL hoặc user cung cấp
      time_spec=...,             # thời gian từ yêu cầu user
      conv_id=...,               # từ [Teams Context: conv_id=...]
      service_url=...,           # từ [Teams Context: service_url=...]
      analysis_request=...,      # mô tả yêu cầu phân tích
      n_highlights=3
   )`

→ **LUÔN dùng `schedule_excel_workbook` thay vì `schedule_excel_report` hoặc `schedule_onedrive_excel` cho các job mới**
→ Tool sẽ tự resolve file ID và lưu job — không cần download_url (không bao giờ expire)
→ Nếu tool trả về link xác thực OAuth → hiển thị link đó cho user, KHÔNG gọi thêm tool nào

**Phân biệt với `schedule_onedrive_excel` (deprecated — không dùng cho job mới):**
- `schedule_excel_workbook`: dùng Graph Workbook API, lưu drive_id/item_id — **ưu tiên**
- `schedule_onedrive_excel`: download file bytes mỗi lần chạy — chỉ dùng nếu có lý do đặc biệt

## Nhận diện intent đặt lịch phân tích Excel định kỳ (LEGACY — giữ để backward compat)
Khi user muốn bot tự động đọc file Excel và gửi báo cáo theo lịch ("mỗi ngày 9h", "hàng ngày") — **nếu không có content_url hoặc sharepoint_url, dùng flow cũ bên dưới**:
→ Gọi `schedule_excel_report(
      time_spec=...,               # thời gian từ yêu cầu user ("09:00", "9h sáng")
      conv_id=...,                 # từ [Teams Context: conv_id=...]
      service_url=...,             # từ [Teams Context: service_url=...]
      content_url=...,             # từ [File Attachment: content_url=...] — BẮT BUỘC dùng content_url cho schedule
      user_aad_id=...,             # từ [File Attachment: user_aad_id=...]
      unique_id=...,               # từ [File Attachment: unique_id=...]
      filename=...,                # từ [File Attachment: name=...]
      analysis_request=...,        # mô tả yêu cầu phân tích
      n_highlights=3
   )`
→ QUAN TRỌNG: Dùng `content_url` (KHÔNG dùng `download_url`) vì download_url là URL tạm, sẽ hết hạn sau ~1 giờ
→ `content_url` là URL SharePoint ổn định, bot sẽ dùng Microsoft Graph API để tải file mỗi lần chạy

## Nhận diện intent theo dõi OneDrive folder
Khi user gửi link SharePoint/OneDrive (chứa `sharepoint.com`) và muốn đặt lịch theo dõi:
→ Gọi `watch_onedrive_folder(
      sharepoint_url=...,          # URL SharePoint từ user
      schedule=...,                # thời gian từ yêu cầu user
      analysis_request=...,        # mô tả yêu cầu phân tích
      conv_id=...,                 # từ [Teams Context: conv_id=...]
      service_url=...,             # từ [Teams Context: service_url=...]
      user_aad_id=...,             # từ [Teams Context: user_aad_id=...]
   )`
→ Nếu tool trả về link xác thực OAuth → hiển thị link đó cho user, KHÔNG gọi thêm tool nào
→ Sau khi user xác thực và gửi lại yêu cầu → gọi lại tool để đặt lịch

## Nhận diện intent phân tích file Excel từ link OneDrive (on-demand)
Khi user gửi URL có chứa `sharepoint.com` hoặc `onedrive.com` trỏ tới file `.xlsx`/`.xls` và muốn phân tích ngay:
→ Gọi `analyze_onedrive_file(
      sharepoint_url=...,          # URL do user gửi
      user_aad_id=...,             # từ [Teams Context: user_aad_id=...]
      analysis_request=...,        # mô tả yêu cầu của user (VD: "phân tích xu hướng doanh thu")
   )`
→ Nếu tool trả về link xác thực OAuth → hiển thị link đó cho user, KHÔNG gọi thêm tool nào
→ Sau khi có kết quả cấu trúc Excel từ tool:
  - Tóm tắt các insight quan trọng nhất theo yêu cầu user
  - Đề xuất thêm 2-3 hướng phân tích user có thể quan tâm (VD: "Tôi cũng có thể: phân tích theo tuần, so sánh tháng, vẽ biểu đồ xu hướng...")
  - Hỏi user muốn đi sâu vào hướng nào

## Nhận diện intent đặt lịch phân tích file Excel OneDrive định kỳ
Khi user gửi link SharePoint/OneDrive trỏ tới file `.xlsx`/`.xls` VÀ muốn nhận báo cáo định kỳ ("mỗi sáng", "hàng ngày", "mỗi tuần"):
→ Gọi `schedule_onedrive_excel(
      sharepoint_url=...,          # URL do user gửi
      time_spec=...,               # thời gian từ yêu cầu user ("09:00", "9h sáng")
      conv_id=...,                 # từ [Teams Context: conv_id=...]
      service_url=...,             # từ [Teams Context: service_url=...]
      user_aad_id=...,             # từ [Teams Context: user_aad_id=...]
      analysis_request=...,        # mô tả yêu cầu phân tích định kỳ
      n_highlights=3
   )`
→ Nếu tool trả về link xác thực OAuth → hiển thị link đó cho user, KHÔNG gọi thêm tool nào
→ Phân biệt với `watch_onedrive_folder`: tool này dành cho **1 file cụ thể**, còn `watch_onedrive_folder` dành cho **toàn bộ folder**

## Nhận diện intent kiểm tra liên kết Microsoft — LUÔN GỌI TOOL

**QUY TẮC TUYỆT ĐỐI:**
- **LUÔN LUÔN gọi `check_microsoft_auth`** để kiểm tra trạng thái thực tế — dù lịch sử hội thoại có gì đi nữa
- **KHÔNG BAO GIỜ** tự suy luận "user đã/chưa liên kết" từ lịch sử hội thoại
- Lý do: User có thể đã click OAuth link (ngoài hội thoại này) và liên kết thành công — token được lưu độc lập trong cache, không phản ánh vào lịch sử chat
- **KHÔNG BAO GIỜ** tự tạo hay tự paste OAuth link — chỉ hiển thị link mà tool `check_microsoft_auth` trả về

Khi user hỏi "đã liên kết Microsoft chưa", "tôi đã xác thực chưa", "tài khoản Microsoft của tôi", "token còn hạn không", "đã kết nối Outlook chưa", "check auth", hoặc bất kỳ câu hỏi nào về trạng thái xác thực:
→ GỌI NGAY `check_microsoft_auth(user_aad_id=...)` — không được bỏ qua bước này
→ `user_aad_id` lấy từ **Teams Context**
→ Nếu tool trả về "✅ Tài khoản Microsoft đã được liên kết" → thông báo cho user rằng đã liên kết thành công
→ Nếu tool trả về link OAuth → hiển thị link đó, KHÔNG gọi thêm tool nào

## Nhận diện intent gửi email
Khi user nói "gửi email cho tôi", "gửi qua mail", "email tóm tắt cho tôi", kết hợp với bất kỳ nội dung nào (tin tức, báo cáo Excel, kết quả A/B test...):
→ Thực hiện task (đọc tin, phân tích file...) trước, lấy kết quả
→ Gọi `send_email_report(subject=..., body=<nội dung>, user_aad_id=...)` — **KHÔNG hỏi email**
→ Tool tự động lấy email của user từ cache (sau khi user đã authorize OneDrive/Mail)
→ Chỉ hỏi email nếu tool trả về lỗi "Không thể xác định địa chỉ email"
→ Nếu user chưa authorize → tool trả về link OAuth → hiển thị link đó cho user

## Giờ hiện tại
Mỗi message chứa `[Current Time: DD/MM/YYYY HH:MM (ICT)]` — đây là giờ Việt Nam **chính xác** lúc user gửi tin.
- Khi user hỏi "bây giờ là mấy giờ?", "hôm nay là ngày mấy?" → đọc từ `[Current Time: ...]`, KHÔNG tự đoán
- Khi đặt lịch → dùng giờ này làm tham chiếu để xác nhận thời gian lịch hẹn với user

## Teams Context
Mỗi message chứa `[Teams Context: ...]` ở cuối — extract:
- `service_url`: URL Teams (dùng cho tools gửi/schedule)
- `conv_id`: ID cuộc trò chuyện (dùng làm thread_id cho memory và cho tools)
- `activity_id`: ID activity (dùng làm reply_to_id nếu muốn reply trong thread)
- `user_aad_id`: Azure AD object ID của user (dùng cho `watch_onedrive_folder` và `list_onedrive_watches`)

Nếu message có `[File Attachment: ...]`, extract:
- `name`: tên file
- `file_type`: loại file (xlsx, xls, ...)
- `content_url`: SharePoint URL ổn định (dùng cho Graph API)
- `user_aad_id`: Azure AD object ID của file owner (BẮT BUỘC cho Graph API)

## Nhận diện intent A/B Test — hướng dẫn chuẩn bị dữ liệu
Khi user hỏi "cần dữ liệu gì", "chuẩn bị file thế nào", "format Excel A/B test ra sao", hoặc muốn bắt đầu thử nghiệm tính năng mới:
→ Gọi `get_ab_test_guide()` và trả kết quả cho user
→ KHÔNG tự soạn hướng dẫn — luôn gọi tool để đảm bảo nội dung nhất quán

## Nhận diện intent phân tích A/B Test — phân tích file
Khi message chứa `[File Attachment: ...]` và user đề cập đến A/B test, thử nghiệm, experiment, variant, uplift, kiểm định thống kê:
→ Gọi `analyze_ab_test(...)` với params **BẮT BUỘC** lấy từ `[File Attachment: ...]` trong **TIN NHẮN HIỆN TẠI**:
   ```
   analyze_ab_test(
       filename=...,       # từ [File Attachment: name=...] — TIN NHẮN HIỆN TẠI
       content_url=...,    # từ [File Attachment: content_url=...] — TIN NHẮN HIỆN TẠI
       user_aad_id=...,    # từ [File Attachment: user_aad_id=...] — TIN NHẮN HIỆN TẠI
       unique_id=...,      # từ [File Attachment: unique_id=...] — TIN NHẮN HIỆN TẠI
       download_url=...,   # từ [File Attachment: download_url=...] — TIN NHẮN HIỆN TẠI (fallback)
       control_variant=..., # để trống nếu không rõ (auto-detect)
   )
   ```
→ **TUYỆT ĐỐI KHÔNG** dùng `download_url` hoặc bất kỳ param nào từ lịch sử chat — `download_url` cũ đã HẾT HẠN sau 1 giờ
→ Để trống `control_variant` nếu không rõ (auto-detect)
→ Sau khi có kết quả thống kê, diễn giải kết quả bằng tiếng Việt và **hỏi user**: "Bạn muốn tôi gửi báo cáo này qua đâu — **Teams** hay **email**?"
→ Nếu user chọn **Teams**: gọi `send_teams_message(conv_id=..., service_url=..., message=<report>)`
→ Nếu user chọn **email**: gọi `send_email_report(subject=..., body=<report>, user_aad_id=...)` — **KHÔNG hỏi email**
  - Tool sẽ tự động lấy email của user từ Microsoft Graph (đã cache sau OAuth)
  - Chỉ hỏi email nếu tool trả về lỗi "Không thể xác định địa chỉ email"
  - Nếu user muốn gửi cho người khác: truyền `to_email=<email người nhận>`
→ `user_aad_id` lấy từ `[Teams Context: user_aad_id=...]`

## Nhận diện intent vẽ biểu đồ
Khi user nói "vẽ biểu đồ", "chart", "visualize", "hiển thị dữ liệu", "plot", "biểu đồ cột/đường/tròn/...":
→ Xác định `chart_type` từ intent:
  - "so sánh", "compare", "cột" → "bar"
  - "cột chồng", "stacked bar" → "area" (dùng area cho stacked)
  - "xu hướng", "trend", "theo thời gian", "tháng", "ngày" → "line"
  - "vùng", "area", "stacked" → "area"
  - "tỷ lệ", "phần trăm", "chiếm", "pie" → "pie"
  - "donut" → "donut"
  - "phân phối", "distribution", "histogram" → "histogram"
  - "tương quan", "scatter", "correlation" → "scatter"
  - "heatmap", "ma trận" → "heatmap"
  - "funnel", "phễu", "phễu marketing", "phễu chuyển đổi", "conversion funnel", "vẽ phễu", "phễu sản phẩm" → "funnel"
  - không rõ → "auto"
→ Convert dữ liệu của user sang đúng JSON format của tool
→ Gọi `visualize_data(data_json=..., chart_type=..., title=..., conv_id=..., service_url=...)`
→ `conv_id` và `service_url` lấy từ `[Teams Context: ...]`

**Vẽ biểu đồ sau khi đã phân tích Excel (quan trọng):**
→ Nếu kết quả phân tích Excel đang có trong lịch sử chat → **KHÔNG gọi lại `analyze_excel_file`**
→ Trích xuất số liệu trực tiếp từ kết quả phân tích trong lịch sử (từ bảng thống kê, preview rows, v.v.)
→ Convert sang JSON format phù hợp rồi gọi `visualize_data`
→ Ví dụ nếu user muốn "biểu đồ cột chồng theo tỉnh thành và ngày": trích xuất dữ liệu từ bảng "5 hàng đầu tiên" hoặc thống kê mô tả trong lịch sử, nhóm theo province và date, rồi tạo `data_json`
→ Nếu dữ liệu quá ít trong lịch sử (chỉ có 5 hàng preview), hãy nói rõ với user rằng biểu đồ chỉ dựa trên sample data và kết quả có thể không đầy đủ
→ KHÔNG BAO GIỜ truyền `{"filename": ..., "download_url": ...}` vào `data_json` — `data_json` phải là số liệu thực (nhãn + giá trị), không phải file metadata

→ Có thể vẽ biểu đồ multi-series: `[{"label":"Jan","doanh_thu":100,"chi_phi":80},...]`
→ palette "default" cho hầu hết trường hợp; "warm" cho cảnh báo/risk; "blue"/"green" theo brand

**Vẽ funnel chuyển đổi (quan trọng):**
Khi user nói "vẽ funnel", "phễu marketing", "phễu chuyển đổi", "conversion funnel", "phễu sản phẩm", "vẽ phễu với dữ liệu đó":
→ chart_type = "funnel"
→ Data format: `[{"label": "TÊN_BƯỚC", "value": SỐ_LƯỢNG}, ...]` — các bước giảm dần từ trên xuống
→ Nếu dữ liệu có trong lịch sử Excel/chat: trích xuất stages và values từ đó, không cần hỏi lại
→ Nếu chưa có dữ liệu: hỏi user tên các bước và số liệu tương ứng
→ Biểu đồ tự động tính % drop-off và % conversion rate giữa các bước

## Nhận diện intent xem danh sách lịch tự động
Khi user hỏi về các lịch đang chạy bằng bất kỳ từ khóa nào:
"job tự động", "lịch tự động", "danh sách lịch", "danh sách job",
"xem lịch", "các lịch đang chạy", "có lịch gì không", "đặt lịch gì rồi",
"schedule của tôi", "show jobs", "list schedule", "lịch đang hoạt động",
"công việc tự động", "tác vụ tự động", "bot đang làm gì tự động":
→ GỌI NGAY `list_schedules()` — không cần tham số
→ KHÔNG tự đoán hay tự trả lời "không có job nào" — luôn gọi tool để lấy kết quả thực tế

## Quy tắc tuyệt đối — quản lý lịch (CRITICAL — ƯU TIÊN CAO NHẤT)
- **KHÔNG BAO GIỜ** gọi `cancel_schedule` trừ khi user **nói rõ ràng** muốn hủy/xóa lịch (VD: "hủy lịch", "xóa lịch", "bỏ lịch", "cancel schedule", "dừng lịch")
- **KHÔNG BAO GIỜ** gọi `list_schedules` khi user chỉ yêu cầu phân tích file, đọc tin tức, hoặc bất kỳ task nào không liên quan đến lịch
- Phân tích file Excel, A/B test, tin tức... **KHÔNG** liên quan đến schedule management — đừng gọi các tool schedule trong khi xử lý các task đó
- Vi phạm rule này sẽ làm mất lịch quan trọng của user

## Hiển thị danh sách lịch (list_schedules)
Khi tool `list_schedules` trả về kết quả:
→ **Sao chép nguyên văn** bảng markdown từ tool output vào reply, KHÔNG tự format lại
→ KHÔNG thêm cột mới (ví dụ: cột UTC, cột ICT+7)
→ KHÔNG đổi Job ID sang dạng đầy đủ — chỉ dùng 6-char short ID từ tool
→ KHÔNG convert hay diễn giải lại thời gian — dùng nguyên giá trị từ tool output
→ Được phép thêm 1-2 dòng text giới thiệu/ghi chú ngắn ở đầu hoặc cuối, nhưng bảng phải giữ nguyên

## Nguyên tắc trả lời
- Trả lời bằng **tiếng Việt**, ngắn gọn, dưới 5000 ký tự
- Dùng emoji và markdown
- Xác nhận rõ ràng khi đặt lịch thành công (thời gian + nội dung)
- Khi phân tích Excel: trình bày insight ngắn gọn, không paste toàn bộ bảng số

## TUYỆT ĐỐI KHÔNG output raw JSON cho user
- **KHÔNG BAO GIỜ** trả lời user bằng chuỗi JSON thô như `{"subject":"...","body":"..."}` hay `{"filename":"...","download_url":"..."}`
- Nếu cần gọi tool → **gọi tool qua function call**, KHÔNG output tham số của tool dưới dạng text
- Câu trả lời cuối cùng cho user phải là **text/markdown tự nhiên**, không bao giờ là JSON object"""


class TeamsAgent:
    """LangChain ReAct agent with conversation memory for Teams News delivery."""

    # Auto-compact thresholds
    _COMPACT_TOOL_THRESHOLD = 2_000     # chars — Excel ToolMessage > này sẽ bị compact (Phase 1)
    _COMPACT_TRIGGER_CHARS  = 60_000    # chars — tổng history > này trigger Phase 2 background
    _COMPACT_KEEP_MSGS      = 20        # số message cuối KHÔNG tóm tắt (≈4 exchanges gần nhất)
    _EXCEL_COMPACT_TOOLS    = frozenset({"query_excel_data", "analyze_excel_file"})

    # History trim thresholds — used by both pre-invoke and post-invoke trim.
    # 60k chars ≈ 15k tokens. With ~7.5k tokens system prompt + 3k safety buffer,
    # total stays well within a 32k context window.
    _HISTORY_CHAR_BUDGET = 60_000
    _MIN_HISTORY_KEEP    = 4

    def __init__(self) -> None:
        self.llm = get_llm_model()
        self.checkpointer = get_checkpointer()
        self._compacting: set[str] = set()  # thread_ids currently being compacted
        set_shared_llm(self.llm)  # share LLM instance with pandas dataframe agent in tools
        memory_tools = get_memory_tools()
        all_tools = TOOLS + memory_tools

        self.agent = create_agent(
            self.llm,
            all_tools,
            system_prompt=SYSTEM_PROMPT,
            checkpointer=self.checkpointer,
        )

        logger.info(
            "TeamsAgent initialized — tools=%d, memory_tools=%d, checkpointer=%s",
            len(all_tools), len(memory_tools), type(self.checkpointer).__name__,
        )

    # ------------------------------------------------------------------
    # Auto-compact helpers
    # ------------------------------------------------------------------

    def _compact_excel_toolmessages(self, all_msgs: list, config: dict) -> int:
        """Phase 1: Replace large Excel ToolMessage content with compact stubs.

        No LLM call — runs synchronously, zero latency impact.
        Targets old ToolMessages (excluding last 4) from Excel tools with content > threshold.
        Returns count of messages compacted.
        """
        if len(all_msgs) <= 4:
            return 0

        ops: list = []
        for msg in all_msgs[:-4]:  # skip current exchange
            if not isinstance(msg, ToolMessage):
                continue
            tool_name = getattr(msg, "name", "") or ""
            if tool_name not in self._EXCEL_COMPACT_TOOLS:
                continue
            content = str(getattr(msg, "content", ""))
            if len(content) <= self._COMPACT_TOOL_THRESHOLD:
                continue

            # Extract metadata for compact stub
            rows_match = re.search(r"(\d[\d,]*)\s*rows?", content, re.IGNORECASE)
            rows_info = rows_match.group(0) if rows_match else "data"
            fname_match = re.search(r"`([^`]+\.xlsx?)`", content, re.IGNORECASE)
            filename = fname_match.group(1) if fname_match else "Excel file"

            # Replace content in-place: same id → LangGraph add_messages reducer updates it
            ops.append(ToolMessage(
                content=(
                    f"[COMPACTED: Excel — `{filename}`, {rows_info} analyzed. "
                    f"Chi tiết đã được xử lý ở turn trước.]"
                ),
                tool_call_id=getattr(msg, "tool_call_id", ""),
                name=tool_name,
                id=msg.id,
            ))

        if ops:
            try:
                self.agent.update_state(config, {"messages": ops})
                logger.info("[COMPACT_P1] compacted %d excel toolmessages", len(ops))
            except Exception as e:
                logger.debug("[COMPACT_P1] skipped (non-critical): %s", e)
                return 0

        return len(ops)

    async def _compact_in_background(self, msgs: list, config: dict, thread_id: str) -> None:
        """Phase 2: Summarize old conversation exchanges using LLM.

        Runs in background after response is already sent to user — no latency impact.
        Uses LLM to produce a short Vietnamese summary of old exchanges, replacing them
        with a single SystemMessage stub to free up context space.
        """
        self._compacting.add(thread_id)
        try:
            non_sys = [m for m in msgs if not isinstance(m, SystemMessage)]
            if len(non_sys) <= self._COMPACT_KEEP_MSGS:
                return

            to_summarize = non_sys[:-self._COMPACT_KEEP_MSGS]
            total_chars = sum(len(str(getattr(m, "content", ""))) for m in non_sys)

            # Build conversation text (truncate each message to avoid recursive overflow)
            lines = []
            for m in to_summarize:
                msg_type = type(m).__name__.replace("Message", "")
                preview = str(getattr(m, "content", ""))[:300]
                lines.append(f"[{msg_type}]: {preview}")
            conversation_text = "\n".join(lines)

            _prompt = (
                "Tóm tắt 3-5 câu tiếng Việt lịch sử hội thoại dưới đây. "
                "Chỉ giữ lại: user đã hỏi gì, kết quả chính là gì, "
                "file hoặc lịch tự động nào đã được tạo. "
                "Bỏ qua nội dung kỹ thuật, dữ liệu CSV thô, và thông tin thừa."
            )

            summary_resp = await asyncio.to_thread(
                self.llm.invoke,
                [SystemMessage(content=_prompt), HumanMessage(content=conversation_text)],
            )
            summary = str(getattr(summary_resp, "content", "")).strip()
            if not summary:
                return

            n = len(to_summarize)
            remove_ops = [RemoveMessage(id=m.id) for m in to_summarize if getattr(m, "id", None)]
            summary_msg = SystemMessage(content=f"[TÓM TẮT — {n} turns trước]: {summary}")

            await asyncio.to_thread(
                self.agent.update_state, config, {"messages": remove_ops + [summary_msg]}
            )
            logger.info(
                "[COMPACT_P2] done thread=%s removed=%d chars_before=%d",
                thread_id, n, total_chars,
            )

        except Exception as e:
            logger.warning("[COMPACT_P2] failed for thread=%s: %s", thread_id, e)
        finally:
            self._compacting.discard(thread_id)

    def _pre_trim_history(self, config: dict) -> None:
        """Trim accumulated history in the checkpointer BEFORE agent.invoke().

        Post-invoke trim only runs when invoke() succeeds — if the LLM call fails due to
        context overflow, trim never executes and every subsequent message also fails.
        This method proactively reads the current state and removes oldest messages so
        the prompt fits within _HISTORY_CHAR_BUDGET before the LLM call is made.
        """
        try:
            state = self.agent.get_state(config)
            current_msgs = list(state.values.get("messages", []))
        except Exception as e:
            logger.debug("[PRE_TRIM] get_state failed (non-critical): %s", e)
            return

        non_sys = [m for m in current_msgs if not isinstance(m, SystemMessage)]
        total_chars = sum(len(str(getattr(m, "content", ""))) for m in non_sys)

        if total_chars <= self._HISTORY_CHAR_BUDGET:
            return

        logger.info(
            "[PRE_TRIM] history %d chars > %d budget — trimming before invoke",
            total_chars, self._HISTORY_CHAR_BUDGET,
        )

        keep_msgs = list(non_sys[-self._MIN_HISTORY_KEEP:])
        kept_chars = sum(len(str(getattr(m, "content", ""))) for m in keep_msgs)

        for m in reversed(non_sys[:-self._MIN_HISTORY_KEEP]):
            msg_chars = len(str(getattr(m, "content", "")))
            if kept_chars + msg_chars <= self._HISTORY_CHAR_BUDGET:
                keep_msgs.insert(0, m)
                kept_chars += msg_chars
            else:
                break

        to_remove = [m for m in non_sys if m not in keep_msgs]
        remove_ops = [RemoveMessage(id=m.id) for m in to_remove if getattr(m, "id", None)]
        if remove_ops:
            try:
                self.agent.update_state(config, {"messages": remove_ops})
                logger.info(
                    "[PRE_TRIM] done: removed=%d kept=%d chars_after=%d",
                    len(remove_ops), len(keep_msgs), kept_chars,
                )
            except Exception as e:
                logger.warning("[PRE_TRIM] update_state failed (non-critical): %s", e)

    async def _run_manual_compact(self, config: dict, thread_id: str) -> str:
        """Handle /compact command: summarize full history then reload long-term memories.

        Steps:
          1. Summarize all conversation turns into 5-7 Vietnamese sentences via LLM.
          2. Replace entire history with a single SystemMessage summary.
          3. If GreenNode Memory is configured, run smart recall using the summary
             as query and inject matching long-term memories as an additional SystemMessage.
          4. Return a confirmation card showing the summary + recalled memories.

        The /compact message itself is never added to conversation history.
        """
        # 1. Read current state
        try:
            state = self.agent.get_state(config)
            msgs = list(state.values.get("messages", []))
        except Exception as e:
            return f"❌ Không thể đọc lịch sử hội thoại: {e}"

        non_sys = [m for m in msgs if not isinstance(m, SystemMessage)]
        if len(non_sys) <= 2:
            return "💬 Lịch sử quá ngắn, không cần compact."

        n = len(non_sys)
        total_chars = sum(len(str(getattr(m, "content", ""))) for m in non_sys)

        # 2. Build conversation text for summarization (cap each message to avoid recursive overflow)
        lines = []
        for m in non_sys:
            msg_type = type(m).__name__.replace("Message", "")
            preview = str(getattr(m, "content", ""))[:500]
            lines.append(f"[{msg_type}]: {preview}")
        conversation_text = "\n".join(lines)

        _prompt = (
            "Tóm tắt 5-7 câu tiếng Việt toàn bộ lịch sử hội thoại dưới đây. "
            "Chỉ giữ lại: user đã hỏi gì, kết quả chính là gì, "
            "file hoặc lịch tự động nào đã được tạo. "
            "Bỏ qua nội dung kỹ thuật, dữ liệu CSV thô, và thông tin thừa."
        )

        summary = ""
        try:
            resp = await asyncio.to_thread(
                self.llm.invoke,
                [SystemMessage(content=_prompt), HumanMessage(content=conversation_text)],
            )
            summary = str(getattr(resp, "content", "")).strip()
        except Exception as e:
            logger.warning("[COMPACT_CMD] LLM summarize failed: %s", e)

        # 3. Build new state: remove all + add summary SystemMessage
        remove_ops = [RemoveMessage(id=m.id) for m in non_sys if getattr(m, "id", None)]
        new_msgs: list = []
        if summary:
            new_msgs.append(SystemMessage(content=f"[TÓM TẮT LỊCH SỬ ({n} turns)]: {summary}"))

        # 4. Smart recall: search long-term memories using summary as query context
        recalled_lines: list[str] = []
        if MEMORY_ID and summary:
            try:
                from greennode_agentbase.memory import MemoryClient
                from greennode_agentbase.memory.models import MemoryRecordSearchRequest

                actor_id = config.get("configurable", {}).get("actor_id", "default")
                ns = f"/strategies/{MEMORY_STRATEGY_ID}/actors/{actor_id}"
                _mclient = MemoryClient()
                results = await asyncio.to_thread(
                    _mclient.search_memory_records,
                    id=MEMORY_ID,
                    namespace=ns,
                    request=MemoryRecordSearchRequest(query=summary[:300], limit=5),
                )
                recalled_lines = [
                    f"- {r.memory}"
                    for r in (results or [])
                    if getattr(r, "memory", None)
                ]
                if recalled_lines:
                    new_msgs.append(SystemMessage(
                        content="[LONG-TERM MEMORIES về user]:\n" + "\n".join(recalled_lines)
                    ))
                    logger.info("[COMPACT_CMD] recalled %d memories actor=%s", len(recalled_lines), actor_id)
            except Exception as e:
                logger.warning("[COMPACT_CMD] recall failed (non-critical): %s", e)

        # 5. Apply changes to checkpointer
        try:
            await asyncio.to_thread(
                self.agent.update_state, config, {"messages": remove_ops + new_msgs}
            )
        except Exception as e:
            return f"❌ Không thể cập nhật lịch sử: {e}"

        logger.info("[COMPACT_CMD] done thread=%s n=%d chars=%d recalled=%d",
                    thread_id, n, total_chars, len(recalled_lines))

        # 6. Build confirmation response
        freed_kb = total_chars // 1000
        out = [f"✅ **Đã compact lịch sử hội thoại!**", ""]
        out.append(f"📊 **{n} turns → 1 summary** (giải phóng ~{freed_kb}k chars)")

        if summary:
            out += ["", "📝 **Tóm tắt:**", summary]

        if recalled_lines:
            out += ["", "🧠 **Long-term memories đã nạp lại:**"]
            out += recalled_lines
        elif MEMORY_ID:
            out.append("_💡 Không tìm thấy long-term memories liên quan._")

        out += ["", "_Bộ nhớ đã dọn dẹp. Cuộc trò chuyện tiếp theo sẽ nhẹ hơn._"]
        return "\n".join(out)

    async def handle_message(
        self,
        text: str,
        service_url: str = "",
        conv_id: str = "",
        activity_id: str = "",
        user_id: str = "default",
        user_aad_id: str = "",
        file_attachments: list | None = None,
    ) -> str:
        if not text or not text.strip():
            # If only a file was sent with no text, still proceed
            if not file_attachments:
                return "Tôi nhận được tin nhắn trống. Hãy gửi câu hỏi hoặc lệnh nhé! 😊"
            text = "Hãy phân tích file đính kèm giúp tôi."

        # Inject Teams context so LLM can pass correct values to tools
        now_vn = datetime.datetime.now(_VN_TZ).strftime("%d/%m/%Y %H:%M (ICT)")
        ctx = (
            f"\n[Current Time: {now_vn}]"
            f"\n[Teams Context: service_url={service_url}, "
            f"conv_id={conv_id}, activity_id={activity_id}, user_aad_id={user_aad_id}]"
        )

        # Inject file attachment info so LLM knows to call analyze_excel_file / schedule_excel_report
        if file_attachments:
            for fa in file_attachments:
                ctx += (
                    f"\n[File Attachment: name={fa['name']}, "
                    f"file_type={fa['file_type']}, "
                    f"content_url={fa.get('content_url', '')}, "
                    f"unique_id={fa.get('unique_id', '')}, "
                    f"user_aad_id={fa.get('user_aad_id', '')}, "
                    f"download_url={fa.get('download_url', '')}]"
                )

        full_message = text.strip() + ctx

        # Each user gets isolated history: thread_id = "{aad_id}:{conv_id}"
        # actor_id uses aad_id (stable Azure AD object ID), falls back to Teams user_id
        _actor = user_aad_id or user_id or "default"
        _thread = f"{_actor}:{conv_id}" if (user_aad_id and conv_id) else conv_id or "default"
        config = {
            "configurable": {
                "thread_id": _thread,
                "actor_id": _actor,
            }
        }

        # /compact command: summarize history + reload long-term memories, skip agent invoke
        if text.strip().lower().startswith("/compact"):
            return await self._run_manual_compact(config, _thread)

        messages = [HumanMessage(content=full_message)]

        def _invoke() -> tuple[str, list, bool]:
            """Run agent and return (response, all_msgs, should_compact_background)."""
            logger.info(
                "[INTENT] thread=%s actor=%s msg_preview=%.120s",
                _thread, _actor, full_message.replace("\n", " "),
            )

            # Pre-trim: ensure history is within budget BEFORE the LLM call.
            # If invoke() throws an overflow error, post-invoke trim never runs —
            # so every subsequent message also fails. This breaks that loop.
            self._pre_trim_history(config)

            result = self.agent.invoke({"messages": messages}, config=config)
            all_msgs = result["messages"]

            # Log each tool call and result for intent tracing
            for msg in all_msgs:
                msg_type = type(msg).__name__
                if msg_type == "AIMessage":
                    tool_calls = getattr(msg, "tool_calls", []) or []
                    for tc in tool_calls:
                        args_preview = str(tc.get("args", {}))[:200]
                        logger.info(
                            "[TOOL_CALL] tool=%s args=%.200s",
                            tc.get("name", "?"), args_preview,
                        )
                elif msg_type == "ToolMessage":
                    result_preview = str(getattr(msg, "content", ""))[:200]
                    logger.info(
                        "[TOOL_RESULT] tool=%s result=%.200s",
                        getattr(msg, "name", "?"), result_preview,
                    )

            content = all_msgs[-1].content
            logger.info("[FINAL_RESPONSE] preview=%.200s", str(content).replace("\n", " "))

            # Phase 1: compact large Excel ToolMessages in-place (sync, no LLM call)
            self._compact_excel_toolmessages(all_msgs, config)

            # Post-invoke trim — ensure next turn starts within budget.
            # Pre-trim above handles overflow prevention; this keeps history clean after each turn.
            non_sys = [m for m in all_msgs if not isinstance(m, SystemMessage)]
            if len(non_sys) > self._MIN_HISTORY_KEEP:
                keep_msgs = list(non_sys[-self._MIN_HISTORY_KEEP:])
                kept_chars = sum(len(str(getattr(m, "content", ""))) for m in keep_msgs)

                for m in reversed(non_sys[:-self._MIN_HISTORY_KEEP]):
                    msg_chars = len(str(getattr(m, "content", "")))
                    if kept_chars + msg_chars <= self._HISTORY_CHAR_BUDGET:
                        keep_msgs.insert(0, m)
                        kept_chars += msg_chars
                    else:
                        break

                to_remove = [m for m in non_sys if m not in keep_msgs]
                if to_remove:
                    remove_ops = [RemoveMessage(id=m.id) for m in to_remove if getattr(m, "id", None)]
                    if remove_ops:
                        try:
                            self.agent.update_state(config, {"messages": remove_ops})
                            logger.debug(
                                "History trimmed: removed=%d kept=%d chars=~%d",
                                len(remove_ops), len(keep_msgs), kept_chars,
                            )
                        except Exception as trim_err:
                            logger.debug("History trim skipped (non-critical): %s", trim_err)

            # Phase 2 trigger check: should background summarization run?
            total_chars = sum(len(str(getattr(m, "content", ""))) for m in non_sys)
            should_compact = (
                total_chars > self._COMPACT_TRIGGER_CHARS
                and _thread not in self._compacting
            )

            return content if content and content.strip() else "✅ Đã xử lý xong.", all_msgs, should_compact

        try:
            content, all_msgs, should_compact = await asyncio.to_thread(_invoke)
            # Phase 2: fire-and-forget background summarization (after response is ready)
            if should_compact:
                asyncio.create_task(self._compact_in_background(all_msgs, config, _thread))
                logger.info("[COMPACT_P2] background task scheduled for thread=%s", _thread)
            return content
        except Exception as e:
            logger.error("Agent error: %s", e, exc_info=True)
            err = str(e)
            if "max_tokens" in err and "got -" in err:
                # Context overflow despite pre-trim (e.g. current message itself is huge).
                # Aggressively purge all but the last 2 messages so the next turn can succeed.
                try:
                    state = self.agent.get_state(config)
                    msgs = list(state.values.get("messages", []))
                    non_sys_err = [m for m in msgs if not isinstance(m, SystemMessage)]
                    to_purge = non_sys_err[:-2]
                    if to_purge:
                        purge_ops = [RemoveMessage(id=m.id) for m in to_purge if getattr(m, "id", None)]
                        if purge_ops:
                            await asyncio.to_thread(
                                self.agent.update_state, config, {"messages": purge_ops}
                            )
                            logger.info("[OVERFLOW_RECOVERY] purged %d messages for thread=%s", len(purge_ops), _thread)
                except Exception as purge_err:
                    logger.warning("[OVERFLOW_RECOVERY] purge failed: %s", purge_err)
                return "⚠️ Lịch sử hội thoại quá dài, tôi đã tự động dọn dẹp bộ nhớ. Bạn hãy thử lại câu hỏi nhé!"
            if "401" in err or "authentication" in err.lower() or "api_key" in err.lower():
                return "❌ Lỗi xác thực LLM. Kiểm tra LLM_API_KEY và LLM_BASE_URL."
            return f"❌ Đã xảy ra lỗi: {err[:200]}"
