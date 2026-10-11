'use strict';
/* Light Bridge — shared interface for Chihiros and HeliaLux lights. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pad2 = n => String(n).padStart(2, '0');
const fmt = t => `${pad2(Math.floor(t / 60) % 24)}:${pad2(Math.round(t) % 60)}`;
const parseHHMM = s => { const [h, m] = String(s || '0:0').split(':').map(Number); return ((h || 0) * 60 + (m || 0)) % 1440; };
const clone = o => JSON.parse(JSON.stringify(o));
const ORDER = ['w', 'r', 'g', 'b'];
const NAMES = { w: 'White', r: 'Red', g: 'Green', b: 'Blue' };
const LETTER = { w: 'W', r: 'R', g: 'G', b: 'B' };
const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const CLOUD_I = ['Off', 'Light', 'Medium', 'Strong'];
const CLOUD_M = ['Off', 'Slow', 'Medium', 'Fast'];
const sortCh = chs => ORDER.filter(c => (chs || []).includes(c));
const nowMinute = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); };
const todayIndex = () => (new Date().getDay() + 6) % 7;
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

/* ---------------- API ---------------- */
// Access key: phones get it once from the QR code on the PC (?k=…). The PC itself needs none.
const KEY = (() => {
  const fromUrl = new URLSearchParams(location.search).get('k');
  try { if (fromUrl) localStorage.setItem('lbKey', fromUrl); return fromUrl || localStorage.getItem('lbKey') || ''; }
  catch (e) { return fromUrl || ''; }
})();
async function api(path, body) {
  const headers = KEY ? { 'X-Light-Bridge-Key': KEY } : {};
  const opts = body === undefined ? { headers } : { method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (r.status === 401 && data.pair) { S.unpaired = true; render(true); }
  if (!r.ok) throw new Error(data.error || `Error ${r.status}`);
  return data;
}
async function act(promise, okMsg) {
  try { const r = await promise; if (okMsg) toast(okMsg); await poll(); return r; }
  catch (e) { toast(e.message, true); return null; }
}

/* ---------------- Curves (same rules as the bridge) ---------------- */
function sample(points, minute, chs) {
  const pts = [...points].sort((a, b) => a.t - b.t);
  const out = {};
  if (!pts.length) { chs.forEach(c => { out[c] = 0; }); return out; }
  if (pts.length === 1) { chs.forEach(c => { out[c] = pts[0].v[c] || 0; }); return out; }
  minute = ((minute % 1440) + 1440) % 1440;
  let prev = null, next = null;
  for (const p of pts) { if (p.t <= minute) prev = p; else { next = p; break; } }
  if (!prev) prev = pts[pts.length - 1];
  if (!next) next = pts[0];
  const span = ((next.t - prev.t) % 1440 + 1440) % 1440 || 1440;
  const pos = ((minute - prev.t) % 1440 + 1440) % 1440;
  chs.forEach(c => { out[c] = Math.round((prev.v[c] || 0) + ((next.v[c] || 0) - (prev.v[c] || 0)) * pos / span); });
  return out;
}
function mixCss(v) {
  const w = v.w || 0, R = (v.r || 0) + 0.55 * w, G = (v.g || 0) + 0.55 * w, B = (v.b || 0) + 0.6 * w;
  const m = Math.max(R, G, B);
  if (m <= 0) return 'rgb(0,0,0)';
  const bright = Math.pow(Math.max(v.r || 0, v.g || 0, v.b || 0, w) / 100, 0.5);
  const f = x => Math.round(255 * (x / m) * bright);
  return `rgb(${f(R)},${f(G)},${f(B)})`;
}
function gradient(points, chs) {
  const stops = [];
  for (let i = 0; i <= 48; i++) stops.push(`${mixCss(sample(points, Math.min(1439, i * 30), chs))} ${(i / 48 * 100).toFixed(2)}%`);
  return `linear-gradient(90deg, ${stops.join(',')})`;
}
const levelsHtml = (lv, chs) => sortCh(chs).map(c => `<span class="lv ${c}">${LETTER[c]} ${lv[c] ?? 0}</span>`).join('');
const valsText = (lv, chs) => sortCh(chs).map(c => `${LETTER[c]} <b>${lv[c] ?? 0}</b>`).join(' · ');

/* ---------------- Toast & sheet ---------------- */
let toastTimer;
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.classList.toggle('err', !!err); t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), err ? 4500 : 2400);
}
let sheetClose = null;
function openSheet(html, onClose) {
  const body = document.createElement('div'); body.innerHTML = html;
  $('#sheetBody').replaceChildren(body);
  $('#scrim').classList.add('open'); $('#sheet').classList.add('open');
  sheetClose = onClose || null;
  return body;
}
function closeSheet() {
  $('#scrim').classList.remove('open'); $('#sheet').classList.remove('open');
  const cb = sheetClose; sheetClose = null; if (cb) cb();
}
$('#scrim').addEventListener('click', closeSheet);
function confirmSheet(title, text, ok, danger) {
  return new Promise(res => {
    let done = false;
    const b = openSheet(`<h3>${esc(title)}</h3><p class="muted">${text}</p><div class="btn-row"><button class="btn" data-a="no">Cancel</button><button class="btn ${danger ? 'danger' : 'primary'}" data-a="yes">${esc(ok)}</button></div>`, () => { if (!done) res(false); });
    b.addEventListener('click', e => { const a = e.target.closest('[data-a]')?.dataset.a; if (!a) return; done = true; closeSheet(); res(a === 'yes'); });
  });
}

/* ---------------- State, routing, polling ---------------- */
const S = { state: null, route: { name: 'home' }, ed: null, minutes: 60, dragging: false, rendered: '', unreachable: false, stopped: false, templates: null, unpaired: false };
const light = id => S.state?.lights.find(l => l.id === id);
const mine = () => (S.state?.lights || []).filter(l => l.adopted !== false);      // confirmed lights
const nearby = () => (S.state?.lights || []).filter(l => l.adopted === false);    // seen nearby, not confirmed
function parseRoute() {
  const p = location.hash.replace(/^#\/?/, '').split('/');
  if (p[0] === 'light' && p[1]) return { name: 'light', id: decodeURIComponent(p[1]), tab: p[2] || 'now' };
  if (p[0] === 'settings') return { name: 'settings' };
  return { name: 'home' };
}
window.addEventListener('hashchange', async e => {
  const prev = S.route, next = parseRoute();
  const leavingEdit = prev.name === 'light' && prev.tab === 'program' && S.ed?.dirty
    && !(next.name === 'light' && next.id === prev.id && next.tab === 'program');
  if (leavingEdit) {                      // unsaved day program: ask before throwing it away
    const target = location.hash || '#/';
    history.replaceState(null, '', `#/light/${encodeURIComponent(prev.id)}/program`);
    if (!await confirmSheet('Discard your changes?', 'This day program has changes that are not saved yet.', 'Discard', true)) return;
    S.ed.dirty = false; location.hash = target; return;
  }
  S.route = next;
  closeSheet();
  if (prev.name === 'light' && prev.tab === 'program') stopPreview(prev.id);
  if (S.route.name === 'light' && S.route.tab === 'program') loadEditor(S.route.id);
  render(true); window.scrollTo(0, 0);
});
async function poll() {
  try { S.state = await api('/api/state'); S.unreachable = false; S.unpaired = false; }
  catch (e) { if (!S.unpaired) S.unreachable = true; }
  render(false);
}
setInterval(() => { if (document.visibilityState === 'visible' && !S.stopped) poll(); }, 2500);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') poll(); });
$('#backBtn').addEventListener('click', () => { location.hash = '#/'; });
$('#settingsBtn').addEventListener('click', () => { location.hash = '#/settings'; });

/* ---------------- Render dispatcher ---------------- */
function render(full) {
  renderHeader();
  const app = $('#app');
  if (S.stopped) { app.innerHTML = stoppedHtml(); return; }
  if (S.unpaired) { app.innerHTML = unpairedHtml(); S.rendered = ''; return; }
  if (!S.state) { app.innerHTML = S.unreachable ? unreachableHtml() : `<div class="empty"><div class="spinner"></div><p class="muted">Connecting to the bridge…</p></div>`; S.rendered = ''; return; }
  const r = S.route;
  const key = r.name === 'light' ? `light:${r.id}:${r.tab}:${!!light(r.id)}:${light(r.id)?.adopted}:${S.ed?.loaded ? S.ed.slot : '-'}`
    : r.name === 'home' ? `home:${S.state.lights.map(l => l.id + (l.adopted === false ? '?' : '')).join(',')}` : r.name;
  const notice = S.unreachable ? unreachableHtml(true) : '';
  if (!full && key === S.rendered) { live(); setNotice(notice); return; }
  S.rendered = key;
  app.className = r.name === 'home' ? 'wide' : '';
  let html = '';
  if (r.name === 'home') html = homeHtml();
  else if (r.name === 'settings') html = settingsHtml();
  else if (!light(r.id)) html = `<div class="card">This light is no longer known. <a href="#/">Back to overview</a></div>`;
  else if (light(r.id).adopted === false) html = `<div class="card">First confirm on the overview that this light is yours. <a href="#/">Back to overview</a></div>`;
  else html = tabsHtml(r) + (r.tab === 'program' ? programHtml() : r.tab === 'settings' ? lightSettingsHtml() : nowHtml());
  app.innerHTML = `<div id="topNotice">${notice}</div>${html}`;
  afterRender();
  live();
}
function setNotice(html) { const n = $('#topNotice'); if (n && n.innerHTML !== html) n.innerHTML = html; }
function live() {
  const r = S.route;
  if (r.name === 'home') liveHome();
  else if (r.name === 'light' && light(r.id)) {
    if (r.tab === 'now') liveNow();
    if (r.tab === 'program') liveProgram();
  } else if (r.name === 'settings') liveSettings();
}
function afterRender() {
  const r = S.route;
  if (r.name === 'light' && r.tab === 'now') bindNow();
  if (r.name === 'light' && r.tab === 'program') bindProgram();
}

function renderHeader() {
  const r = S.route, st = S.state;
  $('#backBtn').classList.toggle('hidden', r.name === 'home');
  let title = 'Light Bridge', sub = 'Connecting…';
  if (st) {
    const my = mine(), n = my.length, rooms = new Set(my.map(l => l.room || '?')).size;
    const searching = st.bridge.ble.scanning || st.bridge.lan.scanning;
    sub = `${n} light${n === 1 ? '' : 's'}${n ? ` · ${rooms} room${rooms === 1 ? '' : 's'}` : ''}${searching ? ' · searching…' : ''}${st.bridge.demo ? ' · DEMO' : ''}`;
  }
  if (r.name === 'settings') { title = 'Bridge settings'; sub = st ? `Version ${st.bridge.version}` : ''; }
  if (r.name === 'light' && light(r.id) && light(r.id).adopted !== false) { const l = light(r.id); title = l.name; sub = [l.room, l.kind_label].filter(Boolean).join(' · '); }
  if (S.unreachable) sub = 'Bridge not reachable';
  if (S.unpaired) sub = 'Not connected to the bridge yet';
  $('#title').textContent = title; $('#subtitle').textContent = sub;
}
const unreachableHtml = small => `<div class="notice warn"><b>Can't reach the bridge.</b>Is the PC switched on and the Light Bridge window open? ${small ? 'Showing the last known state.' : ''}</div>`;
const unpairedHtml = () => `<div class="card empty"><h2>Connect this device once</h2>
  <p>For safety, Light Bridge only answers devices that have its code.</p>
  <p>On the PC, open Light Bridge and scan the code under <b>“Use it on your iPhone”</b> with this device's camera. Then open the link.</p>
  <p class="muted small">Other devices in the same network (for example at work) cannot use the bridge without this code.</p></div>`;
const stoppedHtml = () => `<div class="card empty"><h2>Bridge stopped</h2><p>The lights keep running the programs stored in them.</p><p class="muted small">To start again, double-click “Start-Light-Bridge” on the PC.</p></div>`;

/* ---------------- Mode texts ---------------- */
const stored = l => l.kind === 'chihiros';            // lights that store their program themselves
const untilText = l => { if (!l.manual_until) return ''; const d = new Date(l.manual_until * 1000); return fmt(d.getHours() * 60 + d.getMinutes()); };
const waiting = l => (stored(l) && l.info.program_pending ? ` <span class="tag warn">${l.online ? 'storing…' : 'change waiting'}</span>` : '');
const LEFT_IN = { manual: 'Still in manual light', test: 'Curve test not finished', erased: 'Program only partly stored' };
const leftIn = l => (stored(l) && !l.online && l.info && LEFT_IN[l.info.left_in]) ? l.info.left_in : null;
function modeText(l) {
  if (leftIn(l)) return `<b>${LEFT_IN[leftIn(l)]}</b> · fixed as soon as the PC is near the light`;
  if (stored(l) && l.info.test) return '<b>Curve test running</b> · watch the light';
  if (!l.online && !stored(l)) return `<span class="muted">${esc(l.status_text)}</span>`;
  const away = !l.online ? ' <span class="muted">(switches back when the PC is near)</span>' : '';
  if (l.preview) return '<b>Preview</b> · showing a test colour';
  const near = stored(l) && l.online ? ' · keep the PC near until then' : '';
  if (l.mode === 'maintenance') return `<b>Maintenance light</b> until ${untilText(l)}${near}${away}`;
  if (l.mode === 'manual') return `<b>Manual light</b> until ${untilText(l)}${near}${away}`;
  return `Day program <b>${esc(l.active_name)}</b>${l.next_change != null ? ` · next phase ${fmt(l.next_change)}` : ''}${waiting(l)}`;
}
function modeDetail(l) {
  if (leftIn(l)) return `${LEFT_IN[leftIn(l)]} · fixed as soon as the PC is near the light`;
  if (stored(l) && l.info.test) return 'Curve test running';
  if (!l.online && !stored(l)) return esc(l.status_text);
  if (l.preview) return 'Showing a test colour';
  if (l.mode !== 'program') return `Back to the day program at ${untilText(l)}`;
  return `<b>${esc(l.active_name)}</b>${l.next_change != null ? ` · next phase ${fmt(l.next_change)}` : ''}${waiting(l)}`;
}
function signalText(rssi) {
  if (rssi == null) return '<span class="muted">not measured yet</span>';
  const q = rssi >= -70 ? ['good', ''] : rssi >= -85 ? ['fair', 'warn'] : ['weak — move the PC closer', 'warn'];
  return `<span class="${q[1] ? 'tag warn' : 'tag'}">${q[0]}</span> <span class="muted small">${rssi} dBm</span>`;
}
function statusLine(l) {
  if (leftIn(l)) return `<span class="status connecting"><i></i>Out of range · ${LEFT_IN[leftIn(l)].toLowerCase()}</span>`;
  if (stored(l) && l.connection === 'offline') return '<span class="status away"><i></i>Out of range · runs its own program</span>';
  const how = l.kind === 'chihiros' ? 'Bluetooth' : 'Wi-Fi';
  const txt = l.connection === 'connected' ? `${how} connected` : l.connection === 'connecting' ? 'Connecting…' : 'Not reachable';
  return `<span class="status ${l.connection}"><i></i>${txt}</span>`;
}

/* ---------------- Home ---------------- */
function nearbyHtml(list, first) {
  if (!list.length) return '';
  let html = `<div class="card new-lights"><h2>${list.length === 1 ? 'New light found nearby' : `${list.length} new lights found nearby`}</h2>
    <p class="small">${first ? 'Is this your light? ' : ''}Tap <b>Flash</b>: the light blinks blue three times and then runs the program stored in it. Nothing is stored in a light until you tap <b>This is mine</b>.</p>`;
  list.forEach(l => {
    const id = esc(l.id);
    html += `<div class="new-light"><div class="row"><div class="grow"><div class="name">${esc(l.name)}</div>
        <div class="small muted">Chihiros · signal ${signalText(l.info.rssi)}</div></div></div>
      <div class="btn-row"><button class="btn small" data-act="identify" data-id="${id}">Flash</button>
        <button class="btn small primary" data-act="adopt" data-id="${id}">This is mine</button>
        <button class="btn small" data-act="ignore" data-id="${id}">Not mine</button></div></div>`;
  });
  return html + `<p class="small muted" style="margin-top:10px">Not sure? Flash each light and watch. Lights you mark as “Not mine” (for example a neighbour's) are left alone.</p></div>`;
}
function homeHtml() {
  const st = S.state, lights = mine(), news = nearby();
  let html = st.bridge.demo ? `<div class="notice"><b>Demo mode</b>The lights below are simulated. Nothing is sent to real lights.</div>` : '';
  if (st.bridge.notice) html += `<div class="notice warn"><b>Please note</b>${esc(st.bridge.notice)}</div>`;
  html += nearbyHtml(news, !lights.length);
  if (!lights.length && news.length) return html + qrCardHtml(true);
  if (!lights.length) {
    html += `<div class="card empty"><div class="spinner"></div><h2>Looking for your lights…</h2>
      <p>Switch the lights on and take the PC to within a few metres of a light. Each light appears as soon as the PC is near it.</p>
      <div class="btn-row"><button class="btn primary" data-act="scan">Search again</button></div>
      <p class="small muted" style="margin-top:12px">${esc(st.bridge.ble.message || '')} ${esc(st.bridge.lan.message || '')}</p></div>`;
    return html + qrCardHtml(true);
  }
  const rooms = {};
  lights.forEach(l => { (rooms[l.room || ''] = rooms[l.room || ''] || []).push(l); });
  const names = Object.keys(rooms).sort((a, b) => (a === '') - (b === '') || a.localeCompare(b));
  html += '<div class="grid2">';
  names.forEach(room => {
    html += `<section><h3 class="room">${room ? esc(room) : 'No room yet — tap a light to name it'}</h3>`;
    rooms[room].forEach(l => { html += lightCardHtml(l); });
    html += '</section>';
  });
  return html + '</div>' + qrCardHtml(true);
}
function lightCardHtml(l) {
  return `<div class="card light-card" data-go="#/light/${encodeURIComponent(l.id)}" data-id="${esc(l.id)}">
    <div class="row"><div class="swatch" data-f="sw"></div><div class="grow"><div class="name">${esc(l.name)}</div>
      <div class="small muted">${esc(l.kind_label)}</div><div data-f="status"></div></div><span class="muted" aria-hidden="true">›</span></div>
    <div class="modeline" data-f="mode"></div>
    <div class="strip" data-f="strip"><div class="now"></div></div>
    <div class="btn-row" style="margin-top:12px" data-f="actions"></div></div>`;
}
function liveHome() {
  mine().forEach(l => {
    const card = $(`.light-card[data-id="${CSS.escape(l.id)}"]`);
    if (!card) { S.rendered = ''; return; }
    card.querySelector('[data-f=sw]').style.background = mixCss(l.levels);
    card.querySelector('[data-f=status]').innerHTML = statusLine(l);
    card.querySelector('[data-f=mode]').innerHTML = modeText(l);
    const strip = card.querySelector('[data-f=strip]');
    strip.style.background = gradient(l.lamp_points || l.today_points, l.channels);
    strip.querySelector('.now').style.left = `calc(${(nowMinute() / 1440 * 100).toFixed(2)}% - 1px)`;
    const acts = l.mode === 'program' && !l.preview
      ? `<button class="btn small" data-act="maint" data-id="${esc(l.id)}">Maintenance light</button><a class="btn small" href="#/light/${encodeURIComponent(l.id)}">Adjust</a>`
      : `<button class="btn small primary" data-act="program" data-id="${esc(l.id)}">Back to day program</button>`;
    const box = card.querySelector('[data-f=actions]');
    if (box.innerHTML !== acts) box.innerHTML = acts;
  });
}
function qrCardHtml(deskOnly) {
  const url = S.state.bridge.urls[0];
  if (!url) return '';
  let qr = '';
  try { qr = QR.svg(url, 168); } catch (e) { qr = ''; }
  return `<div class="card ${deskOnly ? 'desk-only' : ''}" style="margin-top:18px"><h2>Use it on your iPhone</h2>
    <div class="row wrap" style="align-items:flex-start;gap:18px"><div class="qr">${qr}</div>
    <div style="flex:1 1 230px;min-width:0"><p class="mono" style="font-size:15px;word-break:break-all">${esc(url)}</p><ol class="steps small">
      <li>Connect the iPhone to the same Wi-Fi as this PC.</li><li>Point the iPhone camera at the code and tap the link.</li>
      <li>In Safari tap <b>Share</b> → <b>Add to Home Screen</b>. You now have a “Lights” icon.</li></ol></div></div></div>`;
}

/* ---------------- Light page: tabs ---------------- */
function tabsHtml(r) {
  const base = `#/light/${encodeURIComponent(r.id)}`;
  const t = (k, label) => `<a href="${base}/${k}" class="${r.tab === k ? 'on' : ''}">${label}</a>`;
  return `<nav class="tabs">${t('now', 'Now')}${t('program', 'Day program')}${t('settings', 'Settings')}</nav>`;
}

/* ---------------- Now tab ---------------- */
function nowHtml() {
  const l = light(S.route.id), chs = sortCh(l.channels);
  const chip = (m, label) => `<button class="chip ${S.minutes === m ? 'on' : ''}" data-min="${m}">${label}</button>`;
  let extra = '';
  if (l.kind === 'chihiros') {
    extra = `<div class="card"><h2>Bluetooth</h2><dl class="kv"><dt>Status</dt><dd data-f="conn"></dd><dt>Signal</dt><dd data-f="rssi"></dd><dt>Address</dt><dd class="mono">${esc(l.info.address || '–')}</dd></dl>
      <div class="btn-row" style="margin-top:12px"><button class="btn" data-act="clock" data-id="${esc(l.id)}">Set the light's clock</button></div>
      <p class="small muted" style="margin-top:10px">The clock is also set automatically every time the bridge connects, for example after a power cut.</p></div>`;
  } else {
    extra = `<div class="card"><h2>Wi-Fi</h2><dl class="kv"><dt>Status</dt><dd data-f="conn"></dd><dt>Address</dt><dd class="mono">${esc(l.info.ip || '–')}</dd><dt>Light's clock</dt><dd data-f="clock"></dd></dl></div>`;
  }
  return `<div class="card"><div class="row"><div class="swatch lg" data-f="sw"></div><div class="grow">
      <div style="font-weight:700;font-size:18px" data-f="title"></div><div class="small muted" data-f="sub"></div></div></div>
      <div class="levels" style="margin-top:12px" data-f="levels"></div>
      <div class="btn-row" style="margin-top:14px" data-f="actions"></div></div>
    <div class="card"><h2>Manual light</h2>
      <p class="small muted">Move a slider and the light changes right away. It goes back to the day program after the time below.</p>
      ${stored(l) ? '<p class="small notice" style="margin:0 0 6px">Manual light is the only thing that needs this PC nearby. If you take the PC away before the time is up, the light keeps the manual colour until the PC is back. <b style="display:inline">Tip:</b> tap “Back to day program” before you leave.</p>' : ''}
      ${chs.map(c => `<div class="ch ${c}"><label for="m-${c}">${LETTER[c]}</label><input type="range" min="0" max="100" step="1" id="m-${c}" data-ch="${c}" aria-label="${NAMES[c]}"><output id="o-${c}">0</output></div>`).join('')}
      <div class="field"><span>Back to the day program after</span><div class="chips" id="durChips">${chip(30, '30 min')}${chip(60, '1 hour')}${chip(180, '3 hours')}${chip(720, '12 hours')}</div></div>
    </div>${extra}`;
}
function liveNow() {
  const l = light(S.route.id); if (!l) return;
  const set = (f, html) => { const el = $(`[data-f=${f}]`); if (el && el.innerHTML !== html) el.innerHTML = html; };
  $('[data-f=sw]').style.background = mixCss(l.levels);
  set('title', l.online || stored(l) ? (l.preview ? 'Preview' : l.mode === 'program' ? 'Day program' : l.mode === 'maintenance' ? 'Maintenance light' : 'Manual light') : 'Not reachable');
  set('sub', modeDetail(l));
  set('levels', levelsHtml(l.levels, l.channels));
  const id = esc(l.id);
  let acts = `<button class="btn" data-act="maint" data-id="${id}">Maintenance light</button><button class="btn" data-act="off" data-id="${id}">Lights off (1 h)</button>`;
  if (l.mode !== 'program' || l.preview) acts = `<button class="btn primary" data-act="program" data-id="${id}">Back to day program</button>` + acts;
  set('actions', acts);
  set('conn', statusLine(l));
  if (l.kind === 'chihiros') set('rssi', signalText(l.info.rssi));
  if (l.kind === 'helialux') {
    const off = l.info.device_time_offset;
    set('clock', off == null ? '–' : Math.abs(off) <= 2 ? 'correct' : `<span class="tag warn">${off > 0 ? '+' : ''}${off} min</span> set the time on the HeliaLux`);
  }
  if (!S.dragging) sortCh(l.channels).forEach(c => { const r = $(`#m-${c}`); if (r) { r.value = l.levels[c] ?? 0; $(`#o-${c}`).textContent = r.value; } });
}
function bindNow() {
  const l = light(S.route.id);
  const send = debounce(() => {
    const levels = {}; sortCh(l.channels).forEach(c => { levels[c] = Number($(`#m-${c}`).value); });
    api(`/api/lights/${encodeURIComponent(l.id)}/manual`, { levels, minutes: S.minutes }).then(poll).catch(e => toast(e.message, true));
  }, 250);
  $$('#app input[type=range][data-ch]').forEach(r => {
    r.addEventListener('pointerdown', () => { S.dragging = true; });
    r.addEventListener('input', () => { S.dragging = true; $(`#o-${r.dataset.ch}`).textContent = r.value; send(); });
    r.addEventListener('change', () => { setTimeout(() => { S.dragging = false; }, 1500); });
  });
  $('#durChips').addEventListener('click', e => {
    const b = e.target.closest('[data-min]'); if (!b) return;
    S.minutes = Number(b.dataset.min);
    $$('#durChips .chip').forEach(x => x.classList.toggle('on', x === b));
    const cur = light(S.route.id);
    if (cur && cur.mode === 'manual') send();
  });
}

/* ---------------- Day program tab ---------------- */
async function loadEditor(id) {
  S.ed = { id, loaded: false };
  try {
    const d = await api(`/api/lights/${encodeURIComponent(id)}/profiles`);
    const slot = d.active_slot || 0;
    S.ed = { id, loaded: true, profiles: d.profiles, weekdays: d.weekdays, caps: d.caps, channels: sortCh(d.channels),
      slot, draft: clone(d.profiles[slot]), dirty: false, scrub: Math.floor(nowMinute() / 5) * 5, scrubLive: false, previewing: false };
  } catch (e) { toast(e.message, true); }
  render(true);
}
function programHtml() {
  const ed = S.ed;
  if (!ed || !ed.loaded || ed.id !== S.route.id) return `<div class="empty"><div class="spinner"></div><p class="muted">Loading day programs…</p></div>`;
  const l = light(ed.id), today = todayIndex();
  const opts = ed.profiles.map((p, i) => `<option value="${i}" ${i === ed.slot ? 'selected' : ''}>${i + 1} · ${esc(p.name)}${ed.weekdays[today] === i ? ' (runs today)' : ''}</option>`).join('');
  const days = DAYS.map((d, i) => `<button data-day="${i}" class="${ed.weekdays[i] === ed.slot ? 'on' : ''} ${i === today ? 'today' : ''}" ${ed.caps.weekdays_editable ? '' : 'disabled'}>${d}</button>`).join('');
  const dayNote = stored(l) ? (ed.caps.lamp_mode === 'curve' ? 'With full curves, the light runs one program every day.' : 'Tap a day to use this program on that day.')
    : ed.caps.weekdays_editable ? 'Tap a day to use this program on that day.'
    : (l.info.weekdays_known ? 'Weekdays are changed on the HeliaLux itself (menu “Weekdays”).' : 'The weekday plan could not be read from the HeliaLux.');
  return `<div class="card">
      <label class="field" style="margin-top:0"><span>Day program</span><select id="slotSel">${opts}</select></label>
      <label class="field"><span>Name</span><input type="text" id="progName" maxlength="20" value="${esc(ed.draft.name)}"></label>
      <div class="field"><span>Used on</span><div class="days" id="days">${days}</div><p class="small muted" style="margin:8px 0 0">${dayNote}</p></div>
    </div>
    ${storedHtml(ed, l)}
    <div class="card">
      <div class="strip tall" id="edStrip"><div class="now"></div><div class="mark" id="edMark"></div></div>
      <div class="axis"><span>0:00</span><span>6:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
      <svg class="chart" id="edChart" viewBox="0 0 1440 110" preserveAspectRatio="none" role="img" aria-label="Colour levels over the day"></svg>
      <div class="legend">${ed.channels.map(c => `<span><i style="background:var(--ch-${c})"></i>${NAMES[c]}</span>`).join('')}</div>
      <hr>
      <div class="scrub"><div class="row between"><b>Check a time of day</b><span class="mono" id="scrubTime"></span></div>
        <input type="range" min="0" max="1435" step="5" id="scrub" aria-label="Time of day">
        <div class="row"><span class="swatch sm" id="scrubSw"></span><span class="small muted" id="scrubVals"></span></div>
        <label class="toggle"><span>Show it on the light</span><input type="checkbox" id="scrubLive" ${ed.scrubLive ? 'checked' : ''}></label></div>
    </div>
    <div class="card">
      <div class="row between"><h2 style="margin:0">Phases</h2><span class="count" id="phaseCount"></span></div>
      <p class="small muted" style="margin-top:8px">Each phase sets the colours the light reaches at that time. In between, the light fades smoothly. Tap a phase to change it.</p>
      <ul class="phases" id="phases"></ul>
      <div class="btn-row" style="margin-top:14px"><button class="btn" data-act="templates">Start from a template</button><button class="btn" data-act="copyto">Copy to another light</button></div>
    </div>
    <div class="savebar ${ed.dirty ? '' : 'hidden'}" id="savebar"><span>Unsaved changes</span><button class="btn small" data-act="discard">Discard</button>
      <button class="btn small primary" data-act="save">${ed.caps.profiles_on_device || ed.caps.program_on_device ? 'Save to light' : 'Save'}</button></div>`;
}
function storedHtml(ed, l) {
  if (!stored(l)) return '';
  const c = ed.caps, id = esc(l.id), curve = c.lamp_mode === 'curve', t = c.curve_tested;
  const when = t ? new Date(t.at * 1000).toLocaleDateString() : '';
  let test;
  if (curve) {
    test = `<p class="small" style="margin-top:0"><span class="tag">full curves on</span> The light stores this curve with all phases (up to 20)${t && t.copied_from ? `, tested on ${esc(t.copied_from)}` : ''}.</p>
      ${ed.weekdays[0] !== ed.slot ? `<p class="small">This program is not the one in the light.</p><button class="btn small primary" data-act="runonlight">Put this program into the light</button> ` : ''}
      <button class="btn small" data-act="lampsimple" data-id="${id}">Use the simple version instead</button>`;
  } else if (t && !t.ok) {
    test = `<p class="small muted" style="margin-top:0">Full curves were tested on ${when} and did not work on this light. It keeps the simple version.</p>
      <button class="btn small" data-act="curvetest" data-id="${id}">Test again</button>`;
  } else {
    test = `<p class="small" style="margin-top:0">Should the light store your whole curve, with every phase, breaks and moonlight? A short test (about 5 minutes, PC near the light) shows whether it can.</p>
      <button class="btn small primary" data-act="curvetest" data-id="${id}">Test full curves · 5 min</button>`;
  }
  return `<div class="card"><h2>Stored in the light</h2>
    <p class="small" style="margin-top:0">The light runs its program on its own, also when the PC is away.${curve ? '' : ' Right now it stores a simple version of this program:'}</p>
    ${curve ? '' : '<div class="strip" id="lampStrip"></div><div class="axis"><span>0:00</span><span>6:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div><p class="small muted" id="lampText" style="margin:8px 0 0"></p>'}
    ${l.info.program_pending ? (l.online
      ? '<p class="small" style="margin-top:8px"><span class="tag warn">storing</span> The last change is being stored in the light. Keep the PC near for a few seconds.</p>'
      : '<p class="small" style="margin-top:8px"><span class="tag warn">waiting</span> The last change is stored as soon as the PC is near the light again.</p>') : ''}
    <hr>${test}</div>`;
}
function deriveBasic(points, chs) {      // same rules as curves.derive_basic_schedule on the bridge
  const step = 5, samples = [];
  for (let m = 0; m < 1440; m += step) samples.push([m, sample(points, m, chs)]);
  const sum = v => chs.reduce((a, c) => a + (v[c] || 0), 0);
  const total = samples.map(x => sum(x[1])), peak = Math.max(0, ...total);
  if (peak <= 0) return null;
  const lit = total.map(t => t >= peak * 0.25), n = lit.length;
  let first = 0, last = n - 1, cut = false;
  if (!lit.every(Boolean)) {
    const dark = lit.indexOf(false);
    let bestLen = 0, bestStart = 0, runStart = null, runLen = 0;
    for (let k = 1; k <= n; k++) {
      const i = (dark + k) % n;
      if (lit[i]) { if (runStart === null) { runStart = i; runLen = 0; } runLen++; if (runLen > bestLen) { bestLen = runLen; bestStart = runStart; } }
      else runStart = null;
    }
    first = bestStart; last = (bestStart + bestLen - 1) % n;
    if (bestStart + bestLen > n) {
      cut = true;
      const before = n - bestStart, after = bestLen - before;
      if (before >= after) last = n - 1; else first = 0;
    }
  }
  const on = first * step, off = Math.min(1435, last * step + step);
  const win = samples.map((x, i) => [x[0], total[i], x[1]]).filter(x => x[0] >= on && x[0] < off);
  const top = Math.max(...win.map(x => x[1]));
  const levels = (win.find(x => x[1] === top) || [0, 0, samples[0][1]])[2];
  const reach = (win.find(x => x[1] >= top * 0.9) || [on])[0];
  return { on, off, ramp: Math.max(0, Math.min(150, reach - on, Math.floor((off - on) / 2))), levels, cut };
}
function simplePoints(b, chs) {
  const z = {}, lv = {};
  chs.forEach(c => { z[c] = 0; lv[c] = b.levels[c] || 0; });
  const ramp = Math.max(1, b.ramp), full = Math.min(b.on + ramp, b.off - 1), dim = Math.max(b.off - ramp, full + 1);
  const raw = [{ t: 0, v: z }, { t: b.on, v: z }, { t: full, v: lv }, { t: Math.min(dim, b.off - 1), v: lv }, { t: b.off, v: z }];
  const seen = new Set();
  return raw.sort((a, c) => a.t - c.t).filter(p => (seen.has(p.t) ? false : seen.add(p.t)));
}
function drawProgram() {
  const ed = S.ed; if (!ed?.loaded) return;
  const pts = ed.draft.points, chs = ed.channels;
  const strip = $('#edStrip'); if (!strip) return;
  strip.style.background = gradient(pts, chs);
  strip.querySelector('.now').style.left = `calc(${(nowMinute() / 1440 * 100).toFixed(2)}% - 1px)`;
  $('#edMark').style.left = `calc(${(ed.scrub / 1440 * 100).toFixed(2)}% - 1px)`;
  const y = v => 105 - v;
  let svg = '';
  [0, 360, 720, 1080, 1440].forEach(g => { svg += `<line class="grid" x1="${g}" x2="${g}" y1="0" y2="110"/>`; });
  svg += `<line class="grid" x1="0" x2="1440" y1="105" y2="105"/><line class="grid" x1="0" x2="1440" y1="55" y2="55" opacity=".5"/>`;
  chs.forEach(c => {
    const xs = [...new Set([0, 1439, ...pts.map(p => p.t)])].sort((a, b) => a - b);
    svg += `<polyline class="line" stroke="var(--ch-${c})" points="${xs.map(t => `${t},${y(sample(pts, t, chs)[c])}`).join(' ')}"/>`;
  });
  svg += `<line class="nowline" x1="${nowMinute()}" x2="${nowMinute()}" y1="0" y2="110"/><line class="scrubline" x1="${ed.scrub}" x2="${ed.scrub}" y1="0" y2="110"/>`;
  $('#edChart').innerHTML = svg;
  const v = sample(pts, ed.scrub, chs);
  $('#scrub').value = ed.scrub; $('#scrubTime').textContent = fmt(ed.scrub);
  $('#scrubSw').style.background = mixCss(v); $('#scrubVals').innerHTML = valsText(v, chs);
  const max = ed.caps.max_points;
  $('#phaseCount').textContent = `${pts.length} / ${max}`;
  const canAdd = pts.length < max;
  $('#phases').innerHTML = pts.map((p, i) => `<li class="phase" data-i="${i}"><span class="t">${fmt(p.t)}</span><span class="swatch sm" style="background:${mixCss(p.v)}"></span>
      <span class="v">${i === 0 ? '<span class="muted">Start · </span>' : ''}${valsText(p.v, chs)}</span>
      <span>${ed.caps.clouds && p.ci > 0 ? '<span class="tag">clouds</span>' : '<span class="muted">›</span>'}</span></li>
      ${canAdd ? `<li class="insert"><button data-insert="${i}">+ Add phase</button></li>` : ''}`).join('');
  $('#savebar').classList.toggle('hidden', !ed.dirty);
  const ls = $('#lampStrip');
  if (ls) {
    const b = deriveBasic(pts, chs);
    ls.style.background = b ? gradient(simplePoints(b, chs), chs) : '#000';
    $('#lampText').innerHTML = b ? `On at <b>${fmt(b.on)}</b>, full at <b>${fmt(b.on + b.ramp)}</b>, off at <b>${fmt(b.off)}</b>. One colour mix for the whole day; moonlight and breaks are left out.${b.cut ? ' <b>The light part after midnight is left out</b> (the light’s own program ends at midnight).' : ''}`
      : 'Dark all day.';
  }
}
function liveProgram() { if (S.ed?.loaded) { const s = $('#edStrip .now'); if (s) s.style.left = `calc(${(nowMinute() / 1440 * 100).toFixed(2)}% - 1px)`; } }
const sendPreview = debounce((id, levels) => { api(`/api/lights/${encodeURIComponent(id)}/preview`, { levels }).catch(e => toast(e.message, true)); }, 150);
function stopPreview(id) {
  if (S.ed && S.ed.previewing) { S.ed.previewing = false; api(`/api/lights/${encodeURIComponent(id || S.ed.id)}/preview`, { stop: true }).catch(() => {}); }
}
function markDirty() { S.ed.dirty = true; drawProgram(); }
function bindProgram() {
  const ed = S.ed; if (!ed?.loaded) return;
  drawProgram();
  $('#slotSel').addEventListener('change', async e => {
    const to = Number(e.target.value);
    if (ed.dirty && !(await confirmSheet('Discard changes?', 'You changed this program but did not save it.', 'Discard', true))) { e.target.value = ed.slot; return; }
    ed.slot = to; ed.draft = clone(ed.profiles[to]); ed.dirty = false; render(true);
  });
  $('#progName').addEventListener('input', e => { ed.draft.name = e.target.value; ed.dirty = true; $('#savebar').classList.remove('hidden'); });
  $('#days').addEventListener('click', async e => {
    const b = e.target.closest('[data-day]'); if (!b || !ed.caps.weekdays_editable) return;
    const d = Number(b.dataset.day), days = [...ed.weekdays];
    days[d] = days[d] === ed.slot ? 0 : ed.slot;
    const r = await act(api(`/api/lights/${encodeURIComponent(ed.id)}/weekdays`, { weekdays: days }), `${DAYS[d]}: program ${days[d] + 1}`);
    if (r) { ed.weekdays = days; render(true); }
  });
  $('#scrub').addEventListener('input', e => {
    ed.scrub = Number(e.target.value); drawProgram();
    if (ed.scrubLive) { ed.previewing = true; sendPreview(ed.id, sample(ed.draft.points, ed.scrub, ed.channels)); }
  });
  $('#scrubLive').addEventListener('change', e => {
    ed.scrubLive = e.target.checked;
    if (ed.scrubLive) { ed.previewing = true; sendPreview(ed.id, sample(ed.draft.points, ed.scrub, ed.channels)); }
    else stopPreview();
  });
  $('#phases').addEventListener('click', e => {
    const ins = e.target.closest('[data-insert]');
    if (ins) {
      const i = Number(ins.dataset.insert), pts = ed.draft.points;
      const a = pts[i].t, b = i + 1 < pts.length ? pts[i + 1].t : 1440;
      let t = Math.round((a + (b - a) / 2) / 5) * 5;
      if (t <= a) t = a + 1;
      if (t >= 1440) t = 1439;
      const p = { t, v: sample(pts, t, ed.channels), ci: 0, cm: 0 };
      editPhase(null, p);
      return;
    }
    const row = e.target.closest('[data-i]'); if (row) editPhase(Number(row.dataset.i));
  });
}

function editPhase(index, draftPoint) {
  const ed = S.ed, l = light(ed.id), isNew = index == null;
  const p = isNew ? clone(draftPoint) : clone(ed.draft.points[index]);
  const first = !isNew && index === 0;
  let liveOn = !!(l && l.online);
  const seg = (name, labels, val) => `<div class="seg" data-seg="${name}">${labels.map((t, i) => `<button data-v="${i}" class="${val === i ? 'on' : ''}">${t}</button>`).join('')}</div>`;
  const body = openSheet(`<h3>${isNew ? 'New phase' : first ? 'Start of the day' : 'Change phase'}</h3>
    <label class="field"><span>Reached at</span><input type="time" id="phTime" step="60" value="${fmt(p.t)}" ${first ? 'disabled' : ''}></label>
    ${first ? '<p class="small muted">The first phase always starts at 00:00, so the night is never undefined.</p>' : ''}
    <div class="row" style="margin:6px 0"><span class="swatch" id="phSw"></span><span class="small muted grow">Colours the light reaches at this time</span></div>
    ${ed.channels.map(c => `<div class="ch ${c}"><label>${LETTER[c]}</label><input type="range" min="0" max="100" step="1" data-ch="${c}" value="${p.v[c] || 0}" aria-label="${NAMES[c]}"><output data-o="${c}">${p.v[c] || 0}</output></div>`).join('')}
    ${ed.caps.clouds ? `<div class="field"><span>Clouds (HeliaLux)</span>${seg('ci', CLOUD_I, p.ci || 0)}</div><div class="field"><span>Cloud movement</span>${seg('cm', CLOUD_M, p.cm || 0)}</div>` : ''}
    <label class="toggle"><span>Show on the light while editing</span><input type="checkbox" id="phLive" ${liveOn ? 'checked' : ''}></label>
    <p class="small" id="phErr" style="color:var(--danger)"></p>
    <div class="btn-row">${!isNew && !first ? '<button class="btn danger" data-a="del">Delete</button>' : ''}<button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">${isNew ? 'Add phase' : 'Done'}</button></div>`,
    () => { if (ed.previewing && !ed.scrubLive) stopPreview(); });
  const sw = () => { $('#phSw', body).style.background = mixCss(p.v); };
  const pushLive = () => { if (liveOn) { ed.previewing = true; sendPreview(ed.id, p.v); } };
  sw(); pushLive();
  $$('input[type=range]', body).forEach(r => r.addEventListener('input', () => {
    p.v[r.dataset.ch] = Number(r.value); $(`[data-o=${r.dataset.ch}]`, body).textContent = r.value; sw(); pushLive();
  }));
  $$('[data-seg]', body).forEach(s => s.addEventListener('click', e => {
    const b = e.target.closest('[data-v]'); if (!b) return;
    p[s.dataset.seg] = Number(b.dataset.v); $$('button', s).forEach(x => x.classList.toggle('on', x === b));
  }));
  $('#phLive', body).addEventListener('change', e => { liveOn = e.target.checked; if (liveOn) pushLive(); else stopPreview(); });
  body.addEventListener('click', e => {
    const a = e.target.closest('[data-a]')?.dataset.a; if (!a) return;
    if (a === 'cancel') return closeSheet();
    if (a === 'del') { ed.draft.points.splice(index, 1); closeSheet(); markDirty(); return; }
    const t = first ? 0 : parseHHMM($('#phTime', body).value);
    if (!first && t === 0) { $('#phErr', body).textContent = '00:00 is the start of the day. Pick a later time.'; return; }
    if (ed.draft.points.some((q, i) => q.t === t && i !== index)) { $('#phErr', body).textContent = `There is already a phase at ${fmt(t)}.`; return; }
    p.t = t;
    if (isNew) ed.draft.points.push(p); else ed.draft.points[index] = p;
    ed.draft.points.sort((x, y) => x.t - y.t);
    closeSheet(); markDirty();
  });
}

async function saveProgram() {
  const ed = S.ed;
  const r = await act(api(`/api/lights/${encodeURIComponent(ed.id)}/profiles/${ed.slot}`, { name: ed.draft.name, points: ed.draft.points }));
  if (!r) return;
  toast(r.message || 'Saved', r.verified === false);
  if (r.pending) setTimeout(() => toast(r.message), 2600);
  await loadEditor(ed.id);
}
async function showTemplates() {
  if (!S.templates) S.templates = await api('/api/templates').catch(() => []);
  const b = openSheet(`<h3>Start from a template</h3><p class="small muted">Replaces the phases of this program. Nothing is sent until you save.</p>
    ${S.templates.map(t => `<button class="tpl" data-t="${esc(t.id)}"><b>${esc(t.name)}</b><span class="small muted">${esc(t.description)}</span></button>`).join('')}
    <button class="btn" style="width:100%;margin-top:6px" data-a="close">Cancel</button>`);
  b.addEventListener('click', async e => {
    if (e.target.closest('[data-a=close]')) return closeSheet();
    const t = e.target.closest('[data-t]'); if (!t) return;
    const prof = await api(`/api/lights/${encodeURIComponent(S.ed.id)}/template/${t.dataset.t}`).catch(err => { toast(err.message, true); return null; });
    if (!prof) return;
    S.ed.draft = { name: prof.name.slice(0, 20), points: prof.points };
    closeSheet(); S.ed.dirty = true; render(true); toast('Template loaded. Adjust it, then save.');
  });
}
async function copyTo() {
  const others = mine().filter(l => l.id !== S.ed.id);
  if (!others.length) { toast('There is no other light yet.', true); return; }
  const b = openSheet(`<h3>Copy “${esc(S.ed.draft.name)}” to…</h3>
    <label class="field"><span>Light</span><select id="cpLight">${others.map(l => `<option value="${esc(l.id)}">${esc(l.name)} (${esc(l.room || 'no room')})</option>`).join('')}</select></label>
    <label class="field"><span>Into program</span><select id="cpSlot"></select></label>
    <p class="small muted">Colours are matched by name. A 2-colour HeliaLux only takes white and blue.</p>
    <div class="btn-row"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Copy</button></div>`);
  const fill = async () => {
    const d = await api(`/api/lights/${encodeURIComponent($('#cpLight', b).value)}/profiles`).catch(() => null);
    $('#cpSlot', b).innerHTML = (d?.profiles || []).map((p, i) => `<option value="${i}">${i + 1} · ${esc(p.name)}</option>`).join('');
  };
  $('#cpLight', b).addEventListener('change', fill); await fill();
  b.addEventListener('click', async e => {
    const a = e.target.closest('[data-a]')?.dataset.a; if (!a) return;
    if (a === 'cancel') return closeSheet();
    const target = $('#cpLight', b).value, slot = $('#cpSlot', b).value;
    closeSheet();
    const r = await act(api(`/api/lights/${encodeURIComponent(target)}/profiles/${slot}`, { name: S.ed.draft.name, points: S.ed.draft.points }));
    if (r) toast(`Copied. ${r.message || ''}`, r.verified === false);
  });
}

/* ---------------- Full-curve test ---------------- */
async function curveTest(id) {
  const l = light(id), others = mine().filter(x => x.kind === 'chihiros' && x.id !== id);
  const ORDER = [2, 1];                 // the light's "auto" way first: it needs no full rewrite later
  let attempt = 0, variant = ORDER[0], timer = null, finished = false, started = false;
  const base = `/api/lights/${encodeURIComponent(id)}`;
  const cancel = () => { clearInterval(timer); if (started && !finished) api(`${base}/lamp-mode`, { mode: l.caps.lamp_mode || 'simple' }).catch(() => {}); };
  const intro = () => {
    const b = openSheet(`<h3>Can this light store full curves?</h3>
      <p>Light Bridge stores a short test program in <b>${esc(l.name)}</b>. You watch the light and tell us what you saw.</p>
      <ol class="steps small"><li>The light turns soft white.</li><li>About two minutes later it turns <b>blue</b> for two minutes.</li><li>Then it turns soft white again.</li></ol>
      <p class="small muted">Keep the PC near the light. Afterwards, your own program is stored again, whatever the result.</p>
      <div class="btn-row"><button class="btn" data-a="no">Not now</button><button class="btn primary" data-a="go">Start test</button></div>`, cancel);
    b.addEventListener('click', e => { const a = e.target.closest('[data-a]')?.dataset.a; if (a === 'no') closeSheet(); if (a === 'go') start(); });
  };
  const start = async () => {
    let info;
    try { info = await api(`${base}/curve-test`, { variant }); started = true; } catch (e) { finished = true; closeSheet(); toast(e.message, true); return; }
    const blue = new Date(info.blue_at * 1000), hhmm = fmt(blue.getHours() * 60 + blue.getMinutes());
    const b = openSheet(`<h3>Watch the light</h3>
      <p>It should now be soft white. At <b>${hhmm}</b> it should turn <b>blue</b> for about two minutes.</p>
      <div style="font-size:30px;font-weight:800;font-variant-numeric:tabular-nums;text-align:center;margin:10px 0" id="ctClock"></div>
      <div class="progress"><div id="ctBar"></div></div>
      <div id="ctAsk" class="hidden"><p><b>Did the light turn blue at ${hhmm}?</b></p>
        ${others.length ? `<label class="toggle"><span>Also switch my other Chihiros to full curves (only if it is the same model). Each light then runs its program of today every day.</span><input type="checkbox" id="ctAll"></label>` : ''}
        <div class="btn-row"><button class="btn" data-a="missed">I missed it</button><button class="btn" data-a="no">No</button><button class="btn primary" data-a="yes">Yes, it turned blue</button></div></div>`, cancel);
    const t0 = Date.now() / 1000;
    const tickUi = () => {
      const now = Date.now() / 1000, el = $('#ctClock', b); if (!el) return;
      if (now < info.blue_at) el.textContent = `blue in ${Math.ceil(info.blue_at - now)} s`;
      else if (now < info.back_at) el.textContent = `should be blue now (${Math.ceil(info.back_at - now)} s)`;
      else { el.textContent = 'test finished'; $('#ctAsk', b).classList.remove('hidden'); clearInterval(timer); }
      $('#ctBar', b).style.width = `${Math.min(100, (now - t0) / (info.back_at - t0) * 100)}%`;
    };
    timer = setInterval(tickUi, 500); tickUi();
    b.addEventListener('click', async e => {
      const a = e.target.closest('[data-a]')?.dataset.a; if (!a) return;
      if (a === 'missed') { clearInterval(timer); return start(); }
      const all = !!$('#ctAll', b)?.checked;
      if (a === 'yes') {
        finished = true; closeSheet();
        const r = await act(api(`${base}/curve-result`, { variant, worked: true, apply_to_all: all }));
        if (r) toast(r.message || 'Full curves switched on.');
        return loadEditor(id);
      }
      if (attempt + 1 < ORDER.length) { attempt++; variant = ORDER[attempt]; clearInterval(timer); toast('Let’s try the second way.'); return start(); }
      finished = true; closeSheet();
      await act(api(`${base}/curve-result`, { variant, worked: false }), 'The light keeps the simple version. Nothing is lost.');
      loadEditor(id);
    });
  };
  intro();
}

/* ---------------- Adding a light found nearby ---------------- */
function adoptLight(id) {
  const l = light(id), n = mine().filter(x => x.kind === 'chihiros').length;
  const rooms = [...new Set(['Living room', 'Bedroom', ...mine().map(x => x.room).filter(Boolean)])];
  const b = openSheet(`<h3>Add this light</h3>
    <label class="field"><span>Name</span><input type="text" id="adName" maxlength="40" placeholder="${n ? `Chihiros ${n + 1}` : 'Chihiros'}"></label>
    <label class="field"><span>Room</span><input type="text" id="adRoom" maxlength="40" list="adRooms" placeholder="e.g. Living room"></label>
    <datalist id="adRooms">${rooms.map(r => `<option value="${esc(r)}">`).join('')}</datalist>
    <div class="notice warn small"><b>Good to know</b>Light Bridge now stores its day program in this light. The program you set in the My Chihiros app is replaced. You can change the new program at any time.</div>
    <div class="btn-row"><button class="btn" data-a="cancel">Cancel</button><button class="btn primary" data-a="ok">Add light</button></div>`);
  b.addEventListener('click', async e => {
    const a = e.target.closest('[data-a]')?.dataset.a; if (!a) return;
    if (a === 'cancel') return closeSheet();
    const name = $('#adName', b).value, room = $('#adRoom', b).value;
    closeSheet();
    await act(api(`/api/lights/${encodeURIComponent(l.id)}/adopt`, { name, room }), 'Added. The day program is stored in the light.');
  });
}

/* ---------------- Light settings tab ---------------- */
function lightSettingsHtml() {
  const l = light(S.route.id), id = esc(l.id);
  const rooms = [...new Set(mine().map(x => x.room).filter(Boolean))];
  let kind = '';
  if (l.kind === 'helialux') {
    kind = `<div class="card"><h2>HeliaLux</h2>
      <div class="field"><span>Colours</span><div class="seg" id="colSeg"><button data-c="4" class="${l.channels.length === 4 ? 'on' : ''}">4 colours (Spectrum)</button><button data-c="2" class="${l.channels.length === 2 ? 'on' : ''}">2 colours (LED)</button></div>
      <p class="small muted" style="margin:8px 0 0">Detected automatically. Change it only if it is wrong.</p></div>
      <label class="field"><span>Address in your home network</span><input type="text" id="ipIn" inputmode="decimal" value="${esc(l.info.ip || '')}"></label>
      <div class="btn-row"><button class="btn" data-act="saveip" data-id="${id}">Save address</button><button class="btn" data-act="reload" data-id="${id}">Read programs again</button></div>
      <p class="small muted" style="margin-top:10px">If the address changes (for example after a power cut), the bridge finds the light again by itself.</p></div>`;
  } else {
    const c = l.caps, t = c.curve_tested;
    kind = `<div class="card"><h2>Chihiros</h2><dl class="kv"><dt>Bluetooth address</dt><dd class="mono">${esc(l.info.address || '–')}</dd>
      <dt>Signal (last search)</dt><dd>${signalText(l.info.rssi)}</dd>
      <dt>Stored in the light</dt><dd>${c.lamp_mode === 'curve' ? 'full curve' : 'simple version'}${l.info.program_pending ? ' <span class="tag warn">change waiting</span>' : ''}</dd>
      <dt>Full-curve test</dt><dd>${t ? (t.ok ? 'works' : 'did not work') + ` (${new Date(t.at * 1000).toLocaleDateString()})` : 'not done yet'}</dd></dl>
      <p class="small muted" style="margin-top:10px">The light runs its stored program on its own. The PC only sends changes, sets the clock and handles manual light.</p></div>`;
  }
  return `<div class="card"><h2>Name and room</h2>
      <label class="field"><span>Name</span><input type="text" id="nameIn" maxlength="40" value="${esc(l.name)}"></label>
      <label class="field"><span>Room</span><input type="text" id="roomIn" maxlength="40" list="roomList" value="${esc(l.room)}" placeholder="e.g. Living room"></label>
      <datalist id="roomList">${rooms.map(r => `<option value="${esc(r)}">`).join('')}</datalist>
      <button class="btn primary" data-act="savename" data-id="${id}">Save</button></div>
    ${kind}
    <div class="card"><h2>Remove</h2><p class="small muted">Removes the light from the bridge. The light keeps running the program stored in it. If it is switched on and nearby, it shows up again as a new light.</p>
      <button class="btn danger" data-act="forget" data-id="${id}">Remove this light</button></div>`;
}

/* ---------------- Bridge settings ---------------- */
function settingsHtml() {
  const b = S.state.bridge;
  return `<div class="card"><h2>Find lights</h2><dl class="kv" id="scanInfo"></dl>
      <div class="btn-row" style="margin-top:12px"><button class="btn primary" data-act="scan">Search now</button></div>
      <p class="small muted" style="margin-top:10px">Chihiros lights are found over Bluetooth when the PC is within a few metres.</p>
      ${b.hidden ? `<p class="small" style="margin:6px 0">Lights you marked as “Not mine”: ${b.hidden}. <button class="btn small" data-act="unignore">Show them again</button></p>` : ''}
      <details style="margin-top:6px"><summary>Optional: Juwel HeliaLux (beta)</summary>
        <p class="small muted">Only if you also have a HeliaLux SmartControl in your home Wi-Fi.</p>
        <button class="btn" data-act="findhl">Search this home network</button>
        <label class="field"><span>…or add it by its address</span><input type="text" id="addIp" inputmode="decimal" placeholder="e.g. 192.168.178.40"></label>
        <button class="btn" data-act="addip">Add</button></details></div>
    ${qrCardHtml(false)}
    <div class="card"><h2>Backup</h2><p class="small muted">Saves names, rooms and all day programs in one file.</p>
      <div class="btn-row"><a class="btn" href="/api/backup" download="light-bridge-backup.json">Download backup</a>
      <label class="btn">Restore from file<input type="file" id="restoreFile" accept=".json,application/json" class="hidden"></label></div></div>
    <div class="card"><h2>Stop the bridge</h2><p class="small muted">The lights keep running the programs stored in them. If a manual light is on, it is switched back to the day program first.</p>
      <button class="btn danger" data-act="stop">Stop bridge</button></div>
    <div class="card"><h2>About</h2><dl class="kv"><dt>Version</dt><dd>${esc(b.version)}${b.demo ? ' (demo)' : ''}</dd><dt>Settings folder</dt><dd class="mono">${esc(b.data_dir)}</dd></dl></div>`;
}
function liveSettings() {
  const b = S.state.bridge, el = $('#scanInfo'); if (!el) return;
  const when = t => t ? new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '–';
  const html = `<dt>Bluetooth (Chihiros)</dt><dd>${b.ble.scanning ? 'searching…' : b.ble.available === false ? 'not available' : `last search ${when(b.ble.last_scan)}`}</dd>
    ${b.ble.message ? `<dt></dt><dd class="small" style="color:var(--warn)">${esc(b.ble.message)}</dd>` : ''}
    ${b.lan.scanning ? '<dt>HeliaLux</dt><dd>searching…</dd>' : ''}
    ${b.lan.message ? `<dt></dt><dd class="small" style="color:var(--warn)">${esc(b.lan.message)}</dd>` : ''}`;
  if (el.innerHTML !== html) el.innerHTML = html;
}

/* ---------------- Global click handling ---------------- */
$('#app').addEventListener('click', async e => {
  const a = e.target.closest('[data-act]');
  if (a) {
    e.preventDefault(); e.stopPropagation();
    const id = a.dataset.id, base = id ? `/api/lights/${encodeURIComponent(id)}` : '';
    switch (a.dataset.act) {
      case 'maint': return act(api(`${base}/maintenance`, { minutes: 60 }), 'Maintenance light for 60 minutes');
      case 'off': return act(api(`${base}/manual`, { levels: { w: 0, r: 0, g: 0, b: 0 }, minutes: 60 }), 'Lights off for one hour');
      case 'program': return act(api(`${base}/program`, {}), 'Back to the day program');
      case 'clock': return act(api(`${base}/sync-clock`, {}), 'Clock set');
      case 'scan': return act(api('/api/scan', {}), 'Searching for lights…');
      case 'findhl': return act(api('/api/find-helialux', {}), 'Searching the home network…');
      case 'unignore': return act(api('/api/unignore', {}), 'Searching again, including hidden lights…').then(() => render(true));
      case 'curvetest': return curveTest(id);
      case 'identify': toast('Watch your lights: this one blinks blue three times…'); return act(api(`${base}/identify`, {}), 'Done. Was it yours?');
      case 'adopt': return adoptLight(id);
      case 'ignore':
        if (await confirmSheet('Not your light?', 'Light Bridge will leave this light alone and not show it again. Nothing is changed on it.', 'Not mine')) {
          await act(api(`${base}/ignore`, {}), 'Hidden. It will not be shown again.');
        }
        return;
      case 'lampsimple': return act(api(`${base}/lamp-mode`, { mode: 'simple' }), 'The light gets the simple version').then(() => loadEditor(id));
      case 'runonlight': {
        const days = [0, 0, 0, 0, 0, 0, 0].map(() => S.ed.slot);
        const r = await act(api(`/api/lights/${encodeURIComponent(S.ed.id)}/weekdays`, { weekdays: days }), 'This program goes into the light');
        if (r) loadEditor(S.ed.id);
        return;
      }
      case 'addip': return act(api('/api/lights/add', { ip: $('#addIp').value }), 'Light added');
      case 'reload': return act(api(`${base}/reload`, {}), 'Programs read again');
      case 'savename': return act(api(`${base}/settings`, { name: $('#nameIn').value, room: $('#roomIn').value }), 'Saved').then(() => render(true));
      case 'saveip': return act(api(`${base}/settings`, { ip: $('#ipIn').value }), 'Address saved');
      case 'templates': return showTemplates();
      case 'copyto': return copyTo();
      case 'save': return saveProgram();
      case 'discard': S.ed.draft = clone(S.ed.profiles[S.ed.slot]); S.ed.dirty = false; return render(true);
      case 'forget':
        if (await confirmSheet('Remove this light?', 'Its day programs on the bridge are deleted. The light itself keeps running the program stored in it.', 'Remove', true)) {
          await act(api(`${base}/forget`, {}), 'Light removed'); location.hash = '#/';
        }
        return;
      case 'stop':
        if (await confirmSheet('Stop the bridge?', 'The lights keep running on their own programs. Start the bridge again on the PC later.', 'Stop', true)) {
          await api('/api/stop', {}).catch(() => {}); S.stopped = true; render(true);
        }
        return;
    }
    return;
  }
  const seg = e.target.closest('#colSeg [data-c]');
  if (seg) return act(api(`/api/lights/${encodeURIComponent(S.route.id)}/settings`, { colours: Number(seg.dataset.c) }), 'Saved').then(() => render(true));
  const go = e.target.closest('[data-go]');
  if (go && !e.target.closest('a,button,input,select,label')) location.hash = go.dataset.go;
});
$('#app').addEventListener('change', async e => {
  if (e.target.id !== 'restoreFile') return;
  const f = e.target.files?.[0]; if (!f) return;
  try {
    const data = JSON.parse(await f.text());
    if (await confirmSheet('Restore this backup?', 'Names, rooms and day programs of your lights are replaced by the ones in the file. The programs are then stored in the lights again.', 'Restore')) {
      const r = await api('/api/restore', data); toast(`Restored ${r.restored} light(s)`); poll();
    }
  }
  catch (err) { toast(err.message, true); }
  e.target.value = '';
});

S.route = parseRoute();
if (S.route.name === 'light' && S.route.tab === 'program') loadEditor(S.route.id);
poll();
