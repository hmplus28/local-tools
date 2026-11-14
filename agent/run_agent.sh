#!/usr/bin/env bash
# اجرای ایجنت روی لینوکس (نیازمند نشست X11 است؛ در Wayland کپچر/کنترل کار نمی‌کند)
# نمونه اولین اجرا:
#   ./run_agent.sh --server ws://192.168.1.10:8788/ws --code 1234
# اجراهای بعدی فقط:  ./run_agent.sh
cd "$(dirname "$0")"
if [ -f agent.json ]; then
  exec python3 agent.py "$@"
else
  python3 agent.py "$@"
fi
