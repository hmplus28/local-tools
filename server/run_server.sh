#!/usr/bin/env bash
# اجرای سرور مرکزی LocalTools
# نمونه: ./run_server.sh --port 8788 --code 1234
# برای قابلیت «اشتراک صفحه من» روی LAN حتماً --ssl بگذارید.
cd "$(dirname "$0")"
exec python3 server.py "$@"
