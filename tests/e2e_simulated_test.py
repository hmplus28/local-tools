#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تست E2E شبیه‌سازی‌شده — سرور واقعی + کلاینت‌های شبیه‌سازی‌شده (ایجنت/کنسول)

سناریوها:
  1.  /health
  2.  احراز هویت: ایجنت با کد اشتباه / کنسول بدون کوکی → رد؛ کنسول با لاگین → قبول
  3.  لیست کلاینت‌ها (ثبت‌نام دو ایجنت)
  4.  ارسال فایل کنسول→ایجنت (تایید + انتقال + مطابقت SHA256 + transfer_done)
  5.  رد شدن ارسال فایل توسط ایجنت (بدون تایید، انتقال شروع نمی‌شود)
  6.  درخواست فایل از ایجنت (انتخاب + ارسال + مطابقت SHA256)
  7.  اشتراک صفحه ایجنت→کنسول (فریم‌های باینری با meta صحیح)
  8.  کنترل موس/کیبورد (تایید + عبور رخدادها) و سپس release (قطع عبور رخداد)
  9.  stop_view → توقف کپچر
  10. اشتراک صفحه کنسول→ایجنت + پایان
  11. بی‌پاسخی ایجنت → request_result(timeout)

سرور با LT_REQUEST_TIMEOUT=5 اجرا می‌شود تا تست سریع باشد.
"""
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import websockets
from websockets.asyncio.client import connect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18788
BASE = f"http://127.0.0.1:{PORT}"
WS = f"ws://127.0.0.1:{PORT}/ws"
CODE = "test123"
BIN_SCREEN, BIN_FILE = 1, 2
MAX_MSG = 8 * 1024 * 1024
CHUNK = 256 * 1024
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
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


def make_test_file(path, size=700 * 1024):
    data = os.urandom(size)
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest(), size


class Conn:
    def __init__(self):
        self.ws = None
        self.text_q = asyncio.Queue()
        self.bin_q = asyncio.Queue()

    async def start(self, url, headers=None):
        self.ws = await connect(url, max_size=MAX_MSG, additional_headers=headers or {})
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

    async def send_bin(self, data):
        await self.ws.send(data)

    async def recv_json(self, mtype=None, timeout=10):
        while True:
            msg = await asyncio.wait_for(self.text_q.get(), timeout)
            if mtype is None or msg.get("type") == mtype:
                return msg

    async def recv_bin(self, timeout=10):
        return await asyncio.wait_for(self.bin_q.get(), timeout)

    async def close(self):
        with contextlib.suppress(Exception):
            await self.ws.close()


class FakeAgent:
    """ایجنت شبیه‌سازی‌شده — رفتار auto-approve با سه حالت:
    reject=False → تایید | reject=True → رد | reject=None → بی‌پاسخ"""

    def __init__(self, client_id, hostname):
        self.cid = client_id
        self.hostname = hostname
        self.reject = False
        self.conn = Conn()
        self.received_file_path = os.path.join(TMP, f"recv_{client_id}.bin")
        self.file_to_offer = os.path.join(TMP, f"offer_{client_id}.bin")
        self.approval_requests = []
        self.received_input_events = []
        self.control_states = []
        self.frames_from_console = 0
        self.share_ended = asyncio.Event()
        self.capture_active = False
        self._fh = None

    async def start(self):
        await self.conn.start(WS + f"?role=agent&code={CODE}")
        await self.conn.send({
            "type": "register", "client_id": self.cid,
            "info": {"hostname": self.hostname, "os": "Linux", "os_version": "test",
                     "user": "tester", "ip": "127.0.0.1", "version": "1.0.0",
                     "capabilities": {"capture": True, "input": True, "gui": False},
                     "monitors": 1}})

    async def loop(self, stop_evt):
        while not stop_evt.is_set():
            got = False
            # پیام‌های JSON
            try:
                while True:
                    msg = self.conn.text_q.get_nowait()
                    got = True
                    await self._on_json(msg)
            except asyncio.QueueEmpty:
                pass
            # پیام‌های باینری
            try:
                while True:
                    raw = self.conn.bin_q.get_nowait()
                    got = True
                    self._on_bin(raw)
            except asyncio.QueueEmpty:
                pass
            if not got:
                await asyncio.sleep(0.05)

    async def _on_json(self, msg):
        t = msg.get("type")
        if t == "approval_request":
            self.approval_requests.append(msg)
            await self._on_approval(msg)
        elif t == "transfer_begin":
            self._fh = open(self.received_file_path, "wb")
        elif t == "transfer_ready":
            asyncio.ensure_future(self._send_file(msg))
        elif t == "input":
            self.received_input_events.append(msg.get("ev"))
        elif t == "control_state":
            self.control_states.append(msg.get("granted"))
        elif t == "share_screen_started":
            await self.conn.send({"type": "console_share_ack", "stream_id": msg["stream_id"], "ok": True})
        elif t == "share_screen_stopped":
            self.share_ended.set()
        elif t == "stop_capture":
            self.capture_active = False

    def _on_bin(self, raw):
        btype, sid, meta, payload = parse_binary(raw)
        if btype == BIN_FILE and self._fh is not None:
            self._fh.write(payload)
            if meta.get("last"):
                self._fh.close()
                self._fh = None
                asyncio.ensure_future(self.conn.send({"type": "transfer_done", "transfer_id": meta["transfer_id"]}))
        elif btype == BIN_SCREEN:
            self.frames_from_console += 1

    async def _on_approval(self, msg):
        if self.reject is True:
            await self.conn.send({"type": "approve", "req_id": msg["req_id"], "ok": False, "detail": {}})
            return
        if self.reject is None:
            return  # بی‌پاسخ
        action = msg["action"]
        payload = msg.get("payload", {})
        if action == "request_files":
            size = os.path.getsize(self.file_to_offer)
            await self.conn.send({"type": "approve", "req_id": msg["req_id"], "ok": True,
                                  "detail": {"picked_files": [{"name": os.path.basename(self.file_to_offer), "size": size}]}})
            return
        await self.conn.send({"type": "approve", "req_id": msg["req_id"], "ok": True, "detail": {}})
        if action == "screen_share":
            sid = payload["stream_id"]
            self.capture_active = True
            await self.conn.send({"type": "screen_status", "stream_id": sid, "active": True})
            asyncio.ensure_future(self._send_frames(sid))

    async def _send_frames(self, sid):
        for i in range(6):
            if not self.capture_active:
                return
            img = bytes([i % 256]) * 2048
            await self.conn.send_bin(build_binary(BIN_SCREEN, sid, {"w": 1280, "h": 720, "cx": i * 10, "cy": 300}, img))
            await asyncio.sleep(0.05)

    async def _send_file(self, msg):
        tid = msg["transfer_id"]
        size = os.path.getsize(self.file_to_offer)
        with open(self.file_to_offer, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                await self.conn.send_bin(build_binary(BIN_FILE, 0,
                                                      {"transfer_id": tid, "seq": 0, "last": f.tell() >= size}, chunk))
        await self.conn.send({"type": "transfer_done", "transfer_id": tid})


async def wait_for(cond_fn, timeout=5, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond_fn():
            return True
        await asyncio.sleep(step)
    return False


async def main():
    env = dict(os.environ, LT_REQUEST_TIMEOUT="5")
    srv_log = open(os.path.join(TMP, "server.log"), "wb")
    srv = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "server.py"),
         "--port", str(PORT), "--code", CODE],
        env=env, stdout=srv_log, stderr=srv_log)
    for _ in range(50):
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=1) as r:
                if json.load(r)["ok"]:
                    break
        except Exception:
            time.sleep(0.2)
    else:
        print("server did not start — log:")
        try:
            print(open(os.path.join(TMP, "server.log")).read()[-2000:])
        except Exception as e:
            print("  (log read failed)", e)
        srv.kill()
        sys.exit(1)
    print("== سرور اجرا شد ==")

    try:
        # ---------- 1. health ----------
        with urllib.request.urlopen(BASE + "/health") as r:
            h = json.load(r)
        check("1. /health پاسخ می‌دهد", h["ok"] is True)

        # ---------- 2. auth ----------
        bad = Conn()
        ok2 = False
        try:
            await bad.start(WS + "?role=agent&code=WRONG")
            m = await bad.recv_json(timeout=5)
            ok2 = m.get("code") == "auth_failed"
        except Exception:
            ok2 = True
        check("2a. ایجنت با کد اشتباه رد می‌شود", ok2)

        noauth = Conn()
        ok2b = False
        try:
            await noauth.start(WS + "?role=console")
            m = await noauth.recv_json(timeout=5)
            ok2b = m.get("code") == "auth_failed"
        except Exception:
            ok2b = True
        check("2b. کنسول بدون کوکی رد می‌شود", ok2b)

        import http.cookiejar
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        data = urllib.parse.urlencode({"password": CODE}).encode()
        opener.open(BASE + "/login_auth", data=data, timeout=5)
        cookie = "; ".join(f"{c.name}={c.value}" for c in cj)
        check("2c. لاگین کنسول با کد صحیح موفق", "lt_session=" in cookie)

        console = Conn()
        await console.start(WS + "?role=console", headers={"Cookie": cookie})
        await console.send({"type": "list_clients"})
        empty = await console.recv_json("clients")
        check("2d. کنسول احرازشده وصل می‌شود", isinstance(empty.get("clients"), list))

        # ---------- agents ----------
        agentA = FakeAgent("agent-a", "PC-A")
        agentB = FakeAgent("agent-b", "PC-B")
        a_hash, a_size = make_test_file(agentA.file_to_offer)
        b_hash, b_size = make_test_file(agentB.file_to_offer)
        await agentA.start()
        await agentB.start()
        stop_evt = asyncio.Event()
        tA = asyncio.ensure_future(agentA.loop(stop_evt))
        tB = asyncio.ensure_future(agentB.loop(stop_evt))

        # صبر تا ثبت‌نام هر دو (جلوگیری از race با snapshot اولیه)
        snap = None
        deadline = time.time() + 10
        while time.time() < deadline:
            await console.send({"type": "list_clients"})
            snap = await console.recv_json("clients")
            ids = {c["id"] for c in snap["clients"]}
            if {"agent-a", "agent-b"} <= ids:
                break
            await asyncio.sleep(0.1)
        onlines = {c["id"] for c in snap["clients"] if c["online"]}
        check("3. لیست کلاینت‌ها هر دو ایجنت را نشان می‌دهد", {"agent-a", "agent-b"} <= ids)
        check("3b. هر دو آنلاین هستند", {"agent-a", "agent-b"} <= onlines)

        # ---------- 4. send file console→A ----------
        send_hash, send_size = make_test_file(os.path.join(TMP, "to_send.bin"))
        await console.send({"type": "send_files", "target": "agent-a",
                            "files": [{"name": "report.bin", "size": send_size}]})
        ok4a = await wait_for(lambda: len(agentA.approval_requests) == 1)
        check("4a. پاپ‌آپ approval_request به ایجنت می‌رسد",
              ok4a and agentA.approval_requests[0]["action"] == "send_files")
        ta = await console.recv_json("transfers_approved", timeout=10)
        tid = ta["transfers"][0]["transfer_id"]
        check("4b. تایید شد و transfers_approved آمد", tid > 0)
        await console.send({"type": "transfer_ready", "transfer_id": tid})
        with open(os.path.join(TMP, "to_send.bin"), "rb") as f:
            data = f.read()
        seq = 0
        for off in range(0, len(data), CHUNK):
            chunk = data[off:off + CHUNK]
            seq += 1
            await console.send_bin(build_binary(BIN_FILE, 0, {"transfer_id": tid, "seq": seq,
                                                              "last": off + len(chunk) >= len(data)}, chunk))
            await asyncio.sleep(0.005)
        done = await console.recv_json("transfer_done", timeout=15)
        check("4c. ایجنت فایل را کامل گرفت و transfer_done فرستاد", done["transfer_id"] == tid)
        recv_hash = hashlib.sha256(open(agentA.received_file_path, "rb").read()).hexdigest()
        check("4d. محتوای فایل سالم است (SHA256)", recv_hash == send_hash)

        # ---------- 5. rejection ----------
        agentA.reject = True
        await console.send({"type": "send_files", "target": "agent-a",
                            "files": [{"name": "no.bin", "size": 10}]})
        res = await console.recv_json("request_result", timeout=10)
        check("5. رد شدن توسط کلاینت → انتقال شروع نمی‌شود",
              res["action"] == "send_files" and res["ok"] is False and res["reason"] == "rejected")
        agentA.reject = False

        # ---------- 6. request files from B ----------
        await console.send({"type": "request_files", "target": "agent-b"})
        incoming = await console.recv_json("file_incoming", timeout=10)
        itid = incoming["transfer_id"]
        check("6a. file_incoming پس از تایید آمد", incoming["size"] == b_size)
        await console.send({"type": "transfer_ready", "transfer_id": itid})
        parts = []
        while True:
            bt, sid2, meta, payload = parse_binary(await console.recv_bin(timeout=10))
            if bt == BIN_FILE and meta["transfer_id"] == itid:
                parts.append(payload)
                if meta.get("last"):
                    break
        await console.recv_json("transfer_done", timeout=10)
        got = hashlib.sha256(b"".join(parts)).hexdigest()
        check("6b. فایل درخواستی سالم رسید (SHA256)", got == b_hash)

        # ---------- 7. screen share A→console ----------
        await console.send({"type": "request_screen", "target": "agent-a", "quality": "medium"})
        rr = await console.recv_json("request_result", timeout=10)
        check("7-pre. request_result(ok) برای اشتراک صفحه", rr["ok"] is True)
        started = await console.recv_json("screen_started", timeout=10)
        sid = started["stream_id"]
        frames = []
        for _ in range(6):
            bt, fsid, meta, payload = parse_binary(await console.recv_bin(timeout=10))
            frames.append((fsid, meta, len(payload)))
        check("7a. فریم‌های صفحه رسیدند", len(frames) >= 5)
        check("7b. stream_id فریم‌ها درست است", all(f[0] == sid for f in frames))
        check("7c. meta ابعاد و نشانگر دارد", frames[0][1].get("w") == 1280 and "cx" in frames[0][1])

        # ---------- 8. control ----------
        await console.send({"type": "request_control", "target": "agent-a", "stream_id": sid})
        rr = await console.recv_json("request_result", timeout=10)
        cg = await console.recv_json("control_granted", timeout=10)
        check("8-pre. control_granted", rr["ok"] is True and cg["stream_id"] == sid)
        await console.send({"type": "input", "stream_id": sid,
                            "ev": {"kind": "key", "key": "a", "pressed": True}})
        await console.send({"type": "input", "stream_id": sid,
                            "ev": {"kind": "mouse_move", "x": 100, "y": 200}})
        ok8a = await wait_for(lambda: {"kind": "key", "key": "a", "pressed": True} in agentA.received_input_events
                              and {"kind": "mouse_move", "x": 100, "y": 200} in agentA.received_input_events)
        check("8a. رخدادهای کنترل به ایجنت رسیدند", ok8a)
        await console.send({"type": "release_control", "stream_id": sid})
        ok8b = await wait_for(lambda: False in agentA.control_states)
        check("8b. release → control_state(False) به ایجنت", ok8b)
        n_before = len(agentA.received_input_events)
        await console.send({"type": "input", "stream_id": sid, "ev": {"kind": "key", "key": "x", "pressed": True}})
        await asyncio.sleep(0.5)
        check("8c. بعد از release دیگر رخداد عبور نمی‌کند",
              len(agentA.received_input_events) == n_before)

        # ---------- 9. stop view ----------
        await console.send({"type": "stop_view", "stream_id": sid})
        ok9 = await wait_for(lambda: agentA.capture_active is False)
        check("9. بعد از stop_view کپچر ایجنت متوقف شد", ok9)

        # ---------- 10. share my screen console→B ----------
        await console.send({"type": "share_screen_to", "target": "agent-b"})
        rr = await console.recv_json("request_result", timeout=10)
        ss = await console.recv_json("share_screen_started", timeout=10)
        ssid = ss["stream_id"]
        check("10-pre. share_screen_started", rr["ok"] is True and ssid > 0)
        for i in range(3):
            await console.send_bin(build_binary(BIN_SCREEN, ssid, {"w": 640, "h": 400, "cx": -1, "cy": -1},
                                                b"frame" + bytes([i])))
            await asyncio.sleep(0.05)
        ok10a = await wait_for(lambda: agentB.frames_from_console >= 3)
        check("10a. کلاینت فریم‌های صفحه کنسول را گرفت", ok10a)
        await console.send({"type": "stop_share_to", "stream_id": ssid})
        await asyncio.wait_for(agentB.share_ended.wait(), 5)
        check("10b. پایان اشتراک به کلاینت رسید", agentB.share_ended.is_set())

        # ---------- 11. timeout ----------
        n_reqs = len(agentB.approval_requests)
        agentB.reject = None  # بی‌پاسخ
        await console.send({"type": "send_files", "target": "agent-b",
                            "files": [{"name": "x.bin", "size": 1}]})
        await wait_for(lambda: len(agentB.approval_requests) > n_reqs)
        res = await console.recv_json("request_result", timeout=15)
        check("11. بی‌پاسخی کلاینت → timeout", res["ok"] is False and res["reason"] == "timeout")
        agentB.reject = False

        # ---------- cleanup ----------
        stop_evt.set()
        await asyncio.gather(tA, tB, return_exceptions=True)
        await console.close()
        await agentA.conn.close()
        await agentB.conn.close()

    finally:
        srv.send_signal(signal.SIGINT)
        with contextlib.suppress(Exception):
            srv.wait(5)

    print("\n" + "=" * 60)
    print(f"نتیجه: {len(PASS)} موفق / {len(FAIL)} ناموفق")
    if FAIL:
        for f in FAIL:
            print("  ❌", f)
        sys.exit(1)
    print("همه سناریوها سبز بودند ✅")


if __name__ == "__main__":
    asyncio.run(main())
