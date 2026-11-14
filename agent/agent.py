#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocalTools Agent — سرویس سمت کلاینت (ویندوز / لینوکس)

امکانات:
  • ثبت‌نام روی سرور مرکزی و نمایش در لیست کنسول
  • دریافت فایل با تایید پاپ‌آپ (tkinter) → ذخیره در Downloads/LocalTools
  • درخواست فایل از کنسول → انتخاب فایل با فایل‌دایالوگ → ارسال
  • اشتراک صفحه خود کلاینت برای کنسول (mss + JPEG)
  • کنترل موس/کیبورد از کنسول (pynput) — فقط با تایید حین جلسه
  • نمایش صفحه اشتراک‌شده از کنسول در پنجره tkinter

نصب پیش‌نیازها: pip install -r requirements.txt
اجرای آزمایشی بدون GUI (برای سرورهای headless):
  python agent.py --server ws://IP:8788/ws --code XXX --auto-approve --fake-capture
"""

import argparse
import asyncio
import getpass
import json
import logging
import os
import platform
import socket
import sys
import threading
import time
import queue as pyqueue

VERSION = "1.0.0"
BIN_SCREEN_FRAME = 0x01
BIN_FILE_CHUNK = 0x02
CHUNK_SIZE = 256 * 1024
HEARTBEAT_INTERVAL = 10
POPUP_TIMEOUT = 60
# (scale, jpeg_quality, fps)
QUALITY_PRESET = {"low": (0.45, 55, 8), "medium": (0.65, 68, 10), "high": (1.0, 78, 14)}

log = logging.getLogger("localtools.agent")

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------
try:
    import websockets
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    print("websockets نصب نیست:  pip install websockets", file=sys.stderr)
    sys.exit(1)

try:
    import mss
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

try:
    from PIL import Image, ImageTk
    import io as _io
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    HAS_TK = True
except ImportError:
    HAS_TK = False

try:
    import pynput
    from pynput.mouse import Button as PyButton
    from pynput.keyboard import Key as PyKey, KeyCode
    HAS_PYNPUT = True
except Exception:
    HAS_PYNPUT = False


def build_binary(btype: int, stream_id: int, meta: dict, payload: bytes = b"") -> bytes:
    m = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    return bytes([btype]) + stream_id.to_bytes(4, "big") + len(m).to_bytes(4, "big") + m + payload


def unique_path(path: str) -> str:
    """اگر فایل موجود بود، نام (n) می‌گیرد — همان رفتار file_share"""
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def safe_name(name: str) -> str:
    name = name.replace("\\", "_").replace("/", "_").strip()
    return name or "file"


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, path):
        self.path = path
        self.data = {"server": "", "code": "", "client_id": "", "save_dir": "", "name": ""}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception as e:
                log.warning("config load failed: %s", e)
        if not self.data.get("client_id"):
            import uuid
            self.data["client_id"] = uuid.uuid4().hex[:12]
            self.save()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("config save failed: %s", e)


# ---------------------------------------------------------------------------
# GUI: notifier thread (پاپ‌آپ‌های تایید)
# ---------------------------------------------------------------------------
class GuiNotifier(threading.Thread):
    """یک ترد مستقل با Tk root مخفی؛ پاپ‌آپ‌ها به ترتیب نمایش داده می‌شوند."""

    def __init__(self, agent):
        super().__init__(daemon=True, name="gui-notifier")
        self.agent = agent
        self.q = pyqueue.Queue()
        self.dialog = None          # (req_id, win)
        self.cancelled = set()
        self.start()

    def run(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.title("LocalTools Agent")
            self._poll()
            self.root.mainloop()
        except Exception as e:
            log.error("notifier thread died: %s", e)

    # --- called from asyncio thread ---
    def ask(self, req_id, action, from_info, payload):
        self.q.put((req_id, action, from_info, payload))

    def cancel(self, req_id):
        self.cancelled.add(req_id)
        d = self.dialog
        if d and d[0] == req_id:
            try:
                d[1].after(0, d[1].destroy)
            except Exception:
                pass

    # --- tk thread ---
    def _poll(self):
        try:
            if self.dialog is None and not self.q.empty():
                req_id, action, from_info, payload = self.q.get_nowait()
                if req_id in self.cancelled:
                    self.cancelled.discard(req_id)
                else:
                    self._show(req_id, action, from_info, payload)
        except Exception as e:
            log.error("poll error: %s", e)
        try:
            self.root.after(120, self._poll)
        except Exception:
            pass

    def _respond(self, req_id, ok, detail=None):
        self.dialog = None
        ctx = (self.dialog_ctx.pop(req_id, None) or {}) if hasattr(self, "dialog_ctx") else {}
        detail = dict(detail or {})
        if ctx.get("action") == "screen_share":
            detail["_start_stream"] = ctx.get("stream_id")
            detail["_quality"] = ctx.get("quality", "medium")
        self.agent.loop.call_soon_threadsafe(
            self.agent.on_popup_result, req_id, ok, detail)

    def _show(self, req_id, action, from_info, payload):
        win = tk.Toplevel(self.root)
        win.title("درخواست دسترسی — LocalTools")
        win.geometry("460x330")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg="#f8fafc")
        self.dialog = (req_id, win)
        if not hasattr(self, "dialog_ctx"):
            self.dialog_ctx = {}
        self.dialog_ctx[req_id] = {"action": action, "stream_id": payload.get("stream_id"),
                                   "quality": payload.get("quality", "medium")}

        who = from_info.get("via", "کنسول مدیریت") if from_info else "کنسول مدیریت"
        texts = {
            "send_files": ("📥 درخواست ارسال فایل", "کنسول «%s» می‌خواهد این فایل‌ها را به شما بفرستد:"),
            "request_files": ("📤 درخواست دریافت فایل", "کنسول «%s» درخواست دریافت فایل از شما را دارد."),
            "screen_share": ("🖥️ اشتراک صفحه", "کنسول «%s» می‌خواهد صفحه شما را مشاهده کند.\n(ابتدا فقط مشاهده است؛ کنترل موس/کیبورد جداگانه تایید می‌شود)"),
            "console_share": ("📺 نمایش صفحه کنسول", "کنسول «%s» صفحه خود را برای شما به اشتراک می‌گذارد."),
            "control": ("⌨️ درخواست کنترل", "کنسول «%s» درخواست کنترل موس و کیبورد شما را دارد.\nهر لحظه می‌توانید از کنسول آن را رها کند."),
        }
        title, body = texts.get(action, ("درخواست", "درخواستی از «%s» دارد:"))
        tk.Label(win, text=title, bg="#f8fafc", fg="#0f172a",
                 font=("Segoe UI", 13, "bold")).pack(pady=(18, 6))
        tk.Label(win, text=body % who, bg="#f8fafc", fg="#334155",
                 font=("Segoe UI", 10), wraplength=420, justify="center").pack(padx=16)

        detail_holder = {}

        if action == "send_files":
            files = payload.get("files", [])
            total = sum(f.get("size", 0) for f in files)
            listbox = tk.Listbox(win, height=min(5, max(2, len(files))), font=("Segoe UI", 9),
                                 bg="#ffffff", fg="#0f172a", relief="flat", highlightthickness=1)
            for f in files:
                listbox.insert("end", f"  {f.get('name')}  ({f.get('size', 0)/1024:.1f} KB)")
            listbox.pack(fill="x", padx=20, pady=8)
            tk.Label(win, text=f"حجم کل: {total/1048576:.1f} MB — ذخیره در Downloads/LocalTools",
                     bg="#f8fafc", fg="#64748b", font=("Segoe UI", 9)).pack()

        if action == "request_files":
            picked = {"files": None}

            def pick():
                paths = filedialog.askopenfilenames(parent=win, title="انتخاب فایل برای ارسال")
                if paths:
                    picked["files"] = [{"name": os.path.basename(p), "size": os.path.getsize(p)} for p in paths]
                    picked["paths"] = paths
                    lbl.config(text=f"{len(paths)} فایل انتخاب شد — تایید کنید", fg="#10b981")

            tk.Button(win, text="📂 انتخاب فایل(ها)", command=pick, bg="#e2e8f0", fg="#0f172a",
                      font=("Segoe UI", 10, "bold"), bd=0, pady=6, padx=12).pack(pady=8)
            lbl = tk.Label(win, text="فایلی انتخاب نشده", bg="#f8fafc", fg="#64748b", font=("Segoe UI", 9))
            lbl.pack()
            detail_holder["picked"] = picked

        btns = tk.Frame(win, bg="#f8fafc")
        btns.pack(side="bottom", pady=16)

        def approve():
            if action == "request_files":
                p = detail_holder["picked"]
                files = p.get("files")
                if not files:
                    messagebox.showinfo("انتخاب فایل", "ابتدا فایل انتخاب کنید", parent=win)
                    return
                self._respond(req_id, True, {"picked_files": files, "paths": list(p.get("paths", []))})
            else:
                self._respond(req_id, True, {})
            win.destroy()

        def reject():
            self._respond(req_id, False, {})
            win.destroy()

        ok_btn = tk.Button(btns, text="✓ تایید", command=approve, bg="#10b981", fg="white",
                           font=("Segoe UI", 11, "bold"), bd=0, padx=26, pady=8)
        ok_btn.pack(side="left", padx=8)
        no_btn = tk.Button(btns, text="✕ رد", command=reject, bg="#f43f5e", fg="white",
                           font=("Segoe UI", 11, "bold"), bd=0, padx=26, pady=8)
        no_btn.pack(side="left", padx=8)

        # timeout محلی
        win.after(POPUP_TIMEOUT * 1000, lambda: (win.winfo_exists() and reject()))

        # وسط صفحه
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - 460) // 2}+{(sh - 330) // 2}")
        win.lift()


# ---------------------------------------------------------------------------
# GUI: پنجره نمایش صفحه کنسول
# ---------------------------------------------------------------------------
class ViewerWindow(threading.Thread):
    def __init__(self, agent, stream_id):
        super().__init__(daemon=True, name=f"viewer-{stream_id}")
        self.agent = agent
        self.stream_id = stream_id
        self.frames = pyqueue.Queue(maxsize=3)
        self.closed = threading.Event()
        self.start()

    def run(self):
        try:
            self.root = tk.Tk()
            self.root.title("اشتراک صفحه کنسول — LocalTools")
            self.root.geometry("1100x680")
            self.root.configure(bg="#000000")
            self.label = tk.Label(self.root, bg="#000000",
                                  text="در انتظار تصویر از کنسول…", fg="#64748b",
                                  font=("Segoe UI", 12))
            self.label.pack(fill="both", expand=True)
            hint = tk.Label(self.root, text="F11: تمام‌صفحه · Esc: خروج از تمام‌صفحه · بستن پنجره = پایان اشتراک",
                            bg="#0f172a", fg="#64748b", font=("Segoe UI", 9))
            hint.pack(fill="x")

            def toggle_fs(e=None):
                self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))
            self.root.bind("<F11>", toggle_fs)
            self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)

            self._photo = None
            self._draw()
            self.root.mainloop()
        except Exception as e:
            log.error("viewer window died: %s", e)
            self.on_close()

    def push_frame(self, payload):
        try:
            if self.frames.full():
                self.frames.get_nowait()  # فریم قدیمی دور ریخته می‌شود
            self.frames.put_nowait(payload)
        except Exception:
            pass

    def _draw(self):
        if self.closed.is_set():
            return
        try:
            payload = None
            try:
                payload = self.frames.get_nowait()
            except pyqueue.Empty:
                pass
            if payload is not None and HAS_PIL:
                img = Image.open(_io.BytesIO(payload))
                # fit به اندازه پنجره
                w = max(self.root.winfo_width(), 400)
                h = max(self.root.winfo_height() - 24, 300)
                scale = min(w / img.width, h / img.height)
                img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                                 Image.LANCZOS if scale < 1 else Image.BILINEAR)
                self._photo = ImageTk.PhotoImage(img)
                self.label.config(image=self._photo, text="")
            self.root.after(60, self._draw)
        except tk.TclError:
            return  # پنجره بسته شده است
        except Exception as e:
            log.debug("viewer draw: %s", e)

    def on_close(self):
        self.closed.set()
        try:
            self.root.destroy()
        except Exception:
            pass
        self.agent.loop.call_soon_threadsafe(
            self.agent.loop.create_task,
            self.agent.send_json({"type": "stop_share_view", "stream_id": self.stream_id}))


# ---------------------------------------------------------------------------
# Capture session (اشتراک صفحه خود کلاینت)
# ---------------------------------------------------------------------------
class CaptureSession:
    def __init__(self, agent, stream_id, quality):
        self.agent = agent
        self.stream_id = stream_id
        self.params = list(QUALITY_PRESET.get(quality, QUALITY_PRESET["medium"]))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"capture-{stream_id}")
        self.thread.start()

    def set_quality(self, quality):
        self.params = list(QUALITY_PRESET.get(quality, self.params))

    def stop(self):
        self.stop_event.set()

    def _run(self):
        a = self.agent
        scale, jpeg_q, fps = self.params
        delay = 1.0 / fps
        sent = 0
        try:
            if HAS_MSS and not a.fake_capture:
                with mss.MSS() as sct:
                    mon = sct.monitors[a.monitor] if a.monitor < len(sct.monitors) else sct.monitors[0]
                    left, top, width, height = mon["left"], mon["top"], mon["width"], mon["height"]
                    while not self.stop_event.is_set():
                        t0 = time.time()
                        scale, jpeg_q = self.params[0], self.params[1]
                        raw = sct.grab(mon)
                        if HAS_PIL:
                            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                            if scale < 1.0:
                                img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                                                 Image.BILINEAR)
                            buf = _io.BytesIO()
                            img.save(buf, "JPEG", quality=jpeg_q)
                            payload = buf.getvalue()
                            w, h = img.width, img.height
                        else:
                            break
                        cx, cy = a.cursor_position(raw_to_abs=(left, top, width, height))
                        data = build_binary(BIN_SCREEN_FRAME, self.stream_id,
                                            {"w": w, "h": h, "cx": cx, "cy": cy}, payload)
                        a.enqueue_binary(data)
                        sent += 1
                        dt = time.time() - t0
                        self.stop_event.wait(max(0.01, delay - dt))
            else:
                # fake capture — تصویر آزمایشی برای سرورهای headless
                import io as _io2
                from PIL import ImageDraw
                fpos = [0]
                while not self.stop_event.is_set():
                    t0 = time.time()
                    scale, jpeg_q = self.params[0], self.params[1]
                    w0, h0 = 1280, 720
                    img = Image.new("RGB", (w0, h0), (24, 26, 38))
                    px = img.load()
                    fpos[0] = (fpos[0] + 7) % (w0 - 80)
                    for yy in range(0, h0, 40):
                        for xx in range(0, w0, 40):
                            px[xx, yy] = (30 + (xx // 40) * 8 % 60, 34 + (yy // 40) * 6 % 50, 52)
                    for rr in range(30):
                        px[min(w0 - 1, fpos[0] + rr), 360] = (99, 102, 241)
                    d = ImageDraw.Draw(img)
                    d.text((40, 40), f"LocalTools fake capture | frame {sent} | {time.strftime('%H:%M:%S')}",
                           fill=(200, 205, 220))
                    if scale < 1.0:
                        img = img.resize((int(w0 * scale), int(h0 * scale)), Image.BILINEAR)
                    buf = _io2.BytesIO()
                    img.save(buf, "JPEG", quality=jpeg_q)
                    payload = buf.getvalue()
                    data = build_binary(BIN_SCREEN_FRAME, self.stream_id,
                                        {"w": img.width, "h": img.height, "cx": fpos[0], "cy": 360}, payload)
                    a.enqueue_binary(data)
                    sent += 1
                    dt = time.time() - t0
                    self.stop_event.wait(max(0.01, delay - dt))
        except Exception as e:
            log.error("capture error: %s", e)
            a.loop.call_soon_threadsafe(
                a.loop.create_task,
                a.send_json({"type": "screen_status", "stream_id": self.stream_id,
                             "active": False, "reason": f"capture_error: {e}"}))
            return
        finally:
            a.captures.pop(self.stream_id, None)
        log.info("capture stream %s stopped after %d frames", self.stream_id, sent)


# ---------------------------------------------------------------------------
# Outgoing file sender (a2c)
# ---------------------------------------------------------------------------
async def send_file_task(agent, transfer_id, path, size):
    try:
        seq = 0
        with open(path, "rb") as f:
            offset = 0
            while offset < size:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                offset += len(chunk)
                data = build_binary(BIN_FILE_CHUNK, 0,
                                    {"transfer_id": transfer_id, "seq": seq, "last": offset >= size}, chunk)
                await agent.ws_send_raw(data)
                seq += 1
                await asyncio.sleep(0)
        await agent.send_json({"type": "transfer_done", "transfer_id": transfer_id})
        log.info("file sent: %s (%d bytes) tid=%d", os.path.basename(path), size, transfer_id)
    except Exception as e:
        log.error("send file failed tid=%d: %s", transfer_id, e)
        await agent.send_json({"type": "transfer_failed", "transfer_id": transfer_id, "reason": str(e)})


# ---------------------------------------------------------------------------
# Input control (pynput)
# ---------------------------------------------------------------------------
KEY_MAP = {}
if HAS_PYNPUT:
    def _k(*names):
        """اولین کلید موجود در بک‌اند فعلی (نام‌ها بین ویندوز/لینوکس فرق می‌کنند)"""
        for n in names:
            k = getattr(PyKey, n, None)
            if k is not None:
                return k
        return None

    KEY_MAP = {
        "Enter": _k("enter"), "Backspace": _k("backspace"), "Tab": _k("tab"),
        "Escape": _k("esc"), " ": _k("space"), "Space": _k("space"),
        "ArrowUp": _k("up"), "ArrowDown": _k("down"), "ArrowLeft": _k("left"), "ArrowRight": _k("right"),
        "Home": _k("home"), "End": _k("end"), "PageUp": _k("page_up"), "PageDown": _k("page_down"),
        "Delete": _k("delete"), "Insert": _k("insert"),
        "Shift": _k("shift"), "Control": _k("ctrl"), "Alt": _k("alt"),
        "Meta": _k("cmd", "super_l"),
        "CapsLock": _k("caps_lock"),
        "F1": _k("f1"), "F2": _k("f2"), "F3": _k("f3"), "F4": _k("f4"), "F5": _k("f5"),
        "F6": _k("f6"), "F7": _k("f7"), "F8": _k("f8"), "F9": _k("f9"),
        "F10": _k("f10"), "F11": _k("f11"), "F12": _k("f12"),
        "PrintScreen": _k("print_screen"), "ContextMenu": _k("menu"),
        "AudioVolumeMute": _k("media_volume_mute", "volume_mute"),
        "AudioVolumeDown": _k("media_volume_down", "volume_down"),
        "AudioVolumeUp": _k("media_volume_up", "volume_up"),
        "MediaPlayPause": _k("media_play_pause", "play_pause"),
    }
    KEY_MAP = {k: v for k, v in KEY_MAP.items() if v is not None}
BTN_MAP = {}
if HAS_PYNPUT:
    BTN_MAP = {"left": PyButton.left, "right": PyButton.right, "middle": PyButton.middle}


class InputController:
    def __init__(self, agent):
        self.agent = agent
        self.mouse = None
        self.kb = None
        self.available = False
        if HAS_PYNPUT:
            try:
                self.mouse = pynput.mouse.Controller()
                self.kb = pynput.keyboard.Controller()
                self.available = True
            except Exception as e:
                log.warning("pynput controllers unavailable: %s", e)

    def apply(self, ev):
        if not self.available:
            return
        kind = ev.get("kind")
        try:
            if kind == "mouse_move":
                self.mouse.position = (int(ev.get("x", 0)), int(ev.get("y", 0)))
            elif kind == "mouse_btn":
                btn = BTN_MAP.get(ev.get("btn", "left"), PyButton.left)
                if ev.get("pressed"):
                    self.mouse.press(btn)
                else:
                    self.mouse.release(btn)
            elif kind == "wheel":
                dy = float(ev.get("dy", 0))
                clicks = -dy / 100.0
                if abs(clicks) < 1:
                    clicks = -1 if dy > 0 else 1
                self.mouse.scroll(0, int(round(clicks)))
            elif kind == "key":
                self._key(ev.get("key", ""), bool(ev.get("pressed")))
        except Exception as e:
            log.debug("input apply error: %s", e)

    def _key(self, name, pressed):
        k = KEY_MAP.get(name)
        if k is None:
            if len(name) == 1:
                k = KeyCode.from_char(name)
            else:
                return
        if pressed:
            self.kb.press(k)
        else:
            self.kb.release(k)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class Agent:
    def __init__(self, args):
        self.args = args
        self.config_path = args.config or os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.json")
        self.config = Config(self.config_path)
        if args.server:
            self.config.data["server"] = args.server
        if args.code is not None:
            self.config.data["code"] = args.code
        if args.save_dir:
            self.config.data["save_dir"] = args.save_dir
        if not self.config.data.get("server"):
            print("نشانی سرور تنظیم نیست. مثال:\n  python agent.py --server ws://192.168.1.10:8788/ws --code 1234",
                  file=sys.stderr)
            sys.exit(1)
        if not self.config.data.get("save_dir"):
            home = os.path.expanduser("~")
            self.config.data["save_dir"] = os.path.join(home, "Downloads", "LocalTools")
        os.makedirs(self.config.data["save_dir"], exist_ok=True)
        self.config.save()

        self.loop = None
        self.ws = None
        self.incoming = {}      # transfer_id -> dict(fh, path, name, size, bytes)
        self.selected_for_send = []   # [(name, path)] — فایل‌های انتخاب‌شده برای request_files
        self.captures = {}      # stream_id -> CaptureSession
        self.control_streams = set()
        self.input = InputController(self)
        self.share_viewers = {}  # stream_id -> ViewerWindow
        self.share_frames_in = {}
        self.fake_capture = args.fake_capture
        self.monitor = args.monitor
        self.auto_approve = args.auto_approve
        self.auto_file = args.auto_file
        self.sent_frames = 0

        has_display = (os.name == "nt") or (sys.platform == "darwin") or bool(os.environ.get("DISPLAY"))
        self.gui = HAS_TK and has_display and not args.no_gui
        self.notifier = GuiNotifier(self) if self.gui else None
        if not self.gui:
            log.info("GUI در دسترس نیست — پاپ‌آپ‌های تایید غیرفعال (برای تست از --auto-approve استفاده کنید)")

    # ---------- connection ----------
    async def run(self):
        self.loop = asyncio.get_running_loop()
        url = self.config.data["server"]
        # role و code به نشانی اضافه می‌شود (اگر کاربر ننوشته باشد)
        if "role=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}role=agent"
        if self.config.data.get("code"):
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}code={self.config.data['code']}"
        backoff = 2
        while True:
            try:
                log.info("connecting to %s …", url.split("?")[0])
                async with ws_connect(url, max_size=8 * 1024 * 1024, ping_interval=20) as ws:
                    self.ws = ws
                    backoff = 2
                    await self.register()
                    hb = asyncio.ensure_future(self._heartbeat())
                    try:
                        async for raw in ws:
                            if isinstance(raw, str):
                                try:
                                    await self.handle_json(json.loads(raw))
                                except Exception as e:
                                    log.error("handle_json(%s): %s", raw[:80], e, exc_info=True)
                            else:
                                await self.handle_binary(raw)
                    finally:
                        hb.cancel()
                        self._on_disconnect()
            except Exception as e:
                log.warning("connection lost: %s — retry in %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    def _on_disconnect(self):
        self.ws = None
        for cap in list(self.captures.values()):
            cap.stop()
        self.captures.clear()
        self.control_streams.clear()
        for tid, v in list(self.incoming.items()):
            try:
                v["fh"].close()
            except Exception:
                pass
            try:
                os.remove(v["path"])
            except Exception:
                pass
        self.incoming.clear()
        for v in list(self.share_viewers.values()):
            v.closed.set()
        self.share_viewers.clear()

    async def _heartbeat(self):
        while True:
            await self.send_json({"type": "heartbeat"})
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def register(self):
        caps = {"capture": HAS_MSS or self.fake_capture, "input": self.input.available, "gui": self.gui}
        info = {
            "hostname": socket.gethostname(),
            "os": platform.system(),
            "os_version": platform.release() + (" " + platform.version() if os.name == "nt" else ""),
            "user": getpass.getuser(),
            "ip": lan_ip(),
            "version": VERSION,
            "capabilities": caps,
            "monitors": 1,
        }
        if HAS_MSS and not self.fake_capture:
            try:
                with mss.MSS() as sct:
                    info["monitors"] = max(1, len(sct.monitors) - 1)
            except Exception:
                pass
        await self.send_json({"type": "register", "client_id": self.config.data["client_id"], "info": info})
        log.info("registered: %s (%s %s | user=%s | caps=%s)",
                 info["hostname"], info["os"], info["os_version"], info["user"], caps)

    # ---------- send helpers ----------
    async def send_json(self, obj):
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps(obj, ensure_ascii=False))
            except Exception as e:
                log.debug("send_json failed: %s", e)

    async def ws_send_raw(self, data: bytes):
        if self.ws is not None:
            await self.ws.send(data)

    def enqueue_binary(self, data: bytes):
        """از تردهای کپچر — بدون بلاک شدن؛ صف کوچک با drop"""
        if self.loop is None or self.ws is None:
            return
        self.loop.call_soon_threadsafe(self._enqueue_now, data)

    def _enqueue_now(self, data):
        if self.ws is None:
            return
        self.sent_frames += 1
        asyncio.ensure_future(self.ws_send_raw(data))

    def cursor_position(self, raw_to_abs=None):
        if HAS_PYNPUT and self.input.mouse is not None:
            try:
                x, y = self.input.mouse.position
                if raw_to_abs:
                    left, top, width, height = raw_to_abs
                    return int(x - left), int(y - top)
                return int(x), int(y)
            except Exception:
                pass
        return -1, -1

    # ---------- messages ----------
    async def handle_json(self, msg):
        mtype = msg.get("type", "")
        if mtype == "approval_request":
            await self.handle_approval(msg)
        elif mtype == "request_cancelled":
            if self.notifier:
                self.notifier.cancel(int(msg.get("req_id", 0)))
        elif mtype == "transfer_begin":
            await self._begin_incoming(int(msg.get("transfer_id", 0)), msg.get("name", "file"), int(msg.get("size", 0)))
        elif mtype == "transfer_ready":
            # فایل انتخابی با همین نام از صف انتخاب برداشته و ارسال می‌شود
            tid = int(msg.get("transfer_id", 0))
            name = msg.get("name", "")
            path = None
            for i, (nm, pth) in enumerate(self.selected_for_send):
                if nm == name:
                    path = pth
                    self.selected_for_send.pop(i)
                    break
            if path and os.path.isfile(path):
                asyncio.ensure_future(send_file_task(self, tid, path, int(msg.get("size", 0)) or os.path.getsize(path)))
            else:
                asyncio.ensure_future(self.send_json({"type": "transfer_failed", "transfer_id": tid,
                                                      "reason": "فایل محلی پیدا نشد"}))
        elif mtype == "input":
            sid = int(msg.get("stream_id", 0))
            if sid in self.control_streams:
                self.input.apply(msg.get("ev", {}))
        elif mtype == "control_state":
            sid = int(msg.get("stream_id", 0))
            if msg.get("granted"):
                self.control_streams.add(sid)
                log.info("control ENABLED for stream %s", sid)
            else:
                self.control_streams.discard(sid)
                log.info("control DISABLED for stream %s", sid)
        elif mtype == "set_quality":
            cap = self.captures.get(int(msg.get("stream_id", 0)))
            if cap:
                cap.set_quality(msg.get("quality", "medium"))
        elif mtype == "viewers":
            sid = int(msg.get("stream_id", 0))
            cap = self.captures.get(sid)
            if cap and int(msg.get("count", 0)) == 0:
                cap.stop()
        elif mtype == "stop_capture":
            sid = int(msg.get("stream_id", 0))
            cap = self.captures.pop(sid, None)
            if cap:
                cap.stop()
        elif mtype == "share_screen_started":
            await self._open_share_viewer(int(msg.get("stream_id", 0)))
        elif mtype == "share_screen_stopped":
            sid = int(msg.get("stream_id", 0))
            v = self.share_viewers.pop(sid, None)
            if v:
                v.closed.set()
            self.share_frames_in.pop(sid, None)
        elif mtype == "pong":
            pass
        elif mtype == "error":
            log.warning("server error: %s", msg.get("message"))
        else:
            log.debug("unknown message: %s", mtype)

    async def handle_binary(self, raw: bytes):
        if len(raw) < 9:
            return
        btype = raw[0]
        meta_len = int.from_bytes(raw[5:9], "big")
        try:
            meta = json.loads(raw[9:9 + meta_len].decode("utf-8"))
        except Exception:
            return
        payload = raw[9 + meta_len:]
        if btype == BIN_FILE_CHUNK:
            tid = int(meta.get("transfer_id", 0))
            inc = self.incoming.get(tid)
            if inc is None:
                return
            fh = inc["fh"]
            fh.write(payload)
            inc["bytes"] += len(payload)
            if meta.get("last"):
                fh.close()
                self.incoming.pop(tid, None)
                log.info("file received: %s (%d bytes)", inc["name"], inc["bytes"])
                await self.send_json({"type": "transfer_done", "transfer_id": tid})
        elif btype == BIN_SCREEN_FRAME:
            sid = int.from_bytes(raw[1:5], "big")
            self.share_frames_in[sid] = self.share_frames_in.get(sid, 0) + 1
            v = self.share_viewers.get(sid)
            if v and not v.closed.is_set():
                v.push_frame(payload)

    # ---------- approvals ----------
    async def handle_approval(self, msg):
        req_id = int(msg.get("req_id", 0))
        action = msg.get("action", "")
        payload = msg.get("payload", {})
        if self.auto_approve:
            await self._auto_approve(req_id, action, payload)
        elif self.notifier:
            self.notifier.ask(req_id, action, msg.get("from"), payload)
        else:
            await self.send_json({"type": "approve", "req_id": req_id, "ok": False, "detail": {"reason": "no_gui"}})

    async def _auto_approve(self, req_id, action, payload):
        if action == "send_files":
            await self.send_json({"type": "approve", "req_id": req_id, "ok": True, "detail": {}})
        elif action == "request_files":
            if self.auto_file and os.path.isfile(self.auto_file):
                name = os.path.basename(self.auto_file)
                self.selected_for_send.append((name, self.auto_file))
                await self.send_json({"type": "approve", "req_id": req_id, "ok": True,
                                      "detail": {"picked_files": [{"name": name, "size": os.path.getsize(self.auto_file)}]}})
            else:
                await self.send_json({"type": "approve", "req_id": req_id, "ok": False,
                                      "detail": {"reason": "no auto-file"}})
        elif action == "screen_share":
            await self.send_json({"type": "approve", "req_id": req_id, "ok": True, "detail": {}})
            await self._start_capture(int(payload.get("stream_id", 0)), payload.get("quality", "medium"))
        elif action == "control":
            await self.send_json({"type": "approve", "req_id": req_id, "ok": True, "detail": {}})
        elif action == "console_share":
            await self.send_json({"type": "approve", "req_id": req_id, "ok": True, "detail": {}})
        else:
            await self.send_json({"type": "approve", "req_id": req_id, "ok": False, "detail": {}})

    def on_popup_result(self, req_id, ok, detail):
        """از ترد GUI — همیشه با call_soon_threadsafe صدا زده می‌شود"""
        asyncio.ensure_future(self._popup_result(req_id, ok, detail))

    async def _popup_result(self, req_id, ok, detail):
        paths = detail.get("paths") or []
        if ok and detail.get("picked_files"):
            for f in detail["picked_files"]:
                self.selected_for_send.append((f.get("name", ""), paths and self._match_path(f.get("name"), paths) or f.get("name", "")))
        stream_to_start = detail.pop("_start_stream", None)
        await self.send_json({"type": "approve", "req_id": req_id, "ok": ok, "detail": detail})
        if ok and stream_to_start:
            await self._start_capture(int(stream_to_start), detail.get("_quality", "medium"))

    @staticmethod
    def _match_path(name, paths):
        for p in paths:
            if os.path.basename(p) == name:
                return p
        return paths[0] if paths else name

    # ---------- capture / viewer ----------
    async def _start_capture(self, stream_id, quality):
        if stream_id in self.captures:
            return
        if not (HAS_MSS or self.fake_capture):
            await self.send_json({"type": "screen_status", "stream_id": stream_id, "active": False,
                                  "reason": "no capture backend (mss نصب نیست)"})
            return
        if not self.fake_capture and os.name != "nt" and not os.environ.get("DISPLAY"):
            await self.send_json({"type": "screen_status", "stream_id": stream_id, "active": False,
                                  "reason": "no DISPLAY (X11 لازم است)"})
            return
        self.captures[stream_id] = CaptureSession(self, stream_id, quality)
        await self.send_json({"type": "screen_status", "stream_id": stream_id, "active": True})

    async def _open_share_viewer(self, stream_id):
        if self.gui:
            v = ViewerWindow(self, stream_id)
            self.share_viewers[stream_id] = v
            await self.send_json({"type": "console_share_ack", "stream_id": stream_id, "ok": True})
        else:
            log.info("share viewer headless — frames counted only")
            await self.send_json({"type": "console_share_ack", "stream_id": stream_id, "ok": True,
                                  "reason": "headless"})

    # ---------- incoming file (c2a) ----------
    async def _begin_incoming(self, tid, name, size):
        name = safe_name(name)
        save_dir = self.config.data["save_dir"]
        path = unique_path(os.path.join(save_dir, name))
        fh = open(path, "wb")
        self.incoming[tid] = {"fh": fh, "path": path, "name": name, "size": size, "bytes": 0}
        log.info("receiving: %s (%d bytes) → %s", name, size, path)


def parse_args():
    ap = argparse.ArgumentParser(description="LocalTools Agent (Windows/Linux)")
    ap.add_argument("--server", help="نشانی سرور مثل ws://192.168.1.10:8788/ws")
    ap.add_argument("--code", help="کد دسترسی سرور")
    ap.add_argument("--config", help="مسیر فایل کانفیگ (پیش‌فرض agent.json کنار اسکریپت)")
    ap.add_argument("--save-dir", help="پوشه ذخیره فایل‌های دریافتی")
    ap.add_argument("--monitor", type=int, default=0,
                    help="شماره مانیتور برای کپچر (0=همه، 1=اولین، …)")
    ap.add_argument("--no-gui", action="store_true", help="بدون پاپ‌آپ/پنجره (headless)")
    ap.add_argument("--auto-approve", action="store_true",
                    help="همه درخواست‌ها خودکار تایید می‌شوند (فقط برای تست!)")
    ap.add_argument("--auto-file", help="فایل پیش‌فرض برای request_files در حالت auto-approve")
    ap.add_argument("--fake-capture", action="store_true", help="کپچر مصنوعی به‌جای صفحه واقعی (تست)")
    ap.add_argument("--log-level", default="INFO")
    return ap.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    agent = Agent(args)
    print("=" * 58)
    print(f"LocalTools Agent v{VERSION} — {socket.gethostname()} ({platform.system()})")
    print(f"  سرور       : {agent.config.data['server']}")
    print(f"  ذخیره فایل : {agent.config.data['save_dir']}")
    print(f"  کپچر       : {'مصنوعی (fake)' if agent.fake_capture else ('mss ✓' if HAS_MSS else '✗')}")
    print(f"  کنترل      : {'pynput ✓' if agent.input.available else '✗'}")
    print(f"  پاپ‌آپ GUI : {'✓' if agent.gui else '✗'}")
    if agent.auto_approve:
        print("  ⚠ حالت auto-approve: همه درخواست‌ها بدون تایید قبول می‌شوند (فقط تست)")
    print("=" * 58)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\nخروج.")


if __name__ == "__main__":
    main()
