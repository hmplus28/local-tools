# راهنمای استقرار (Deploy)

## ۱. سرور مرکزی Hub

### لینوکس (روش پیشنهادی)
```bash
# یک جا کپی کنید، مثلاً /opt/local-tools
sudo cp -r . /opt/local-tools
pip install -r /opt/local-tools/server/requirements.txt

# تست دستی:
python3 /opt/local-tools/server/server.py --port 8788 --code <کد-شرکت> --ssl

# سرویس دائمی:
sudo cp /opt/local-tools/server/localtools-hub.service /etc/systemd/system/
# داخل فایل: مسیرها و --code را ویرایش کنید
sudo systemctl daemon-reload
sudo systemctl enable --now localtools-hub
sudo systemctl status localtools-hub
```

### ویندوز
```bat
pip install -r server\requirements.txt
server\run_server.bat --port 8788 --code <کد-شرکت> --ssl
```
برای اجرای دائمی می‌توانید یک Task در Task Scheduler بسازید (Action: `python server.py --port 8788 --code ... --ssl`، Trigger: At startup).

### گواهی SSL
با `--ssl` اگر `certs/hub.crt` و `certs/hub.key` نباشند، خودکار با openssl ساخته می‌شود (self-signed، CN=localtools-hub).
اگر می‌خواهید مرورگرها هشدار نگیرند، گواهی داخلی شرکت (مثلاً از CA خودِ AD) را با `--certfile/--keyfile` بدهید.

> **چرا SSL؟** دکمه «نمایش صفحه من» از API `getDisplayMedia` مرورگر استفاده می‌کند که فقط در Secure Context کار می‌کند.
> بدون `--ssl` فقط وقتی کنسول را از `localhost` باز کنید کار می‌کند.

## ۲. ایجنت روی کلاینت‌ها

### ویندوز
1. پایتون ۳.۹+ نصب باشد.
2. پوشه `agent/` را کنار هم کپی کنید (مثلاً `C:\LocalTools\agent`).
3. یک بار با پاورشل:
   ```
   pip install -r C:\LocalTools\agent\requirements.txt
   python C:\LocalTools\agent\agent.py --server ws://IP-SERVER:8788/ws --code <کد-شرکت>
   ```
4. از این به بعد `run_agent.bat` (بدون پنجره کنسول، با pythonw).
5. **استارت‌آپ خودکار**: از Task Manager → Startup یا Task Scheduler؛ یا میان‌بُر `run_agent.bat` را در پوشه Startup بگذارید:
   `shell:startup`
6. **فایروال**: خروجی به پورت ۸۷۸۸ سرور باید باز باشد (خروجی پیش‌فرض معمولاً باز است).

### لینوکس
1. `pip install -r agent/requirements.txt`
2. `python3 agent.py --server ws://IP-SERVER:8788/ws --code <کد-شرکت>`
3. سرویس کاربر:
   ```
   cp localtools-agent.service ~/.config/systemd/user/
   # ویرایش مسیرها و نشانی سرور
   systemctl --user daemon-reload
   systemctl --user enable --now localtools-agent
   loginctl enable-linger $USER   # اجرا حتی بدون لاگین‌شدن دسکتاپ
   ```
4. ⚠️ **X11 لازم است** — در نشست Wayland کپچر/کنترل کار نمی‌کند (روی Gnome: در صفحه ورود «Gnome on Xorg» را انتخاب کنید).

## ۳. فایل‌ها و پوشه‌های مهم

| مسیر | توضیح |
|---|---|
| `agent/agent.json` | کانفیگ ایجنت: سرور، کد، client_id (خودکار ساخته می‌شود)، save_dir |
| `~/Downloads/LocalTools` | محل ذخیره فایل‌های دریافتی کلاینت (قابل تغییر با `--save-dir`) |
| `server/certs/` | گواهی self-signed (اگر `--ssl` و بدون فایل گواهی اجرا شده باشد) |

## ۴. پارامترهای اجرا

### server.py
| پارامتر | پیش‌فرض | توضیح |
|---|---|---|
| `--port` | 8788 | پورت HTTP/WS |
| `--code` | — | کد دسترسی؛ بدون آن کنسول باز و ایجنت بدون کد |
| `--ssl` | خاموش | HTTPS با گواهی self-signed خودکار |
| `--certfile/--keyfile` | certs/hub.crt,key | گواهی اختصاصی |
| `--webconsole` | کنار repo | مسیر پوشه webconsole |

### agent.py
| پارامتر | پیش‌فرض | توضیح |
|---|---|---|
| `--server` | از agent.json | `ws://IP:8788/ws` (role و code خودکار اضافه می‌شود) |
| `--code` | از agent.json | کد دسترسی سرور |
| `--save-dir` | ~/Downloads/LocalTools | محل ذخیره فایل‌های دریافتی |
| `--monitor` | 0 | مانیتور کپچر: 0=همه (ترکیبی)، 1=اولین… |
| `--config` | agent.json کنار اسکریپت | مسیر کانفیگ |
| `--no-gui` | — | بدون پاپ‌آپ/پنجره (سرور headless) |
| `--auto-approve` | — | ⚠ فقط تست: همه تاییدها خودکار |
| `--fake-capture` | — | ⚠ فقط تست: کپچر مصنوعی |

## ۵. چک‌لیست راه‌اندازی روز اول
1. سرور را با `--code` و `--ssl` بالا بیاورید؛ صفحه PIN را باز کنید و وارد شوید.
2. روی یک کلاینت تستی ایجنت را اجرا کنید — باید بلافاصله در لیست سبز شود.
3. یک فایل درگ‌ودراپ کنید — روی کلاینت پاپ‌آپ تایید بیاید و بعد از تایید انتقال کامل شود.
4. «مشاهده صفحه» → تایید روی کلاینت → تصویر ببینید؛ «درخواست کنترل» → تایید دوم → موس/کیبورد تست کنید؛ «رها کردن کنترل».
5. «نمایش صفحه من» → تایید روی کلاینت → پنجره نمایش روی کلاینت باز شود.
