// ── State ────────────────────────────────────────────────────────────────────
const S = {
  seriesId: null,
  series: null,
  episodes: [],
  episodeNum: null,
  episode: null,
  editingCharId: null,
  editingLocId: null,
};

// ── Batch / chunk helpers ─────────────────────────────────────────────────────
const TOTAL_SUB_EPS = 70;
function isBatchMode(s) { return !!(s && s.batch_mode); }
function batchSize(s)   { return isBatchMode(s) ? (parseInt(s.batch_size, 10) || 5) : 1; }
function chunkCount(s)  { const bs = batchSize(s) || 1; return Math.ceil(TOTAL_SUB_EPS / bs); }
function epToChunk(s, ep) { const bs = batchSize(s) || 1; return bs <= 1 ? ep : Math.floor((ep - 1) / bs) + 1; }
function chunkRange(s, num) {
  const bs = batchSize(s);
  if (bs <= 1) return [num, num];
  return [(num - 1) * bs + 1, num * bs];
}
function chunkLabel(s, num, { short = false } = {}) {
  if (!isBatchMode(s)) return short ? `Эп. ${num}` : `Эпизод ${num}`;
  const [a, b] = chunkRange(s, num);
  return short ? `С. ${a}–${b}` : `Серии ${a}–${b}`;
}
function milestoneIndices(s) {
  if (!isBatchMode(s)) return [1, 10, 20, 30, 40, 50, 60, 70];
  const set = new Set([1, 10, 20, 30, 40, 50, 60, TOTAL_SUB_EPS].map(n => epToChunk(s, n)));
  return [...set].sort((a, b) => a - b);
}
function requiredMilestones(s) {
  if (!isBatchMode(s)) return [1, 10, 70];
  return [...new Set([1, 10, TOTAL_SUB_EPS].map(n => epToChunk(s, n)))].sort((a, b) => a - b);
}
function stage2ChunkRange(s) {
  // The "synopses 1–10" stage maps to chunks 1..chunk-of(10) in batch mode.
  if (!isBatchMode(s)) return [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
  const last = epToChunk(s, 10);
  return Array.from({ length: last }, (_, i) => i + 1);
}

// ── API helpers ───────────────────────────────────────────────────────────────
async function parseApiError(r) {
  const text = await r.text();
  let msg = text;
  let payload = null;
  try {
    const j = JSON.parse(text);
    payload = j;
    // Prefer the human-readable `message` (set by handlers that pair a short
    // `error` code with a full sentence — e.g. moderation_precheck_reject)
    // and fall back to the bare code. Keeps callers without payload-aware
    // catches from showing just «moderation_precheck_reject» in toasts.
    msg = (typeof j.message === 'string' && j.message.trim()) ? j.message : (j.error || text);
  } catch {}
  // If the backend signals an AVAI auth problem, surface a focused modal that
  // points the user at Settings instead of just throwing the raw error string
  // up the stack (which usually ends as a giant JSON wall in a toast). This
  // runs once per error — modal is idempotent (existing one is removed first).
  try {
    if (typeof msg === 'string' && /AVAI_KEY_(MISSING|INVALID)|Invalid API token|Unauthorized/i.test(msg)) {
      if (typeof _showApiKeySetupModal === 'function') {
        const reason = /MISSING/.test(msg) ? 'missing' : 'invalid';
        setTimeout(() => _showApiKeySetupModal('avai', { reason }), 50);
      }
    }
  } catch {}
  const err = new Error(msg);
  err.payload = payload;
  err.status = r.status;
  return err;
}

const api = {
  async get(url) {
    const r = await fetch(url);
    if (!r.ok) throw await parseApiError(r);
    return r.json();
  },
  async post(url, body, callOpts = {}) {
    const { timeoutMs, idempotencyKey, _anthroRetried } = callOpts;
    const headers = { 'Content-Type': 'application/json' };
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
    const opts = { method: 'POST', headers, body: JSON.stringify(body) };
    if (timeoutMs) opts.signal = AbortSignal.timeout(timeoutMs);
    let r;
    try { r = await fetch(url, opts); }
    catch (e) { throw new Error(e.name === 'TimeoutError' ? `Таймаут (${Math.round(timeoutMs/1000)}с) — сервер не ответил` : e.message); }
    if (!r.ok) {
      // Anthro gate (HTTP 409): a generation would render a NON-human in a
      // series that hasn't decided its world type yet. Ask the user ONCE, then
      // retry the original request. Granting is per-series and never re-asks.
      if (r.status === 409 && !_anthroRetried) {
        let data = null;
        try { data = await r.clone().json(); } catch {}
        if (data && data.needs_anthro_decision) {
          const decided = await resolveAnthroGate(url, data);
          if (decided) return api.post(url, body, { ...callOpts, _anthroRetried: true });
          throw new Error('Генерация отменена — не выбран тип мира сериала.');
        }
      }
      throw await parseApiError(r);
    }
    return r.json();
  },
  async put(url, body) {
    const r = await fetch(url, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!r.ok) throw await parseApiError(r);
    return r.json();
  },
  async del(url) {
    const r = await fetch(url, { method: 'DELETE' });
    if (!r.ok) throw await parseApiError(r);
    return r.json();
  },
  async upload(url, formData) {
    const r = await fetch(url, { method: 'POST', body: formData });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  // Multipart POST with optional timeout — mirrors post() ergonomics but for
  // FormData payloads (file uploads alongside text fields).
  async postForm(url, formData, { timeoutMs } = {}) {
    const opts = { method: 'POST', body: formData };
    if (timeoutMs) opts.signal = AbortSignal.timeout(timeoutMs);
    let r;
    try { r = await fetch(url, opts); }
    catch (e) { throw new Error(e.name === 'TimeoutError' ? `Таймаут (${Math.round(timeoutMs/1000)}с) — сервер не ответил` : e.message); }
    if (!r.ok) throw await parseApiError(r);
    return r.json();
  },
};

// ── Task monitor (floating progress widget) ───────────────────────────────────
const Tasks = {
  _items: [],
  _seq: 0,
  _minimized: false,
  start(label, ctx = {}) {
    const id = ++this._seq;
    this._items.push({ id, label, ctx, status: 'running', startedAt: Date.now(), msg: '' });
    this._render();
    return id;
  },
  update(id, msg) {
    const t = this._items.find(x => x.id === id); if (!t) return;
    t.msg = msg || ''; this._render();
  },
  done(id, ok = true, msg = '') {
    const t = this._items.find(x => x.id === id); if (!t) return;
    t.status = ok ? 'done' : 'error';
    t.msg = msg || t.msg;
    t.endedAt = Date.now();
    this._render();
    if (ok) setTimeout(() => this.dismiss(id), 25000);
  },
  dismiss(id) {
    this._items = this._items.filter(x => x.id !== id);
    this._render();
  },
  clearFinished() {
    this._items = this._items.filter(x => x.status === 'running');
    this._render();
  },
  toggleMin() {
    this._minimized = !this._minimized;
    this._render();
  },
  goTo(id) {
    const t = this._items.find(x => x.id === id); if (!t) return;
    const ctx = t.ctx || {};
    if (ctx.episodeNum && ctx.seriesId) {
      navigate('episode', { seriesId: ctx.seriesId, episodeNum: ctx.episodeNum });
    } else if (ctx.seriesId) {
      navigate('series', { seriesId: ctx.seriesId });
    }
  },
  _render() {
    const root = document.getElementById('task-monitor');
    const list = document.getElementById('task-monitor-list');
    const cnt  = document.getElementById('task-monitor-count');
    if (!root || !list) return;
    if (!this._items.length) { root.classList.add('hidden'); return; }
    root.classList.remove('hidden');
    root.classList.toggle('minimized', this._minimized);
    const running = this._items.filter(x => x.status === 'running').length;
    if (cnt) cnt.textContent = running ? `${running}/${this._items.length}` : `${this._items.length}`;
    list.innerHTML = this._items.map(t => {
      const dur = ((t.endedAt || Date.now()) - t.startedAt) / 1000;
      const durStr = dur < 60 ? `${dur.toFixed(0)}с` : `${(dur/60).toFixed(1)}м`;
      const icon = t.status === 'running' ? '<span class="spinner"></span>'
                 : t.status === 'done' ? '✓'
                 : '✗';
      const ctxLabel = t.ctx?.episodeNum ? `Эп. ${t.ctx.episodeNum}` : (t.ctx?.seriesTitle || '');
      const clickable = t.status !== 'running' && (t.ctx?.seriesId);
      return `
        <div class="task-item task-${t.status}${clickable ? ' task-clickable' : ''}"
             ${clickable ? `onclick="Tasks.goTo(${t.id})"` : ''}>
          <span class="task-icon">${icon}</span>
          <div class="task-body">
            <div class="task-label">${esc(t.label)}${ctxLabel ? ` <span class="task-ctx">· ${esc(ctxLabel)}</span>` : ''}</div>
            ${t.msg ? `<div class="task-msg">${esc(t.msg)}</div>` : ''}
          </div>
          <span class="task-meta">${durStr}</span>
          <button class="task-close" onclick="event.stopPropagation();Tasks.dismiss(${t.id})" title="Убрать">×</button>
        </div>
      `;
    }).join('');
  },
};

// Track an async function: registers a task, runs fn, updates state on resolve/reject.
async function trackTask(label, ctx, fn) {
  const id = Tasks.start(label, ctx);
  try {
    const result = await fn();
    Tasks.done(id, true, 'Готово');
    return result;
  } catch (e) {
    Tasks.done(id, false, e?.message || 'Ошибка');
    throw e;
  }
}

// Cache-bust counter for asset URLs.
//
// CRITICAL: must persist across page loads — otherwise EVERY page reload
// generates a fresh Date.now() and the browser sees N different URLs for
// the same N images → re-downloads everything. User reported "сайт всё
// очень долго прогружается" — this was the cause.
//
// Now: read last-bump value from localStorage on boot. Bumps happen only
// when an asset actually changes (upload, regen, autogen tick). Browser's
// HTTP cache then works as intended — same URL between reloads → 304s
// instead of full re-downloads.
window._assetVer = (function () {
  try {
    const saved = localStorage.getItem('asset_ver');
    if (saved) return parseInt(saved, 10);
  } catch {}
  // First-ever boot: pick a stable starter so subsequent same-day reloads
  // return the same URLs.
  const init = Date.now();
  try { localStorage.setItem('asset_ver', String(init)); } catch {}
  return init;
})();
function bumpAssetVersion() {
  window._assetVer = Date.now();
  try { localStorage.setItem('asset_ver', String(window._assetVer)); } catch {}
}
function assetUrl(rel) {
  if (!rel) return '';
  if (typeof rel !== 'string') return rel;
  if (/^https?:/.test(rel)) return rel;  // remote URLs (avai-gen) — leave alone
  const sid = (typeof S !== 'undefined' && S && S.seriesId) ? S.seriesId : '';
  const base = sid ? `/assets/${sid}/${rel}` : (rel.startsWith('/') ? rel : '/' + rel);
  const sep = base.includes('?') ? '&' : '?';
  return `${base}${sep}v=${window._assetVer}`;
}

// Resizable sidebar plumbing. Both the series-page sidebar (drag-handle on
// its right edge, growing rightward) and the episode-page sidebar (handle
// on its left edge, growing leftward) get the same treatment. Each persists
// its width independently via localStorage.
function _wireResizable(handleId, sidebarId, storageKey, direction = 'right') {
  const handle = document.getElementById(handleId);
  const sidebar = document.getElementById(sidebarId);
  if (!handle || !sidebar) return;
  try {
    const saved = parseInt(localStorage.getItem(storageKey) || '0', 10);
    if (saved >= 200 && saved <= 700) sidebar.style.width = saved + 'px';
  } catch {}
  let dragging = false, startX = 0, startW = 0;
  handle.addEventListener('mousedown', (e) => {
    dragging = true; startX = e.clientX;
    startW = sidebar.getBoundingClientRect().width;
    handle.classList.add('dragging');
    document.body.classList.add('sidebar-dragging');
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    // direction='right' means handle is on sidebar's right edge → drag right grows. Episode
    // sidebar is on the right of the page, handle on its left → drag right SHRINKS.
    let next = direction === 'right' ? startW + dx : startW - dx;
    next = Math.max(200, Math.min(700, next));
    sidebar.style.width = next + 'px';
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.classList.remove('sidebar-dragging');
    try { localStorage.setItem(storageKey, String(parseInt(sidebar.style.width, 10) || 280)); } catch {}
  });
}
document.addEventListener('DOMContentLoaded', () => {
  _wireResizable('sidebar-resizer',  'series-sidebar',  'sidebar_width',         'right');
  _wireResizable('episode-resizer',  'episode-sidebar', 'episode_sidebar_width', 'left');
});

// Wire up monitor controls once DOM is ready
async function _checkApiKeysOnBoot() {
  try {
    const me = await fetch('/api/me').then(r => r.ok ? r.json() : null);
    if (!me || !me.authenticated) return;
    window._currentUser = me;
    // Show the logs button to ALL authenticated users (their own logs are
    // visible to them). Primary user additionally gets the user dropdown
    // inside the modal so they can switch between users.
    const logBtn = document.getElementById('admin-logs-btn');
    if (logBtn) {
      logBtn.style.display = '';
      logBtn.title = me.is_primary
        ? 'Логи всех пользователей (admin view)'
        : 'Мои логи — здесь видны мои ошибки и события';
    }
    if (!me.has_avai_key) {
      _showApiKeySetupModal('avai', { firstTime: true });
      return;
    }
    // Has a key string — but is it actually VALID? Live-check via the cheap
    // /validate-avai-key endpoint (auth-only, no generation, no cost). Pop
    // the fix-it modal proactively if AVAI rejects it (401) or it's empty.
    // Skip the unreachable case — could be transient AVAI downtime, don't
    // bother user about that.
    try {
      const v = await fetch('/api/me/validate-avai-key').then(r => r.ok ? r.json() : null);
      if (v && (v.state === 'invalid' || v.state === 'missing')) {
        _showApiKeySetupModal('avai', { reason: v.state });
      }
    } catch {}
  } catch {}
}

// Re-check key validity on demand (after Settings save, or before a
// generation flow if the user has been idle a while). Returns the state
// string so callers can short-circuit if invalid.
async function recheckAvaiKey() {
  try {
    const v = await fetch('/api/me/validate-avai-key').then(r => r.ok ? r.json() : null);
    if (!v) return 'unreachable';
    if (v.state === 'invalid' || v.state === 'missing') {
      _showApiKeySetupModal('avai', { reason: v.state });
    }
    return v.state;
  } catch { return 'unreachable'; }
}

// ── Admin: per-user log viewer ──────────────────────────────────────────────
// Only visible to the primary user. Lets the operator inspect any user's
// recent server-side activity (HTTP requests, errors, structured events from
// background workers) without SSHing into the VPS. Backed by JSONL files at
// <DATA_ROOT>/<user-slug>/_logs/YYYY-MM-DD.jsonl.

async function openAdminLogs() {
  openModal('modal-admin-logs');
  const isPrimary = !!(window._currentUser && window._currentUser.is_primary);
  const titleEl = document.getElementById('admin-logs-title');
  if (titleEl) {
    titleEl.textContent = isPrimary ? '📋 Логи пользователей (admin view)' : '📋 Мои логи';
  }
  const userPicker = document.getElementById('admin-logs-user-wrap')
    || document.getElementById('admin-logs-user')?.closest('label');
  const sel = document.getElementById('admin-logs-user');
  // Non-primary: hide user-picker entirely (always self); primary: populate.
  if (!isPrimary) {
    if (userPicker) userPicker.style.display = 'none';
    if (sel) {
      // Stub option so loadAdminLogs() has a value (even though backend
      // ignores ?email= for non-primary anyway).
      sel.innerHTML = `<option value="${esc((window._currentUser || {}).email || '')}">Мои логи</option>`;
    }
    // Populate the date dropdown with last 14 days for self.
    _adminLogsBuildDateRange(14);
    loadAdminLogs();
    return;
  }
  if (userPicker) userPicker.style.display = '';
  try {
    const r = await fetch('/api/admin/users').then(x => x.json());
    if (sel) {
      sel.innerHTML = (r.users || []).map(u => {
        const date = u.log_dates?.[0] || '—';
        return `<option value="${esc(u.slug)}" data-dates='${JSON.stringify(u.log_dates || [])}'>${esc(u.slug)} (last: ${date}, projects: ${u.project_count})</option>`;
      }).join('');
      _adminLogsRebuildDateDropdown();
      sel.addEventListener('change', _adminLogsRebuildDateDropdown);
    }
    loadAdminLogs();
  } catch (e) {
    document.getElementById('admin-logs-table').textContent = 'Ошибка: ' + (e.message || e);
  }
}

// Builds last N days into the date dropdown — used for non-primary users
// who can't query /api/admin/users to discover available dates.
function _adminLogsBuildDateRange(days) {
  const dateSel = document.getElementById('admin-logs-date');
  if (!dateSel) return;
  const opts = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(Date.now() - i * 86400000);
    const iso = d.toISOString().slice(0, 10);
    opts.push(`<option value="${iso}">${iso}${i === 0 ? ' · сегодня' : ''}</option>`);
  }
  dateSel.innerHTML = opts.join('');
}

function _adminLogsRebuildDateDropdown() {
  const userSel = document.getElementById('admin-logs-user');
  const dateSel = document.getElementById('admin-logs-date');
  if (!userSel || !dateSel) return;
  const opt = userSel.selectedOptions?.[0];
  let dates = [];
  try { dates = JSON.parse(opt?.dataset?.dates || '[]'); } catch {}
  if (!dates.length) {
    const today = new Date().toISOString().slice(0, 10);
    dates = [today];
  }
  dateSel.innerHTML = dates.map(d => `<option value="${d}">${d}</option>`).join('');
}

let _adminLogsGrepTimer = null;
function _adminLogsGrepDebounced() {
  if (_adminLogsGrepTimer) clearTimeout(_adminLogsGrepTimer);
  _adminLogsGrepTimer = setTimeout(loadAdminLogs, 350);
}

// Last loaded log lines, kept around for the «Копировать» button so we can
// emit plain text without re-parsing the rendered HTML (and lose level/event
// info in the process).
let _adminLogsLastLines = [];

async function loadAdminLogs() {
  const userSel = document.getElementById('admin-logs-user');
  const dateSel = document.getElementById('admin-logs-date');
  const levelSel = document.getElementById('admin-logs-level');
  const grepEl  = document.getElementById('admin-logs-grep');
  const tableEl = document.getElementById('admin-logs-table');
  const statsEl = document.getElementById('admin-logs-stats');
  if (!userSel || !dateSel || !tableEl) return;
  const params = new URLSearchParams({
    email: userSel.value || '',
    date:  dateSel.value || '',
    lines: '1000',
  });
  const level = levelSel?.value || '';
  if (level) params.set('level', level);
  const grep = grepEl?.value?.trim() || '';
  if (grep) params.set('grep', grep);
  tableEl.textContent = '⏳ Загружаю...';
  try {
    const r = await fetch('/api/admin/logs?' + params.toString()).then(x => x.json());
    if (r.error) throw new Error(r.error);
    if (statsEl) statsEl.textContent = `Файл: ${r.file} · показано ${r.returned || 0} из ${r.total_lines || 0} строк`;
    _adminLogsLastLines = r.lines || [];
    if (!r.lines || !r.lines.length) {
      tableEl.innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center">Нет записей по фильтрам</div>';
      return;
    }
    tableEl.innerHTML = r.lines.map(_renderLogLine).join('');
    tableEl.scrollTop = tableEl.scrollHeight;
  } catch (e) {
    tableEl.textContent = 'Ошибка: ' + (e.message || e);
  }
}

// Flatten one log entry to a single plain-text line — same shape one would
// see in tail -f, but human-friendly. Used by «📋 Копировать».
function _logLineToText(obj) {
  const time = (obj.ts || '').slice(11, 23);
  const lvl = (obj.level || '').padEnd(5);
  const ev = obj.event || '?';
  let body = '';
  if (ev === 'http') {
    body = `${obj.status || '?'} ${obj.method || ''} ${obj.path || ''} · ${obj.ms || '?'}ms${obj.ip ? ' · ' + obj.ip : ''}`;
  } else if (ev === 'uncaught') {
    body = `${obj.exc_type || ''}: ${obj.exc_msg || ''} · ${obj.method || ''} ${obj.path || ''}`;
    if (obj.trace) body += '\n' + obj.trace;
  } else {
    const fields = Object.entries(obj)
      .filter(([k]) => !['ts', 'level', 'event', 'email'].includes(k))
      .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join(' ');
    body = `${ev} ${fields}`.trim();
  }
  return `${time} ${lvl} ${body}`;
}

async function copyAdminLogs(btn) {
  if (!_adminLogsLastLines.length) {
    showToast('Сначала загрузи логи', 3000);
    return;
  }
  const text = _adminLogsLastLines.map(_logLineToText).join('\n');
  const orig = btn ? btn.innerHTML : '';
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      // Fallback: temp textarea + execCommand. Works under non-https / older
      // browsers where clipboard API is gated.
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    if (btn) { btn.innerHTML = '✓ Скопировано'; setTimeout(() => { btn.innerHTML = orig; }, 1800); }
    showToast(`✓ Скопировано ${_adminLogsLastLines.length} строк в буфер`, 3000);
  } catch (e) {
    if (btn) btn.innerHTML = orig;
    showToast('Ошибка копирования: ' + (e.message || e), 4000);
  }
}

function _renderLogLine(obj) {
  const lvl = obj.level || '';
  const lvlColor = lvl === 'ERROR' ? '#f87171' : lvl === 'WARN' ? '#fbbf24' : 'var(--muted)';
  const time = (obj.ts || '').slice(11, 23);
  const ev = obj.event || '?';
  let body = '';
  if (ev === 'http') {
    const stColor = obj.status >= 500 ? '#f87171' : obj.status >= 400 ? '#fbbf24' : '#10b981';
    body = `<span style="color:${stColor}">${obj.status}</span> ${esc(obj.method || '')} ${esc(obj.path || '')} <span style="color:var(--muted)">· ${obj.ms}ms${obj.ip ? ' · ' + esc(obj.ip) : ''}</span>`;
  } else if (ev === 'uncaught') {
    body = `<span style="color:#f87171">${esc(obj.exc_type || '')}: ${esc(obj.exc_msg || '')}</span> · ${esc(obj.method || '')} ${esc(obj.path || '')}`;
    if (obj.trace) body += `<details style="margin-top:4px"><summary style="cursor:pointer;color:var(--muted)">stack trace</summary><pre style="margin:4px 0;font-size:0.72rem;color:#e0e0e0;white-space:pre-wrap">${esc(obj.trace)}</pre></details>`;
  } else {
    // Generic event
    const fields = Object.entries(obj)
      .filter(([k]) => !['ts', 'level', 'event', 'email'].includes(k))
      .map(([k, v]) => `${k}=${typeof v === 'string' ? esc(v).slice(0, 200) : JSON.stringify(v).slice(0, 200)}`)
      .join(' ');
    body = `<strong>${esc(ev)}</strong> ${fields}`;
  }
  return `<div style="padding:3px 0;border-bottom:1px solid var(--border)">
    <span style="color:var(--muted)">${time}</span>
    <span style="color:${lvlColor};font-weight:700;display:inline-block;min-width:50px">${lvl}</span>
    ${body}
  </div>`;
}

// Show modal demanding the user enter an API key. `kind` = 'avai' | 'reteller'.
// firstTime=true is shown after first login; provider-switch case shows it on
// demand with a different headline.
function _showApiKeySetupModal(kind, opts = {}) {
  const isAvai = kind === 'avai';
  // `reason` distinguishes 3 cases: undefined (provider-switch), 'missing'
  // (sweep/generation failed because key is empty), 'invalid' (AVAI returned
  // 401 — key expired or wrong). Tailor headline so user knows what to do.
  const reasonHeadline = (() => {
    if (opts.firstTime) return '👋 Добро пожаловать! Введи свой AVAI API ключ';
    if (opts.reason === 'invalid') return '⚠ AVAI не принял твой ключ (401 Unauthorized)';
    if (opts.reason === 'missing') return '⚠ AVAI ключ не задан — генерация невозможна';
    return 'Нужен AVAI API ключ';
  })();
  const title = isAvai ? reasonHeadline : 'Нужен Reteller API ключ';
  const reasonHint = (() => {
    if (opts.reason === 'invalid') return '<p style="color:var(--warning);margin:6px 0 0">Возможные причины: ключ истёк, потерял доступ, или закончились средства на счёте AVAI.</p>';
    if (opts.reason === 'missing') return '<p style="color:var(--warning);margin:6px 0 0">У твоего аккаунта на этом сайте нет привязанного AVAI ключа. Добавь и попробуй снова.</p>';
    return '';
  })();
  const subtitle = isAvai
    ? 'AVAI — провайдер для генерации видео и изображений (Seedance / Banana / Seedream). Ключ возьми на <a href="https://avai-gen.com" target="_blank" style="color:var(--accent)">avai-gen.com</a> в настройках своего аккаунта.' + reasonHint
    : 'Reteller — альтернативный провайдер для генерации видео из сценариев. Ключ возьми в настройках аккаунта на <a href="https://reteller.ai" target="_blank" style="color:var(--accent)">reteller.ai</a>.';
  const fieldId = isAvai ? 'apikey-modal-avai' : 'apikey-modal-reteller';
  const placeholder = isAvai ? 'avai-...' : 'rtl_sk_...';
  const existing = document.getElementById('modal-api-key-setup');
  if (existing) existing.remove();
  const div = document.createElement('div');
  div.id = 'modal-api-key-setup';
  div.className = 'modal';
  div.innerHTML = `
    <div class="modal-box" style="max-width:460px">
      <div class="modal-header">
        <h2 style="font-size:18px">${title}</h2>
      </div>
      <div class="modal-body" style="font-size:14px;line-height:1.55">
        <p style="color:var(--muted);margin-top:0">${subtitle}</p>
        <div class="field-group" style="margin-top:14px">
          <label style="font-size:0.82rem">${isAvai ? 'AVAI' : 'Reteller'} API Key</label>
          <input id="${fieldId}" type="password" placeholder="${placeholder}" style="font-family:monospace;font-size:0.9rem">
        </div>
        <div id="apikey-modal-status" style="font-size:0.8rem;margin-top:6px;min-height:18px"></div>
      </div>
      <div class="modal-footer">
        ${opts.firstTime ? '' : `<button class="btn-ghost" onclick="_closeApiKeyModal()">Позже</button>`}
        <button class="btn-primary" onclick="_saveApiKeyFromModal('${kind}')">Сохранить</button>
      </div>
    </div>
  `;
  document.body.appendChild(div);
  setTimeout(() => document.getElementById(fieldId)?.focus(), 50);
}

function _closeApiKeyModal() {
  document.getElementById('modal-api-key-setup')?.remove();
}

async function _saveApiKeyFromModal(kind) {
  const isAvai = kind === 'avai';
  const fieldId = isAvai ? 'apikey-modal-avai' : 'apikey-modal-reteller';
  const value = (document.getElementById(fieldId)?.value || '').trim();
  const status = document.getElementById('apikey-modal-status');
  if (!value) {
    if (status) { status.textContent = '⚠ Ключ не может быть пустым'; status.style.color = 'var(--danger)'; }
    return;
  }
  if (status) { status.textContent = '⏳ Сохраняю...'; status.style.color = 'var(--muted)'; }
  try {
    const payload = isAvai ? { avai_key: value } : { reteller_key: value };
    await api.post('/api/config', payload);
    // For AVAI: post-save live validation so the user gets immediate feedback
    // if they pasted a typo / expired key. Reteller has no cheap auth-check
    // endpoint exposed yet, so we skip live-validate there.
    if (isAvai) {
      if (status) { status.textContent = '⏳ Проверяю...'; }
      try {
        const v = await fetch('/api/me/validate-avai-key').then(r => r.ok ? r.json() : null);
        if (v && v.state === 'ok') {
          if (status) {
            const bal = v.balance != null ? ` (баланс: ${v.balance})` : '';
            status.textContent = '✓ Ключ валиден' + bal; status.style.color = 'var(--success)';
          }
        } else if (v && (v.state === 'invalid' || v.state === 'missing')) {
          if (status) {
            status.textContent = '✗ AVAI отверг ключ — проверь что скопировал правильно';
            status.style.color = 'var(--danger)';
          }
          return;  // don't close modal, let user retry
        } else {
          // unreachable / network blip — accept, user can re-validate later
          if (status) { status.textContent = '✓ Сохранено (проверка авторизации не удалась — попробуй сгенерить)'; status.style.color = 'var(--warning)'; }
        }
      } catch {
        if (status) { status.textContent = '✓ Сохранено'; status.style.color = 'var(--success)'; }
      }
    } else {
      if (status) { status.textContent = '✓ Сохранено'; status.style.color = 'var(--success)'; }
    }
    setTimeout(() => {
      _closeApiKeyModal();
      // Refresh /api/me state so subsequent gates see the new key
      _checkApiKeysOnBoot();
    }, 800);
  } catch (e) {
    if (status) { status.textContent = '✗ ' + (e.message || e); status.style.color = 'var(--danger)'; }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  // Random ironic slogan under the logo, picked fresh each page load
  const SLOGANS = [
    "Because deadlines don’t care about taste.",
    "For creators with vision, deadlines, and no shame.",
    "Write it. Cut it. Deny responsibility.",
    "Making “somehow it works” a business model.",
    "Make dramas faster than you can regret them.",
    "It's not perfect... Just like you.",
    "Just think about money..",
  ];
  const slEl = document.getElementById('nav-slogan');
  if (slEl) slEl.textContent = SLOGANS[Math.floor(Math.random() * SLOGANS.length)];

  // Bootstrap: check if logged-in user has set their API keys. Non-primary
  // users without an AVAI key get a one-time setup modal — generation would
  // fail otherwise with confusing 401s from AvAIGen.
  _checkApiKeysOnBoot();

  const t = document.getElementById('task-monitor-toggle');
  const c = document.getElementById('task-monitor-clear');
  if (t) t.addEventListener('click', () => Tasks.toggleMin());
  if (c) c.addEventListener('click', () => Tasks.clearFinished());
  // Tick durations every 2s while anything is running
  setInterval(() => { if (Tasks._items.some(x => x.status === 'running')) Tasks._render(); }, 2000);
  // Load current user info → header pill
  fetch('/api/me').then(r => r.ok ? r.json() : null).then(me => {
    if (!me || !me.email) return;
    const pill = document.getElementById('user-pill');
    const lbl = document.getElementById('user-pill-email');
    if (pill && lbl) {
      lbl.textContent = me.email;
      pill.title = me.name ? `${me.name} · ${me.email}` : me.email;
      pill.classList.remove('hidden');
    }
  }).catch(() => {});
});

