/* LocalTools Web Console — منطق اصلی
 * پروتکل: docs/PROTOCOL.md
 */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const CHUNK_SIZE = 256 * 1024;
const QUALITY_PRESET = { low: { scale: 0.45, jpg: 0.55 }, medium: { scale: 0.65, jpg: 0.68 }, high: { scale: 1.0, jpg: 0.8 } };

let ws = null;
let reconnectTimer = null;
let clients = [];
let myFilter = 'all';
let searchText = '';

// ارسال فایل (کنسول → کلاینت)
let sendTargetId = null;
let sendQueue = [];               // [{file, el}]
let sendingActive = false;

// دریافت فایل (کلاینت → کنسول)
const incoming = new Map();       // tid -> {name,size,parts,bytes,el}
let a2cQueue = [];                // tid ها به ترتیب

// ویوئر صفحه کلاینت
const viewer = {
  streamId: 0, targetId: null, control: false,
  frameW: 0, frameH: 0, bitmap: null, cursor: { x: -1, y: -1 },
  listenersAttached: false, lastPointerSent: 0, lastPt: null,
};

// اشتراک صفحه من (کنسول → کلاینت)
const share = { streamId: 0, targetId: null, mediaStream: null, sendTimer: null, video: null, canvas: null };

const $ = id => document.getElementById(id);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fmtSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}
function osEmoji(os) {
  const s = (os || '').toLowerCase();
  if (s.includes('win')) return '🪟';
  if (s.includes('lin')) return '🐧';
  if (s.includes('dar')) return '🍎';
  return '💻';
}
function toast(msg, kind = '') {
  const c = $('toastContainer');
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  t.textContent = msg;
  c.appendChild(t);
  requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add('show')));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 400); }, 3500);
}
function binMsg(type, streamId, meta, payload) {
  const metaBytes = new TextEncoder().encode(JSON.stringify(meta));
  const total = 9 + metaBytes.length + (payload ? payload.byteLength : 0);
  const buf = new Uint8Array(total);
  const dv = new DataView(buf.buffer);
  buf[0] = type;
  dv.setUint32(1, streamId);
  dv.setUint32(5, metaBytes.length);
  buf.set(metaBytes, 9);
  if (payload) buf.set(new Uint8Array(payload), 9 + metaBytes.length);
  return buf.buffer;
}
function parseBin(data) {
  const dv = new DataView(data);
  if (data.byteLength < 9) return null;
  const type = dv.getUint8(0);
  const streamId = dv.getUint32(1);
  const metaLen = dv.getUint32(5);
  if (data.byteLength < 9 + metaLen) return null;
  let meta = {};
  try { meta = JSON.parse(new TextDecoder().decode(new Uint8Array(data, 9, metaLen))); } catch (e) { }
  return { type, streamId, meta, payload: data.slice(9 + metaLen) };
}
function openModal(id) { $(id).classList.add('show'); }
function closeModal(id) { $(id).classList.remove('show'); }
document.querySelectorAll('[data-close]').forEach(btn =>
  btn.addEventListener('click', () => closeModal(btn.dataset.close)));

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  setConn('connecting', 'در حال اتصال…');
  ws = new WebSocket(`${proto}//${location.host}/ws?role=console`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    setConn('online', 'متصل به سرور');
    sendJson({ type: 'list_clients' });
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  };
  ws.onmessage = ev => {
    if (typeof ev.data === 'string') { try { handleJson(JSON.parse(ev.data)); } catch (e) { console.error(e); } }
    else handleBinary(ev.data);
  };
  ws.onclose = ev => {
    setConn('offline', 'قطع اتصال — تلاش مجدد…');
    if (ev.reason === 'auth_failed' || (ev.code && ev.code === 4401)) { location.href = '/'; return; }
    if (!reconnectTimer) reconnectTimer = setTimeout(connect, 2000);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { } };
}
function setConn(cls, text) {
  const d = $('connDot'), t = $('connText');
  d.className = 'conn-dot ' + cls;
  t.textContent = text;
}
function sendJson(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}
function sendBin(buf) {
  if (ws && ws.readyState === 1) ws.send(buf);
}

// ---------------------------------------------------------------------------
// JSON dispatch
// ---------------------------------------------------------------------------
const waiters = new Map();  // transfer_id -> resolve(نتیجه)
function waitTransferDone(tid) {
  return new Promise(res => waiters.set(tid, res));
}

function handleJson(msg) {
  switch (msg.type) {
    case 'clients':
      clients = msg.clients || [];
      renderClients();
      $('statTotal').textContent = clients.length;
      $('statOnline').textContent = clients.filter(c => c.online).length;
      break;

    case 'request_result':
      onRequestResult(msg);
      break;

    case 'transfers_approved': {
      // ترتیب فایل‌های انتخاب‌شده همان ترتیب transfers است
      uploadTransfers(msg.transfers).catch(e => { toast('خطا در ارسال: ' + e.message, 'err'); finishSending(); });
      break;
    }

    case 'file_incoming': {
      const tid = msg.transfer_id;
      incoming.set(tid, { name: msg.name, size: msg.size, parts: [], bytes: 0, el: null });
      a2cQueue.push(tid);
      createIncomingItem(tid, msg.name, msg.size);
      startNextA2C();
      break;
    }

    case 'transfer_done': {
      const tid = msg.transfer_id;
      const w = waiters.get(tid);
      if (w) { waiters.delete(tid); w(true); }
      finishIncoming(tid);
      break;
    }

    case 'transfer_failed': {
      const tid = msg.transfer_id;
      const w = waiters.get(tid);
      if (w) { waiters.delete(tid); w(false); }
      const inc = incoming.get(tid);
      if (inc && inc.el) inc.el.querySelector('.in-status').textContent = 'خطا: ' + (msg.reason || '');
      incoming.delete(tid);
      toast('❌ انتقال فایل ناموفق: ' + (inc ? inc.name : tid) + ' — ' + (msg.reason || ''), 'err');
      break;
    }

    case 'screen_started':
      viewer.streamId = msg.stream_id;
      viewer.targetId = msg.target && msg.target.id;
      $('viewerTargetInfo').textContent = (msg.target && msg.target.info.hostname) || '';
      openModal('viewerModal');
      resetViewerBadge();
      // تایید شد — حالت فقط-مشاهده فعال
      $('viewerBadge').className = 'vt-badge view';
      $('viewerBadge').textContent = '👁️ فقط مشاهده';
      break;

    case 'control_granted':
      setViewerControl(true);
      break;

    case 'control_denied':
      setViewerControl(false);
      toast('درخواست کنترل رد شد: ' + (msg.reason === 'busy' ? 'کنسول دیگری کنترل دارد' : (msg.reason || 'تایید نشد')), 'warn');
      break;

    case 'control_revoked':
      setViewerControl(false);
      break;

    case 'stream_ended':
      if (msg.stream_id === viewer.streamId) closeViewer('جلسه پایان یافت (' + (msg.reason || '') + ')');
      break;

    case 'share_screen_started':
      share.streamId = msg.stream_id;
      share.targetId = msg.target && msg.target.id;
      startDisplayCapture().catch(e => {
        toast('دسترسی به صفحه ممکن نشد: ' + e.message, 'err');
        sendJson({ type: 'stop_share_to', stream_id: share.streamId });
        closeModal('shareModal');
      });
      break;

    case 'share_screen_ack':
      if (!msg.ok) {
        toast('کلاینت پنجره نمایش را باز نکرد: ' + (msg.reason || ''), 'err');
        stopMyShare();
        closeModal('shareModal');
      }
      break;

    case 'share_ended':
      if (msg.stream_id === share.streamId) {
        stopMyShare();
        closeModal('shareModal');
        toast('اشتراک صفحه پایان یافت (' + (msg.reason || '') + ')', 'warn');
      }
      break;

    case 'error':
      toast('⚠ ' + (msg.message || msg.code), 'err');
      if (viewer.streamId && (msg.code === 'target_offline')) closeViewer();
      break;
  }
}

function onRequestResult(msg) {
  if (!msg.ok) {
    const why = msg.reason === 'timeout' ? 'پاسخی از کلاینت نیامد (60s)' : msg.reason === 'rejected' ? 'کاربر کلاینت رد کرد' : msg.reason;
    if (msg.action === 'request_screen') closeViewer('درخواست تایید نشد: ' + why);
    if (msg.action === 'share_screen_to') {
      closeModal('shareModal');
      toast('اشتراک صفحه تایید نشد: ' + why, 'warn');
    }
    if (msg.action === 'send_files') { finishSending(); toast('ارسال فایل تایید نشد: ' + why, 'warn'); }
    else toast('درخواست تایید نشد: ' + why, 'warn');
    return;
  }
  if (msg.action === 'send_files') { /* transfers_approved جدا می‌آید */ }
  if (msg.action === 'request_files') toast('درخواست فایل تایید شد — در انتظار انتخاب فایل…');
  if (msg.action === 'request_control') { /* control_granted جدا می‌آید */ }
}

// ---------------------------------------------------------------------------
// Clients rendering
// ---------------------------------------------------------------------------
function renderClients() {
  const grid = $('clientsGrid');
  const q = searchText.trim().toLowerCase();
  let list = clients.filter(c => {
    if (myFilter === 'online' && !c.online) return false;
    if (myFilter === 'offline' && c.online) return false;
    if (!q) return true;
    const i = c.info || {};
    return (i.hostname || '').toLowerCase().includes(q) ||
      (i.user || '').toLowerCase().includes(q) ||
      (i.ip || '').includes(q);
  });
  grid.innerHTML = '';
  if (!list.length) {
    grid.innerHTML = `<div class="empty-state"><div class="empty-icon">📡</div>
      <div class="empty-title">کلاینتی پیدا نشد</div>
      <div class="empty-sub">ایجنت را روی کلاینت‌ها اجرا کنید: <code>python agent.py --server ws://SERVER_IP:8788/ws</code></div></div>`;
    return;
  }
  list.forEach((c, idx) => {
    const i = c.info || {};
    const caps = i.capabilities || {};
    const card = document.createElement('div');
    card.className = 'client-card' + (c.online ? '' : ' offline');
    card.style.animationDelay = (idx * 0.04) + 's';
    card.dataset.clientId = c.id;
    card.innerHTML = `
      <span class="cc-status ${c.online ? 'st-on' : 'st-off'}">${c.online ? 'آنلاین' : 'آفلاین'}</span>
      <div class="cc-head">
        <div class="cc-os">${osEmoji(i.os)}</div>
        <div>
          <div class="cc-name">${escapeHtml(i.hostname || c.id)}</div>
          <div class="cc-user">${escapeHtml(i.user || '?')} · ${escapeHtml(i.os || '?')}${i.os_version ? ' ' + escapeHtml(i.os_version) : ''}</div>
        </div>
      </div>
      <div class="cc-meta">
        <span class="chip">🌐 ${escapeHtml(i.ip || '?')}</span>
        ${caps.capture ? '<span class="chip cap">🖥️ کپچر</span>' : ''}
        ${caps.input ? '<span class="chip cap">⌨️ کنترل</span>' : ''}
        <span class="chip">📺 ${i.monitors || 1} مانیتور</span>
      </div>
      <div class="cc-actions">
        <button class="cc-btn primary" data-act="send" ${c.online ? '' : 'disabled'}>📤 ارسال فایل</button>
        <button class="cc-btn" data-act="reqfile" ${c.online ? '' : 'disabled'}>📥 درخواست فایل</button>
        <button class="cc-btn" data-act="view" ${c.online && caps.capture ? '' : 'disabled'}>🖥️ مشاهده صفحه</button>
        <button class="cc-btn" data-act="share" ${c.online ? '' : 'disabled'}>📺 نمایش صفحه من</button>
      </div>`;
    card.querySelector('[data-act=send]').addEventListener('click', () => openSendModal(c));
    card.querySelector('[data-act=reqfile]').addEventListener('click', () => {
      sendJson({ type: 'request_files', target: c.id });
      toast('درخواست فایل برای «' + (i.hostname || c.id) + '» ارسال شد — در انتظار تایید…', 'warn');
    });
    card.querySelector('[data-act=view]').addEventListener('click', () => viewScreen(c));
    card.querySelector('[data-act=share]').addEventListener('click', () => shareScreenTo(c));

    // درگ‌ودراپ روی کارت
    card.addEventListener('dragover', e => { e.preventDefault(); card.classList.add('dragover'); });
    card.addEventListener('dragleave', () => card.classList.remove('dragover'));
    card.addEventListener('drop', e => {
      e.preventDefault(); e.stopPropagation();
      card.classList.remove('dragover');
      $('dropOverlay').classList.remove('show');
      if (!c.online) { toast('این کلاینت آفلاین است', 'warn'); return; }
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length) offerFiles(c, files);
    });
    grid.appendChild(card);
  });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
}
$('searchInput').addEventListener('input', e => { searchText = e.target.value; renderClients(); });
document.querySelectorAll('.filter-btn').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.filter-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  myFilter = b.dataset.filter;
  renderClients();
}));

// ---------------------------------------------------------------------------
// Drag&drop سراسری
// ---------------------------------------------------------------------------
let dragDepth = 0;
window.addEventListener('dragenter', e => {
  if (e.dataTransfer && Array.from(e.dataTransfer.types).includes('Files')) {
    dragDepth++; $('dropOverlay').classList.add('show');
  }
});
window.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('dropOverlay').classList.remove('show'); });
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => {
  e.preventDefault(); dragDepth = 0; $('dropOverlay').classList.remove('show');
  const files = Array.from(e.dataTransfer.files || []);
  if (files.length) toast('فایل را روی کارت کلاینتِ مقصد رها کنید تا ارسال شود', 'warn');
});

// ---------------------------------------------------------------------------
// ارسال فایل
// ---------------------------------------------------------------------------
function openSendModal(client) {
  sendTargetId = client.id;
  sendQueue = [];
  $('sendTitle').textContent = '📤 ارسال فایل به «' + ((client.info && client.info.hostname) || client.id) + '»';
  renderSendQueue();
  openModal('sendModal');
}
$('sendDropzone').addEventListener('dragover', e => { e.preventDefault(); e.stopPropagation(); $('sendDropzone').classList.add('dragover'); });
$('sendDropzone').addEventListener('dragleave', e => { e.stopPropagation(); $('sendDropzone').classList.remove('dragover'); });
$('sendDropzone').addEventListener('drop', e => {
  e.preventDefault(); e.stopPropagation();
  $('sendDropzone').classList.remove('dragover');
  addFilesToSend(Array.from(e.dataTransfer.files || []));
});
$('sendInput').addEventListener('change', e => { addFilesToSend(Array.from(e.target.files || [])); e.target.value = ''; });
function addFilesToSend(files) {
  for (const f of files) sendQueue.push({ file: f });
  renderSendQueue();
}
function renderSendQueue() {
  const ul = $('sendQueue');
  ul.innerHTML = '';
  sendQueue.forEach((item, i) => {
    const li = document.createElement('li');
    li.className = 'queue-item';
    li.innerHTML = `<span class="qi-name">📄 ${escapeHtml(item.file.name)}</span>
      <span class="qi-progress"><div class="progress-track"><div class="progress-fill" style="width:0%"></div></div><span class="qi-pct">0%</span></span>
      <span class="qi-size">${fmtSize(item.file.size)}</span>
      <button class="qi-remove">✕</button>`;
    li.querySelector('.qi-remove').addEventListener('click', () => { sendQueue.splice(i, 1); renderSendQueue(); });
    ul.appendChild(li);
  });
  $('sendBtn').disabled = !sendQueue.length || sendingActive;
}
$('sendBtn').addEventListener('click', () => {
  if (!sendQueue.length || !sendTargetId || sendingActive) return;
  const files = sendQueue.map(x => ({ name: x.file.name, size: x.file.size }));
  sendJson({ type: 'send_files', target: sendTargetId, files });
  toast('پیشنهاد ارسال رفت — در انتظار تایید کاربر کلاینت…', 'warn');
});
function setSendProgress(idx, pct) {
  const li = $('sendQueue').children[idx];
  if (li) {
    li.querySelector('.progress-fill').style.width = pct + '%';
    li.querySelector('.qi-pct').textContent = pct + '%';
  }
}
function finishSending() {
  sendingActive = false;
  $('sendBtn').disabled = false;
}
async function uploadTransfers(transfers) {
  if (!transfers.length) { finishSending(); return; }
  sendingActive = true;
  $('sendBtn').disabled = true;
  for (let idx = 0; idx < transfers.length; idx++) {
    const t = transfers[idx];
    const item = sendQueue[idx];
    if (!item) break;
    const file = item.file;
    sendJson({ type: 'transfer_ready', transfer_id: t.transfer_id });
    const donePromise = waitTransferDone(t.transfer_id);
    let seq = 0;
    for (let offset = 0; offset < file.size; offset += CHUNK_SIZE) {
      const blobSlice = file.slice(offset, Math.min(offset + CHUNK_SIZE, file.size));
      const buf = await blobSlice.arrayBuffer();
      const last = offset + CHUNK_SIZE >= file.size;
      sendBin(binMsg(0x02, 0, { transfer_id: t.transfer_id, seq: seq++, last }, buf));
      setSendProgress(idx, Math.round(Math.min(100, (offset + buf.byteLength) / file.size * 100)));
      await new Promise(r => setTimeout(r, 0)); // نفس کشیدن UI
    }
    setSendProgress(idx, 100);
    const ok = await donePromise;
    if (!ok) throw new Error('انتقال «' + file.name + '» ناموفق بود');
  }
  finishSending();
  closeModal('sendModal');
  toast('✅ همه فایل‌ها با موفقیت ارسال شد (' + transfers.length + ' فایل)', 'ok');
}
function offerFiles(client, files) {
  if (!files.length) return;
  const payload = files.map(f => ({ name: f.name, size: f.size }));
  sendJson({ type: 'send_files', target: client.id, files: payload });
  // ذخیره موقت برای بعد از تایید
  pendingOffers.set(client.id, files);
  toast('ارسال ' + files.length + ' فایل به «' + ((client.info && client.info.hostname) || client.id) + '» — در انتظار تایید…', 'warn');
}
const pendingOffers = new Map(); // client_id -> File[]

// در transfers_approved باید فایل‌های واقعی همان target باشد
const _origHandleTransfers = true;
(function patchTransfersHandler() {
  const orig = handleJson;
  handleJson = function (msg) {
    if (msg.type === 'transfers_approved') {
      const files = pendingOffers.get(msg.target) || sendQueue.map(x => x.file);
      pendingOffers.delete(msg.target);
      if (!sendTargetId || sendTargetId !== msg.target) { /* ارسال از طریق drop */ }
      startUploadFromFiles(msg.transfers, files).catch(e => { toast('خطا در ارسال: ' + e.message, 'err'); finishSending(); });
      return;
    }
    orig(msg);
  };
})();

async function startUploadFromFiles(transfers, files) {
  sendingActive = true;
  // نمایش پیشرفت داخل مودال اگر باز است، وگرنه toast
  for (let idx = 0; idx < transfers.length; idx++) {
    const t = transfers[idx];
    const file = files[idx];
    if (!file) throw new Error('فایل محلی یافت نشد');
    sendJson({ type: 'transfer_ready', transfer_id: t.transfer_id });
    const donePromise = waitTransferDone(t.transfer_id);
    let seq = 0;
    for (let offset = 0; offset < file.size; offset += CHUNK_SIZE) {
      const buf = await file.slice(offset, Math.min(offset + CHUNK_SIZE, file.size)).arrayBuffer();
      const last = offset + CHUNK_SIZE >= file.size;
      sendBin(binMsg(0x02, 0, { transfer_id: t.transfer_id, seq: seq++, last }, buf));
      setSendProgress(idx, Math.round(Math.min(100, (offset + buf.byteLength) / file.size * 100)));
      await new Promise(r => setTimeout(r, 0));
    }
    setSendProgress(idx, 100);
    const ok = await donePromise;
    if (!ok) throw new Error('انتقال «' + file.name + '» ناموفق بود');
  }
  finishSending();
  closeModal('sendModal');
  toast('✅ همه فایل‌ها ارسال شد (' + transfers.length + ')', 'ok');
}

// ---------------------------------------------------------------------------
// دریافت فایل (a2c)
// ---------------------------------------------------------------------------
function startNextA2C() {
  for (const tid of a2cQueue) {
    if (incoming.has(tid) && !incoming.get(tid).started) {
      incoming.get(tid).started = true;
      sendJson({ type: 'transfer_ready', transfer_id: tid });
      return;
    }
  }
}
function createIncomingItem(tid, name, size) {
  const panel = $('incomingPanel');
  const el = document.createElement('div');
  el.className = 'incoming-item';
  el.innerHTML = `<div class="in-item-head"><span>📥</span><span class="in-name">${escapeHtml(name)}</span>
    <span class="in-status">در حال دریافت…</span></div>
    <div style="display:flex;align-items:center;gap:8px">
    <div class="progress-track"><div class="progress-fill" style="width:0%"></div></div>
    <span class="in-size">0/${fmtSize(size)}</span></div>`;
  panel.appendChild(el);
  incoming.get(tid).el = el;
}
function handleFileChunk(parsed) {
  const tid = parsed.meta.transfer_id;
  const inc = incoming.get(tid);
  if (!inc) return;
  inc.parts.push(parsed.payload);
  inc.bytes += parsed.payload.byteLength;
  if (inc.el) {
    inc.el.querySelector('.progress-fill').style.width = Math.round(inc.bytes / Math.max(1, inc.size) * 100) + '%';
    inc.el.querySelector('.in-size').textContent = fmtSize(inc.bytes) + '/' + fmtSize(inc.size);
  }
}
function finishIncoming(tid) {
  const inc = incoming.get(tid);
  if (!inc) { startNextA2C(); return; }
  const blob = new Blob(inc.parts, { type: 'application/octet-stream' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = inc.name || ('file_' + tid);
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  if (inc.el) inc.el.querySelector('.in-status').textContent = '✅ کامل شد — دانلود شد';
  setTimeout(() => { if (inc.el) inc.el.remove(); incoming.delete(tid); }, 6000);
  a2cQueue = a2cQueue.filter(t => t !== tid);
  toast('📥 «' + inc.name + '» دریافت شد', 'ok');
  startNextA2C();
}

// ---------------------------------------------------------------------------
// مشاهده صفحه کلاینت (مرحله 1 مشاهده، مرحله 2 کنترل)
// ---------------------------------------------------------------------------
function viewScreen(client) {
  if (viewer.streamId) closeViewer();
  viewer.targetId = client.id;
  viewer.control = false;
  const canvas = $('viewerCanvas');
  canvas.width = 1280; canvas.height = 720;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  sendJson({ type: 'request_screen', target: client.id, quality: $('qualitySelect').value });
}
function resetViewerBadge() {
  const b = $('viewerBadge');
  b.className = 'vt-badge wait';
  b.textContent = 'در انتظار تایید کلاینت…';
  $('requestControlBtn').style.display = '';
  $('releaseControlBtn').style.display = 'none';
  $('viewerWrap').classList.remove('control-mode');
}
function setViewerControl(on) {
  viewer.control = on;
  const b = $('viewerBadge');
  if (on) {
    b.className = 'vt-badge control';
    b.textContent = '⌨️ کنترل فعال';
    $('requestControlBtn').style.display = 'none';
    $('releaseControlBtn').style.display = '';
    $('viewerWrap').classList.add('control-mode');
    $('viewerCanvas').focus();
    toast('کنترل موس/کیبورد فعال شد', 'ok');
  } else {
    b.className = 'vt-badge view';
    b.textContent = '👁️ فقط مشاهده';
    $('requestControlBtn').style.display = '';
    $('releaseControlBtn').style.display = 'none';
    $('viewerWrap').classList.remove('control-mode');
  }
}
function closeViewer(msg) {
  if (viewer.streamId) sendJson({ type: 'stop_view', stream_id: viewer.streamId });
  if (viewer.control) sendJson({ type: 'release_control', stream_id: viewer.streamId });
  if (viewer.bitmap) { viewer.bitmap.close(); viewer.bitmap = null; }
  viewer.streamId = 0; viewer.targetId = null; viewer.control = false;
  closeModal('viewerModal');
  if (msg) toast(msg, 'warn');
}
$('stopViewBtn').addEventListener('click', () => closeViewer());
$('fullscreenBtn').addEventListener('click', () => {
  const el = $('viewerModal');
  if (document.fullscreenElement) document.exitFullscreen();
  else el.requestFullscreen().catch(() => { });
});
$('qualitySelect').addEventListener('change', () => {
  if (viewer.streamId) sendJson({ type: 'set_quality', stream_id: viewer.streamId, quality: $('qualitySelect').value });
});
$('requestControlBtn').addEventListener('click', () => {
  if (viewer.streamId && !viewer.control) {
    sendJson({ type: 'request_control', target: viewer.targetId, stream_id: viewer.streamId });
    toast('درخواست کنترل ارسال شد — در انتظار تایید کاربر…', 'warn');
  }
});
$('releaseControlBtn').addEventListener('click', () => {
  if (viewer.streamId) {
    sendJson({ type: 'release_control', stream_id: viewer.streamId });
    setViewerControl(false);
  }
});

// --- رسم فریم‌ها ---
function handleScreenFrame(parsed) {
  if (parsed.streamId !== viewer.streamId) return;
  viewer.frameW = parsed.meta.w || 1;
  viewer.frameH = parsed.meta.h || 1;
  viewer.cursor = { x: parsed.meta.cx ?? -1, y: parsed.meta.cy ?? -1 };
  blobToBitmap(parsed.payload).then(bmp => {
    if (viewer.bitmap) viewer.bitmap.close();
    viewer.bitmap = bmp;
    drawViewer();
  }).catch(() => { });
}
function blobToBitmap(payload) {
  return new Promise((res, rej) => {
    const blob = new Blob([payload], { type: 'image/jpeg' });
    if (window.createImageBitmap) createImageBitmap(blob).then(res).catch(rej);
    else {
      const img = new Image();
      img.onload = () => res(img);
      img.onerror = rej;
      img.src = URL.createObjectURL(blob);
    }
  });
}
function drawViewer() {
  if (!viewer.bitmap) return;
  const canvas = $('viewerCanvas');
  const bw = viewer.bitmap.width, bh = viewer.bitmap.height;
  if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; }
  const ctx = canvas.getContext('2d');
  ctx.drawImage(viewer.bitmap, 0, 0);
  // نشانگر ماوسِ کلاینت
  if (viewer.cursor.x >= 0) {
    const cx = viewer.cursor.x / viewer.frameW * canvas.width;
    const cy = viewer.cursor.y / viewer.frameH * canvas.height;
    ctx.beginPath();
    ctx.arc(cx, cy, 7, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(34,211,238,.85)';
    ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = '#fff'; ctx.stroke();
  }
}

// --- ورودی موس/کیبورد (فقط وقتی control فعال است) ---
function attachViewerListeners() {
  const canvas = $('viewerCanvas');
  canvas.addEventListener('contextmenu', e => e.preventDefault());
  canvas.addEventListener('pointermove', e => {
    if (!viewer.control || !viewer.frameW) return;
    const now = performance.now();
    if (now - viewer.lastPointerSent < 30) { viewer.lastPt = e; return; }
    viewer.lastPointerSent = now;
    sendPointer(e);
  });
  canvas.addEventListener('pointerdown', e => { if (viewer.control) { e.preventDefault(); canvas.focus(); sendPointer(e); sendJson({ type: 'input', stream_id: viewer.streamId, ev: { kind: 'mouse_btn', btn: btnName(e.button), pressed: true } }); } });
  canvas.addEventListener('pointerup', e => { if (viewer.control) { sendPointer(e); sendJson({ type: 'input', stream_id: viewer.streamId, ev: { kind: 'mouse_btn', btn: btnName(e.button), pressed: false } }); } });
  canvas.addEventListener('wheel', e => {
    if (!viewer.control) return;
    e.preventDefault();
    sendJson({ type: 'input', stream_id: viewer.streamId, ev: { kind: 'wheel', dy: e.deltaY } });
  }, { passive: false });
  canvas.addEventListener('keydown', e => {
    if (!viewer.control) return;
    e.preventDefault();
    sendJson({ type: 'input', stream_id: viewer.streamId, ev: { kind: 'key', key: e.key, pressed: true } });
  });
  canvas.addEventListener('keyup', e => {
    if (!viewer.control) return;
    e.preventDefault();
    sendJson({ type: 'input', stream_id: viewer.streamId, ev: { kind: 'key', key: e.key, pressed: false } });
  });
  canvas.addEventListener('blur', () => {
    if (!viewer.control) return;
    ['Control', 'Alt', 'Shift', 'Meta'].forEach(k =>
      sendJson({ type: 'input', stream_id: viewer.streamId, ev: { kind: 'key', key: k, pressed: false } }));
  });
  viewer.listenersAttached = true;
}
attachViewerListeners();

function btnName(b) { return b === 2 ? 'right' : b === 1 ? 'middle' : 'left'; }
function sendPointer(e) {
  const canvas = $('viewerCanvas');
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)) * viewer.frameW;
  const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height)) * viewer.frameH;
  sendJson({ type: 'input', stream_id: viewer.streamId, ev: { kind: 'mouse_move', x: Math.round(x), y: Math.round(y) } });
}

// ---------------------------------------------------------------------------
// اشتراک صفحه من (کنسول → کلاینت)
// ---------------------------------------------------------------------------
function shareScreenTo(client) {
  if (share.mediaStream) { toast('یک اشتراک صفحه در حال اجراست', 'warn'); return; }
  share.targetId = client.id;
  $('shareStatus').innerHTML = '<span class="spinner"></span><span>در انتظار تایید «' + escapeHtml((client.info && client.info.hostname) || client.id) + '»…</span>';
  $('sharePreview').srcObject = null;
  openModal('shareModal');
  sendJson({ type: 'share_screen_to', target: client.id });
}
async function startDisplayCapture() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
    const insecure = !window.isSecureContext;
    throw new Error(insecure
      ? 'مرورگر روی HTTP غیرامن اجازه گرفتن صفحه نمی‌دهد — سرور را با --ssl اجرا کنید یا از localhost باز کنید'
      : 'مرورگر از getDisplayMedia پشتیبانی نمی‌کند');
  }
  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: { frameRate: 10 }, audio: false,
  });
  share.mediaStream = stream;
  const video = $('sharePreview');
  video.srcObject = stream;
  share.video = video;
  $('shareStatus').innerHTML = '<span>🔴 در حال نمایش به کلاینت — ' + fmtFPS() + '</span>';
  // اگر کاربر در مرورگر اشتراک را قطع کرد
  stream.getVideoTracks()[0].addEventListener('ended', () => { stopMyShare(); closeModal('shareModal'); toast('اشتراک صفحه پایان یافت', 'warn'); });
  // حلقه ارسال فریم
  share.canvas = document.createElement('canvas');
  const cctx = share.canvas.getContext('2d');
  const sendLoop = async () => {
    if (!share.mediaStream || !share.streamId) return;
    const w = video.videoWidth, h = video.videoHeight;
    if (w && h) {
      const preset = QUALITY_PRESET[$('qualitySelect').value] || QUALITY_PRESET.medium;
      const scale = Math.min(1, 1600 / w) * preset.scale;
      share.canvas.width = Math.round(w * scale);
      share.canvas.height = Math.round(h * scale);
      cctx.drawImage(video, 0, 0, share.canvas.width, share.canvas.height);
      share.canvas.toBlob(async blob => {
        if (!blob || !ws || ws.readyState !== 1) return;
        const buf = await blob.arrayBuffer();
        sendBin(binMsg(0x01, share.streamId, { w: share.canvas.width, h: share.canvas.height, cx: -1, cy: -1 }, buf));
      }, 'image/jpeg', preset.jpg);
    }
  };
  share.sendTimer = setInterval(sendLoop, 120);
  toast('اشتراک صفحه آغاز شد', 'ok');
}
function fmtFPS() { return '~8fps'; }
function stopMyShare() {
  if (share.sendTimer) { clearInterval(share.sendTimer); share.sendTimer = null; }
  if (share.mediaStream) { share.mediaStream.getTracks().forEach(t => t.stop()); share.mediaStream = null; }
  if (share.streamId && ws && ws.readyState === 1) sendJson({ type: 'stop_share_to', stream_id: share.streamId });
  $('sharePreview').srcObject = null;
  share.streamId = 0;
}
$('stopShareBtn').addEventListener('click', () => { stopMyShare(); closeModal('shareModal'); });

// ---------------------------------------------------------------------------
// Binary dispatch
// ---------------------------------------------------------------------------
function handleBinary(data) {
  const parsed = parseBin(data);
  if (!parsed) return;
  if (parsed.type === 0x01) handleScreenFrame(parsed);
  else if (parsed.type === 0x02) handleFileChunk(parsed);
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
connect();
