#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocalTools Hub — سرور مرکزی ابزار شرکت (ارسال فایل + اشتراک صفحه + کنترل از راه دور)

همه‌چیز از یک پورت عبور می‌کند:
  - HTTP : صفحه ورود + کنسول وب (استاتیک) + /health
  - WS   : /ws  (role=agent | role=console)

بر اساس طراحی file_shareV3 (پالت و مدل PIN) اما با معماری Realtime.
Protocol contract: docs/PROTOCOL.md
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import ssl
import subprocess
import sys
import time

from aiohttp import web, WSMsgType

VERSION = "1.0.0"
BIN_SCREEN_FRAME = 0x01
BIN_FILE_CHUNK = 0x02
REQUEST_TIMEOUT = int(os.environ.get("LT_REQUEST_TIMEOUT", "60"))  # ثانیه — مهلت پاسخ پاپ‌آپ تایید
HEARTBEAT_INTERVAL = 10       # ثانیه — از سمت ایجنت
OFFLINE_AFTER = 35            # ثانیه بدون heartbeat → offline
MAX_FILES_PER_OFFER = 200
MAX_MSG = 8 * 1024 * 1024     # حداکثر اندازه پیام WS (چانک 256KB + فریم)

log = logging.getLogger("localtools")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_binary(data: bytes):
    """[1B type][4B stream_id][4B meta_len][meta json][payload]"""
    if len(data) < 9:
        return None
    btype = data[0]
    stream_id = int.from_bytes(data[1:5], "big")
    meta_len = int.from_bytes(data[5:9], "big")
    if len(data) < 9 + meta_len:
        return None
    try:
        meta = json.loads(data[9:9 + meta_len].decode("utf-8"))
    except Exception:
        return None
    return btype, stream_id, meta, data[9 + meta_len:]


def build_binary(btype: int, stream_id: int, meta: dict, payload: bytes = b"") -> bytes:
    m = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    return bytes([btype]) + stream_id.to_bytes(4, "big") + len(m).to_bytes(4, "big") + m + payload


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
# State objects
# ---------------------------------------------------------------------------
class AgentConn:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.ws = None
        self.info = {}
        self.online = False
        self.last_seen = time.time()


class Stream:
    def __init__(self, sid: int, kind: str, agent_id: str, console_ws, meta: dict):
        self.id = sid
        self.kind = kind            # 'agent_view' (کلاینت اشتراک می‌دهد) | 'console_view' (کنسول اشتراک می‌دهد)
        self.agent_id = agent_id    # کلاینت هدف
        self.initiator = console_ws  # کنسول شروع‌کننده
        self.viewers = set()        # کنسول‌هایی که فریم‌های ایجنت را می‌بینند
        self.controller = None      # کنسولی که کنترل موس/کیبورد دارد
        self.active = False
        self.meta = meta


class Transfer:
    def __init__(self, tid: int, direction: str, name: str, size: int, console_ws, agent_id: str):
        self.id = tid
        self.direction = direction  # 'c2a' | 'a2c'
        self.name = name
        self.size = size
        self.console_ws = console_ws
        self.agent_id = agent_id
        self.bytes_done = 0


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
class Hub:
    def __init__(self, access_code=None):
        self.access_code = access_code
        self.sessions = set()          # توکن‌های معتبر کنسول وب
        self.agents = {}               # client_id -> AgentConn
        self.consoles = set()          # ws
        self.addr_by_ws = {}           # ws -> ip کلاینت
        self.streams = {}              # stream_id -> Stream
        self.transfers = {}            # transfer_id -> Transfer
        self.pending = {}              # req_id -> dict(action, console_ws, agent_id, task)
        self._ids = 1000

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    # ---------- snapshot ----------
    def clients_snapshot(self):
        now = time.time()
        out = []
        for cid, a in self.agents.items():
            out.append({
                "id": cid,
                "online": a.online and a.ws is not None,
                "last_seen": int(a.last_seen),
                "info": a.info or {},
            })
        return {"type": "clients", "clients": out, "server_time": int(now)}

    async def broadcast_clients(self):
        snap = self.clients_snapshot()
        for ws in list(self.consoles):
            asyncio.ensure_future(self.send_json(ws, snap))

    async def send_json(self, ws, obj):
        try:
            await ws.send_str(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    def schedule_json(self, ws, obj):
        asyncio.ensure_future(self.send_json(ws, obj))

    # ---------- approvals ----------
    def create_request(self, action, console_ws, agent_id, payload, on_approve):
        req_id = self.next_id()
        task = asyncio.ensure_future(self._request_timeout(req_id))
        self.pending[req_id] = {
            "action": action,
            "console_ws": console_ws,
            "agent_id": agent_id,
            "payload": payload,
            "on_approve": on_approve,
            "task": task,
        }
        return req_id

    async def _request_timeout(self, req_id):
        try:
            await asyncio.sleep(REQUEST_TIMEOUT)
        except asyncio.CancelledError:
            return
        req = self.pending.pop(req_id, None)
        if not req:
            return
        await self.send_json(req["console_ws"], {
            "type": "request_result", "req_id": req_id, "action": req["action"],
            "ok": False, "reason": "timeout", "payload": {},
        })
        agent = self.agents.get(req["agent_id"])
        if agent and agent.ws:
            self.schedule_json(agent.ws, {"type": "request_cancelled", "req_id": req_id, "reason": "timeout"})

    async def resolve_request(self, req_id, ok, detail, agent_ws):
        req = self.pending.pop(req_id, None)
        if not req:
            return
        req["task"].cancel()
        if ok and callable(req["on_approve"]):
            await req["on_approve"](detail or {})
        else:
            await self.send_json(req["console_ws"], {
                "type": "request_result", "req_id": req_id, "action": req["action"],
                "ok": False, "reason": "rejected" if not ok else "error", "payload": detail or {},
            })

    def agent_ws(self, agent_id):
        a = self.agents.get(agent_id)
        return a.ws if (a and a.online) else None

    def from_info(self, ws):
        return {"role": "console", "via": socket.gethostname(),
                "addr": self.addr_by_ws.get(ws, "?")}

    # ---------- console actions ----------
    async def console_send_files(self, ws, msg):
        target = msg.get("target", "")
        files = msg.get("files") or []
        if not isinstance(files, list) or not files or len(files) > MAX_FILES_PER_OFFER:
            await self.send_json(ws, {"type": "error", "code": "bad_request", "message": "files list invalid"})
            return
        clean = []
        for f in files[:MAX_FILES_PER_OFFER]:
            name = str(f.get("name", ""))[:300].replace("\\", "_").replace("/", "_").strip()
            size = int(f.get("size", 0))
            if name and size >= 0:
                clean.append({"name": name, "size": size})
        if not clean or self.agent_ws(target) is None:
            await self.send_json(ws, {"type": "error", "code": "target_offline", "message": "کلاینت آنلاین نیست"})
            return

        async def on_approve(detail):
            transfers = []
            for f in clean:
                tid = self.next_id()
                self.transfers[tid] = Transfer(tid, "c2a", f["name"], f["size"], ws, target)
                transfers.append({"transfer_id": tid, "name": f["name"], "size": f["size"]})
            await self.send_json(ws, {"type": "transfers_approved", "req_id": req_id,
                                      "target": target, "transfers": transfers})

        req_id = self.create_request("send_files", ws, target, {"files": clean}, on_approve)
        agent_ws = self.agent_ws(target)
        self.schedule_json(agent_ws, {
            "type": "approval_request", "req_id": req_id, "action": "send_files",
            "from": self.from_info(ws), "payload": {"files": clean},
        })

    async def console_request_files(self, ws, msg):
        target = msg.get("target", "")
        if self.agent_ws(target) is None:
            await self.send_json(ws, {"type": "error", "code": "target_offline", "message": "کلاینت آنلاین نیست"})
            return

        async def on_approve(detail):
            picked = detail.get("picked_files") or []
            if not picked:
                await self.send_json(ws, {"type": "request_result", "req_id": req_id, "action": "request_files",
                                          "ok": False, "reason": "no_file_selected", "payload": {}})
                return
            for f in picked[:MAX_FILES_PER_OFFER]:
                tid = self.next_id()
                self.transfers[tid] = Transfer(tid, "a2c", str(f.get("name", ""))[:300],
                                               int(f.get("size", 0)), ws, target)
                self.schedule_json(ws, {"type": "file_incoming", "transfer_id": tid,
                                        "name": self.transfers[tid].name, "size": self.transfers[tid].size,
                                        "from": self.from_info(ws)})

        req_id = self.create_request("request_files", ws, target, {}, on_approve)
        self.schedule_json(self.agent_ws(target), {
            "type": "approval_request", "req_id": req_id, "action": "request_files",
            "from": self.from_info(ws), "payload": {},
        })

    async def console_request_screen(self, ws, msg):
        target = msg.get("target", "")
        quality = msg.get("quality", "medium")
        if quality not in ("low", "medium", "high"):
            quality = "medium"
        agent = self.agent_ws(target)
        if agent is None:
            await self.send_json(ws, {"type": "error", "code": "target_offline", "message": "کلاینت آنلاین نیست"})
            return
        agent_conn = self.agents.get(target)
        if not (agent_conn.info.get("capabilities") or {}).get("capture", True):
            await self.send_json(ws, {"type": "error", "code": "no_capture",
                                      "message": "این کلاینت امکان کپچر صفحه ندارد"})
            return
        sid = self.next_id()
        stream = Stream(sid, "agent_view", target, ws, {"quality": quality})
        self.streams[sid] = stream

        async def on_approve(detail):
            stream.active = True
            stream.viewers.add(ws)
            await self.send_json(ws, {"type": "request_result", "req_id": req_id, "action": "request_screen",
                                      "ok": True, "reason": "", "payload": {"stream_id": sid}})
            await self.send_json(ws, {"type": "screen_started", "stream_id": sid, "target": stream_meta(self, target)})
            aw = self.agent_ws(target)
            if aw:
                self.schedule_json(aw, {"type": "viewers", "stream_id": sid, "count": len(stream.viewers)})

        req_id = self.create_request("request_screen", ws, target,
                                     {"stream_id": sid, "quality": quality}, on_approve)
        self.schedule_json(agent, {
            "type": "approval_request", "req_id": req_id, "action": "screen_share",
            "from": self.from_info(ws), "payload": {"stream_id": sid, "quality": quality},
        })

    async def console_request_control(self, ws, msg):
        sid = int(msg.get("stream_id", 0))
        stream = self.streams.get(sid)
        if not stream or stream.kind != "agent_view" or not stream.active:
            await self.send_json(ws, {"type": "error", "code": "no_stream", "message": "جلسه اشتراک صفحه فعال نیست"})
            return
        if stream.controller and stream.controller is not ws:
            await self.send_json(ws, {"type": "control_denied", "stream_id": sid, "reason": "busy"})
            return
        agent = self.agent_ws(stream.agent_id)
        if agent is None:
            await self.send_json(ws, {"type": "error", "code": "target_offline", "message": "کلاینت آنلاین نیست"})
            return
        if not (self.agents.get(stream.agent_id).info.get("capabilities") or {}).get("input", True):
            await self.send_json(ws, {"type": "control_denied", "stream_id": sid, "reason": "no_input_capability"})
            return

        async def on_approve(detail):
            stream.controller = ws
            await self.send_json(ws, {"type": "request_result", "req_id": req_id, "action": "request_control",
                                      "ok": True, "reason": "", "payload": {"stream_id": sid}})
            self.schedule_json(ws, {"type": "control_granted", "stream_id": sid})
            aw = self.agent_ws(stream.agent_id)
            if aw:
                self.schedule_json(aw, {"type": "control_state", "stream_id": sid, "granted": True})
            for v in stream.viewers:
                if v is not ws:
                    self.schedule_json(v, {"type": "control_denied", "stream_id": sid, "reason": "busy"})

        req_id = self.create_request("request_control", ws, stream.agent_id, {"stream_id": sid}, on_approve)
        self.schedule_json(agent, {
            "type": "approval_request", "req_id": req_id, "action": "control",
            "from": self.from_info(ws), "payload": {"stream_id": sid},
        })

    async def console_release_control(self, ws, msg):
        sid = int(msg.get("stream_id", 0))
        stream = self.streams.get(sid)
        if stream and stream.controller is ws:
            stream.controller = None
            self.schedule_json(ws, {"type": "control_revoked", "stream_id": sid, "reason": "released"})
            agent = self.agent_ws(stream.agent_id)
            if agent:
                self.schedule_json(agent, {"type": "control_state", "stream_id": sid, "granted": False})

    async def console_stop_view(self, ws, msg):
        sid = int(msg.get("stream_id", 0))
        self._close_agent_view_stream(sid, ws, reason="viewer_stopped")

    async def console_share_screen_to(self, ws, msg):
        target = msg.get("target", "")
        agent = self.agent_ws(target)
        if agent is None:
            await self.send_json(ws, {"type": "error", "code": "target_offline", "message": "کلاینت آنلاین نیست"})
            return
        sid = self.next_id()
        stream = Stream(sid, "console_view", target, ws, {})
        self.streams[sid] = stream

        async def on_approve(detail):
            stream.active = True
            self.schedule_json(ws, {"type": "request_result", "req_id": req_id, "action": "share_screen_to",
                                    "ok": True, "reason": "", "payload": {"stream_id": sid}})
            self.schedule_json(ws, {"type": "share_screen_started", "stream_id": sid,
                                    "target": stream_meta(self, target)})
            aw = self.agent_ws(target)
            if aw:
                self.schedule_json(aw, {"type": "share_screen_started", "stream_id": sid})

        req_id = self.create_request("share_screen_to", ws, target, {"stream_id": sid}, on_approve)
        self.schedule_json(agent, {
            "type": "approval_request", "req_id": req_id, "action": "console_share",
            "from": self.from_info(ws), "payload": {"stream_id": sid},
        })

    async def console_stop_share_to(self, ws, msg):
        sid = int(msg.get("stream_id", 0))
        stream = self.streams.pop(sid, None)
        if stream and stream.kind == "console_view":
            aw = self.agent_ws(stream.agent_id)
            if aw:
                self.schedule_json(aw, {"type": "share_screen_stopped", "stream_id": sid})
            self.schedule_json(ws, {"type": "share_ended", "stream_id": sid, "reason": "stopped"})

    async def console_input(self, ws, msg):
        sid = int(msg.get("stream_id", 0))
        stream = self.streams.get(sid)
        if not stream or stream.controller is not ws or not stream.active:
            return
        ev = msg.get("ev") or {}
        agent = self.agent_ws(stream.agent_id)
        if agent:
            self.schedule_json(agent, {"type": "input", "stream_id": sid, "ev": ev})

    # ---------- stream lifecycle ----------
    def _close_agent_view_stream(self, sid, ws=None, reason="ended"):
        stream = self.streams.get(sid)
        if not stream or stream.kind != "agent_view":
            return
        if ws is not None:
            stream.viewers.discard(ws)
            if stream.controller is ws:
                stream.controller = None
                aw = self.agent_ws(stream.agent_id)
                if aw:
                    self.schedule_json(aw, {"type": "control_state", "stream_id": sid, "granted": False})
        if ws is None or not stream.viewers:
            self.streams.pop(sid, None)
            for v in list(stream.viewers):
                self.schedule_json(v, {"type": "stream_ended", "stream_id": sid, "reason": reason})
            aw = self.agent_ws(stream.agent_id)
            if aw:
                self.schedule_json(aw, {"type": "stop_capture", "stream_id": sid})

    # ---------- agent messages ----------
    async def agent_screen_status(self, agent_ws, msg):
        sid = int(msg.get("stream_id", 0))
        active = bool(msg.get("active"))
        stream = self.streams.get(sid)
        if not stream:
            return
        if not active:
            self.streams.pop(sid, None)
            for v in list(stream.viewers):
                self.schedule_json(v, {"type": "stream_ended", "stream_id": sid,
                                       "reason": msg.get("reason", "agent_stopped")})

    async def agent_transfer_done(self, agent_ws, msg):
        tid = int(msg.get("transfer_id", 0))
        tx = self.transfers.pop(tid, None)
        if tx and tx.console_ws:
            self.schedule_json(tx.console_ws, {"type": "transfer_done", "transfer_id": tid,
                                               "name": tx.name, "size": tx.size})

    async def agent_transfer_failed(self, agent_ws, msg):
        tid = int(msg.get("transfer_id", 0))
        tx = self.transfers.pop(tid, None)
        if tx and tx.console_ws:
            self.schedule_json(tx.console_ws, {"type": "transfer_failed", "transfer_id": tid,
                                               "reason": msg.get("reason", "error"), "name": tx.name})

    async def agent_stop_share_view(self, agent_ws, msg):
        sid = int(msg.get("stream_id", 0))
        stream = self.streams.pop(sid, None)
        if stream and stream.kind == "console_view":
            self.schedule_json(stream.initiator, {"type": "share_ended", "stream_id": sid, "reason": "client_closed"})

    async def agent_console_share_ack(self, agent_ws, msg):
        sid = int(msg.get("stream_id", 0))
        stream = self.streams.get(sid)
        if stream and stream.initiator:
            ok = bool(msg.get("ok", False))
            if not ok:
                self.streams.pop(sid, None)
            self.schedule_json(stream.initiator, {"type": "share_screen_ack", "stream_id": sid,
                                                  "ok": ok, "reason": msg.get("reason", "")})

    # ---------- registration / lifecycle ----------
    async def register_agent(self, ws, msg):
        client_id = str(msg.get("client_id", ""))[:64]
        info = msg.get("info") or {}
        if not client_id:
            client_id = uuid4_hex()
        conn = self.agents.get(client_id)
        if conn is None:
            conn = AgentConn(client_id)
            self.agents[client_id] = conn
        if conn.ws is not None and conn.ws is not ws:
            try:
                await conn.ws.close(code=4001, message=b"replaced by new connection")
            except Exception:
                pass
        conn.ws = ws
        conn.info = info
        conn.online = True
        conn.last_seen = time.time()
        log.info("agent registered: %s (%s | %s | %s)", client_id,
                 info.get("hostname"), info.get("os"), info.get("user"))
        await self.broadcast_clients()
        return client_id

    def unregister_ws(self, ws):
        if ws in self.consoles:
            self.consoles.discard(ws)
            # خروج از همه stream ها
            for sid in list(self.streams.keys()):
                st = self.streams[sid]
                if st.kind == "agent_view":
                    if ws in st.viewers:
                        self._close_agent_view_stream(sid, ws, reason="viewer_left")
                    elif st.controller is ws:
                        st.controller = None
                elif st.kind == "console_view" and st.initiator is ws:
                    self.streams.pop(sid, None)
                    aw = self.agent_ws(st.agent_id)
                    if aw:
                        self.schedule_json(aw, {"type": "share_screen_stopped", "stream_id": sid})
            return True
        # ایجنت؟
        for cid, a in self.agents.items():
            if a.ws is ws:
                a.ws = None
                a.online = False
                a.last_seen = time.time()
                log.info("agent offline: %s", cid)
                # قطع جلسات این ایجنت
                for sid in list(self.streams.keys()):
                    st = self.streams[sid]
                    if st.agent_id == cid:
                        self.streams.pop(sid, None)
                        for v in list(st.viewers):
                            self.schedule_json(v, {"type": "stream_ended", "stream_id": sid, "reason": "client_offline"})
                        if st.initiator and st.kind == "console_view":
                            self.schedule_json(st.initiator, {"type": "share_ended", "stream_id": sid,
                                                              "reason": "client_offline"})
                for tid in [t for t, tx in self.transfers.items() if tx.agent_id == cid]:
                    tx = self.transfers.pop(tid)
                    self.schedule_json(tx.console_ws, {"type": "transfer_failed", "transfer_id": tid,
                                                       "reason": "client_offline", "name": tx.name})
                asyncio.ensure_future(self.broadcast_clients())
                return True
        return False

    async def watchdog(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            changed = False
            for a in self.agents.values():
                if a.online and now - a.last_seen > OFFLINE_AFTER:
                    a.online = False
                    changed = True
                    log.info("agent heartbeat timeout: %s", a.client_id)
            if changed:
                await self.broadcast_clients()

    # ---------- binary relay ----------
    async def relay_binary(self, ws, data: bytes, role: str):
        parsed = parse_binary(data)
        if not parsed:
            return
        btype, sid, meta, payload = parsed
        if btype == BIN_FILE_CHUNK:
            tid = int(meta.get("transfer_id", 0))
            tx = self.transfers.get(tid)
            if not tx:
                return
            tx.bytes_done += len(payload)
            if role == "console":
                if tx.console_ws is not ws or tx.direction != "c2a":
                    return
                aw = self.agent_ws(tx.agent_id)
                if aw:
                    await aw.send_bytes(data)
            else:
                if tx.direction != "a2c":
                    return
                if tx.console_ws:
                    await tx.console_ws.send_bytes(data)
        elif btype == BIN_SCREEN_FRAME:
            stream = self.streams.get(sid)
            if not stream or not stream.active:
                return
            if role == "agent":
                for v in list(stream.viewers):
                    if v is not ws:
                        await v.send_bytes(data)
            elif role == "console":
                if stream.kind != "console_view" or stream.initiator is not ws:
                    return
                aw = self.agent_ws(stream.agent_id)
                if aw:
                    await aw.send_bytes(data)


def stream_meta(hub: Hub, agent_id: str):
    a = hub.agents.get(agent_id)
    return {"id": agent_id, "info": (a.info if a else {})}


def uuid4_hex():
    import uuid as _uuid
    return _uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# HTTP / WS handlers
# ---------------------------------------------------------------------------
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LocalTools — ورود</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#06070a;--surface:rgba(255,255,255,.04);--surface-2:rgba(255,255,255,.07);
--border:rgba(255,255,255,.08);--text:#e8eaed;--text-dim:#6b7280;
--accent:#6366f1;--accent2:#8b5cf6;--err:#f43f5e}
body{font-family:'Segoe UI',Tahoma,'Vazirmatn',sans-serif;background:var(--bg);color:var(--text);
min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden}
.bg-canvas{position:fixed;inset:0;z-index:0;pointer-events:none}
.orb{position:absolute;border-radius:50%;filter:blur(110px);opacity:.45}
.orb-1{width:480px;height:480px;background:#6366f1;top:-15%;right:-10%;animation:drift 22s ease-in-out infinite alternate}
.orb-2{width:380px;height:380px;background:#06b6d4;bottom:-10%;left:-8%;animation:drift 18s ease-in-out infinite alternate-reverse}
@keyframes drift{0%{transform:translate(0,0)}50%{transform:translate(-35px,25px)}100%{transform:translate(20px,-15px)}}
.card{position:relative;z-index:2;width:100%;max-width:400px;padding:44px 36px 36px;
background:var(--surface);backdrop-filter:blur(40px);border:1px solid var(--border);border-radius:24px;
box-shadow:0 24px 48px rgba(0,0,0,.35);animation:in .5s cubic-bezier(.16,1,.3,1) both}
@keyframes in{from{opacity:0;transform:translateY(24px) scale(.97)}to{opacity:1;transform:none}}
.logo{width:58px;height:58px;margin:0 auto 18px;border-radius:16px;
background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;font-size:28px}
h2{text-align:center;font-size:20px;font-weight:700;margin-bottom:6px}
.sub{text-align:center;color:var(--text-dim);font-size:13px;margin-bottom:26px;line-height:1.7}
input{width:100%;padding:14px;font-size:16px;text-align:center;letter-spacing:6px;
background:var(--surface-2);border:1.5px solid var(--border);border-radius:14px;color:var(--text);
outline:none;font-family:'JetBrains Mono',monospace;transition:border-color .2s,box-shadow .2s}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(99,102,241,.25)}
button{width:100%;padding:14px;margin-top:14px;font-size:15px;font-weight:700;border:none;border-radius:14px;
cursor:pointer;color:#fff;background:linear-gradient(135deg,#6366f1,#8b5cf6)}
button:hover{filter:brightness(1.1)}
.error{text-align:center;color:var(--err);font-size:13px;margin-bottom:16px;padding:10px;border-radius:10px;
background:rgba(244,63,94,.12);border:1px solid rgba(244,63,94,.25)}
.footer{text-align:center;margin-top:22px;font-size:11px;color:var(--text-dim);opacity:.6}
</style></head>
<body>
<div class="bg-canvas"><div class="orb orb-1"></div><div class="orb orb-2"></div></div>
<div class="card">
<div class="logo">🛠️</div>
<h2>کنسول مدیریت LocalTools</h2>
<p class="sub">برای ورود، کد دسترسی سرور را وارد کنید</p>
{error}
<form method="POST" action="/login_auth">
<input type="password" name="password" placeholder="••••••" autofocus autocomplete="off">
<button type="submit">ورود به کنسول</button>
</form>
<div class="footer">LocalTools · LAN Suite · بدون ارسال به بیرون از شبکه</div>
</div>
</body></html>"""


def make_hub_routes(hub: Hub, webconsole_dir: str):
    routes = web.RouteTableDef()

    def session_valid(request):
        if hub.access_code is None:
            return True
        tok = request.cookies.get("lt_session", "")
        return tok in hub.sessions

    async def index_handler(request):
        if not session_valid(request):
            err = ""
            if request.query.get("err"):
                err = '<div class="error">کد دسترسی اشتباه است</div>'
            return web.Response(text=LOGIN_PAGE.replace("{error}", err), content_type="text/html", charset="utf-8")
        idx = os.path.join(webconsole_dir, "index.html")
        if os.path.exists(idx):
            with open(idx, "r", encoding="utf-8") as f:
                return web.Response(text=f.read(), content_type="text/html", charset="utf-8")
        return web.Response(status=404, text="webconsole/index.html not found")

    async def login_handler(request):
        if hub.access_code is None:
            raise web.HTTPFound("/")
        data = await request.post()
        if str(data.get("password", "")) == hub.access_code:
            tok = secrets.token_hex(16)
            hub.sessions.add(tok)
            resp = web.HTTPFound("/")
            resp.set_cookie("lt_session", tok, httponly=True, samesite="Strict", max_age=86400 * 7)
            log.info("console login ok from %s", request.remote)
            return resp
        log.info("console login FAILED from %s", request.remote)
        raise web.HTTPFound("/?err=1")

    async def health_handler(request):
        return web.json_response({"ok": True, "version": VERSION,
                                  "clients": len(hub.agents), "online": sum(1 for a in hub.agents.values() if a.online)})

    async def ws_handler(request):
        role = request.query.get("role", "console")
        # احراز هویت
        if hub.access_code is not None:
            if role == "agent":
                if request.query.get("code", "") != hub.access_code:
                    return await _ws_reject(request, "auth_failed")
            else:
                if not session_valid(request):
                    return await _ws_reject(request, "auth_failed")
        ws = web.WebSocketResponse(max_msg_size=MAX_MSG, heartbeat=30)
        await ws.prepare(request)
        hub.addr_by_ws[ws] = request.remote
        log.info("ws connected: role=%s from %s", role, request.remote)

        client_id = None
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    mtype = data.get("type", "")
                    if role == "agent":
                        if mtype == "register":
                            client_id = await hub.register_agent(ws, data)
                        elif mtype == "heartbeat":
                            a = hub.agents.get(client_id or "")
                            if a:
                                a.last_seen = time.time()
                                if not a.online:
                                    a.online = True
                                    await hub.broadcast_clients()
                            await hub.send_json(ws, {"type": "pong"})
                        elif mtype == "approve":
                            await hub.resolve_request(int(data.get("req_id", 0)),
                                                      bool(data.get("ok")), data.get("detail"), ws)
                        elif mtype == "transfer_done":
                            await hub.agent_transfer_done(ws, data)
                        elif mtype == "transfer_failed":
                            await hub.agent_transfer_failed(ws, data)
                        elif mtype == "screen_status":
                            await hub.agent_screen_status(ws, data)
                        elif mtype == "stop_share_view":
                            await hub.agent_stop_share_view(ws, data)
                        elif mtype == "console_share_ack":
                            await hub.agent_console_share_ack(ws, data)
                    else:  # console
                        hub.consoles.add(ws)
                        if mtype == "list_clients":
                            await hub.send_json(ws, hub.clients_snapshot())
                        elif mtype == "send_files":
                            await hub.console_send_files(ws, data)
                        elif mtype == "transfer_ready":
                            tid = int(data.get("transfer_id", 0))
                            tx = hub.transfers.get(tid)
                            if tx and tx.console_ws is ws:
                                aw = hub.agent_ws(tx.agent_id)
                                if aw:
                                    if tx.direction == "c2a":
                                        await hub.send_json(aw, {"type": "transfer_begin", "transfer_id": tid,
                                                                 "name": tx.name, "size": tx.size})
                                    else:
                                        await hub.send_json(aw, {"type": "transfer_ready", "transfer_id": tid,
                                                                 "name": tx.name, "size": tx.size})
                        elif mtype == "request_files":
                            await hub.console_request_files(ws, data)
                        elif mtype == "request_screen":
                            await hub.console_request_screen(ws, data)
                        elif mtype == "set_quality":
                            sid = int(data.get("stream_id", 0))
                            st = hub.streams.get(sid)
                            aw = hub.agent_ws(st.agent_id) if st else None
                            if aw:
                                await hub.send_json(aw, {"type": "set_quality", "stream_id": sid,
                                                         "quality": data.get("quality", "medium")})
                        elif mtype == "request_control":
                            await hub.console_request_control(ws, data)
                        elif mtype == "release_control":
                            await hub.console_release_control(ws, data)
                        elif mtype == "stop_view":
                            await hub.console_stop_view(ws, data)
                        elif mtype == "share_screen_to":
                            await hub.console_share_screen_to(ws, data)
                        elif mtype == "stop_share_to":
                            await hub.console_stop_share_to(ws, data)
                        elif mtype == "input":
                            await hub.console_input(ws, data)
                elif msg.type == WSMsgType.BINARY:
                    await hub.relay_binary(ws, msg.data, role)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        finally:
            hub.addr_by_ws.pop(ws, None)
            hub.unregister_ws(ws)
        return ws

    async def _ws_reject(request, code):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({"type": "error", "code": code, "message": "احراز هویت ناموفق"}))
        await ws.close(code=4401, message=code.encode())
        return ws

    routes.get("/")(index_handler)
    routes.post("/login_auth")(login_handler)
    routes.get("/health")(health_handler)
    routes.get("/ws")(ws_handler)
    return routes


def auto_selfsigned_cert(certfile, keyfile):
    """ساخت گواهی self-signed با openssl برای فعال‌سازی getDisplayMedia روی LAN"""
    os.makedirs(os.path.dirname(certfile) or ".", exist_ok=True)
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", keyfile, "-out", certfile, "-days", "825",
           "-subj", "/CN=localtools-hub"]
    subprocess.run(cmd, check=True, capture_output=True)
    log.info("self-signed certificate created: %s", certfile)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="LocalTools Hub Server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--code", default=None, help="کد دسترسی (PIN) — اگر خالی باشد کنسول بدون رمز باز می‌شود")
    ap.add_argument("--ssl", action="store_true", help="فعال‌سازی HTTPS (برای اشتراک صفحه از مرورگر روی LAN لازم است)")
    ap.add_argument("--certfile", default="certs/hub.crt")
    ap.add_argument("--keyfile", default="certs/hub.key")
    ap.add_argument("--webconsole", default=None, help="مسیر پوشه webconsole (پیش‌فرض: کنار server.py)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    here = os.path.dirname(os.path.abspath(__file__))
    webconsole_dir = args.webconsole or os.path.normpath(os.path.join(here, "..", "webconsole"))
    if not os.path.isdir(webconsole_dir):
        print(f"[!] پوشه webconsole پیدا نشد: {webconsole_dir}", file=sys.stderr)
        sys.exit(1)

    hub = Hub(access_code=args.code)
    app = web.Application(client_max_size=MAX_MSG)
    app.add_routes(make_hub_routes(hub, webconsole_dir))
    app.router.add_static("/static/", webconsole_dir)

    async def _start_watchdog(_app):
        asyncio.ensure_future(hub.watchdog())

    app.on_startup.append(_start_watchdog)

    ssl_ctx = None
    scheme = "http"
    if args.ssl:
        cert, key = args.certfile, args.keyfile
        if not (os.path.exists(cert) and os.path.exists(key)):
            try:
                auto_selfsigned_cert(cert, key)
            except Exception as e:
                print(f"[!] ساخت گواهی ناموفق: {e} — با openssl دستی بسازید.", file=sys.stderr)
                sys.exit(1)
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cert, key)
        scheme = "https"

    ip = lan_ip()
    print("=" * 62)
    print("  LocalTools Hub — سرور مرکزی ابزار شرکت")
    print(f"  نسخه: {VERSION}")
    print(f"  نشانی کنسول وب : {scheme}://{ip}:{args.port}/")
    print(f"  نشانی محلی     : {scheme}://127.0.0.1:{args.port}/")
    print(f"  نشانی ایجنت‌ها : {scheme}://{ip}:{args.port}/ws?role=agent" + (f"&code={args.code}" if args.code else ""))
    if args.code:
        print(f"  کد دسترسی کنسول: {args.code}")
    else:
        print("  ⚠ بدون کد دسترسی اجرا شده — برای محیط شرکت با --code اجرا کنید")
    if args.ssl:
        print("  HTTPS فعال است (گواهی self-signed — هنگام اولین باز شدن، Accept کنید)")
    else:
        print("  ⚠ HTTP ساده: دکمه «اشتراک صفحه من» فقط روی localhost کار می‌کند (محدودیت مرورگر)")
        print("    برای LAN از --ssl استفاده کنید")
    print("=" * 62)

    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx,
                print=lambda *a, **k: None)


if __name__ == "__main__":
    main()
