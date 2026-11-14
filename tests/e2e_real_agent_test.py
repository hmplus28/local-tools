#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تست E2E با ایجنت واقعی (agent.py) روی سرور مجازی Xvfb

  • کپچر واقعی صفحه با mss (داخل Xvfb)
  • کنترل واقعی موس با pynput → بررسی جابه‌جایی نشانگر X
  • ذخیره واقعی فایل دریافتی در save-dir و مطابقت SHA256
  • ارسال واقعی فایل انتخابی (--auto-file) با چانک‌های باینری
  • فریم‌های JPEG واقعی (decode با PIL) + کنترل دو مرحله‌ای
  • ری‌استارت ایجنت با همان client_id (پایداری شناسه)

اجرا: python tests/e2e_real_agent_test.py
"""
import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from websockets.asyncio.client import connect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18790
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}/ws"
CODE = "test123"
BIN_SCREEN, BIN_FILE = 1, 2
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_real")
os.makedirs(TMP, exist_ok=True)

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f" — {extra}" if extra and not cond else ""))


def build_binary(btype, stream_id, meta, payload=b""):
    m = json.dumps(meta).encode()
    return bytes([btype]) + stream_id.to_bytes(4, "big") + len(m).to_bytes(4, "big") + m + payload


def parse_binary(data):
    btype = data[0]
    sid = int.from_bytes(data[1:5], "big")
    mlen = int.from_bytes(data[5:9], "big")
    meta = json.loads(data[9:9 + mlen].decode())
    return btype, sid, meta, data[9 + mlen:]


class Conn:
    def __init__(self):
        self.ws = None
        self.text_q = asyncio.Queue()
        self.bin_q = asyncio.Queue()

    async def start(self, url, headers=None):
        self.ws = await connect(url, max_size=8 * 1024 * 1024, additional_headers=headers or {})
        asyncio.ensure_future(self._reader())

    async def _reader(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, str):
                    await self.text_q.put(json.loads(msg))
                else:
                    await self.bin_q.put(msg)
        except Exception:
            pass

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def send_bin(self, d):
        await self.ws.send(d)

    async def recv_json(self, mtype=None, timeout=15):
        while True:
            msg = await asyncio.wait_for(self.text_q.get(), timeout)
            if mtype is None or msg.get("type") == mtype:
                return msg

    async def recv_bin(self, timeout=15):
        return await asyncio.wait_for(self.bin_q.get(), timeout)


async def wait_for(cond_fn, timeout=8, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond_fn():
            return True
        await asyncio.sleep(step)
    return False


async def main():
    # ---------- Xvfb ----------
    xvfb = None
    if shutil.which("Xvfb"):
        xvfb = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x800x24"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        print("== Xvfb :99 اجرا شد ==")
    agent_env = dict(os.environ, DISPLAY=":99", PYTHONUNBUFFERED="1")

    # ---------- server ----------
    srv_log = open(os.path.join(TMP, "server.log"), "wb")
    srv = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "server.py"), "--port", str(PORT), "--code", CODE],
        env=dict(os.environ, LT_REQUEST_TIMEOUT="20"), stdout=srv_log, stderr=srv_log)
    for _ in range(50):
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=1) as r:
                if json.load(r)["ok"]:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        print("server did not start"); sys.exit(1)
    print("== سرور اجرا شد ==")

    # ---------- agent files ----------
    save_dir = os.path.join(TMP, "saveA")
    shutil.rmtree(save_dir, ignore_errors=True)
    os.makedirs(save_dir, exist_ok=True)
    cfg_a = os.path.join(TMP, "agentA.json")
    sample_path = os.path.join(TMP, "sample.bin")
    sdata = os.urandom(480 * 1024)
    with open(sample_path, "wb") as f:
        f.write(sdata)
    sample_hash = hashlib.sha256(sdata).hexdigest()

    # فایل محلی برای ارسال کنسول→ایجنت
    send_path = os.path.join(TMP, "outgoing.bin")
    odata = os.urandom(600 * 1024)
    with open(send_path, "wb") as f:
        f.write(odata)
    send_hash = hashlib.sha256(odata).hexdigest()

    agent_log = open(os.path.join(TMP, "agent.log"), "wb")

    async def start_agent():
        return subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "agent", "agent.py"),
             "--server", WS, "--code", CODE,
             "--config", cfg_a, "--save-dir", save_dir,
             "--auto-approve", "--auto-file", sample_path],
            env=agent_env, stdout=agent_log, stderr=agent_log)

    agent = await start_agent()

    # ---------- console ----------
    cj_txt = ""
    try:
        import http.cookiejar
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.open(BASE + "/login_auth",
                    data=urllib.parse.urlencode({"password": CODE}).encode(), timeout=5)
        cj_txt = "; ".join(f"{c.name}={c.value}" for c in cj)
    except Exception as e:
        print("login failed", e); sys.exit(1)

    console = Conn()
    await console.start(WS + "?role=console", headers={"Cookie": cj_txt})

    try:
        # ---------- 1. presence ----------
        snap = None
        deadline = time.time() + 15
        while time.time() < deadline:
            await console.send({"type": "list_clients"})
            snap = await console.recv_json("clients")
            if snap["clients"]:
                break
            await asyncio.sleep(0.3)
        check("1. ایجنت واقعی در لیست ظاهر شد", len(snap["clients"]) == 1)
        a = snap["clients"][0]
        client_id = a["id"]
        info = a["info"]
        check("1b. capabilities درست (کپچر+کنترل+GUI)", a["online"] and
              info["capabilities"].get("capture") is True and
              info["capabilities"].get("input") is True and
              info["capabilities"].get("gui") is True)
        check("1c. hostname و OS درست", info["hostname"] and info["os"] == "Linux")

        # ---------- 2. send file (real save) ----------
        await console.send({"type": "send_files", "target": client_id,
                            "files": [{"name": "outgoing.bin", "size": len(odata)}]})
        ta = await console.recv_json("transfers_approved", timeout=15)
        tid = ta["transfers"][0]["transfer_id"]
        await console.send({"type": "transfer_ready", "transfer_id": tid})
        seq = 0
        with open(send_path, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                seq += 1
                await console.send_bin(build_binary(BIN_FILE, 0,
                                                    {"transfer_id": tid, "seq": seq, "last": f.tell() >= len(odata)}, chunk))
                await asyncio.sleep(0.005)
        await console.recv_json("transfer_done", timeout=15)
        got_path = os.path.join(save_dir, "outgoing.bin")
        ok2 = await wait_for(lambda: os.path.exists(got_path) and os.path.getsize(got_path) == len(odata))
        check("2. فایل واقعاً در save-dir ذخیره شد", ok2, str(os.listdir(save_dir)))
        if ok2:
            check("2b. محتوای ذخیره‌شده سالم (SHA256)",
                  hashlib.sha256(open(got_path, "rb").read()).hexdigest() == send_hash)

        # ---------- 3. request file (real --auto-file) ----------
        await console.send({"type": "request_files", "target": client_id})
        fi = await console.recv_json("file_incoming", timeout=15)
        await console.send({"type": "transfer_ready", "transfer_id": fi["transfer_id"]})
        parts = []
        while True:
            bt, _, meta, payload = parse_binary(await console.recv_bin(timeout=15))
            if bt == BIN_FILE and meta["transfer_id"] == fi["transfer_id"]:
                parts.append(payload)
                if meta.get("last"):
                    break
        await console.recv_json("transfer_done", timeout=15)
        check("3. فایل درخواستی واقعاً ارسال شد (SHA256)",
              hashlib.sha256(b"".join(parts)).hexdigest() == sample_hash)

        # ---------- 4. real screen capture (mss on Xvfb) ----------
        from PIL import Image
        await console.send({"type": "request_screen", "target": client_id, "quality": "high"})
        rr = await console.recv_json("request_result", timeout=15)
        check("4-pre. اشتراک صفحه تایید شد", rr["ok"] is True)
        st = await console.recv_json("screen_started", timeout=15)
        sid = st["stream_id"]
        frames = []
        for _ in range(8):
            bt, fsid, meta, payload = parse_binary(await console.recv_bin(timeout=15))
            frames.append((fsid, meta, payload))
        check("4a. فریم‌های واقعی mss رسیدند", len(frames) >= 5)
        check("4b. ابعاد = رزولوشن Xvfb (1280x800)",
              frames[0][1]["w"] == 1280 and frames[0][1]["h"] == 800,
              str(frames[0][1]))
        img = Image.open(io.BytesIO(frames[0][2]))
        check("4c. payload یک JPEG واقعی قابل decode است", img.format == "JPEG" and img.size == (1280, 800))
        check("4d. موقعیت نشانگر واقعی در meta هست", "cx" in frames[0][1] and "cy" in frames[0][1])

        # ---------- 5. real mouse control via pynput ----------
        await console.send({"type": "request_control", "target": client_id, "stream_id": sid})
        rr = await console.recv_json("request_result", timeout=15)
        await console.recv_json("control_granted", timeout=15)
        check("5-pre. کنترل موس تایید و فعال شد", rr["ok"] is True)
        os.environ["DISPLAY"] = ":99"
        try:
            import pynput
            probe = pynput.mouse.Controller()
            probe.position = (10, 10)
            await asyncio.sleep(0.2)
            await console.send({"type": "input", "stream_id": sid,
                                "ev": {"kind": "mouse_move", "x": 900, "y": 350}})
            await console.send({"type": "input", "stream_id": sid,
                                "ev": {"kind": "mouse_btn", "btn": "left", "pressed": True}})
            await console.send({"type": "input", "stream_id": sid,
                                "ev": {"kind": "mouse_btn", "btn": "left", "pressed": False}})
            await console.send({"type": "input", "stream_id": sid,
                                "ev": {"kind": "key", "key": "l", "pressed": True}})
            await console.send({"type": "input", "stream_id": sid,
                                "ev": {"kind": "key", "key": "l", "pressed": False}})
            await console.send({"type": "input", "stream_id": sid,
                                "ev": {"kind": "wheel", "dy": 120}})
            moved = False
            deadline = time.time() + 5
            while time.time() < deadline:
                x, y = probe.position
                if abs(x - 900) < 30 and abs(y - 350) < 30:
                    moved = True
                    break
                await asyncio.sleep(0.1)
            check("5a. موس واقعاً در X حرکت کرد (900,350)", moved, str(probe.position))
        except Exception as e:
            check("5a. موس واقعاً در X حرکت کرد (900,350)", False, f"pynput error: {e}")

        await console.send({"type": "release_control", "stream_id": sid})
        await console.send({"type": "stop_view", "stream_id": sid})

        # ---------- 6. share my screen console→agent (viewer window) ----------
        await console.send({"type": "share_screen_to", "target": client_id})
        rr = await console.recv_json("request_result", timeout=15)
        ss = await console.recv_json("share_screen_started", timeout=15)
        ssid = ss["stream_id"]
        acked = False
        # یک JPEG واقعی کوچک برای پنجره نمایش ایجنت
        from PIL import Image as _Img
        import io as _io
        _buf = _io.BytesIO()
        _Img.new("RGB", (640, 400), (30, 40, 90)).save(_buf, "JPEG", quality=70)
        _jpg = _buf.getvalue()
        for i in range(4):
            await console.send_bin(build_binary(BIN_SCREEN, ssid, {"w": 640, "h": 400, "cx": -1, "cy": -1}, _jpg))
            await asyncio.sleep(0.15)
        # ack از ایجنت (پنجره tkinter در Xvfb باز می‌شود)
        try:
            while True:
                m = await console.recv_json(timeout=6)
                if m.get("type") == "share_screen_ack":
                    acked = m.get("ok") is True
                    break
                if m.get("type") == "share_ended":
                    break
        except asyncio.TimeoutError:
            pass
        check("6. ایجنت پنجره نمایش را باز کرد و ack داد", acked)
        await console.send({"type": "stop_share_to", "stream_id": ssid})

        # ---------- 7. restart با همان client_id ----------
        agent.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.to_thread(agent.wait, 8), 10)
        agent2 = await start_agent()
        snap2 = None
        deadline = time.time() + 15
        while time.time() < deadline:
            await console.send({"type": "list_clients"})
            snap2 = await console.recv_json("clients")
            if any(c["online"] and c["id"] == client_id for c in snap2["clients"]):
                break
            await asyncio.sleep(0.3)
        check("7. بعد از ری‌استارت، همان client_id آنلاین شد", any(
            c["online"] and c["id"] == client_id for c in snap2["clients"]))

        agent2.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.to_thread(agent2.wait, 8), 10)

    finally:
        with contextlib.suppress(Exception):
            agent.terminate()
        srv.send_signal(signal.SIGINT)
        with contextlib.suppress(Exception):
            srv.wait(5)
        if xvfb:
            xvfb.terminate()

    print("\n" + "=" * 60)
    print(f"نتیجه: {len(PASS)} موفق / {len(FAIL)} ناموفق")
    if FAIL:
        for f in FAIL:
            print("  ❌", f)
        print("\n--- agent.log tail:")
        print(open(os.path.join(TMP, "agent.log")).read()[-1500:])
        sys.exit(1)
    print("همه سناریوهای ایجنت واقعی سبز بودند ✅")


if __name__ == "__main__":
    asyncio.run(main())
