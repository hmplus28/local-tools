#!/usr/bin/env bash
# diagnose.sh — عیب‌یابی «کنسول وب از سیستم دیگر باز نمی‌شود»
# اجرا (بهتر است با sudo باشد تا فایروال هم چک شود):
#   sudo bash diagnose.sh [پورت]        — پیش‌فرض: 8788
set -u
PORT="${1:-8788}"

ok()   { printf '  ✓ %s\n' "$1"; }
bad()  { printf '  ✗ %s\n' "$1"; }
warn() { printf '  ⚠ %s\n' "$1"; }
hdr()  { printf '\n== %s ==\n' "$1"; }

echo "=============================================================="
echo "  LocalTools — عیب‌یابی دسترسی کنسول از سیستم دیگر"
echo "  پورت: $PORT"
echo "=============================================================="

# ۱) فرآیند سرور
hdr "۱) فرآیند سرور"
if pgrep -f "server\.py" >/dev/null 2>&1; then
  ok "server.py در حال اجراست: $(pgrep -af 'server\.py' | head -1)"
else
  bad "server.py اجرا نیست — در پوشه server اجرا کنید:"
  echo "      python server.py --port $PORT --code <کد> --ssl"
fi

# ۲) گوش دادن به پورت و آدرس بایند
hdr "۲) گوش دادن روی پورت $PORT"
LS=$(ss -tlnp 2>/dev/null | grep ":$PORT " || true)
if [ -n "$LS" ]; then
  ok "پورت گوش می‌دهد: $LS"
  if echo "$LS" | grep -q "127.0.0.1:$PORT"; then
    warn "فقط روی 127.0.0.1 بایند شده! از سیستم دیگر دیده نمی‌شود."
    echo "      سرور را بدون --host اجرا کنید (پیش‌فرض 0.0.0.0)."
  fi
else
  bad "هیچ فرآیندی روی پورت $PORT گوش نمی‌دهد (سرور خاموش یا پورت اشتباه)"
fi

# ۳) فایروال
hdr "۳) فایروال"
if command -v ufw >/dev/null 2>&1; then
  UOUT=$(ufw status 2>&1 || true)
  if echo "$UOUT" | grep -qi "root"; then
    warn "برای چک ufw باید با sudo اجرا کنید:  sudo bash diagnose.sh $PORT"
  elif echo "$UOUT" | grep -q "Status: active"; then
    if echo "$UOUT" | grep -q "$PORT/tcp"; then
      ok "ufw فعال است و قانون $PORT/tcp وجود دارد"
    else
      bad "ufw فعال است ولی قانون پورت $PORT وجود ندارد — اجرا کنید:"
      echo "      sudo ufw allow $PORT/tcp"
    fi
  else
    ok "ufw غیرفعال است — فایروال مانع نیست"
  fi
elif command -v firewall-cmd >/dev/null 2>&1; then
  FOUT=$(firewall-cmd --state 2>&1 || true)
  if echo "$FOUT" | grep -qi "root\|not running\|failed"; then
    warn "firewalld در دسترس/فعال نیست — فایروال احتمالاً مانع نیست"
  else
    if firewall-cmd --list-ports 2>/dev/null | grep -q "$PORT/tcp"; then
      ok "firewalld فعال و پورت باز است"
    else
      bad "firewalld فعال ولی پورت بسته — اجرا کنید:"
      echo "      sudo firewall-cmd --permanent --add-port=$PORT/tcp && sudo firewall-cmd --reload"
    fi
  fi
else
  warn "ufw/firewalld پیدا نشد — iptables یا فایروال شرکت را دستی چک کنید"
fi

# ۴) پاسخ‌گویی محلی
hdr "۴) پاسخ‌گویی محلی"
if command -v curl >/dev/null 2>&1; then
  CODE=$(curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "https://127.0.0.1:$PORT/health" 2>/dev/null)
  if [ "${CODE:-000}" = "200" ]; then
    ok "GET /health محلی (HTTPS) → 200 — سرور سالم است"
  else
    CODE=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null)
    if [ "${CODE:-000}" = "200" ]; then
      warn "سرور بدون --ssl اجرا شده (HTTP) — «نمایش صفحه من» از LAN کار نمی‌کند؛ با --ssl اجرا کنید"
    else
      bad "پاسخ محلی: ${CODE:-000} — سرور بالا نیست یا روی پورت دیگری است"
    fi
  fi
else
  warn "curl نصب نیست — پاسخ‌گویی محلی را با مرورگر روی https://127.0.0.1:$PORT/ چک کنید"
fi

# ۵) IPهای ماشین و URL درست برای سیستم دیگر
hdr "۵) نشانی‌ها"
IPS=""
if hostname -I >/dev/null 2>&1; then
  IPS=$(hostname -I | tr ' ' '\n' | grep -v '^127\.' | grep -v '^$')
elif command -v ip >/dev/null 2>&1; then
  IPS=$(ip -4 -o addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]}')
fi
if [ -n "$IPS" ]; then
  # بریج‌های داکر/LXC (172.16-31) را ته لیست ببر — IP شبکه معمولاً 192.168.x یا 10.x است
  IPS=$(echo "$IPS" | awk -F. '{br=($1=="172" && $2+0>=16 && $2+0<=31)?1:0; print br"\t"$0}' | sort -s -k1,1 | cut -f2)
  for ip in $IPS; do echo "  • $ip"; done
  FIRST=$(echo "$IPS" | head -1)
  ok "از سیستم دیگر این را باز کنید:  https://$FIRST:$PORT/"
else
  warn "IP محلی پیدا نشد — با «ip a» یا «ipconfig» چک کنید"
fi

# ۶) قدم بعدی: تست از سیستم دیگر + جدول تفسیر
hdr "۶) قدم بعدی — روی سیستم دیگر تست کنید"
echo "  ویندوز (PowerShell):"
echo "    Test-NetConnection <IP-سرور> -Port $PORT"
echo "    curl.exe -kI https://<IP-سرور>:$PORT/health"
echo "  لینوکس/macOS:"
echo "    nc -zv <IP-سرور> $PORT"
echo "    curl -kI https://<IP-سرور>:$PORT/health"
echo ""
echo "  تفسیر نتایج:"
echo "    ping/TCP ناموفق          → شبکه: سابنت متفاوت، AP/client isolation وای‌فای، VPN روشن روی کلاینت"
echo "    TCP ناموفق               → فایروال سرور (بخش ۳) یا سرور خاموش (بخش ۱ و ۲)"
echo "    TCP موفق ولی مرورگر باز نمی‌کند → هشدار گواهی را Proceed/Accept کنید؛ پروکسی/VPN مرورگر را خاموش کنید"
echo ""
echo "  ترفند: حین تست از سیستم دیگر، ترمینال سرور را نگاه کنید —"
echo "  اگر خطی مثل «GET /health 200» چاپ نشد، ترافیک اصلاً به سرور نرسیده (مشکل شبکه/فایروال است)."
echo "=============================================================="
