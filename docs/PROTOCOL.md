# LocalTools — Communication Protocol v1.0

این سند، قرارداد رسمی ارتباط بین سه کامپوننت است:

- **Hub Server** — سرور مرکزی روی شبکه محلی (`server/server.py`)
- **Agent** — سرویسِ نصب‌شده روی هر کلاینت ویندوز/لینوکس (`agent/agent.py`)
- **Web Console** — کنسول مدیریتی که در مرورگر باز می‌شود (`webconsole/`)

## 1. Transport

- همه چیز از یک پورت عبور می‌کند (پیش‌فرض `8788`).
- HTTP: صفحه لاگین + کنسول وب (فایل‌های استاتیک) + `/health`.
- WebSocket: `GET /ws` — پارامترهای کوئری:
  - `role=agent` یا `role=console` (الزامی)
  - `code=<access_code>` (فقط ایجنت؛ اگر سرور با `--code` اجرا شده باشد)
  - احراز هویت کنسول وب از طریق کوکی سشن (`lt_session`) که بعد از لاگین HTTP ست می‌شود.
- پیام‌های متنی: JSON با کدگذاری UTF-8.
- پیام‌های باینری (بخش 3).

## 2. Binary Framing

قالب هر پیام باینری:

```
[1 byte  type][4 bytes stream_id (big-endian)][4 bytes meta_len (big-endian)][meta JSON utf8][payload]
```

| type | نام | payload | meta |
|---|---|---|---|
| `0x01` | SCREEN_FRAME | JPEG کامل یک فریم | `{"w":w,"h":h,"cx":cx,"cy":cy}` — ابعاد پیکسلی تصویر خام + موقعیت نشانگر ماوس روی ماشین مبدا (اگر ناموجود: -1) |
| `0x02` | FILE_CHUNK | بایت‌های فایل | `{"transfer_id":N,"seq":N,"last":bool}` |

`stream_id=0` برای FILE_CHUNK کافی است (transfer_id در meta است).

## 3. Presence و ثبت‌نام

### Agent → Server: `register`
```json
{"type":"register","client_id":"a1b2c3d4e5f6","info":{
  "hostname":"PC-01","os":"Windows","os_version":"10 22H2","user":"ali",
  "ip":"192.168.1.35","version":"1.0.0",
  "capabilities":{"capture":true,"input":true,"gui":true},
  "monitors":2}}
```
- `client_id` یک UUID پایدار است که ایجنت در فایل کانفیگ خود ذخیره می‌کند.
- اتصال تکراری با همان `client_id` اتصال قبلی را جایگزین می‌کند (reconnect).
- سرور بعد از هر تغییر وضعیت، snapshot جدید `clients` را به همه کنسول‌ها می‌فرستد.

### Server → Console: `clients`
```json
{"type":"clients","clients":[{"id":"...","online":true,"last_seen":1730000000,
  "info":{...}}]}
```

### Agent → Server: `heartbeat` (هر 10s) → Server پاسخ `pong` می‌دهد.
کلاینت بعد از ~30s بدون heartbeat به صورت offline علامت می‌خورد (رکورد حفظ می‌شود، فقط خاکستری).

## 4. چرخه درخواست/تایید (Approval Flow)

هر اکشن کنسول روی کلاینت باید توسط ایجنتِ همان کلاینت تایید شود:

1. Console → Server: پیام اکشن (بخش 5)
2. Server: `req_id` می‌سازد → به ایجنتِ هدف: `approval_request` می‌فرستد (پاپ‌آپ tkinter)
3. Agent → Server: `{"type":"approve","req_id":N,"ok":true/false,"detail":{...}}`
4. Server → Console: `request_result` با همان `req_id` و `action`
5. Timeout سمت سرور: 60 ثانیه → پاسخ `ok:false, reason:"timeout"` + `request_cancelled` به ایجنت (بستن پاپ‌آپ)

## 5. JSON Messages

### Console → Server

| type | payload | توضیح |
|---|---|---|
| `list_clients` | — | درخواست snapshot لیست |
| `send_files` | `{"target":id,"files":[{"name":..,"size":..}]}` | پیشنهاد ارسال فایل(ها) |
| `transfer_ready` | `{"transfer_id":N}` | کنسول آماده ارسال چانک‌هاست (بعد از تایید) |
| `request_files` | `{"target":id}` | درخواست دریافت فایل از کلاینت |
| `request_screen` | `{"target":id,"quality":"low\|medium\|high"}` | درخواست مشاهده صفحه کلاینت |
| `set_quality` | `{"stream_id":N,"quality":...}` | تغییر کیفیت حین تماشا |
| `request_control` | `{"target":id,"stream_id":N}` | درخواست کنترل موس/کیبورد (مرحله 2) |
| `release_control` | `{"stream_id":N}` | پس دادن کنترل |
| `stop_view` | `{"stream_id":N}` | پایان تماشا |
| `share_screen_to` | `{"target":id}` | نمایش صفحه خودِ کنسول به کلاینت |
| `stop_share_to` | `{"stream_id":N}` | پایان اشتراک صفحه کنسول |
| `input` | `{"stream_id":N,"ev":{...}}` | رخداد موس/کیبورد (فقط وقتی کنترل فعال است) |

اشکال `ev`:
- `{"kind":"mouse_move","x":..,"y":..}` — مختصات در دستگاه تصویر خام (meta فریم)
- `{"kind":"mouse_btn","x":..,"y":..,"btn":"left|right|middle","pressed":true|false}`
- `{"kind":"wheel","x":..,"y":..,"dy":..}` — مثبت = اسکرول پایین
- `{"kind":"key","key":"a|Enter|Shift|ArrowLeft|...","pressed":true|false,"ctrl":b,"alt":b,"shift":b,"meta":b}`

### Agent → Server

| type | payload | توضیح |
|---|---|---|
| `register` / `heartbeat` | بالا | — |
| `approve` | `{"req_id":N,"ok":bool,"detail":{}}` | برای `request_files`: `detail.picked_files=[{"name","size"}]` |
| `transfer_done` | `{"transfer_id":N}` | فایل کامل شد |
| `transfer_failed` | `{"transfer_id":N,"reason":"..."}` | خطا |
| `screen_status` | `{"stream_id":N,"active":bool,"reason":"..."}` | شروع/توقف کپچر |
| `stop_share_view` | `{"stream_id":N}` | پنجره نمایش در کلاینت بسته شد → سرور به کنسول `share_ended` |
| `console_share_ack` | `{"stream_id":N,"ok":bool,"reason":"..."}` | پنجره نمایش باز شد/خطا |

### Server → Console

| type | payload |
|---|---|
| `clients` | snapshot لیست |
| `request_result` | `{"req_id":N,"action":"send_files\|request_files\|request_screen\|request_control\|share_screen_to","ok":bool,"reason":"...","payload":{}}` |
| `transfers_approved` | `{"req_id":N,"target":id,"transfers":[{"transfer_id":N,"name":..,"size":..}]}` |
| `file_incoming` | `{"transfer_id":N,"name":..,"size":..,"from":{...info}}` — شروع دریافت روی کنسول (کنسول خودکار `transfer_ready` می‌فرستد) |
| `transfer_done` / `transfer_failed` | `{"transfer_id":N,"reason":...}` |
| `screen_started` | `{"stream_id":N,"target":{...info}}` — کنسول ویوئر را باز می‌کند |
| `control_granted` / `control_denied` / `control_revoked` | `{"stream_id":N,"reason":...}` |
| `share_screen_ack` | `{"stream_id":N,"ok":bool,"reason":...}` — وضعیت باز شدن پنجره نمایش در کلاینت |
| `stream_ended` | `{"stream_id":N,"reason":"..."}` |
| `share_screen_started` | `{"stream_id":N,"target":{...}}` — کنسول شروع به فرستادن فریم می‌کند |
| `share_ended` | `{"stream_id":N,"reason":...}` |
| `error` | `{"code":..,"message":..}` |

### Server → Agent

| type | payload |
|---|---|
| `approval_request` | `{"req_id":N,"action":"send_files\|request_files\|screen_share\|console_share\|control","from":{...},"payload":{...}}` |
| `request_cancelled` | `{"req_id":N,"reason":...}` |
| `transfer_begin` | `{"transfer_id":N,"name":..,"size":..}` (جهت کنسول→ایجنت، بعد از تایید) |
| `transfer_ready` | `{"transfer_id":N,"name":..,"size":..}` (جهت ایجنت→کنسول؛ ایجنت چانک‌ها را می‌فرستد) |
| `input` | مثل جدول کنسول |
| `control_state` | `{"stream_id":N,"granted":bool}` — فقط بعد از تایید مرحله ۲ True می‌شود؛ رخدادهای ورودی بدون آن drop می‌شوند |
| `set_quality` | `{"stream_id":N,"quality":...}` |
| `stop_capture` | `{"stream_id":N}` |
| `share_screen_started` | `{"stream_id":N}` — پنجره نمایش را باز کن |
| `share_screen_stopped` | `{"stream_id":N}` |
| `viewers` | `{"stream_id":N,"count":n}` — اگر 0 شد کپچر متوقف می‌شود |
| `stop_share_view` | `{"stream_id":N}` |
| `error` / `pong` | — |

## 6. جریان‌های اصلی

### 6.1 ارسال فایل کنسول → کلاینت (درگ‌ودراپ)
```
Console: send_files ──▶ Server ──approval_request──▶ Agent (پاپ‌آپ)
Console ◀── transfers_approved ◀── Server ◀── approve(ok) ── Agent
Console: [transfer_ready] سپس چانک‌های باینری FILE_CHUNK (به‌ترتیب، یک فایل در لحظه)
Agent: ذخیره → transfer_done → Server → Console (نوار پیشرفت 100%)
```
- مسیر ذخیره پیش‌فرض در کلاینت: `~/Downloads/LocalTools` (قابل تغییر در کانفیگ)؛ نام تکراری خودکار `(n)` می‌گیرد.

### 6.2 درخواست فایل از کلاینت
```
Console: request_files → Agent (پاپ‌آپ «انتخاب و ارسال») → approve + picked_files
Server: برای هر فایل transfer می‌سازد → file_incoming به کنسول → کنسول transfer_ready
Server → Agent: transfer_ready → ایجنت چانک می‌فرستد → transfer_done → بعدی
Console: Blob می‌سازد و دانلود خودکار شروع می‌شود.
```

### 6.3 مشاهده صفحه کلاینت (مرحله 1: فقط مشاهده)
```
Console: request_screen(quality) → Server stream_id می‌سازد → پاپ‌آپ «مشاهده صفحه»
Agent: approve → screen_status(active) → CaptureThread شروع می‌کند → SCREEN_FRAME
Server: فریم را به همه ویوئرهای همان stream می‌فرستد (چند کنسول هم‌زمان مجاز)
Console: request_control → پاپ‌آپ «کنترل موس/کیبورد» → control_granted
Console: رخدادهای input → Agent (pynput) — تا release_control یا stop_view
```

### 6.4 اشتراک صفحه کنسول → کلاینت
```
Console: share_screen_to → پاپ‌آپ در کلاینت → approve
Server → Console: share_screen_started(stream_id) → getDisplayMedia → فریم‌های SCREEN_FRAME
Server → Agent: share_screen_started → پنجره tkinter نمایش فریم‌ها
پایان: دکمه توقف کنسول / بستن پنجره کلاینت (stop_share_view) / قطع اتصال
```
> این جهت فقط مشاهده است (مرورگر اجازه تزریق ورودی به سیستم نمی‌دهد). برای کنترل کامل، همان ماشین هم می‌تواند ایجنت را اجرا کند و مثل بقیه کلاینت‌ها کنترل شود.

## 7. امنیت

- بدون تایید پاپ‌آپ روی خود کلاینت، هیچ انتقال/اشتراکی شروع نمی‌شود.
- اگر سرور با `--code CODE` اجرا شود: ایجنت‌ها باید همان کد را در کانفیگ داشته باشند و کنسول وب باید با صفحه PIN وارد شود؛ در غیر این صورت اتصال WS با `error auth_failed` بسته می‌شود.
- کنترل موس/کیبورد همیشه جداگانه و حین جلسه تایید می‌شود (مرحله 2) و با `release_control`/پایان جلسه فوراً قطع می‌شود.
- `stream_id` کنترل‌شده در سرور تعیین تکلیف می‌شود: فقط یک کنسول در لحظه کنترل دارد.
