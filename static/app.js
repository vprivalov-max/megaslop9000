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
  try { const j = JSON.parse(text); msg = j.error || text; } catch {}
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
  return msg;
}

const api = {
  async get(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(await parseApiError(r));
    return r.json();
  },
  async post(url, body, { timeoutMs, idempotencyKey } = {}) {
    const headers = { 'Content-Type': 'application/json' };
    if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
    const opts = { method: 'POST', headers, body: JSON.stringify(body) };
    if (timeoutMs) opts.signal = AbortSignal.timeout(timeoutMs);
    let r;
    try { r = await fetch(url, opts); }
    catch (e) { throw new Error(e.name === 'TimeoutError' ? `Таймаут (${Math.round(timeoutMs/1000)}с) — сервер не ответил` : e.message); }
    if (!r.ok) throw new Error(await parseApiError(r));
    return r.json();
  },
  async put(url, body) {
    const r = await fetch(url, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await parseApiError(r));
    return r.json();
  },
  async del(url) {
    const r = await fetch(url, { method: 'DELETE' });
    if (!r.ok) throw new Error(await parseApiError(r));
    return r.json();
  },
  async upload(url, formData) {
    const r = await fetch(url, { method: 'POST', body: formData });
    if (!r.ok) throw new Error(await r.text());
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

// Cache-bust counter for asset URLs. Bumped after every operation that
// changes an image on disk (upload, regen, autogen sweep tick), then every
// <img src> formed via assetUrl() carries `?v=<ts>` so the browser refetches
// instead of showing the cached old version. User reported: "изменения в
// картинках появляются только после перезагрузки".
window._assetVer = Date.now();
function bumpAssetVersion() { window._assetVer = Date.now(); }
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

// ── Navigation ────────────────────────────────────────────────────────────────
function navigate(view, params = {}) {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById('view-' + view).classList.remove('hidden');
  Object.assign(S, params);

  // Topnav "Редактор" button — visible whenever a series is open and we're
  // not already inside the montage editor.
  const navMt = document.getElementById('nav-montage-btn');
  if (navMt) {
    const showMt = !!S.seriesId && view !== 'montage' && view !== 'projects';
    navMt.classList.toggle('hidden', !showMt);
  }

  // Persist current location in URL hash so a page reload restores the view.
  try {
    let h = '';
    if (view === 'projects')      h = '';
    else if (view === 'series')   h = `#series/${encodeURIComponent(S.seriesId || '')}`;
    else if (view === 'episode')  h = `#episode/${encodeURIComponent(S.seriesId || '')}/${S.episodeNum || ''}`;
    else if (view === 'montage')  h = `#montage/${encodeURIComponent(S.seriesId || '')}`;
    if (location.hash !== h) location.hash = h;
  } catch {}

  if (view === 'projects') {
    S.seriesId = null;
    if (navMt) navMt.classList.add('hidden');
    setBreadcrumb([]);
    loadProjects();
  } else if (view === 'series') {
    loadSeriesView();
  } else if (view === 'episode') {
    loadEpisodeView();
  } else if (view === 'montage') {
    loadMontageView();
  }
}

function _navFromHash() {
  const h = (location.hash || '').replace(/^#/, '');
  if (!h) { navigate('projects'); return; }
  const parts = h.split('/').map(decodeURIComponent);
  const [view, sid, epNum] = parts;
  if (view === 'series' && sid)   { navigate('series',  { seriesId: sid }); return; }
  if (view === 'episode' && sid && epNum) {
    navigate('episode', { seriesId: sid, episodeNum: parseInt(epNum, 10) });
    return;
  }
  if (view === 'montage' && sid)  { navigate('montage', { seriesId: sid }); return; }
  navigate('projects');
}

window.addEventListener('hashchange', () => {
  // Only react to genuine outside changes (back/forward) — guard re-entry
  // by comparing to current state, otherwise navigate() already wrote it.
  const h = (location.hash || '').replace(/^#/, '');
  const currentExpected = (() => {
    if (!S.seriesId) return '';
    const view = document.querySelector('.view:not(.hidden)')?.id?.replace('view-', '');
    if (view === 'series')  return `series/${encodeURIComponent(S.seriesId)}`;
    if (view === 'episode') return `episode/${encodeURIComponent(S.seriesId)}/${S.episodeNum || ''}`;
    if (view === 'montage') return `montage/${encodeURIComponent(S.seriesId)}`;
    return '';
  })();
  if (h !== currentExpected) _navFromHash();
});

function setBreadcrumb(items) {
  const el = document.getElementById('breadcrumb');
  if (!items.length) { el.innerHTML = ''; return; }
  el.innerHTML = items.map((item, i) => {
    if (i === items.length - 1) return `<span class="crumb">${item.label}</span>`;
    return `<span class="crumb link" onclick="${item.action}" style="cursor:pointer;color:var(--muted)">${item.label}</span><span class="sep">/</span>`;
  }).join('');
}

function goBackToSeries() {
  if (S.episode) {
    // Auto-save before going back
    const changed = collectEpisodeForm();
    if (changed) saveEpisodeSilent().then(() => navigate('series', { seriesId: S.seriesId }));
    else navigate('series', { seriesId: S.seriesId });
  } else {
    navigate('series', { seriesId: S.seriesId });
  }
}

// ── Projects view ─────────────────────────────────────────────────────────────
let _allProjects = [];
let _filterColor = '';      // '' = any
let _filterStarred = false; // true = only starred

async function loadProjects() {
  _allProjects = await api.get('/api/series');
  applyProjectFilters();
  loadBalance();
}

function applyProjectFilters() {
  let list = _allProjects;
  if (_filterColor) list = list.filter(s => (s.color || '') === _filterColor);
  if (_filterStarred) list = list.filter(s => !!s.starred);
  renderProjects(list);
  // sync filter-bar visual state
  document.querySelectorAll('.color-filter-btn').forEach(b => {
    b.classList.toggle('active', (b.dataset.color || '') === _filterColor);
  });
  const starBtn = document.getElementById('star-filter-btn');
  if (starBtn) {
    starBtn.classList.toggle('active', _filterStarred);
    starBtn.innerHTML = _filterStarred ? '★ Только избранные' : '☆ Только избранные';
  }
}

function setColorFilter(color) {
  _filterColor = color || '';
  applyProjectFilters();
}

function toggleStarFilter() {
  _filterStarred = !_filterStarred;
  applyProjectFilters();
}

const COLOR_PALETTE = ['', 'red','orange','yellow','green','teal','blue','purple','pink','gray'];

function renderProjects(list) {
  const grid = document.getElementById('projects-grid');
  const empty = document.getElementById('projects-empty');
  if (!list.length) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    // Empty-state copy depends on whether filter is on
    const ep = empty.querySelector('p');
    if (ep) {
      if (_filterColor || _filterStarred) ep.innerHTML = 'Под этот фильтр ничего не подошло.<br>Сними фильтр или измени условия.';
      else ep.innerHTML = 'Ещё нет ни одного проекта.<br>Создай первый сериал!';
    }
    setupProjectDropzones();
    return;
  }
  empty.classList.add('hidden');
  grid.innerHTML = list.map(s => {
    const colorCls = s.color ? ` color-${s.color}` : '';
    const starGlyph = s.starred ? '★' : '☆';
    return `
    <div class="project-card${s.pinned ? ' pinned' : ''}${colorCls}" draggable="true" data-sid="${s.id}" data-title="${esc(s.title)}" onclick="if(event.target.closest('.project-delete-btn')||event.target.closest('.project-pin-btn')||event.target.closest('.project-star-btn')||event.target.closest('.project-color-btn')||event.target.closest('.project-color-popover'))return;navigate('series',{seriesId:'${s.id}'})">
      <button class="project-pin-btn${s.pinned ? ' active' : ''}" type="button" onmousedown="event.stopPropagation()" ontouchstart="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();togglePin('${s.id}')" title="${s.pinned ? 'Открепить' : 'Закрепить вверху'}">${s.pinned ? '📌' : '📍'}</button>
      <div class="project-card-tools">
        <button class="project-star-btn${s.starred ? ' active' : ''}" type="button" onmousedown="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();toggleStar('${s.id}')" title="${s.starred ? 'Убрать из избранного' : 'В избранное'}">${starGlyph}</button>
        <button class="project-color-btn" type="button" onmousedown="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();openColorPicker(event,'${s.id}')" title="Цвет ячейки">🎨</button>
        <button class="project-delete-btn" type="button" onmousedown="event.stopPropagation()" ontouchstart="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();confirmDeleteSeries('${s.id}','${esc(s.title)}')" title="Удалить">✕</button>
      </div>
      <h3>${esc(s.title)}</h3>
      <div class="meta">
        <span>${esc(s.genre || '—')}</span>
        <span>${esc(s.tone || '—')}</span>
      </div>
      <div class="ep-count">${s._episode_count}/${s._episode_total ?? s._episode_count} ${s.batch_mode ? `чанков × ${s.batch_size || 5}` : 'эп.'}</div>
      <div class="project-ep-badge" title="Готовых серий: ${s._episode_count} из ${s._episode_total ?? s._episode_count}">${s._episode_count}</div>
    </div>
  `;
  }).join('');

  // Wire drag handlers on freshly rendered cards
  grid.querySelectorAll('.project-card').forEach(card => {
    card.addEventListener('dragstart', onProjectDragStart);
    card.addEventListener('dragend',   onProjectDragEnd);
  });
  setupProjectDropzones();
}

async function toggleStar(sid) {
  const s = _allProjects.find(x => x.id === sid);
  const next = !(s && s.starred);
  // Optimistic update
  if (s) s.starred = next;
  applyProjectFilters();
  try {
    await api.post(`/api/series/${sid}/meta`, { starred: next });
  } catch (e) {
    if (s) s.starred = !next;
    applyProjectFilters();
    alert('Не удалось переключить избранное: ' + e.message);
  }
}

let _colorPopoverOpen = null;
function openColorPicker(e, sid) {
  // Close any prior popover
  document.querySelectorAll('.project-color-popover').forEach(p => p.remove());
  if (_colorPopoverOpen === sid) { _colorPopoverOpen = null; return; }
  _colorPopoverOpen = sid;

  const btn = e.currentTarget;
  const card = btn.closest('.project-card');
  const current = (_allProjects.find(x => x.id === sid)?.color) || '';
  const pop = document.createElement('div');
  pop.className = 'project-color-popover';
  pop.innerHTML = COLOR_PALETTE.map(c => {
    const isActive = c === current;
    const cls = c ? `swatch-${c}` : 'swatch-clear';
    return `<button class="color-swatch ${cls}${isActive ? ' active' : ''}" data-color="${c}" title="${c || 'Без цвета'}">${c ? '' : '∅'}</button>`;
  }).join('');
  card.appendChild(pop);

  pop.querySelectorAll('.color-swatch').forEach(sw => {
    sw.addEventListener('click', async (ev) => {
      ev.stopPropagation();
      const color = sw.dataset.color || '';
      pop.remove();
      _colorPopoverOpen = null;
      await setProjectColor(sid, color);
    });
  });

  // Click-outside closes the popover
  setTimeout(() => {
    const closer = (ev) => {
      if (!pop.contains(ev.target)) {
        pop.remove();
        _colorPopoverOpen = null;
        document.removeEventListener('click', closer, true);
      }
    };
    document.addEventListener('click', closer, true);
  }, 0);
}

async function setProjectColor(sid, color) {
  const s = _allProjects.find(x => x.id === sid);
  const prev = s?.color || '';
  if (s) s.color = color;
  applyProjectFilters();
  try {
    await api.post(`/api/series/${sid}/meta`, { color });
  } catch (e) {
    if (s) s.color = prev;
    applyProjectFilters();
    alert('Не удалось задать цвет: ' + e.message);
  }
}

// ── Drag & drop: archive / trash ─────────────────────────────────────────────
let _draggingProject = null;

function onProjectDragStart(e) {
  const card = e.currentTarget;
  _draggingProject = { sid: card.dataset.sid, title: card.dataset.title };
  card.classList.add('dragging');
  try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', card.dataset.sid); } catch (_) {}
  document.body.classList.add('drag-active');
}

function onProjectDragEnd(e) {
  e.currentTarget.classList.remove('dragging');
  document.body.classList.remove('drag-active');
  document.querySelectorAll('.dropzone-btn').forEach(z => z.classList.remove('drop-hover'));
  _draggingProject = null;
}

let _dropzonesWired = false;
function setupProjectDropzones() {
  if (_dropzonesWired) return;
  _dropzonesWired = true;
  const archiveZone = document.getElementById('dz-archive');
  const trashZone   = document.getElementById('dz-trash');
  [archiveZone, trashZone].forEach(zone => {
    if (!zone) return;
    zone.addEventListener('dragover',  e => { if (_draggingProject) { e.preventDefault(); zone.classList.add('drop-hover'); } });
    zone.addEventListener('dragleave', () => zone.classList.remove('drop-hover'));
    zone.addEventListener('drop',      e => {
      if (!_draggingProject) return;
      e.preventDefault();
      zone.classList.remove('drop-hover');
      const item = _draggingProject;
      if (zone.id === 'dz-archive') archiveProject(item.sid, item.title);
      else trashProject(item.sid, item.title);
    });
  });
}

async function archiveProject(sid, title) {
  try {
    await api.post(`/api/series/${sid}/archive`, { archived: true });
    showToast(`«${title}» отправлен в архив`);
    loadProjects();
  } catch (e) { alert('Не удалось архивировать: ' + e.message); }
}

async function trashProject(sid, title) {
  const ok = confirm(
    `⚠️ УДАЛИТЬ НАВСЕГДА «${title}»?\n\n` +
    `Будут стёрты с диска ВСЕ файлы сериала: серии, скрипты, референсы, локации, видео.\n\n` +
    `Это действие НЕЛЬЗЯ отменить. Продолжить?`
  );
  if (!ok) return;
  try {
    await api.del(`/api/series/${sid}`);
    showToast(`«${title}» удалён навсегда`);
    loadProjects();
  } catch (e) { alert('Не удалось удалить: ' + e.message); }
}

// ── Archive view ─────────────────────────────────────────────────────────────
async function openArchiveModal() {
  openModal('modal-archive');
  await renderArchiveList();
}

async function renderArchiveList() {
  const list = document.getElementById('archive-list');
  const empty = document.getElementById('archive-empty');
  list.innerHTML = '<p class="muted">Загружаем…</p>';
  empty.classList.add('hidden');
  try {
    const items = await api.get('/api/series?archived=1');
    if (!items.length) {
      list.innerHTML = '';
      empty.classList.remove('hidden');
      return;
    }
    list.innerHTML = items.map(s => `
      <div class="archive-item">
        <div class="archive-info">
          <div class="archive-title">${esc(s.title)}</div>
          <div class="archive-meta">${esc(s.genre || '—')} · ${esc(s.tone || '—')} · ${s._episode_count} эп.</div>
        </div>
        <div class="archive-actions">
          <button class="btn-ghost btn-sm" onclick="restoreFromArchive('${s.id}','${esc(s.title)}')">↩ Вернуть</button>
          <button class="btn-ghost btn-sm danger" onclick="trashFromArchive('${s.id}','${esc(s.title)}')">🗑 Удалить навсегда</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = `<p class="muted">Ошибка: ${esc(e.message)}</p>`;
  }
}

async function restoreFromArchive(sid, title) {
  try {
    await api.post(`/api/series/${sid}/archive`, { archived: false });
    showToast(`«${title}» возвращён из архива`);
    await renderArchiveList();
    loadProjects();
  } catch (e) { alert('Не удалось вернуть: ' + e.message); }
}

async function trashFromArchive(sid, title) {
  const ok = confirm(`⚠️ УДАЛИТЬ НАВСЕГДА «${title}»?\n\nВсе файлы сериала будут стёрты с диска. Это действие нельзя отменить.`);
  if (!ok) return;
  try {
    await api.del(`/api/series/${sid}`);
    showToast(`«${title}» удалён`);
    await renderArchiveList();
    loadProjects();
  } catch (e) { alert('Не удалось удалить: ' + e.message); }
}

async function togglePin(sid) {
  try {
    await api.post(`/api/series/${sid}/pin`, {});
    loadProjects();
  } catch (e) { alert('Не удалось переключить закрепление: ' + e.message); }
}

async function confirmDeleteSeries(sid, title) {
  if (!confirm(`Удалить сериал «${title}»? Это действие нельзя отменить.`)) return;
  await api.del(`/api/series/${sid}`);
  loadProjects();
}

function openCreateSeries() {
  clearFields(['series-idea-input','new-series-title','new-series-genre','new-series-tone','new-series-audience','new-series-world','new-series-synopsis']);
  document.getElementById('series-ideas-list').classList.add('hidden');
  document.getElementById('series-ideas-list').innerHTML = '';
  document.getElementById('series-gen-status').textContent = '';
  const autogen = document.getElementById('new-series-autogen');
  if (autogen) autogen.checked = true;
  const singleMode = document.querySelector('input[name="new-series-mode"][value="single"]');
  if (singleMode) singleMode.checked = true;
  buildGenreFilters();
  // Reset import-mode fields too.
  setVal('import-series-title', '');
  setVal('import-series-script', '');
  document.getElementById('import-series-script-stats').textContent = '0 символов';
  document.getElementById('import-series-preview').innerHTML = '';
  setSeriesCreateMode('generate');
  openModal('modal-create-series');
}

// ── Series-create mode picker ────────────────────────────────────────────────
// Two top-level modes inside the create-series modal:
//   generate — existing AI-flow (idea → generate → fill fields)
//   import   — paste/upload an existing script, split into episodes, extract
//              chars/locs/items in the background
// Each mode shows its own block + footer button; the unused parts are hidden.
function setSeriesCreateMode(mode) {
  const isImport = mode === 'import';
  const genBlock = document.getElementById('series-generate-block');
  const impBlock = document.getElementById('series-import-block');
  if (genBlock) genBlock.style.display = isImport ? 'none' : '';
  if (impBlock) impBlock.style.display = isImport ? '' : 'none';
  const genBtn = document.getElementById('series-mode-generate-btn');
  const impBtn = document.getElementById('series-mode-import-btn');
  if (genBtn) genBtn.classList.toggle('active', !isImport);
  if (impBtn) impBtn.classList.toggle('active', isImport);
  // Footer "Создать" only relevant in generate mode (import has its own button).
  const footerCreateBtn = document.getElementById('series-generate-create-btn');
  if (footerCreateBtn) footerCreateBtn.style.display = isImport ? 'none' : '';
}

// ── Import-from-script flow ─────────────────────────────────────────────────
function importDropFile(ev) {
  const file = ev.dataTransfer?.files?.[0];
  if (!file) return;
  _importReadFile(file);
}
function importPickFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  _importReadFile(file);
}
function _importReadFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    setVal('import-series-script', reader.result || '');
    importUpdateStats();
    // Auto-fill title from filename if empty.
    const titleEl = document.getElementById('import-series-title');
    if (titleEl && !titleEl.value.trim()) {
      titleEl.value = (file.name || 'Imported series').replace(/\.[^.]+$/, '');
    }
  };
  reader.readAsText(file);
}
function importUpdateStats() {
  const txt = (document.getElementById('import-series-script')?.value || '');
  const stats = document.getElementById('import-series-script-stats');
  if (stats) stats.textContent = `${txt.length.toLocaleString('ru-RU')} символов`;
}

async function importPreviewSplit() {
  const script = (document.getElementById('import-series-script')?.value || '').trim();
  const previewEl = document.getElementById('import-series-preview');
  if (!script) { previewEl.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  previewEl.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Анализирую разбивку...</div>';
  try {
    const r = await api.post('/api/series/import-from-script/preview', { script });
    if (r.error) throw new Error(r.error);
    const eps = r.episodes || [];
    if (!eps.length) {
      previewEl.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Не удалось разбить — будет создан 1 эпизод со всем текстом</div>';
      return;
    }
    previewEl.innerHTML = `
      <div style="font-size:0.85rem;color:var(--success);margin-bottom:6px">
        ✓ Найдено эпизодов: <strong>${eps.length}</strong>
      </div>
      <div style="max-height:240px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px;background:var(--surface2)">
        ${eps.map(e => `
          <div style="padding:5px 4px;border-bottom:1px solid var(--border);font-size:0.82rem">
            <strong>Эп. ${e.number}</strong>
            ${e.title ? `<span style="color:var(--muted)"> · ${esc(e.title)}</span>` : ''}
            <span style="color:var(--muted);margin-left:8px">(${e.length.toLocaleString('ru-RU')} симв.)</span>
            <div style="color:var(--muted);font-size:0.75rem;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(e.preview)}</div>
          </div>`).join('')}
      </div>`;
  } catch (e) {
    previewEl.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  }
}

async function importCreateSeries() {
  const title = (document.getElementById('import-series-title')?.value || '').trim();
  const script = (document.getElementById('import-series-script')?.value || '').trim();
  const extract = !!document.getElementById('import-extract-entities')?.checked;
  if (!title) { alert('Введи название сериала'); return; }
  if (!script) { alert('Сценарий пустой — вставь текст или подгрузи файл'); return; }
  const btn = document.getElementById('import-create-btn');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Создаю сериал...';
  try {
    const r = await api.post('/api/series/import-from-script', {
      title, script, extract_entities: extract,
    });
    if (r.error) throw new Error(r.error);
    closeModal('modal-create-series');
    showToast(`✓ Создано: сериал + ${r.episodes_created} эпизодов${extract ? ' · извлечение запущено в фоне' : ''}`, 5000);
    navigate('series', { seriesId: r.sid });
    // The series view will pick up the import-status banner via pollImportStatus.
    if (extract) setTimeout(() => pollImportStatus(r.sid), 600);
    // First-time choice: ask for visual style right after the user creates the
    // series. They can dismiss to use cinematic default; saving locks in choice
    // for all character/loc/item generations + Seedance video.
    setTimeout(() => maybePromptForStyle(true), 900);
  } catch (e) {
    alert('Ошибка импорта: ' + (e?.message || e));
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Wire stats update on textarea typing.
document.addEventListener('DOMContentLoaded', () => {
  const ta = document.getElementById('import-series-script');
  if (ta) ta.addEventListener('input', importUpdateStats);
});

// Polls /import-status while extraction is running. Renders a banner on the
// series page (#import-progress-banner) with X/Y counter + progress bar +
// current episode label.
let _importStatusTimer = null;
async function pollImportStatus(sid) {
  if (_importStatusTimer) { clearInterval(_importStatusTimer); _importStatusTimer = null; }
  const tick = async () => {
    try {
      const st = await fetch(`/api/series/${sid}/import-status`).then(r => r.json());
      _renderImportBanner(st);
      if (!st.running && st.done > 0) {
        if (_importStatusTimer) { clearInterval(_importStatusTimer); _importStatusTimer = null; }
        // Final refresh so chars/locs/items show up.
        try {
          const fresh = await api.get(`/api/series/${sid}`);
          if (fresh && S.seriesId === sid) {
            S.series = fresh;
            renderCharactersList && renderCharactersList();
            renderLocationsList && renderLocationsList();
            renderItemsList && renderItemsList();
          }
        } catch {}
        // Banner already shows "✓ Готово" with counts — no need for an
        // additional toast. Was the second "import finished" indicator
        // user reported as «дёргается / показывает дважды».
      }
    } catch {}
  };
  await tick();
  _importStatusTimer = setInterval(tick, 3000);
}

// Single hide-timeout id so we don't stack multiple hide-trigger setTimeouts
// across poll ticks.
let _importBannerHideTimer = null;
function _renderImportBanner(st) {
  // Banner lives at #import-progress-banner pinned to body so it survives
  // renderSeriesView() rebuilding .series-main (the previous host). Multiple
  // ticks reuse the SAME element — only inner text/width update, not full
  // innerHTML rebuild (which restarted the spinner CSS animation each tick
  // and looked like flicker).
  let el = document.getElementById('import-progress-banner');
  const wasNew = !el;
  if (!el) {
    el = document.createElement('div');
    el.id = 'import-progress-banner';
    el.className = 'import-progress-banner';
    el.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);max-width:720px;width:90vw;z-index:1500';
    document.body.appendChild(el);
  }
  if (!st.running && st.done === 0) {
    el.classList.add('hidden');
    return;
  }
  el.classList.remove('hidden');
  const pct = st.total ? Math.round(100 * st.done / st.total) : 0;
  const errCount = (st.errors || []).length;
  if (wasNew || !el.querySelector('.ipb-row')) {
    // Build skeleton ONCE per banner instance.
    el.innerHTML = `
      <div class="ipb-row">
        <span class="ipb-spinner-slot"></span>
        <span class="ipb-text"></span>
      </div>
      <div class="ipb-bar"><div class="ipb-bar-fill" style="width:0%"></div></div>
    `;
  }
  // Targeted updates: spinner / done state, text, bar width. Pure text/style
  // mutations — no DOM teardown, no spinner-animation restart.
  const spinSlot = el.querySelector('.ipb-spinner-slot');
  if (spinSlot) {
    if (st.running && !spinSlot.querySelector('.ipb-spinner')) {
      spinSlot.innerHTML = '<span class="ipb-spinner"></span>';
    } else if (!st.running) {
      spinSlot.innerHTML = '<span style="color:#10b981">✓</span>';
    }
  }
  const textEl = el.querySelector('.ipb-text');
  if (textEl) {
    const cur = st.current ? `· сейчас: <span style="color:var(--muted)">${esc(st.current)}</span>` : '';
    const errs = errCount ? ` · <span style="color:var(--warning)">ошибок: ${errCount}</span>` : '';
    textEl.innerHTML = `<strong>Импорт сценария:</strong> ${st.done} / ${st.total} серий обработано ${cur}${errs}`;
  }
  const fill = el.querySelector('.ipb-bar-fill');
  if (fill) fill.style.width = `${pct}%`;
  if (!st.running) {
    if (_importBannerHideTimer) clearTimeout(_importBannerHideTimer);
    _importBannerHideTimer = setTimeout(() => {
      const cur = document.getElementById('import-progress-banner');
      if (cur) cur.remove();
      _importBannerHideTimer = null;
    }, 8000);
  } else if (_importBannerHideTimer) {
    clearTimeout(_importBannerHideTimer);
    _importBannerHideTimer = null;
  }
}

async function generateFromIdea() {
  const idea = val('series-idea-input');
  if (!idea) return alert('Опиши идею для сериала');
  const btn = document.getElementById('btn-gen-from-idea');
  const status = document.getElementById('series-gen-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = '';
  try {
    const genres = getSelectedGenres();
    const data = await trackTask('Сериал по идее', {}, () =>
      api.post('/api/generate-series-from-idea', { idea, genres })
    );
    fillSeriesForm(data);
    status.textContent = '✓ Поля заполнены — проверь и отредактируй если нужно';
    status.style.color = 'var(--success)';
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '✨ Сгенерировать по идее';
  }
}

const GENRES = [
  { value: 'Romance',          label: 'Romance',          checked: false, desc: 'Любовная история с эмоциональным напряжением, притяжением и препятствиями между героями. Сердце — отношения.' },
  { value: 'Revenge Drama',    label: 'Revenge',          checked: false, desc: 'Главная героиня была унижена или предана — и теперь методично разрушает жизни обидчиков. Катарсис через справедливость.' },
  { value: 'Cinderella',       label: 'Cinderella',       checked: false, desc: 'Девушка из низов попадает в мир богатых и влиятельных. Классический подъём через любовь, случай или скрытый талант.' },
  { value: 'Enemies to Lovers',label: 'Enemies→Lovers',  checked: false, desc: 'Герои ненавидят друг друга с первой сцены — и именно это притяжение переходит в страсть. Медленное горение.' },
  { value: 'Thriller',         label: 'Thriller',         checked: false, desc: 'Постоянное напряжение, угроза жизни или тайна, которую надо раскрыть. Зритель всегда на краю.' },
  { value: 'Melodrama',        label: 'Melodrama',        checked: false, desc: 'Семейные тайны, рождения, смерти, измены. Высокие эмоции, слёзы, прощения. Акцент на чувствах, не экшене.' },
  { value: 'Dark Drama',       label: 'Dark Drama',       checked: false, desc: 'Мрачный реализм без хэппи-энда. Герои морально неоднозначны, мир жесток, выборы — без правильного ответа.' },
  { value: 'Mystery',          label: 'Mystery',          checked: false, desc: 'Загадка, которую герои (и зритель) распутывают по кусочкам. Каждый эпизод — новая деталь головоломки.' },
  { value: 'Supernatural',     label: 'Supernatural',     checked: false, desc: 'Магия, судьба, реинкарнация, духи или сверхъестественные силы вплетены в бытовой конфликт.' },
  { value: 'Comedy',           label: 'Comedy',           checked: false, desc: 'Лёгкий тон, ситуативный юмор, недопонимания и неловкие моменты. Конфликт смешной, а не травмирующий.' },
  { value: 'Power Struggle',   label: 'Power Struggle',   checked: false, desc: 'Война за власть — в корпорации, семье, или политике. Кто наверху, кто внизу — и как это меняется.' },
  { value: 'Forbidden Love',   label: 'Forbidden Love',   checked: false, desc: 'Отношения, которые запрещены — из-за семьи, класса, закона или обстоятельств. Страсть против правил.' },
  { value: 'Coming of Age',    label: 'Coming of Age',    checked: false, desc: 'Молодая героиня взрослеет через боль, ошибки и открытия. История становления характера.' },
  { value: 'Scandal',          label: 'Scandal',          checked: false, desc: 'Тайная жизнь богатых и влиятельных разрушается под давлением огласки. Ложь, измены, секреты.' },
  { value: 'Second Chance',    label: 'Second Chance',    checked: false, desc: 'Бывшие влюблённые или старые враги встречаются снова. Старые раны открываются — но есть шанс исправить прошлое.' },
  { value: 'Obsession',        label: 'Obsession',        checked: false, desc: 'Один персонаж одержим другим — романтически или мстительно. Граница между страстью и опасностью размыта.' },
  { value: 'Rags to Riches',   label: 'Rags to Riches',  checked: false, desc: 'Героиня поднимается из бедности к власти и богатству своими силами. Триумф через лишения.' },
  { value: 'Hidden Identity',  label: 'Hidden Identity',  checked: false, desc: 'Герой скрывает кто он на самом деле. Когда правда выйдет — всё изменится.' },
  { value: 'Betrayal',         label: 'Betrayal',         checked: false, desc: 'Предательство близкого человека — главный двигатель сюжета. Кому верить, когда все лгут?' },
  { value: 'Family Secrets',   label: 'Family Secrets',   checked: false, desc: 'Семья хранит тёмные тайны. Когда они всплывают — рушится всё, что герои считали правдой.' },
  { value: 'Pregnancy Drama',  label: '🤰 Pregnancy',     checked: false, desc: 'Беременность как двигатель сюжета: скрытый ребёнок, отцовство под вопросом, шантаж, воссоединение или финальный реванш через наследника.' },
  { value: 'Cinderella Revenge', label: '👑 Cinderella Revenge', checked: false, desc: 'Унижают официантку — а она уже владелец компании. Скрытый статус + мгновенная карма: чем больше унижение, тем сокрушительнее реванш. Взлёт → падение → взлёт с союзником.' },
  { value: 'Mafia Romance',    label: '🔫 Mafia',          checked: false, desc: 'Лидер мафии или криминального мира как любовный интерес или союзник. Опасность и защита в одном человеке. Власть через страх.' },
  { value: 'CEO Drama',        label: '💼 CEO Drama',      checked: false, desc: 'Корпоративная власть, враждебные поглощения, наследники и самозванцы. Офис как поле боя, деловые переговоры как война.' },
];

function buildGenreFilters() {
  const block = document.getElementById('genre-filter-block');
  if (!block) return;
  const label = block.querySelector('.genre-filter-label');
  block.innerHTML = '';
  block.appendChild(label);
  GENRES.forEach(g => {
    const lbl = document.createElement('label');
    lbl.className = 'genre-chip';
    if (g.checked) lbl.classList.add('genre-chip-checked');
    lbl.innerHTML = `<input type="checkbox" value="${g.value}" ${g.checked ? 'checked' : ''}> ${g.label}`;
    lbl.addEventListener('mouseenter', (e) => showGenreTooltip(g.desc, e));
    lbl.addEventListener('mouseleave', hideGenreTooltip);
    lbl.querySelector('input').addEventListener('change', (e) => {
      lbl.classList.toggle('genre-chip-checked', e.target.checked);
    });
    block.appendChild(lbl);
  });
}

function showGenreTooltip(desc, e) {
  const tip = document.getElementById('genre-tooltip');
  if (!tip) return;
  tip.textContent = desc;
  tip.classList.remove('hidden');
  const rect = e.target.closest('.genre-chip').getBoundingClientRect();
  const blockRect = document.getElementById('genre-filter-block').getBoundingClientRect();
  tip.style.top = (rect.bottom - blockRect.top + 6) + 'px';
  tip.style.left = Math.max(0, rect.left - blockRect.left) + 'px';
}

function hideGenreTooltip() {
  document.getElementById('genre-tooltip')?.classList.add('hidden');
}

function getSelectedGenres() {
  return [...document.querySelectorAll('.genre-chip input:checked')].map(cb => cb.value);
}

function randomizeGenres() {
  // Pick 2-4 random genres, uncheck everything else
  const chips = [...document.querySelectorAll('.genre-chip')];
  if (!chips.length) return;
  const count = 2 + Math.floor(Math.random() * 3); // 2, 3, or 4
  const indices = [...chips.keys()];
  // Fisher-Yates shuffle, take first `count`
  for (let i = indices.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [indices[i], indices[j]] = [indices[j], indices[i]];
  }
  const picked = new Set(indices.slice(0, count));
  chips.forEach((chip, idx) => {
    const cb = chip.querySelector('input');
    cb.checked = picked.has(idx);
    chip.classList.toggle('genre-chip-checked', cb.checked);
  });
}

async function generateSeriesIdeas() {
  const btn = document.getElementById('btn-gen-ideas');
  const status = document.getElementById('series-gen-status');
  const list = document.getElementById('series-ideas-list');
  const genres = getSelectedGenres();
  // No genres = full creative freedom across all 6 axes (settings/twists/
  // premises/protag/antag/tones). Backend handles empty genres list fine —
  // see /api/generate-series-ideas: genre_rule is empty when genres=[].
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = '';
  list.classList.add('hidden');
  list.innerHTML = '';
  try {
    const ideas = await api.post('/api/generate-series-ideas', { genres });
    list.innerHTML = ideas.map((idea, i) => `
      <div class="idea-card" onclick="pickSeriesIdea(${i})">
        <div class="idea-card-title">${esc(idea.title)}</div>
        <div class="idea-card-meta">${esc(idea.genre)} · ${esc(idea.tone)} · ${esc(idea.target_audience)}</div>
        <div class="idea-card-synopsis">${esc(idea.synopsis_ru || idea.synopsis)}</div>
      </div>
    `).join('');
    list._ideas = ideas;
    list.classList.remove('hidden');
    status.textContent = 'Выбери идею — поля заполнятся автоматически';
    status.style.color = 'var(--muted)';
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '💡 5 идей на выбор';
  }
}

function pickSeriesIdea(index) {
  const list = document.getElementById('series-ideas-list');
  const idea = list._ideas?.[index];
  if (!idea) return;
  fillSeriesForm(idea);
  list.classList.add('hidden');
  const status = document.getElementById('series-gen-status');
  status.textContent = '✓ Поля заполнены — проверь и отредактируй если нужно';
  status.style.color = 'var(--success)';
}

function fillSeriesForm(data) {
  if (data.title)            setVal('new-series-title', data.title);
  if (data.genre)            setVal('new-series-genre', data.genre);
  if (data.tone)             setVal('new-series-tone', data.tone);
  if (data.target_audience)  setVal('new-series-audience', data.target_audience);
  if (data.world_description) setVal('new-series-world', data.world_description);
  if (data.synopsis)         setVal('new-series-synopsis', data.synopsis);
}

async function createSeries() {
  const title = val('new-series-title');
  if (!title) return alert('Введи название');
  const autogen = document.getElementById('new-series-autogen')?.checked ?? true;
  try {
    const data = await api.post('/api/series', {
      title, genre: val('new-series-genre'), tone: val('new-series-tone'),
      target_audience: val('new-series-audience'), world_description: val('new-series-world'),
      synopsis: val('new-series-synopsis'),
      auto_generate_assets: autogen,
      batch_mode: false,
      batch_size: 1,
    });
    closeModal('modal-create-series');
    if (data?._scaffold?.prproj_warning) {
      showToast('⚠ ' + data._scaffold.prproj_warning + ' (templates/empty.prproj)');
    }
    navigate('series', { seriesId: data.id });
    // Same first-time style prompt as the import flow.
    setTimeout(() => maybePromptForStyle(true), 900);
  } catch(e) {
    showToast('Ошибка: ' + e.message);
  }
}

// Toggle "ready" on the current episode — instant save so the projects-grid badge updates.
async function onReadyToggle() {
  const el = document.getElementById('ep-ready-toggle');
  if (!el || !S.seriesId || !S.episodeNum) return;
  const ready = !!el.checked;
  try {
    await api.put(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`, { ready });
    if (S.episode) S.episode.ready = ready;
    showToast(ready ? '✓ Серия отмечена как готовая' : 'Серия снова в работе');
  } catch(e) {
    el.checked = !ready;
    showToast('Ошибка: ' + e.message);
  }
}

// Rename files in <series>/OUT/ to studio delivery convention.
async function renameOutFiles() {
  if (!S.seriesId) return;
  if (!confirm('Переименовать все файлы в папке OUT по студийным требованиям?\n\nВидео → Series_Name_E1.mp4\nАудио → VO_/MUS_/SFX_Series_Name_N.wav\n\nФайлы которые не получится опознать — будут пропущены.')) return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/rename-out`, {});
    const lines = [];
    lines.push(`Переименовано: ${r.renamed.length} из ${r.total_files}`);
    if (r.skipped.length) lines.push(`Пропущено: ${r.skipped.length}`);
    if (r.errors.length) lines.push(`Ошибок: ${r.errors.length}`);
    showToast(lines.join(' · '));
    // Detailed report in console for debugging / spot-check
    console.group('[rename-out] ' + r.series_safe_name);
    if (r.renamed.length) { console.log('renamed:'); r.renamed.forEach(x => console.log('  ', x.from, '→', x.to)); }
    if (r.skipped.length) { console.warn('skipped:'); r.skipped.forEach(x => console.warn('  ', x.name, '—', x.reason)); }
    if (r.errors.length) { console.error('errors:'); r.errors.forEach(x => console.error('  ', x.name, '—', x.error)); }
    console.groupEnd();
    if (r.skipped.length || r.errors.length) {
      const detail = [
        r.skipped.length ? 'ПРОПУЩЕНЫ:\n' + r.skipped.map(x=>`• ${x.name} — ${x.reason}`).join('\n') : '',
        r.errors.length  ? 'ОШИБКИ:\n'   + r.errors.map(x=>`• ${x.name} — ${x.error}`).join('\n') : '',
      ].filter(Boolean).join('\n\n');
      alert(detail);
    }
  } catch(e) {
    showToast('Ошибка: ' + e.message);
  }
}

// Toggle auto-generate-assets on an existing series + trigger an immediate sweep
async function toggleSeriesAutogen() {
  const cb = document.getElementById('series-autogen-toggle');
  if (!cb) return;
  const enabled = cb.checked;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/auto-generate`, { enabled });
    S.series.auto_generate_assets = r.enabled;
    if (r.enabled && r.sweep_started) {
      showToast('Авто-генерация включена — недостающие картинки уже генерятся');
    } else if (r.enabled) {
      showToast('Авто-генерация включена');
    } else {
      showToast('Авто-генерация выключена');
    }
  } catch (e) {
    cb.checked = !enabled; // revert
    showToast('Ошибка: ' + e.message);
  }
}

let _autogenPollTimer = null;

async function triggerAutogenSweep() {
  const btns = [
    document.getElementById('autogen-sweep-btn'),
    document.getElementById('autogen-sweep-btn-ep'),
  ].filter(Boolean);
  const statuses = [
    document.getElementById('autogen-sweep-status'),
    document.getElementById('autogen-sweep-status-ep'),
  ].filter(Boolean);
  if (!btns.length) return;
  btns.forEach(b => { b.disabled = true; b.innerHTML = '<span class="spinner"></span> запускаю…'; });
  try {
    await fetch(`/api/series/${S.seriesId}/auto-generate/sweep`, {method: 'POST'});
    pollAutogenStatus();
  } catch (e) {
    btns.forEach(b => { b.disabled = false; b.innerHTML = '🎨 Сгенерировать недостающее'; });
    statuses.forEach(s => { s.textContent = 'Ошибка: ' + e.message; });
  }
}

function _autogenApplyInProgress(inProgress) {
  // Reset all overlays
  document.querySelectorAll('[data-autogen-kind] .autogen-overlay').forEach(el => {
    el.hidden = true;
  });
  document.querySelectorAll('[data-autogen-kind].is-generating').forEach(el => {
    el.classList.remove('is-generating');
  });
  // Apply current in-progress entries (char + loc + item — outfits live in modal)
  for (const entry of (inProgress || [])) {
    if (!['char', 'loc', 'item'].includes(entry.kind)) continue;
    const sel = `[data-autogen-kind="${entry.kind}"][data-autogen-id="${entry.parent_id}"]`;
    const card = document.querySelector(sel);
    if (!card) continue;
    card.classList.add('is-generating');
    const ov = card.querySelector('.autogen-overlay');
    if (ov) ov.hidden = false;
  }
}

async function pollAutogenStatus() {
  // Two button/status pairs: one in series sidebar, one in episode sidebar.
  // Helpers below mutate BOTH so the user sees identical state regardless
  // of which view they're on.
  const _allBtns = () => [
    document.getElementById('autogen-sweep-btn'),
    document.getElementById('autogen-sweep-btn-ep'),
  ].filter(Boolean);
  const _allStatuses = () => [
    document.getElementById('autogen-sweep-status'),
    document.getElementById('autogen-sweep-status-ep'),
  ].filter(Boolean);
  const _setBtnHtml = (html) => _allBtns().forEach(b => { b.innerHTML = html; });
  const _setBtnDisabled = (d) => _allBtns().forEach(b => { b.disabled = d; });
  const _setStatusText = (t) => _allStatuses().forEach(s => { s.textContent = t; });
  const _setStatusHtml = (h) => _allStatuses().forEach(s => { s.innerHTML = h; });
  const status = document.getElementById('autogen-sweep-status') || document.getElementById('autogen-sweep-status-ep');
  const btn = document.getElementById('autogen-sweep-btn') || document.getElementById('autogen-sweep-btn-ep');
  if (!status && !btn) return;
  if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }

  // Track previous done count so we know when to re-fetch+re-render lists
  let prevDone = -1;
  const tick = async () => {
    try {
      const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
      const st = await r.json();
      // Mark in-progress items on DOM (spinner overlay) and clear stale marks
      _autogenApplyInProgress(st.in_progress || []);
      if (st.running) {
        const ipBits = (st.in_progress || []).map(x => x.name).filter(Boolean).slice(0, 3).join(', ');
        const ipSuffix = ipBits ? ` · сейчас: ${ipBits}` : '';
        _setStatusText(`генерация… ${st.done}/${st.queue}` + (st.errors.length ? ` · ошибок: ${st.errors.length}` : '') + ipSuffix);
        _setBtnHtml('<span class="spinner"></span> ' + st.done + '/' + st.queue);
        // Re-fetch + re-render every time `done` increments — lets assets pop in live
        if (prevDone !== -1 && st.done > prevDone) {
          try {
            const fresh = await fetch(`/api/series/${S.seriesId}`).then(r => r.json());
            S.series = fresh;
            // Asset on disk changed — invalidate the cache-buster so next render
            // fetches fresh image bytes instead of showing the previous version.
            bumpAssetVersion();
            renderCharactersList();
            renderLocationsList();
            renderItemsList();
            // Episode-side panels also have <img> for the same chars/locs/items —
            // they'd otherwise keep showing stale until user navigates away.
            if (typeof renderEpCharacters === 'function') renderEpCharacters();
            if (typeof renderEpLocations === 'function') renderEpLocations();
            if (typeof renderEpItems === 'function') renderEpItems();
            _autogenApplyInProgress(st.in_progress || []);  // re-apply spinners after re-render wipes them
          } catch {}
        }
        prevDone = st.done;
      } else {
        if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }
        _setBtnDisabled(false);
        _setBtnHtml('🎨 Сгенерировать недостающее');
        if (st.queue === 0 && st.done === 0) {
          _setStatusText('— ничего не нужно генерить');
        } else if (st.errors && st.errors.length) {
          _setStatusHtml(`<span style="color:var(--danger,#f87171)">готово ${st.done}/${st.queue} · ошибок ${st.errors.length}</span>`);
          showToast('Авто-генерация: ошибок — ' + st.errors.length + '. Подробности в консоли сервера.');
          console.warn('[autogen errors]', st.errors);
        } else {
          _setStatusHtml(`<span style="color:var(--success,#4ade80)">✓ готово ${st.done}/${st.queue}</span>`);
        }
        // Refresh series state to show new images
        try {
          const fresh = await fetch(`/api/series/${S.seriesId}`).then(r => r.json());
          S.series = fresh;
          renderCharactersList();
          renderLocationsList();
          renderItemsList();
        } catch {}
        setTimeout(() => _setStatusText(''), 6000);
      }
    } catch (e) {
      if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }
      _setBtnDisabled(false);
      _setBtnHtml('🎨 Сгенерировать недостающее');
      _setStatusText('Ошибка опроса: ' + e.message);
    }
  };
  await tick();
  _autogenPollTimer = setInterval(tick, 2500);
}

// Pre-flight check before any video generation. Returns list of missing assets:
//   { chars: [{name, missing}], outfits: [{char, label}], locs: [{name}] }
// where `missing` is 'base' or 'outfit_label'. Empty arrays = all good.
function _checkMissingAssets(scope = 'episode') {
  const out = { chars: [], outfits: [], locs: [], running: false };
  if (!S.series || !S.episode) return out;
  const charById = Object.fromEntries((S.series.characters || []).map(c => [c.id, c]));
  const locById = Object.fromEntries((S.series.locations || []).map(l => [l.id, l]));
  const usedCharIds = new Set(S.episode.characters_used || []);
  const usedLocIds  = new Set(S.episode.locations_used  || []);
  const charOutfits = S.episode.character_outfits || {};
  for (const cid of usedCharIds) {
    const c = charById[cid];
    if (!c) continue;
    if (!c.ref_images || !c.ref_images.length) {
      out.chars.push({ name: c.name, id: cid });
    }
    // Check requested outfits for this char. The episode stores values that
    // may be either outfit LABELS or outfit IDS depending on when the entry
    // was written (legacy episodes mix both). Try both to find the outfit.
    // If the value matches NEITHER an existing label NOR an existing id —
    // it's an orphan ref pointing at a deleted/renamed outfit. We silently
    // ignore it instead of reporting "missing" (the outfit literally doesn't
    // exist anymore so there's nothing to generate). This is what was
    // happening in the user's screenshot: SARAH/49d33f6b, MARCUS/d22d7423,
    // ELENA/c5d28813 were stale 8-char outfit ids; backend autogen saw all
    // real outfits already had photos and reported "ничего не нужно генерить",
    // while this preflight kept screaming about orphans.
    const requested = charOutfits[cid] || [];
    const labels = Array.isArray(requested) ? requested : (requested ? [requested] : []);
    for (const ref of labels) {
      if (!ref || ref === 'base') continue;
      const outfits = c.outfits || [];
      const outfit = outfits.find(o => o.label === ref) || outfits.find(o => o.id === ref);
      if (!outfit) continue;  // orphan — drop silently
      if (!outfit.photo && !outfit.is_base) {
        out.outfits.push({ char: c.name, label: outfit.label });
      }
    }
  }
  for (const lid of usedLocIds) {
    const l = locById[lid];
    if (!l) continue;
    if (!l.ref_images || !l.ref_images.length) {
      out.locs.push({ name: l.name, id: lid });
    }
  }
  return out;
}

// Confirm dialog returning a Promise<bool>. Returns false if user cancels.
// Empty missing-list returns true without prompting.
async function _confirmMissingAssetsBeforeGen(label = 'генерации') {
  const missing = _checkMissingAssets();
  const total = missing.chars.length + missing.outfits.length + missing.locs.length;
  if (total === 0) return true;
  // Check if autogen is currently running — friendlier message
  let runningSuffix = '';
  try {
    const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
    const st = await r.json();
    if (st.running) {
      runningSuffix = `\n\n⏳ Авто-генерация СЕЙЧАС идёт: ${st.done}/${st.queue}. Можешь подождать ~30-60с — оставшиеся ассеты доделаются.`;
    } else {
      runningSuffix = `\n\n⚠ Auto-generate не запущен. Нажми "🎨 Сгенерировать недостающее" в панели персонажей/локаций, или запусти видео-генерацию всё равно — но сцена будет с placeholder/неправильным видом для перечисленных выше.`;
    }
  } catch {}
  const lines = [];
  if (missing.chars.length) lines.push(`👤 Персонажи без портрета: ${missing.chars.map(c => c.name).join(', ')}`);
  if (missing.outfits.length) lines.push(`👕 Аутфиты не сгенерены: ${missing.outfits.map(o => `${o.char}/${o.label}`).join(', ')}`);
  if (missing.locs.length) lines.push(`🏛 Локации без фото: ${missing.locs.map(l => l.name).join(', ')}`);
  const msg = `⚠ Не все ассеты этой сцены готовы:\n\n${lines.join('\n')}${runningSuffix}\n\nЗапустить ${label} всё равно?`;
  return confirm(msg);
}

// Auto-resume polling if a sweep is running when the user opens the page
async function checkAutogenOnLoad() {
  try {
    const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
    const st = await r.json();
    if (st.running) pollAutogenStatus();
  } catch {}
  // Always start the heartbeat — it watches for autogen runs that kick off
  // AFTER the page loaded (e.g. server-side trigger from /extract-characters
  // or get_series self-heal). Without this, the spinner overlay only appears
  // for sweeps that were already running at page-load time.
  _startAutogenHeartbeat();
}

// Lightweight heartbeat that polls autogen status every 4s while user is on
// a series/episode view. When it sees `running=true` and no UI poller is
// active, it attaches the live UI poller. When sweeps kick off in the
// background (autogen sometimes triggers from the server side after script-gen
// or get_series self-heal), this catches them automatically — without needing
// a page refresh.
let _autogenHeartbeatTimer = null;
function _startAutogenHeartbeat() {
  if (_autogenHeartbeatTimer) return;
  _autogenHeartbeatTimer = setInterval(async () => {
    if (!S.seriesId) return;
    try {
      const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
      const st = await r.json();
      // If sweep is running AND we don't have an active UI poller — attach.
      if (st.running && !_autogenPollTimer) {
        pollAutogenStatus();
      }
      // If sweep just finished (no longer running) AND we still see in_progress
      // markers from a previous render — clear them.
      if (!st.running && (!st.in_progress || !st.in_progress.length)) {
        _autogenApplyInProgress([]);
      }
    } catch {}
  }, 4000);
}

// ── Production pipeline ───────────────────────────────────────────────────────

function renderPipeline() {
  const el = document.getElementById('pipeline-section');
  if (!el) return;
  // The legacy stage-1/2 milestones grid has been retired. New series start
  // empty; the user adds episodes via "+ Эпизод" and may pin checkpoints / finale
  // via the toolbar. Story-landmarks panel is rendered separately.
  el.innerHTML = '';
  renderLandmarksPanel();
}

// ── Story landmarks panel (checkpoints + finale) ─────────────────────────────
function renderLandmarksPanel() {
  const el = document.getElementById('landmarks-panel');
  if (!el) return;
  const cps = (S.series.checkpoints || []).slice().sort((a,b) => a.episode - b.episode);
  const fin = S.series.finale;
  if (!cps.length && !fin) { el.innerHTML = ''; return; }

  const cpsHtml = cps.map(c => `
    <div class="landmark-card" onclick="openCheckpointModal(${c.episode})" title="Редактировать">
      <div class="landmark-badge cp">Эп. ${c.episode}</div>
      <div class="landmark-text">${esc(c.description || '—')}</div>
      <button class="landmark-del" onclick="event.stopPropagation();confirmDeleteCheckpoint(${c.episode})" title="Удалить точку">✕</button>
    </div>
  `).join('');

  const finHtml = fin ? `
    <div class="landmark-card finale" onclick="openFinaleModal()" title="Редактировать">
      <div class="landmark-badge fin">🏁 Финал · Эп. ${fin.episode}</div>
      <div class="landmark-text">${esc(fin.description || '—')}</div>
    </div>
  ` : '';

  el.innerHTML = `
    <div class="landmarks-wrap">
      <div class="landmarks-header">📌 Сюжетные ориентиры</div>
      <div class="landmarks-list">${cpsHtml}${finHtml}</div>
    </div>
  `;
}

// ── Checkpoint modal ─────────────────────────────────────────────────────────
let _editingCheckpointEp = null;

function openCheckpointModal(ep) {
  _editingCheckpointEp = (typeof ep === 'number') ? ep : null;
  const cp = (S.series.checkpoints || []).find(c => c.episode === _editingCheckpointEp);
  setVal('cp-episode', cp ? cp.episode : '');
  setVal('cp-description', cp ? (cp.description || '') : '');
  document.getElementById('modal-checkpoint-title').textContent = cp ? '🎯 Контрольная точка (редактирование)' : '🎯 Новая контрольная точка';
  document.getElementById('cp-delete-btn').style.display = cp ? '' : 'none';
  document.getElementById('cp-status').textContent = '';
  openModal('modal-checkpoint');
}

async function generateCheckpointDraft() {
  const epStr = val('cp-episode');
  const ep = parseInt(epStr, 10);
  if (!ep || ep < 1) { showToast('Сначала укажи номер серии'); return; }
  const btn = document.getElementById('cp-gen-btn');
  const status = document.getElementById('cp-status');
  btn.disabled = true; const orig = btn.innerHTML; btn.innerHTML = '<span class="spinner"></span> Думаем...';
  status.textContent = '';
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title, episodeNum: ep };
    const res = await trackTask(`Контрольная точка Эп. ${ep}`, ctx, () =>
      api.post(`/api/series/${S.seriesId}/checkpoints/${ep}/generate`, {}, { timeoutMs: 90_000 })
    );
    if (res.description) {
      setVal('cp-description', res.description);
      status.textContent = '✓ Драфт готов — отредактируй или сохрани';
      status.style.color = 'var(--success)';
    }
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

async function saveCheckpoint() {
  const ep = parseInt(val('cp-episode'), 10);
  const description = val('cp-description').trim();
  if (!ep || ep < 1) { showToast('Укажи корректный номер серии'); return; }
  if (!description) { showToast('Опиши, что должно произойти, или сгенерируй драфт'); return; }
  const status = document.getElementById('cp-status');
  const saveBtn = document.getElementById('cp-save-btn');
  saveBtn.disabled = true;
  try {
    // If user changed the episode number while editing, drop the old entry first
    if (_editingCheckpointEp !== null && _editingCheckpointEp !== ep) {
      try { await api.del(`/api/series/${S.seriesId}/checkpoints/${_editingCheckpointEp}`); } catch (_) {}
    }
    const res = await api.post(`/api/series/${S.seriesId}/checkpoints`, { episode: ep, description });
    S.series.checkpoints = res.checkpoints;
    renderLandmarksPanel();
    closeModal('modal-checkpoint');
    showToast(`Контрольная точка на эп. ${ep} сохранена`);
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    saveBtn.disabled = false;
  }
}

async function deleteCheckpoint() {
  if (_editingCheckpointEp === null) return;
  if (!confirm(`Удалить контрольную точку на эп. ${_editingCheckpointEp}?`)) return;
  try {
    const res = await api.del(`/api/series/${S.seriesId}/checkpoints/${_editingCheckpointEp}`);
    S.series.checkpoints = res.checkpoints;
    renderLandmarksPanel();
    closeModal('modal-checkpoint');
  } catch (e) {
    alert('Не удалось удалить: ' + e.message);
  }
}

async function confirmDeleteCheckpoint(ep) {
  if (!confirm(`Удалить контрольную точку на эп. ${ep}?`)) return;
  try {
    const res = await api.del(`/api/series/${S.seriesId}/checkpoints/${ep}`);
    S.series.checkpoints = res.checkpoints;
    renderLandmarksPanel();
  } catch (e) {
    alert('Не удалось удалить: ' + e.message);
  }
}

// ── Finale modal ─────────────────────────────────────────────────────────────
function openFinaleModal() {
  const fin = S.series.finale;
  setVal('fin-episode', fin ? fin.episode : '');
  setVal('fin-description', fin ? (fin.description || '') : '');
  document.getElementById('fin-delete-btn').style.display = fin ? '' : 'none';
  document.getElementById('fin-status').textContent = '';
  openModal('modal-finale');
}

async function generateFinaleDraft() {
  const ep = parseInt(val('fin-episode'), 10);
  if (!ep || ep < 1) { showToast('Сначала укажи номер финальной серии'); return; }
  const btn = document.getElementById('fin-gen-btn');
  const status = document.getElementById('fin-status');
  btn.disabled = true; const orig = btn.innerHTML; btn.innerHTML = '<span class="spinner"></span> Пишем финал...';
  status.textContent = '';
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title, episodeNum: ep };
    const res = await trackTask(`Финал Эп. ${ep}`, ctx, () =>
      api.post(`/api/series/${S.seriesId}/finale/generate`, { episode: ep }, { timeoutMs: 90_000 })
    );
    if (res.description) {
      setVal('fin-description', res.description);
      status.textContent = '✓ Драфт готов — отредактируй или сохрани';
      status.style.color = 'var(--success)';
    }
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

async function saveFinale() {
  const ep = parseInt(val('fin-episode'), 10);
  const description = val('fin-description').trim();
  if (!ep || ep < 1) { showToast('Укажи корректный номер финальной серии'); return; }
  if (!description) { showToast('Опиши финал или сгенерируй драфт'); return; }
  const saveBtn = document.getElementById('fin-save-btn');
  const status = document.getElementById('fin-status');
  saveBtn.disabled = true;
  try {
    const res = await api.put(`/api/series/${S.seriesId}/finale`, { episode: ep, description });
    S.series.finale = res.finale;
    renderLandmarksPanel();
    closeModal('modal-finale');
    showToast(`Финал на эп. ${ep} сохранён`);
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    saveBtn.disabled = false;
  }
}

async function deleteFinale() {
  if (!confirm('Убрать финал? (точку можно будет указать заново)')) return;
  try {
    await api.del(`/api/series/${S.seriesId}/finale`);
    S.series.finale = null;
    renderLandmarksPanel();
    closeModal('modal-finale');
  } catch (e) {
    alert('Не удалось удалить: ' + e.message);
  }
}

// ── Stage 1: Milestones ───────────────────────────────────────────────────────
const MILESTONES = [1, 10, 20, 30, 40, 50, 60, 70]; // legacy single-mode constant
const MILESTONES_REQUIRED = [1, 10, 70];

function renderStage1(el) {
  const s = S.series;
  const ms = s.milestone_synopses || {};
  const milestones = milestoneIndices(s);
  const required = requiredMilestones(s);
  const allDone = required.every(n => ms[String(n)]);
  const total = chunkCount(s);
  const batch = isBatchMode(s);
  const firstN = milestones[0];
  const lastN = milestones[milestones.length - 1];
  const turnN = batch ? epToChunk(s, 10) : 10;
  const genLabel = batch
    ? `⚡ Сгенерировать ${chunkLabel(s, firstN, {short:true})}, ${chunkLabel(s, turnN, {short:true})}, ${chunkLabel(s, lastN, {short:true})}`
    : '⚡ Сгенерировать Эп. 1, 10, 70';
  el.innerHTML = `
    <div class="pipeline-stage">
      <div class="pipeline-stage-header">
        <div class="pipeline-stage-num">1</div>
        <div class="pipeline-stage-title">Контрольные точки${batch ? ` <span style="font-size:0.78rem;color:var(--muted);font-weight:400">· ${total} чанков по ${batchSize(s)} серий</span>` : ''}</div>
      </div>
      <div class="pipeline-stage-body">

        <div>
          <div style="font-size:0.78rem;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.7px">Синопсис</div>
          <div class="pipeline-synopsis-box">${esc(s.synopsis || '—')}</div>
        </div>

        <div class="pipeline-row">
          <button class="btn-idea-gen" id="pl-gen-ms-btn" onclick="plGenerateMilestones()">${genLabel}</button>
        </div>

        <div class="milestones-grid" id="pl-milestones-grid">
          ${milestones.map(n => {
            const isFirst = n === firstN, isLast = n === lastN;
            const tag = isFirst ? 'Пилот' : (isLast ? 'Финал' : '');
            const badgeText = batch ? `${chunkLabel(s, n, {short:true})}` : `Эп. ${n}`;
            return `
            <div class="milestone-card" id="ms-card-${n}">
              <div class="milestone-ep-badge is-milestone">${badgeText}${tag ? `<br><span style="font-size:0.65rem;font-weight:400;color:var(--muted)">${tag}</span>` : ''}</div>
              <textarea class="milestone-textarea" id="ms-ta-${n}" placeholder="Синопсис ${batch ? chunkLabel(s, n).toLowerCase() : 'эпизода ' + n}..." onchange="plSaveMilestone(${n})">${esc(ms[String(n)] || '')}</textarea>
              <div class="milestone-actions">
                <button class="milestone-regen-btn" id="ms-regen-${n}" onclick="plRegenMilestone(${n})" title="Перегенерировать">↻</button>
                ${ms[String(n)] ? `<button class="milestone-clear-btn" onclick="plClearMilestone(${n})" title="Очистить">✕</button>` : ''}
              </div>
            </div>`;
          }).join('')}
        </div>

        <div id="pl-stage2-status" class="pipeline-status"></div>

        <div style="display:flex;justify-content:flex-end">
          <button class="pipeline-confirm-btn" ${allDone ? '' : 'disabled'} id="pl-confirm-ms-btn" onclick="plConfirmMilestones()">→ Подтвердить и перейти к эпизодам</button>
        </div>
      </div>
    </div>
  `;
}

async function plGenerateMilestones() {
  const btn = document.getElementById('pl-gen-ms-btn');
  const status = document.getElementById('pl-stage2-status');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = ''; status.className = 'pipeline-status';
  try {
    const ms = await api.post(`/api/series/${S.seriesId}/generate-milestones`, {});
    S.series = await api.get(`/api/series/${S.seriesId}`);
    renderStage2(document.getElementById('pipeline-section'));
    const labelList = requiredMilestones(S.series).map(n => chunkLabel(S.series, n, {short:true})).join(', ');
    status.textContent = `✓ ${labelList} сгенерированы. Остальные точки можно заполнить вручную или через ↻`; status.className = 'pipeline-status ok';
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
    const labelList = requiredMilestones(S.series).map(n => chunkLabel(S.series, n, {short:true})).join(', ');
    btn.disabled = false; btn.innerHTML = `⚡ Сгенерировать ${labelList}`;
  }
}

async function plSaveMilestone(n) {
  const ta = document.getElementById(`ms-ta-${n}`);
  if (!ta) return;
  await api.put(`/api/series/${S.seriesId}/milestones/${n}`, { synopsis: ta.value });
  S.series.milestone_synopses = S.series.milestone_synopses || {};
  S.series.milestone_synopses[String(n)] = ta.value;
  const confirmBtn = document.getElementById('pl-confirm-ms-btn');
  if (confirmBtn) confirmBtn.disabled = !requiredMilestones(S.series).every(m => (S.series.milestone_synopses[String(m)] || '').trim());
}

async function plClearMilestone(n) {
  await api.put(`/api/series/${S.seriesId}/milestones/${n}`, { synopsis: '' });
  S.series.milestone_synopses = S.series.milestone_synopses || {};
  S.series.milestone_synopses[String(n)] = '';
  const ta = document.getElementById(`ms-ta-${n}`);
  if (ta) ta.value = '';
  // Re-render to remove the clear button
  renderStage2(document.getElementById('pipeline-section'));
}

async function plRegenMilestone(n) {
  const btn = document.getElementById(`ms-regen-${n}`);
  const status = document.getElementById('pl-stage2-status');
  btn.disabled = true; btn.textContent = '...';
  try {
    await plSaveMilestone(n);
    const res = await api.post(`/api/series/${S.seriesId}/milestones/${n}/regenerate`, {});
    bumpAssetVersion();
    const ta = document.getElementById(`ms-ta-${n}`);
    if (ta) ta.value = res.synopsis;
    S.series.milestone_synopses = S.series.milestone_synopses || {};
    S.series.milestone_synopses[String(n)] = res.synopsis;
    status.textContent = `✓ Эп. ${n} перегенерирован`; status.className = 'pipeline-status ok';
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
  } finally {
    btn.disabled = false; btn.textContent = '↻';
  }
}

async function plConfirmMilestones() {
  for (const n of milestoneIndices(S.series)) {
    const ta = document.getElementById(`ms-ta-${n}`);
    if (ta && ta.value.trim()) await plSaveMilestone(n);
  }
  const status = document.getElementById('pl-stage2-status');
  try {
    S.series = await api.post(`/api/series/${S.seriesId}/confirm-milestones`, {});
    // Characters/locations are NO LONGER auto-extracted from synopsis here —
    // they'd often include people/places that never make it into the actual
    // script. The episode view's "Извлечь персонажей и локации" button reads
    // from the script instead, which is the single source of truth.
    renderCharactersList();
    renderLocationsList();
    renderItemsList();
    renderPipeline();
    S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
    renderEpisodesList();
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
  }
}

// ── Stage 2: Episode synopses 1-10 (or first N chunks in batch mode) ────────
function renderStage2(el) {
  const s = S.series;
  const slots = stage2ChunkRange(s);  // e.g. [1,2] in batch_size=5, or 1..10 in single mode
  const lastSlot = slots[slots.length - 1];
  const eps = S.episodes.filter(e => e.number <= lastSlot).sort((a,b) => a.number - b.number);
  const allHaveSynopsis = slots.every(n => eps.find(e => e.number === n)?.synopsis);
  const batch = isBatchMode(s);
  const subEpRange = batch ? `1–${batchSize(s) * lastSlot}` : '1–10';
  const stageTitle = batch
    ? `Синопсисы чанков 1–${lastSlot} (серии ${subEpRange})`
    : 'Синопсисы эпизодов 1–10';
  const genBtnLabel = batch
    ? `⚡ Сгенерировать синопсисы чанков 1–${lastSlot}`
    : '⚡ Сгенерировать синопсисы 1–10';

  el.innerHTML = `
    <div class="pipeline-stage">
      <div class="pipeline-stage-header">
        <div class="pipeline-stage-num">2</div>
        <div class="pipeline-stage-title">${stageTitle}</div>
        ${allHaveSynopsis ? '<span style="color:var(--success);font-size:0.82rem">✓ Готово — можно писать сценарии</span>' : ''}
      </div>
      <div class="pipeline-stage-body">
        <div class="pipeline-row">
          <button class="btn-idea-gen" id="pl-gen-ep-syn-btn" onclick="plGenerateEpSynopses()">${genBtnLabel}</button>
          <span style="font-size:0.8rem;color:var(--muted)">Уже написанные не будут перезаписаны</span>
        </div>
        <div class="ep-synopsis-rows">
          ${slots.map(n => {
            const ep = eps.find(e => e.number === n);
            const syn = ep?.synopsis || '';
            const badge = batch ? chunkLabel(s, n, {short:true}) : `Эп. ${n}`;
            return `<div class="ep-synopsis-row">
              <div class="ep-synopsis-badge">${badge}</div>
              <div class="ep-synopsis-text ${syn ? '' : 'empty'}">${syn ? esc(syn) : 'Нет синопсиса'}</div>
            </div>`;
          }).join('')}
        </div>
        <div id="pl-stage2-status" class="pipeline-status"></div>
      </div>
    </div>
  `;
}

async function plExtractFromStory() {
  const btn = document.getElementById('pl-extract-btn');
  const status = document.getElementById('pl-stage2-status');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Извлекаем...';
  status.textContent = ''; status.className = 'pipeline-status';
  try {
    const res = await api.post(`/api/series/${S.seriesId}/extract-from-story`, {});
    S.series = res.series;
    renderCharactersList();
    renderLocationsList();
    renderItemsList();
    const chars = res.added_characters.join(', ');
    const locs  = res.added_locations.join(', ');
    status.textContent = `✓ Добавлено: персонажи [${chars || 'нет новых'}], локации [${locs || 'нет новых'}]`;
    status.className = 'pipeline-status ok';
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
  } finally {
    btn.disabled = false; btn.innerHTML = '🤖 Извлечь персонажей и локации из сюжета';
  }
}

async function plGenerateEpSynopses() {
  const btn = document.getElementById('pl-gen-ep-syn-btn');
  const status = document.getElementById('pl-stage2-status');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = ''; status.className = 'pipeline-status';
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title };
    await trackTask('Синопсисы серий 1–10', ctx, () =>
      api.post(`/api/series/${S.seriesId}/generate-episode-synopses`, {})
    );
    S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
    renderStage2(document.getElementById('pipeline-section'));
    renderEpisodesList();
    status.textContent = isBatchMode(S.series) ? '✓ Синопсисы чанков готовы. Открывай чанк и генерируй сценарий!' : '✓ Синопсисы готовы. Открывай эпизод и генерируй сценарий!';
    status.className = 'pipeline-status ok';
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
    const slots = stage2ChunkRange(S.series);
    btn.disabled = false; btn.innerHTML = isBatchMode(S.series) ? `⚡ Сгенерировать синопсисы чанков 1–${slots[slots.length-1]}` : '⚡ Сгенерировать синопсисы 1–10';
  }
}

// ── Series view ───────────────────────────────────────────────────────────────
async function loadSeriesView() {
  S.series = await api.get(`/api/series/${S.seriesId}`);
  S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
  renderSeriesView();
  setBreadcrumb([
    { label: 'Сериалы', action: "navigate('projects')" },
    { label: S.series.title },
  ]);
  // Resume the import-progress banner if a script-import job is still active
  // (e.g. user closed the tab and reopened mid-import).
  try {
    const st = await fetch(`/api/series/${S.seriesId}/import-status`).then(r => r.json());
    if (st && (st.running || (st.done > 0 && st.total > 0 && st.finished_at &&
        (Date.now() - new Date(st.finished_at).getTime()) < 60000))) {
      pollImportStatus(S.seriesId);
    }
  } catch {}
}

function applyVideoProviderMode() {
  const provider = (S.series && S.series.video_provider) || 'reteller';
  document.body.classList.toggle('seedance-mode', provider === 'seedance');
  const sel = document.getElementById('video-provider-select');
  if (sel) sel.value = provider;
  // Sync image-provider dropdown to persisted choice. '' = auto fallback.
  const imgSel = document.getElementById('image-provider-select');
  if (imgSel) {
    const pref = (S.series && S.series.preferred_image_provider) || '';
    imgSel.value = pref || 'auto';
  }
}

// Persist per-series image-provider preference (banana / seedream / auto).
// Backend reads this via _series_image_provider() and reorders the provider
// chain in avai_generate so user's pick goes first.
async function setImageProvider(provider) {
  if (!S.series) return;
  S.series.preferred_image_provider = provider === 'auto' ? '' : provider;
  try {
    await api.put(`/api/series/${S.seriesId}`, { preferred_image_provider: S.series.preferred_image_provider });
    showToast(`✓ Image provider: ${provider}`);
  } catch (e) {
    console.error('setImageProvider failed', e);
  }
}

async function setVideoProvider(provider) {
  if (!S.series) return;
  // Switching to Reteller requires a Reteller API key. Block + prompt if missing
  // (primary user is grandfathered to global env, others must enter their own).
  if (provider === 'reteller') {
    try {
      const me = await fetch('/api/me').then(r => r.ok ? r.json() : null);
      if (me && me.authenticated && !me.has_reteller_key) {
        // Roll back the dropdown UI so it reflects reality
        const sel = document.getElementById('video-provider-select');
        if (sel) sel.value = S.series.video_provider || 'seedance';
        _showApiKeySetupModal('reteller');
        return;
      }
    } catch {}
  }
  S.series.video_provider = provider;
  applyVideoProviderMode();
  try {
    await api.put(`/api/series/${S.seriesId}`, { video_provider: provider });
  } catch (e) {
    console.error('setVideoProvider failed', e);
  }
}

function renderSeriesView() {
  const s = S.series;
  document.getElementById('series-title-display').textContent = s.title;
  applyVideoProviderMode();

  // Bible
  const autogenOn = !!s.auto_generate_assets;
  document.getElementById('bible-content').innerHTML = `
    ${s.genre ? `<div><strong>Жанр:</strong> ${esc(s.genre)}</div>` : ''}
    ${s.tone ? `<div><strong>Тон:</strong> ${esc(s.tone)}</div>` : ''}
    ${s.target_audience ? `<div><strong>Аудитория:</strong> ${esc(s.target_audience)}</div>` : ''}
    ${s.world_description ? `<div style="margin-top:6px">${esc(s.world_description)}</div>` : ''}
    <label style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:8px 10px;background:rgba(132,94,247,0.06);border:1px solid rgba(132,94,247,0.2);border-radius:6px;cursor:pointer;font-size:0.85rem">
      <input type="checkbox" id="series-autogen-toggle" ${autogenOn ? 'checked' : ''} onchange="toggleSeriesAutogen()" style="width:16px;height:16px">
      <span><strong>⚡ Авто-генерация картинок</strong> — новые персонажи, костюмы и локации генерятся сами как появляются</span>
    </label>
    <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
      <button class="btn btn-sm btn-ghost" onclick="triggerAutogenSweep()" id="autogen-sweep-btn" style="font-size:0.78rem">🎨 Сгенерировать недостающее</button>
      <span id="autogen-sweep-status" style="font-size:0.78rem;color:var(--muted)"></span>
    </div>
    <div id="canon-summary" style="margin-top:10px;padding:8px 10px;background:rgba(80,180,140,0.06);border:1px solid rgba(80,180,140,0.2);border-radius:6px;font-size:0.83rem;color:var(--muted);cursor:pointer" onclick="openCanonViewer()">
      <strong style="color:var(--text)">📚 Канон сериала</strong> <span style="opacity:0.7">— автоматическая проверка логики</span>
      <div id="canon-summary-stats" style="margin-top:4px;font-size:0.78rem">загрузка…</div>
    </div>
  `;
  loadCanonSummary();
  checkAutogenOnLoad();

  // Pipeline
  renderPipeline();

  // Characters
  renderCharactersList();

  // Locations
  renderLocationsList();

  // Items
  renderItemsList();

  // Style
  renderStyleSection();

  // Episodes
  renderEpisodesList();
}

function renderCharactersList() {
  const s = S.series;
  const el = document.getElementById('characters-list');
  if (!s.characters.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.83rem">Нет персонажей</div>';
    return;
  }
  el.innerHTML = s.characters.map(c => {
    const hasRefs = c.ref_images && c.ref_images.length > 0;
    const imgUrl = hasRefs ? `/assets/${s.id}/${c.ref_images[0]}` : null;
    return `
      <div class="char-item" data-autogen-kind="char" data-autogen-id="${c.id}" onclick="openCharAssets('${c.id}')">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(c.name)}" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : esc(c.name[0])}
          <div class="autogen-overlay" hidden><span class="spinner"></span></div>
        </div>
        <div class="char-info">
          <div class="char-name">${esc(c.name)}</div>
          <div class="char-role">${esc(c.gender === 'male' ? 'М' : 'Ж')} · ${esc(c.description?.slice(0,30) || '—')}</div>
        </div>
        <div class="char-ref-dot ${hasRefs ? 'has-refs' : 'no-refs'}" title="${hasRefs ? 'Есть фото' : 'Нет фото'}"></div>
        <button class="btn-icon" onclick="event.stopPropagation();openEditCharacter('${c.id}')" title="Редактировать">✎</button>
        <button class="btn-icon" onclick="event.stopPropagation();deleteCharacter('${c.id}')" title="Удалить" style="color:var(--danger)">✕</button>
      </div>
    `;
  }).join('');
}

function renderLocationsList() {
  const s = S.series;
  const el = document.getElementById('locations-list');
  const locs = s.locations || [];
  if (!locs.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.83rem">Нет локаций</div>';
    return;
  }
  el.innerHTML = locs.map(l => {
    const hasRefs = l.ref_images && l.ref_images.length > 0;
    const imgUrl = hasRefs ? `/assets/${s.id}/${l.ref_images[0]}` : null;
    return `
      <div class="char-item" data-autogen-kind="loc" data-autogen-id="${l.id}" onclick="openLocAssets('${l.id}')">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(l.name)}" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : '📍'}
          <div class="autogen-overlay" hidden><span class="spinner"></span></div>
        </div>
        <div class="char-info">
          <div class="char-name">${esc(l.name)}</div>
          <div class="char-role">${esc(l.description?.slice(0,30) || '—')}</div>
        </div>
        <div class="char-ref-dot ${hasRefs ? 'has-refs' : 'no-refs'}" title="${hasRefs ? 'Есть фото' : 'Нет фото'}"></div>
        <button class="btn-icon" onclick="event.stopPropagation();openEditLocation('${l.id}')" title="Редактировать">✎</button>
        <button class="btn-icon" onclick="event.stopPropagation();deleteLocation('${l.id}')" title="Удалить" style="color:var(--danger)">✕</button>
      </div>
    `;
  }).join('');
}

function renderStyleSection() {
  const s = S.series;
  const refs = (s.style.ref_images || []);
  document.getElementById('style-section').innerHTML = `
    <div style="font-size:0.85rem;color:var(--muted);margin-bottom:6px">
      <strong style="color:var(--text)">${esc(styleLabel(s.style.type))}</strong>
      ${s.style.custom_description ? `<br><span>${esc(s.style.custom_description.slice(0,60))}</span>` : ''}
    </div>
    <div class="style-refs-row">
      ${refs.map(r => `<img class="style-thumb" src="/assets/${s.id}/${r}" alt="style ref">`).join('')}
    </div>
  `;
}

function styleLabel(t) {
  const map = {cinematic:'Кинематограф',photorealistic:'Фотореализм',anime:'Аниме',auto:'Авто',custom:'Свой'};
  return map[t] || t;
}

function renderEpisodesList() {
  const el = document.getElementById('episodes-list');
  const empty = document.getElementById('episodes-empty');
  if (!S.episodes.length) {
    el.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  el.innerHTML = S.episodes.map(ep => {
    const rtlStatus = ep.reteller?.status;
    const hasVideo = ep.reteller?.video_url;
    const ar = ep.audit_report;
    let auditBadge = '';
    if (ar) {
      const critCount = (ar.violations || []).filter(v => v.severity === 'critical').length;
      if (ar.passes && critCount === 0) {
        auditBadge = `<span class="status-badge" style="background:rgba(74,222,128,0.15);color:#4ade80" title="Логика проверена · попыток: ${ar.retries}">✓ logic</span>`;
      } else {
        auditBadge = `<span class="status-badge" style="background:rgba(248,113,113,0.15);color:#f87171" title="Критических проблем: ${critCount}">⚠ ${critCount}</span>`;
      }
    }
    const dsp = ep.days_since_previous;
    const dspBadge = (dsp !== undefined && dsp !== null && ep.number > 1)
      ? `<span class="status-badge" style="background:rgba(132,94,247,0.12);color:#a78bfa" title="Дней с прошлой серии">+${dsp}д</span>` : '';
    return `
      <div class="episode-row" onclick="navigate('episode',{seriesId:'${S.seriesId}',episodeNum:${ep.number}})">
        <div class="ep-num" title="${esc(chunkLabel(S.series, ep.number))}">${isBatchMode(S.series) ? chunkRange(S.series, ep.number).join('–') : ep.number}</div>
        <div class="ep-info">
          <div class="ep-title">${esc(ep.title)}</div>
          <div class="ep-synopsis">${esc(ep.synopsis || (ep.script ? ep.script.slice(0,80) : '—'))}</div>
        </div>
        <div class="ep-badges">
          <span class="status-badge status-${ep.status}">${statusLabel(ep.status)}</span>
          ${dspBadge}
          ${auditBadge}
          ${rtlStatus ? `<span class="status-badge status-${rtlStatus}">${statusLabel(rtlStatus)}</span>` : ''}
          ${hasVideo ? `<a href="${ep.reteller.video_url}" target="_blank" class="btn-ghost btn-sm" onclick="event.stopPropagation()">▶ Видео</a>` : ''}
        </div>
        <div class="ep-actions">
          <button class="btn-icon" onclick="event.stopPropagation();deleteEpisodeConfirm(${ep.number})" title="Удалить эпизод" style="color:var(--danger)">✕</button>
        </div>
      </div>
    `;
  }).join('');
}

function statusLabel(s) {
  const m = {draft:'Черновик',sent:'Отправлено',processing:'Генерация...',completed:'Готово',error:'Ошибка',ready:'Готов'};
  return m[s] || s;
}

// ── Series Canon (story bible / logic state) ─────────────────────────────────
async function loadCanonSummary() {
  const el = document.getElementById('canon-summary-stats');
  if (!el) return;
  try {
    const r = await fetch(`/api/series/${S.seriesId}/canon`);
    if (!r.ok) { el.textContent = 'нет данных'; return; }
    const c = await r.json();
    const wc = c.world_clock || {};
    const lastAudit = (c.audit_log || []).slice(-1)[0];
    const auditTxt = lastAudit
      ? (lastAudit.passes ? '✓' : `⚠ ${lastAudit.violations.length}`) + ` (ep${lastAudit.ep}, retries=${lastAudit.retries})`
      : '—';
    el.innerHTML =
      `день ${wc.current_day || 0} · эп ${wc.last_episode || 0} · ` +
      `фактов ${c.facts?.length || 0} · ` +
      `тредов ${(c.open_threads || []).filter(t => t.status !== 'closed').length} открыто · ` +
      `аудит: ${auditTxt}`;
  } catch (e) {
    el.textContent = 'ошибка загрузки';
  }
}

async function openCanonViewer() {
  let modal = document.getElementById('modal-canon');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'modal-canon';
    modal.className = 'modal';
    modal.innerHTML = `
      <div class="modal-content" style="max-width:760px;max-height:85vh;overflow:auto">
        <div class="modal-header">
          <h2>📚 Канон сериала</h2>
          <button class="btn-icon" onclick="closeModal('modal-canon')">✕</button>
        </div>
        <div id="canon-body" style="padding:14px"></div>
        <div class="modal-footer" style="padding:10px 14px;border-top:1px solid var(--border)">
          <button class="btn btn-ghost" onclick="rebuildCanon()">⟲ Пересобрать из всех серий</button>
          <button class="btn" onclick="closeModal('modal-canon')">Закрыть</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
  }
  openModal('modal-canon');
  const body = document.getElementById('canon-body');
  body.innerHTML = 'загрузка…';
  try {
    const c = await (await fetch(`/api/series/${S.seriesId}/canon`)).json();
    body.innerHTML = renderCanonBody(c);
  } catch (e) {
    body.innerHTML = 'ошибка: ' + e.message;
  }
}

function renderCanonBody(c) {
  const wc = c.world_clock || {};
  const tl = (c.timeline || []).map(t =>
    `<li><strong>Ep ${t.ep}</strong> (день ${t.day}): ${(t.events || []).map(esc).join('; ') || '—'}</li>`
  ).join('');
  const facts = (c.facts || []).map(f =>
    `<li><code>${f.id}</code> [ep ${f.ep}] ${esc(f.fact)}${f.supersedes ? ` <span style="color:var(--muted)">(сменяет ${f.supersedes})</span>` : ''}</li>`
  ).join('');
  const threads = (c.open_threads || []).map(t =>
    `<li><code>${t.id}</code> ep${t.opened_ep} — ${esc(t.question)} <span style="color:${t.status==='closed' ? 'var(--success,#4ade80)' : 'var(--warning,#fbbf24)'}">[${t.status}${t.resolved_ep ? ` ep${t.resolved_ep}` : ''}]</span></li>`
  ).join('');
  const cs = Object.entries(c.character_state || {}).map(([name, st]) => {
    const knows = (st.knows || []).slice(-6).map(esc).join(' · ') || '—';
    const phys = Object.entries(st.physical || {}).map(([k,v]) => `${k}=${v}`).join(', ') || '—';
    return `<li><strong>${esc(name)}</strong> — знает: ${knows}<br><span style="color:var(--muted)">состояние: ${esc(phys)} · локация: ${esc(st.location || '—')}</span></li>`;
  }).join('');
  const audit = (c.audit_log || []).slice(-10).reverse().map(a => {
    const badge = a.passes ? '<span style="color:var(--success,#4ade80)">✓</span>' : `<span style="color:var(--danger,#f87171)">⚠ ${a.violations.length}</span>`;
    const vs = a.violations.map(v => `<div style="margin-left:14px;font-size:0.78rem;color:var(--muted)">[${v.severity}] ${esc(v.type || '')}: ${esc(v.explanation || '')}</div>`).join('');
    return `<li>Ep ${a.ep} ${badge} retries=${a.retries}${vs}</li>`;
  }).join('');
  return `
    <div style="margin-bottom:12px"><strong>Мировые часы:</strong> день ${wc.current_day || 0} · последняя серия: ep ${wc.last_episode || 0}</div>
    <h4>📅 Таймлайн</h4><ul style="font-size:0.85rem">${tl || '<li style="color:var(--muted)">пусто</li>'}</ul>
    <h4>🔒 Канонические факты</h4><ul style="font-size:0.85rem">${facts || '<li style="color:var(--muted)">пусто</li>'}</ul>
    <h4>👥 Состояние персонажей</h4><ul style="font-size:0.85rem">${cs || '<li style="color:var(--muted)">пусто</li>'}</ul>
    <h4>🧵 Открытые/закрытые треды</h4><ul style="font-size:0.85rem">${threads || '<li style="color:var(--muted)">пусто</li>'}</ul>
    <h4>🔍 Журнал аудита (последние 10)</h4><ul style="font-size:0.85rem">${audit || '<li style="color:var(--muted)">пусто</li>'}</ul>
  `;
}

async function rebuildCanon() {
  if (!confirm('Перестроить канон из всех существующих сценариев? Текущий канон будет перезаписан.')) return;
  const body = document.getElementById('canon-body');
  body.innerHTML = 'пересобираю канон…';
  try {
    const r = await fetch(`/api/series/${S.seriesId}/canon/rebuild`, {method: 'POST'});
    const data = await r.json();
    if (data.error) { body.innerHTML = 'ошибка: ' + data.error; return; }
    body.innerHTML = renderCanonBody(data.canon);
    loadCanonSummary();
  } catch (e) {
    body.innerHTML = 'ошибка: ' + e.message;
  }
}

// ── Bible editor ──────────────────────────────────────────────────────────────
function openBibleEditor() {
  const s = S.series;
  setVal('bible-title', s.title);
  setVal('bible-genre', s.genre);
  setVal('bible-tone', s.tone);
  setVal('bible-audience', s.target_audience);
  setVal('bible-world', s.world_description);
  setVal('bible-visual-style', s.visual_style || '');
  openModal('modal-bible');
}

async function saveBible() {
  const data = {
    title: val('bible-title'), genre: val('bible-genre'),
    tone: val('bible-tone'), target_audience: val('bible-audience'),
    world_description: val('bible-world'),
    visual_style: val('bible-visual-style'),
  };
  S.series = await api.put(`/api/series/${S.seriesId}`, data);
  closeModal('modal-bible');
  renderSeriesView();
}

// ── Characters ────────────────────────────────────────────────────────────────
// ── Quick-add character (from episode sidebar) ────────────────────────────
const QC = { file: null, generatedRel: null };

function openQuickAddChar() {
  if (!S.seriesId) return;
  QC.file = null;
  QC.generatedRel = null;
  setVal('qc-name', '');
  setVal('qc-gender', 'male');
  setVal('qc-description', '');
  document.getElementById('qc-gen-block')?.classList.add('hidden');
  document.getElementById('qc-gen-status').textContent = '';
  const prev = document.getElementById('qc-preview');
  if (prev) { prev.src = ''; prev.classList.add('hidden'); }
  const empty = document.querySelector('#qc-drop .qc-drop-empty');
  if (empty) empty.classList.remove('hidden');
  const f = document.getElementById('qc-file');
  if (f) f.value = '';
  openModal('modal-quickchar');
}

function qcHandleDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('hover');
  const f = e.dataTransfer.files?.[0];
  if (f) qcHandleFile(f);
}

function qcHandleFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    showToast('Можно перетащить только картинку');
    return;
  }
  QC.file = file;
  QC.generatedRel = null;
  const prev = document.getElementById('qc-preview');
  const empty = document.querySelector('#qc-drop .qc-drop-empty');
  const reader = new FileReader();
  reader.onload = (e) => {
    prev.src = e.target.result;
    prev.classList.remove('hidden');
    empty?.classList.add('hidden');
  };
  reader.readAsDataURL(file);
}

function qcToggleGen() {
  document.getElementById('qc-gen-block')?.classList.toggle('hidden');
}

async function qcGenerate() {
  const name = (val('qc-name') || '').trim();
  const description = (val('qc-description') || '').trim();
  if (!name) return alert('Сначала введи имя');
  if (!description) return alert('Опиши персонажа');
  const btn = document.getElementById('qc-gen-btn');
  const st  = document.getElementById('qc-gen-status');
  btn.disabled = true; btn.innerHTML = '⏳ генерирую...';
  st.textContent = '';
  try {
    // Create char first (so /generate-image can attach output to it)
    const created = await api.post(`/api/series/${S.seriesId}/characters`, {
      name, description, appearance: description, gender: val('qc-gender') || 'male',
    });
    const charId = created.character?.id || created.id;
    if (!charId) throw new Error('cannot create char');
    QC._tempCharId = charId;
    const r = await api.post(`/api/series/${S.seriesId}/characters/${charId}/generate-image`, {});
    bumpAssetVersion();
    if (!r.ready) throw new Error(r.error || 'no image');
    QC.generatedRel = r.url || '';
    QC.file = null;
    const prev = document.getElementById('qc-preview');
    const empty = document.querySelector('#qc-drop .qc-drop-empty');
    if (prev) { prev.src = QC.generatedRel; prev.classList.remove('hidden'); }
    if (empty) empty.classList.add('hidden');
    st.textContent = '✓ готово · нажми Сохранить';
    showToast('🪄 Фото сгенерировано');
  } catch (e) {
    st.textContent = '✗ ' + (e.message || e);
  } finally {
    btn.disabled = false; btn.innerHTML = '🪄 Сгенерировать фото';
  }
}

async function qcSave() {
  const name = (val('qc-name') || '').trim();
  if (!name) return alert('Введи имя персонажа');
  const btn = document.getElementById('qc-save-btn');
  const old = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '⏳';
  try {
    let charId = QC._tempCharId;  // set if user already generated photo
    if (!charId) {
      // Create char now
      const created = await api.post(`/api/series/${S.seriesId}/characters`, {
        name,
        description: (val('qc-description') || '').trim(),
        appearance:  (val('qc-description') || '').trim(),
        gender: val('qc-gender') || 'male',
      });
      charId = created.character?.id || created.id;
      if (!charId) throw new Error('cannot create char');
    }
    // Upload dropped file if any
    if (QC.file) {
      const fd = new FormData();
      fd.append('photo', QC.file);
      const resp = await fetch(`/api/series/${S.seriesId}/characters/${charId}/upload-photo`, {
        method: 'POST', body: fd,
      });
      bumpAssetVersion();
      if (!resp.ok) throw new Error(await resp.text());
    }
    // Refresh series view
    S.series = await api.get(`/api/series/${S.seriesId}`);
    closeModal('modal-quickchar');
    QC._tempCharId = null;
    if (typeof renderCharactersList === 'function') renderCharactersList();
    if (typeof renderEpisodeView === 'function') renderEpisodeView();
    if (S.episodeNum) { try { await loadEpisodeView(); } catch {} }
    showToast('✓ Персонаж добавлен');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    btn.disabled = false; btn.innerHTML = old;
  }
}

// ── Quick-add location (from episode sidebar) ────────────────────────────
const QL = { file: null, generatedRel: null };

function openQuickAddLoc() {
  if (!S.seriesId) return;
  QL.file = null;
  QL.generatedRel = null;
  QL._tempLocId = null;
  setVal('ql-name', '');
  setVal('ql-description', '');
  document.getElementById('ql-gen-block')?.classList.add('hidden');
  document.getElementById('ql-gen-status').textContent = '';
  const prev = document.getElementById('ql-preview');
  if (prev) { prev.src = ''; prev.classList.add('hidden'); }
  const empty = document.querySelector('#ql-drop .qc-drop-empty');
  if (empty) empty.classList.remove('hidden');
  const f = document.getElementById('ql-file');
  if (f) f.value = '';
  openModal('modal-quickloc');
}

function qlHandleDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('hover');
  const f = e.dataTransfer.files?.[0];
  if (f) qlHandleFile(f);
}

function qlHandleFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    showToast('Можно перетащить только картинку');
    return;
  }
  QL.file = file;
  QL.generatedRel = null;
  const prev = document.getElementById('ql-preview');
  const empty = document.querySelector('#ql-drop .qc-drop-empty');
  const reader = new FileReader();
  reader.onload = (e) => {
    prev.src = e.target.result;
    prev.classList.remove('hidden');
    empty?.classList.add('hidden');
  };
  reader.readAsDataURL(file);
}

function qlToggleGen() {
  document.getElementById('ql-gen-block')?.classList.toggle('hidden');
}

async function qlGenerate() {
  const name = (val('ql-name') || '').trim();
  const description = (val('ql-description') || '').trim();
  if (!name) return alert('Сначала введи название');
  if (!description) return alert('Опиши локацию');
  const btn = document.getElementById('ql-gen-btn');
  const st  = document.getElementById('ql-gen-status');
  btn.disabled = true; btn.innerHTML = '⏳ генерирую...';
  st.textContent = '';
  try {
    const created = await api.post(`/api/series/${S.seriesId}/locations`, {
      name, description,
    });
    const locId = created.location?.id || created.id;
    if (!locId) throw new Error('cannot create location');
    QL._tempLocId = locId;
    const r = await api.post(`/api/series/${S.seriesId}/locations/${locId}/generate-image`, {});
    bumpAssetVersion();
    if (!r.ready) throw new Error(r.error || 'no image');
    QL.generatedRel = r.url || '';
    QL.file = null;
    const prev = document.getElementById('ql-preview');
    const empty = document.querySelector('#ql-drop .qc-drop-empty');
    if (prev) { prev.src = QL.generatedRel; prev.classList.remove('hidden'); }
    if (empty) empty.classList.add('hidden');
    st.textContent = '✓ готово · нажми Сохранить';
    showToast('🪄 Фото сгенерировано');
  } catch (e) {
    st.textContent = '✗ ' + (e.message || e);
  } finally {
    btn.disabled = false; btn.innerHTML = '🪄 Сгенерировать фото';
  }
}

async function qlSave() {
  const name = (val('ql-name') || '').trim();
  if (!name) return alert('Введи название локации');
  const btn = document.getElementById('ql-save-btn');
  const old = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '⏳';
  try {
    let locId = QL._tempLocId;
    if (!locId) {
      const created = await api.post(`/api/series/${S.seriesId}/locations`, {
        name, description: (val('ql-description') || '').trim(),
      });
      locId = created.location?.id || created.id;
      if (!locId) throw new Error('cannot create location');
    }
    if (QL.file) {
      const fd = new FormData();
      fd.append('photo', QL.file);
      const resp = await fetch(`/api/series/${S.seriesId}/locations/${locId}/upload-photo`, {
        method: 'POST', body: fd,
      });
      bumpAssetVersion();
      if (!resp.ok) throw new Error(await resp.text());
    }
    S.series = await api.get(`/api/series/${S.seriesId}`);
    closeModal('modal-quickloc');
    QL._tempLocId = null;
    if (typeof renderLocationsList === 'function') renderLocationsList();
    if (S.episodeNum) { try { await loadEpisodeView(); } catch {} }
    showToast('✓ Локация добавлена');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    btn.disabled = false; btn.innerHTML = old;
  }
}

function openAddCharacter() {
  S.editingCharId = null;
  document.getElementById('char-modal-title').textContent = 'Новый персонаж';
  clearFields(['char-name','char-description','char-appearance','char-voice-id']);
  setVal('char-gender', 'female');
  openModal('modal-character');
}

function openEditCharacter(charId) {
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  S.editingCharId = charId;
  document.getElementById('char-modal-title').textContent = 'Редактировать персонажа';
  setVal('char-name', c.name);
  setVal('char-description', c.description);
  setVal('char-appearance', c.appearance);
  setVal('char-gender', c.gender);
  setVal('char-voice-id', c.voice_id);
  openModal('modal-character');
}

async function saveCharacter() {
  const data = {
    name: val('char-name'), description: val('char-description'),
    appearance: val('char-appearance'), gender: val('char-gender'),
    voice_id: val('char-voice-id'),
  };
  if (!data.name) return alert('Введи имя персонажа');

  if (S.editingCharId) {
    await api.put(`/api/series/${S.seriesId}/characters/${S.editingCharId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/characters`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-character');
  renderCharactersList();
  renderEpisodesList();
}

async function deleteCharacter(charId) {
  if (!confirm('Удалить персонажа?')) return;
  await api.del(`/api/series/${S.seriesId}/characters/${charId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
  renderCharactersList();
}

// ── Locations ─────────────────────────────────────────────────────────────────
function openAddLocation() {
  S.editingLocId = null;
  document.getElementById('loc-modal-title').textContent = 'Новая локация';
  clearFields(['loc-name','loc-description']);
  openModal('modal-location');
}

function openEditLocation(locId) {
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  S.editingLocId = locId;
  document.getElementById('loc-modal-title').textContent = 'Редактировать локацию';
  setVal('loc-name', l.name);
  setVal('loc-description', l.description);
  openModal('modal-location');
}

async function saveLocation() {
  const data = { name: val('loc-name'), description: val('loc-description') };
  if (!data.name) return alert('Введи название локации');
  if (S.editingLocId) {
    await api.put(`/api/series/${S.seriesId}/locations/${S.editingLocId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/locations`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-location');
  renderLocationsList();
}

async function deleteLocation(locId) {
  if (!confirm('Удалить локацию?')) return;
  await api.del(`/api/series/${S.seriesId}/locations/${locId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  renderLocationsList();
}

let currentLocId = null;

function openLocAssets(locId) {
  currentLocId = locId;
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  document.getElementById('loc-assets-title').textContent = `Фото: ${l.name}`;
  renderLocAssetsGrid(l);
  setVal('regen-loc-wishes', l.image_constraints || '');
  const inp = document.getElementById('loc-asset-file-input');
  inp.onchange = () => uploadLocationRefs(locId, inp.files);
  openModal('modal-loc-assets');
}

async function regenerateLocation() {
  if (!currentLocId) return;
  const wishes = (val('regen-loc-wishes') || '').trim();
  const btn = document.getElementById('regen-loc-btn');
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ генерирую...'; }
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/locations/${currentLocId}/regenerate`,
      { wishes }
    );
    if (!r.ready) throw new Error(r.error || 'unknown');
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const l = (S.series.locations || []).find(x => x.id === currentLocId);
    if (l) renderLocAssetsGrid(l);
    if (typeof renderLocationsList === 'function') renderLocationsList();
    showToast('✓ Локация перегенерирована');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old; }
  }
}

function renderLocAssetsGrid(loc) {
  const grid = document.getElementById('loc-assets-grid');
  const refs = loc.ref_images || [];
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `${assetUrl(r)}`;
    return `
      <div class="photo-thumb-wrap" onclick="openLocLightbox('${loc.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteLocRef('${loc.id}','${fname}')">✕</button>
      </div>`;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
}

// Location-photo lightbox: image + regenerate panel (mirrors openCharLightbox).
function openLocLightbox(locId, url) {
  closeLightbox();
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  currentLocId = locId;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать локацию</h3>
        <div class="hint">
          Старое фото удалится, новое сгенерируется с учётом пожеланий.
          Пожелания сохранятся в локации и будут применяться при всех будущих генерациях.
        </div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-loc-wishes" rows="5"
            placeholder="Например:&#10;«больше окон»&#10;«утренний свет»&#10;«без людей в кадре»&#10;«теплая палитра»">${esc(l.image_constraints || '')}</textarea>
        </div>
        <div id="lb-regen-loc-status" class="lb-status"></div>
        <button id="lb-regen-loc-btn" class="btn-regen" onclick="regenerateLocationFromLightbox()">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

async function regenerateLocationFromLightbox() {
  if (!currentLocId) return;
  const wishes = (document.getElementById('lb-regen-loc-wishes').value || '').trim();
  const status = document.getElementById('lb-regen-loc-status');
  const btn = document.getElementById('lb-regen-loc-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.className = 'lb-status';
  status.textContent = 'Перегенерируем фото локации (~15-30 сек)...';
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/locations/${currentLocId}/regenerate`,
      { wishes }
    );
    if (!res.ready) throw new Error(res.error || 'unknown');
    status.className = 'lb-status ok';
    status.textContent = '✓ Локация обновлена';
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const l = (S.series.locations || []).find(x => x.id === currentLocId);
    if (l) renderLocAssetsGrid(l);
    renderLocationsList();
    setTimeout(() => closeLightbox(), 800);
  } catch (e) {
    status.className = 'lb-status err';
    status.textContent = 'Ошибка: ' + (e.message || e);
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

async function uploadLocationRefs(locId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/location/${locId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const l = (S.series.locations || []).find(x => x.id === locId);
  renderLocAssetsGrid(l);
  renderLocationsList();
}

async function deleteLocRef(locId, filename) {
  await api.del(`/api/series/${S.seriesId}/assets/location/${locId}/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const l = (S.series.locations || []).find(x => x.id === locId);
  renderLocAssetsGrid(l);
  renderLocationsList();
}

// ── Items (story-relevant props: handbag, gun, locket, ...) ───────────────────
let currentItemId = null;

function renderItemsList() {
  const s = S.series;
  const el = document.getElementById('items-list');
  if (!el) return;
  const items = s.items || [];
  if (!items.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.83rem">Нет предметов</div>';
    return;
  }
  el.innerHTML = items.map(it => {
    const hasRefs = it.ref_images && it.ref_images.length > 0;
    const imgUrl = hasRefs ? `/assets/${s.id}/${it.ref_images[0]}` : null;
    return `
      <div class="char-item" data-autogen-kind="item" data-autogen-id="${it.id}" onclick="openItemAssets('${it.id}')">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(it.name)}" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : '🎒'}
          <div class="autogen-overlay" hidden><span class="spinner"></span></div>
        </div>
        <div class="char-info">
          <div class="char-name">${esc(it.name)}</div>
          <div class="char-role">${esc(it.description?.slice(0,30) || '—')}</div>
        </div>
        <div class="char-ref-dot ${hasRefs ? 'has-refs' : 'no-refs'}" title="${hasRefs ? 'Есть фото' : 'Нет фото'}"></div>
        <button class="btn-icon" onclick="event.stopPropagation();openEditItem('${it.id}')" title="Редактировать">✎</button>
        <button class="btn-icon" onclick="event.stopPropagation();deleteItem('${it.id}')" title="Удалить" style="color:var(--danger)">✕</button>
      </div>
    `;
  }).join('');
}

function openAddItem() {
  S.editingItemId = null;
  document.getElementById('item-modal-title').textContent = 'Новый предмет';
  clearFields(['item-name','item-description']);
  openModal('modal-item');
}

// One-click cleanup of duplicate items in the current series. Hits the
// /dedupe-items endpoint which runs fuzzy + LLM-synonym pass over series.items
// and merges synonym groups (dictaphone/voice recorder, locket/pendant). All
// episode.items_used arrays are rewritten to point at canonical ids.
async function dedupeSeriesItems() {
  if (!S.seriesId) return;
  const items = S.series?.items || [];
  if (items.length < 2) { showToast('Меньше 2 предметов — нечего объединять'); return; }
  if (!confirm(`Найти и склеить дубликаты среди ${items.length} предметов? Claude проверит синонимы (диктофон/recorder и т.п.) и объединит. Действие необратимо, но не теряет данные — берётся самое полное описание.`)) return;
  showToast('🧹 Анализирую дубликаты…');
  try {
    const r = await api.post(`/api/series/${S.seriesId}/dedupe-items`, {});
    bumpAssetVersion();
    if (r.error) throw new Error(r.error);
    if (!r.merged) {
      showToast('✓ Дубликатов не найдено');
      return;
    }
    const groups = (r.groups || []).map(g =>
      `  • ${g.canonical.name} ← ${g.merged.map(m => m.name).join(', ')}`
    ).join('\n');
    alert(`✓ Склеено ${r.merged} дубликатов в ${r.groups?.length || 0} группах:\n\n${groups}`);
    // Refresh state and re-render lists.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    if (typeof renderItemsList === 'function') renderItemsList();
    if (typeof renderEpItems === 'function') renderEpItems();
  } catch (e) {
    alert('Ошибка дедупликации: ' + (e?.message || e));
  }
}

function openEditItem(itemId) {
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  S.editingItemId = itemId;
  document.getElementById('item-modal-title').textContent = 'Редактировать предмет';
  setVal('item-name', it.name);
  setVal('item-description', it.description);
  openModal('modal-item');
}

async function saveItem() {
  const data = { name: val('item-name'), description: val('item-description') };
  if (!data.name) return alert('Введи название предмета');
  if (S.editingItemId) {
    await api.put(`/api/series/${S.seriesId}/items/${S.editingItemId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/items`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-item');
  renderItemsList();
}

async function deleteItem(itemId) {
  if (!confirm('Удалить предмет?')) return;
  await api.del(`/api/series/${S.seriesId}/items/${itemId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  renderItemsList();
}

function openItemAssets(itemId) {
  currentItemId = itemId;
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  document.getElementById('item-assets-title').textContent = `Фото: ${it.name}`;
  renderItemAssetsGrid(it);
  setVal('regen-item-wishes', it.image_constraints || '');
  const inp = document.getElementById('item-asset-file-input');
  inp.onchange = () => uploadItemRefs(itemId, inp.files);
  openModal('modal-item-assets');
}

function renderItemAssetsGrid(item) {
  const grid = document.getElementById('item-assets-grid');
  const refs = item.ref_images || [];
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `${assetUrl(r)}`;
    return `
      <div class="photo-thumb-wrap" onclick="openItemLightbox('${item.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteItemRef('${item.id}','${fname}')">✕</button>
      </div>`;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
}

async function uploadItemRefs(itemId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/item/${itemId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const it = (S.series.items || []).find(x => x.id === itemId);
  renderItemAssetsGrid(it);
  renderItemsList();
}

async function deleteItemRef(itemId, filename) {
  await api.del(`/api/series/${S.seriesId}/assets/item/${itemId}/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const it = (S.series.items || []).find(x => x.id === itemId);
  renderItemAssetsGrid(it);
  renderItemsList();
}

async function regenerateItem() {
  if (!currentItemId) return;
  const wishes = (val('regen-item-wishes') || '').trim();
  const btn = document.getElementById('regen-item-btn');
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ генерирую...'; }
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/items/${currentItemId}/regenerate`,
      { wishes }
    );
    if (!r.ready) throw new Error(r.error || 'unknown');
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const it = (S.series.items || []).find(x => x.id === currentItemId);
    if (it) renderItemAssetsGrid(it);
    renderItemsList();
    showToast('✓ Предмет перегенерирован');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old; }
  }
}

// Item-photo lightbox: image + regenerate panel
function openItemLightbox(itemId, url) {
  closeLightbox();
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  currentItemId = itemId;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать предмет</h3>
        <div class="hint">
          Старое фото удалится, новое сгенерируется по описанию + пожеланиям.
          Пожелания сохранятся в предмете и будут применяться при всех будущих генерациях.
        </div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-item-wishes" rows="5"
            placeholder="Например:&#10;«потёртая кожа, не новая»&#10;«золотая фурнитура»&#10;«царапина на боку»&#10;«без бренда»">${esc(it.image_constraints || '')}</textarea>
        </div>
        <div id="lb-regen-item-status" class="lb-status"></div>
        <button id="lb-regen-item-btn" class="btn-regen" onclick="regenerateItemFromLightbox()">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

async function regenerateItemFromLightbox() {
  if (!currentItemId) return;
  const wishes = (document.getElementById('lb-regen-item-wishes').value || '').trim();
  const status = document.getElementById('lb-regen-item-status');
  const btn = document.getElementById('lb-regen-item-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.className = 'lb-status';
  status.textContent = 'Перегенерируем фото предмета (~15-30 сек)...';
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/items/${currentItemId}/regenerate`,
      { wishes }
    );
    if (!res.ready) throw new Error(res.error || 'unknown');
    status.className = 'lb-status ok';
    status.textContent = '✓ Предмет обновлён';
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const it = (S.series.items || []).find(x => x.id === currentItemId);
    if (it) renderItemAssetsGrid(it);
    renderItemsList();
    setTimeout(() => closeLightbox(), 800);
  } catch (e) {
    status.className = 'lb-status err';
    status.textContent = 'Ошибка: ' + (e.message || e);
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

// ── Character assets modal ────────────────────────────────────────────────────
let currentCharId = null;

async function openCharAssets(charId) {
  currentCharId = charId;
  // Refresh series state so any outfits added by recent script-gen / autogen
  // show up — without this, the modal renders stale `S.series.characters[i]`
  // and "no other outfits" looks like a bug even when they exist on disk.
  try {
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh && fresh.characters) S.series = fresh;
  } catch {}
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  document.getElementById('char-assets-title').textContent = `Фото: ${c.name}`;
  renderCharAssetsGrid(c);
  renderOutfitsList(c);
  renderCharCanonicalDesc(c);

  const inp = document.getElementById('char-asset-file-input');
  inp.onchange = () => uploadCharacterRefs(charId, inp.files);
  openModal('modal-char-assets');
}

// Build the canonical description shown in the card and copied into Seedance prompts.
// Mirrors backend _canonical_char_description() — appearance + base outfit description.
function _buildCanonicalCharDesc(char) {
  if (!char) return '';
  const appearance = (char.appearance || '').trim();
  const outfits = char.outfits || [];
  const base = outfits.find(o => o.is_base) || outfits[0];
  const outfitDesc = (base && (base.description || '').trim()) || '';
  return [appearance, outfitDesc].filter(Boolean).join('; ');
}

function renderCharCanonicalDesc(char) {
  const el = document.getElementById('char-canonical-desc');
  if (!el) return;
  const text = _buildCanonicalCharDesc(char);
  el.textContent = text || '(описание не задано — заполни appearance персонажа и/или базовый outfit)';
  el.dataset.text = text;
}

async function copyCanonicalDescription() {
  const el = document.getElementById('char-canonical-desc');
  const text = el?.dataset.text || el?.textContent || '';
  if (!text || text.startsWith('(')) {
    showToast('⚠ Описание пустое', 2500);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    showToast('✓ Скопировано', 1800);
  } catch {
    // Fallback for older browsers
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    ta.remove();
    showToast('✓ Скопировано', 1800);
  }
}

function renderCharAssetsGrid(char) {
  const grid = document.getElementById('char-assets-grid');
  const refs = char.ref_images || [];
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `${assetUrl(r)}`;
    return `
      <div class="photo-thumb-wrap" onclick="openCharLightbox('${char.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteCharRef('${char.id}','${fname}')">✕</button>
      </div>
    `;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
}

// Builds an inline DOM node that replaces a broken <img>. Shows a clearly-
// broken visual + click-to-debug. Used everywhere asset paths might point at
// a file that vanished (concurrent-write JSON corruption used to do this; the
// atomic-write fix should prevent it now, but the placeholder stays as a
// safety net + diagnostic tool).
function _brokenImagePlaceholder(url) {
  const div = document.createElement('div');
  div.className = 'broken-image-placeholder';
  div.title = 'Картинка не загрузилась — кликни для дебага';
  div.dataset.assetUrl = url;
  div.innerHTML = `
    <div class="bip-icon">🚫</div>
    <div class="bip-label">Фото утеряно</div>
    <div class="bip-hint">Клик — дебаг</div>`;
  div.onclick = (e) => { e.stopPropagation(); debugAsset(url); };
  return div;
}

// Click-handler for broken-image placeholders. Hits a debug endpoint that
// reports filesystem state for the asset path so the user can paste the JSON
// blob back to support / dev. Resilient: never throws into the UI.
async function debugAsset(url) {
  try {
    // url looks like '/assets/<sid>/<rel_path>?v=<cache-buster>' — strip query
    // string before feeding rel_path to the debug API. Without this strip the
    // backend tries to open `file.jpg?v=1778...` literally, always reports
    // exists=false even when the file IS on disk. (User-reported false alarm.)
    const cleanUrl = url.split('?')[0].split('#')[0];
    const m = cleanUrl.match(/^\/assets\/([^/]+)\/(.+)$/);
    if (!m) {
      alert('Не разобрать путь к ассету: ' + url);
      return;
    }
    const [, sid, relPath] = m;
    const r = await api.get(`/api/series/${encodeURIComponent(sid)}/debug-asset?path=${encodeURIComponent(relPath)}`);
    const dump = JSON.stringify(r, null, 2);
    // Modal-ish: open a textarea-in-prompt so user can copy.
    const overlay = document.createElement('div');
    overlay.className = 'lightbox-overlay';
    overlay.style.zIndex = 99999;
    overlay.innerHTML = `
      <button class="lb-close" onclick="this.parentElement.remove()">✕</button>
      <div class="lightbox-content" onclick="event.stopPropagation()" style="max-width:720px">
        <div class="lightbox-panel" style="width:100%">
          <h3>🔍 Дебаг ассета</h3>
          <div style="font-size:0.78rem;color:var(--muted);word-break:break-all">${esc(url)}</div>
          <textarea readonly rows="18" style="width:100%;font-family:monospace;font-size:0.78rem">${esc(dump)}</textarea>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
            <button class="btn-regen" onclick="navigator.clipboard.writeText(this.parentElement.previousElementSibling.value);this.textContent='✓ Скопировано'">📋 Скопировать дебаг</button>
            <button class="btn-regen" style="background:linear-gradient(135deg,#10b981,#059669)" onclick="relinkOrphanedAssets(this)">🔗 Найти и привязать потерянные</button>
          </div>
          <div id="relink-result" style="font-size:0.82rem;color:var(--muted);margin-top:8px;white-space:pre-wrap"></div>
        </div>
      </div>`;
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    document.body.appendChild(overlay);
  } catch (e) {
    alert('Дебаг не удался: ' + (e?.message || e));
  }
}

// Calls /relink-assets which scans the assets/ folder and re-attaches orphan
// files to characters/locations/items whose ref_images is empty. Runs from
// the broken-image debug modal so the user can self-heal a series after a
// JSON-corruption incident wiped refs.
async function relinkOrphanedAssets(btn) {
  if (!S.seriesId) { alert('Открой сериал сначала'); return; }
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ Сканирую...';
  const out = document.getElementById('relink-result');
  if (out) out.textContent = '';
  try {
    const r = await api.post(`/api/series/${S.seriesId}/relink-assets`, {});
    bumpAssetVersion();
    if (r?.error) throw new Error(r.error);
    const lines = (r.relinked || []).map(x =>
      `✓ ${x.kind} «${x.name}» → ${x.files.join(', ')}`
    ).join('\n');
    if (out) out.textContent = r.count
      ? `Привязано: ${r.count}\n${lines}`
      : 'Орфанов не найдено — все ассеты на местах.';
    // Refresh series state and re-render lists.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    renderCharactersList && renderCharactersList();
    renderLocationsList && renderLocationsList();
    renderItemsList && renderItemsList();
    renderEpCharacters && renderEpCharacters();
    renderEpLocations && renderEpLocations();
    renderEpItems && renderEpItems();
    btn.textContent = '✓ Готово';
  } catch (e) {
    if (out) out.textContent = '✗ ' + (e?.message || e);
    btn.disabled = false; btn.textContent = orig;
  }
}

// Plain image lightbox (used for outfit photos — no regen panel)
function openLightbox(url) {
  closeLightbox();
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

// Outfit lightbox: shows the outfit photo with prev/next nav across ALL of the
// character's photos (base portrait as item 0, then each outfit with a photo).
// Lets the user browse the wardrobe full-size without closing the lightbox
// between picks. Arrow keys + on-screen arrows + regenerate-with-wishes.
let _outfitLightboxState = null;  // {charId, items: [{kind, id, label, photoUrl}], idx}

function openOutfitLightbox(charId, outfitId) {
  closeLightbox();
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  const items = [];
  if (c.ref_images?.length) {
    items.push({
      kind: 'base', id: 'base', label: 'База',
      photoUrl: `${assetUrl(c.ref_images[0])}`,
    });
  }
  for (const o of (c.outfits || [])) {
    if (o.photo) {
      items.push({
        kind: 'outfit', id: o.id, label: o.label || '(без названия)',
        photoUrl: `${assetUrl(o.photo)}`,
      });
    }
  }
  if (!items.length) { showToast('Нет фото для просмотра'); return; }
  let idx = items.findIndex(it => it.kind === 'outfit' && it.id === outfitId);
  if (idx < 0) idx = items.findIndex(it => it.kind === 'base' && outfitId === 'base');
  if (idx < 0) idx = 0;
  _outfitLightboxState = { charId, items, idx };
  _renderOutfitLightbox();
}

function _renderOutfitLightbox() {
  const st = _outfitLightboxState;
  if (!st) return;
  const cur = st.items[st.idx];
  const c = S.series.characters.find(x => x.id === st.charId);
  const total = st.items.length;
  const prevDisabled = total < 2 ? 'disabled' : '';
  const nextDisabled = total < 2 ? 'disabled' : '';
  let div = document.getElementById('lightbox-overlay');
  if (!div) {
    div = document.createElement('div');
    div.id = 'lightbox-overlay';
    div.className = 'lightbox-overlay';
    div.onclick = (e) => { if (e.target === div) closeLightbox(); };
    document.body.appendChild(div);
  }
  // Re-render only the inner content; keep the overlay (avoids flicker on nav).
  const isOutfit = cur.kind === 'outfit';
  const outfitObj = isOutfit ? (c?.outfits || []).find(o => o.id === cur.id) : null;
  const regenLabel = isOutfit
    ? `↻ Перегенерировать образ «${esc(cur.label)}»`
    : '↻ Перегенерировать базу персонажа';
  const wishesPlaceholder = isOutfit
    ? 'Например:\n«Цвет более тёмный»\n«Без сумки»\n«Длиннее юбка»'
    : 'Например:\n«Без шрама на лице»\n«Глаза карие, не голубые»';
  const initialWishes = isOutfit ? '' : (c?.image_constraints || '');
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <button class="lb-nav lb-prev" ${prevDisabled} onclick="event.stopPropagation();outfitLightboxNav(-1)" title="Предыдущий (←)">‹</button>
    <button class="lb-nav lb-next" ${nextDisabled} onclick="event.stopPropagation();outfitLightboxNav(1)" title="Следующий (→)">›</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap">
        <img src="${cur.photoUrl}" alt="${esc(cur.label)}"
             onerror="this.replaceWith(_brokenImagePlaceholder('${cur.photoUrl}'))">
        <div class="lb-caption">
          <strong>${esc(cur.label)}</strong>
          <span style="color:var(--muted);margin-left:8px">${st.idx + 1} / ${total}</span>
        </div>
      </div>
      <div class="lightbox-panel">
        <h3>${regenLabel}</h3>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-wishes" rows="5"
            placeholder="${esc(wishesPlaceholder)}">${esc(initialWishes)}</textarea>
        </div>
        ${!isOutfit ? `
          <label class="cb">
            <input type="checkbox" id="lb-regen-outfits" checked>
            <span>Также перегенерировать все костюмы</span>
          </label>
        ` : ''}
        <div id="lb-regen-status" class="lb-status"></div>
        <button id="lb-regen-btn" class="btn-regen" onclick="${isOutfit
            ? `regenerateOutfitFromLightbox('${cur.id}')`
            : 'regenerateCharacterFromLightbox()'}">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
}

function outfitLightboxNav(delta) {
  const st = _outfitLightboxState;
  if (!st || st.items.length < 2) return;
  st.idx = (st.idx + delta + st.items.length) % st.items.length;
  _renderOutfitLightbox();
}

async function regenerateOutfitFromLightbox(outfitId) {
  const st = _outfitLightboxState;
  if (!st) return;
  const wishes = (document.getElementById('lb-regen-wishes')?.value || '').trim();
  const btn = document.getElementById('lb-regen-btn');
  const status = document.getElementById('lb-regen-status');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерирую...'; }
  if (status) status.textContent = '';
  try {
    // Persist wishes onto the outfit's description so future i2i bakes them in.
    if (wishes) {
      await api.put(`/api/series/${S.seriesId}/characters/${st.charId}/outfits/${outfitId}`, {
        description: wishes,
      });
    }
    // Trigger generation (uses existing endpoint that the outfit row uses).
    const r = await api.post(`/api/series/${S.seriesId}/characters/${st.charId}/outfits/${outfitId}/generate`, {});
    if (r?.error) throw new Error(r.error);
    // Refresh series state and re-render the lightbox with the new image.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    if (typeof renderCharactersList === 'function') renderCharactersList();
    if (typeof openCharAssets === 'function' && document.getElementById('modal-char-assets')?.classList.contains('open')) {
      const c = S.series.characters.find(x => x.id === st.charId);
      if (c) renderOutfitsList(c);
    }
    // Rebuild item list from fresh state and stay on the same outfit.
    openOutfitLightbox(st.charId, outfitId);
    if (status) status.textContent = '✓ Готово';
  } catch (e) {
    if (status) status.textContent = '✗ ' + (e?.message || e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Перегенерировать'; }
  }
}

// Character-photo lightbox: image + regenerate panel
function openCharLightbox(charId, url) {
  closeLightbox();
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  currentCharId = charId;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать персонажа</h3>
        <div class="hint">
          Сначала перегенерируется <strong style="color:var(--text)">основной образ</strong>,
          затем по нему как референсу — все костюмы. Старые фото удалятся.
        </div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-wishes" rows="5"
            placeholder="Например:&#10;«Без шрама на лице»&#10;«Глаза карие, не голубые»&#10;«Волосы чуть короче»">${esc(c.image_constraints || '')}</textarea>
          <div style="font-size:0.76rem;color:var(--muted);margin-top:4px">
            Сохранится в персонаже и будет применяться при всех будущих генерациях.
          </div>
        </div>
        <label class="cb">
          <input type="checkbox" id="lb-regen-outfits" checked>
          <span>Также перегенерировать все костюмы</span>
        </label>
        <div id="lb-regen-status" class="lb-status"></div>
        <button id="lb-regen-btn" class="btn-regen" onclick="regenerateCharacterFromLightbox()">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

function closeLightbox() {
  const old = document.getElementById('lightbox-overlay');
  if (old) old.remove();
  _outfitLightboxState = null;
}
document.addEventListener('keydown', (e) => {
  if (!document.getElementById('lightbox-overlay')) return;
  if (e.key === 'Escape') return closeLightbox();
  if (_outfitLightboxState) {
    if (e.key === 'ArrowLeft')  { e.preventDefault(); outfitLightboxNav(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); outfitLightboxNav(1);  }
  }
});

async function regenerateCharacterFromLightbox() {
  if (!currentCharId) return;
  const wishes = (document.getElementById('lb-regen-wishes').value || '').trim();
  const regenOutfits = document.getElementById('lb-regen-outfits').checked;
  const status = document.getElementById('lb-regen-status');
  const btn = document.getElementById('lb-regen-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.className = 'lb-status';
  status.textContent = 'Перегенерируем основной образ' + (regenOutfits ? ' + костюмы' : '') + ' (~15-30 сек на каждый)...';

  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/characters/${currentCharId}/regenerate`,
      { wishes, regenerate_outfits: regenOutfits }
    );
    if (res.error) {
      status.className = 'lb-status err';
      status.textContent = 'Ошибка: ' + res.error;
      btn.disabled = false;
      btn.innerHTML = '↻ Перегенерировать';
      return;
    }
    const totalOutfits = (res.regenerated_outfits || []).length;
    const failed = res.failed_outfits || [];
    status.className = failed.length ? 'lb-status' : 'lb-status ok';
    let msg = '✓ Основной образ обновлён';
    if (regenOutfits) msg += ` · костюмов: ${totalOutfits}`;
    if (failed.length) msg += ` · ошибок: ${failed.length} (${failed.join(', ')})`;
    status.textContent = msg;

    // Refresh state
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const c = S.series.characters.find(x => x.id === currentCharId);
    renderCharAssetsGrid(c);
    renderOutfitsList(c);
    renderCharactersList();

    // Swap lightbox image to the freshly generated base (cache-bust)
    const freshUrl = res.base_url + '?t=' + Date.now();
    const lbImg = document.querySelector('#lightbox-overlay .lb-img-wrap img');
    if (lbImg) lbImg.src = freshUrl;

    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать ещё раз';
  } catch (e) {
    status.className = 'lb-status err';
    status.textContent = 'Ошибка: ' + e.message;
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

async function uploadCharacterRefs(charId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/character/${charId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const c = S.series.characters.find(x => x.id === charId);
  renderCharAssetsGrid(c);
  renderCharactersList();
}

async function deleteCharRef(charId, filename) {
  await api.del(`/api/series/${S.seriesId}/assets/character/${charId}/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const c = S.series.characters.find(x => x.id === charId);
  renderCharAssetsGrid(c);
  renderCharactersList();
}

// ── Outfit management ─────────────────────────────────────────────────────────
let editingOutfitId = null;

function renderOutfitsList(char) {
  const el = document.getElementById('outfits-list');
  if (!el) return;
  const outfits = char.outfits || [];
  if (!outfits.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">Нет костюмов. Добавь первый!</div>';
    return;
  }
  el.innerHTML = outfits.map(o => {
    const hasPhoto = !!o.photo;
    const photoUrl = hasPhoto ? `${assetUrl(o.photo)}` : '';
    const photoHtml = hasPhoto
      ? `<img src="${photoUrl}" alt="">`
      : `<div style="font-size:2.2rem">👗</div>`;
    const photoClick = hasPhoto ? `onclick="openOutfitLightbox('${char.id}','${o.id}')"` : '';
    return `
      <div class="outfit-card ${hasPhoto ? 'has-photo' : ''}" id="outfit-card-${o.id}">
        <div class="outfit-photo" ${photoClick}>${photoHtml}</div>
        <div class="outfit-info">
          <div class="outfit-label">${esc(o.label)}</div>
          ${o.description ? `<div class="outfit-desc">${esc(o.description)}</div>` : ''}
          <div class="outfit-btns">
            <button class="btn-ghost" onclick="openEditOutfit('${char.id}','${o.id}')">✎</button>
            ${hasPhoto
              ? `<button class="btn-ghost" style="color:var(--accent)" onclick="generateOutfit('${char.id}','${o.id}')">↻ Перегенерировать</button>`
              : `<button class="btn-ghost" style="color:var(--accent)" onclick="generateOutfit('${char.id}','${o.id}')">⚡ Сгенерировать</button>`}
            <button class="btn-ghost" style="color:var(--danger)" onclick="deleteOutfit('${char.id}','${o.id}')">✕</button>
          </div>
          <div class="outfit-status" id="outfit-status-${o.id}"></div>
        </div>
      </div>
    `;
  }).join('');
}

function openAddOutfit(charId) {
  editingOutfitId = null;
  currentCharId = charId;
  document.getElementById('outfit-modal-title').textContent = 'Новый костюм';
  clearFields(['outfit-label', 'outfit-description']);
  openModal('modal-outfit');
}

function openEditOutfit(charId, outfitId) {
  const c = S.series.characters.find(x => x.id === charId);
  const o = (c?.outfits || []).find(x => x.id === outfitId);
  if (!o) return;
  editingOutfitId = outfitId;
  currentCharId = charId;
  document.getElementById('outfit-modal-title').textContent = 'Редактировать костюм';
  setVal('outfit-label', o.label);
  setVal('outfit-description', o.description);
  openModal('modal-outfit');
}

async function saveOutfit() {
  const label = val('outfit-label');
  if (!label) return alert('Введи название костюма');
  const data = { label, description: val('outfit-description') };
  if (editingOutfitId) {
    await api.put(`/api/series/${S.seriesId}/characters/${currentCharId}/outfits/${editingOutfitId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/characters/${currentCharId}/outfits`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-outfit');
  const c = S.series.characters.find(x => x.id === currentCharId);
  renderOutfitsList(c);
}

async function deleteOutfit(charId, outfitId) {
  if (!confirm('Удалить костюм?')) return;
  await api.del(`/api/series/${S.seriesId}/characters/${charId}/outfits/${outfitId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const c = S.series.characters.find(x => x.id === charId);
  renderOutfitsList(c);
}

async function generateOutfit(charId, outfitId) {
  const statusEl = document.getElementById(`outfit-status-${outfitId}`);
  if (!statusEl) return;

  const card = document.getElementById(`outfit-card-${outfitId}`);
  const genBtn = card?.querySelector('.outfit-btns button[style*="accent"]');
  if (genBtn) { genBtn.disabled = true; genBtn.innerHTML = '<span class="spinner"></span>'; }
  statusEl.textContent = 'Генерируем (~15 сек)...';

  try {
    const res = await api.post(`/api/series/${S.seriesId}/characters/${charId}/outfits/${outfitId}/generate`, {});
    if (res.ready) {
      statusEl.textContent = '';
      S.series = await api.get(`/api/series/${S.seriesId}`);
      const c = S.series.characters.find(x => x.id === charId);
      renderOutfitsList(c);
      showToast('Костюм готов!');
    } else {
      statusEl.textContent = 'Ошибка: ' + (res.error || 'Неизвестная ошибка');
      if (genBtn) { genBtn.disabled = false; genBtn.textContent = '⚡ Сгенерировать'; }
    }
  } catch (e) {
    statusEl.textContent = 'Ошибка: ' + e.message;
    if (genBtn) { genBtn.disabled = false; genBtn.textContent = '⚡ Сгенерировать'; }
  }
}

// ── Regenerate character (base first, then outfits) ─────────────────────────
function openRegenChar() {
  if (!currentCharId) return;
  const c = S.series.characters.find(x => x.id === currentCharId);
  if (!c) return;
  setVal('regen-char-wishes', c.image_constraints || '');
  document.getElementById('regen-char-outfits').checked = true;
  document.getElementById('regen-char-status').textContent = '';
  const btn = document.getElementById('regen-char-confirm-btn');
  btn.disabled = false;
  btn.innerHTML = '↻ Перегенерировать';
  openModal('modal-regen-char');
}

async function regenerateCharacter() {
  if (!currentCharId) return;
  const wishes = (val('regen-char-wishes') || '').trim();
  const regenOutfits = document.getElementById('regen-char-outfits').checked;
  const status = document.getElementById('regen-char-status');
  const btn = document.getElementById('regen-char-confirm-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем основной образ...';
  status.style.color = 'var(--warning)';
  status.textContent = 'Перегенерируем основной образ (~15 сек)...';

  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/characters/${currentCharId}/regenerate`,
      { wishes, regenerate_outfits: regenOutfits }
    );
    if (res.error) {
      status.style.color = 'var(--danger)';
      status.textContent = 'Ошибка: ' + res.error;
      btn.disabled = false;
      btn.innerHTML = '↻ Перегенерировать';
      return;
    }
    const totalOutfits = (res.regenerated_outfits || []).length;
    const failedOutfits = (res.failed_outfits || []);
    status.style.color = 'var(--success)';
    let msg = '✓ Основной образ обновлён';
    if (regenOutfits) msg += ` · костюмов перегенерировано: ${totalOutfits}`;
    if (failedOutfits.length) msg += ` · ошибок: ${failedOutfits.length} (${failedOutfits.join(', ')})`;
    status.textContent = msg;
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const c = S.series.characters.find(x => x.id === currentCharId);
    renderCharAssetsGrid(c);
    renderOutfitsList(c);
    renderCharactersList();
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать ещё раз';
    setTimeout(() => {
      if (!failedOutfits.length) closeModal('modal-regen-char');
    }, 1800);
  } catch (e) {
    status.style.color = 'var(--danger)';
    status.textContent = 'Ошибка: ' + e.message;
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

// ── Style editor ──────────────────────────────────────────────────────────────
// Cached preset list (loaded once per session).
let _stylePresetsCache = null;

async function _loadStylePresets() {
  if (_stylePresetsCache) return _stylePresetsCache;
  try {
    const r = await fetch('/api/style-presets').then(x => x.json());
    _stylePresetsCache = r.presets || [];
  } catch {
    _stylePresetsCache = [];
  }
  return _stylePresetsCache;
}

async function openStyleEditor() {
  const presets = await _loadStylePresets();
  const cur = (S.series && S.series.style) || {};
  const grid = document.getElementById('style-presets-grid');
  if (!grid) { openModal('modal-style'); return; }
  // Show admin-only "Регенерить сэмплы" button only to primary user.
  const adminBtn = document.getElementById('btn-regen-style-samples');
  if (adminBtn) {
    adminBtn.style.display = (window._currentUser && window._currentUser.is_primary) ? '' : 'none';
  }
  setVal('style-type', cur.type || 'cinematic');
  // Render preset cards + custom card at the end.
  grid.innerHTML = presets.map(p => `
    <div class="style-card ${cur.type === p.id ? 'selected' : ''}" data-preset="${esc(p.id)}" onclick="_styleCardSelect('${esc(p.id)}')">
      <div class="style-thumb">
        ${p.sample
          ? `<img src="${esc(p.sample)}" alt="${esc(p.label)}" onerror="this.parentElement.innerHTML='<span>нет сэмпла<br><small>(сгенерируй через 🎨 в админке)</small></span>'">`
          : `<span style="font-size:1.6rem">🎲</span>`}
      </div>
      <div class="style-info">
        <div class="name">${esc(p.label)}</div>
        ${p.desc ? `<div class="desc">${esc(p.desc.slice(0, 90))}${p.desc.length > 90 ? '…' : ''}</div>` : '<div class="desc">AI выберет стиль исходя из тона серии</div>'}
      </div>
    </div>`).join('') + `
    <div class="style-card custom-card ${cur.type === 'custom' ? 'selected' : ''}" data-preset="custom" onclick="_styleCardSelect('custom')">
      <div class="style-thumb" id="style-custom-preview">
        🎨
      </div>
      <div class="style-info">
        <div class="name">Свой стиль</div>
        <div class="desc">Опиши и сгенерь сэмпл</div>
      </div>
      <div class="custom-input">
        <textarea id="style-custom-desc" placeholder="Например: art-nouveau с сепией, гравюрная штриховка, стиль 1920-х" onclick="event.stopPropagation()" oninput="event.stopPropagation()">${esc(cur.custom_description || '')}</textarea>
        <button class="btn-sample" onclick="event.stopPropagation();_generateStyleSample()">🎨 Сгенерировать сэмпл</button>
      </div>
    </div>`;
  openModal('modal-style');
}

function _styleCardSelect(id) {
  document.querySelectorAll('.style-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.preset === id);
  });
  setVal('style-type', id);
}

async function _generateStyleSample() {
  const desc = (document.getElementById('style-custom-desc')?.value || '').trim();
  if (!desc) { alert('Опиши стиль текстом сначала'); return; }
  const btn = document.querySelector('.style-card.custom-card .btn-sample');
  const preview = document.getElementById('style-custom-preview');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерирую...'; }
  if (preview) preview.innerHTML = '<span class="spinner"></span>';
  try {
    const r = await api.post('/api/style-sample', { description: desc, base: 'man' });
    if (r.error) throw new Error(r.error);
    if (preview) preview.innerHTML = `<img src="${esc(r.url)}" alt="custom style sample" style="width:100%;height:100%;object-fit:cover;display:block">`;
    // Auto-select the custom card so save uses it.
    _styleCardSelect('custom');
  } catch (e) {
    if (preview) preview.innerHTML = `<span style="color:var(--danger);font-size:0.78rem">✗ ${esc(e?.message || e)}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🎨 Сгенерировать сэмпл'; }
  }
}

async function saveStyle() {
  await api.put(`/api/series/${S.seriesId}`, {
    style: { type: val('style-type'), custom_description: val('style-custom-desc') }
  });
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-style');
  renderStyleSection();
}

// Primary-only: rebuild all baseline preset samples (cinematic, photorealistic,
// anime, pixar, noir) by calling AVAI 5 times. ~50s + ~$0.10. Saves images
// to static/img/style-samples/<id>.jpg so every user sees them in the picker.
async function regenerateAllStyleSamples() {
  if (!confirm('Сгенерировать все 5 базовых сэмплов через AVAI?\n\nЗаймёт ~50 секунд, потратит ~$0.10. После — карточки стилей у всех юзеров получат превью.')) return;
  const btn = document.getElementById('btn-regen-style-samples');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерирую (~50с)...'; }
  try {
    const r = await api.post('/api/admin/regenerate-style-samples', {});
    const ok = (r.results || []).filter(x => x.ok).length;
    const fail = (r.results || []).filter(x => !x.ok);
    let msg = `✓ Готово: ${ok}/${r.results?.length || 0} сэмплов сгенерировано`;
    if (fail.length) {
      msg += '\n\nОшибки:\n' + fail.map(x => `  • ${x.id}: ${x.err}`).join('\n');
    }
    alert(msg);
    // Force browser to refetch the new images by bumping cache buster
    bumpAssetVersion();
    // Refresh preset cache so new sample paths show
    _stylePresetsCache = null;
    closeModal('modal-style');
    setTimeout(() => openStyleEditor(), 200);
  } catch (e) {
    alert('Ошибка: ' + (e?.message || e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Регенерить сэмплы'; }
  }
}

// Auto-prompt the user for a style choice after script-import or when a
// fresh series with no style picked yet opens. Called from importCreateSeries
// (right after navigating to the new series) and from loadSeriesView when
// style.type === 'cinematic' is the default-untouched value.
let _styleAutoPromptedFor = null;
async function maybePromptForStyle(force = false) {
  if (!S.series) return;
  const sid = S.seriesId;
  if (!force && _styleAutoPromptedFor === sid) return;
  // Skip if user already actively saved a non-default style choice.
  const cur = S.series.style || {};
  const isDefault = (cur.type === 'cinematic' && !cur.custom_description) || !cur.type;
  if (!force && !isDefault) return;
  _styleAutoPromptedFor = sid;
  setTimeout(() => openStyleEditor(), 400);
}

// ── Episodes ──────────────────────────────────────────────────────────────────
// One-shot create-episode: skip the modal entirely. Pick the next free number
// (or the first existing one with no synopsis), POST it, jump straight into
// the episode view. Synopsis generation moved to be a button inside the
// episode page next to the synopsis textarea.
async function openCreateEpisode() {
  if (_creatingEpisode) return;
  const existing = new Map(S.episodes.map(e => [e.number, e]));
  const maxNum = existing.size > 0 ? Math.max(...existing.keys()) : 0;
  let defaultNum = maxNum + 1;
  for (let n = 1; n <= maxNum + 1; n++) {
    if (!existing.has(n)) { defaultNum = n; break; }
    const ep = existing.get(n);
    if (!ep.synopsis || !ep.synopsis.trim()) { defaultNum = n; break; }
  }
  _creatingEpisode = true;
  const idempotencyKey = (window.crypto && crypto.randomUUID && crypto.randomUUID())
    || `ep-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  try {
    const ep = await api.post(
      `/api/series/${S.seriesId}/episodes`,
      { synopsis: '', number: defaultNum },
      { idempotencyKey }
    );
    if (!S.episodes.some(e => e.number === ep.number)) S.episodes.push(ep);
    // Stay on the series view — re-render the episode list so the new card
    // appears. User clicks it themselves when they want to enter.
    if (typeof renderEpisodesList === 'function') renderEpisodesList();
    showToast(`✓ Создан Эп. ${ep.number}`);
  } catch (e) {
    showToast('Ошибка: ' + (e?.message || e));
  } finally {
    _creatingEpisode = false;
  }
}

// Renders a strip of clickable pills for the episodes immediately before
// and after the current one. Replaces the old "Эпизод N" duplicate input
// next to the "Эп. N" badge — gives the user one-click navigation between
// neighbouring episodes without going back to the series view.
function renderEpisodeNeighbours() {
  const nav = document.getElementById('ep-neighbours');
  if (!nav) return;
  const eps = (S.episodes || []).slice().sort((a, b) => a.number - b.number);
  if (!eps.length) { nav.innerHTML = ''; return; }
  const cur = S.episode?.number;
  const idx = eps.findIndex(e => e.number === cur);
  if (idx < 0) { nav.innerHTML = ''; return; }
  // Show ±3 around current (configurable). Stops at boundaries.
  const window = 3;
  const start = Math.max(0, idx - window);
  const end   = Math.min(eps.length - 1, idx + window);
  const parts = [];
  if (start > 0) parts.push(`<span class="epn-arrow" title="Есть ещё эпизоды до этих">…</span>`);
  for (let i = start; i <= end; i++) {
    const ep = eps[i];
    const isCur = ep.number === cur;
    const label = isBatchMode(S.series) ? chunkLabel(S.series, ep.number, { short: true }) : `Эп. ${ep.number}`;
    parts.push(`<a class="epn-pill ${isCur ? 'current' : ''}" ${isCur ? '' : `onclick="navigate('episode',{seriesId:'${S.seriesId}',episodeNum:${ep.number}})"`} title="${esc(ep.title || '')}">${label}</a>`);
  }
  if (end < eps.length - 1) parts.push(`<span class="epn-arrow" title="Есть ещё эпизоды после этих">…</span>`);
  nav.innerHTML = parts.join('');
}

// Inline synopsis generation inside the episode page (replaces the
// modal-based one that fired before the episode was created).
async function generateEpisodeSynopsisInline() {
  if (!S.episode) { alert('Открой эпизод'); return; }
  const btn = document.getElementById('ep-syn-gen-btn');
  const status = document.getElementById('ep-syn-gen-status');
  const ta = document.getElementById('ep-synopsis');
  const orig = btn?.innerHTML;
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Генерирую...'; }
  if (status) status.textContent = '';
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title, episodeNum: S.episode.number };
    const res = await trackTask(`Синопсис Эп. ${S.episode.number}`, ctx, () =>
      api.post(`/api/series/${S.seriesId}/generate-next-episode-synopsis`,
               { episode_number: S.episode.number })
    );
    if (res?.synopsis) {
      ta.value = res.synopsis;
      S.episode.synopsis = res.synopsis;
      // Persist immediately so the user doesn't lose the gen on accidental reload.
      await api.put(`/api/series/${S.seriesId}/episodes/${S.episode.number}`, { synopsis: res.synopsis });
      if (status) { status.textContent = '✓ Готово'; setTimeout(() => { if (status.textContent === '✓ Готово') status.textContent = ''; }, 4000); }
    }
  } catch (e) {
    if (status) status.textContent = '✗ ' + (e?.message || e);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

async function generateNewEpSynopsis() {
  const btn = document.getElementById('btn-gen-new-ep-synopsis');
  const status = document.getElementById('new-ep-synopsis-status');
  const epNum = parseInt(document.getElementById('new-ep-number').value) || null;
  btn.disabled = true;
  status.textContent = 'Генерирую…';
  status.style.color = 'var(--muted)';
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title, episodeNum: epNum || undefined };
    const res = await trackTask(`Синопсис${epNum ? ' Эп. ' + epNum : ''}`, ctx, () =>
      api.post(`/api/series/${S.seriesId}/generate-next-episode-synopsis`, { episode_number: epNum })
    );
    document.getElementById('new-ep-synopsis').value = res.synopsis || '';
    status.textContent = '✓ Готово';
    status.style.color = 'var(--success)';
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false;
  }
}

let _creatingEpisode = false;
async function createEpisode() {
  // Guard against double-submit: T7 writes are slow, and a second click while
  // the first POST is in-flight races on episode-number assignment — first POST
  // grabs N, second POST sees N already taken and auto-falls-through to N+1,
  // producing two duplicate episodes.
  if (_creatingEpisode) return;
  _creatingEpisode = true;

  const modal = document.getElementById('modal-create-episode');
  const submitBtn = modal?.querySelector('.modal-footer .btn-primary');
  const cancelBtn = modal?.querySelector('.modal-footer .btn-ghost');
  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Создаю…'; }
  if (cancelBtn) cancelBtn.disabled = true;

  // Idempotency key — fresh per click. If the same POST somehow fires twice
  // (network retry, browser extension, double-handler), backend returns the
  // CACHED first response instead of creating a second episode.
  const idempotencyKey = (window.crypto && crypto.randomUUID && crypto.randomUUID())
    || `ep-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

  try {
    const epNum = parseInt(document.getElementById('new-ep-number').value) || null;
    const ep = await api.post(
      `/api/series/${S.seriesId}/episodes`,
      { synopsis: val('new-ep-synopsis'), number: epNum },
      { idempotencyKey }
    );
    closeModal('modal-create-episode');
    // De-dup: if push would create a duplicate (idempotency replay), skip
    if (!S.episodes.some(e => e.number === ep.number)) {
      S.episodes.push(ep);
    }
    navigate('episode', { seriesId: S.seriesId, episodeNum: ep.number });
  } finally {
    _creatingEpisode = false;
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Создать'; }
    if (cancelBtn) cancelBtn.disabled = false;
  }
}

async function clearEpisodeConfirm(num) {
  if (!confirm(`Очистить эпизод ${num}? Синопсис, сценарий и персонажи будут удалены.`)) return;
  const ep = await api.post(`/api/series/${S.seriesId}/episodes/${num}/clear`, {});
  const idx = S.episodes.findIndex(e => e.number === num);
  if (idx !== -1) S.episodes[idx] = ep;
  renderEpisodesList();
}

async function deleteEpisodeConfirm(num) {
  if (!confirm(`Удалить эпизод ${num}?`)) return;
  await api.del(`/api/series/${S.seriesId}/episodes/${num}`);
  S.episodes = S.episodes.filter(e => e.number !== num);
  renderEpisodesList();
}

// ── Episode editor ────────────────────────────────────────────────────────────
async function loadEpisodeView() {
  S.series = S.series || await api.get(`/api/series/${S.seriesId}`);
  S.episode = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
  applyVideoProviderMode();
  if (typeof sdInitForEpisode === 'function') {
    setTimeout(() => sdInitForEpisode(), 50);
  }
  // Hook the episode-side autogen-sweep button into the same poller as
  // the series view. Если sweep уже идёт (запущен на серии или на другом
  // эпизоде) — кнопка-копия в этом эпизоде сразу подхватит статус и не
  // будет показывать "🎨 Сгенерировать недостающее" пока процесс активен.
  if (typeof checkAutogenOnLoad === 'function') checkAutogenOnLoad();

  document.getElementById('ep-number-badge').textContent = chunkLabel(S.series, S.episodeNum, { short: true });
  renderEpisodeNeighbours();
  setVal('ep-title-input', S.episode.title);
  setVal('ep-synopsis', S.episode.synopsis);
  setVal('ep-script', S.episode.script);
  setVal('ep-scene-blocking', S.episode.scene_blocking || '');
  // If user previously had the scene-view open (it persists across navigation
  // because we don't tear down the DOM), the inner cards still show the
  // PREVIOUS episode's parsed scenes — `setVal('ep-script', ...)` only
  // updates the hidden textarea. Re-render the scene-view body so it reflects
  // the current episode's script.
  const sceneView = document.getElementById('ep-script-scenes');
  // Auto-open scene view if the episode was already accepted (cast_extracted=true).
  // Reproduces the state the user left in: they don't have to click "Принять"
  // again on every page reload. The accept button is also reskinned to "✅ Принят"
  // and the "🔄 Перепроанализировать" button becomes visible for explicit re-runs.
  const acceptBtn = document.getElementById('ep-accept-script-btn');
  const reanalyzeBtn = document.getElementById('ep-reanalyze-btn');
  if (S.episode.cast_extracted === true && (S.episode.script || '').trim()) {
    if (sceneView && sceneView.classList.contains('hidden')) {
      try { toggleSceneView(); } catch {}
    }
    if (acceptBtn) {
      acceptBtn.innerHTML = '✅ Принят';
      acceptBtn.style.opacity = '0.55';
      acceptBtn.title = 'Сценарий уже принят — клик переключит в режим сцен. Чтобы заново разобрать персонажей/предметы — кнопка «🔄 Перепроанализировать» справа.';
    }
    if (reanalyzeBtn) reanalyzeBtn.style.display = '';
  } else {
    if (acceptBtn) {
      acceptBtn.innerHTML = '✅ Принять сценарий';
      acceptBtn.style.opacity = '';
      acceptBtn.title = 'Сохранить сценарий, найти новых персонажей/локации/предметы, при необходимости показать модалку для drag-drop фоток, потом перейти в режим сцен и запустить автоген';
    }
    if (reanalyzeBtn) reanalyzeBtn.style.display = 'none';
  }
  if (sceneView && !sceneView.classList.contains('hidden')) {
    if (typeof _renderSceneViewBody === 'function') _renderSceneViewBody();
  }
  // Apply Turbo/Sequential UI visibility on episode load (scene-blocking
  // section hidden in sequential mode where it's irrelevant).
  if (typeof _applyAutoModeUI === 'function') _applyAutoModeUI();
  setVal('ep-reteller-prompt', S.episode.reteller_prompt || '');
  updateScriptCounter();

  document.getElementById('ep-status-badge').className = `status-badge status-${S.episode.status}`;
  document.getElementById('ep-status-badge').textContent = statusLabel(S.episode.status);
  const readyEl = document.getElementById('ep-ready-toggle');
  if (readyEl) readyEl.checked = !!S.episode.ready;

  // Auto-detect character/location checkboxes from existing script if not set
  const hasScript = !!(S.episode.script || '').trim();
  const hasChecked = (S.episode.characters_used || []).length > 0 || (S.episode.locations_used || []).length > 0;
  if (hasScript && !hasChecked) {
    autoDetectFromScript();
  }

  // NOTE: removed auto-extract-from-story trigger here. Characters & locations
  // are now derived ONLY from the actual script — extraction-from-synopsis-text
  // was producing chars/locs that don't appear in the episode. Use the
  // "🎭 Извлечь персонажей и локации" button on the episode view instead, which
  // calls /extract-characters (script-based).

  renderEpCharacters();
  renderEpLocations();
  renderEpItems();
  renderEpReteller();
  updateGenScriptBtn();

  setBreadcrumb([
    { label: 'Сериалы', action: "navigate('projects')" },
    { label: S.series.title, action: `navigate('series',{seriesId:'${S.seriesId}'})` },
    { label: `${chunkLabel(S.series, S.episodeNum, { short: true })}: ${S.episode.title}` },
  ]);

  // Surface any canon-audit panel that was deferred while user was elsewhere
  if (typeof flushPendingCanonForCurrentEpisode === 'function') {
    flushPendingCanonForCurrentEpisode();
  }
}

function updateGenScriptBtn() {
  const btn = document.getElementById('ep-gen-script-btn');
  if (!btn) return;
  const hasScript = !!(val('ep-script'));
  btn.textContent = hasScript ? '↻ Перегенерировать сценарий' : '⚡ Сгенерировать сценарий';
  btn.className = `ep-script-btn ${hasScript ? 'done' : 'ready'}`;
}

async function openScriptHistory() {
  if (!S.episode) { alert('Сначала открой эпизод'); return; }
  const list = document.getElementById('script-history-list');
  list.innerHTML = '<div style="opacity:0.6">Загрузка...</div>';
  openModal('modal-script-history');
  try {
    const r = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/script-history`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'fail');
    const fmtTs = (ts) => ts ? new Date(ts * 1000).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : '?';
    const REASON_LABELS = {
      'doctor-script': '🩺 Доктор',
      'regenerate':    '⚡ Перегенерация',
      'pre-restore':   '↩ Перед откатом',
      'manual':        '✏ Ручная правка',
    };
    const renderItem = (label, ts, length, preview, actionsHtml) => `
      <div style="border:1px solid var(--border);border-radius:8px;padding:10px;background:var(--bg-secondary)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px">
          <div><b>${label}</b> <span style="opacity:0.7;font-size:0.85em">${fmtTs(ts)} · ${length} симв.</span></div>
          <div style="display:flex;gap:6px">${actionsHtml}</div>
        </div>
        <pre style="margin:0;font-size:0.78em;opacity:0.75;white-space:pre-wrap;max-height:80px;overflow:hidden">${(preview||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</pre>
      </div>
    `;
    let html = renderItem('🟢 Текущая (активная)', null, d.current.length, d.current.preview, '<span style="opacity:0.6;font-size:0.85em">в работе</span>');
    const versions = (d.versions || []).slice().reverse();
    if (versions.length === 0) {
      html += '<div style="opacity:0.6;text-align:center;padding:20px">История пуста — после первой регенерации или работы Доктора здесь появятся предыдущие версии.</div>';
    } else {
      for (const v of versions) {
        const label = REASON_LABELS[v.reason] || v.reason;
        const actions = `
          <button class="btn-ghost btn-sm" onclick="previewScriptVersion(${v.index})">👁 Открыть</button>
          <button class="btn-primary btn-sm" onclick="restoreScriptVersion(${v.index})">↩ Восстановить</button>
        `;
        html += renderItem(label, v.ts, v.length, v.preview, actions);
      }
    }
    list.innerHTML = html;
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">Ошибка: ${e.message}</div>`;
  }
}

async function previewScriptVersion(idx) {
  try {
    const r = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/script-history/${idx}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'fail');
    // Open in a new window/tab as plain text
    const w = window.open('', '_blank');
    if (!w) { alert('Браузер заблокировал popup. Разреши и попробуй ещё раз.'); return; }
    w.document.write(`<pre style="white-space:pre-wrap;font-family:ui-monospace,monospace;padding:20px;background:#111;color:#eee;margin:0">${d.script.replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</pre>`);
    w.document.title = `Версия эп.${S.episode.number} (${d.reason})`;
  } catch (e) {
    alert('Ошибка: ' + e.message);
  }
}

async function restoreScriptVersion(idx) {
  if (!confirm('Восстановить эту версию? Текущий сценарий будет сохранён в истории как "перед откатом" — откат можно отменить.')) return;
  try {
    const r = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/script-restore/${idx}`, { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'fail');
    document.getElementById('ep-script').value = d.script;
    document.getElementById('script-char-count').textContent = d.script.length;
    // Refresh episode state
    const epR = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}`);
    if (epR.ok) S.episode = await epR.json();
    closeModal('modal-script-history');
    alert(`✅ Версия восстановлена (${d.restored_from?.reason || '?'}).`);
  } catch (e) {
    alert('Ошибка: ' + e.message);
  }
}

// ── Scene & 15s-segment view ─────────────────────────────────────────────
const SCENE_COLORS = [
  'rgba(124,92,252,0.10)',  // purple
  'rgba(0,212,170,0.10)',   // teal
  'rgba(255,165,2,0.10)',   // orange
  'rgba(46,213,115,0.10)',  // green
  'rgba(255,71,87,0.10)',   // red
  'rgba(54,162,235,0.10)',  // blue
  'rgba(232,67,147,0.10)',  // pink
];
const SCENE_BORDERS = [
  '#7c5cfc', '#00d4aa', '#ffa502', '#2ed573', '#ff4757', '#36a2eb', '#e84393',
];

// Match SLUGLINE (location heading) — start of a new scene.
//   English: INT., EXT., INT./EXT., I/E.
//   Russian: ИНТ., ИНТА. (typo seen in scripts), ЭКСТ., ЭКС., НАТ., НАТУРА.,
//            ВНУТР., ИНТЕРЬЕР, ВНЕ, СНАРУЖИ
// `[\s*_#>]*` allows leading markdown decorators (**, __, #, >) before the cue
const SCENE_HEADING_RE = /^[\s*_#>]*(INT\.|EXT\.|INT\.?\s*\/\s*EXT\.?|I\/E\.|ИНТ\.|ИНТА\.|ЭКСТ\.|ЭКС\.|НАТ\.|НАТУРА\.|ВНУТР\.|ИНТЕРЬЕР|ВНЕ\.|СНАРУЖИ)\s+/i;

// Inferred scene heading: when the writer didn't bother with INT./EXT./ИНТ.
// — but the line still clearly opens a new scene. Three sub-patterns:
//   • "Локация: ..." or "LOCATION: ..." context preamble
//   • Numbered: "СЦЕНА 5", "Сцена 5.", "SCENE 12"
const SCENE_HEADING_INFER_RE = /^[\s*_#>]*(Локация\s*[:：]|Location\s*[:：]|СЦЕНА\s*\d|Сцена\s*\d|SCENE\s*\d)/i;

// Control / structural tokens that LOOK slug-ish but aren't scene starts.
const _SLUG_BLOCKLIST_RE = /^(REVERSAL|END|FIN|КОНЕЦ|TBD|TBC|БИТ|BIT|HOOK|TWIST|CLIFFHANGER|КЛИФФХЭНГЕР|РАЗВОРОТ|ПАУЗА|ТИШИНА|FLASHBACK|FLASH BACK|MONTAGE|МОНТАЖ|VOICE OVER|V\.O\.|O\.S\.)$/i;

// Standalone ALL-CAPS slug like "ДОМ АННЫ — НОЧЬ" or "OFFICE — DAY".
// Must look like a place/time tag: 5..80 chars, no lowercase letters, no colon.
function _isAllCapsSlug(t) {
  if (!t) return false;
  if (t.length < 5 || t.length > 80) return false;
  if (/[:：\[\]#]/.test(t)) return false;       // character cues, dialogue, brackets, markdown #
  if (/[a-zа-яё]/.test(t)) return false;        // any lowercase → not a slug
  if (!/[A-ZА-ЯЁ]/.test(t)) return false;       // need at least one letter
  if (/^(FADE|CUT|DISSOLVE|SMASH|MATCH)\b/i.test(t)) return false;
  if (_SLUG_BLOCKLIST_RE.test(t.replace(/[\.\—\-\s]+$/, ''))) return false;
  return true;
}

// Bracketed slug: "[КАФЕ — НОЧЬ]" — must be uppercase-only inside.
// Long mixed-case bracketed lines are action prose, not headings.
function _isBracketSlug(t) {
  const m = t.match(/^\[\s*([^\]]{3,80})\s*\]\s*$/);
  if (!m) return false;
  const inner = m[1].trim();
  if (/[a-zа-яё]/.test(inner)) return false;
  if (_SLUG_BLOCKLIST_RE.test(inner)) return false;
  if (/^(FADE|CUT|DISSOLVE|SMASH|MATCH)\b/i.test(inner)) return false;
  return true;
}

// Combined check: is this line some flavour of scene heading?
// Returns { match: bool, inferred: bool } so the renderer can flag inferred ones.
function _matchSceneHeading(t) {
  if (SCENE_HEADING_RE.test(t)) return { match: true, inferred: false };
  if (SCENE_HEADING_INFER_RE.test(t)) return { match: true, inferred: true };
  if (_isAllCapsSlug(t)) return { match: true, inferred: true };
  if (_isBracketSlug(t)) return { match: true, inferred: true };
  return { match: false, inferred: false };
}

// Lines we filter OUT entirely from scene/segment view (cast, notes, separators).
const _SCRIPT_SKIP_PATTERNS = [
  /^={3,}\s*EPISODE CAST/i,        // start of cast block (handled with toggle)
  /^={3,}\s*END CAST/i,
  /^EPISODE NOTES\b/i,
  /^━+/,
  /^Hook type:/i, /^Reversal type:/i, /^Cliffhanger type:/i,
  /^Escalation rung:/i, /^Spoken word count:/i,
  /^Estimated runtime:/i, /^Setup for next episode:/i,
];

// Heuristic per-line duration in seconds (only counts what's actually on screen).
// Per-line on-screen duration (seconds). The 15-sec segmentation packs lines
// into chunks based on these numbers, so what counts here = what eats the budget.
// RULE (calibrated to English short-drama TikTok pacing):
//   • Dialogue replicas are timed by SPEECH_WPS (~3.5 words/sec ≈ 210 wpm —
//     short-drama delivery speed; faster than conversational ~2.4 wps).
//   • Action lines (bracketed OR prose) cost a FIXED 1.5s regardless of length.
//   • Bracketed action notes INSIDE a dialogue line ('[hands letter to her]')
//     are NOT counted as spoken words — they're stage directions, not speech.
//   • Scene headings, transitions, separators: 0s.
const ACTION_BEAT_SEC = 1.5;
const SPEECH_WPS = 3.8;  // ~228 wpm — short-drama TikTok delivery (faster than conversational)

function _lineDuration(line) {
  let t = (line || '').trim();
  if (!t) return 0;
  // Strip leading line-numbering prefix: "1. ", "2) ", "12: ", "3 - " — common
  // when scripts come back with enumerated dialogue/action. Without this strip
  // the dialogue regex below fails (first char is a digit, not a letter) and
  // the line falls into prose-action with fixed 1.5s — drastically underestimating
  // long dialogues.
  t = t.replace(/^\d+[.\):\-—–]\s+/, '');
  if (_matchSceneHeading(t).match) return 0;
  if (/^[-—=]{3,}\s*$/.test(t)) return 0;
  if (/^\[REVERSAL\]\s*$/i.test(t)) return 0;
  if (/^[\s—-]*(FADE|CUT|DISSOLVE|SMASH|MATCH)\s+(IN|OUT|TO|BACK)\b/i.test(t)) return 0;
  // Beat-time headings: "[0:00 — 0:15] HOOK" / "[0:15 - 0:35] BUILD" — meta
  // markers not visible as on-screen content.
  if (/^\[\s*\d+:\d+\s*[—\-–]\s*\d+:\d+\s*\]/.test(t)) return 0;

  // Action line: bracketed prose `[Волк входит и...]`. Scale by length —
  // real action takes time proportional to what's described; a one-liner
  // ≈1.5s, a long sentence ≈4-6s. Cap so a paragraph doesn't blow a chunk.
  if (/^\[/.test(t) && /\]\s*$/.test(t)) {
    const inner = t.replace(/[\[\]]/g, '').trim();
    if (!inner) return 0;
    return _scaleActionDuration(inner);
  }

  // Dialogue line: "Name: (parens) [stage] actual text" — count by speech rate.
  // Accepts BOTH all-caps (AVA:, MAYA:) and Title-case (Adrian:, Clara:) — modern
  // short-drama scripts use Title-case for character cues, classic screenplay
  // format uses all-caps. Discriminator is the colon — prose lines don't have one.
  const m = t.match(/^([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-']{0,40})\s*[:：]\s*(.*)$/);
  // Reject only if the first letter is lowercase (e.g. random colon-prose like
  // "the question: who killed her?"). Either ALL-CAPS or Title-case both pass.
  if (m && /^[A-ZА-ЯЁ]/.test(m[1])) {
    let rest = m[2] || '';
    rest = rest.replace(/\([^)]*\)/g, ' ');   // (parenthetical tone notes) — not spoken
    rest = rest.replace(/\[[^\]]*\]/g, ' ');  // [stage actions inside dialogue] — not spoken
    rest = rest.replace(/\*[^*]*\*/g, ' ');   // *italic emphasis / inline action* — also not spoken
    rest = rest.trim();
    if (!rest) return 0.5;
    const words = rest.split(/\s+/).filter(Boolean).length;
    return 0.4 + words / SPEECH_WPS;          // tiny pre-pause + speech rate
  }

  // Standalone parenthetical: short emotional beat "(angry)" → 0.4s,
  // long action wrapped in parens "(Каэлен падает...)" → scale by length.
  const parenMatch = t.match(/^\((.+)\)\.?$/);
  if (parenMatch) {
    const inner = parenMatch[1].trim();
    if (inner.length <= 25) return 0.4;
    return _scaleActionDuration(inner);
  }

  // Plain prose action (no brackets) — scale by length.
  return _scaleActionDuration(t);
}

// Action duration heuristic: ~35 chars per visible second of footage,
// floor at ACTION_BEAT_SEC=1.5s, cap at 6s so a long paragraph doesn't
// take over an entire 15s segment by itself.
function _scaleActionDuration(text) {
  const chars = (text || '').length;
  return Math.max(ACTION_BEAT_SEC, Math.min(6, 1 + chars / 35));
}

// Find a chunk's [start, end] byte-offset in the script via long-line anchors.
// Mirrors the server-side logic in app.py compose endpoint.
function _findChunkRange(scriptText, chunkText) {
  if (!chunkText || !scriptText) return [-1, -1];
  const cands = [];
  for (const ln of chunkText.split('\n')) {
    const s = ln.trim();
    if (s.length < 25) continue;
    if (_matchSceneHeading(s).match) continue;
    cands.push(s);
    if (cands.length >= 6) break;
  }
  let start = -1;
  for (const c of cands) {
    const idx = scriptText.indexOf(c);
    if (idx === -1) continue;
    if (scriptText.indexOf(c, idx + 1) === -1) { start = idx; break; }
  }
  if (start < 0) {
    for (const c of cands) {
      const idx = scriptText.indexOf(c);
      if (idx !== -1) { start = idx; break; }
    }
  }
  if (start < 0) return [-1, -1];
  let end = start + chunkText.length;
  for (let i = chunkText.split('\n').length - 1; i >= 0; i--) {
    const s = chunkText.split('\n')[i].trim();
    if (s.length < 25) continue;
    if (_matchSceneHeading(s).match) continue;
    const idx = scriptText.indexOf(s, start);
    if (idx >= 0) { end = idx + s.length; break; }
  }
  return [start, end];
}

function _parseScriptScenes(scriptText, overrides) {
  const rawLines = (scriptText || '').split('\n');
  const overrideMap = new Map();
  for (const o of (overrides || [])) {
    if (o && o.anchor && (o.action === 'break' || o.action === 'merge')) {
      overrideMap.set(o.anchor, o.action);
    }
  }
  const scenes = [];
  let inCast = false;
  let inNotes = false;
  let cur = null;
  let runningOffset = 0;
  for (const rawLine of rawLines) {
    const lineStart = runningOffset;
    const lineEnd = runningOffset + rawLine.length;
    runningOffset = lineEnd + 1; // +1 for the \n we removed
    const t = rawLine.trim();
    // CAST block — skip entirely
    if (/^={3,}\s*EPISODE CAST/i.test(t)) { inCast = true;  continue; }
    if (/^={3,}\s*END CAST/i.test(t))     { inCast = false; continue; }
    if (inCast) continue;
    // Episode notes / trailer block — once we hit it, stop processing
    if (_SCRIPT_SKIP_PATTERNS.some(re => re.test(t))) {
      if (/^(EPISODE NOTES|━+|Hook type:|Reversal type:|Cliffhanger type:|Escalation rung:|Spoken word count:|Estimated runtime:|Setup for next episode:)/i.test(t)) {
        inNotes = true;
      }
      continue;
    }
    if (inNotes) continue;
    // Markdown headers (## ЭПИЗОД 4, ### Сцена и т.п.) — episode-level meta, not story content
    if (/^#+\s/.test(t)) continue;
    // Bold meta-labels — story-trailer / scene-trailer annotations the writer attaches:
    //   **КРАТКОЕ СОДЕРЖАНИЕ:** …, **SUMMARY:** …
    //   - **Cliffhanger:** "..."
    //   - **Emotional peak:** Vivian's admission of forcing Clara…
    //   - **Setup for Episode 5:** Legal confrontation begins…
    // Pattern: optional bullet (-, *, •) + **Label:** where Label can include letters, digits, spaces.
    // We do NOT include `.` or `—` in Label so scene headings like **INT. CAFE — DAY** stay safe.
    if (/^(?:[-*•]\s+)?\*\*[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9\s]*:\s*\*\*/.test(t)) continue;
    // Horizontal separators
    if (/^[-—=]{3,}\s*$/.test(t)) continue;

    // Scene heading: open a new scene (formal INT./EXT. or inferred)
    const headMatch = _matchSceneHeading(t);
    if (headMatch.match) {
      cur = { id: scenes.length, heading: t, lines: [], totalSec: 0, inferred: headMatch.inferred };
      scenes.push(cur);
      continue;
    }

    // Empty / structural lines aren't visual time AND not visible body either —
    // skip BEFORE we'd open a synthetic scene, otherwise blank lines between
    // CAST block and the first INT./EXT. would create an empty Сцена 1.
    if (!t) continue;

    // No heading seen yet AND we hit real content → open a synthetic Сцена 1
    // so the script doesn't disappear entirely from the scene view.
    if (!cur) {
      cur = { id: 0, heading: '', lines: [], totalSec: 0, inferred: true, synthetic: true };
      scenes.push(cur);
    }

    const dur = _lineDuration(rawLine);
    cur.lines.push({
      text: rawLine, duration: dur, segIdx: 0,
      offset: lineStart, offsetEnd: lineEnd,
    });
    cur.totalSec += dur;
  }

  // Assign segment indices based on REAL on-screen time. Each Seedance chunk is
  // technically 15s but we aim for ~13s of content + ~2s breathing room so cuts
  // don't jam reactions back-to-back.
  // TWO-PASS algorithm:
  //   Pass 1 (greedy): pack lines into the current segment until it would exceed
  //     SOFT_MAX. NEVER break before the segment has accumulated MIN_SEGMENT — a
  //     1-line 4-second segment wastes a whole 15s Seedance chunk.
  //   Pass 2 (merge tiny): post-walk segments. Any segment < MIN_SEGMENT gets
  //     merged into a neighbour if the combined size stays ≤ HARD_MAX.
  const TARGET = 11.0;          // aim around this (informational)
  const SOFT_MAX = 13.0;        // normal break threshold (2s buffer below 15s chunk)
  const MIN_SEGMENT_SEC = 5.0;  // smaller than this = wasted Seedance chunk
  const HARD_MAX_SEC = 14.5;    // absolute ceiling — Seedance chunk is 15s
  for (const sc of scenes) {
    // Pass 1: greedy, never break before MIN_SEGMENT
    let acc = 0, seg = 0;
    for (const l of sc.lines) {
      if (acc >= MIN_SEGMENT_SEC && acc + l.duration > SOFT_MAX) {
        seg += 1;
        acc = 0;
      }
      l.segIdx = seg;
      acc += l.duration;
    }
    // Pass 2: merge any tiny segments into a neighbour (prefer prev) up to HARD_MAX
    const segTotals = [];
    for (const l of sc.lines) {
      while (segTotals.length <= l.segIdx) segTotals.push(0);
      segTotals[l.segIdx] += l.duration;
    }
    // Walk backward so index shifts after splice don't break the iteration
    for (let i = segTotals.length - 1; i >= 0; i--) {
      if (segTotals[i] >= MIN_SEGMENT_SEC) continue;
      // Try merge with previous
      if (i > 0 && segTotals[i-1] + segTotals[i] <= HARD_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === i) l.segIdx = i - 1;
          else if (l.segIdx > i) l.segIdx -= 1;
        }
        segTotals[i-1] += segTotals[i];
        segTotals.splice(i, 1);
        continue;
      }
      // Else try merge with next
      if (i < segTotals.length - 1 && segTotals[i] + segTotals[i+1] <= HARD_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === i+1) l.segIdx = i;
          else if (l.segIdx > i+1) l.segIdx -= 1;
        }
        segTotals[i] += segTotals[i+1];
        segTotals.splice(i+1, 1);
        continue;
      }
      // Otherwise leave alone (single huge orphan line that can't fit anywhere)
    }
    sc.segCount = sc.lines.length ? (sc.lines[sc.lines.length - 1].segIdx + 1) : 0;
  }

  // Apply manual overrides (force-break / force-merge per line anchor) AFTER
  // auto-segmentation so user's choices win. Skipped lines (meta, headings)
  // never reach lines[] so they can't carry overrides — fine, those aren't
  // displayed segments anyway.
  if (overrideMap.size) {
    for (const sc of scenes) {
      // Snapshot auto-segmentation (transitions = points where auto wanted a break)
      for (const l of sc.lines) l._autoSeg = l.segIdx;
      let curSeg = 0;
      for (let i = 0; i < sc.lines.length; i++) {
        const l = sc.lines[i];
        const action = overrideMap.get(_lineAnchor(l.text));
        if (i === 0) {
          l.segIdx = 0;
          l._override = action || null;
          continue;
        }
        const prev = sc.lines[i - 1];
        if (action === 'break') {
          curSeg += 1;                                  // user forces split here
        } else if (action === 'merge') {
          // user forces no-break — stay with prev
        } else if (l._autoSeg !== prev._autoSeg) {
          curSeg += 1;                                  // respect auto-decision
        }
        l.segIdx = curSeg;
        l._override = action || null;
      }
      sc.segCount = sc.lines.length ? (sc.lines[sc.lines.length - 1].segIdx + 1) : 0;
    }
  }
  return scenes;
}

// Build coverage map: { lineOffset → {status, idx} } based on Seedance chunks.
function _buildSeedanceCoverage(scriptText, chunks) {
  const ranges = [];
  for (const c of chunks || []) {
    const [s, e] = _findChunkRange(scriptText, c.chunk_text || '');
    if (s >= 0) ranges.push({ start: s, end: e, status: c.status, idx: c.idx, video: !!c.video_path });
  }
  return ranges;
}
function _statusForLine(line, coverage) {
  // Return tightest covering range (latest start that contains the line)
  let best = null;
  for (const r of coverage) {
    if (r.start <= line.offset + 5 && r.end >= line.offsetEnd - 5) {
      if (!best || r.start > best.start) best = r;
    }
  }
  return best;
}

function _renderScenesHTML(scenes, coverage = []) {
  if (!scenes.length) {
    return '<div class="muted" style="padding:16px">Сценарий пустой.</div>';
  }
  const showCov   = !!SCENE_VIEW_STATE.showCoverage && coverage.length;
  const editMode  = !!SCENE_VIEW_STATE.editMode;
  const overrideCount = _segmentOverrides().length;
  let html = '';
  // Toolbar — always shown in scene view
  const stats = { completed: 0, pending: 0, failed: 0 };
  for (const r of coverage) {
    if (r.status === 'completed') stats.completed++;
    else if (r.status === 'failed') stats.failed++;
    else stats.pending++;
  }
  // Auto-mode state for the button label
  const autoActive = (typeof AUTO !== 'undefined') && AUTO.active;
  const autoErr    = (localStorage.getItem('auto_error_mode') || 'heal');  // 'heal' | 'stop'
  const autoMode   = (localStorage.getItem('auto_mode_kind')  || 'sequential'); // 'sequential' | 'turbo'
  const seqTip = 'ПОСЛЕДОВАТЕЛЬНО — каждый чанк ждёт предыдущий, ему передаётся last frame видео + кадры перед склейками для непрерывности. Дольше, но качество и консистентность мизансцены лучше. Галочки lastframe/cutframes в Seedance-панели игнорируются — режим всегда включён.';
  const turboTip = 'ТУРБО — все чанки уходят в очередь Seedance параллельно через единый batch-JSON эпизода (один Claude-вызов на весь эпизод). Использует scene blocking для общей геометрии локации. Быстрее в N раз, но без видео-continuity между чанками — возможны мелкие drift-ы.';
  html += `<div class="ep-scene-toolbar">
    <button id="auto-mode-btn" class="${autoActive ? 'btn-danger' : 'btn-accent'}" onclick="_autoModeToggle()"
      title="Запустить генерацию ВСЕЙ серии в Seedance — пройдёт по всем сегментам сценария и отдаст каждый чанк в Seedance. Режим (последовательно / турбо) выбирается справа.">
      ${autoActive
        ? '⏸ Стоп — остановить генерацию серии'
        : '🎬 Сгенерировать всю серию в Seedance'}
    </button>
    <span class="auto-mode-kind" title="Переключатель режима генерации. Наведи на ⓘ для подробностей.">
      <label class="auto-mode-radio recommended" title="${esc(seqTip)}">
        <input type="radio" name="auto-mode-kind" ${autoMode === 'sequential' ? 'checked' : ''} onchange="_autoSaveModeKind('sequential')">
        <span class="amr-text">🐢 Последовательно</span>
        <span class="amr-rec">★ рекомендуется</span>
      </label>
      <label class="auto-mode-radio" title="${esc(turboTip)}">
        <input type="radio" name="auto-mode-kind" ${autoMode === 'turbo' ? 'checked' : ''} onchange="_autoSaveModeKind('turbo')">
        <span class="amr-text">⚡ Турбо</span>
      </label>
    </span>
    <span class="auto-err-mode" title="Что делать если Seedance вернёт moderation error">
      <label><input type="radio" name="auto-err" id="auto-error-mode-heal" ${autoErr === 'heal' ? 'checked' : ''} onchange="_autoSaveErrMode('heal')"> 🩹 Авто-лечение</label>
      <label><input type="radio" name="auto-err" id="auto-error-mode-stop" ${autoErr === 'stop' ? 'checked' : ''} onchange="_autoSaveErrMode('stop')"> ⏹ Стоп + сигнал</label>
    </span>
    <span id="auto-status" class="auto-status muted"></span>
    <span class="ep-tb-separator"></span>
    <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer" title="Включает кнопки разделения/объединения сегментов. Выключи чтобы выделять текст не задевая UI.">
      <input type="checkbox" id="ep-edit-toggle" ${editMode ? 'checked' : ''} onchange="toggleSegmentEditMode(this.checked)">
      ✂ Ручная разбивка сегментов
    </label>
    ${overrideCount ? `<button class="btn-ghost btn-sm" onclick="clearSegmentOverrides()" title="Сбросить все ручные правки и пересчитать заново">Сбросить (×${overrideCount})</button>` : ''}
    ${coverage.length ? `
    <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;margin-left:14px">
      <input type="checkbox" id="ep-cov-toggle" ${showCov ? 'checked' : ''} onchange="toggleCoverage(this.checked)">
      Подсветить что уже сгенерено в Seedance
    </label>
    <span class="muted" style="font-size:0.78rem">
      · ${stats.completed} ✓  · ${stats.pending} ⏳  · ${stats.failed} ✗
    </span>` : ''}
  </div>`;
  scenes.forEach((sc, sIdx) => {
    const bg = SCENE_COLORS[sIdx % SCENE_COLORS.length];
    const bdr = SCENE_BORDERS[sIdx % SCENE_BORDERS.length];
    const heading = sc.heading || `Сцена ${sIdx + 1} (без заголовка)`;
    const inferredBadge = sc.inferred
      ? `<span class="ep-scene-inferred" title="Заголовок определён автоматически — INT./EXT. в сценарии не указан">auto</span>`
      : '';
    html += `<div class="ep-scene${editMode ? ' edit-mode' : ''}" style="background:${bg};border-left:4px solid ${bdr}">`;
    html += `<div class="ep-scene-header">
        <span class="ep-scene-tag">Сцена ${sIdx + 1}</span>
        <span class="ep-scene-loc">${esc(heading.slice(0, 120))}</span>
        ${inferredBadge}
        <span class="ep-scene-meta">~${sc.totalSec.toFixed(0)}с · ${sc.segCount} сегмент(ов) ×15с</span>
      </div>`;
    let lastSeg = -1;
    let segAcc = 0;
    sc.lines.forEach((l, lIdx) => {
      const startsNewSeg = (l.segIdx !== lastSeg);
      // Edit mode: BETWEEN two lines INSIDE same segment, render thin "split here" handle
      if (!startsNewSeg && editMode && lIdx > 0) {
        const anchor = _lineAnchor(l.text);
        const isOvr = l._override === 'break';
        const safeAnchor = anchor.replace(/'/g, "\\'");
        html += `<div class="ep-split-handle ${isOvr ? 'ovr' : ''}"
          onclick="toggleSegmentBreak('${safeAnchor}')"
          title="${isOvr ? 'Убрать ручной разрыв здесь' : 'Разделить сегмент перед этой строкой'}"
        >${isOvr ? '✓ разрыв здесь — клик чтобы убрать' : '✂ разделить здесь'}</div>`;
      }
      if (startsNewSeg) {
        if (lastSeg !== -1) html += `</div></div>`; // close prev seg-body + ep-seg
        lastSeg = l.segIdx;
        segAcc = 0;
        // Compute this segment's total seconds for the toolbar summary
        const segLines = sc.lines.filter(x => x.segIdx === l.segIdx);
        const segTotal = segLines.reduce((s, x) => s + x.duration, 0);
        const overflowWarn = segTotal > 14.5
          ? `<span class="ep-seg-warn" title="Содержимое выходит за 15-сек лимит Seedance">${segTotal.toFixed(1)}с ⚠</span>`
          : `<span class="ep-seg-dur" title="Расчётная длительность сегмента">${segTotal.toFixed(1)}с</span>`;
        // First-line anchor of this segment = target for merge + auto-skip identity
        const firstAnchor = _lineAnchor(l.text);
        const safeFirstAnchor = firstAnchor.replace(/'/g, "\\'");
        const isMergedHere = l._override === 'merge';
        const isAutoSkipped = _isSegmentAutoSkipped(firstAnchor);
        const editBtns = editMode && lIdx > 0
          ? `<button class="ep-seg-mini" onclick="toggleSegmentMerge('${safeFirstAnchor}')"
              title="${isMergedHere ? 'Восстановить разделение' : 'Объединить с предыдущим сегментом'}"
            >${isMergedHere ? '↩ разъединить' : '🔗 ↑ объединить'}</button>`
          : '';
        const autoCb = `<label class="ep-seg-auto-cb" title="${isAutoSkipped ? 'Сегмент пропускается в Auto-mode — клик включит обратно' : 'Сегмент будет сгенерён при запуске Auto-mode — клик исключит его'}">
            <input type="checkbox" ${isAutoSkipped ? '' : 'checked'} onchange="toggleSegmentAutoInclude('${safeFirstAnchor}')">
            <span>auto</span>
          </label>`;
        html += `<div class="ep-seg${isAutoSkipped ? ' auto-skipped' : ''}" data-seg="${l.segIdx + 1}">
          <div class="ep-seg-bracket" title="Seedance-сегмент ${l.segIdx + 1}">${l.segIdx + 1}</div>
          <div class="ep-seg-body">
            <div class="ep-seg-toolbar">
              <button class="ep-seg-send" onclick="sendSceneSegmentToSeedance(${sIdx}, ${l.segIdx})"
                title="Скопировать сегмент в Seedance compose и прокрутить вниз">
                🎬 в Сиданс
              </button>
              ${autoCb}
              ${overflowWarn}
              ${editBtns}
            </div>`;
      }
      segAcc += l.duration;
      let _cls = 'ep-line';
      let _tag = '';
      if (showCov) {
        const cov = _statusForLine(l, coverage);
        if (cov) {
          _cls += cov.status === 'completed' ? ' sd-done' : cov.status === 'failed' ? ' sd-failed' : ' sd-pending';
          const ic = cov.status === 'completed' ? '✓' : cov.status === 'failed' ? '✗' : '⏳';
          _tag = `<span class="ep-line-tag" title="Seedance #${cov.idx} · ${cov.status}">${ic} #${cov.idx}</span>`;
        }
      }
      if (l._override === 'break') _cls += ' ovr-break';
      else if (l._override === 'merge') _cls += ' ovr-merge';
      // Per-line close-up toggle for dialogue lines (always visible — small icon
      // on the left, doesn't interfere with text selection unless clicked).
      const isDialogue = _isDialogueLine(l.text);
      const isCloseUp = isDialogue && _isLineCloseUp(l.text);
      if (isCloseUp) _cls += ' line-close-up';
      const closeUpBtn = isDialogue
        ? `<button class="ep-line-cu-btn ${isCloseUp ? 'active' : ''}"
            onclick="toggleLineCloseUp('${_lineAnchor(l.text).replace(/'/g, "\\'")}')"
            title="${isCloseUp ? 'Убрать метку close-up' : 'Пометить эту реплику как close-up — спикер один в кадре крупным планом, фон размыт'}"
          >${isCloseUp ? '🎯' : '○'}</button>`
        : '';
      html += `<div class="${_cls}">${closeUpBtn}${_tag}${esc(l.text || ' ')}</div>`;
    });
    if (lastSeg !== -1) html += `</div></div>`;
    html += `</div>`;
  });
  return html;
}

function toggleSceneView() {
  const ta = document.getElementById('ep-script');
  const view = document.getElementById('ep-script-scenes');
  const btn = document.getElementById('ep-scenes-btn');
  if (!ta || !view || !btn) return;
  const isOpen = !view.classList.contains('hidden');
  if (isOpen) {
    view.classList.add('hidden');
    ta.classList.remove('hidden');
    btn.classList.remove('active');
    btn.innerHTML = '🎬 Сцены';
  } else {
    try {
      const saved = localStorage.getItem('sceneCov');
      if (saved !== null) SCENE_VIEW_STATE.showCoverage = (saved === '1');
    } catch {}
    _renderSceneViewBody();
    view.classList.remove('hidden');
    ta.classList.add('hidden');
    btn.classList.add('active');
    btn.innerHTML = '✏ Редактировать';
  }
}

const SCENE_VIEW_STATE = { showCoverage: true, editMode: false };

// Manual segment overrides per-episode. Loaded from S.episode.segment_overrides
// at scene-view open time, mutated by user click, persisted via PUT.
// Each override: {anchor: "first ~60 chars of trimmed line", action: "break"|"merge"}
//   - "break"  → that line MUST start a new segment (split before it)
//   - "merge"  → that line MUST stay with the previous segment (no break)
function _segmentOverrides() {
  return (S.episode && S.episode.segment_overrides) || [];
}

// ── Per-segment auto-mode skip flags ───────────────────────────────────────
function _segmentAutoSkips() {
  return (S.episode && S.episode.segment_auto_skips) || [];
}
function _isSegmentAutoSkipped(anchor) {
  return _segmentAutoSkips().includes(anchor);
}
async function _persistAutoSkips(skips) {
  if (!S.episode) return;
  S.episode.segment_auto_skips = skips;
  try {
    await api.put(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/segment-auto-skips`,
      { skips }
    );
  } catch (e) {
    console.warn('save segment auto-skips failed', e);
    showToast('⚠ Не удалось сохранить флаг auto-mode', 4000);
  }
}
async function toggleSegmentAutoInclude(anchor) {
  if (!anchor) return;
  const skips = _segmentAutoSkips().slice();
  const idx = skips.indexOf(anchor);
  if (idx >= 0) skips.splice(idx, 1);  // include — remove from skip list
  else skips.push(anchor);              // skip — add to skip list
  await _persistAutoSkips(skips);
  _renderSceneViewBody();
}

// ── Per-line overrides (currently: close-up flag) ──────────────────────────
function _lineOverrides() {
  return (S.episode && S.episode.line_overrides) || [];
}
function _isLineCloseUp(text) {
  const anchor = _lineAnchor(text);
  if (!anchor) return false;
  const found = _lineOverrides().find(o => o.anchor === anchor);
  return !!(found && (found.flags || []).includes('close_up'));
}
function _isDialogueLine(text) {
  if (!text) return false;
  const t = text.trim();
  // Same dialogue regex as _lineDuration — Title-case OR ALL-CAPS speaker name + colon
  if (!/^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-']{0,40}\s*[:：]\s/.test(t)) return false;
  return /^[A-ZА-ЯЁ]/.test(t);  // first letter must be uppercase
}
async function _persistLineOverrides(overrides) {
  if (!S.episode) return;
  S.episode.line_overrides = overrides;
  try {
    await api.put(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/line-overrides`,
      { overrides }
    );
  } catch (e) {
    console.warn('save line overrides failed', e);
    showToast('⚠ Не удалось сохранить close-up метку', 4000);
  }
}
async function toggleLineCloseUp(anchor) {
  if (!anchor) return;
  const overrides = _lineOverrides().slice();
  const idx = overrides.findIndex(o => o.anchor === anchor);
  if (idx < 0) {
    overrides.push({ anchor, flags: ['close_up'] });
  } else {
    const flags = overrides[idx].flags || [];
    const cuPos = flags.indexOf('close_up');
    if (cuPos >= 0) {
      const newFlags = flags.filter(f => f !== 'close_up');
      if (newFlags.length === 0) overrides.splice(idx, 1);
      else overrides[idx] = { ...overrides[idx], flags: newFlags };
    } else {
      overrides[idx] = { ...overrides[idx], flags: [...flags, 'close_up'] };
    }
  }
  await _persistLineOverrides(overrides);
  _renderSceneViewBody();
}
async function _persistSegmentOverrides(overrides) {
  if (!S.episode) return;
  S.episode.segment_overrides = overrides;
  try {
    await api.put(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/segment-overrides`,
      { overrides }
    );
  } catch (e) {
    console.warn('save segment overrides failed', e);
    showToast('⚠ Не удалось сохранить ручную разбивку', 4000);
  }
}
function _lineAnchor(text) {
  // Stable enough anchor for a script line: first 60 chars of trimmed text.
  // If user edits the line text, the override silently drops.
  return (text || '').trim().slice(0, 60);
}

function _renderSceneViewBody() {
  const ta = document.getElementById('ep-script');
  const view = document.getElementById('ep-script-scenes');
  if (!ta || !view) return;
  const scriptText = ta.value || '';
  const scenes = _parseScriptScenes(scriptText, _segmentOverrides());
  const chunks = (S.episode && S.episode.seedance_chunks) || [];
  const coverage = _buildSeedanceCoverage(scriptText, chunks);
  view.innerHTML = _renderScenesHTML(scenes, coverage);
}

function toggleCoverage(on) {
  SCENE_VIEW_STATE.showCoverage = !!on;
  try { localStorage.setItem('sceneCov', SCENE_VIEW_STATE.showCoverage ? '1' : '0'); } catch {}
  _renderSceneViewBody();
}

function toggleSegmentEditMode(on) {
  SCENE_VIEW_STATE.editMode = !!on;
  _renderSceneViewBody();
}

// Compose segment text from auto-collected lines, then send to Seedance compose
// textarea + scroll the compose panel into view + focus.
function sendSegmentToSeedance(segText) {
  const ta = document.getElementById('sd-chunk-text');
  if (!ta) {
    showToast('⚠ Seedance-панель не найдена на этом эпизоде', 4000);
    return;
  }
  ta.value = segText;
  // Trigger any onchange/oninput hooks listening on the textarea
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  document.getElementById('seedance-panel')?.scrollIntoView({behavior:'smooth', block:'start'});
  setTimeout(() => ta.focus(), 350);
  showToast('✓ Сегмент отправлен в Сиданс — нажми "⚙ Скомпоновать (LLM)"', 4000);
}

// Internal: gather plain text for the Nth segment of the Mth scene.
function _segmentText(sceneIdx, segIdx) {
  const ta = document.getElementById('ep-script');
  if (!ta) return '';
  const scenes = _parseScriptScenes(ta.value || '', _segmentOverrides());
  const sc = scenes[sceneIdx];
  if (!sc) return '';
  const lines = sc.lines.filter(l => l.segIdx === segIdx);
  if (!lines.length) return '';
  // Prepend scene heading so composer has context for INT/EXT
  const head = sc.heading ? sc.heading + '\n\n' : '';
  return head + lines.map(l => l.text).join('\n');
}

// User clicked "send segment N of scene M to Seedance"
function sendSceneSegmentToSeedance(sceneIdx, segIdx) {
  const txt = _segmentText(sceneIdx, segIdx);
  if (!txt.trim()) {
    showToast('⚠ Сегмент пустой', 3000);
    return;
  }
  // If any line in this segment carries the close-up flag, auto-tick the
  // global "🎯 close-up only" checkbox so the next compose enforces it.
  const ta = document.getElementById('ep-script');
  let segHasCloseUp = false;
  if (ta) {
    const scenes = _parseScriptScenes(ta.value || '', _segmentOverrides());
    const sc = scenes[sceneIdx];
    if (sc) {
      segHasCloseUp = sc.lines.some(l => l.segIdx === segIdx && _isLineCloseUp(l.text));
    }
  }
  const cuCb = document.getElementById('sd-close-up-only');
  if (segHasCloseUp && cuCb && !cuCb.checked) {
    cuCb.checked = true;
    sdSavePrefs();
    showToast('🎯 Сегмент содержит close-up метки — close-up only автоматически включён в Seedance-панели', 5500);
  }
  sendSegmentToSeedance(txt);
}

// Toggle "split" override on a specific line anchor within a scene.
async function toggleSegmentBreak(anchor) {
  if (!anchor) return;
  const overrides = _segmentOverrides().slice();
  const idx = overrides.findIndex(o => o.anchor === anchor);
  if (idx < 0) {
    overrides.push({ anchor, action: 'break' });
  } else if (overrides[idx].action === 'break') {
    overrides.splice(idx, 1);                      // toggle off
  } else {
    overrides[idx] = { anchor, action: 'break' };  // override merge → break
  }
  await _persistSegmentOverrides(overrides);
  _renderSceneViewBody();
}

async function toggleSegmentMerge(anchor) {
  if (!anchor) return;
  const overrides = _segmentOverrides().slice();
  const idx = overrides.findIndex(o => o.anchor === anchor);
  if (idx < 0) {
    overrides.push({ anchor, action: 'merge' });
  } else if (overrides[idx].action === 'merge') {
    overrides.splice(idx, 1);                      // toggle off
  } else {
    overrides[idx] = { anchor, action: 'merge' };  // override break → merge
  }
  await _persistSegmentOverrides(overrides);
  _renderSceneViewBody();
}

async function openBatchJsonViewer() {
  if (!S.episode) { showToast('⚠ Сначала открой эпизод', 3000); return; }
  // Restore sounds-checkbox state from localStorage and wire persistence
  const _bjSoundsCb = document.getElementById('batch-json-sounds');
  if (_bjSoundsCb && !_bjSoundsCb.dataset.wired) {
    const saved = localStorage.getItem('batch_json_sounds');
    _bjSoundsCb.checked = saved === null ? true : saved === '1';
    _bjSoundsCb.addEventListener('change', () => {
      localStorage.setItem('batch_json_sounds', _bjSoundsCb.checked ? '1' : '0');
    });
    _bjSoundsCb.dataset.wired = '1';
  }
  // Re-fetch episode to get latest batch_prompts (may have been built in another session)
  let ep;
  try {
    ep = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
    S.episode = ep;
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000); return;
  }

  const promptsMap = ep.batch_prompts || {};
  const anchors = Object.keys(promptsMap);
  const meta = document.getElementById('batch-json-meta');
  const ta   = document.getElementById('batch-json-text');

  // Compute current script hash to compare
  const currentScript = (ep.script || '');
  const computeHash = async (text) => {
    if (!window.crypto?.subtle) return null;
    const buf = new TextEncoder().encode(text);
    const hash = await crypto.subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
  };
  const curHash = await computeHash(currentScript);
  const savedHash = ep.batch_script_hash || '';
  const isStale = savedHash && curHash && savedHash !== curHash;

  if (!anchors.length) {
    meta.innerHTML = `<span style="color:var(--warning)">⚠ Batch-JSON ещё не построен.</span>
      Запусти Auto-mode параллельно (lastframe / cut-frames OFF) — batch-compose построится автоматически.
      Или нажми "🔄 Пересобрать" если хочешь сделать это вручную.`;
    ta.value = '';
    openModal('modal-batch-json');
    return;
  }

  const builtAt = ep.batch_built_at ? new Date(ep.batch_built_at).toLocaleString('ru-RU') : '?';
  const staleNote = isStale
    ? `<span style="color:var(--warning)">⚠ STALE: сценарий менялся после батча (hash ${savedHash} → ${curHash}). Пересобери.</span>`
    : `<span style="color:var(--success)">✓ Актуален (script_hash ${savedHash || '—'})</span>`;

  // Compute current segments from frontend parser to compare with batch coverage
  const currentSegs = (typeof _autoCollectSegments === 'function') ? _autoCollectSegments() : [];
  const expectedCount = currentSegs.length;
  const coverage = expectedCount > 0
    ? `${anchors.length}/${expectedCount}`
    : `${anchors.length}`;
  const coverageWarn = expectedCount > 0 && anchors.length < expectedCount
    ? `<span style="color:var(--warning)"> ⚠ ${expectedCount - anchors.length} сегмент(ов) не покрыто — Пересобери</span>`
    : '';
  // Check fallback markers
  const viaFallback = Object.values(promptsMap).filter(p => p._via_fallback).length;
  const fallbackNote = viaFallback > 0
    ? `<br><span style="color:var(--muted)">📦 ${viaFallback} сегмент(ов) добраны через per-chunk fallback (Claude в batch'е их пропустил)</span>`
    : '';
  meta.innerHTML = `
    <strong>Сегментов:</strong> ${coverage}${coverageWarn} ·
    <strong>Built:</strong> ${builtAt} ·
    ${staleNote}
    ${ep.batch_episode_blocking ? `<br><strong>episodeBlocking words:</strong> ${ep.batch_episode_blocking.split(/\s+/).filter(Boolean).length}` : ''}
    ${fallbackNote}
  `;

  // Construct readable JSON: sort segments by sceneIdx → segIdx
  const segArr = anchors.map(a => ({ anchor: a, ...promptsMap[a] }));
  segArr.sort((a, b) => (a.sceneIdx - b.sceneIdx) || (a.segIdx - b.segIdx));
  const obj = {
    episodeBlocking: ep.batch_episode_blocking || '',
    script_hash: ep.batch_script_hash || '',
    built_at: ep.batch_built_at || '',
    segments: segArr,
  };
  ta.value = JSON.stringify(obj, null, 2);
  openModal('modal-batch-json');
}

async function copyBatchJson() {
  const ta = document.getElementById('batch-json-text');
  const text = ta?.value || '';
  if (!text) { showToast('⚠ JSON пустой', 2500); return; }
  try {
    await navigator.clipboard.writeText(text);
    showToast('✓ Скопировано', 1800);
  } catch {
    ta.select(); document.execCommand('copy');
    showToast('✓ Скопировано', 1800);
  }
}

async function rebuildBatchPrompts() {
  if (!S.episode) { showToast('⚠ Сначала открой эпизод', 3000); return; }
  // Need segments from the parsed scene-view
  const ta = document.getElementById('ep-script');
  if (!ta) return;
  // batch-compose feeds Turbo auto-mode → establishing shots forced ON.
  const segs = (typeof _autoCollectSegments === 'function') ? _autoCollectSegments({ forceEstablishing: true }) : [];
  if (!segs.length) {
    showToast('⚠ Нет сегментов — открой "🎬 Сцены" чтобы сценарий разбился', 4000);
    return;
  }
  const useStyle = !!document.getElementById('sd-use-style')?.checked;
  const styleVal = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly = !!document.getElementById('sd-base-only')?.checked;

  // Locate UI elements for proper loading state
  const meta = document.getElementById('batch-json-meta');
  const jsonTa = document.getElementById('batch-json-text');
  const rebuildBtn = Array.from(document.querySelectorAll('#modal-batch-json button'))
    .find(b => /Пересобрать/.test(b.textContent));

  // Visual loading state — all three areas (button, meta, textarea)
  if (rebuildBtn) {
    rebuildBtn.disabled = true;
    rebuildBtn.dataset.origText = rebuildBtn.innerHTML;
    rebuildBtn.innerHTML = '<span class="spinner"></span> Пересобираю...';
  }
  const startedAt = Date.now();
  let elapsedTimer = null;
  const updateMeta = () => {
    if (!meta) return;
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    meta.innerHTML = `<span class="spinner"></span>
      <strong>batch-compose работает...</strong>
      &nbsp;<span style="color:var(--muted)">прошло ${elapsed}с</span>
      &nbsp;<span style="color:var(--muted)">· сегментов в очереди: ${segs.length}</span>
      &nbsp;<span style="color:var(--muted)">· один Claude call, обычно 15-30с</span>`;
  };
  updateMeta();
  elapsedTimer = setInterval(updateMeta, 1000);
  if (jsonTa) {
    jsonTa.dataset.origValue = jsonTa.value;
    jsonTa.style.opacity = '0.35';
    jsonTa.style.filter = 'blur(0.5px)';
    jsonTa.value = '⏳ Пересборка идёт... старый JSON будет заменён через ~15-30с.\n\nClaude получает: полный сценарий + roster персонажей + список ' + segs.length + ' сегментов\n + текущий scene_blocking из textarea выше.\n\nВ ответе: episodeBlocking + per-segment prompt+refs для всех сегментов разом.';
  }
  showToast('🧠 batch-compose: один Claude call, ~15-30с — жди завершения', 5000);

  try {
    await saveEpisodeSilent();
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/seedance/batch-compose`,
      {
        segments: segs.map(s => ({
          anchor: s.anchor, sceneIdx: s.sceneIdx, segIdx: s.segIdx,
          text: s.text, has_close_up: !!s.has_close_up, durationSec: s.durationSec,
          establishing_shot: !!s.establishing_shot,
        })),
        base_outfits_only: baseOnly,
        style: useStyle ? styleVal : '',
      },
      { timeoutMs: 900_000 }
    );
    const tookS = Math.round((Date.now() - startedAt) / 1000);
    showToast(`✓ Batch пересобран за ${tookS}с · ${res.count} сегментов`, 4000);
    if (document.getElementById('batch-json-sounds')?.checked) {
      try { Sounds.playSuccess(); } catch (e) {}
    }
    if (jsonTa) {
      jsonTa.style.opacity = '';
      jsonTa.style.filter = '';
    }
    await openBatchJsonViewer();   // reload (this also resets meta to fresh state)
  } catch (e) {
    showToast('✗ ' + (e.message || e), 8000);
    if (document.getElementById('batch-json-sounds')?.checked) {
      try { Sounds.playError(); } catch (err) {}
    }
    if (meta) meta.innerHTML = `<span style="color:var(--danger)">✗ ${e.message || e}</span>`;
    if (jsonTa) {
      jsonTa.style.opacity = '';
      jsonTa.style.filter = '';
      if (jsonTa.dataset.origValue !== undefined) jsonTa.value = jsonTa.dataset.origValue;
    }
  } finally {
    if (elapsedTimer) clearInterval(elapsedTimer);
    if (rebuildBtn) {
      rebuildBtn.disabled = false;
      rebuildBtn.innerHTML = rebuildBtn.dataset.origText || '🔄 Пересобрать';
    }
  }
}

async function generateSceneBlocking() {
  if (!S.episode) { showToast('⚠ Сначала открой эпизод', 3000); return; }
  const script = (val('ep-script') || '').trim();
  if (!script) { showToast('⚠ Сценарий пустой — заполни сначала', 3000); return; }
  const btn = document.getElementById('ep-gen-blocking-btn');
  const status = document.getElementById('ep-blocking-status');
  const ta = document.getElementById('ep-scene-blocking');
  if (!btn || !ta) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  if (status) status.textContent = '⚙ Claude разбирает сцену...';
  try {
    // Save script first so server reads latest
    await saveEpisodeSilent();
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/generate-scene-blocking`,
      {}
    );
    ta.value = res.blocking || '';
    if (S.episode) S.episode.scene_blocking = res.blocking || '';
    if (status) status.textContent = `✓ Готово · ${(res.blocking || '').split(/\s+/).length} слов`;
    showToast('✓ Blocking сгенерирован');
  } catch (e) {
    if (status) status.textContent = '✗ ' + (e.message || e);
    showToast('✗ ' + (e.message || e), 6000);
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

async function clearSegmentOverrides() {
  if (!_segmentOverrides().length) {
    showToast('Ручных правок и так нет', 2000);
    return;
  }
  if (!confirm('Сбросить все ручные правки разбивки? Сегменты пересчитаются автоматически.')) return;
  await _persistSegmentOverrides([]);
  _renderSceneViewBody();
  showToast('✓ Ручная разбивка сброшена', 2500);
}

// ════════════════════════════════════════════════════════════════════════════
// AUTO-MODE — sequentially (or parallel) run compose+start for every segment
// in the episode, honoring use_prev_lastframe / use_prev_cutframes toggles.
// On moderation failure: either auto-heal+retry or stop with error sound, per
// user preference (radio in scene toolbar).
// ════════════════════════════════════════════════════════════════════════════
const AUTO = {
  active: false,
  cancelRequested: false,
  segments: [],     // [{sceneIdx, segIdx, text}]
  cursor: 0,
  total: 0,
  parallel: false,
  errorMode: 'heal',
  lastStatus: '',
};

function _autoSaveErrMode(mode) {
  if (mode !== 'heal' && mode !== 'stop') return;
  localStorage.setItem('auto_error_mode', mode);
}

// Auto-mode kind: sequential (default, stable continuity) or turbo (parallel
// batch). Persisted in localStorage; UI hides scene-blocking section in
// sequential mode (it's irrelevant — each chunk gets its own continuity from
// last-frame + cut-frames, no global blocking needed).
function _autoSaveModeKind(kind) {
  if (kind !== 'sequential' && kind !== 'turbo') return;
  localStorage.setItem('auto_mode_kind', kind);
  _applyAutoModeUI();
}
function _autoGetModeKind() {
  return localStorage.getItem('auto_mode_kind') || 'sequential';
}
function _applyAutoModeUI() {
  const kind = _autoGetModeKind();
  const sec = document.getElementById('scene-blocking-section');
  if (sec) sec.style.display = (kind === 'turbo') ? '' : 'none';
}
// Run on episode-view render so visibility matches saved choice
document.addEventListener('DOMContentLoaded', _applyAutoModeUI);

function _autoCollectSegments(opts = {}) {
  const ta = document.getElementById('ep-script');
  if (!ta) return [];
  const scenes = _parseScriptScenes(ta.value || '', _segmentOverrides());
  // Establishing shot is FORCED ON when called from auto-mode (both Sequential
  // and Turbo) — sets a 2s wide-shot of the location at the start of every
  // new scene, regardless of the manual checkbox in the Seedance panel.
  // For other callers (manual segment preview), respect the checkbox.
  const establishing = opts.forceEstablishing
    ? true
    : !!document.getElementById('sd-establishing-shot')?.checked;
  const out = [];
  scenes.forEach((sc, sIdx) => {
    for (let g = 0; g < sc.segCount; g++) {
      const lines = sc.lines.filter(l => l.segIdx === g);
      if (!lines.length) continue;
      const head = sc.heading ? sc.heading + '\n\n' : '';
      const text = head + lines.map(l => l.text).join('\n');
      const hasCloseUp = lines.some(l => _isLineCloseUp(l.text));
      const anchor = _lineAnchor(lines[0].text);
      // Establishing shot: first seg of each new scene gets a 2s wide-shot
      // facade pre-roll merged into its action timeline. Toggle adds +2s to
      // durationSec (clamped to 15) so the dialogue still fits.
      const isFirstOfScene = (g === 0);
      const wantsEstablishing = establishing && isFirstOfScene;
      const contentSec = lines.reduce((s, l) => s + (l.duration || 0), 0);
      const targetSec = Math.ceil(contentSec + 1.5) + (wantsEstablishing ? 2 : 0);
      const durationSec = Math.max(5, Math.min(15, targetSec));
      out.push({
        sceneIdx: sIdx, segIdx: g, text, anchor,
        has_close_up: hasCloseUp, durationSec,
        establishing_shot: !!wantsEstablishing,
      });
    }
  });
  return out;
}

function _autoUpdateStatusUI() {
  const el = document.getElementById('auto-status');
  const btn = document.getElementById('auto-mode-btn');
  if (btn) {
    btn.innerHTML = AUTO.active ? '⏸ Стоп Auto-mode' : '▶ Auto-mode';
    btn.className = AUTO.active ? 'btn-danger' : 'btn-accent';
  }
  if (!el) return;
  if (!AUTO.active) {
    el.textContent = (AUTO.completedCount || AUTO.cursor) > 0 && AUTO.total > 0
      ? `Завершено: ${AUTO.completedCount || AUTO.cursor}/${AUTO.total}`
      : '';
    return;
  }
  const done = AUTO.completedCount || 0;
  const status = AUTO.lastStatus || '...';
  let mode;
  if (AUTO.parallel) mode = 'паралл.';
  else if ((AUTO.activeChains || 0) > 1) mode = `сцены × ${AUTO.activeChains}`;
  else mode = 'последов.';
  el.textContent = `Auto-mode (${mode}) · ${done}/${AUTO.total} · ${status}`;
}

function _autoModeToggle() {
  if (AUTO.active) stopAutoMode();
  else startAutoMode();
}

function stopAutoMode() {
  if (!AUTO.active) return;
  AUTO.cancelRequested = true;
  showToast('⏸ Auto-mode остановится после текущего шага...', 3000);
  _autoUpdateStatusUI();
}

async function startAutoMode() {
  if (AUTO.active) {
    showToast('Auto-mode уже активен');
    return;
  }
  if (!S.episode) {
    showToast('⚠ Сначала открой эпизод');
    return;
  }
  // Pre-flight: warn if any active char/loc lacks ref before kicking off N
  // chunks of generation. User often regrets discovering missing assets only
  // after burning compute on chunks with placeholder/random faces.
  if (!await _confirmMissingAssetsBeforeGen('Auto-mode (видео-генерацию)')) return;
  // Auto-mode hard requirement: duration MUST be 15s. The whole segmentation
  // logic (TARGET=12s, SOFT_MAX=13s, MIN=5s) is calibrated assuming 15s
  // Seedance chunks. If user picked 5/10s clips, segments won't fit and the
  // whole batch will be off-rhythm. Offer to auto-fix or cancel.
  const durEl = document.getElementById('sd-duration');
  const curDur = parseInt(durEl?.value, 10);
  if (curDur !== 15) {
    const confirmFix = confirm(
      `⚠ Длительность Seedance стоит ${curDur || '?'}с, но Auto-mode калиброван под 15-секундные чанки.\n\n` +
      `Сегментация рассчитывала контент ≤13с с 2с буфером — короткие чанки порежут реплики, ` +
      `длинные дадут пустоту в конце.\n\n` +
      `Поставить 15с автоматически и продолжить?\n` +
      `OK — да, ставлю 15с и запускаю.\n` +
      `Cancel — отменить, поставлю сам.`
    );
    if (!confirmFix) return;
    if (durEl) {
      durEl.value = '15';
      durEl.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  // Auto-mode behaviour is now driven by the mode toggle in the toolbar
  // (not by lastframe/cutframes checkboxes which are for manual one-shot use).
  //   sequential — last-frame + cut-frames ALWAYS on, chunks render serially
  //                with full video continuity. Best quality, slowest.
  //   turbo      — all chunks fire in parallel via single batch-compose,
  //                no continuity frames, relies on scene blocking + per-segment
  //                prompt for spatial consistency. Faster, less consistent.
  const autoModeKind = _autoGetModeKind();
  const isTurbo  = autoModeKind === 'turbo';
  const useLastframe = !isTurbo;        // sequential = always true
  const useCutframes = !isTurbo;        // sequential = always true
  const useStyle     = !!document.getElementById('sd-use-style')?.checked;
  const styleVal     = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly     = !!document.getElementById('sd-base-only')?.checked;
  const closeUpOnly  = !!document.getElementById('sd-close-up-only')?.checked;
  AUTO.parallel = isTurbo;

  // Turbo prerequisites: episodeBlocking + batch_prompts must exist.
  // If empty — auto-fill them before starting the parallel run, so the user
  // doesn't have to hit two extra buttons every time.
  if (isTurbo) {
    const blockingEl = document.getElementById('ep-scene-blocking');
    const blocking = (blockingEl?.value || '').trim();
    const hasBatchPrompts = !!(S.episode?.batch_prompts && Object.keys(S.episode.batch_prompts).length);
    if (!blocking) {
      showToast('⚙ Турбо-режим: сначала генерю scene blocking...', 4000);
      try {
        if (typeof generateSceneBlocking === 'function') await generateSceneBlocking();
      } catch (e) {
        showToast('✗ Не удалось сгенерить blocking: ' + (e.message || e), 6000);
        return;
      }
    }
    if (!hasBatchPrompts) {
      showToast('⚙ Турбо-режим: собираю batch JSON эпизода...', 4000);
      try {
        if (typeof rebuildBatchPrompts === 'function') await rebuildBatchPrompts();
      } catch (e) {
        showToast('✗ Не удалось собрать batch JSON: ' + (e.message || e), 6000);
        return;
      }
    }
  }
  AUTO.errorMode = (localStorage.getItem('auto_error_mode') || 'heal');
  // Both auto-mode flavors force establishing shots ON (2s location intro
   // on every new scene), regardless of the manual checkbox.
  const allSegs  = _autoCollectSegments({ forceEstablishing: true });
  // Per-segment auto-mode skip filter
  const skippedCount = allSegs.filter(s => _isSegmentAutoSkipped(s.anchor)).length;
  AUTO.segments = allSegs.filter(s => !_isSegmentAutoSkipped(s.anchor));
  // Annotate each segment with its script-order index so /seedance/start can
  // record the canonical position regardless of network arrival order.
  AUTO.segments.forEach((s, i) => { s.scriptOrder = i; });
  AUTO.total    = AUTO.segments.length;
  AUTO.cursor   = 0;             // back-compat with status UI
  AUTO.completedCount = 0;       // atomic counter across chains
  AUTO.activeChains = 0;
  AUTO.cancelRequested = false;
  AUTO.lastStatus = '';

  if (!AUTO.total) {
    showToast(`⚠ Нет сегментов для генерации${skippedCount ? ` (${skippedCount} помечены как skip)` : ''}`);
    return;
  }
  // Group by scene → tells us scene-parallel layout
  const sceneGroups = (() => {
    const m = new Map();
    for (const s of AUTO.segments) {
      if (!m.has(s.sceneIdx)) m.set(s.sceneIdx, []);
      m.get(s.sceneIdx).push(s);
    }
    return [...m.values()];
  })();
  // Confirm before kicking off
  const modeWord = AUTO.parallel
    ? 'все параллельно'
    : (sceneGroups.length > 1
        ? `${sceneGroups.length} сцен параллельно × последовательно внутри сцены`
        : 'последовательно (1 сцена)');
  const errWord  = AUTO.errorMode === 'heal' ? 'авто-лечение' : 'останов + сигнал';
  const skipNote = skippedCount ? `\nПропущено по чекбоксу: ${skippedCount}` : '';
  if (!confirm(
    `Запустить Auto-mode?\n\n` +
    `Сегментов: ${AUTO.total}${skipNote}\n` +
    `Режим: ${modeWord}\n` +
    `На ошибке модерации: ${errWord}\n\n` +
    `${AUTO.parallel
      ? 'Параллельный режим: все сегменты отправляются в очередь Seedance подряд (~2с между запусками). Текстовый контекст между чанками сохраняется.'
      : sceneGroups.length > 1
        ? 'Внутри каждой сцены чанки идут последовательно (нужно для last-frame / cut-frames continuity). Сцены друг от друга не зависят и идут параллельно (cap = 3 одновременно).'
        : 'Последовательный режим: каждый чанк ждёт предыдущего.'}`
  )) return;

  AUTO.active = true;
  _autoUpdateStatusUI();
  showToast(`▶ Auto-mode запущен · ${AUTO.total} сегмент${AUTO.total > 1 ? 'ов' : ''} (${modeWord})`, 4000);

  // CRITICAL: capture episode identity ONCE — every in-flight request must target
  // the episode the user pressed "auto-mode" on. If user navigates to another
  // episode mid-run, S.episode.number changes and pending writes leak into the
  // wrong episode (chunks ended up in the wrong file, episode 2's batch wrote
  // into episode 3 — May 2026 incident).
  const epSid    = S.seriesId;
  const epNumber = S.episode.number;

  // Read params from sd panel (used for /seedance/start)
  const duration = parseInt(document.getElementById('sd-duration').value) || 15;
  const resolution = document.getElementById('sd-resolution').value;
  const moderation_bypass = document.getElementById('sd-mod-bypass').value;
  const POLL_INTERVAL_MS = 8000;
  const PARALLEL_DELAY_MS = 2000;
  const MAX_HEAL_RETRIES = 1;
  const MAX_PARALLEL_SCENES = 3;
  const sharedOpts = { useLastframe, useCutframes, useStyle, styleVal, baseOnly, closeUpOnly,
                       duration, resolution, moderation_bypass, POLL_INTERVAL_MS, MAX_HEAL_RETRIES };

  // Compose + start one segment, returns startRes or throws.
  async function _autoComposeStart(seg, scriptOrder) {
    const segCloseUp = sharedOpts.closeUpOnly || !!seg.has_close_up;
    AUTO.lastStatus = segCloseUp ? '⚙ компоную (🎯 close-up)...' : '⚙ компоную...';
    _autoUpdateStatusUI();
    const composeRes = await api.post(
      `/api/series/${epSid}/episodes/${epNumber}/seedance/compose`,
      {
        chunk_text: seg.text,
        use_prev_lastframe: sharedOpts.useLastframe,
        use_prev_cutframes: sharedOpts.useCutframes,
        style: sharedOpts.useStyle ? sharedOpts.styleVal : '',
        base_outfits_only: sharedOpts.baseOnly,
        close_up_only: segCloseUp,
      }
    );
    AUTO.lastStatus = '▶ запускаю генерацию...';
    _autoUpdateStatusUI();
    const startRes = await api.post(
      `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
      {
        prompt: composeRes.prompt,
        chunk_text: seg.text,
        duration: seg.durationSec || sharedOpts.duration, resolution: sharedOpts.resolution,
        moderation_bypass: sharedOpts.moderation_bypass,
        script_order: (scriptOrder != null ? scriptOrder : (typeof seg.scriptOrder === 'number' ? seg.scriptOrder : null)),
        refs: (composeRes.refs || []).map(r => ({
          kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
          source: r.source, prev_idx: r.prev_idx, name: r.name,
          cut_index: r.cut_index, cut_time: r.cut_time,
        })),
      }
    );
    return { composeRes, startRes };
  }

  // Poll one chunk until completed/failed, with optional heal+retry.
  // Returns { ok: bool, chunk?, error? }
  async function _autoPollUntilDone(chunkIdx, composeRes, segText, segDuration) {
    let healAttempts = 0;
    let curIdx = chunkIdx;
    while (true) {
      if (AUTO.cancelRequested) return { ok: false, error: 'cancelled' };
      await new Promise(r => setTimeout(r, sharedOpts.POLL_INTERVAL_MS));
      const polled = await sdPollOnce();
      const chunk = (polled || []).find(c => c.idx === curIdx);
      if (!chunk) {
        AUTO.lastStatus = `… не вижу чанка #${curIdx}`;
        _autoUpdateStatusUI();
        continue;
      }
      AUTO.lastStatus = chunk.status === 'processing' && chunk.progress != null
        ? `⏳ #${curIdx} ${chunk.progress}%`
        : `⏳ #${curIdx} ${chunk.status}`;
      _autoUpdateStatusUI();
      if (chunk.status === 'completed') return { ok: true, chunk };
      if (chunk.status === 'failed') {
        if (AUTO.errorMode === 'heal' && healAttempts < sharedOpts.MAX_HEAL_RETRIES) {
          healAttempts++;
          AUTO.lastStatus = `🩹 лечу промпт #${curIdx}...`;
          _autoUpdateStatusUI();
          const healRes = await api.post(
            `/api/series/${epSid}/episodes/${epNumber}/seedance/${curIdx}/heal-prompt`,
            {}
          );
          const restart = await api.post(
            `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
            {
              prompt: healRes.prompt || composeRes.prompt,
              chunk_text: healRes.chunk_text || segText,
              duration: segDuration || sharedOpts.duration, resolution: sharedOpts.resolution,
              moderation_bypass: sharedOpts.moderation_bypass,
              refs: (composeRes.refs || []).map(r => ({
                kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
                source: r.source, prev_idx: r.prev_idx, name: r.name,
                cut_index: r.cut_index, cut_time: r.cut_time,
              })),
            }
          );
          if (restart?.chunk?.idx != null) curIdx = restart.chunk.idx;
          await sdRefreshList();
          continue;
        }
        return { ok: false, chunk, error: chunk.error || 'failed' };
      }
      // processing / submitting / pending → keep polling
    }
  }

  // Run one scene's segments sequentially: each waits for the previous video.
  // Increments AUTO.completedCount as it goes. Returns 'completed' | 'failed' | 'cancelled'.
  async function _autoRunSceneChain(sceneSegs) {
    AUTO.activeChains++;
    _autoUpdateStatusUI();
    try {
      for (const seg of sceneSegs) {
        if (AUTO.cancelRequested) return 'cancelled';
        let cs;
        try {
          cs = await _autoComposeStart(seg);
        } catch (e) {
          Sounds.playError();
          showToast(`✗ Compose/start упал на сегменте сц.${seg.sceneIdx + 1}.${seg.segIdx + 1}: ${e.message || e}`, 8000);
          return 'failed';
        }
        await sdRefreshList();
        const chunkIdx = cs.startRes?.chunk?.idx;
        if (chunkIdx == null) {
          showToast(`✗ Не получил chunk_idx от /start`, 6000);
          return 'failed';
        }
        const poll = await _autoPollUntilDone(chunkIdx, cs.composeRes, seg.text, seg.durationSec);
        if (poll.error === 'cancelled') return 'cancelled';
        if (!poll.ok) {
          Sounds.playError();
          const errBit = poll.error ? ` (${(poll.error || '').slice(0, 80)})` : '';
          showToast(`✗ Auto-mode остановлен на сегменте сц.${seg.sceneIdx + 1}.${seg.segIdx + 1}${errBit}`, 10000);
          return 'failed';
        }
        AUTO.completedCount++;
        AUTO.cursor = AUTO.completedCount;   // back-compat for status UI
        _autoUpdateStatusUI();
      }
      return 'completed';
    } finally {
      AUTO.activeChains--;
      _autoUpdateStatusUI();
    }
  }

  // Concurrency-capped runner — caps at MAX_PARALLEL_SCENES workers
  async function _runWithCap(items, cap, asyncFn) {
    const queue = items.slice();
    const results = [];
    async function worker() {
      while (queue.length) {
        if (AUTO.cancelRequested) return;
        const item = queue.shift();
        try { results.push(await asyncFn(item)); }
        catch (e) { results.push({ error: e }); }
      }
    }
    const workers = [];
    for (let i = 0; i < Math.min(cap, items.length); i++) workers.push(worker());
    await Promise.all(workers);
    return results;
  }

  try {
    if (AUTO.parallel) {
      // Linear-parallel — first try BATCH-COMPOSE (single Claude call for all
      // segments with shared episodeBlocking → guarantees consistent character
      // positioning across all chunks). Then fire /start for each pre-built
      // prompt. Falls back to per-chunk compose if batch-compose fails.
      let batchPrompts = null;
      AUTO.lastStatus = '🧠 batch-compose (один Claude call на всю серию)...';
      _autoUpdateStatusUI();
      try {
        const batchRes = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/batch-compose`,
          {
            segments: AUTO.segments.map(s => ({
              anchor: s.anchor, sceneIdx: s.sceneIdx, segIdx: s.segIdx,
              text: s.text, has_close_up: !!s.has_close_up, durationSec: s.durationSec,
              establishing_shot: !!s.establishing_shot,
            })),
            base_outfits_only: baseOnly,
            style: useStyle ? styleVal : '',
          },
          { timeoutMs: 900_000 }
        );
        // Pull batch_prompts from server response (count check only — full data on episode)
        const epRes = await api.get(`/api/series/${epSid}/episodes/${epNumber}`);
        batchPrompts = epRes.batch_prompts || {};
        if (batchRes.unresolved_anchors?.length) {
          showToast(`⚠ batch: не все сегменты в ответе (${batchRes.unresolved_anchors.length}). Откатимся на per-chunk compose для них.`, 6000);
        }
        showToast(`🧠 batch готов · ${Object.keys(batchPrompts).length}/${AUTO.segments.length} сегментов`, 4000);
      } catch (e) {
        showToast(`⚠ batch-compose упал — использую per-chunk: ${e.message || e}`, 6000);
        batchPrompts = null;
      }

      // Now fire /start for ALL segments SIMULTANEOUSLY (per the reference
      // pipeline: batch-compose pre-built prompts → all chunks queue at once).
      // No 2s delay, no per-chunk Claude call. Just N concurrent Seedance API
      // submissions. Concurrency capped at 5 to play nicely with provider
      // rate-limits (Seedance and AvAIGen tolerate small bursts well).
      const FIRE_CONCURRENCY = 5;
      AUTO.lastStatus = `▶ запускаю ${AUTO.segments.length} чанк(ов) одновременно...`;
      _autoUpdateStatusUI();

      const fireOne = async (seg, i) => {
        if (AUTO.cancelRequested) return;
        const prebuilt = batchPrompts && batchPrompts[seg.anchor];
        try {
          let startRes;
          if (prebuilt && prebuilt.prompt && (prebuilt.refs || []).length) {
            // Per-segment duration: seg.durationSec is the source of truth (computed
            // from line durations). prebuilt.plan.durationSec is just Claude echoing
            // input — and stale batches built before durationSec was passed all say 15.
            const segDur = seg.durationSec || (prebuilt.plan && prebuilt.plan.durationSec) || duration;
            startRes = await api.post(
              `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
              {
                prompt: prebuilt.prompt,
                chunk_text: seg.text,
                duration: segDur, resolution, moderation_bypass,
                script_order: i,
                refs: (prebuilt.refs || []).map(r => ({
                  kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
                  source: r.source, prev_idx: r.prev_idx, name: r.name,
                })),
              }
            );
          } else {
            // Fallback per-chunk compose if batch missed this anchor
            const cs = await _autoComposeStart(seg, i);
            startRes = cs.startRes;
          }
          AUTO.completedCount++;
          AUTO.cursor = AUTO.completedCount;
          AUTO.lastStatus = `▶ ${AUTO.completedCount}/${AUTO.total} в очереди (#${startRes?.chunk?.idx ?? '?'})`;
          _autoUpdateStatusUI();
        } catch (e) {
          console.warn(`[auto-mode] start failed for seg ${i}:`, e);
          throw e;
        }
      };

      // Run with concurrency cap so we don't fire 50 at once on huge episodes
      const failures = [];
      await _runWithCap(
        AUTO.segments.map((seg, i) => ({ seg, i })),
        FIRE_CONCURRENCY,
        async ({ seg, i }) => {
          try { await fireOne(seg, i); }
          catch (e) { failures.push({ i, error: e.message || String(e) }); }
        }
      );
      await sdRefreshList();
      sdEnsurePoll();
      if (failures.length) {
        Sounds.playError();
        showToast(`⚠ Auto-mode: ${failures.length} из ${AUTO.segments.length} сегментов не запустились — смотри карточки`, 8000);
      }
    } else {
      // Scene-parallel: each scene's chain is sequential, scenes run in parallel
      // with a concurrency cap. Each scene's first chunk doesn't depend on
      // previous scene's lastframe (different setting), so they're independent.
      await _runWithCap(sceneGroups, MAX_PARALLEL_SCENES, _autoRunSceneChain);
    }

    // All done
    AUTO.active = false;
    AUTO.lastStatus = '';
    if (!AUTO.cancelRequested) {
      // Voice-only announcement on whole-episode completion. Fanfare was
      // removed by request — too startling. The TTS phrase already tells the
      // user it's the BIG finish, not just a single segment.
      const epNum = S.episode?.number;
      Sounds.speak(`Episode ${epNum != null ? epNum + ' ' : ''}generation finished.`);
      const tail = AUTO.parallel
        ? ' (отправлены в очередь — следи за карточками)'
        : '';
      showToast(`✓ Auto-mode завершён · ${AUTO.completedCount}/${AUTO.total} сегмент${AUTO.completedCount === 1 ? '' : AUTO.completedCount < 5 ? 'а' : 'ов'}${tail}`, 6000);
    } else {
      showToast(`⏸ Auto-mode остановлен · обработано ${AUTO.completedCount}/${AUTO.total}`, 5000);
    }
    _autoUpdateStatusUI();
  } catch (e) {
    AUTO.active = false;
    _autoUpdateStatusUI();
    Sounds.playError();
    showToast(`✗ Auto-mode упал: ${e.message || e}`, 8000);
  }
}

async function doctorScript() {
  const btn = document.getElementById('ep-doctor-btn');
  if (!S.episode) { alert('Сначала открой эпизод'); return; }
  const script = (document.getElementById('ep-script').value || '').trim();
  if (!script) { alert('Сценарий пустой'); return; }
  // Save current script first
  try { await saveEpisode(); } catch(_){}
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Анализирую дыры...';
  try {
    // Stage 1: dry run — find issues, show user, ask permission
    const r1 = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/doctor-script`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: true }),
    });
    const d1 = await r1.json();
    if (!r1.ok) throw new Error(d1.error || 'audit failed');

    const vios = d1.violations || [];
    if (vios.length === 0) {
      if (d1.audit_error) {
        alert('⚠️ Auditor отработал, но JSON не распарсился: ' + d1.audit_error + '\n\nПопробуй ещё раз.');
      } else {
        alert('✅ Логических дыр не найдено — сценарий чист.');
      }
      return;
    }

    // Build a readable summary
    const TYPE_NAMES = {
      status: 'Статус/полномочия',
      hidden_position: 'Скрытая позиция без мотивации',
      enabling_condition: 'Нет объяснения почему действие возможно',
      legal_term: 'Юр. термин не соответствует фактам',
      unmotivated_delay: 'Немотивированная задержка',
      ambiguous_cliffhanger: 'Размытый клиффхэнгер',
    };
    const lines = vios.map((v, i) =>
      `${i + 1}. [${v.severity === 'critical' ? '🔴' : '🟡'} ${TYPE_NAMES[v.type] || v.type}]\n   📍 ${v.where || '?'}\n   ❗ ${v.explanation || ''}\n   💊 ${v.fix || ''}`
    ).join('\n\n');

    const userExtra = prompt(
      `Найдено дыр: ${d1.critical_count} критичных, ${d1.minor_count} минорных.\n\n${lines}\n\n— — —\nНажми OK чтобы применить исправления (минимальные правки в местах указанных выше).\nМожно дописать СВОИ замечания в поле ниже (по-русски, одной строкой) — Доктор тоже их учтёт.\nCancel — оставить как есть.`,
      ''
    );
    if (userExtra === null) return; // cancelled

    btn.innerHTML = '<span class="spinner"></span> Лечу сценарий...';
    const ctx = { seriesId: S.series.id, episodeNum: S.episode.number, seriesTitle: S.series?.title };
    const d2 = await trackTask('Доктор сценария', ctx, async () => {
      const r2 = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/doctor-script`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ extra_notes: userExtra || '' }),
      });
      const j = await r2.json();
      if (!r2.ok) throw new Error(j.error || 'doctor failed');
      return j;
    });
    if (!r2.ok) throw new Error(d2.error || 'doctor failed');

    if (d2.changed) {
      // Update textarea + save state
      document.getElementById('ep-script').value = d2.script;
      document.getElementById('script-char-count').textContent = d2.script.length;
      // Reload episode from server to refresh script_history etc.
      if (S.episode) {
        const epR = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}`);
        if (epR.ok) S.episode = await epR.json();
      }
      alert(`✅ Сценарий обновлён.\nИсправлено пунктов: ${d2.violations_fixed}\nПредыдущая версия сохранена в истории эпизода.`);
    } else {
      alert(d2.message || 'Без изменений.');
    }
  } catch (e) {
    alert('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

async function extractCharsFromScript() {
  const btn = document.getElementById('ep-extract-chars-btn');
  if (!S.episode) { alert('Сначала открой эпизод'); return; }
  const script = (document.getElementById('ep-script').value || '').trim();
  if (!script) { alert('Сценарий пустой — впиши или сгенерируй сначала'); return; }
  // Save script first so the server reads the latest version
  try { await saveEpisode(); } catch(_){}
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Анализирую...';
  try {
    const ctx = { seriesId: S.series.id, episodeNum: S.episode.number, seriesTitle: S.series?.title };
    const d = await trackTask('Извлечение персонажей + канон', ctx, async () => {
      const r = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/extract-characters`, { method: 'POST' });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'fail');
      return j;
    });
    S.series = d.series;
    const addedC  = d.added_characters || [];
    const addedL  = d.added_locations  || [];
    const dropC   = d.dropped_characters || [];
    const dropL   = d.dropped_locations  || [];
    const lines = [];
    if (addedC.length) lines.push(`✅ Добавлено персонажей: ${addedC.length}\n   • ${addedC.join('\n   • ')}`);
    if (addedL.length) lines.push(`✅ Добавлено локаций: ${addedL.length}\n   • ${addedL.join('\n   • ')}`);
    if (dropC.length)  lines.push(`☐ Сняты галочки персонажей (нет в сцене): ${dropC.length}\n   • ${dropC.join('\n   • ')}`);
    if (dropL.length)  lines.push(`☐ Сняты галочки локаций (нет в сцене): ${dropL.length}\n   • ${dropL.join('\n   • ')}`);

    // --- Appearance updates from director notes ---
    const updates = d.appearance_updates || [];
    if (updates.length) {
      const blocks = updates.map(u =>
        `   • ${u.name}\n     старое: ${u.previous || '—'}\n     новое:  ${u.new}\n     причина: ${u.reason || '—'}`
      ).join('\n\n');
      lines.push(`🎭 Обновлены образы по заметкам (${updates.length}) — портреты пересоздадутся автогеном:\n\n${blocks}`);
    } else if (d.director_notes_used) {
      // Notes existed but model didn't trigger any update — let user know it was considered
      lines.push('📝 Заметки учтены, но обновлений образов не потребовалось.');
    }

    // --- Synopsis update ---
    if (d.synopsis && d.synopsis.updated && d.synopsis.new) {
      // Only touch the textarea if user is still on the same episode page
      const stillOnEp = S.seriesId === ctx.seriesId && S.episodeNum === ctx.episodeNum
                     && !document.getElementById('view-episode')?.classList.contains('hidden');
      if (stillOnEp) {
        const synEl = document.getElementById('ep-synopsis');
        if (synEl) {
          synEl.value = d.synopsis.new;
          if (S.episode) S.episode.synopsis = d.synopsis.new;
        }
      }
      lines.push(`📝 Синопсис переписан на основе сценария (${d.synopsis.new.length} симв.).`);
    } else if (d.synopsis && d.synopsis.error) {
      lines.push(`⚠️ Синопсис не обновился: ${d.synopsis.error}`);
    }

    // --- Canon audit ---
    deliverCanonAudit(d.canon || null, ctx);
    if (d.canon) {
      if (d.canon.audited && !d.canon.passes) {
        const critCount = (d.canon.violations || []).filter(v => v.severity === 'critical').length;
        lines.push(`⚠️ Канон: найдено критических нарушений — ${critCount}. Смотри панель ниже сценария.`);
      } else if (d.canon.updated) {
        const sum = d.canon.update_summary || {};
        const facts = sum.new_facts != null ? sum.new_facts : '?';
        const day = sum.world_day != null ? sum.world_day : '?';
        lines.push(`📚 Канон обновлён: новых фактов — ${facts}, world_day — ${day}.`);
      } else if (d.canon.audit_error) {
        lines.push(`⚠️ Канон-аудит: ${d.canon.audit_error}`);
      }
    }

    if (lines.length === 0) {
      alert('Сценарий пере-сверен. Новых персонажей/локаций нет, активные галочки актуальны.');
    } else {
      lines.push('');
      lines.push('Картинки для новых персонажей/локаций запустятся автоматически (если включён автоген).');
      alert(lines.join('\n\n'));
    }
    // Refresh whatever views are currently shown
    if (typeof renderCharactersList === 'function') renderCharactersList();
    if (typeof renderLocationsList  === 'function') renderLocationsList();
    if (typeof renderItemsList      === 'function') renderItemsList();
    if (typeof loadEpisodeView === 'function' && S.episode) loadEpisodeView(S.episode.number);
  } catch (e) {
    alert('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

async function generateEpisodeScript() {
  const btn = document.getElementById('ep-gen-script-btn');
  const status = document.getElementById('ep-script-gen-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  status.textContent = 'Генерируем сценарий...';
  status.style.color = 'var(--warning)';
  const taskCtx = { seriesId: S.seriesId, episodeNum: S.episodeNum, seriesTitle: S.series?.title };
  try {
    await saveEpisodeSilent();
    const res = await trackTask('Сценарий эпизода', taskCtx, () =>
      api.post(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/generate-script`, {})
    );
    setVal('ep-script', res.script);
    S.episode.script = res.script;
    if (res.characters_used) S.episode.characters_used = res.characters_used;
    if (res.character_outfits) {
      // Backend returns lists; older episodes may still hold strings — normalize both sides
      const merged = { ...(S.episode.character_outfits || {}) };
      for (const [cid, raw] of Object.entries(res.character_outfits)) {
        const arr = Array.isArray(raw) ? raw.filter(Boolean).map(String) : (raw ? [String(raw)] : []);
        if (arr.length) merged[cid] = arr; else delete merged[cid];
      }
      S.episode.character_outfits = merged;
    }
    if (res.locations_used) S.episode.locations_used = res.locations_used;
    if (res.audit_report) S.episode.audit_report = res.audit_report;
    if (res.logic_brief) S.episode.logic_brief = res.logic_brief;
    // Surface audit result to user (informational — pipeline already auto-retried internally)
    const ar = res.audit_report;
    if (ar) {
      const crit = (ar.violations || []).filter(v => v.severity === 'critical').length;
      if (ar.passes && crit === 0) {
        console.log(`[logic-audit] ✓ ep ${S.episodeNum} clean (retries: ${ar.retries})`);
      } else {
        console.warn(`[logic-audit] ⚠ ep ${S.episodeNum} has ${crit} critical issues after ${ar.retries} retries`,
                     ar.violations);
      }
    }
    // Backend may have auto-added new characters / outfits parsed from the cast block
    if (res.series) {
      S.series = res.series;
      if (typeof renderCharactersList === 'function') renderCharactersList();
    }
    renderEpCharacters();
    renderEpLocations();
  renderEpItems();
    updateScriptCounter();
    updateGenScriptBtn();
    status.textContent = '✓ Сценарий готов. Жми «🤖 Извлечь персонажей и локации» когда будешь готов.';
    status.style.color = 'var(--success)';
    // Sound notification — gated by per-episode toggle (default ON, persisted)
    if (_scriptSoundsEnabled()) {
      try { Sounds.playSuccess(); } catch (e) {}
    }
    // NOTE: auto-extraction of chars/locations is INTENTIONALLY skipped here.
    // User wants explicit control — they'll click "🤖 Извлечь персонажей и локации"
    // when ready. Backend also no longer auto-syncs cast block on script-save.
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
    if (_scriptSoundsEnabled()) {
      try { Sounds.playError(); } catch (err) {}
    }
    btn.disabled = false;
    updateGenScriptBtn();
  }
}

// Persisted toggle: controls success/error sound at end of script generation.
// Wired once when episode loads; user can toggle without affecting other sounds.
function _scriptSoundsEnabled() {
  const cb = document.getElementById('ep-script-sounds');
  if (!cb) return false;
  if (!cb.dataset.wired) {
    const saved = localStorage.getItem('script_gen_sounds');
    cb.checked = saved === null ? true : saved === '1';
    cb.addEventListener('change', () => {
      localStorage.setItem('script_gen_sounds', cb.checked ? '1' : '0');
    });
    cb.dataset.wired = '1';
  }
  return !!cb.checked;
}

// Apply manual edits to the script textarea — saves what's currently typed
// to the episode on disk, surfaces the result in the gen-status line.
// ──────────────────────────────────────────────────────────────────────────
// "Принять сценарий" — single-click flow that replaces the old multi-step
// dance (Edit script → 🤖 Извлечь → 🎬 Сцены → manual generate). Steps:
//   1. Save the script to disk.
//   2. Snapshot existing chars/locs/items IDs (the "before" set).
//   3. Run extract-characters (adds new chars + locs to series.json).
//   4. Run detect-items (adds new plot-relevant items + items_used).
//   5. Diff vs the snapshot → list of NEW entities.
//   6. If anything new → show modal with empty drop-cards for each new
//      entity. User can drag images. Buttons:
//        ← Назад к редактированию  |  ✅ Принять и сгенерировать недостающие
//   7. On accept: open scene view automatically, kick off the autogen
//      sweep, and show the prominent progress banner above the script.
//   8. If nothing new (or user declines drops): same — just sweep + banner.
//
// The progress banner polls /auto-generate/status and updates the counter,
// the bar, and the "сейчас: <name>" line every 2s. While it's visible, the
// user is warned not to start parallel generations (Reteller / Seedance /
// per-asset clicks) because they'd race the sweep.
// ──────────────────────────────────────────────────────────────────────────

async function acceptScript(opts = {}) {
  if (!S.episode) { alert('Сначала открой эпизод'); return; }
  const script = (document.getElementById('ep-script').value || '').trim();
  if (!script) { alert('Сценарий пустой — впиши или сгенерируй сначала'); return; }
  // Already-accepted shortcut: episode has cast_extracted=true → no need to
  // re-run LLM extraction. Just open scene view (which is what the user
  // expected after their first accept anyway). Pass opts.force=true to
  // override and re-analyse from scratch (button "🔄 Перепроанализировать").
  if (!opts.force && S.episode.cast_extracted === true) {
    const sceneView = document.getElementById('ep-script-scenes');
    if (sceneView && sceneView.classList.contains('hidden')) {
      toggleSceneView();
    }
    showToast('✓ Сценарий уже принят — открыл режим сцен', 3500);
    return;
  }
  const btn = document.getElementById('ep-accept-script-btn');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Анализирую...';
  try {
    // 1+2: Save script + snapshot before-IDs.
    await api.put(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`, { script });
    const beforeChars = new Set((S.series?.characters || []).map(c => c.id));
    const beforeLocs  = new Set((S.series?.locations  || []).map(l => l.id));
    const beforeItems = new Set((S.series?.items      || []).map(it => it.id));

    // 3: extract chars + locs.
    btn.innerHTML = '<span class="spinner"></span> Ищу персонажей и локации...';
    const ext = await fetch(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/extract-characters`, { method: 'POST' })
      .then(r => r.json());
    if (ext.error) throw new Error('extract-characters: ' + ext.error);
    if (ext.series) S.series = ext.series;

    // 4: detect items (plot-relevant). Best-effort — don't fail accept if items endpoint errs.
    btn.innerHTML = '<span class="spinner"></span> Ищу сюжетные предметы...';
    try {
      const det = await api.post(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/detect-items`, {});
      if (det && !det.error) {
        const fresh = await api.get(`/api/series/${S.seriesId}`);
        if (fresh) S.series = fresh;
      }
    } catch (e) {
      console.warn('[acceptScript] item detection failed (continuing):', e);
    }

    // Refresh episode (items_used etc may have changed).
    try {
      const ep = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
      if (ep) S.episode = ep;
    } catch {}

    // 5: diff.
    const newChars = (S.series.characters || []).filter(c => !beforeChars.has(c.id));
    const newLocs  = (S.series.locations  || []).filter(l => !beforeLocs.has(l.id));
    const newItems = (S.series.items      || []).filter(it => !beforeItems.has(it.id));

    // 6: modal or proceed.
    const total = newChars.length + newLocs.length + newItems.length;
    if (total > 0) {
      // Show modal with drop cards. User decides next step.
      _showAcceptScriptModal({ newChars, newLocs, newItems });
    } else {
      // Nothing new — straight to scene view + sweep.
      _proceedAfterAccept();
    }
  } catch (e) {
    alert('Ошибка приёма сценария: ' + (e?.message || e));
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Holds per-section "skip generation" flags during the accept modal lifecycle.
// When user ticks "Не генерить" for a section, those entity ids land in
// _acceptModalSkipIds[kind] and get filtered out when the autogen sweep
// inspects "what's missing". Implementation: we set a sentinel field
// `_skip_autogen=true` on the entity in series.json so the sweep skips it.
// (Persisted so accidental refresh doesn't lose the choice; user can flip
// it back later via the per-asset card.)
let _acceptModalSkipKinds = new Set();

function _showAcceptScriptModal({ newChars, newLocs, newItems }) {
  const sectionHtml = (title, kind, list) => list.length ? `
    <div class="accept-modal-section">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
        <h4 style="margin:0">${esc(title)} (${list.length})</h4>
        <label style="display:inline-flex;align-items:center;gap:6px;font-size:0.82rem;color:var(--muted);cursor:pointer"
               title="Не запускать автоген для этой группы — карточки создадутся, но фото нужно будет сгенерировать вручную позже.">
          <input type="checkbox" class="amc-skip-cb" data-kind="${kind}"
                 onchange="_toggleAcceptSkip('${kind}', this.checked)">
          🚫 Не генерить эту группу
        </label>
      </div>
      <div class="accept-modal-grid" id="acc-grid-${kind}">
        ${list.map(e => _acceptCellHtml(kind, e)).join('')}
      </div>
    </div>` : '';

  // Reset state from any previous open.
  _acceptModalSkipKinds = new Set();

  closeLightbox();
  const overlay = document.createElement('div');
  overlay.id = 'lightbox-overlay';
  overlay.className = 'lightbox-overlay';
  overlay.style.zIndex = 99998;
  overlay.dataset.acceptModal = '1';
  // Stash the entity lists on the DOM so confirmAcceptModal can consult them.
  overlay.dataset.newChars = JSON.stringify(newChars.map(c => c.id));
  overlay.dataset.newLocs  = JSON.stringify(newLocs.map(l  => l.id));
  overlay.dataset.newItems = JSON.stringify(newItems.map(it => it.id));
  overlay.innerHTML = `
    <button class="lb-close" onclick="closeAcceptModal()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()" style="max-width:880px;flex-direction:column">
      <div class="lightbox-panel" style="width:100%;max-height:none;overflow-y:auto">
        <h3>В сценарии нашлось новое</h3>
        <div class="hint">
          Можешь перетащить готовые фотки (drag &amp; drop) на любую карточку — те, на которые не закинешь, сгенерируются автоматически.
          Поставь 🚫 «Не генерить эту группу» рядом с заголовком чтобы пропустить генерацию (карточки останутся пустыми, сгенеришь позже вручную).
        </div>
        ${sectionHtml('🧑 Персонажи', 'char', newChars)}
        ${sectionHtml('📍 Локации',   'loc',  newLocs)}
        ${sectionHtml('🎒 Предметы',  'item', newItems)}
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;justify-content:flex-end">
          <button class="btn-ghost" onclick="closeAcceptModal()">← Назад к редактированию</button>
          <button class="btn-regen" onclick="confirmAcceptModal()">✅ Принять и сгенерировать недостающие →</button>
        </div>
      </div>
    </div>`;
  overlay.onclick = (e) => { if (e.target === overlay) closeAcceptModal(); };
  document.body.appendChild(overlay);
}

function _toggleAcceptSkip(kind, on) {
  if (on) _acceptModalSkipKinds.add(kind);
  else    _acceptModalSkipKinds.delete(kind);
  // Visually dim the cells in this section so user sees the state.
  const grid = document.getElementById(`acc-grid-${kind}`);
  if (grid) grid.style.opacity = on ? '0.35' : '1';
}

function _acceptCellHtml(kind, entity) {
  const sid = S.seriesId;
  const hasRef = entity.ref_images && entity.ref_images.length > 0;
  const photoUrl = hasRef ? `/assets/${sid}/${entity.ref_images[0]}` : null;
  const icon = kind === 'char' ? '👤' : (kind === 'loc' ? '📍' : '🎒');
  return `
    <div class="accept-modal-cell ${hasRef ? 'has-photo' : ''}"
         id="acc-cell-${kind}-${entity.id}"
         ondragover="event.preventDefault();this.classList.add('drop-hover')"
         ondragleave="this.classList.remove('drop-hover')"
         ondrop="event.preventDefault();this.classList.remove('drop-hover');acceptCellDrop(event,'${kind}','${entity.id}')">
      <div class="amc-thumb">
        ${photoUrl ? `<img src="${photoUrl}" alt="">` : icon}
      </div>
      <div class="amc-name">${esc(entity.name)}</div>
      <div class="amc-status">${hasRef ? '✓ есть фото' : 'будет сгенерировано'}</div>
    </div>`;
}

async function acceptCellDrop(event, kind, entityId) {
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const endpoint = kind === 'char'
    ? `/api/series/${S.seriesId}/assets/character/${entityId}`
    : kind === 'loc'
    ? `/api/series/${S.seriesId}/assets/location/${entityId}`
    : `/api/series/${S.seriesId}/assets/item/${entityId}`;
  const cell = document.getElementById(`acc-cell-${kind}-${entityId}`);
  if (cell) cell.querySelector('.amc-status').textContent = '⏳ загружаю...';
  try {
    const r = await fetch(endpoint, { method: 'POST', body: fd });
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    // Refresh series and re-render this cell with the new image.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    const collection = kind === 'char' ? S.series.characters : kind === 'loc' ? S.series.locations : S.series.items;
    const updated = (collection || []).find(x => x.id === entityId);
    if (updated && cell) {
      cell.outerHTML = _acceptCellHtml(kind, updated);
    }
  } catch (e) {
    if (cell) cell.querySelector('.amc-status').textContent = '✗ ' + (e?.message || e);
  }
}

function closeAcceptModal() {
  const ov = document.getElementById('lightbox-overlay');
  if (ov) ov.remove();
}

async function confirmAcceptModal() {
  // Persist per-section skip flags onto the new entities BEFORE closing the
  // modal — the autogen sweep reads `_skip_autogen=true` from series.json
  // and silently bypasses those entities. The user can flip the flag back
  // later via the per-asset "Сгенерировать" click (handled separately).
  const overlay = document.getElementById('lightbox-overlay');
  if (overlay && overlay.dataset.acceptModal === '1' && _acceptModalSkipKinds.size) {
    const payload = { skip: true, chars: [], locs: [], items: [] };
    if (_acceptModalSkipKinds.has('char')) payload.chars = JSON.parse(overlay.dataset.newChars || '[]');
    if (_acceptModalSkipKinds.has('loc'))  payload.locs  = JSON.parse(overlay.dataset.newLocs  || '[]');
    if (_acceptModalSkipKinds.has('item')) payload.items = JSON.parse(overlay.dataset.newItems || '[]');
    try {
      await api.post(`/api/series/${S.seriesId}/skip-autogen`, payload);
      bumpAssetVersion();
      // Refresh series to pick up the persisted flags.
      const fresh = await api.get(`/api/series/${S.seriesId}`);
      if (fresh) S.series = fresh;
    } catch (e) {
      console.warn('[acceptScript] failed to persist skip flags', e);
    }
  }
  closeAcceptModal();
  await _proceedAfterAccept();
}

async function _proceedAfterAccept() {
  // Switch to scene view automatically (replaces the manual 🎬 Сцены click).
  try {
    const view = document.getElementById('ep-script-scenes');
    const ta = document.getElementById('ep-script');
    const btn = document.getElementById('ep-scenes-btn');
    if (view?.classList.contains('hidden')) {
      // Reuse the existing toggle to keep all side-effects consistent.
      toggleSceneView();
    }
  } catch (e) { console.warn('[acceptScript] scene-view switch failed', e); }

  // Kick off the autogen sweep + show the progress banner.
  showAcceptProgressBanner();
  try {
    await fetch(`/api/series/${S.seriesId}/auto-generate/sweep`, { method: 'POST' });
  } catch (e) {
    console.warn('[acceptScript] sweep trigger failed', e);
  }
  // Reuse the existing poller — its UI updates the corner status; we mirror
  // the counts into the big banner.
  pollAutogenStatus();
  _startAcceptBannerPoll();
}

function showAcceptProgressBanner() {
  const el = document.getElementById('ep-accept-progress');
  if (el) el.classList.remove('hidden');
}
function hideAcceptProgressBanner() {
  const el = document.getElementById('ep-accept-progress');
  if (el) el.classList.add('hidden');
  if (_acceptBannerTimer) { clearInterval(_acceptBannerTimer); _acceptBannerTimer = null; }
}

let _acceptBannerTimer = null;
function _startAcceptBannerPoll() {
  if (_acceptBannerTimer) clearInterval(_acceptBannerTimer);
  let stillCount = 0;
  const tick = async () => {
    try {
      const st = await fetch(`/api/series/${S.seriesId}/auto-generate/status`).then(r => r.json());
      const counter = document.getElementById('eap-counter');
      const now = document.getElementById('eap-now');
      const fill = document.getElementById('eap-bar-fill');
      if (counter) counter.textContent = `${st.done || 0} / ${st.queue || 0}`;
      const ipBits = (st.in_progress || []).map(x => x.name).filter(Boolean).slice(0, 3).join(', ');
      if (now) now.textContent = ipBits ? `· сейчас: ${ipBits}` : '';
      if (fill && st.queue) fill.style.width = `${Math.min(100, Math.round(100 * (st.done || 0) / st.queue))}%`;
      if (!st.running) {
        stillCount += 1;
        // Wait a couple ticks of "not running" before hiding (avoids flicker
        // between back-to-back sweeps if the recursive re-run triggers).
        if (stillCount >= 2) {
          if (fill) fill.style.width = '100%';
          setTimeout(hideAcceptProgressBanner, 1500);
        }
      } else {
        stillCount = 0;
      }
    } catch {}
  };
  tick();
  _acceptBannerTimer = setInterval(tick, 2000);
}

async function applyScriptChanges() {
  if (!S.episode) return;
  const btn = document.getElementById('ep-script-apply-btn');
  const status = document.getElementById('ep-script-gen-status');
  const newScript = val('ep-script');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Сохраняю...'; }
  if (status) { status.textContent = ''; }
  try {
    const updated = await api.put(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}`,
      { script: newScript }
    );
    S.episode = updated;
    if (status) {
      status.textContent = '✓ Изменения сохранены';
      status.style.color = 'var(--success)';
      setTimeout(() => { if (status.textContent === '✓ Изменения сохранены') status.textContent = ''; }, 4000);
    }
    if (btn) {
      btn.style.display = 'none';
      btn.dataset.dirty = '';
    }
  } catch (e) {
    if (status) { status.textContent = '✗ ' + (e.message || e); status.style.color = 'var(--danger)'; }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '💾 Применить изменения'; }
  }
}

// ── Inline logic-check (after script gen, before reteller prompt) ────────────
// Stores the violations returned from the dry-run audit so we can re-send them
// to the doctor with the user's selection.
let _logicViolations = [];

function hideLogicPanel() {
  const panel = document.getElementById('ep-logic-panel');
  if (panel) panel.classList.add('hidden');
  _logicViolations = [];
}

async function runLogicCheckThenContinue() {
  const status = document.getElementById('ep-script-gen-status');
  const panel = document.getElementById('ep-logic-panel');
  hideLogicPanel();
  if (status) {
    status.textContent = '🩺 Проверяем логику с учётом предыдущей серии...';
    status.style.color = 'var(--warning)';
  }
  let report;
  try {
    report = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/doctor-script`,
      { dry_run: true },
      { timeoutMs: 180_000 }
    );
  } catch (e) {
    // If the audit itself blew up — don't block the user, just go to reteller.
    console.warn('[logic-check] audit failed → continuing to reteller', e);
    if (status) status.textContent = `⚠ Доктор не отработал (${e.message}) — продолжаем к Reteller`;
    triggerRetellerAfterLogic();
    return;
  }
  const v = (report.violations || []).filter(x => x && x.explanation);
  // Only block on real critical issues — minors are informational.
  const critical = v.filter(x => (x.severity || 'critical') === 'critical');
  if (critical.length === 0) {
    if (status) {
      status.textContent = '✓ Логика чиста — генерируем промпт Reteller...';
      status.style.color = 'var(--success)';
    }
    triggerRetellerAfterLogic();
    return;
  }
  // Show inline panel with checkboxes
  _logicViolations = critical;
  renderLogicViolations(critical);
  if (panel) panel.classList.remove('hidden');
  if (status) {
    status.textContent = `⚠ Найдено ${critical.length} логич. замечаний — отметь, что чинить, или игнорируй всё`;
    status.style.color = 'var(--warning)';
  }
  // Scroll panel into view so the user notices it
  panel?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// Pending canon audits keyed by `${seriesId}:${episodeNum}` so that a result
// arriving while the user is on a different page shows up when they navigate
// back, not on whatever page they're currently looking at.
const _pendingCanonAudits = {};
let _canonViolations = [];   // current panel's violations (for fix flow)
let _canonPanelCtx = null;   // { seriesId, episodeNum } the visible panel belongs to

function _canonKey(seriesId, episodeNum) { return `${seriesId}:${episodeNum}`; }

// Called by extractCharsFromScript with full ctx (seriesId, episodeNum). Stores
// the result in pending and renders ONLY if the user is currently on the
// matching episode page.
function deliverCanonAudit(canon, ctx) {
  if (!canon || !canon.audited || canon.passes || !(canon.violations || []).length) {
    // Clean → drop any pending entry + any visible panel for this episode
    if (ctx) {
      delete _pendingCanonAudits[_canonKey(ctx.seriesId, ctx.episodeNum)];
      if (_canonPanelCtx
          && _canonPanelCtx.seriesId === ctx.seriesId
          && _canonPanelCtx.episodeNum === ctx.episodeNum) {
        _removeCanonPanel();
      }
    }
    return;
  }
  const key = _canonKey(ctx.seriesId, ctx.episodeNum);
  _pendingCanonAudits[key] = { canon, ctx };
  // Render only if we're on the matching episode right now
  const onEpisodeView = !document.getElementById('view-episode')?.classList.contains('hidden');
  if (onEpisodeView && S.seriesId === ctx.seriesId && S.episodeNum === ctx.episodeNum) {
    renderCanonAuditPanel(canon, ctx);
  }
  // Otherwise it stays in _pendingCanonAudits and gets rendered when the user
  // navigates to the right episode (see hook in loadEpisodeView below).
}

// Called from loadEpisodeView once the episode is loaded — surfaces any
// pending canon audit waiting for this episode.
function flushPendingCanonForCurrentEpisode() {
  if (!S.seriesId || !S.episodeNum) return;
  const key = _canonKey(S.seriesId, S.episodeNum);
  const pending = _pendingCanonAudits[key];
  if (pending) {
    renderCanonAuditPanel(pending.canon, pending.ctx);
  } else {
    _removeCanonPanel();
  }
}

function _removeCanonPanel() {
  const p = document.getElementById('ep-canon-panel');
  if (p) p.remove();
  _canonPanelCtx = null;
  _canonViolations = [];
}

function renderCanonAuditPanel(canon, ctx) {
  const violations = (canon.violations || []).filter(v => v);
  if (!violations.length) { _removeCanonPanel(); return; }

  let panel = document.getElementById('ep-canon-panel');
  if (!panel) {
    const anchor = document.getElementById('ep-script')?.closest('.field-group') || document.getElementById('ep-script')?.parentElement;
    if (!anchor) return;
    panel = document.createElement('div');
    panel.id = 'ep-canon-panel';
    panel.className = 'logic-panel';
    anchor.insertAdjacentElement('afterend', panel);
  }
  _canonViolations = violations;
  _canonPanelCtx = { seriesId: ctx.seriesId, episodeNum: ctx.episodeNum };

  const critCount = violations.filter(v => (v.severity || 'critical') === 'critical').length;
  panel.innerHTML = `
    <div class="logic-panel-head">
      <strong>📚 Канон-аудит: нарушения (${violations.length}${critCount && critCount !== violations.length ? `, критич. ${critCount}` : ''})</strong>
      <span class="logic-panel-hint" style="color:var(--muted);font-size:0.78rem">Канон НЕ обновлён до устранения проблем</span>
    </div>
    <div class="logic-panel-toolbar">
      <button type="button" class="btn-ghost btn-sm" id="ep-canon-toggle-all">Выделить всё</button>
      <span id="ep-canon-count" style="color:var(--muted);font-size:0.78rem">отмечено: 0</span>
    </div>
    <div class="logic-panel-body" id="ep-canon-list">
      ${violations.map((v, i) => `
        <label class="logic-violation" data-idx="${i}">
          <input type="checkbox" class="canon-violation-cb" data-idx="${i}">
          <div class="logic-violation-body">
            <div class="logic-violation-head">
              <span class="logic-tag logic-tag-${esc(v.severity || 'critical')}">${esc(v.severity || 'critical')}</span>
              <span class="logic-type">${esc(v.type || v.rule || '?')}</span>
              ${v.where ? `<span class="logic-where">@ ${esc(v.where)}</span>` : ''}
            </div>
            <div class="logic-explain">${esc(v.explanation || v.message || '')}</div>
            ${v.fix ? `<div class="logic-fix"><b>Решение:</b> ${esc(v.fix)}</div>` : ''}
          </div>
        </label>
      `).join('')}
    </div>
    <div class="logic-panel-actions">
      <button type="button" class="btn-primary" id="ep-canon-fix-btn" onclick="fixSelectedCanonViolations()">🩹 Внести изменения</button>
      <button type="button" class="btn-ghost" id="ep-canon-skip-btn" onclick="ignoreCanonViolations()">Игнорировать все несостыковки</button>
    </div>
    <div id="ep-canon-status" class="logic-explain" style="opacity:.75;margin-top:6px;font-size:12px"></div>
  `;
  // Wire toggle-all + checkbox counter
  const list = panel.querySelector('#ep-canon-list');
  const counter = panel.querySelector('#ep-canon-count');
  const updateCount = () => {
    const n = list.querySelectorAll('.canon-violation-cb:checked').length;
    counter.textContent = `отмечено: ${n}`;
  };
  list.addEventListener('change', e => {
    if (e.target.classList.contains('canon-violation-cb')) updateCount();
  });
  panel.querySelector('#ep-canon-toggle-all').addEventListener('click', () => {
    const boxes = list.querySelectorAll('.canon-violation-cb');
    const allChecked = [...boxes].every(b => b.checked);
    boxes.forEach(b => { b.checked = !allChecked; });
    panel.querySelector('#ep-canon-toggle-all').textContent = allChecked ? 'Выделить всё' : 'Снять выделение';
    updateCount();
  });
  panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function _getSelectedCanonViolations() {
  const out = [];
  document.querySelectorAll('#ep-canon-list .canon-violation-cb').forEach(cb => {
    if (cb.checked) {
      const i = parseInt(cb.dataset.idx, 10);
      if (!isNaN(i) && _canonViolations[i]) out.push(_canonViolations[i]);
    }
  });
  return out;
}

async function fixSelectedCanonViolations() {
  const ctx = _canonPanelCtx;
  if (!ctx) return;
  const picked = _getSelectedCanonViolations();
  const status = document.getElementById('ep-canon-status');
  if (!picked.length) {
    if (status) { status.textContent = 'Отметь хотя бы одно нарушение — или нажми «Игнорировать все несостыковки»'; status.style.color = 'var(--warning)'; }
    return;
  }
  const fixBtn = document.getElementById('ep-canon-fix-btn');
  const skipBtn = document.getElementById('ep-canon-skip-btn');
  const orig = fixBtn.innerHTML;
  fixBtn.disabled = true; if (skipBtn) skipBtn.disabled = true;
  fixBtn.innerHTML = '<span class="spinner"></span> Чиним...';
  if (status) { status.textContent = 'Доктор переписывает сцены...'; status.style.color = 'var(--muted)'; }

  try {
    const d = await trackTask(`Канон-фикс Эп. ${ctx.episodeNum}`, ctx, async () => {
      const r = await fetch(`/api/series/${ctx.seriesId}/episodes/${ctx.episodeNum}/doctor-script`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ violations: picked }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'doctor failed');
      return j;
    });

    if (d.changed) {
      // If user is currently on this same episode, refresh the textarea
      if (S.seriesId === ctx.seriesId && S.episodeNum === ctx.episodeNum) {
        const ta = document.getElementById('ep-script');
        if (ta) {
          ta.value = d.script;
          const cnt = document.getElementById('script-char-count');
          if (cnt) cnt.textContent = d.script.length;
        }
        // Reload episode meta
        try {
          const epR = await fetch(`/api/series/${ctx.seriesId}/episodes/${ctx.episodeNum}`);
          if (epR.ok) S.episode = await epR.json();
        } catch {}
      }
      if (status) { status.textContent = `✓ Сценарий обновлён, исправлено: ${d.violations_fixed || picked.length}. Запусти «Извлечь персонажей» ещё раз, чтобы канон обновился.`; status.style.color = 'var(--success)'; }
      // Drop pending: user must re-run extract to re-audit
      delete _pendingCanonAudits[_canonKey(ctx.seriesId, ctx.episodeNum)];
      setTimeout(() => _removeCanonPanel(), 2500);
    } else {
      if (status) { status.textContent = d.message || 'Без изменений.'; status.style.color = 'var(--warning)'; }
    }
  } catch (e) {
    if (status) { status.textContent = 'Ошибка: ' + e.message; status.style.color = 'var(--danger)'; }
  } finally {
    fixBtn.disabled = false; fixBtn.innerHTML = orig;
    if (skipBtn) skipBtn.disabled = false;
  }
}

function ignoreCanonViolations() {
  const ctx = _canonPanelCtx;
  if (ctx) delete _pendingCanonAudits[_canonKey(ctx.seriesId, ctx.episodeNum)];
  _removeCanonPanel();
}

function renderLogicViolations(items) {
  const list = document.getElementById('ep-logic-list');
  const counter = document.getElementById('ep-logic-count');
  if (!list) return;
  if (counter) counter.textContent = items.length;
  list.innerHTML = items.map((v, i) => `
    <label class="logic-violation" data-idx="${i}">
      <input type="checkbox" class="logic-violation-cb" data-idx="${i}">
      <div class="logic-violation-body">
        <div class="logic-violation-head">
          <span class="logic-tag logic-tag-${esc(v.severity || 'critical')}">${esc(v.severity || 'critical')}</span>
          <span class="logic-type">${esc(v.type || '?')}</span>
          ${v.where ? `<span class="logic-where">@ ${esc(v.where)}</span>` : ''}
        </div>
        <div class="logic-explain">${esc(v.explanation || '')}</div>
        ${v.fix ? `<div class="logic-fix"><b>Фикс:</b> ${esc(v.fix)}</div>` : ''}
      </div>
    </label>
  `).join('');
}

function toggleAllLogicViolations() {
  const boxes = document.querySelectorAll('.logic-violation-cb');
  const allChecked = [...boxes].every(b => b.checked);
  boxes.forEach(b => { b.checked = !allChecked; });
  document.getElementById('ep-logic-toggle-all').textContent = allChecked ? 'Выделить всё' : 'Снять выделение';
}

function getSelectedLogicViolations() {
  const picked = [];
  document.querySelectorAll('.logic-violation-cb').forEach(cb => {
    if (cb.checked) {
      const i = parseInt(cb.dataset.idx, 10);
      if (!isNaN(i) && _logicViolations[i]) picked.push(_logicViolations[i]);
    }
  });
  return picked;
}

async function fixSelectedLogicIssues() {
  const picked = getSelectedLogicViolations();
  const status = document.getElementById('ep-script-gen-status');
  if (!picked.length) {
    showToast('Отметь хотя бы одно замечание — или нажми «Игнорировать всё»');
    return;
  }
  const fixBtn = document.getElementById('ep-logic-fix-btn');
  const skipBtn = document.getElementById('ep-logic-skip-btn');
  const orig = fixBtn.innerHTML;
  fixBtn.disabled = true; if (skipBtn) skipBtn.disabled = true;
  fixBtn.innerHTML = '<span class="spinner"></span> Лечим...';
  if (status) {
    status.textContent = `🩹 Доктор чинит ${picked.length} замечаний...`;
    status.style.color = 'var(--warning)';
  }
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/doctor-script`,
      { violations: picked },
      { timeoutMs: 240_000 }
    );
    if (res.script) {
      setVal('ep-script', res.script);
      S.episode.script = res.script;
      updateScriptCounter();
    }
    hideLogicPanel();
    if (status) {
      status.textContent = `✓ Починено ${picked.length} — генерируем промпт Reteller...`;
      status.style.color = 'var(--success)';
    }
    triggerRetellerAfterLogic();
  } catch (e) {
    if (status) {
      status.textContent = 'Ошибка доктора: ' + e.message;
      status.style.color = 'var(--danger)';
    }
    fixBtn.disabled = false; if (skipBtn) skipBtn.disabled = false;
    fixBtn.innerHTML = orig;
  }
}

function skipLogicAndContinue() {
  hideLogicPanel();
  const status = document.getElementById('ep-script-gen-status');
  if (status) {
    status.textContent = 'Игнорируем замечания — генерируем промпт Reteller...';
    status.style.color = 'var(--success)';
  }
  triggerRetellerAfterLogic();
}

function triggerRetellerAfterLogic() {
  const status = document.getElementById('ep-script-gen-status');
  generateEpRettellerPrompt().then(() => {
    if (status) {
      status.textContent = '✓ Готово';
      setTimeout(() => { if (status) status.textContent = ''; }, 3000);
    }
  }).catch(() => {
    if (status) {
      status.textContent = '✓ Сценарий готов (промпт Reteller — нажми ⚡)';
      setTimeout(() => { if (status) status.textContent = ''; }, 5000);
    }
  });
}

function renderEpLocations() {
  const el = document.getElementById('ep-locations-list');
  if (!el) return;
  const locs = S.series.locations || [];
  if (!locs.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">Нет локаций</div>';
    return;
  }
  const used = S.episode.locations_used || [];
  const seedanceMode = document.body.classList.contains('seedance-mode');
  el.innerHTML = locs.map(l => {
    const hasRef = l.ref_images && l.ref_images.length > 0;
    const imgUrl = hasRef ? `${assetUrl(l.ref_images[0])}` : null;
    const inEp = used.includes(l.id);
    const dragAttrs = (seedanceMode && hasRef)
      ? `draggable="true" ondragstart="sdLocDragStart(event,'${l.id}')"` : '';
    return `
      <div class="ep-loc-row ${inEp ? 'in-episode' : ''}" id="ep-loc-${l.id}" ${dragAttrs}
           ondragover="event.preventDefault();this.classList.add('drop-hover')"
           ondragleave="this.classList.remove('drop-hover')"
           ondrop="event.preventDefault();this.classList.remove('drop-hover');dropLocPhoto(event,'${l.id}')">
        <div class="ep-loc-thumb"
             ${imgUrl ? `onclick="event.stopPropagation();openLocLightbox('${l.id}','${imgUrl}')" style="cursor:zoom-in"
                         title="Открыть локацию (можно перегенерировать с пожеланиями)"` : ''}>
          ${imgUrl ? `<img src="${imgUrl}" alt="" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : '📍'}
        </div>
        <div class="ep-loc-name">
          <div class="ep-char-name-row">
            <input type="checkbox" ${inEp ? 'checked' : ''} onchange="toggleEpLoc('${l.id}',this)">
            <span>${esc(l.name)}</span>
          </div>
          ${!hasRef ? `
            <div class="ep-char-gen-btns" id="ep-loc-btns-${l.id}">
              <button class="btn-prompt" onclick="showLocPrompt('${l.id}')">📋 Промпт</button>
              <button class="btn-generate" id="ep-loc-gen-btn-${l.id}" onclick="generateLocImage('${l.id}')">⚡ Сгенерировать</button>
            </div>
            <div class="ep-char-gen-status" id="ep-loc-status-${l.id}"></div>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function toggleEpLoc(locId, cb) {
  if (!S.episode.locations_used) S.episode.locations_used = [];
  if (cb.checked) {
    if (!S.episode.locations_used.includes(locId)) S.episode.locations_used.push(locId);
  } else {
    S.episode.locations_used = S.episode.locations_used.filter(x => x !== locId);
  }
  document.getElementById(`ep-loc-${locId}`)?.classList.toggle('in-episode', cb.checked);
}

// ── Items panel inside the episode tab ──────────────────────────────────────
// Mirrors the Locations panel layout (renderEpLocations + toggleEpLoc) but
// for series.items[] / episode.items_used[]. Items here are PLOT-RELEVANT
// objects (a locket, a USB stick with evidence, the stolen handbag) — not
// every random prop in frame. The auto-detect button sends the script to
// the backend's item extractor which decides what's plot-load-bearing vs
// background dressing.

function renderEpItems() {
  const el = document.getElementById('ep-items-list');
  if (!el) return;
  const items = S.series?.items || [];
  if (!items.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">'
      + 'Нет сюжетных предметов. Нажми 🔍 чтобы автоопределить из сценария или + для ручного добавления.</div>';
    return;
  }
  const used = S.episode?.items_used || [];
  el.innerHTML = items.map(it => {
    const hasRef = it.ref_images && it.ref_images.length > 0;
    const imgUrl = hasRef ? `${assetUrl(it.ref_images[0])}` : null;
    const inEp = used.includes(it.id);
    return `
      <div class="ep-loc-row ${inEp ? 'in-episode' : ''}" id="ep-item-${it.id}"
           ondragover="event.preventDefault();this.classList.add('drop-hover')"
           ondragleave="this.classList.remove('drop-hover')"
           ondrop="event.preventDefault();this.classList.remove('drop-hover');dropItemPhoto && dropItemPhoto(event,'${it.id}')">
        <div class="ep-loc-thumb"
             ${imgUrl ? `onclick="event.stopPropagation();openItemLightbox('${it.id}','${imgUrl}')" style="cursor:zoom-in"
                         title="Открыть предмет (можно перегенерировать с пожеланиями)"` : ''}>
          ${imgUrl
            ? `<img src="${imgUrl}" alt="" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">`
            : '🎒'}
        </div>
        <div class="ep-loc-name">
          <div class="ep-char-name-row">
            <input type="checkbox" ${inEp ? 'checked' : ''} onchange="toggleEpItem('${it.id}',this)">
            <span>${esc(it.name)}</span>
          </div>
          ${!hasRef ? `
            <div class="ep-char-gen-btns" id="ep-item-btns-${it.id}">
              <button class="btn-generate" id="ep-item-gen-btn-${it.id}" onclick="generateItemImage('${it.id}')">⚡ Сгенерировать</button>
            </div>
            <div class="ep-char-gen-status" id="ep-item-status-${it.id}"></div>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function toggleEpItem(itemId, cb) {
  if (!S.episode.items_used) S.episode.items_used = [];
  if (cb.checked) {
    if (!S.episode.items_used.includes(itemId)) S.episode.items_used.push(itemId);
  } else {
    S.episode.items_used = S.episode.items_used.filter(x => x !== itemId);
  }
  document.getElementById(`ep-item-${itemId}`)?.classList.toggle('in-episode', cb.checked);
}

// Item lightbox — full-size + regenerate-with-wishes (mirrors openLocLightbox).
function openItemLightbox(itemId, url) {
  closeLightbox();
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap">
        <img src="${url}" alt="${esc(it.name)}"
             onerror="this.replaceWith(_brokenImagePlaceholder('${url}'))">
        <div class="lb-caption"><strong>🎒 ${esc(it.name)}</strong></div>
      </div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать предмет</h3>
        <div class="hint">Старое фото удалится. Пожелания сохранятся в предмете.</div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">Что учесть / исправить</label>
          <textarea id="lb-regen-wishes" rows="5"
            placeholder="Например:&#10;«потёртый, не новый»&#10;«с инициалами М.К.»&#10;«цвет тёмно-бордовый»">${esc(it.image_constraints || '')}</textarea>
        </div>
        <div id="lb-regen-status" class="lb-status"></div>
        <button id="lb-regen-btn" class="btn-regen" onclick="regenerateItemFromLightbox('${itemId}')">↻ Перегенерировать</button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

async function regenerateItemFromLightbox(itemId) {
  const wishes = (document.getElementById('lb-regen-wishes')?.value || '').trim();
  const btn = document.getElementById('lb-regen-btn');
  const status = document.getElementById('lb-regen-status');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерирую...'; }
  if (status) status.textContent = '';
  try {
    const r = await api.post(`/api/series/${S.seriesId}/items/${itemId}/regenerate`, { wishes });
    bumpAssetVersion();
    if (r?.error) throw new Error(r.error);
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    if (typeof renderItemsList === 'function') renderItemsList();
    if (typeof renderEpItems === 'function') renderEpItems();
    if (status) status.textContent = '✓ Готово';
    const newUrl = r.url ? `${r.url}?t=${Date.now()}` : null;
    if (newUrl) openItemLightbox(itemId, newUrl);
  } catch (e) {
    if (status) status.textContent = '✗ ' + (e?.message || e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Перегенерировать'; }
  }
}

// Quick-add item from the episode sidebar (no full modal — just name + desc).
async function openQuickAddItem() {
  const name = (prompt('Название предмета (e.g. «флешка», «локет», «золотые часы»):') || '').trim();
  if (!name) return;
  const description = (prompt('Краткое описание (можно пусто):') || '').trim();
  try {
    const r = await api.post(`/api/series/${S.seriesId}/items`, { name, description });
    if (r?.error) throw new Error(r.error);
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    renderItemsList && renderItemsList();
    renderEpItems && renderEpItems();
    showToast(`✓ Добавлен «${name}»`);
  } catch (e) {
    alert('Ошибка добавления: ' + (e?.message || e));
  }
}

// Auto-detect plot-relevant items from the current episode's script.
async function autoDetectItems() {
  if (!S.episode?.number) { showToast('Открой эпизод сначала'); return; }
  const btn = document.querySelector('#ep-items-list')?.parentElement?.querySelector('button[onclick="autoDetectItems()"]');
  const orig = btn?.innerHTML;
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳'; }
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/detect-items`, {}
    );
    if (r?.error) throw new Error(r.error);
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    // Re-load episode so items_used updates show.
    const ep = await api.get(`/api/series/${S.seriesId}/episodes/${S.episode.number}`);
    if (ep) S.episode = ep;
    renderItemsList && renderItemsList();
    renderEpItems && renderEpItems();
    showToast(`✓ Найдено ${(r.detected || []).length} предметов`);
  } catch (e) {
    alert('Авто-детект не удался: ' + (e?.message || e));
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig || '🔍'; }
  }
}

// Stub for thumb-drag-drop (mirrors dropLocPhoto). If the matching upload
// endpoint exists, drop events will succeed; otherwise it's a no-op.
async function dropItemPhoto(event, itemId) {
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch(`/api/series/${S.seriesId}/assets/item/${itemId}`,
      { method: 'POST', body: fd });
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    renderItemsList && renderItemsList();
    renderEpItems && renderEpItems();
  } catch (e) {
    alert('Загрузка не удалась: ' + (e?.message || e));
  }
}

async function generateItemImage(itemId) {
  const btn = document.getElementById(`ep-item-gen-btn-${itemId}`);
  const status = document.getElementById(`ep-item-status-${itemId}`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  if (status) status.textContent = 'Генерирую...';
  try {
    const r = await api.post(`/api/series/${S.seriesId}/items/${itemId}/generate-image`, {});
    bumpAssetVersion();
    if (r?.error) throw new Error(r.error);
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    renderItemsList && renderItemsList();
    renderEpItems && renderEpItems();
  } catch (e) {
    if (status) status.textContent = '✗ ' + (e?.message || e);
    if (btn) { btn.disabled = false; btn.textContent = '⚡ Сгенерировать'; }
  }
}

function renderEpCharacters() {
  const el = document.getElementById('ep-characters-list');
  const chars = S.series?.characters || [];
  const used = S.episode.characters_used || [];
  if (!chars.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">Нет персонажей</div>';
    return;
  }
  el.innerHTML = '';
  chars.forEach(c => el.appendChild(buildEpCharCard(c, used.includes(c.id))));
}

async function extractFromStoryInline(btn) {
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try {
    const res = await api.post(`/api/series/${S.seriesId}/extract-from-story`, {});
    S.series = res.series;
    renderEpCharacters();
    renderEpLocations();
  renderEpItems();
    showToast(`Добавлено: ${res.added_characters.length} перс., ${res.added_locations.length} лок.`);
  } catch(e) {
    btn.disabled = false; btn.innerHTML = '🤖 Извлечь из сюжета';
    showToast('Ошибка: ' + e.message);
  }
}

function autoDetectFromScript() {
  const scriptUpper = (S.episode.script || '').toUpperCase();
  const chars = S.series.characters || [];
  const locs  = S.series.locations  || [];

  const nameInScript = (name) => {
    const u = name.toUpperCase();
    if (scriptUpper.includes(u)) return true;
    // Try without leading article (The, A, An)
    const noArticle = u.replace(/^(THE|AN?)\s+/, '');
    if (noArticle !== u && scriptUpper.includes(noArticle)) return true;
    // Try all significant words (>2 chars) present somewhere in the script
    const words = u.split(/\s+/).filter(p => p.length > 2);
    return words.length > 0 && words.every(p => scriptUpper.includes(p));
  };

  const detectedChars = chars.filter(c => nameInScript(c.name)).map(c => c.id);
  const detectedLocs  = locs.filter(l => nameInScript(l.name)).map(l => l.id);
  if (detectedChars.length) S.episode.characters_used = detectedChars;
  if (detectedLocs.length)  S.episode.locations_used  = detectedLocs;
}

async function autoExtractFromStory() {
  const charEl = document.getElementById('ep-characters-list');
  const locEl  = document.getElementById('ep-locations-list');
  if (charEl) charEl.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">⏳ Определяю персонажей и локации...</div>';
  if (locEl)  locEl.innerHTML  = '<div style="color:var(--muted);font-size:0.82rem">⏳ Загрузка...</div>';
  try {
    const res = await api.post(`/api/series/${S.seriesId}/extract-from-story`, {});
    S.series = res.series;
    renderEpCharacters();
    renderEpLocations();
  renderEpItems();
    if (res.added_characters.length || res.added_locations.length) {
      showToast(`Найдено: ${res.added_characters.length} перс., ${res.added_locations.length} лок.`);
    }
  } catch(e) {
    renderEpCharacters();
    renderEpLocations();
  renderEpItems();
  }
}

// Normalize character_outfits[cid] value to array — backward-compat with legacy single-string saves.
function _epOutfitIds(charId) {
  const v = (S.episode.character_outfits || {})[charId];
  if (!v) return [];
  if (Array.isArray(v)) return v.filter(Boolean).map(String);
  return [String(v)];
}

function buildEpCharCard(c, inEpisode) {
  const selectedOutfitIds = _epOutfitIds(c.id);
  const outfits = c.outfits || [];
  const selectedOutfits = selectedOutfitIds
    .map(oid => outfits.find(o => o.id === oid))
    .filter(Boolean);
  const primaryOutfit = selectedOutfits[0] || null;

  // Photo: first selected outfit's photo > base photo > placeholder
  let photoUrl = null;
  if (primaryOutfit?.photo) {
    photoUrl = `${assetUrl(primaryOutfit.photo)}`;
  } else if (c.ref_images?.length) {
    photoUrl = `${assetUrl(c.ref_images[0])}`;
  }
  const hasBasePhoto = !!(c.ref_images?.length);
  // Overlay shown if the primary outfit needs gen
  const needsOutfitGen = !!(primaryOutfit && !primaryOutfit.photo && !primaryOutfit.is_base && hasBasePhoto);

  const card = document.createElement('div');
  card.className = `ep-char-card ${inEpisode ? 'in-episode' : ''}`;
  card.id = `ep-char-${c.id}`;
  if (document.body.classList.contains('seedance-mode') && photoUrl) {
    card.draggable = true;
    card.addEventListener('dragstart', (ev) => {
      ev.dataTransfer.setData('application/json', JSON.stringify({
        kind: 'char', id: c.id, name: c.name, outfit: primaryOutfit?.label || null, photoUrl
      }));
    });
  }

  card.innerHTML = `
    <div class="ep-char-card-top" onclick="toggleEpCharCard('${c.id}')">
      <div class="ep-char-photo" ${photoUrl ? `onclick="event.stopPropagation();openCharAssets('${c.id}')" style="cursor:zoom-in"` : ''}
           ondragover="event.preventDefault();this.classList.add('drop-hover')"
           ondragleave="this.classList.remove('drop-hover')"
           ondrop="event.preventDefault();this.classList.remove('drop-hover');dropCharPhoto(event,'${c.id}')"
           title="${photoUrl ? 'Открыть карточку персонажа — образы, рефы, регенерация' : ''}">
        ${photoUrl
          ? `<img src="${photoUrl}" alt="${esc(c.name)}" onerror="this.replaceWith(_brokenImagePlaceholder('${photoUrl}'))">`
          : `<div class="no-photo">${primaryOutfit ? '👗' : '👤'}</div>`}
        <div class="ep-outfit-gen-status" id="ep-outfit-status-${c.id}" title=""
             style="position:absolute;bottom:0;left:0;right:0;max-height:32%;font-size:0.6rem;line-height:1.05;color:#fff;text-align:center;text-shadow:0 1px 2px rgba(0,0,0,0.9);background:rgba(0,0,0,0.55);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:1px 2px;"></div>
      </div>
      <div class="ep-char-body">
        <div class="ep-char-name-row">
          <input type="checkbox" ${inEpisode ? 'checked' : ''} onclick="event.stopPropagation();toggleEpCharCheck('${c.id}',this)">
          <span class="name">${esc(c.name)}</span>
        </div>
        ${outfits.length > 0 ? `
          <div class="ep-outfit-row ep-outfit-row-multi" onclick="event.stopPropagation()">
            <span class="ep-outfit-label" title="Можно выбрать несколько образов — отправятся в Reteller все">👗</span>
            <div class="ep-outfit-chips">
              ${outfits.map(o => {
                const on = selectedOutfitIds.includes(o.id);
                const isPrimary = on && primaryOutfit && primaryOutfit.id === o.id;
                const stateIcon = o.photo ? '✓' : (o.is_base ? '★' : '❗');
                return `<button type="button"
                  class="ep-outfit-chip ${on ? 'on' : ''} ${isPrimary ? 'primary' : ''}"
                  title="${on ? 'Снять выбор' : 'Добавить образ для этой серии'} — ${esc(o.label)}${o.photo ? ' (фото готово)' : (o.is_base ? ' (базовое фото)' : ' (фото не готово — кликни ❗)')}"
                  onclick="event.stopPropagation();toggleEpCharOutfit('${c.id}','${o.id}')">
                  ${esc(o.label)} ${stateIcon}
                </button>`;
              }).join('')}
            </div>
            ${primaryOutfit ? `
              <div class="ep-outfit-actions">
                <button class="btn-prompt" style="padding:2px 6px;font-size:0.7rem" title="Скопировать промпт для основного образа"
                        onclick="event.stopPropagation();showOutfitPrompt('${c.id}','${primaryOutfit.id}')">📋</button>
                ${hasBasePhoto && !primaryOutfit.is_base ? `<button class="btn-prompt" style="padding:2px 6px;font-size:0.7rem" title="Это базовый образ — связать с базовым фото без перегенерации"
                        onclick="event.stopPropagation();linkOutfitToBase('${c.id}','${primaryOutfit.id}')">🔗</button>` : ''}
                ${hasBasePhoto && !primaryOutfit.is_base ? `<button class="btn-prompt" style="padding:2px 6px;font-size:0.7rem" title="${primaryOutfit.photo ? 'Перегенерировать образ' : 'Сгенерировать образ (i2i из базы)'}"
                        onclick="event.stopPropagation();generateEpOutfit('${c.id}','${primaryOutfit.id}')">${primaryOutfit.photo ? '↻' : '⚡'}</button>` : ''}
              </div>
            ` : ''}
          </div>
          ${selectedOutfits.length > 1 ? `
            <div class="ep-outfit-multi-hint" title="Все выбранные образы уйдут в Reteller как отдельные референсы">
              👔 ${selectedOutfits.length} образа в этой серии — все попадут в Reteller
            </div>
          ` : ''}
        ` : ''}
        ${!hasBasePhoto ? `
          <div class="ep-char-gen-btns" id="ep-char-btns-${c.id}">
            <button class="btn-prompt" onclick="event.stopPropagation();showCharPrompt('${c.id}')">📋 Промпт</button>
            <button class="btn-generate" id="ep-gen-btn-${c.id}" onclick="event.stopPropagation();generateCharImage('${c.id}')">⚡ Сгенерировать</button>
          </div>
          <div class="ep-char-gen-status" id="ep-char-status-${c.id}"></div>
        ` : ''}
      </div>
    </div>
  `;
  return card;
}

async function generateEpOutfit(charId, outfitId) {
  const statusEl = document.getElementById(`ep-outfit-status-${charId}`);
  const overlay = document.getElementById(`ep-outfit-overlay-${charId}`);
  if (overlay) { overlay.style.pointerEvents = 'none'; overlay.innerHTML = '<span class="spinner"></span>'; }
  if (statusEl) statusEl.textContent = 'Переодеваем (~15с)...';
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/characters/${charId}/outfits/${outfitId}/generate`,
      {},
      { timeoutMs: 180000 }
    );
    if (res.ready) {
      // Refresh series so the new outfit.photo is reflected
      S.series = await api.get(`/api/series/${S.seriesId}`);
      const c = S.series.characters.find(x => x.id === charId);
      const used = S.episode.characters_used || [];
      const oldCard = document.getElementById(`ep-char-${charId}`);
      if (oldCard && c) oldCard.replaceWith(buildEpCharCard(c, used.includes(charId)));
      showToast('Готово — образ переодет');
    } else {
      const msg = res.error || 'Не удалось сгенерировать';
      if (statusEl) { statusEl.textContent = '✕ ошибка'; statusEl.title = msg; }
      if (overlay) { overlay.style.pointerEvents = ''; overlay.innerHTML = '⚡'; }
      showToast(msg.length > 200 ? msg.slice(0, 200) + '…' : msg);
    }
  } catch (e) {
    const msg = (e && e.message) || String(e);
    if (statusEl) { statusEl.textContent = '✕ ошибка'; statusEl.title = msg; }
    if (overlay) { overlay.style.pointerEvents = ''; overlay.innerHTML = '⚡'; }
    showToast(msg.length > 200 ? msg.slice(0, 200) + '…' : msg);
  }
}

// Toggle a single outfit on/off for this episode. Multiple outfits per character
// are allowed — each one is sent to Reteller as a separate visual reference so
// the model can render the character in different looks within one episode.
function toggleEpCharOutfit(charId, outfitId) {
  if (!S.episode.character_outfits) S.episode.character_outfits = {};
  // Normalize current value to array (legacy could be string)
  let current = S.episode.character_outfits[charId];
  if (!current) current = [];
  else if (!Array.isArray(current)) current = [String(current)];
  else current = current.slice();

  const idx = current.indexOf(outfitId);
  if (idx >= 0) {
    current.splice(idx, 1);
  } else {
    current.push(outfitId);
  }

  if (current.length) {
    S.episode.character_outfits[charId] = current;
  } else {
    delete S.episode.character_outfits[charId];
  }

  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  const used = S.episode.characters_used || [];
  const oldCard = document.getElementById(`ep-char-${charId}`);
  if (oldCard) oldCard.replaceWith(buildEpCharCard(c, used.includes(charId)));
}

// Backward-compat shim — keep old callers working in case any inline handlers survive
function setEpCharOutfit(charId, outfitId) {
  if (!S.episode.character_outfits) S.episode.character_outfits = {};
  if (outfitId) {
    S.episode.character_outfits[charId] = [outfitId];
  } else {
    delete S.episode.character_outfits[charId];
  }
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  const used = S.episode.characters_used || [];
  const oldCard = document.getElementById(`ep-char-${charId}`);
  if (oldCard) oldCard.replaceWith(buildEpCharCard(c, used.includes(charId)));
}

function toggleEpCharCard(charId) {
  const used = S.episode.characters_used || [];
  const inEpisode = used.includes(charId);
  const card = document.getElementById(`ep-char-${charId}`);
  const cb = card?.querySelector('input[type=checkbox]');
  if (inEpisode) {
    S.episode.characters_used = used.filter(x => x !== charId);
    card?.classList.remove('in-episode');
    if (cb) cb.checked = false;
  } else {
    S.episode.characters_used = [...used, charId];
    card?.classList.add('in-episode');
    if (cb) cb.checked = true;
  }
}

function toggleEpCharCheck(charId, cb) {
  const used = S.episode.characters_used || [];
  const card = document.getElementById(`ep-char-${charId}`);
  if (cb.checked) {
    if (!used.includes(charId)) S.episode.characters_used = [...used, charId];
    card?.classList.add('in-episode');
  } else {
    S.episode.characters_used = used.filter(x => x !== charId);
    card?.classList.remove('in-episode');
  }
}

async function dropCharPhoto(event, charId) {
  const file = event.dataTransfer.files[0];
  if (!file || !file.type.startsWith('image/')) return;
  const card = document.getElementById(`ep-char-${charId}`);
  const photoEl = card?.querySelector('.ep-char-photo');
  if (photoEl) photoEl.innerHTML = '<span class="spinner"></span>';
  const fd = new FormData();
  fd.append('photo', file);
  try {
    const res = await fetch(`/api/series/${S.seriesId}/characters/${charId}/upload-photo`, { method: 'POST', body: fd });
    bumpAssetVersion();
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    S.series = data.series;
    const c = S.series.characters.find(x => x.id === charId);
    const used = S.episode.characters_used || [];
    if (card && c) card.replaceWith(buildEpCharCard(c, used.includes(charId)));
    showToast(`${c?.name || 'Персонаж'} — фото обновлено`);
  } catch(e) {
    showToast('Ошибка загрузки: ' + e.message);
    if (photoEl) photoEl.innerHTML = '<div class="no-photo">👤</div>';
  }
}

async function dropLocPhoto(event, locId) {
  const file = event.dataTransfer.files[0];
  if (!file || !file.type.startsWith('image/')) return;
  const fd = new FormData();
  fd.append('photo', file);
  try {
    const res = await fetch(`/api/series/${S.seriesId}/locations/${locId}/upload-photo`, { method: 'POST', body: fd });
    bumpAssetVersion();
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    S.series = data.series;
    renderEpLocations();
  renderEpItems();
    const loc = S.series.locations?.find(x => x.id === locId);
    showToast(`${loc?.name || 'Локация'} — фото обновлено`);
  } catch(e) {
    showToast('Ошибка загрузки: ' + e.message);
  }
}

async function showCharPrompt(charId) {
  const result = await api.post(`/api/series/${S.seriesId}/generate-prompt`, { type: 'character', id: charId });
  showPromptModal(result.prompt);
}

function setRtlDuration(sec) {
  const el = document.getElementById('rtl-duration');
  if (el) el.value = sec;
  document.querySelectorAll('#rtl-duration-presets button').forEach(b => b.classList.toggle('active', b.textContent.includes(`· ${sec}с`) || b.textContent.includes(`· ${sec/60}мин`)));
}

async function linkOutfitToBase(charId, outfitId) {
  try {
    await api.post(`/api/series/${S.seriesId}/characters/${charId}/outfits/${outfitId}/use-base`, {});
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const c = S.series.characters.find(x => x.id === charId);
    const used = S.episode.characters_used || [];
    const oldCard = document.getElementById(`ep-char-${charId}`);
    if (oldCard && c) oldCard.replaceWith(buildEpCharCard(c, used.includes(charId)));
    showToast('Образ помечен как базовый — генерация не нужна');
  } catch (e) {
    alert('Ошибка: ' + (e?.message || e));
  }
}

async function showOutfitPrompt(charId, outfitId) {
  try {
    const result = await api.post(`/api/series/${S.seriesId}/generate-prompt`, { type: 'outfit', id: outfitId, char_id: charId });
    showPromptModal(result.prompt);
  } catch (e) {
    alert('Ошибка: ' + (e?.message || e));
  }
}

async function showLocPrompt(locId) {
  const result = await api.post(`/api/series/${S.seriesId}/generate-prompt`, { type: 'location', id: locId });
  showPromptModal(result.prompt);
}

async function generateLocImage(locId) {
  const btn = document.getElementById(`ep-loc-gen-btn-${locId}`);
  const statusEl = document.getElementById(`ep-loc-status-${locId}`);
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  statusEl.textContent = 'Генерируем (~15 сек)...';

  try {
    const res = await api.post(`/api/series/${S.seriesId}/locations/${locId}/generate-image`, {});
    bumpAssetVersion();
    if (res.ready) {
      statusEl.textContent = '';
      S.series = await api.get(`/api/series/${S.seriesId}`);
      renderEpLocations();
  renderEpItems();
      showToast('Локация — фото готово!');
    } else {
      statusEl.textContent = 'Ошибка: ' + (res.error || 'Неизвестная ошибка');
      btn.disabled = false; btn.innerHTML = '⚡ Сгенерировать';
    }
  } catch (e) {
    statusEl.textContent = 'Ошибка: ' + e.message;
    btn.disabled = false; btn.innerHTML = '⚡ Сгенерировать';
  }
}

async function generateCharImage(charId) {
  const btn = document.getElementById(`ep-gen-btn-${charId}`);
  const statusEl = document.getElementById(`ep-char-status-${charId}`);
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  statusEl.textContent = 'Генерируем (~15 сек)...';

  try {
    const res = await api.post(`/api/series/${S.seriesId}/characters/${charId}/generate-image`, {});
    bumpAssetVersion();
    if (res.ready) {
      statusEl.textContent = '';
      S.series = await api.get(`/api/series/${S.seriesId}`);
      const c = S.series.characters.find(x => x.id === charId);
      const used = S.episode.characters_used || [];
      const oldCard = document.getElementById(`ep-char-${charId}`);
      if (oldCard && c) oldCard.replaceWith(buildEpCharCard(c, used.includes(charId)));
      showToast(`${c?.name || 'Персонаж'} — фото готово!`);
    } else {
      statusEl.textContent = 'Ошибка: ' + (res.error || 'Неизвестная ошибка');
      btn.disabled = false; btn.innerHTML = '⚡ Сгенерировать';
    }
  } catch (e) {
    statusEl.textContent = 'Ошибка: ' + e.message;
    btn.disabled = false; btn.innerHTML = '⚡ Сгенерировать';
  }
}

// pollReteller removed — image generation now goes directly through AVAI (synchronous ~15s)
// Legacy: kept save-frame endpoints in backend for any existing Reteller projects

async function _legacyPollReteller(projectId, assetType, assetId, btn, statusEl, btnLabel, onDone) {
  let attempts = 0;
  const MAX = 60;

  async function checkOnce() {
    attempts++;
    if (attempts > MAX) {
      clearInterval(poll);
      statusEl.textContent = 'Таймаут.';
      btn.disabled = false; btn.innerHTML = btnLabel;
      return;
    }
    try {
      const status = await api.get(`/api/series/${S.seriesId}/reteller/status/${projectId}`);
      const s = status.status;
      if (s === 'completed') {
        clearInterval(poll);
        statusEl.textContent = 'Скачиваем кадр...';
        try {
          const saved = await api.post(`/api/series/${S.seriesId}/${assetType}/${assetId}/save-frame/${projectId}`, {});
          if (saved.ready) { statusEl.textContent = ''; await onDone(); }
          else { statusEl.textContent = 'Нет кадров.'; btn.disabled = false; btn.innerHTML = btnLabel; }
        } catch(e) { statusEl.textContent = 'Ошибка: ' + e.message; btn.disabled = false; btn.innerHTML = btnLabel; }
      } else if (s === 'error' || s === 'failed') {
        clearInterval(poll);
        statusEl.textContent = 'Ошибка генерации.';
        btn.disabled = false; btn.innerHTML = btnLabel;
      } else { statusEl.textContent = `Статус: ${s || 'processing'}...`; }
    } catch(e) { statusEl.textContent = 'Ошибка: ' + e.message; }
  }

  // Check immediately, then every 6 seconds
  await checkOnce();
  const poll = setInterval(checkOnce, 6000);
}

function toggleEpCharacter(charId, el) {
  const used = S.episode.characters_used || [];
  const cb = el.querySelector('input');
  if (used.includes(charId)) {
    S.episode.characters_used = used.filter(x => x !== charId);
    cb.checked = false;
    el.classList.remove('checked');
  } else {
    S.episode.characters_used = [...used, charId];
    cb.checked = true;
    el.classList.add('checked');
  }
}

function renderEpReteller() {
  const rtl = S.episode.reteller || {};
  const el = document.getElementById('ep-reteller-info');
  if (!rtl.project_id) {
    el.innerHTML = '<div>Не отправлен в Reteller</div>';
    return;
  }
  el.innerHTML = `
    <div>ID: <code style="font-size:0.75rem">${rtl.project_id.slice(0,12)}…</code></div>
    <div>Статус: <strong>${statusLabel(rtl.status)}</strong></div>
    ${rtl.project_url ? `<div style="margin-top:4px"><a href="${rtl.project_url}" target="_blank" class="btn-accent btn-sm">Открыть драфт в Reteller →</a></div>` : ''}
    ${rtl.video_url ? `<div><a href="${rtl.video_url}" target="_blank">▶ Смотреть видео</a></div>` : ''}
    ${rtl.submitted_at ? `<div style="color:var(--muted);font-size:0.78rem">${new Date(rtl.submitted_at).toLocaleString('ru')}</div>` : ''}
    <button class="btn-ghost btn-sm" style="margin-top:6px" onclick="pollEpReteller('${rtl.project_id}')">↻ Обновить статус</button>
  `;
}

async function pollEpReteller(projectId) {
  const data = await api.get(`/api/series/${S.seriesId}/reteller/status/${projectId}`);
  S.episode.reteller.status = data.status;
  if (data.videoUrl) S.episode.reteller.video_url = data.videoUrl;
  renderEpReteller();
}

function updateScriptCounter() {
  const len = (val('ep-script') || '').length;
  document.getElementById('script-char-count').textContent = len.toLocaleString('ru');
  // Show "Apply changes" button when textarea diverges from saved episode script
  const applyBtn = document.getElementById('ep-script-apply-btn');
  if (applyBtn && S.episode) {
    const dirty = val('ep-script') !== (S.episode.script || '');
    applyBtn.style.display = dirty ? '' : 'none';
    applyBtn.dataset.dirty = dirty ? '1' : '';
  }
}
document.addEventListener('DOMContentLoaded', () => {
  const ta = document.getElementById('ep-script');
  if (ta) ta.addEventListener('input', updateScriptCounter);
});

function collectEpisodeForm() {
  return {
    title: val('ep-title-input'),
    synopsis: val('ep-synopsis'),
    script: val('ep-script'),
    reteller_prompt: val('ep-reteller-prompt'),
    scene_blocking: val('ep-scene-blocking'),
    ready: !!document.getElementById('ep-ready-toggle')?.checked,
    characters_used: S.episode.characters_used || [],
    character_outfits: S.episode.character_outfits || {},
    locations_used: S.episode.locations_used || [],
  };
}

async function getEpRettellerPrompt() {
  const btn = document.getElementById('ep-rtl-prompt-btn');
  const origText = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try {
    await saveEpisodeSilent();
    const res = await api.post(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/reteller-prompt`, {}, { timeoutMs: 165_000 });
    showPromptModal(res.prompt);
  } catch(e) {
    alert('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false; btn.innerHTML = origText;
  }
}

async function generateEpRettellerPrompt() {
  const btn = document.getElementById('ep-rtl-gen-btn');
  const status = document.getElementById('ep-rtl-status');
  if (!btn) return;
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  status.textContent = 'Генерирую (~2 мин)...'; status.style.color = 'var(--muted)';
  const taskCtx = { seriesId: S.seriesId, episodeNum: S.episodeNum, seriesTitle: S.series?.title };
  try {
    await saveEpisodeSilent();
    const res = await trackTask('Промпт ретеллера', taskCtx, () =>
      api.post(`/api/series/${taskCtx.seriesId}/episodes/${taskCtx.episodeNum}/reteller-prompt`, {}, { timeoutMs: 165_000 })
    );
    const stillOnEp = S.seriesId === taskCtx.seriesId && S.episodeNum === taskCtx.episodeNum && !document.getElementById('view-episode')?.classList.contains('hidden');
    if (stillOnEp) {
      setVal('ep-reteller-prompt', res.prompt);
      if (S.episode) S.episode.reteller_prompt = res.prompt;
      status.textContent = '✓ Готово'; status.style.color = 'var(--success)';
      setTimeout(() => { const s = document.getElementById('ep-rtl-status'); if (s) s.textContent = ''; }, 4000);
    }
  } catch(e) {
    const stillOnEp = S.seriesId === taskCtx.seriesId && S.episodeNum === taskCtx.episodeNum;
    if (stillOnEp && status) { status.textContent = 'Ошибка: ' + e.message; status.style.color = 'var(--danger)'; }
  } finally {
    const stillOnEp = S.seriesId === taskCtx.seriesId && S.episodeNum === taskCtx.episodeNum;
    if (stillOnEp && btn) { btn.disabled = false; btn.innerHTML = '⚡ Сгенерировать'; }
  }
}

async function saveEpisode() {
  const data = collectEpisodeForm();
  S.episode = await api.put(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`, data);
  const badge = document.getElementById('ep-status-badge');
  badge.className = `status-badge status-${S.episode.status}`;
  badge.textContent = statusLabel(S.episode.status);
  showToast('Сохранено');
}

async function saveEpisodeSilent() {
  const data = collectEpisodeForm();
  S.episode = await api.put(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`, data);
}

// Ctrl+S save
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 's') {
    e.preventDefault();
    if (!document.getElementById('view-episode').classList.contains('hidden')) {
      saveEpisode();
    }
  }
});

// ── Reteller drawer ───────────────────────────────────────────────────────────
let rtlPollingInterval = null;

async function openAssetFolder(type) {
  try {
    await api.post(`/api/series/${S.seriesId}/open-folder`, { type });
  } catch(e) {
    showToast('Ошибка: ' + e.message);
  }
}

function openReteller() {
  // Init range from episode list
  const nums = S.episodes.map(e => e.number);
  const minN = nums.length ? Math.min(...nums) : 1;
  const maxN = nums.length ? Math.max(...nums) : 1;
  document.getElementById('range-from').value = minN;
  document.getElementById('range-to').value = maxN;
  document.getElementById('range-from').max = maxN;
  document.getElementById('range-to').max = maxN;

  // Populate settings from series defaults
  const st = S.series.settings;
  document.getElementById('rtl-duration').value = st.duration || 90;
  document.getElementById('rtl-language').value = st.language || 'Russian';
  document.getElementById('rtl-style').value = S.series.style.type || 'cinematic';
  document.getElementById('rtl-image-provider').value = st.image_provider || 'seedream';
  document.getElementById('rtl-voice').value = st.voice || 'Enceladus';
  document.getElementById('rtl-tts-provider').value = st.tts_provider || 'elevenlabs';
  document.getElementById('rtl-aspect').value = st.aspect_ratio || '9:16';
  document.getElementById('rtl-music').checked = st.enable_music !== false;
  document.getElementById('rtl-animation').checked = !!st.enable_animation;
  document.getElementById('rtl-multivoice').checked = !!st.multi_voice;

  document.getElementById('rtl-results').innerHTML = '';
  document.getElementById('reteller-overlay').classList.remove('hidden');
  document.getElementById('reteller-drawer').classList.remove('hidden');
  updateRangePreview();
}

function closeReteller() {
  document.getElementById('reteller-overlay').classList.add('hidden');
  document.getElementById('reteller-drawer').classList.add('hidden');
  if (rtlPollingInterval) { clearInterval(rtlPollingInterval); rtlPollingInterval = null; }
}

async function updateRangePreview() {
  const from = parseInt(document.getElementById('range-from').value);
  const to = parseInt(document.getElementById('range-to').value);
  if (!from || !to || from > to) return;

  const data = await api.post(`/api/series/${S.seriesId}/reteller/preview`, { from, to });
  renderRangePreview(data);
}

function renderRangePreview(data) {
  // Episode chips
  const chips = data.episodes.map(ep => {
    const hasScript = ep.script && ep.script.length > 10;
    return `<div class="range-ep-chip ${hasScript ? 'has-script' : 'no-script'}">
      Эп.${ep.number}: ${esc(ep.title)}${hasScript ? '' : ' ⚠️'}
    </div>`;
  }).join('');
  document.getElementById('range-episodes-preview').innerHTML = chips || '<span style="color:var(--muted)">Нет эпизодов в этом диапазоне</span>';

  // Character asset slots
  const charGrid = document.getElementById('asset-characters');
  if (!data.characters.length) {
    charGrid.innerHTML = '<span style="color:var(--muted);font-size:0.85rem">В этих эпизодах нет персонажей</span>';
  } else {
    charGrid.innerHTML = '';
    data.characters.forEach(c => {
      charGrid.appendChild(buildAssetCard(c, 'character'));
    });
  }

  // Location asset slots
  const locGrid = document.getElementById('asset-locations');
  if (!data.locations || !data.locations.length) {
    locGrid.innerHTML = '<span style="color:var(--muted);font-size:0.85rem">В этих эпизодах нет локаций</span>';
  } else {
    locGrid.innerHTML = '';
    data.locations.forEach(l => {
      locGrid.appendChild(buildAssetCard(l, 'location'));
    });
  }

  // Style refs
  const styleGrid = document.getElementById('asset-style');
  styleGrid.innerHTML = (data.style.ref_urls || []).map((u, i) => {
    const fname = (data.style.ref_images || [])[i]?.split('/').pop() || '';
    return `<div class="photo-thumb-wrap">
      <img src="${u}" alt="" style="width:56px;height:56px;object-fit:cover;border-radius:6px;border:1px solid var(--border)">
      <button class="del-btn" onclick="deleteStyleRefFromDrawer('${fname}')">✕</button>
    </div>`;
  }).join('');
}

// Build a character or location asset card (filled or placeholder with drag&drop)
function buildAssetCard(item, type) {
  const uploadFn = type === 'character'
    ? (files) => uploadCharRefFromDrawer(item.id, files)
    : (files) => uploadLocRefFromDrawer(item.id, files);

  if (item.has_refs) {
    // Filled card
    const card = document.createElement('div');
    card.className = 'asset-char-card ready';
    const outfits = (item.outfits_in_range || []);
    const outfitsHtml = type === 'character' && outfits.length ? `
      <div class="asset-char-outfits" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;padding-top:6px;border-top:1px dashed var(--border)">
        ${outfits.map(o => {
          const epList = o.episodes.map(n => `Эп.${n}`).join(', ');
          const labelTxt = `${o.label || '—'} · ${epList}`;
          if (o.has_photo) {
            return `<div class="outfit-mini" title="${esc(o.label)} — ${epList}\n${esc(o.description||'')}" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:60px">
              <img src="${o.photo_url}" alt="" style="width:54px;height:54px;object-fit:cover;border-radius:6px;border:1px solid var(--border)">
              <div style="font-size:0.65rem;color:var(--muted);text-align:center;line-height:1.1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%">${esc(o.label)}</div>
              <div style="font-size:0.6rem;color:var(--accent)">${esc(epList)}</div>
            </div>`;
          }
          return `<div class="outfit-mini missing" title="${esc(o.label)} — нет фото\n${esc(o.description||'')}" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:60px">
            <div style="width:54px;height:54px;border-radius:6px;border:1px dashed var(--warn,#f5a623);display:flex;align-items:center;justify-content:center;background:rgba(245,166,35,0.08);font-size:1.2rem">⚡</div>
            <div style="font-size:0.65rem;color:var(--muted);text-align:center;line-height:1.1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%">${esc(o.label)}</div>
            <div style="font-size:0.6rem;color:var(--accent)">${esc(epList)}</div>
          </div>`;
        }).join('')}
      </div>` : '';
    card.innerHTML = `
      <div class="asset-char-name">${esc(item.name)}</div>
      <div class="asset-char-refs">
        ${(item.ref_urls || []).map(u => `<img class="asset-ref-thumb" src="${u}" alt="">`).join('')}
      </div>
      ${outfitsHtml}
      <label class="asset-upload-btn">
        <input type="file" accept="image/*" multiple hidden>
        + Добавить
      </label>`;
    card.querySelector('input[type=file]').addEventListener('change', e => uploadFn(e.target.files));
    return card;
  }

  // Placeholder with drag & drop
  const placeholder = document.createElement('div');
  placeholder.className = 'asset-placeholder';
  placeholder.innerHTML = `
    <div class="placeholder-icon">${type === 'character' ? '👤' : '📍'}</div>
    <div class="placeholder-label">${esc(item.name)}</div>
    <div class="placeholder-hint">Перетащи фото сюда или нажми загрузить</div>
    <div class="placeholder-actions">
      <label class="btn-upload">
        <input type="file" accept="image/*" multiple hidden>
        ⬆ Загрузить
      </label>
      <button class="btn-gen-prompt" data-id="${item.id}" data-type="${type}">
        ✨ Промпт для Banana 2
      </button>
    </div>`;

  // File input
  placeholder.querySelector('input[type=file]').addEventListener('change', e => uploadFn(e.target.files));

  // Drag & drop
  placeholder.addEventListener('dragover', e => { e.preventDefault(); placeholder.classList.add('drag-over'); });
  placeholder.addEventListener('dragleave', () => placeholder.classList.remove('drag-over'));
  placeholder.addEventListener('drop', e => {
    e.preventDefault();
    placeholder.classList.remove('drag-over');
    if (e.dataTransfer.files.length) uploadFn(e.dataTransfer.files);
  });

  // Generate prompt button
  placeholder.querySelector('.btn-gen-prompt').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const origText = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>';
    btn.disabled = true;
    try {
      const result = await api.post(`/api/series/${S.seriesId}/generate-prompt`, {
        type: btn.dataset.type,
        id: btn.dataset.id,
      });
      showPromptModal(result.prompt);
    } catch (err) {
      alert('Ошибка: ' + err.message);
    } finally {
      btn.innerHTML = origText;
      btn.disabled = false;
    }
  });

  return placeholder;
}

async function uploadLocRefFromDrawer(locId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/location/${locId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  updateRangePreview();
}

async function uploadCharRefFromDrawer(charId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/character/${charId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  updateRangePreview();
}

async function uploadStyleRefs(files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/style`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  updateRangePreview();
}

async function deleteStyleRefFromDrawer(filename) {
  await api.del(`/api/series/${S.seriesId}/assets/style/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  updateRangePreview();
}

async function generateRangePrompt() {
  const from = parseInt(document.getElementById('range-from').value);
  const to   = parseInt(document.getElementById('range-to').value);
  if (!from || !to || from > to) return alert('Укажи корректный диапазон');
  const btn    = document.getElementById('rtl-range-prompt-btn');
  const status = document.getElementById('rtl-range-prompt-status');
  const result = document.getElementById('rtl-range-prompt-result');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = ''; result.classList.add('hidden');
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title };
    const res = await trackTask(`Промпт диапазона ${from}–${to}`, ctx, () =>
      api.post(`/api/series/${S.seriesId}/reteller/range-prompt`, { from, to })
    );
    document.getElementById('rtl-range-prompt-text').value = res.prompt;
    const chars = (res.characters || []).map(c => c.name).join(', ');
    const locs  = (res.locations  || []).map(l => l.name).join(', ');
    document.getElementById('rtl-range-cast').innerHTML =
      (chars ? `<div><strong>Персонажи:</strong> ${esc(chars)}</div>` : '') +
      (locs  ? `<div><strong>Локации:</strong> ${esc(locs)}</div>`   : '');
    result.classList.remove('hidden');
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.className = 'pipeline-status err';
  } finally {
    btn.disabled = false; btn.innerHTML = '📋 Сгенерировать промпт для Reteller';
  }
}

function copyRangePrompt() {
  const ta = document.getElementById('rtl-range-prompt-text');
  navigator.clipboard.writeText(ta.value).then(() => showToast('Скопировано!')).catch(() => {
    ta.select(); document.execCommand('copy'); showToast('Скопировано!');
  });
}

async function submitEpisodeToReteller() {
  if (!S.episodeNum) return alert('Открой эпизод');
  const btn = document.getElementById('ep-rtl-submit-btn');
  const statusEl = document.getElementById('ep-rtl-submit-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Отправляем...';
  statusEl.textContent = '';

  // Use series defaults; user can tweak in the drawer for batch mode
  const st = S.series.settings || {};
  const settings = {
    duration:             st.duration || 90,
    language:             st.language || 'Russian',
    style:                (S.series.style && S.series.style.type) || 'cinematic',
    image_provider:       st.image_provider || 'banana',
    voice:                st.voice || 'Enceladus',
    tts_provider:         st.tts_provider || 'elevenlabs',
    aspect_ratio:         st.aspect_ratio || '9:16',
    enable_music:         st.enable_music === true,
    enable_animation:     st.enable_animation !== false,
    animation_speed:      st.animation_speed || 'fast',
    animation_resolution: st.animation_resolution || '480p',
    animation_model:      st.animation_model || 'seedance-2-ref',
    cinema:               !!st.cinema,
    trim:                 st.trim !== false,
    no_fades:             st.no_fades !== false,
    multi_voice:          !!st.multi_voice,
  };

  try {
    const num = S.episodeNum;
    const results = await api.post(`/api/series/${S.seriesId}/reteller/submit`, { from: num, to: num, settings });
    const r = results[0] || {};
    if (r.error) {
      statusEl.innerHTML = `<span style="color:var(--danger)">${esc(r.error.slice(0,200))}</span>`;
    } else {
      const link = r.project_url
        ? `<a href="${r.project_url}" target="_blank" class="btn-accent btn-sm" style="margin-top:6px;display:inline-block">Открыть драфт →</a>`
        : '';
      statusEl.innerHTML = `✓ Драфт создан в Reteller. ${link}`;
      // refresh episode state
      S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
      S.episode = await api.get(`/api/series/${S.seriesId}/episodes/${num}`);
      renderEpReteller();
      renderEpisodesList();
    }
  } catch (e) {
    statusEl.innerHTML = `<span style="color:var(--danger)">Ошибка: ${esc(e.message || e)}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '📝 Создать драфт этой серии';
  }
}

async function submitToReteller() {
  const from = parseInt(document.getElementById('range-from').value);
  const to = parseInt(document.getElementById('range-to').value);
  if (!from || !to || from > to) return alert('Укажи корректный диапазон');

  const btn = document.getElementById('rtl-submit-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Отправляем...';

  const st = S.series.settings || {};
  const settings = {
    duration:             parseInt(document.getElementById('rtl-duration').value),
    language:             document.getElementById('rtl-language').value,
    style:                document.getElementById('rtl-style').value,
    image_provider:       document.getElementById('rtl-image-provider').value,
    voice:                document.getElementById('rtl-voice').value,
    tts_provider:         document.getElementById('rtl-tts-provider').value,
    aspect_ratio:         document.getElementById('rtl-aspect').value,
    enable_music:         document.getElementById('rtl-music').checked,
    enable_animation:     document.getElementById('rtl-animation').checked,
    multi_voice:          document.getElementById('rtl-multivoice').checked,
    // Reteller animation settings — read from series (no UI fields yet, defaults from screenshot)
    animation_speed:      st.animation_speed || 'fast',
    animation_resolution: st.animation_resolution || '480p',
    animation_model:      st.animation_model || 'seedance-2-ref',
    cinema:               !!st.cinema,
    trim:                 st.trim !== false,
    no_fades:             st.no_fades !== false,
  };

  try {
    const results = await api.post(`/api/series/${S.seriesId}/reteller/submit`, { from, to, settings });
    renderSubmitResults(results);
    S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
    renderEpisodesList();
    // No polling — drafts wait for the user to start them on Reteller's page
    showToast('Драфты созданы в Reteller — открой ссылки и запусти вручную');
  } catch (e) {
    alert('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '📝 Создать драфты в Reteller';
  }
}

function renderSubmitResults(results) {
  const el = document.getElementById('rtl-results');
  el.innerHTML = results.map(r => `
    <div class="rtl-result-row" id="rtl-row-${r.episode}">
      <div class="rtl-result-ep">Эп. ${r.episode}</div>
      <div class="rtl-result-status">
        ${r.error
          ? `<span style="color:var(--danger)">${esc(r.error.slice(0,80))}</span>`
          : `<span class="status-badge status-${r.reteller_status||'draft'}">${statusLabel(r.reteller_status||'draft')}</span>`}
      </div>
      ${r.project_url
        ? `<a class="btn-ghost btn-sm" href="${r.project_url}" target="_blank" onclick="event.stopPropagation()">Открыть в Reteller →</a>`
        : (r.project_id ? `<div class="rtl-result-link" id="rtl-link-${r.episode}"></div>` : '')}
    </div>
  `).join('');
}

function startPolling(results) {
  const pending = results.filter(r => r.project_id && r.reteller_status !== 'completed' && r.reteller_status !== 'error');
  if (!pending.length) return;

  rtlPollingInterval = setInterval(async () => {
    let allDone = true;
    for (const r of pending) {
      try {
        const data = await api.get(`/api/series/${S.seriesId}/reteller/status/${r.project_id}`);
        updateResultRow(r.episode, data);
        if (data.status !== 'completed' && data.status !== 'error') allDone = false;
        else {
          S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
          renderEpisodesList();
        }
      } catch (_) { allDone = false; }
    }
    if (allDone) { clearInterval(rtlPollingInterval); rtlPollingInterval = null; }
  }, 8000);
}

function updateResultRow(epNum, data) {
  const statusEl = document.querySelector(`#rtl-row-${epNum} .rtl-result-status`);
  if (statusEl) statusEl.innerHTML = `<span class="status-badge status-${data.status}">${statusLabel(data.status)}</span>`;
  if (data.videoUrl) {
    const linkEl = document.getElementById(`rtl-link-${epNum}`);
    if (linkEl) linkEl.innerHTML = `<a href="${data.videoUrl}" target="_blank">▶ Видео</a>`;
  }
}

// ── Prompt modal ──────────────────────────────────────────────────────────────
function showPromptModal(prompt) {
  document.getElementById('prompt-text').value = prompt;
  openModal('modal-prompt');
}

async function translateField(fieldId, label) {
  const text = (document.getElementById(fieldId)?.value || '').trim();
  if (!text) { showToast('Нет текста для перевода'); return; }
  document.getElementById('translate-modal-title').textContent = `Перевод — ${label}`;
  const contentEl = document.getElementById('translate-content');
  contentEl.textContent = '⏳ Переводим...';
  openModal('modal-translate');
  try {
    const res = await api.post('/api/translate', { text });
    contentEl.textContent = res.translation;
  } catch(e) {
    contentEl.textContent = 'Ошибка: ' + e.message;
  }
}

function copyTranslation() {
  const text = document.getElementById('translate-content')?.textContent || '';
  navigator.clipboard.writeText(text).then(() => showToast('Скопировано!'));
}

function copyPrompt() {
  const ta = document.getElementById('prompt-text');
  ta.select();
  navigator.clipboard.writeText(ta.value).then(() => showToast('Скопировано!')).catch(() => {
    document.execCommand('copy');
    showToast('Скопировано!');
  });
}

// ── Settings ──────────────────────────────────────────────────────────────────
// Instant-apply for the sounds toggle so user can test it from modal-settings
// without having to hit "Save" (uncovered API keys still need Save).
function onSoundsToggleChanged(on) {
  Sounds.setEnabled(!!on);
  // Tiny audible confirmation that toggle works (only when turning ON)
  if (on) {
    try { Sounds.playSuccess(); } catch (e) {}
  }
}

async function openSettings() {
  // Server returns masked status — never the raw key. We show "•••••abcd" as
  // placeholder; user types a fresh key only when rotating. Empty input on
  // save = no change (we only POST non-empty fields).
  const cfg = await api.get('/api/config');
  const rtlInp = document.getElementById('settings-rtl-key');
  const avaiInp = document.getElementById('settings-avai-key');
  if (rtlInp) {
    rtlInp.value = '';
    rtlInp.placeholder = cfg.reteller_key_masked
      ? `Текущий: ${cfg.reteller_key_masked} (оставь пустым чтобы не менять)`
      : (cfg.is_primary && cfg.has_reteller_key ? 'Используется глобальный env-ключ' : 'rtl_sk_...');
  }
  if (avaiInp) {
    avaiInp.value = '';
    avaiInp.placeholder = cfg.avai_key_masked
      ? `Текущий: ${cfg.avai_key_masked} (оставь пустым чтобы не менять)`
      : (cfg.is_primary && cfg.has_avai_key ? 'Используется глобальный env-ключ' : 'avai-...');
  }
  // Anthropic key стайс global — поле спрятано/не нужно (operator-only)
  const soundsCb = document.getElementById('settings-sounds-enabled');
  if (soundsCb) soundsCb.checked = Sounds.isEnabled();
  const voiceCb = document.getElementById('settings-voice-enabled');
  if (voiceCb) voiceCb.checked = Sounds.isVoiceEnabled();
  const mlgCb = document.getElementById('settings-mlg-hitmarker');
  if (mlgCb) mlgCb.checked = Sounds.isHitmarkerEnabled();
  openModal('modal-settings');
}

async function saveSettings() {
  const payload = {};
  const rtlVal = val('settings-rtl-key').trim();
  const avaiVal = (val('settings-avai-key') || '').trim();
  if (rtlVal) payload.reteller_key = rtlVal;
  if (avaiVal) payload.avai_key = avaiVal;
  if (Object.keys(payload).length) {
    await api.post('/api/config', payload);
  }
  // Local-only settings (no server roundtrip needed)
  const soundsCb = document.getElementById('settings-sounds-enabled');
  if (soundsCb) Sounds.setEnabled(!!soundsCb.checked);
  const voiceCb = document.getElementById('settings-voice-enabled');
  if (voiceCb) Sounds.setVoiceEnabled(!!voiceCb.checked);
  const mlgCb = document.getElementById('settings-mlg-hitmarker');
  if (mlgCb) Sounds.setHitmarkerEnabled(!!mlgCb.checked);
  closeModal('modal-settings');
  loadBalance();
}

async function loadBalance() {
  try {
    const data = await api.get('/api/reteller/balance');
    const el = document.getElementById('balance-badge');
    if (data.tokens !== undefined) el.textContent = `${data.tokens} токенов`;
    else el.textContent = '';
  } catch (_) {}
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

// Close modal on backdrop click
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal')) closeModal(e.target.id);
});

// ── DOM helpers ───────────────────────────────────────────────────────────────
function val(id) { return (document.getElementById(id)?.value || '').trim(); }
function setVal(id, v) { const el = document.getElementById(id); if (el) el.value = v ?? ''; }
function clearFields(ids) { ids.forEach(id => setVal(id, '')); }
function esc(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function showToast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  Object.assign(t.style, {
    position:'fixed', bottom:'24px', right:'24px', background:'var(--surface3)',
    border:'1px solid var(--border)', color:'var(--text)', padding:'10px 20px',
    borderRadius:'8px', zIndex:'999', fontSize:'0.9rem', boxShadow:'var(--shadow)',
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2000);
}

// ── Sound effects ───────────────────────────────────────────────────────────
// Synthesized via Web Audio API — no external assets to bundle/serve.
// User toggle stored in localStorage as 'sounds_enabled' (default ON).
const Sounds = (() => {
  let ctx = null;
  function _ensureCtx() {
    if (ctx) return ctx;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    ctx = new Ctx();
    return ctx;
  }
  function isEnabled() {
    // Default ON: only OFF when user explicitly stored 'false'
    return localStorage.getItem('sounds_enabled') !== 'false';
  }
  function setEnabled(on) {
    localStorage.setItem('sounds_enabled', on ? 'true' : 'false');
  }
  // Schedule a tone at offset `t0`, freq, duration (sec), gain.
  // Sine + small attack/release envelope so it doesn't click.
  function _tone(t0, freq, dur, gain = 0.18) {
    const c = _ensureCtx();
    if (!c) return;
    const osc = c.createOscillator();
    const g = c.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, t0);
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(gain, t0 + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g).connect(c.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.05);
  }
  // Pleasant ascending major-third — used on completion of a generation
  function playSuccess() {
    if (!isEnabled()) return;
    const c = _ensureCtx();
    if (!c) return;
    if (c.state === 'suspended') c.resume().catch(() => {});
    const t = c.currentTime;
    _tone(t,        523.25, 0.18, 0.20); // C5
    _tone(t + 0.10, 659.25, 0.18, 0.20); // E5
    _tone(t + 0.20, 783.99, 0.32, 0.22); // G5
  }
  // Lower descending dissonant pair — used on generation failure
  function playError() {
    if (!isEnabled()) return;
    const c = _ensureCtx();
    if (!c) return;
    if (c.state === 'suspended') c.resume().catch(() => {});
    const t = c.currentTime;
    _tone(t,        311.13, 0.22, 0.22); // Eb4
    _tone(t + 0.14, 233.08, 0.40, 0.22); // Bb3
  }
  // Distinct 6-note fanfare for whole-episode completion (auto-mode all-done).
  // Differentiates from per-chunk playSuccess so user knows the BIG finish vs
  // a single segment finishing.
  function playFanfare() {
    if (!isEnabled()) return;
    const c = _ensureCtx();
    if (!c) return;
    if (c.state === 'suspended') c.resume().catch(() => {});
    const t = c.currentTime;
    _tone(t,        523.25, 0.14, 0.20); // C5
    _tone(t + 0.08, 659.25, 0.14, 0.20); // E5
    _tone(t + 0.16, 783.99, 0.14, 0.20); // G5
    _tone(t + 0.24, 1046.5, 0.14, 0.22); // C6
    _tone(t + 0.40, 783.99, 0.14, 0.20); // G5
    _tone(t + 0.48, 1046.5, 0.55, 0.26); // C6 — long
  }
  // Speak a phrase via the browser's SpeechSynthesis API (free, offline,
  // works in Chrome/Safari/Firefox). Voice is picked by language; falls
  // back to default if no English voice available. Independent of the
  // chime-sounds toggle — has its own setting.
  function isVoiceEnabled() {
    return localStorage.getItem('voice_announce_enabled') !== 'false';
  }
  function setVoiceEnabled(on) {
    localStorage.setItem('voice_announce_enabled', on ? 'true' : 'false');
  }
  function speak(text, opts = {}) {
    if (!isVoiceEnabled()) return;
    if (!('speechSynthesis' in window)) return;
    try {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = opts.lang || 'en-US';
      u.rate = opts.rate || 1.0;
      u.pitch = opts.pitch || 1.0;
      u.volume = opts.volume != null ? opts.volume : 0.9;
      // Pick an English voice when available
      const voices = window.speechSynthesis.getVoices();
      const en = voices.find(v => /^en[-_]/i.test(v.lang)) || voices[0];
      if (en) u.voice = en;
      window.speechSynthesis.speak(u);
    } catch (e) { /* ignore */ }
  }
  // MLG hitmarker — real CoD/Halo "tink!" sample. Pre-loaded on first call
  // and reused for every click. Cloned per-play so rapid clicks overlap
  // (HTMLAudio can only play one stream at a time per element).
  // Audio sample loader — uses Web Audio API AudioBuffer when pre-decoded
  // (zero-latency, fully in-memory), falls back to HTMLAudioElement clone
  // for samples that haven't finished decoding yet. The preload phase
  // (_preloadMlgAssets) decodes all known MLG sounds into AudioBuffers up-front,
  // so by the first user click everything plays instantly.
  const _audioElCache = {};
  function _playSample(path, volume = 0.7) {
    // Hot path: AudioBuffer is decoded and ready in window._mlgBufferCache
    const buf = window._mlgBufferCache && window._mlgBufferCache[path];
    if (buf) {
      const c = _ensureCtx();
      if (!c) return;
      if (c.state === 'suspended') c.resume().catch(() => {});
      try {
        const src = c.createBufferSource();
        const g = c.createGain();
        src.buffer = buf;
        g.gain.value = volume;
        src.connect(g).connect(c.destination);
        src.start(0);
      } catch {}
      return;
    }
    // Fallback: HTMLAudioElement (used until AudioBuffer finishes decoding)
    if (!_audioElCache[path]) {
      const preloaded = window._mlgAudioPreload && window._mlgAudioPreload[path];
      if (preloaded) {
        _audioElCache[path] = preloaded;
      } else {
        try {
          _audioElCache[path] = new Audio(path);
          _audioElCache[path].preload = 'auto';
          _audioElCache[path].load();
        } catch { return; }
      }
    }
    try {
      const clone = _audioElCache[path].cloneNode(true);
      clone.volume = volume;
      clone.play().catch(() => {});
    } catch {}
  }
  function playHitmarker() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/hitmarker.mp3', 0.7);
  }
  function playGunshot() {
    if (!isHitmarkerEnabled()) return;   // gated by same MLG toggle
    _playSample('/static/sounds/gunshot.mp3', 0.85);
  }
  // "Oh baby a triple!" — plays on every 3rd Snoop kill. Louder than the
  // others because the original sample is mixed quiet.
  function playTriple() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/triple.mp3', 1.0);
  }
  // "WOW" Owen Wilson — random chance on Snoop kills (when not a triple).
  function playWow() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/wow.mp3', 0.95);
  }
  // "DAMN SON! WHERE'D YOU FIND THIS!" — alternate random reaction.
  function playDamnSon() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/damnson.mp3', 0.95);
  }
  // "GET NO-SCOPED!" — alternate random reaction.
  function playNoScoped() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/noscoped.mp3', 0.95);
  }
  // "OH MY GOD" — plays on the 2nd kill of a DOUBLE event (1/25 spawn).
  // File path is the standard /static/sounds/omg.mp3 — drop the asset there
  // when ready. Gracefully no-ops if the file 404s (Sounds._playSample's
  // try/catch handles that already).
  function playOmg() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/omg.mp3', 1.0);
  }
  // "WOBO COMBO" — plays starting on the 2nd kill of a WOBO event (1/100).
  // Loops through the rest of the chain (5 → 2 → 1 spawns) as ambience.
  function playWoboCombo() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/wobo-combo.mp3', 1.0);
  }
  function isHitmarkerEnabled() {
    return localStorage.getItem('mlg_hitmarker_enabled') === '1';
  }
  function setHitmarkerEnabled(on) {
    localStorage.setItem('mlg_hitmarker_enabled', on ? '1' : '0');
  }
  return { playSuccess, playError, playFanfare, speak,
           isEnabled, setEnabled, isVoiceEnabled, setVoiceEnabled,
           playHitmarker, playGunshot, playTriple, playWow, playDamnSon, playNoScoped,
           playOmg, playWoboCombo,
           isHitmarkerEnabled, setHitmarkerEnabled };
})();

// ── MLG hitmarker — visual overlay + sound on button clicks ─────────────────
// 4-line cross expanding from click point, fades over 220ms. Pure CSS
// transform animation, no library. Only fires when toggle is on.
(function _initHitmarker() {
  document.addEventListener('click', (e) => {
    if (!Sounds.isHitmarkerEnabled()) return;
    // Catch all clickable controls — buttons, anything with onclick handler,
    // common row-style cards (episode list, character/loc rows, breadcrumb
    // crumb-links). The previous selector was buttons-only and missed
    // navigation rows / breadcrumbs / cards.
    const target = e.target.closest(
      'button, [onclick], [role="button"], a, ' +
      '.btn-primary, .btn-accent, .btn-ghost, .btn-icon, .btn-sm, .btn-danger, .btn-idea-gen, .btn-idea-random, ' +
      '.episode-row, .char-item, .crumb.link, .nav-logo, .idea-card, .sd-card, .outfit-card'
    );
    if (!target || target.disabled) return;
    // Exclude form inputs that bubble up clicks — clicking a checkbox shouldn't fire hitmarker
    const tag = target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'LABEL') return;
    // Big "create / open editor" actions get the gunshot sound; everything
    // else gets the regular hitmarker tink. Visual X overlay always plays.
    const action = target.getAttribute('data-mlg-action');
    if (action === 'gunshot') {
      Sounds.playGunshot();
    } else {
      Sounds.playHitmarker();
    }
    _spawnHitmarkerVisual(e.clientX, e.clientY);
  }, true);
}());

function _spawnHitmarkerVisual(x, y) {
  // PNG sprite — simpler and looks right (CoD/Halo X with rounded ends).
  // CSS centers it on (x, y) via negative margins; pure animation does the rest.
  const wrap = document.createElement('div');
  wrap.className = 'mlg-hitmarker';
  wrap.style.left = x + 'px';
  wrap.style.top = y + 'px';
  document.body.appendChild(wrap);
  setTimeout(() => wrap.remove(), 360);
}

// ── MLG Snoop — random-spawn dancing target ──────────────────────────────
// Pops in at a random screen position every ~25-60s (only when MLG 420 MODE
// is on). Clicking him plays gunshot + hitmarker visual + despawns him.
// Self-removes after ~12s if nobody shoots him.
let _snoopSpawnTimer = null;
function _scheduleSnoop() {
  if (_snoopSpawnTimer) return;
  const tick = () => {
    if (!Sounds.isHitmarkerEnabled()) {
      _snoopSpawnTimer = setTimeout(tick, 5000);  // re-check soon
      return;
    }
    // Don't spawn if one is already on screen. _rollAndSpawn (defined below)
    // rolls the rare-combo dice (1/100 wobo, 1/25 double) and falls through
    // to a regular single spawn otherwise.
    if (!document.querySelector('.mlg-snoop')) {
      if (typeof _rollAndSpawn === 'function') _rollAndSpawn();
      else _spawnSnoop();
    }
    const nextDelay = 25000 + Math.random() * 35000;   // 25-60s
    _snoopSpawnTimer = setTimeout(tick, nextDelay);
  };
  // First spawn 8-15s after page load (only if MLG mode on)
  _snoopSpawnTimer = setTimeout(tick, 8000 + Math.random() * 7000);
}

// Kill-streak counter for MLG targets (currently Snoop, more characters to
// come). Every 3rd kill triggers "Triple!" voice. Stored both locally (for
// instant streak math) AND server-side (per-user, drives the leaderboard).
function _mlgKillCount() {
  return parseInt(localStorage.getItem('mlg_kills') || '0', 10) || 0;
}
function _bumpMlgKill(targetKind = 'snoop') {
  const next = _mlgKillCount() + 1;
  localStorage.setItem('mlg_kills', String(next));
  // Fire-and-forget POST — don't block the gunshot UI on network. Auth is
  // implicit via session cookie. Failure is silent (kill still counts locally).
  try {
    fetch('/api/mlg/kill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: targetKind }),
    }).catch(() => {});
  } catch {}
  return next;
}
// Migrate legacy key (was 'mlg_snoop_kills' before character pool generalized).
(function _migrateKillCounter() {
  const old = localStorage.getItem('mlg_snoop_kills');
  if (old != null && localStorage.getItem('mlg_kills') == null) {
    localStorage.setItem('mlg_kills', old);
  }
})();

// Pretty-print a slugged user dir back into something readable.
// e.g. "v_privalov_gamegears_online" → "v.privalov@gamegears.online"
// Best-effort — slug is irreversible, but for our org pattern it's close.
function _prettifyUserSlug(slug) {
  if (!slug) return '?';
  // Replace last "_" group (domain) with "@"
  // e.g. v_privalov_gamegears_online → v.privalov@gamegears.online
  // Heuristic: find " _gamegears_online" or "_dev" suffix
  const m = slug.match(/^(.+?)_([a-z0-9]+(?:_[a-z]{2,4})?)$/);
  if (m && /^(gamegears|com|org|net|dev|ru|io)/.test(m[2])) {
    return m[1].replace(/_/g, '.') + '@' + m[2].replace(/_/g, '.');
  }
  return slug.replace(/_/g, '.');
}

// Enable MLG mode from inside the leaderboard modal — flips the toggle, plays
// a confirmation gunshot so the user immediately knows it's live, then
// re-renders the leaderboard without the explainer.
function _enableMlgFromLeaderboard() {
  Sounds.setHitmarkerEnabled(true);
  // Sync the Settings checkbox if it's been opened previously
  const cb = document.getElementById('settings-mlg-hitmarker');
  if (cb) cb.checked = true;
  // Confirmation shot
  Sounds.playGunshot();
  // Re-render leaderboard sans explainer
  setTimeout(openMlgLeaderboard, 200);
}

async function openMlgLeaderboard() {
  const list = document.getElementById('mlg-leaderboard-list');
  if (!list) return;
  list.innerHTML = '<div style="color:var(--muted)">⏳ Загружаю...</div>';
  openModal('modal-mlg-leaderboard');

  // If MLG mode is OFF — show explainer + activation CTA above the table.
  // Builds the user's understanding of what they're opting into without
  // forcing them to dig through Settings.
  const mlgOff = !Sounds.isHitmarkerEnabled();
  const explainerHtml = mlgOff ? `
    <div style="background:linear-gradient(135deg, rgba(124,92,252,0.18), rgba(0,212,170,0.12));
                border:1px solid rgba(124,92,252,0.4); border-radius:8px;
                padding:14px 16px; margin-bottom:14px; font-size:0.9rem; line-height:1.55">
      <div style="font-weight:700;font-size:1rem;margin-bottom:6px">🌿 MLG 420 MODE — что это?</div>
      <div style="color:var(--text)">
        Дизайнерский прикол + анти-стресс во время работы. Когда включён:
      </div>
      <ul style="margin:8px 0 8px 18px;padding:0;color:var(--text)">
        <li>На каждый клик по кнопке — хитмаркер «tink!» + крестик-вспышка (как в CoD/Halo)</li>
        <li>На больших действиях («Создать сериал», «+ Эпизод», «🎬 Редактор») — звук выстрела</li>
        <li>Иногда вылазит Snoop Dogg или MLG-лягушка в случайном углу — кликни их и стреляй</li>
        <li>За убийства начисляются киллы. На каждом 10-м — большая радужная надпись OMG!!! / DAMN SON!!</li>
        <li>Случайные голосовые комментарии: WOW / NO SCOPED / DAMN SON / TRIPLE на 3-м килле</li>
        <li>Все киллы попадают в эту таблицу лидеров среди коллег</li>
      </ul>
      <div style="color:var(--muted);font-size:0.84rem;margin-bottom:10px">
        Помогает разрядиться когда сценарий не пишется или генерация падает 5 раз подряд.
        Можно выключить в любой момент в ⚙ Настройках.
      </div>
      <button class="btn-primary" onclick="_enableMlgFromLeaderboard()" style="width:100%">
        🌿 Включить MLG 420 MODE и начать стрелять
      </button>
    </div>
  ` : '';

  try {
    const r = await fetch('/api/mlg/leaderboard').then(r => r.json());
    const rows = r.leaderboard || [];
    if (!rows.length) {
      list.innerHTML = explainerHtml + `
        <div style="text-align:center;color:var(--muted);padding:24px 0">
          <div style="font-size:36px;margin-bottom:8px">🎯</div>
          ${mlgOff
            ? 'В таблице пока пусто. Включи MLG MODE — будешь первым.'
            : "Пока никто не убил ни одного. Стреляй по Snoop'у/лягушке — попадёшь в таблицу первым."}
        </div>`;
      return;
    }
    const me = (window._currentUser && window._currentUser.email) || '';
    const meSlug = me.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    const medals = ['🥇', '🥈', '🥉'];
    list.innerHTML = explainerHtml + `
      <table style="width:100%;border-collapse:collapse;font-size:0.92rem">
        <thead>
          <tr style="border-bottom:1px solid var(--border);color:var(--muted);text-align:left">
            <th style="padding:6px 8px;width:40px"></th>
            <th style="padding:6px 8px">Игрок</th>
            <th style="padding:6px 8px;text-align:right">Киллы</th>
            <th style="padding:6px 8px;color:var(--muted);font-weight:normal" title="Когда последний кил">когда</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row, i) => {
            const isMe = row.user === meSlug;
            const rank = medals[i] || `#${i + 1}`;
            const when = row.last_kill
              ? new Date(row.last_kill * 1000).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })
              : '—';
            return `
              <tr style="border-bottom:1px solid var(--border);${isMe ? 'background:rgba(124,92,252,0.10);font-weight:600' : ''}">
                <td style="padding:8px;font-size:1.1rem">${rank}</td>
                <td style="padding:8px;${isMe ? 'color:var(--accent)' : ''}">${esc(_prettifyUserSlug(row.user))}${isMe ? ' (это ты)' : ''}</td>
                <td style="padding:8px;text-align:right;font-family:monospace">${row.kills}</td>
                <td style="padding:8px;color:var(--muted);font-size:0.82rem">${when}</td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>
      <div style="margin-top:10px;font-size:0.78rem;color:var(--muted);text-align:center">
        ${rows.length} игрок${rows.length === 1 ? '' : (rows.length < 5 ? 'а' : 'ов')} с убийствами
      </div>`;
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">✗ ${e.message || e}</div>`;
  }
}

// MLG target skins. Each entry is rolled at spawn time. To add another
// character — drop GIF into static/img, append { kind, src, width? } here.
const _MLG_SKINS = [
  { kind: 'snoop', src: '/static/img/snoop.gif', width: 110 },
  { kind: 'frog',  src: '/static/img/frog.gif',  width: 110 },
];

// ── Combo events (rare scheduled multi-spawns) ──────────────────────────────
// Combo events are NOT rolled per-spawn-tick — they live on independent
// schedulers so we can preload their sound files ~20s before the event
// fires (rather than bloating the page-load budget with audio for events
// the user might never see in a session).
//
//   DOUBLE  (every 8-20 min): 2 spawn → kill #2 fires OMG (random of two
//                             sound variants) → 3rd target sneaks in 300ms
//                             after the 2nd kill. Per-kill text overlay
//                             picks a hype line.
//   WOBO    (every 30-90 min): 3 spawn → 2nd kill of wave-1 starts the
//                              wobo-combo loop sound → wave-1 cleared →
//                              2 spawn → wave-2 cleared → 1 final spawn.
//
// State: { type, soundUrl, kills, stage, ... }
const _MLG_COMBOS = {};

// Hype lines flashed during DOUBLE combos. Each kill flashes one at random.
const _OMG_TEXT_LINES = [
  'MY GOD!', 'OH MY GOD!!', 'JESUS!', 'HOLY SHIT!', 'FUCKING HELL!',
  'INSANE!', 'NO WAY!', 'WHAT THE—!', 'GOD DAMN!', 'UNREAL!!',
];
const _OMG_SOUND_URLS = [
  '/static/sounds/omg-fuckn.mp3',
  '/static/sounds/omg-mlg.mp3',
];
const _WOBO_SOUND_URL = '/static/sounds/wobo-combo.mp3';

// Lazy preloader — fetches the URL into HTTP cache + Web Audio buffer, but
// only when called. Used by combo schedulers ~20s before each event.
async function _lazyPreloadSound(url) {
  if (!url) return;
  if (window._mlgBufferCache && window._mlgBufferCache[url]) return; // already loaded
  try { await fetch(url, { credentials: 'same-origin', cache: 'force-cache' }); } catch {}
  // HTMLAudioElement fallback
  if (!window._mlgAudioPreload) window._mlgAudioPreload = {};
  if (!window._mlgAudioPreload[url]) {
    try { const a = new Audio(url); a.preload = 'auto'; a.load(); window._mlgAudioPreload[url] = a; } catch {}
  }
  // AudioBuffer for zero-latency playback
  try {
    if (!window._mlgBufferCache) window._mlgBufferCache = {};
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = window._mlgPreloadCtx || (window._mlgPreloadCtx = new Ctx());
    const res = await fetch(url, { credentials: 'same-origin', cache: 'force-cache' });
    if (!res.ok) return;
    const ab = await res.arrayBuffer();
    const buf = await new Promise((resolve, reject) => {
      try {
        const p = ctx.decodeAudioData(ab, resolve, reject);
        if (p && typeof p.then === 'function') p.then(resolve, reject);
      } catch (e) { reject(e); }
    });
    window._mlgBufferCache[url] = buf;
  } catch {}
}

// Plays a sample BY URL (not by hardcoded path inside Sounds.*) — needed for
// random-variant OMG + the on-demand wobo. Mirrors Sounds._playSample but
// scoped to a URL passed in at runtime. Honours the same MLG-toggle gate.
function _playSampleByUrl(url, vol = 1.0) {
  if (!Sounds.isHitmarkerEnabled()) return;
  // Buffer path
  try {
    const buf = window._mlgBufferCache && window._mlgBufferCache[url];
    if (buf) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = window._mlgPlaybackCtx || (window._mlgPlaybackCtx = new Ctx());
      if (ctx.state === 'suspended') ctx.resume();
      const src = ctx.createBufferSource(); src.buffer = buf;
      const gain = ctx.createGain(); gain.gain.value = vol;
      src.connect(gain); gain.connect(ctx.destination); src.start(0);
      return;
    }
  } catch {}
  // HTMLAudioElement fallback (works without buffer cache).
  try {
    const cached = window._mlgAudioPreload && window._mlgAudioPreload[url];
    const a = cached ? cached.cloneNode() : new Audio(url);
    a.volume = vol;
    a.play().catch(() => {});
  } catch {}
}

// Spawn a combo. soundUrl is the pre-chosen + pre-loaded sound; required
// because the scheduler picks the variant at scheduling time so it can
// preload the right one.
function _spawnCombo(type, soundUrl) {
  const id = `${type}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  if (type === 'double') {
    _MLG_COMBOS[id] = { type, kills: 0, total: 2, followUpQueued: false, soundUrl };
    for (let i = 0; i < 2; i++) {
      setTimeout(() => _spawnSnoop({ comboId: id }), i * 120);
    }
    _spawnMlgVoiceText('DOUBLE SPAWN!');
  } else if (type === 'wobo') {
    _MLG_COMBOS[id] = { type, kills: 0, total: 3, stage: 1, soundStarted: false, soundUrl };
    for (let i = 0; i < 3; i++) {
      setTimeout(() => _spawnSnoop({ comboId: id }), i * 120);
    }
    _spawnMlgVoiceText('★ WOBO COMBO ★');
  }
}

function _onComboKill(combo, comboId) {
  if (!combo) return;
  combo.kills += 1;
  // ── DOUBLE event ───────────────────────────────────────────────────────
  if (combo.type === 'double') {
    // Per-kill hype text overlay — fires after EVERY kill in a DOUBLE combo.
    const line = _OMG_TEXT_LINES[Math.floor(Math.random() * _OMG_TEXT_LINES.length)];
    setTimeout(() => _spawnMlgVoiceText(line), 80);
    if (combo.kills === 2 && !combo.followUpQueued) {
      combo.followUpQueued = true;
      // OMG sound + followup spawn appears suddenly (300ms gap).
      setTimeout(() => _playSampleByUrl(combo.soundUrl, 1.0), 100);
      setTimeout(() => _spawnSnoop({ comboId }), 300);
      combo.total = 3;
    }
    if (combo.kills >= 3) {
      // Combo cleared: drop a chunky finish line overlay, then queue the next.
      const finish = _DOUBLE_FINISH_LINES[Math.floor(Math.random() * _DOUBLE_FINISH_LINES.length)];
      setTimeout(() => _spawnMlgFlash({ big: finish, small: 'DOUBLE COMBO COMPLETE' }), 300);
      delete _MLG_COMBOS[comboId];
      _scheduleNextDoubleCombo();
    }
  }
  // ── WOBO event: 3 → 2 → 1 ──────────────────────────────────────────────
  else if (combo.type === 'wobo') {
    if (combo.stage === 1 && combo.kills === 2 && !combo.soundStarted) {
      combo.soundStarted = true;
      _playSampleByUrl(combo.soundUrl, 1.0);
    }
    if (combo.stage === 1 && combo.kills >= 3) {
      combo.stage = 2; combo.kills = 0; combo.total = 2;
      setTimeout(() => _spawnMlgVoiceText('+2 INCOMING!'), 100);
      for (let i = 0; i < 2; i++) {
        setTimeout(() => _spawnSnoop({ comboId }), 250 + i * 120);
      }
    } else if (combo.stage === 2 && combo.kills >= 2) {
      combo.stage = 3; combo.kills = 0; combo.total = 1;
      setTimeout(() => _spawnMlgVoiceText('FINISH HIM!'), 100);
      setTimeout(() => _spawnSnoop({ comboId }), 300);
    } else if (combo.stage === 3 && combo.kills >= 1) {
      // Crown moment — chunky three-line overlay celebrates clearing the
      // entire 3+2+1 chain. Comes in 250ms after the kill so it doesn't
      // visually collide with the gunshot+hitmarker animation.
      setTimeout(() => _spawnMlgFlash({
        big: '★ WOBO COMBO MEISTER ★',
        small: 'CHAIN CLEARED · 6 IN A ROW',
      }), 250);
      delete _MLG_COMBOS[comboId];
      _scheduleNextWoboCombo();
    }
  }
}

// Overlay headlines used when a DOUBLE combo finishes successfully. Each
// completion picks one at random — variety so the user doesn't see the same
// line every time.
const _DOUBLE_FINISH_LINES = [
  'TRIPLE THREAT', 'GG EZ', 'TOO EASY', 'GET REKT',
  'NO MERCY', 'STAY DOWN', 'OWNED', '3X DOWN',
  'CLEAN SWEEP', 'GODLIKE',
];

// ── Combo schedulers ────────────────────────────────────────────────────────
// Independent timers per combo type. The same flow on each tick:
//   1. Pick the sound variant (DOUBLE has two — pick at schedule time).
//   2. Pick a delay (T) within range.
//   3. Schedule a preload at T-20s (so the file is in cache + AudioBuffer).
//   4. Schedule the actual event firing at T.
// MLG-toggle is checked at firing time; if off, the event is silently
// dropped and rescheduled for the next slot.
let _doubleComboTimer = null, _doublePreloadTimer = null;
let _woboComboTimer   = null, _woboPreloadTimer   = null;

function _scheduleNextDoubleCombo() {
  if (_doubleComboTimer) clearTimeout(_doubleComboTimer);
  if (_doublePreloadTimer) clearTimeout(_doublePreloadTimer);
  // Range: 8-20 minutes between DOUBLE events.
  const delayMs = (8 * 60 * 1000) + Math.random() * (12 * 60 * 1000);
  const soundUrl = _OMG_SOUND_URLS[Math.floor(Math.random() * _OMG_SOUND_URLS.length)];
  // Preload 20s before fire (or right now if delay < 20s, which it never is here).
  const preloadAt = Math.max(0, delayMs - 20000);
  _doublePreloadTimer = setTimeout(() => { _lazyPreloadSound(soundUrl); }, preloadAt);
  const fire = () => {
    if (!Sounds.isHitmarkerEnabled()) { _scheduleNextDoubleCombo(); return; }
    // Don't fire if a target is already on screen — let it clear, retry in 30s.
    if (document.querySelector('.mlg-snoop')) {
      _doubleComboTimer = setTimeout(fire, 30000);
      return;
    }
    _spawnCombo('double', soundUrl);
  };
  _doubleComboTimer = setTimeout(fire, delayMs);
}

function _scheduleNextWoboCombo() {
  if (_woboComboTimer) clearTimeout(_woboComboTimer);
  if (_woboPreloadTimer) clearTimeout(_woboPreloadTimer);
  // Range: 30-90 minutes between WOBO events (rare).
  const delayMs = (30 * 60 * 1000) + Math.random() * (60 * 60 * 1000);
  const preloadAt = Math.max(0, delayMs - 20000);
  _woboPreloadTimer = setTimeout(() => { _lazyPreloadSound(_WOBO_SOUND_URL); }, preloadAt);
  const fire = () => {
    if (!Sounds.isHitmarkerEnabled()) { _scheduleNextWoboCombo(); return; }
    if (document.querySelector('.mlg-snoop')) {
      _woboComboTimer = setTimeout(fire, 60000);
      return;
    }
    _spawnCombo('wobo', _WOBO_SOUND_URL);
  };
  _woboComboTimer = setTimeout(fire, delayMs);
}

// NOTE: combo schedulers boot was moved to _bootMlgFeatureWhenIdle() so
// MLG never starts working until window 'load' + idle callback fired —
// keeps page-load lightweight.

function _spawnSnoop(opts = {}) {
  const comboId = opts.comboId || null;
  // Pick a random skin from the pool — same kill-logic for all of them.
  const skin = _MLG_SKINS[Math.floor(Math.random() * _MLG_SKINS.length)];
  const sn = document.createElement('img');
  sn.src = skin.src;
  sn.className = 'mlg-snoop';
  sn.dataset.kind = skin.kind;
  if (comboId) sn.dataset.comboId = comboId;
  sn.alt = skin.kind;
  sn.style.width = (skin.width || 110) + 'px';
  // Random position — keep him fully on-screen, away from edges
  const W = window.innerWidth, H = window.innerHeight;
  const sw = skin.width || 110;
  const sh = 160;
  sn.style.left = (40 + Math.random() * (W - sw - 80)) + 'px';
  sn.style.top  = (60 + Math.random() * (H - sh - 120)) + 'px';
  sn.addEventListener('click', (e) => {
    e.stopPropagation();
    Sounds.playGunshot();
    _spawnHitmarkerVisual(e.clientX, e.clientY);
    const kills = _bumpMlgKill(skin.kind);

    // Combo-event hook: drives DOUBLE / WOBO follow-up spawns and sounds.
    // Suppresses the regular voice-line lottery so combo announcer voices
    // don't talk over each other.
    let suppressRegularAnnouncer = false;
    if (comboId && _MLG_COMBOS[comboId]) {
      _onComboKill(_MLG_COMBOS[comboId], comboId);
      suppressRegularAnnouncer = true;
    }

    // Kill announcer logic — sound + matching text overlay (skipped during combo):
    let played = false;
    if (!suppressRegularAnnouncer) {
      if (kills === 3) {
        setTimeout(() => { Sounds.playTriple(); _spawnMlgVoiceText('TRIPLE!!!'); }, 180);
        played = true;
      } else if (kills > 3 && kills % 3 === 0 && Math.random() < 0.35) {
        setTimeout(() => { Sounds.playTriple(); _spawnMlgVoiceText('TRIPLE!!!'); }, 180);
        played = true;
      }
      if (!played && Math.random() < 0.32) {
        const voiceLines = [
          { play: Sounds.playWow,      text: 'WOW!!!' },
          { play: Sounds.playDamnSon,  text: 'DAMN SON!!' },
          { play: Sounds.playNoScoped, text: 'NO SCOPED!!' },
        ];
        const pick = voiceLines[Math.floor(Math.random() * voiceLines.length)];
        setTimeout(() => { pick.play(); _spawnMlgVoiceText(pick.text); }, 220);
      }
    }
    // Round-number milestone overlay — every 5 kills (always shown, even mid-combo).
    if (kills > 0 && kills % 5 === 0) {
      _spawnMlgMilestone(kills);
    }
    sn.classList.add('mlg-snoop-shot');
    setTimeout(() => sn.remove(), 280);
  }, { once: false });
  document.body.appendChild(sn);
  // Auto-despawn after 12s if user ignores him. Combo targets get 18s grace
  // because the user might be busy clicking siblings.
  const despawnMs = comboId ? 18000 : 12000;
  setTimeout(() => {
    if (sn.parentNode) {
      sn.classList.add('mlg-snoop-fade');
      setTimeout(() => sn.remove(), 600);
    }
  }, despawnMs);
}

// Per-tick spawn: always a single target. Combo events live on their own
// schedulers (see _scheduleNextDoubleCombo / _scheduleNextWoboCombo) so
// their sound files can be preloaded ~20s before firing rather than at
// page load — keeps initial page weight down.
function _rollAndSpawn() {
  _spawnSnoop();
}

// MLG entire feature is gated behind page-fully-loaded. User reported page
// load was tracking sluggish — pushing all MLG asset preload + spawn
// schedulers to fire ONLY after window 'load' (which waits for all images,
// CSS, fonts, deferred scripts to settle). Inside that handler we further
// defer to requestIdleCallback so we don't compete with the first
// interactive paint for the user. Combined effect: page is responsive
// before any MLG fetch/decode work begins.

let _mlgBootStarted = false;
function _bootMlgFeatureWhenIdle() {
  if (_mlgBootStarted) return;
  _mlgBootStarted = true;
  const start = () => {
    // Order matters: assets first (so when scheduler fires its 12.5-min
    // interval the buffers are ready), then the spawn-tick scheduler, then
    // combo schedulers. _preloadMlgAssets is async — combo schedulers don't
    // need to await it (they preload their own sound at T-20s anyway).
    _preloadMlgAssets().finally(() => {
      _scheduleSnoop();
      _scheduleNextDoubleCombo();
      _scheduleNextWoboCombo();
    });
  };
  // Prefer requestIdleCallback so we yield to the browser's first paint /
  // post-load layout work. Fallback to a generous setTimeout in browsers
  // that don't have it (Safari < 17 etc).
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(start, { timeout: 4000 });
  } else {
    setTimeout(start, 1500);
  }
}
if (document.readyState === 'complete') {
  // Page already loaded by the time this script ran (rare with our defer
  // pattern, but possible with cached pages / back-button restore).
  _bootMlgFeatureWhenIdle();
} else {
  // 'load' fires after every <img>, <link>, deferred script settles —
  // strictly later than DOMContentLoaded.
  window.addEventListener('load', _bootMlgFeatureWhenIdle, { once: true });
}

// Preload ALL MLG assets up-front so first-play has zero latency.
// Strategy: Web Audio AudioBuffer (fully decoded, plays instantly) +
// HTMLAudioElement fallback for samples still decoding. Without buffers,
// HTMLAudioElement.play() has 100-500ms first-call latency even with
// preload='auto' because browsers defer actual buffer fill until play().
const _MLG_AUDIO_URLS = [
  '/static/sounds/hitmarker.mp3',
  '/static/sounds/gunshot.mp3',
  '/static/sounds/triple.mp3',
  '/static/sounds/wow.mp3',
  '/static/sounds/damnson.mp3',
  '/static/sounds/noscoped.mp3',
  '/static/sounds/wait-a-minute.mp3',
];
const _MLG_IMAGE_URLS = [
  '/static/img/hitmarker.png',
  '/static/img/snoop.gif',
  '/static/img/frog.gif',
];

async function _preloadMlgAssets() {
  // Phase 1: HTTP cache warmup — fetch into browser cache so subsequent
  // requests are instant. Doesn't require user-gesture or audio context.
  for (const url of _MLG_AUDIO_URLS.concat(_MLG_IMAGE_URLS)) {
    try { fetch(url, { credentials: 'same-origin', cache: 'force-cache' }).catch(() => {}); }
    catch {}
  }
  // Phase 2: HTMLAudioElement fallback cache (for the ~50ms window before
  // AudioBuffers finish decoding).
  if (!window._mlgAudioPreload) window._mlgAudioPreload = {};
  for (const url of _MLG_AUDIO_URLS) {
    try {
      const a = new Audio(url);
      a.preload = 'auto';
      a.load();
      window._mlgAudioPreload[url] = a;
    } catch {}
  }
  // Phase 3: image prefetch + decode
  for (const url of _MLG_IMAGE_URLS) {
    try {
      const img = new Image();
      img.decoding = 'async';
      img.src = url;
    } catch {}
  }
  // Phase 4: decode each MP3 into an AudioBuffer for true zero-latency
  // playback. AudioContext can be created without user-gesture (it'll be
  // 'suspended' until first play, but decodeAudioData() works regardless).
  if (!window._mlgBufferCache) window._mlgBufferCache = {};
  let ctx = null;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) ctx = new Ctx();
  } catch {}
  if (!ctx) return;
  // Decode in parallel — fetch + arrayBuffer + decodeAudioData are all async.
  // Use force-cache so we re-use Phase 1's downloads.
  await Promise.all(_MLG_AUDIO_URLS.map(async (url) => {
    try {
      const res = await fetch(url, { credentials: 'same-origin', cache: 'force-cache' });
      if (!res.ok) return;
      const ab = await res.arrayBuffer();
      // Safari needs the callback signature; modern browsers accept Promise.
      const buf = await new Promise((resolve, reject) => {
        try {
          const p = ctx.decodeAudioData(ab, resolve, reject);
          if (p && typeof p.then === 'function') p.then(resolve, reject);
        } catch (e) { reject(e); }
      });
      window._mlgBufferCache[url] = buf;
    } catch (e) { /* fallback path will handle it */ }
  }));
}
// NOTE: _preloadMlgAssets is now invoked from _bootMlgFeatureWhenIdle()
// (which fires on window 'load' + requestIdleCallback). Direct firing here
// was moved out so MLG asset bytes don't compete with first-paint resources.

// Big rainbow MLG text overlay — used for both round-number milestones
// and voice-line reactions. `opts.big` is the top giant line, `opts.small`
// (optional) is the secondary line. Random tilt + shake + 2.4s lifetime.
const _MLG_PHRASES = [
  'OMG!!!', 'DAMN SON!!', 'NO SCOPED!!', 'WOW!!!',
  '420 BLAZE IT', 'GET REKT', 'SAVAGE!!', 'OWNED!!',
  'INSANE!!!', 'RAMPAGE!', 'UNREAL!!', 'FROGGED!!',
  'YOU MAD?', 'GG EZ', 'GODLIKE', 'MLG PRO',
];
function _spawnMlgFlash(opts) {
  const wrap = document.createElement('div');
  wrap.className = 'mlg-milestone';
  // The big line is the headline; small (when present) sits below.
  const bigHtml   = `<div class="mlg-milestone-num">${opts.big}</div>`;
  const smallHtml = opts.small ? `<div class="mlg-milestone-phrase">${opts.small}</div>` : '';
  wrap.innerHTML = bigHtml + smallHtml;
  const tilt = (Math.random() * 12 - 6).toFixed(1);
  wrap.style.setProperty('--mlg-tilt', `${tilt}deg`);
  document.body.appendChild(wrap);
  // Spawn a rainbow vignette around the screen edges for the same lifetime
  // as the popup. Multiple stacked popups → one shared vignette (re-uses the
  // existing DOM node and bumps its expiry timestamp). CSS handles the
  // rainbow hue-rotation animation on the inset box-shadow.
  _spawnMlgVignette(2400);
  setTimeout(() => wrap.remove(), 2400);
}

let _mlgVignetteTimer = null;
function _spawnMlgVignette(durationMs) {
  let v = document.getElementById('mlg-vignette');
  if (!v) {
    v = document.createElement('div');
    v.id = 'mlg-vignette';
    v.className = 'mlg-vignette';
    document.body.appendChild(v);
  }
  // Restart the animation by force-reflowing (so new popups extend the show).
  v.classList.remove('on');
  // eslint-disable-next-line no-unused-expressions
  v.offsetWidth;  // reflow
  v.classList.add('on');
  if (_mlgVignetteTimer) clearTimeout(_mlgVignetteTimer);
  _mlgVignetteTimer = setTimeout(() => {
    v.classList.remove('on');
    _mlgVignetteTimer = null;
  }, durationMs);
}
// Round-number kill milestone — shown every 5 kills.
function _spawnMlgMilestone(killCount) {
  const phrase = _MLG_PHRASES[Math.floor(Math.random() * _MLG_PHRASES.length)];
  _spawnMlgFlash({ big: `${killCount} KILLS`, small: phrase });
}
// Voice-line text overlay — shown together with the matching audio so the
// phrase you hear is also the phrase you see flashing center-screen.
function _spawnMlgVoiceText(label) {
  _spawnMlgFlash({ big: label });
}

// ════════════════════════════════════════════════════════════════════════════
// SEEDANCE — video generation panel
// ════════════════════════════════════════════════════════════════════════════
const SD = { refs: [], pollTimer: null, lastStatuses: {} /* idx → status, used to detect transitions */, selected: new Set() };

// Detect status transitions (anything → completed/failed) and play sound + toast.
// Called from sdRenderList AND from poll tick.
function _sdNotifyTransitions(chunks) {
  if (!Array.isArray(chunks)) return;
  const fresh = {};
  let newCompleted = 0;
  let newFailed = 0;
  for (const c of chunks) {
    const idx = c.idx;
    if (idx == null) continue;
    fresh[idx] = c.status;
    const was = SD.lastStatuses[idx];
    if (was != null && was !== c.status) {
      if (c.status === 'completed') newCompleted++;
      else if (c.status === 'failed') newFailed++;
    }
  }
  SD.lastStatuses = fresh;
  if (newCompleted > 0) {
    Sounds.playSuccess();
    showToast(`✓ Готово видео: ${newCompleted} чанк${newCompleted > 1 ? 'а' : ''}`);
    // Auto-mark episode "Готово" when every script-derived segment has a
    // completed chunk. Fires only on the moment of completion (newCompleted>0)
    // and only once per episode-load (sessionAutoReadyDone) so a user who
    // manually un-checks the box doesn't get fought by the next poll tick.
    _maybeAutoMarkEpisodeReady(chunks);
  }
  if (newFailed > 0) {
    Sounds.playError();
    showToast(`✗ Ошибка генерации: ${newFailed} чанк${newFailed > 1 ? 'а' : ''}`);
  }
}

let _autoReadyAppliedFor = null;  // remembers (seriesId, episodeNum) we auto-marked this session
function _maybeAutoMarkEpisodeReady(chunks) {
  if (!S.episode || S.episode.ready) return;          // already ready — nothing to do
  const key = `${S.seriesId}::${S.episode.number}`;
  if (_autoReadyAppliedFor === key) return;           // already auto-marked this session
  // Count completed chunks (any chunk that finished its render).
  const completed = chunks.filter(c => c.status === 'completed').length;
  if (!completed) return;
  // Expected segments = how many script-derived segments the current script
  // would produce. Use _autoCollectSegments which mirrors the scene-view's
  // per-segment math (handles establishing-shot toggle, scene heading split,
  // etc.). If the script is empty / not in scene mode, skip — user is still
  // editing, not done.
  let expected = 0;
  try { expected = (typeof _autoCollectSegments === 'function') ? _autoCollectSegments().length : 0; }
  catch { return; }
  if (expected <= 0) return;
  if (completed < expected) return;
  // All segments rendered — flip the toggle and persist.
  _autoReadyAppliedFor = key;
  const cb = document.getElementById('ep-ready-toggle');
  if (cb) {
    cb.checked = true;
    // Trigger the existing onchange handler so the PUT fires + toast shows.
    onReadyToggle();
  }
  showToast('🎉 Все сегменты сгенерированы — серия отмечена как готовая', 5000);
}

function sdLocDragStart(ev, locId) {
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  const photoUrl = l.ref_images?.[0] ? `${assetUrl(l.ref_images[0])}` : '';
  ev.dataTransfer.setData('application/json', JSON.stringify({
    kind: 'loc', id: locId, name: l.name, photoUrl
  }));
}

async function sdHandleDrop(ev) {
  ev.preventDefault();
  if (SD.refs.length >= 9) { showToast('Максимум 9 референсов'); return; }

  // 1. Internal payload (char/loc card) — CHECK FIRST. The browser may
  // also attach a "file" representation of the dragged element image
  // (the drag-preview bitmap shows up as dataTransfer.files[0]), and if
  // we look at files BEFORE JSON we'd treat an internal card drag as a
  // new external file → uploaded to AVAI as a custom ref, ignoring the
  // existing entity id. User-reported bug: "перетаскиваю локацию,
  // появляются часики, но не прикрепляется".
  try {
    const json = ev.dataTransfer.getData('application/json');
    if (json) {
      const payload = JSON.parse(json);
      if (payload && payload.kind && payload.id) {
        if (SD.refs.some(r => r.kind === payload.kind && r.id === payload.id && r.outfit === payload.outfit)) return;
        SD.refs.push({ ...payload, tag: _sdNextFreeTag() });
        sdRenderRefs();
        return;
      }
    }
  } catch {}

  // 2. File from desktop / external app
  const files = Array.from(ev.dataTransfer.files || []).filter(f => f.type.startsWith('image/'));
  if (files.length) {
    for (const file of files) {
      if (SD.refs.length >= 9) break;
      await sdUploadCustomFile(file);
    }
    return;
  }

  // 3. Image URL dragged from another browser tab
  const uri = ev.dataTransfer.getData('text/uri-list') || ev.dataTransfer.getData('text/plain');
  if (uri && /^https?:\/\//.test(uri.trim()) && /\.(png|jpe?g|webp|gif)(\?|$)/i.test(uri.trim())) {
    await sdUploadCustomUrl(uri.trim());
    return;
  }
}

// Pick the smallest 1-9 integer not currently used as a `tag` on SD.refs.
// Stable tags decouple the visible @ImageN label from the array position so
// removing a middle ref doesn't renumber the survivors (user's mental model
// breaks when @Image3 silently becomes @Image2 after deleting @Image2).
function _sdNextFreeTag() {
  const used = new Set((SD.refs || []).map(r => r && r.tag).filter(Boolean));
  for (let n = 1; n <= 9; n++) if (!used.has(n)) return n;
  return null;
}

async function sdUploadCustomFile(file) {
  const slot = document.getElementById('sd-ref-slots');
  const placeholder = document.createElement('div');
  placeholder.className = 'sd-ref-chip';
  placeholder.innerHTML = `<div class="label">⏳ ${esc(file.name.slice(0,18))}</div>`;
  slot?.appendChild(placeholder);
  try {
    const fd = new FormData();
    fd.append('file', file);
    const res = await api.upload(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/upload-ref`, fd
    );
    if (!res.url) throw new Error(res.error || 'no url');
    SD.refs.push({
      kind: 'url', id: 'custom-' + Date.now(),
      name: res.name || file.name, photoUrl: res.url, url: res.url,
      tag: _sdNextFreeTag(),
    });
  } catch (e) {
    showToast('✗ загрузка: ' + (e.message || e));
  } finally {
    placeholder.remove();
    sdRenderRefs();
  }
}

async function sdUploadCustomUrl(srcUrl) {
  const slot = document.getElementById('sd-ref-slots');
  const placeholder = document.createElement('div');
  placeholder.className = 'sd-ref-chip';
  placeholder.innerHTML = `<div class="label">⏳ url</div>`;
  slot?.appendChild(placeholder);
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/upload-ref`,
      { url: srcUrl }
    );
    if (!res.url) throw new Error(res.error || 'no url');
    SD.refs.push({
      kind: 'url', id: 'custom-' + Date.now(),
      name: res.name || 'custom', photoUrl: res.url, url: res.url,
      tag: _sdNextFreeTag(),
    });
  } catch (e) {
    showToast('✗ загрузка: ' + (e.message || e));
  } finally {
    placeholder.remove();
    sdRenderRefs();
  }
}

function sdRenderRefs() {
  const slot = document.getElementById('sd-ref-slots');
  if (!slot) return;
  // Backfill `tag` for refs loaded from older state (chunks reused via "Reuse"
  // or sdComposeFill paths that don't go through sdSlotDrop). Use array
  // position +1 only when no stable tag exists yet — preserves backwards-compat.
  SD.refs.forEach((r, i) => { if (r && !r.tag) r.tag = i + 1; });
  slot.innerHTML = SD.refs.map((r, i) => {
    const tag = r.tag || (i + 1);
    const subtitle = r.outfit ? `<div class="ref-sub">${esc(r.outfit)}</div>` : '';
    const kindIcon = r.kind === 'loc' ? '🏛'
                  : r.kind === 'lastframe' ? '🎞'
                  : r.kind === 'char' ? '👤' : '🖼';
    return `
    <div class="sd-ref-chip" data-i="${i}"
         ondragover="sdSlotDragOver(event)"
         ondragleave="sdSlotDragLeave(event)"
         ondrop="sdSlotDrop(event,${i})"
         title="@Image${tag}: ${esc(r.name)}${r.outfit ? ' / '+esc(r.outfit) : ''} — перетащи сюда другую карточку чтобы заменить">
      <div class="ref-top">@Image${tag}</div>
      ${r.photoUrl
        ? `<img src="${r.photoUrl}" alt="">`
        : '<div class="ref-noimg">no photo</div>'}
      <div class="ref-bottom">
        <span class="ref-kind">${kindIcon}</span>
        <span class="ref-name">${esc(r.name || '—')}</span>
        ${subtitle}
      </div>
      <button class="rm" onclick="sdRemoveRef(${i})" title="Убрать">×</button>
    </div>`;
  }).join('');
}

function sdSlotDragOver(e) {
  e.preventDefault();
  e.currentTarget.classList.add('drop-target');
}
function sdSlotDragLeave(e) {
  e.currentTarget.classList.remove('drop-target');
}
async function sdSlotDrop(e, idx) {
  e.preventDefault();
  e.stopPropagation();
  e.currentTarget.classList.remove('drop-target');

  // 1. File from desktop
  const files = Array.from(e.dataTransfer.files || []).filter(f => f.type.startsWith('image/'));
  if (files.length) {
    // Replace this slot with custom-uploaded file
    const fd = new FormData();
    fd.append('file', files[0]);
    try {
      const res = await api.upload(
        `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/upload-ref`, fd
      );
      if (res.url) {
        SD.refs[idx] = {
          kind: 'url', id: 'custom-' + Date.now(),
          name: res.name || files[0].name, photoUrl: res.url, url: res.url,
        };
        sdRenderRefs();
      }
    } catch (err) { showToast('✗ ' + (err.message || err)); }
    return;
  }

  // 2. URL from external tab
  const uri = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
  if (uri && /^https?:\/\//.test(uri.trim()) && /\.(png|jpe?g|webp|gif)(\?|$)/i.test(uri.trim())) {
    try {
      const res = await api.post(
        `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/upload-ref`,
        { url: uri.trim() }
      );
      if (res.url) {
        SD.refs[idx] = {
          kind: 'url', id: 'custom-' + Date.now(),
          name: res.name || 'custom', photoUrl: res.url, url: res.url,
        };
        sdRenderRefs();
      }
    } catch (err) { showToast('✗ ' + (err.message || err)); }
    return;
  }

  // 3. Internal char/loc card payload
  let payload;
  try { payload = JSON.parse(e.dataTransfer.getData('application/json') || '{}'); }
  catch { return; }
  if (!payload.kind || !payload.id) return;
  // Resolve name + photoUrl from series data so chip renders correctly
  let name = payload.name || '', photoUrl = payload.photoUrl || '';
  if (!name || !photoUrl) {
    if (payload.kind === 'char') {
      const c = (S.series.characters || []).find(x => x.id === payload.id);
      if (c) {
        name = c.name;
        if (payload.outfit) {
          const o = (c.outfits || []).find(o => o.label === payload.outfit);
          if (o?.photo) photoUrl = `${assetUrl(o.photo)}`;
        }
        if (!photoUrl && c.ref_images?.[0]) photoUrl = `${assetUrl(c.ref_images[0])}`;
      }
    } else if (payload.kind === 'loc') {
      const l = (S.series.locations || []).find(x => x.id === payload.id);
      if (l) { name = l.name; if (l.ref_images?.[0]) photoUrl = `${assetUrl(l.ref_images[0])}`; }
    }
  }
  SD.refs[idx] = { ...payload, name, photoUrl };
  sdRenderRefs();
}

function sdRemoveRef(i) {
  SD.refs.splice(i, 1);
  sdRenderRefs();
}

// Remap @ImageN tags in `promptText` so they reference refs by their CURRENT
// positional order (Seedance API is positional), while the UI shows STABLE
// tags. Removes any @ImageN entries whose tag has no surviving ref.
//
// e.g. user has refs [A(tag=1), C(tag=3)] (after deleting tag=2) and prompt:
//   "@Image1=A, @Image2=B, @Image3=C. A talks to C."
// Returns:
//   "@Image1=A, @Image2=C. A talks to C."  (B's BINDING entry dropped, C
//   renumbered from @Image3 to @Image2 to match positional order in refs[])
function _sdRemapPromptForSubmit(promptText, refs) {
  if (!promptText) return promptText;
  // tag → newPos (1-based)
  const tagToPos = new Map();
  refs.forEach((r, i) => {
    if (r && r.tag) tagToPos.set(r.tag, i + 1);
  });
  // Sentinel-based two-pass to avoid double-rewriting (e.g. 1→2 then 2→3).
  let out = promptText;
  // Pass 1: original @ImageN → \x01IMG\x01N
  out = out.replace(/@Image(\d+)/g, (m, n) => `\x01IMG\x01${n}`);
  // Pass 2: \x01IMG\x01N → @ImageK (remap or drop)
  // For each placeholder, look up tag → new pos. If tag has no pos (ref deleted),
  // we want to strip the WHOLE BINDING entry like `@Image2=Name (description),`
  // — handle that with a separate pass first.
  // First strip BINDING entries for orphan tags:
  for (const [origTag] of [...new Set([...promptText.matchAll(/@Image(\d+)/g)].map(m => parseInt(m[1])))].entries()) {
    // unused — replaced by simpler strip below
  }
  // Simpler: scan all @ImageN occurrences in original prompt; for each unique N
  // that is NOT in tagToPos, strip its binding entry from the post-sentinel text.
  const allTags = [...new Set([...promptText.matchAll(/@Image(\d+)/g)].map(m => parseInt(m[1])))];
  for (const t of allTags) {
    if (!tagToPos.has(t)) {
      // Drop binding-style entry: optional leading comma/space, @ImageT=Name (...) up to next comma/period/newline
      const bindingRe = new RegExp(`,?\\s*\\x01IMG\\x01${t}\\s*[=\\-—]\\s*[^,.\\n]*`, 'g');
      out = out.replace(bindingRe, '');
      // Drop standalone @ImageT mentions
      const standaloneRe = new RegExp(`\\s*\\x01IMG\\x01${t}\\b`, 'g');
      out = out.replace(standaloneRe, '');
    }
  }
  // Now remap surviving \x01IMG\x01N → @ImageK
  out = out.replace(/\x01IMG\x01(\d+)/g, (m, n) => {
    const pos = tagToPos.get(parseInt(n));
    return pos != null ? `@Image${pos}` : '';
  });
  // Tidy: empty parens left from inline tag removal "Kyle (@Image2) listens" → "Kyle  listens",
  // double commas, leading commas after BINDING strips.
  out = out.replace(/\(\s*\)/g, '');
  out = out.replace(/[ \t]+/g, ' ');
  out = out.replace(/,\s*,/g, ',').replace(/(:\s*),/g, '$1').replace(/\(\s*,/g, '(');
  return out;
}

// Recompose the prompt via Claude USING ONLY the refs currently in the SD panel.
// Use case: user manually removed redundant char refs (e.g. extras hanging in
// the corner) and wants the prompt rewritten to focus on who's left, instead
// of editing tokens by hand. Server-side `locked_refs` instructs Claude to use
// EXACTLY these — no auto-detection of missing chars from chunk text.
async function sdRecomposeWithCurrentRefs() {
  const chunk = document.getElementById('sd-chunk-text').value.trim();
  if (!chunk) { showToast('Вставь кусок сценария'); return; }
  if (!SD.refs.length) { showToast('Нет рефов — сначала добавь хотя бы один'); return; }
  const st = document.getElementById('sd-compose-status');
  if (st) st.textContent = '🔄 перекомпоную с текущими рефами...';
  const useLastframe = !!document.getElementById('sd-use-lastframe')?.checked;
  const useCutframes = !!document.getElementById('sd-use-cutframes')?.checked;
  const useStyle     = !!document.getElementById('sd-use-style')?.checked;
  const styleVal     = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly     = !!document.getElementById('sd-base-only')?.checked;
  const closeUpOnly  = !!document.getElementById('sd-close-up-only')?.checked;
  // Snapshot tags so we restore stable labels after the recompose returns
  // (server returns refs without `tag`, so we re-attach them by id+kind+outfit).
  const tagSnapshot = SD.refs.map(r => ({ kind: r.kind, id: r.id, outfit: r.outfit || null, tag: r.tag }));
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/compose`,
      {
        chunk_text: chunk,
        use_prev_lastframe: useLastframe,
        use_prev_cutframes: useCutframes,
        style: useStyle ? styleVal : '',
        base_outfits_only: baseOnly,
        close_up_only: closeUpOnly,
        locked_refs: SD.refs.map(r => ({ kind: r.kind, id: r.id, outfit: r.outfit || null })),
      }
    );
    document.getElementById('sd-prompt').value = res.prompt || '';
    SD.refs = (res.refs || []).map(r => {
      let name = '', photoUrl = '';
      if (r.kind === 'char') {
        const c = (S.series.characters || []).find(x => x.id === r.id);
        if (c) {
          name = c.name;
          if (r.outfit) {
            const o = (c.outfits || []).find(o => o.label === r.outfit);
            if (o?.photo) photoUrl = `${assetUrl(o.photo)}`;
          }
          if (!photoUrl && c.ref_images?.[0]) photoUrl = `${assetUrl(c.ref_images[0])}`;
        }
      } else if (r.kind === 'loc') {
        const l = (S.series.locations || []).find(x => x.id === r.id);
        if (l) {
          name = l.name;
          if (l.ref_images?.[0]) photoUrl = `${assetUrl(l.ref_images[0])}`;
        }
      } else if (r.kind === 'lastframe' || r.kind === 'cutframe') {
        name = r.name || (r.kind === 'lastframe' ? 'last frame' : 'pre-cut frame');
        photoUrl = r.url || '';
      }
      // Restore stable tag from pre-recompose snapshot
      const snap = tagSnapshot.find(s => s.kind === r.kind && s.id === r.id && (s.outfit || null) === (r.outfit || null));
      return { ...r, name, photoUrl, tag: snap?.tag };
    });
    sdRenderRefs();
    if (st) st.textContent = '✓ перекомпоновано с пинн-рефами';
  } catch (e) {
    if (st) st.textContent = '✗ ' + (e.message || e);
  }
}

async function sdCompose() {
  const chunk = document.getElementById('sd-chunk-text').value.trim();
  if (!chunk) { showToast('Вставь кусок сценария'); return; }
  const st = document.getElementById('sd-compose-status');
  st.textContent = '⚙ компоную через Claude...';
  const useLastframe = !!document.getElementById('sd-use-lastframe')?.checked;
  const useCutframes = !!document.getElementById('sd-use-cutframes')?.checked;
  const useStyle     = !!document.getElementById('sd-use-style')?.checked;
  const styleVal     = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly     = !!document.getElementById('sd-base-only')?.checked;
  const closeUpOnly  = !!document.getElementById('sd-close-up-only')?.checked;
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/compose`,
      {
        chunk_text: chunk,
        use_prev_lastframe: useLastframe,
        use_prev_cutframes: useCutframes,
        style: useStyle ? styleVal : '',
        base_outfits_only: baseOnly,
        close_up_only: closeUpOnly,
      }
    );
    document.getElementById('sd-prompt').value = res.prompt || '';
    SD.refs = (res.refs || []).map(r => {
      // Re-pull name + photoUrl from the loaded series
      let name = '', photoUrl = '';
      if (r.kind === 'char') {
        const c = (S.series.characters || []).find(x => x.id === r.id);
        if (c) {
          name = c.name;
          if (r.outfit) {
            const o = (c.outfits || []).find(o => o.label === r.outfit);
            if (o?.photo) photoUrl = `${assetUrl(o.photo)}`;
          }
          if (!photoUrl && c.ref_images?.[0]) photoUrl = `${assetUrl(c.ref_images[0])}`;
        }
      } else if (r.kind === 'loc') {
        const l = (S.series.locations || []).find(x => x.id === r.id);
        if (l) {
          name = l.name;
          if (l.ref_images?.[0]) photoUrl = `${assetUrl(l.ref_images[0])}`;
        }
      } else if (r.kind === 'lastframe') {
        // Server-attached continuity frame from previous chunk
        name = r.name || 'last frame';
        photoUrl = r.url || '';
      } else if (r.kind === 'cutframe') {
        // Pre-cut keyframe extracted from inside the previous chunk
        name = r.name || 'pre-cut frame';
        photoUrl = r.url || '';
      }
      return { ...r, name, photoUrl };
    });
    sdRenderRefs();
    let msg = res.scene_continuity ? '✓ продолжение прошлой сцены' : '✓ скомпоновано';
    if (res.lastframe_attached) msg += ' · 🎞 last frame прицеплен';
    if (res.cutframes_attached) msg += ` · ✂ ${res.cutframes_attached} кадр(ов) перед склейками`;
    if (res.state_analysis_attached) msg += ' · 🧠 состояние персов проанализировано';
    if (!res.cur_pos_found) {
      msg += ' · ⚠ позицию в сценарии не нашёл (continuity без соседа)';
    } else if (res.prev_neighbour) {
      const pn = res.prev_neighbour;
      const epPart = pn.episode && pn.episode !== S.episode.number ? ` (эп ${pn.episode})` : '';
      const vid = pn.has_video ? '' : ' [нет видео]';
      msg += ` · prev: #${pn.idx}${epPart}${vid}`;
    } else {
      msg += ' · prev: нет (первый в эп.)';
    }
    if ((res.unresolved_refs || []).length) {
      const names = res.unresolved_refs.map(r => `${r.kind}:${r.id}`).join(', ');
      msg += ` · ⚠ не подгрузились: ${names}`;
      showToast('⚠ Часть рефов не удалось подгрузить (' + names + ') — проверь, есть ли у локации/перса фото');
    }
    if ((res.outfit_fallbacks || []).length) {
      const fbs = res.outfit_fallbacks;
      msg += ` · ⚠ outfit-fallback × ${fbs.length}`;
      const lines = fbs.map(f => {
        const avail = (f.available_labels || []).length
          ? `имеются: ${f.available_labels.join(', ')}`
          : 'у перса нет outfits — только база';
        return `• ${f.char_name}: запрошен "${f.requested_outfit}", откатились на base (${avail})`;
      }).join('\n');
      showToast('⚠ Composer выбрал несуществующий outfit-label — откат на базу:\n' + lines, 9000);
    }
    if ((res.duplicate_chars_dropped || []).length) {
      const dups = res.duplicate_chars_dropped;
      msg += ` · 🚫 удалено дублей × ${dups.length}`;
      showToast(
        '🚫 Composer пытался добавить персонажа дважды — продублированные ref\'ы удалены автоматически (защита от двойников в кадре). ' +
        'Если такое повторяется часто — пришли промпт, докрутим правила.',
        7000
      );
    }
    if ((res.closeup_dropped || []).length) {
      const dropped = res.closeup_dropped;
      msg += ` · 🎯 close-up: дроп ${dropped.length} перс`;
      const names = dropped.map(d => d.char_name).join(', ');
      showToast(`🎯 Close-up only: оставлен один перс + локация. Дропнуты из refs: ${names}.`, 5000);
    }
    if (res.auto_close_up_detected) {
      msg += ' · 🎯 auto-close-up hint';
    }
    st.textContent = msg;
  } catch (e) {
    st.textContent = '✗ ' + (e.message || e);
  }
}

function sdToggleStyleField() {
  const cb = document.getElementById('sd-use-style');
  const inp = document.getElementById('sd-style');
  if (!inp) return;
  inp.classList.toggle('hidden', !cb?.checked);
  if (cb?.checked) inp.focus();
  sdSavePrefs();
}

// Per-series localStorage key for moderation_bypass — keeps grid/cartoon/etc
// scoped to one show. Switching series should NOT carry over the mod-bypass
// setting (different shows have different moderation profiles, e.g. romantic
// drama works fine with `grid` while a violent thriller needs `cartoon`).
function _sdModKey(sid) { return `sd_mod_bypass_${sid || ''}`; }

function sdSavePrefs() {
  try {
    const modBypass = document.getElementById('sd-mod-bypass').value;
    localStorage.setItem('sd_prefs', JSON.stringify({
      duration: document.getElementById('sd-duration').value,
      resolution: document.getElementById('sd-resolution').value,
      moderation_bypass: modBypass,    // also kept globally as fallback default for new series
      use_prev_lastframe: !!document.getElementById('sd-use-lastframe')?.checked,
      use_prev_cutframes: !!document.getElementById('sd-use-cutframes')?.checked,
      use_style: !!document.getElementById('sd-use-style')?.checked,
      style: (document.getElementById('sd-style')?.value || '').trim(),
      base_outfits_only: !!document.getElementById('sd-base-only')?.checked,
      close_up_only: !!document.getElementById('sd-close-up-only')?.checked,
      establishing_shot: !!document.getElementById('sd-establishing-shot')?.checked,
    }));
    // Per-series override
    if (S.seriesId) {
      localStorage.setItem(_sdModKey(S.seriesId), modBypass);
    }
  } catch (e) {}
}

function sdLoadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('sd_prefs') || '{}');
    if (p.duration) document.getElementById('sd-duration').value = p.duration;
    if (p.resolution) document.getElementById('sd-resolution').value = p.resolution;
    // moderation_bypass: per-series override wins over global default
    const seriesMod = S.seriesId ? localStorage.getItem(_sdModKey(S.seriesId)) : null;
    const finalMod = seriesMod || p.moderation_bypass;
    if (finalMod) document.getElementById('sd-mod-bypass').value = finalMod;
    const cb = document.getElementById('sd-use-lastframe');
    if (cb && typeof p.use_prev_lastframe === 'boolean') cb.checked = p.use_prev_lastframe;
    const cf = document.getElementById('sd-use-cutframes');
    if (cf && typeof p.use_prev_cutframes === 'boolean') cf.checked = p.use_prev_cutframes;
    const sc = document.getElementById('sd-use-style');
    const si = document.getElementById('sd-style');
    if (sc && typeof p.use_style === 'boolean') sc.checked = p.use_style;
    if (si && typeof p.style === 'string') si.value = p.style;
    if (si && sc) si.classList.toggle('hidden', !sc.checked);
    const bo = document.getElementById('sd-base-only');
    if (bo && typeof p.base_outfits_only === 'boolean') bo.checked = p.base_outfits_only;
    const cu = document.getElementById('sd-close-up-only');
    if (cu && typeof p.close_up_only === 'boolean') cu.checked = p.close_up_only;
    const es = document.getElementById('sd-establishing-shot');
    if (es && typeof p.establishing_shot === 'boolean') es.checked = p.establishing_shot;
  } catch (e) {}
}

async function sdGenerate() {
  console.log('[sdGenerate] click');
  // Pre-flight: warn if any active char/loc lacks a generated ref. Without
  // this the user gets a video with random face/location for the missing one.
  if (!await _confirmMissingAssetsBeforeGen('генерацию видео')) return;
  const promptEl = document.getElementById('sd-prompt');
  const prompt = (promptEl?.value || '').trim();
  if (!prompt) {
    showToast('⚠ Промпт пустой — заполни поле "Prompt"', 4000);
    promptEl?.focus();
    promptEl?.classList.add('input-error');
    setTimeout(() => promptEl?.classList.remove('input-error'), 2000);
    return;
  }
  const chunk = document.getElementById('sd-chunk-text').value.trim();
  const duration = parseInt(document.getElementById('sd-duration').value) || 15;
  const resolution = document.getElementById('sd-resolution').value;
  const moderation_bypass = document.getElementById('sd-mod-bypass').value;
  sdSavePrefs();

  const btn = document.querySelector('#seedance-panel button[onclick="sdGenerate()"]');
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ ...'; }

  // Optimistic placeholder so the user sees a card immediately
  try {
    const optimistic = {
      idx: '…', status: 'submitting', prompt, duration, resolution,
      moderation_bypass, progress: null, _optimistic: true,
    };
    sdRenderList([...(SD._lastChunks || []), optimistic]);
    document.getElementById('sd-gen-list')?.scrollIntoView({behavior:'smooth', block:'nearest'});
  } catch (err) {
    console.warn('[sdGenerate] optimistic render failed (continuing anyway):', err);
  }

  try {
    // Remap @ImageN tags in prompt to match positional order of refs[] before
    // sending to API. UI keeps stable tags (so removing @Image2 leaves @Image3
    // visible as @Image3), but Seedance is positional — we must renumber on
    // submit. Also strips BINDING entries for tags whose ref was deleted.
    const submitPrompt = _sdRemapPromptForSubmit(prompt, SD.refs);
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/start`,
      {
        prompt: submitPrompt, chunk_text: chunk, duration, resolution, moderation_bypass,
        refs: SD.refs.map(r => ({ kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null }))
      }
    );
    showToast(`▶ Чанк #${res.chunk?.idx ?? '?'} в очереди — можно листать дальше, генерация 1-15 мин`);
    // Не очищаем prompt/chunk_text/refs — часто хочется доработать тот же промпт
    // и сгенерировать вариацию. Хочешь чистый лист — кнопка ↻ Reuse / руками.
    await sdRefreshList();
    sdEnsurePoll();
  } catch (e) {
    showToast('✗ Ошибка запуска: ' + (e.message || e));
    await sdRefreshList();
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '▶ Сгенерировать'; }
  }
}

async function sdRefreshList() {
  if (!S.episode) return;
  try {
    const res = await api.get(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/list`
    );
    const chunks = (res.chunks || []).slice();
    // Display order: prefer canonical script_order (set by parallel auto-mode);
    // fall back to creation idx so legacy chunks without script_order keep
    // their original ordering. Stable sort: chunks with order < chunks without.
    chunks.sort((a, b) => {
      const ao = (typeof a.script_order === 'number') ? a.script_order : Infinity;
      const bo = (typeof b.script_order === 'number') ? b.script_order : Infinity;
      if (ao !== bo) return ao - bo;
      return (a.idx || 0) - (b.idx || 0);
    });
    sdRenderList(chunks);
  } catch (e) { /* ignore */ }
}

function _sdCardHTML(c) {
  const stCls = `sd-status-${c.status || 'pending'}`;
  const videoUrl = c.video_path ? `${assetUrl(c.video_path)}` : '';
  const cost = c.cost != null ? `· $${Number(c.cost).toFixed(2)}` : '';
  let placeholderText;
  if (c.status === 'failed') placeholderText = '✗ failed';
  else if (c.status === 'submitting') placeholderText = '📤 отправляю...';
  else if (c.progress != null) placeholderText = c.progress + '%';
  else placeholderText = '⏳ генерируется';
  const isSelected = SD.selected && SD.selected.has(c.idx);
  // Retry only makes sense if we have stored prompt + refs
  const canRetry = !!(c.prompt && (c.refs || []).length && c.status !== 'submitting');
  return `
    <label class="sd-card-cb-wrap" title="Выбрать для bulk-действий">
      <input type="checkbox" class="sd-card-cb" ${isSelected ? 'checked' : ''}
        onclick="event.stopPropagation();sdToggleSelect(${c.idx})">
    </label>
    ${videoUrl
      ? `<video src="${videoUrl}" controls preload="metadata"></video>`
      : `<div class="sd-placeholder">${placeholderText}</div>`}
    <div class="sd-gen-meta">
      <div><span class="${stCls}">●</span> #${c.idx} · ${c.status} · ${c.duration}s ${c.resolution} · ${c.moderation_bypass} ${cost}</div>
      <div class="sd-prompt">${esc(c.prompt || '')}</div>
      ${c.error ? `<div style="color:#e74c3c">${esc(c.error)}</div>` : ''}
      <div class="sd-gen-actions">
        ${videoUrl ? `<a class="btn-ghost btn-sm" href="${videoUrl}" download>⬇ Скачать</a>` : ''}
        ${videoUrl ? `<button class="btn-ghost btn-sm" onclick="sdAddToTimeline(${c.idx}, this)">➕ На таймлайн</button>` : ''}
        ${canRetry ? `<button class="btn-ghost btn-sm" onclick="sdRetry(${c.idx}, this)" title="Перезапустить генерацию с тем же промптом и refs (без compose) — мгновенно создаёт новый чанк">🔁 Retry</button>` : ''}
        <button class="btn-ghost btn-sm" onclick="sdReuse(${c.idx})" title="Подставить параметры этого чанка в форму выше — для ручной правки и повторной генерации">↻ Reuse</button>
        ${c.status === 'failed' ? `<button class="btn-ghost btn-sm" onclick="sdHealAndReuse(${c.idx}, this)" title="Переписать промпт чтобы прошёл модерацию + Reuse">🩹 Лечить</button>` : ''}
        <button class="btn-ghost btn-sm" onclick="sdDelete(${c.idx})">🗑</button>
      </div>
    </div>
  `;
}

function _sdCardSig(c) {
  // Signature changes only on something user-visible — so playback isn't reset
  // when the poll just brought the same card back.
  return [
    c.idx, c.status, c.video_path || '',
    c.progress ?? '', c.error || '',
    c.prompt || '', c.duration, c.resolution, c.moderation_bypass,
    c.cost ?? '',
  ].join('|');
}

function sdRenderList(chunks) {
  const el = document.getElementById('sd-gen-list');
  if (!el) return;
  // cache the last server-state list so optimistic adds can stack on top
  if (!chunks.some(c => c._optimistic)) SD._lastChunks = chunks;
  // Show/hide the always-visible bulk toolbar above the list. Renders the
  // chunk count so user sees "5 / 12 selected" at a glance without diving
  // into the floating bulk-bar (which only appears after first selection).
  const toolbar = document.getElementById('sd-list-toolbar');
  if (toolbar) {
    if (!chunks.length) {
      toolbar.classList.add('hidden');
    } else {
      toolbar.classList.remove('hidden');
      const cnt = document.getElementById('sd-list-count');
      if (cnt) {
        const sel = (SD.selected && SD.selected.size) || 0;
        cnt.textContent = sel ? `Выбрано ${sel} из ${chunks.length}` : `Всего ${chunks.length}`;
      }
    }
  }
  if (!chunks.length) { el.innerHTML = ''; return; }
  try {
    // Order in DOM: newest (highest idx) first — same as before (.slice().reverse()).
    const ordered = chunks.slice().reverse();
    const wantedIdxs = new Set(ordered.map(c => String(c.idx)));
    const existing = new Map();
    el.querySelectorAll('.sd-gen-card[data-idx]').forEach(node => {
      existing.set(node.getAttribute('data-idx'), node);
    });
    // Remove cards that no longer exist on server
    existing.forEach((node, idx) => { if (!wantedIdxs.has(idx)) node.remove(); });
    // Walk wanted order, inserting / updating in place
    let prevNode = null;
    for (const c of ordered) {
      const idx = String(c.idx);
      const sig = _sdCardSig(c);
      let node = existing.get(idx);
      if (!node) {
        node = document.createElement('div');
        node.className = 'sd-gen-card';
        node.setAttribute('data-idx', idx);
        node.setAttribute('data-sig', sig);
        node.innerHTML = _sdCardHTML(c);
      } else if (node.getAttribute('data-sig') !== sig) {
        node.setAttribute('data-sig', sig);
        node.innerHTML = _sdCardHTML(c);
      }
      // Selection class survives polling re-renders
      if (SD.selected && SD.selected.has(c.idx)) node.classList.add('sd-card-selected');
      else node.classList.remove('sd-card-selected');
      // Place node at correct DOM slot
      if (prevNode) {
        if (node.previousSibling !== prevNode) prevNode.after(node);
      } else {
        if (el.firstChild !== node) el.prepend(node);
      }
      prevNode = node;
    }
    // Refresh bulk bar in case server removed/added chunks
    _sdUpdateBulkBar();
  } catch (err) {
    console.error('[sdRenderList] diff failed, falling back to full render:', err);
    el.innerHTML = chunks.slice().reverse().map(c => {
      return `<div class="sd-gen-card" data-idx="${c.idx}">${_sdCardHTML(c)}</div>`;
    }).join('');
  }
}

async function sdDelete(idx) {
  if (!confirm('Удалить эту генерацию?')) return;
  await api.del(`/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/${idx}`);
  if (SD.selected) SD.selected.delete(idx);
  _sdUpdateBulkBar();
  await sdRefreshList();
}

// Instant Retry — re-fire /seedance/start with the chunk's stored prompt + refs.
// Creates a NEW chunk. Old one stays as-is (preserves history).
// Throws on failure so bulk-retry can count failures.
async function sdRetry(idx, btn) {
  const list = SD._lastChunks || [];
  const c = list.find(x => x.idx === idx);
  if (!c) throw new Error(`Чанк #${idx} не найден`);
  if (!c.prompt) throw new Error(`У чанка #${idx} нет сохранённого промпта — используй Reuse`);
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳'; }
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/start`,
      {
        prompt: c.prompt,
        chunk_text: c.chunk_text || '',
        duration: c.duration || 15,
        resolution: c.resolution || '720p',
        moderation_bypass: c.moderation_bypass || 'collage_grid',
        refs: (c.refs || []).map(r => ({
          kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
          source: r.source, prev_idx: r.prev_idx, name: r.name,
          cut_index: r.cut_index, cut_time: r.cut_time,
        })),
      }
    );
    if (btn && !btn.dataset.bulk) {
      showToast(`▶ Retry: новый чанк #${res.chunk?.idx} в очереди`, 3000);
      await sdRefreshList();
      sdEnsurePoll();
    }
    return res;
  } catch (e) {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml; }
    throw e;
  }
}

// ── Bulk-select for chunk cards ────────────────────────────────────────────
function sdToggleSelect(idx) {
  if (!SD.selected) SD.selected = new Set();
  if (SD.selected.has(idx)) SD.selected.delete(idx);
  else SD.selected.add(idx);
  // Toggle visual class on the card
  const card = document.querySelector(`.sd-gen-card[data-idx="${idx}"]`);
  if (card) card.classList.toggle('sd-card-selected', SD.selected.has(idx));
  _sdUpdateBulkBar();
}

function sdBulkClearSelection() {
  if (!SD.selected) return;
  SD.selected.clear();
  document.querySelectorAll('.sd-gen-card.sd-card-selected').forEach(c => c.classList.remove('sd-card-selected'));
  document.querySelectorAll('input.sd-card-cb:checked').forEach(cb => { cb.checked = false; });
  _sdUpdateBulkBar();
}

function sdBulkSelectAll() {
  if (!SD.selected) SD.selected = new Set();
  const list = SD._lastChunks || [];
  for (const c of list) SD.selected.add(c.idx);
  document.querySelectorAll('.sd-gen-card[data-idx]').forEach(card => {
    card.classList.add('sd-card-selected');
    const cb = card.querySelector('input.sd-card-cb');
    if (cb) cb.checked = true;
  });
  _sdUpdateBulkBar();
}

function _sdUpdateBulkBar() {
  let bar = document.getElementById('sd-bulk-bar');
  const sel = SD.selected || new Set();
  const count = sel.size;
  const list = SD._lastChunks || [];
  // Sync the always-visible top-bar counter with current selection.
  const cnt = document.getElementById('sd-list-count');
  if (cnt && list.length) {
    cnt.textContent = count ? `Выбрано ${count} из ${list.length}` : `Всего ${list.length}`;
  }
  if (count === 0) {
    if (bar) bar.classList.add('hidden');
    return;
  }
  if (!bar) {
    const listEl = document.getElementById('sd-gen-list');
    if (!listEl) return;
    bar = document.createElement('div');
    bar.id = 'sd-bulk-bar';
    bar.className = 'sd-bulk-bar';
    listEl.parentNode.insertBefore(bar, listEl);
  }
  // Count what's actionable
  const ready = [...sel].filter(idx => {
    const c = list.find(x => x.idx === idx);
    return c && c.video_path;
  }).length;
  const retryable = [...sel].filter(idx => {
    const c = list.find(x => x.idx === idx);
    return c && c.prompt && (c.refs || []).length;
  }).length;
  bar.classList.remove('hidden');
  bar.innerHTML = `
    <span class="sd-bulk-count">Выбрано: ${count}</span>
    <button class="btn-ghost btn-sm" onclick="sdBulkRetry()" ${retryable === 0 ? 'disabled' : ''}
      title="Перезапустить ${retryable} генерац(ий) с теми же промптами и refs">🔁 Retry · ${retryable}</button>
    <button class="btn-ghost btn-sm" onclick="sdBulkAddToTimeline()" ${ready === 0 ? 'disabled' : ''}
      title="${ready} готовых видео — добавить на таймлайн">➕ На таймлайн · ${ready}</button>
    <button class="btn-ghost btn-sm" onclick="sdBulkDownload()" ${ready === 0 ? 'disabled' : ''}
      title="${ready} готовых видео — скачать ZIP-архивом">⬇ Скачать ZIP · ${ready}</button>
    <button class="btn-ghost btn-sm" onclick="sdBulkDelete()" style="color:#e74c3c"
      title="Удалить ${count} генерац(ий)">🗑 Удалить · ${count}</button>
    <span style="flex:1"></span>
    <button class="btn-ghost btn-sm" onclick="sdBulkSelectAll()">Все</button>
    <button class="btn-ghost btn-sm" onclick="sdBulkClearSelection()">✕ Снять выбор</button>
  `;
}

async function sdBulkRetry() {
  const idxs = Array.from(SD.selected || []);
  if (!idxs.length) return;
  if (!confirm(`Перезапустить генерацию ${idxs.length} чанк(ов)?\n\nКаждый создаст НОВЫЙ чанк с тем же промптом и refs. Старые останутся (можешь удалить вручную).`)) return;
  // Concurrency cap to avoid hammering Seedance API
  const CAP = 5;
  let ok = 0, failed = 0;
  const queue = idxs.slice();
  const fakeBtn = { dataset: { bulk: '1' }, innerHTML: '', disabled: false };
  async function worker() {
    while (queue.length) {
      const idx = queue.shift();
      try { await sdRetry(idx, fakeBtn); ok++; }
      catch (e) { console.warn('bulk retry failed for', idx, e); failed++; }
    }
  }
  const workers = [];
  for (let i = 0; i < Math.min(CAP, idxs.length); i++) workers.push(worker());
  await Promise.all(workers);
  sdBulkClearSelection();
  await sdRefreshList();
  sdEnsurePoll();
  if (failed > 0) {
    Sounds.playError();
    showToast(`⚠ Retry: ${ok} запущено, ${failed} ошибок`, 6000);
  } else {
    showToast(`✓ Retry: ${ok} новых чанк(ов) в очереди`, 4000);
  }
}

async function sdBulkDelete() {
  const idxs = Array.from(SD.selected || []);
  if (!idxs.length) return;
  if (!confirm(`Удалить ${idxs.length} генерац(ий)? Видео-файлы тоже удалятся с диска.`)) return;
  let ok = 0, failed = 0;
  for (const idx of idxs) {
    try {
      await api.del(`/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/${idx}`);
      ok++;
    } catch (e) { console.warn('bulk delete failed for', idx, e); failed++; }
  }
  sdBulkClearSelection();
  await sdRefreshList();
  showToast(`✓ Удалено: ${ok}${failed ? `, ошибок: ${failed}` : ''}`, 4000);
}

async function sdBulkDownload() {
  const idxs = Array.from(SD.selected || []);
  if (!idxs.length) return;
  const list = SD._lastChunks || [];
  const ready = idxs.filter(idx => {
    const c = list.find(x => x.idx === idx);
    return c && c.video_path;
  });
  if (!ready.length) {
    showToast('⚠ Среди выбранных нет готовых видео', 4000);
    return;
  }
  // Stream the zip via a server endpoint — browser handles the download.
  // Idxs as query string (short enough even for 50+ chunks).
  const url = `/api/series/${encodeURIComponent(S.seriesId)}/episodes/${S.episode.number}/seedance/download-zip?idxs=${ready.join(',')}`;
  // Trigger a normal download (anchor click — preserves filename header)
  const a = document.createElement('a');
  a.href = url;
  a.download = '';   // let server set filename via Content-Disposition
  document.body.appendChild(a);
  a.click();
  a.remove();
  showToast(`⬇ Архив с ${ready.length} видео формируется...`, 3500);
}

async function sdBulkAddToTimeline() {
  const idxs = Array.from(SD.selected || []);
  if (!idxs.length) return;
  const list = SD._lastChunks || [];
  const ready = idxs.filter(idx => {
    const c = list.find(x => x.idx === idx);
    return c && c.video_path;
  });
  if (!ready.length) {
    showToast('⚠ Среди выбранных нет готовых видео', 4000);
    return;
  }
  let ok = 0, failed = 0;
  for (const idx of ready) {
    try {
      await api.post(`/api/series/${S.seriesId}/timeline/clips/add`, {
        episode: S.episode.number, chunk_idx: idx,
      });
      ok++;
    } catch (e) { console.warn('bulk add to timeline failed for', idx, e); failed++; }
  }
  sdBulkClearSelection();
  showToast(`✓ На таймлайн: ${ok}${failed ? `, ошибок: ${failed}` : ''}`, 4000);
}

async function sdAddToTimeline(idx, btn) {
  if (!S.episode || !S.seriesId) return;
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳'; }
  try {
    const res = await api.post(`/api/series/${S.seriesId}/timeline/clips/add`, {
      episode: S.episode.number, chunk_idx: idx,
    });
    showToast(`✓ Добавлено на таймлайн (всего ${res.count})`);
    if (btn) { btn.innerHTML = '✓ на таймлайне'; setTimeout(() => { btn.innerHTML = old; btn.disabled = false; }, 1500); }
  } catch (e) {
    showToast('✗ ' + (e.message || e));
    if (btn) { btn.innerHTML = old; btn.disabled = false; }
  }
}

async function sdHealAndReuse(idx, btn) {
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ лечу...'; }
  try {
    // 1) Reuse first — fills prompt, chunk_text, refs, params from the failed chunk
    await sdReuse(idx);
    // 2) Ask backend to rewrite prompt+chunk to pass moderation
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/${idx}/heal-prompt`,
      {}
    );
    // 3) Apply healed text into composer
    if (res.prompt) document.getElementById('sd-prompt').value = res.prompt;
    if (res.chunk_text) document.getElementById('sd-chunk-text').value = res.chunk_text;
    // 4) Show changes summary in a modal so user understands what shifted
    const changes = res.changes || [];
    const lines = changes.length
      ? changes.map(s => `  • ${s}`).join('\n')
      : '  (модель не выделила конкретных правок — проверь сам)';
    const reason = res.reasoning ? `\n\nОбоснование: ${res.reasoning}` : '';
    alert(
      `🩹 Промпт пролечен. Что изменено:\n\n${lines}${reason}\n\n` +
      'Промпт и chunk_text обновлены в композере. Нажми ▶ Сгенерировать чтобы попробовать.'
    );
    showToast('🩹 Готово · промпт пролечен', 4000);
  } catch (e) {
    showToast('✗ heal: ' + (e.message || e), 6000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🩹 Лечить'; }
  }
}

async function sdReuse(idx) {
  const res = await api.get(`/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/list`);
  const c = (res.chunks || []).find(x => x.idx === idx);
  if (!c) return;
  document.getElementById('sd-chunk-text').value = c.chunk_text || '';
  document.getElementById('sd-prompt').value = c.prompt || '';
  document.getElementById('sd-duration').value = c.duration || 15;
  document.getElementById('sd-resolution').value = c.resolution || '720p';
  document.getElementById('sd-mod-bypass').value = c.moderation_bypass || 'collage_grid';
  // Rebuild refs from stored descriptors
  SD.refs = (c.refs || []).map(r => {
    let name = '', photoUrl = '';
    if (r.kind === 'char') {
      const ch = (S.series.characters || []).find(x => x.id === r.id);
      if (ch) {
        name = ch.name;
        if (r.outfit) {
          const o = (ch.outfits || []).find(o => o.label === r.outfit);
          if (o?.photo) photoUrl = `${assetUrl(o.photo)}`;
        }
        if (!photoUrl && ch.ref_images?.[0]) photoUrl = `${assetUrl(ch.ref_images[0])}`;
      }
    } else if (r.kind === 'loc') {
      const l = (S.series.locations || []).find(x => x.id === r.id);
      if (l) { name = l.name; if (l.ref_images?.[0]) photoUrl = `${assetUrl(l.ref_images[0])}`; }
    } else if (r.kind === 'lastframe') {
      // Last-frame ref: URL is already a public AVAI image — use it as the thumbnail too.
      name = r.name || `last frame · prev #${r.prev_idx ?? '?'}`;
      photoUrl = r.url || '';
    }
    return { ...r, name, photoUrl };
  });
  sdRenderRefs();
  showToast('↻ Параметры подставлены');
  document.getElementById('seedance-panel')?.scrollIntoView({behavior:'smooth', block:'start'});
}

async function sdPollOnce() {
  if (!S.episode) return null;
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/poll`, {}
    );
    const chunks = res.chunks || [];
    _sdNotifyTransitions(chunks);  // beep + toast on completed/failed transitions
    sdRenderList(chunks);
    return chunks;
  } catch (e) { return null; }
}

function sdEnsurePoll() {
  if (SD.pollTimer) return;
  const tick = async () => {
    const chunks = await sdPollOnce();
    const anyPending = (chunks || []).some(c => c.status !== 'completed' && c.status !== 'failed');
    const st = document.getElementById('sd-poll-status');
    if (st) st.textContent = anyPending ? '⏳ ждём генерации...' : '';
    if (!anyPending) {
      clearInterval(SD.pollTimer);
      SD.pollTimer = null;
    }
  };
  SD.pollTimer = setInterval(tick, 8000);
  tick();
}

// Recompute the duration the chunk_text would naturally need (sum of
// per-line durations + 1.5s padding, clamped to Seedance's 5-15s window).
// Mirrors the per-segment math in scriptToSegments() so manual chunks
// auto-fit without the user having to count seconds in their head.
function _sdRecommendedDurationFromChunkText(text) {
  const lines = (text || '').split(/\r?\n/);
  const contentSec = lines.reduce((s, ln) => s + (typeof _lineDuration === 'function' ? _lineDuration(ln) : 0), 0);
  if (contentSec <= 0) return null;  // nothing to estimate from
  const target = Math.ceil(contentSec + 1.5);
  return Math.max(5, Math.min(15, target));
}

function _sdAutoSetDurationIfManual() {
  const ta = document.getElementById('sd-chunk-text');
  const dur = document.getElementById('sd-duration');
  if (!ta || !dur) return;
  const rec = _sdRecommendedDurationFromChunkText(ta.value);
  if (rec == null) return;
  // Respect user override: if they've manually changed the slider away from
  // the prior auto-set value, don't fight them. Track the last auto value
  // on the element via dataset.autoVal — only update if the slider STILL
  // matches the previous auto-set (i.e. user hasn't touched it).
  const prevAuto = parseInt(dur.dataset.autoVal || '0', 10) || 0;
  const cur = parseInt(dur.value, 10) || 0;
  if (prevAuto && cur !== prevAuto) return;  // user moved the slider — leave it alone
  dur.value = String(rec);
  dur.dataset.autoVal = String(rec);
  dur.dispatchEvent(new Event('input'));   // refresh the "15с" label
  dur.dispatchEvent(new Event('change'));  // persist via sdSavePrefs
}

function sdInitForEpisode() {
  // Called when an episode opens
  if (SD.pollTimer) { clearInterval(SD.pollTimer); SD.pollTimer = null; }
  SD.refs = [];
  sdRenderRefs();
  sdLoadPrefs();
  // Also persist on change
  ['sd-duration','sd-resolution','sd-mod-bypass'].forEach(id => {
    const el = document.getElementById(id);
    if (el && !el._sdBound) { el.addEventListener('change', sdSavePrefs); el._sdBound = true; }
  });
  // Auto-set duration when user types/pastes a chunk into the manual textarea.
  const chunkTa = document.getElementById('sd-chunk-text');
  if (chunkTa && !chunkTa._sdAutoDurBound) {
    chunkTa.addEventListener('input', _sdAutoSetDurationIfManual);
    chunkTa.addEventListener('blur',  _sdAutoSetDurationIfManual);
    chunkTa._sdAutoDurBound = true;
  }
  // Mark the slider's current value as "user override" if user moves it manually
  // (so subsequent chunk-text changes don't clobber their choice).
  const durEl = document.getElementById('sd-duration');
  if (durEl && !durEl._sdManualBound) {
    durEl.addEventListener('input', () => {
      // If user dragged it away from the last auto value, clear the autoVal
      // marker so further auto-sets won't fire.
      const prev = parseInt(durEl.dataset.autoVal || '0', 10) || 0;
      if (prev && parseInt(durEl.value, 10) !== prev) durEl.dataset.autoVal = '';
    });
    durEl._sdManualBound = true;
  }
  // Live-update the slider value display
  const dur = document.getElementById('sd-duration');
  const durVal = document.getElementById('sd-duration-val');
  if (dur && durVal) {
    const sync = () => { durVal.textContent = dur.value + 'с'; };
    if (!dur._sdValBound) { dur.addEventListener('input', sync); dur._sdValBound = true; }
    sync();
  }
  if (document.body.classList.contains('seedance-mode')) {
    sdRefreshList().then(() => {
      // Auto-resume polling if anything is still pending
      api.get(`/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/list`).then(res => {
        const pending = (res.chunks || []).some(c => c.status !== 'completed' && c.status !== 'failed');
        if (pending) sdEnsurePoll();
      });
    });
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Montage / Timeline
// ─────────────────────────────────────────────────────────────────────────────
const MT = {
  clips: [],          // [{id, in, out, video_path, episode, chunk_idx, orig_duration, ...}]
  dragId: null,
  // Preview engine state:
  pxPerSec: 30,       // timeline scale
  globalTime: 0,      // current playhead position in TIMELINE seconds (sum of trimmed clips)
  playing: false,
  curIdx: -1,         // currently loaded clip index in <video>
  scrubbing: false,
};

function openMontage() {
  if (!S.seriesId) return;
  navigate('montage', { seriesId: S.seriesId });
}
function closeMontage() {
  navigate('series', { seriesId: S.seriesId });
}

function mtPlayheadKey() { return `mt_playhead_${S.seriesId}`; }
function mtSavePlayhead() {
  try {
    if (!S.seriesId) return;
    localStorage.setItem(mtPlayheadKey(), JSON.stringify({
      t: MT.globalTime, ts: Date.now(),
    }));
  } catch {}
}
function mtLoadPlayhead() {
  try {
    if (!S.seriesId) return 0;
    const raw = localStorage.getItem(mtPlayheadKey());
    if (!raw) return 0;
    const d = JSON.parse(raw);
    return Math.max(0, +d.t || 0);
  } catch { return 0; }
}

async function loadMontageView() {
  // Reset preview state (will be restored from localStorage below if data exists)
  MT.globalTime = 0; MT.curIdx = -1; MT.playing = false;
  const v = document.getElementById('mt-preview');
  if (v) { v.pause?.(); v.removeAttribute('src'); v.load?.(); }
  const playBtn = document.getElementById('mt-playbtn');
  if (playBtn) playBtn.textContent = '▶';
  // Title
  try {
    const s = await api.get(`/api/series/${S.seriesId}`);
    document.getElementById('montage-title').textContent = '🎬 Монтаж · ' + (s.title || '');
  } catch {}
  mtAttachPreviewListeners();
  await Promise.all([mtRefreshTimeline(), mtRefreshLibrary(), mtRefreshRenders()]);
  // Restore playhead position (per series, in localStorage)
  if (MT.clips.length) {
    const saved = mtLoadPlayhead();
    const total = mtTotalDur();
    mtSeek(Math.min(saved, Math.max(0, total - 0.1)));
  }
  // Re-position playhead on window resize / scroll
  if (!MT._respBound) {
    window.addEventListener('resize', mtUpdatePlayhead);
    document.getElementById('mt-timeline-wrap')?.addEventListener('scroll', mtUpdatePlayhead, true);
    document.getElementById('mt-timeline')?.addEventListener('scroll', mtUpdatePlayhead, true);
    // Cmd/Ctrl + wheel = zoom
    document.getElementById('mt-timeline')?.addEventListener('wheel', (e) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      e.preventDefault();
      mtZoom(e.deltaY < 0 ? 1 : -1);
    }, { passive: false });
    // Block horizontal page scroll / browser-back swipe anywhere on the
    // montage view, EXCEPT when the wheel happens over the timeline strip
    // (which has its own horizontal-scroll handler).
    document.getElementById('view-montage')?.addEventListener('wheel', (e) => {
      if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return; // only horizontal-intent
      // If event originated inside the timeline strip, let its handler do its job
      if (e.target.closest && e.target.closest('#mt-timeline')) return;
      e.preventDefault();
    }, { passive: false });
    // Cmd/Ctrl+Z while montage view is open
    document.addEventListener('keydown', (e) => {
      const view = document.getElementById('view-montage');
      if (!view || view.classList.contains('hidden')) return;
      // Ignore when typing in inputs/textareas
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z' && !e.shiftKey) {
        e.preventDefault();
        mtUndo();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && (
        (e.key.toLowerCase() === 'z' && e.shiftKey) || e.key.toLowerCase() === 'y'
      )) {
        e.preventDefault();
        mtRedo();
        return;
      }
      // Plain (no-modifier) playback shortcuts
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (!MT.clips || !MT.clips.length) return;
      if (e.code === 'Space') {
        e.preventDefault();
        mtTogglePlay();
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        mtStepFrame(-1);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        mtStepFrame(1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        mtJumpClip(-1);
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        mtJumpClip(1);
      } else if (e.key === 'c' || e.key === 'C' || e.key === 'с' || e.key === 'С') {
        e.preventDefault();
        mtCutAtPlayhead();
      } else if (e.key === 'q' || e.key === 'Q' || e.key === 'й' || e.key === 'Й') {
        e.preventDefault();
        mtTrimToPlayhead('left');
      } else if (e.key === 'w' || e.key === 'W' || e.key === 'ц' || e.key === 'Ц') {
        e.preventDefault();
        mtTrimToPlayhead('right');
      } else if (e.key === 'Home') {
        e.preventDefault();
        mtSeekToStart();
      } else if (e.key === 'End') {
        e.preventDefault();
        mtSeekToEnd();
      } else if (e.key === 'Backspace' || e.key === 'Delete') {
        e.preventDefault();
        if (MT.curIdx >= 0 && MT.clips[MT.curIdx]) {
          mtRemoveClip(MT.clips[MT.curIdx].id);
        } else {
          showToast('Поставь playhead на клип');
        }
      }
    });
    MT._respBound = true;
  }
}

async function mtRefreshTimeline() {
  try {
    const res = await api.get(`/api/series/${S.seriesId}/timeline`);
    MT.clips = res.clips || [];
    mtRenderTimeline();
    mtRefreshUndoBtn();
  } catch (e) { console.error(e); }
}

async function mtRefreshUndoBtn() {
  const undo = document.getElementById('mt-undo-btn');
  const redo = document.getElementById('mt-redo-btn');
  if (!undo) return;
  try {
    const h = await api.get(`/api/series/${S.seriesId}/timeline/history`);
    undo.disabled = !h.depth;
    undo.title = h.depth
      ? `Отменить (⌘Z) — ${h.depth} в стеке, последнее: ${h.last?.label || ''}`
      : 'Нечего отменять';
    if (redo) {
      redo.disabled = !h.redo_depth;
      redo.title = h.redo_depth
        ? `Вернуть (⌘⇧Z) — ${h.redo_depth} в стеке, следующее: ${h.next?.label || ''}`
        : 'Нечего возвращать';
    }
  } catch {}
}

async function mtUndo() {
  const btn = document.getElementById('mt-undo-btn');
  if (btn?.disabled) return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/timeline/undo`, {});
    showToast(`↶ Отменено: ${r.restored || ''}`);
    await mtAfterHistoryChange();
  } catch (e) {
    showToast('✗ ' + (e.message || e));
  }
}

async function mtRedo() {
  const btn = document.getElementById('mt-redo-btn');
  if (btn?.disabled) return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/timeline/redo`, {});
    showToast(`↷ Возвращено: ${r.restored || ''}`);
    await mtAfterHistoryChange();
  } catch (e) {
    showToast('✗ ' + (e.message || e));
  }
}

async function mtAfterHistoryChange() {
  await mtRefreshTimeline();
  const total = mtTotalDur();
  if (MT.globalTime > total) MT.globalTime = Math.max(0, total - 0.5);
  if (MT.clips.length) mtSeek(MT.globalTime);
}

function mtFmtSec(s) { return (Math.round((s || 0) * 10) / 10) + 'с'; }

function mtRenderTimeline() {
  const el = document.getElementById('mt-timeline');
  const empty = document.getElementById('mt-empty');
  const summary = document.getElementById('mt-summary');
  const playhead = document.getElementById('mt-playhead');
  if (!el) return;
  const totalDur = mtTotalDur();
  summary.textContent = MT.clips.length
    ? `${MT.clips.length} клип(ов) · ~${mtFmtSec(totalDur)}` : '';
  if (!MT.clips.length) {
    el.innerHTML = '';
    empty?.classList.remove('hidden');
    if (playhead) playhead.classList.add('hidden');
    mtUpdateTimeLabel();
    return;
  }
  empty?.classList.add('hidden');
  el.innerHTML = MT.clips.map((c, i) => {
    const url = c.video_path ? `${assetUrl(c.video_path)}` : '';
    const dur = Math.max(0.1, (c.out || 0) - (c.in || 0));
    const w = Math.max(70, dur * MT.pxPerSec);
    const trimMark = (c.in > 0.05 || (c.orig_duration && Math.abs(c.out - c.orig_duration) > 0.05))
      ? ' ✂' : '';
    const origDur = c.orig_duration || c.out || 0;
    return `
      <div class="mt-clip${i === MT.curIdx ? ' is-active' : ''}" draggable="true" data-id="${c.id}"
           style="width:${w}px"
           ondragstart="mtDragStart(event,'${c.id}')"
           ondragover="mtDragOver(event,'${c.id}')"
           ondragleave="mtDragLeave(event)"
           ondrop="mtDrop(event,'${c.id}')"
           ondragend="mtDragEnd(event)">
        <div class="mt-trim-handle left"  title="Тяни — обрезать слева"
             onmousedown="mtTrimStart(event,'${c.id}','in',${origDur})"></div>
        <div class="mt-trim-handle right" title="Тяни — обрезать справа"
             onmousedown="mtTrimStart(event,'${c.id}','out',${origDur})"></div>
        ${url ? `<video src="${url}#t=${c.in||0},${c.out||0}" preload="metadata" muted></video>`
              : `<div style="height:180px;background:#000;color:#888;display:flex;align-items:center;justify-content:center;font-size:11px">нет видео</div>`}
        <div class="mt-clip-meta">
          #${i+1} · <span class="ep">ep${c.episode}·#${c.chunk_idx}</span><br>
          ${mtFmtSec(dur)}${trimMark}${c.crop ? ' <span class="crop-mark">▢</span>' : ''}
        </div>
        <div class="mt-clip-actions">
          <button onclick="mtSeekToClip(${i})" title="Перейти к началу клипа">⏵</button>
          <button onclick="cropOpen('${c.id}')" title="Crop &amp; position">▢</button>
          <button class="danger" onclick="mtRemoveClip('${c.id}')" title="Убрать">✕</button>
        </div>
      </div>
    `;
  }).join('');
  mtRenderRuler();
  // Bind ruler scrubbing once
  const ruler = document.getElementById('mt-ruler');
  if (ruler && !ruler._mtBound) {
    ruler.addEventListener('mousedown', mtScrubStart);
    ruler.style.cursor = 'pointer';
    ruler._mtBound = true;
  }
  // Click on the strip to scrub
  if (!el._mtBound) {
    el.addEventListener('mousedown', mtScrubStart);
    el.addEventListener('dragover',  mtStripDragOver);
    el.addEventListener('dragleave', mtStripDragLeave);
    el.addEventListener('drop',      mtStripDrop);
    // Sync ruler scroll with timeline scroll
    el.addEventListener('scroll', () => {
      const r = document.getElementById('mt-ruler');
      if (r) r.scrollLeft = el.scrollLeft;
    });
    // Capture wheel/trackpad — scroll the timeline horizontally instead of
    // letting the page scroll. Vertical wheel translates to horizontal too.
    el.addEventListener('wheel', (ev) => {
      const dx = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
      if (dx === 0) return;
      // Only consume if there is room to scroll in that direction
      const max = el.scrollWidth - el.clientWidth;
      const before = el.scrollLeft;
      el.scrollLeft = Math.max(0, Math.min(max, before + dx));
      if (el.scrollLeft !== before || max > 0) ev.preventDefault();
    }, { passive: false });
    el._mtBound = true;
  }
  mtUpdatePlayhead();
}

function mtTotalDur() {
  return MT.clips.reduce((a, c) => a + Math.max(0, (c.out || 0) - (c.in || 0)), 0);
}

// Map global timeline time → {idx, localOffset}
function mtTimeToClip(t) {
  let acc = 0;
  for (let i = 0; i < MT.clips.length; i++) {
    const c = MT.clips[i];
    const d = Math.max(0, (c.out || 0) - (c.in || 0));
    if (t < acc + d || i === MT.clips.length - 1) {
      return { idx: i, local: Math.max(0, Math.min(d, t - acc)) };
    }
    acc += d;
  }
  return { idx: -1, local: 0 };
}

// Map global time → x px on timeline strip
function mtTimeToPx(t) {
  let acc = 0, x = 0;
  for (const c of MT.clips) {
    const d = Math.max(0.1, (c.out || 0) - (c.in || 0));
    const w = Math.max(70, d * MT.pxPerSec);
    if (t <= acc + d) {
      const frac = d > 0 ? (t - acc) / d : 0;
      return x + frac * w;
    }
    acc += d; x += w + 8 /* gap */;
  }
  return x;
}

// Map x px on strip → global time
function mtPxToTime(px) {
  let acc = 0, x = 0;
  for (const c of MT.clips) {
    const d = Math.max(0.1, (c.out || 0) - (c.in || 0));
    const w = Math.max(70, d * MT.pxPerSec);
    if (px <= x + w) {
      const frac = w > 0 ? (px - x) / w : 0;
      return acc + frac * d;
    }
    acc += d; x += w + 8;
  }
  return acc;
}

function mtUpdatePlayhead() {
  const ph = document.getElementById('mt-playhead');
  const strip = document.getElementById('mt-timeline');
  if (!ph || !strip || !MT.clips.length) return;
  const px = mtTimeToPx(MT.globalTime);
  // Strip scrolls horizontally on its own (overflow-x:auto on .mt-timeline).
  // Wrap is non-scrolling parent; playhead lives inside wrap.
  // Visible left within wrap = strip-padding + px − strip.scrollLeft.
  const stripRect = strip.getBoundingClientRect();
  const wrapRect = strip.parentElement.getBoundingClientRect();
  const baseOffset = stripRect.left - wrapRect.left; // usually 0
  const left = baseOffset + 12 /* strip padding */ + px - strip.scrollLeft;
  // Hide if scrolled out of visible area
  const visible = (left >= baseOffset - 2) && (left <= baseOffset + strip.clientWidth + 2);
  ph.style.left = left + 'px';
  ph.classList.toggle('hidden', !visible);
}

function mtFmtTime(s) {
  // MM:SS.t  (one decimal)
  s = Math.max(0, s || 0);
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return `${String(m).padStart(2,'0')}:${sec.toFixed(1).padStart(4,'0')}`;
}
function mtUpdateTimeLabel() {
  const lbl = document.getElementById('mt-time');
  if (!lbl) return;
  lbl.textContent = `${mtFmtTime(MT.globalTime)} / ${mtFmtTime(mtTotalDur())}`;
}

function mtRenderRuler() {
  const inner = document.getElementById('mt-ruler-inner');
  const strip = document.getElementById('mt-timeline');
  if (!inner || !strip) return;
  const total = mtTotalDur();
  const pps = MT.pxPerSec || 30;
  const padLeft = 12; // matches .mt-timeline padding
  const widthPx = Math.max(strip.scrollWidth, total * pps + padLeft * 2);
  inner.style.width = widthPx + 'px';
  if (total <= 0) { inner.innerHTML = ''; return; }
  // Pick a tick step that gives ~50-100 px between major labels
  const targetMajorPx = 80;
  const candidates = [1, 2, 5, 10, 15, 30, 60, 120, 300];
  let major = candidates[candidates.length - 1];
  for (const c of candidates) {
    if (c * pps >= targetMajorPx) { major = c; break; }
  }
  const minor = major / (major >= 5 ? 5 : (major >= 2 ? 2 : 1));
  const parts = [];
  for (let t = 0; t <= total + 0.001; t += minor) {
    const x = padLeft + t * pps;
    const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
    parts.push(`<div class="mt-ruler-tick ${isMajor ? 'major' : 'minor'}" style="left:${x}px"></div>`);
    if (isMajor) {
      parts.push(`<div class="mt-ruler-label" style="left:${x}px">${mtFmtTime(t)}</div>`);
    }
  }
  inner.innerHTML = parts.join('');
}

function mtScrubStart(e) {
  if (!MT.clips.length) return;
  if (e.target.closest('button') || e.target.closest('.mt-clip-actions')) return;
  MT.scrubbing = true;
  mtScrubMove(e);
  document.addEventListener('mousemove', mtScrubMove);
  document.addEventListener('mouseup', mtScrubEnd, { once: true });
}
function mtScrubMove(e) {
  if (!MT.scrubbing) return;
  const strip = document.getElementById('mt-timeline');
  if (!strip) return;
  const rect = strip.getBoundingClientRect();
  const px = e.clientX - rect.left - 12 /* padding */ + strip.scrollLeft;
  const t = Math.max(0, Math.min(mtTotalDur(), mtPxToTime(px)));
  mtSeek(t);
}
function mtScrubEnd() {
  MT.scrubbing = false;
  document.removeEventListener('mousemove', mtScrubMove);
}

// ─── Trim handles ───
function mtTrimStart(e, clipId, edge /* 'in'|'out' */, origDur) {
  e.preventDefault();
  e.stopPropagation();
  const clip = MT.clips.find(c => c.id === clipId);
  if (!clip) return;
  const clipEl = document.querySelector(`.mt-clip[data-id="${clipId}"]`);
  if (!clipEl) return;
  const handleEl = e.currentTarget;
  // Disable native HTML5 drag-and-drop on the parent during trim
  clipEl.setAttribute('draggable', 'false');
  clipEl.classList.add('is-trimming');
  handleEl.classList.add('active');

  const startX  = e.clientX;
  const startIn  = clip.in  || 0;
  const startOut = clip.out || 0;
  const maxOut = origDur || startOut;

  // Tooltip
  const tip = document.createElement('div');
  tip.className = 'mt-trim-tooltip';
  document.body.appendChild(tip);

  const onMove = (ev) => {
    const dxSec = (ev.clientX - startX) / MT.pxPerSec;
    let newIn = startIn, newOut = startOut;
    if (edge === 'in')  newIn  = Math.max(0, Math.min(startOut - 0.2, startIn + dxSec));
    else                newOut = Math.max(startIn + 0.2, Math.min(maxOut, startOut + dxSec));
    clip.in = newIn; clip.out = newOut;
    // Live re-render: just update width + meta
    const dur = newOut - newIn;
    const w = Math.max(70, dur * MT.pxPerSec);
    clipEl.style.width = w + 'px';
    const meta = clipEl.querySelector('.mt-clip-meta');
    if (meta) meta.innerHTML = meta.innerHTML.replace(/^.+/, '');  // we won't bother updating text live
    tip.textContent = `${edge === 'in' ? '▶' : '◀'} ${mtFmtSec(dur)} (${mtFmtSec(newIn)}…${mtFmtSec(newOut)})`;
    tip.style.left = ev.clientX + 'px';
    tip.style.top  = ev.clientY + 'px';
    mtUpdateTimeLabel();
  };

  const onUp = async () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    handleEl.classList.remove('active');
    clipEl.classList.remove('is-trimming');
    clipEl.setAttribute('draggable', 'true');
    tip.remove();
    // PATCH if values actually changed
    if (Math.abs(clip.in - startIn) > 0.01 || Math.abs(clip.out - startOut) > 0.01) {
      try {
        const r = await fetch(
          `/api/series/${S.seriesId}/timeline/clips/${clipId}/trim`,
          {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ in: clip.in, out: clip.out }),
          }
        );
        if (!r.ok) throw new Error(await r.text());
        await mtRefreshTimeline();
        // Keep playhead within bounds
        const total = mtTotalDur();
        if (MT.globalTime > total) MT.globalTime = Math.max(0, total - 0.1);
        mtSeek(MT.globalTime);
      } catch (err) {
        showToast('✗ trim: ' + (err.message || err));
        await mtRefreshTimeline();
      }
    } else {
      // reset just in case width drifted
      mtRenderTimeline();
    }
  };

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

// Seek to a specific global time + sync video element
function mtSeek(t) {
  MT.globalTime = t;
  const { idx, local } = mtTimeToClip(t);
  mtLoadClipIntoPlayer(idx, local);
  mtUpdatePlayhead();
  mtUpdateTimeLabel();
  mtSavePlayhead();
  // Auto-scroll strip so playhead stays in view
  const strip = document.getElementById('mt-timeline');
  if (strip) {
    const px = mtTimeToPx(t) + 12 /* strip padding */;
    const margin = 40;
    if (px < strip.scrollLeft + margin) strip.scrollLeft = Math.max(0, px - margin);
    else if (px > strip.scrollLeft + strip.clientWidth - margin)
      strip.scrollLeft = px - strip.clientWidth + margin;
  }
}

function mtLoadClipIntoPlayer(idx, localOffset = 0) {
  const v = document.getElementById('mt-preview');
  if (!v) return;
  if (idx < 0 || idx >= MT.clips.length) return;
  const c = MT.clips[idx];
  if (!c.video_path) return;
  const url = `${assetUrl(c.video_path)}`;
  const targetTime = (c.in || 0) + localOffset;
  if (MT.curIdx !== idx) {
    MT.curIdx = idx;
    v.src = url;
    v.addEventListener('loadedmetadata', function once() {
      v.currentTime = targetTime;
      mtApplyPreviewCrop();
      v.removeEventListener('loadedmetadata', once);
    });
    // Mark active clip
    document.querySelectorAll('.mt-clip').forEach((el, i) => {
      el.classList.toggle('is-active', i === idx);
    });
  } else {
    if (Math.abs(v.currentTime - targetTime) > 0.05) v.currentTime = targetTime;
    mtApplyPreviewCrop();
  }
}

// Apply crop fractions of the current clip to the preview <video> via CSS scale + translate.
// Container is .mt-preview-frame (W×H). To make crop window [fx..fx+fw]×[fy..fy+fh]
// fill the container: video CSS size = W/fw × H/fh, translate(-fx*W/fw, -fy*H/fh).
function mtApplyPreviewCrop() {
  const v = document.getElementById('mt-preview');
  const frame = v && v.parentElement;
  if (!v || !frame) return;
  const c = MT.clips[MT.curIdx];
  const crop = c && c.crop;
  // Backend stores {x,y,w,h} as fractions of source frame
  const fx = crop ? (crop.x ?? crop.fx) : null;
  const fy = crop ? (crop.y ?? crop.fy) : null;
  const fw = crop ? (crop.w ?? crop.fw) : null;
  const fh = crop ? (crop.h ?? crop.fh) : null;
  const trivial = !crop || fw == null
    || (fw >= 0.999 && fh >= 0.999 && (fx ?? 0) < 0.001 && (fy ?? 0) < 0.001);
  if (trivial) {
    v.classList.remove('cropped');
    v.style.width = '100%';
    v.style.height = '100%';
    v.style.transform = 'none';
    return;
  }
  v.classList.add('cropped');
  const W = frame.clientWidth || 240;
  const H = frame.clientHeight || 360;
  const cfx = Math.max(0, Math.min(1, fx || 0));
  const cfy = Math.max(0, Math.min(1, fy || 0));
  const cfw = Math.max(0.01, Math.min(1, fw || 1));
  const cfh = Math.max(0.01, Math.min(1, fh || 1));
  const dispW = W / cfw;
  const dispH = H / cfh;
  v.style.width = dispW + 'px';
  v.style.height = dispH + 'px';
  v.style.transform = `translate(${-cfx * dispW}px, ${-cfy * dispH}px)`;
}

// Zoom timeline (px per second). Keeps playhead anchored in viewport.
function mtZoom(dir) {
  const STEPS = [10, 15, 20, 30, 45, 60, 90, 120, 180, 240];
  const cur = MT.pxPerSec;
  let i = STEPS.findIndex(s => s >= cur);
  if (i < 0) i = STEPS.length - 1;
  if (dir > 0) i = Math.min(STEPS.length - 1, i + 1);
  else i = Math.max(0, i - 1);
  mtSetZoom(STEPS[i]);
}
function mtZoomFit() {
  const strip = document.getElementById('mt-timeline');
  const total = mtTotalDur();
  if (!strip || total <= 0) return;
  const avail = Math.max(100, strip.clientWidth - 24); // minus padding
  // ensure clips can shrink: minimum is what mtRenderTimeline enforces (Math.max(70, dur*pxPerSec))
  // so target is avail/total
  let v = Math.max(8, Math.min(240, Math.floor(avail / total)));
  mtSetZoom(v);
}
function mtSetZoom(v) {
  // Anchor: keep playhead pixel position stable in viewport
  const strip = document.getElementById('mt-timeline');
  const oldPx = mtTimeToPx(MT.globalTime);
  const visOld = strip ? (oldPx - strip.scrollLeft) : 0;
  MT.pxPerSec = v;
  const lbl = document.getElementById('mt-zoom-val');
  if (lbl) lbl.textContent = v + ' px/s';
  mtRenderTimeline();
  // Restore: scroll strip so playhead lands at same viewport offset
  if (strip) {
    const newPx = mtTimeToPx(MT.globalTime);
    strip.scrollLeft = Math.max(0, newPx - visOld);
  }
  mtUpdatePlayhead();
}

// Jump playhead to the previous/next clip boundary (cut between clips).
// dir = -1 → previous boundary, +1 → next.
// Boundaries are at the start of each clip (0, dur(0), dur(0)+dur(1), …) and
// at the end of the last clip (= total duration).
function mtJumpClip(dir) {
  if (!MT.clips || !MT.clips.length) return;
  const v = document.getElementById('mt-preview');
  if (v && !v.paused) { v.pause(); MT.playing = false;
    const btn = document.getElementById('mt-playbtn'); if (btn) btn.textContent = '▶'; }
  // Build sorted list of boundary times
  const bounds = [0];
  let acc = 0;
  for (const c of MT.clips) {
    acc += Math.max(0, (c.out || 0) - (c.in || 0));
    bounds.push(acc);
  }
  const t = MT.globalTime || 0;
  const eps = 0.01;
  let target;
  if (dir < 0) {
    // largest boundary strictly less than t (with small epsilon to avoid getting stuck)
    target = bounds.filter(b => b < t - eps).pop();
    if (target == null) target = 0;
  } else {
    target = bounds.find(b => b > t + eps);
    if (target == null) target = bounds[bounds.length - 1];
  }
  mtSeek(target);
}

// Step the playhead by ±1 frame (assume 30fps).
function mtStepFrame(dir) {
  if (!MT.clips.length) return;
  const v = document.getElementById('mt-preview');
  if (v && !v.paused) { v.pause(); MT.playing = false;
    const btn = document.getElementById('mt-playbtn'); if (btn) btn.textContent = '▶'; }
  const fps = 30;
  const total = MT.clips.reduce((a, c) => a + Math.max(0, (c.out || 0) - (c.in || 0)), 0);
  let t = (MT.globalTime || 0) + (dir > 0 ? 1 : -1) / fps;
  t = Math.max(0, Math.min(total - 0.001, t));
  mtSeek(t);
}

function mtTogglePlay() {
  const v = document.getElementById('mt-preview');
  if (!v) return;
  if (!MT.clips.length) return;
  if (MT.curIdx < 0) mtSeek(0);
  if (v.paused) {
    v.play();
    MT.playing = true;
    document.getElementById('mt-playbtn').textContent = '⏸';
  } else {
    v.pause();
    MT.playing = false;
    document.getElementById('mt-playbtn').textContent = '▶';
  }
}

function mtSeekToStart() { mtSeek(0); }
function mtSeekToEnd() {
  const total = mtTotalDur();
  if (total <= 0) return;
  // Land just-before-end so playhead is visible / video doesn't auto-stop awkwardly
  mtSeek(Math.max(0, total - 0.05));
}
function mtSeekToClip(idx) {
  // Compute global time at start of clip idx
  let t = 0;
  for (let i = 0; i < idx; i++) {
    t += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
  }
  mtSeek(t);
}

function mtAttachPreviewListeners() {
  const v = document.getElementById('mt-preview');
  if (!v || v._mtBound) return;
  v.addEventListener('timeupdate', () => {
    if (MT.curIdx < 0 || MT.scrubbing) return;
    const c = MT.clips[MT.curIdx];
    if (!c) return;
    // If we ran past the trim out → switch to next clip
    if (v.currentTime >= (c.out || 0) - 0.02) {
      const nextIdx = MT.curIdx + 1;
      if (nextIdx < MT.clips.length) {
        // Recompute global time for next clip start
        let t = 0;
        for (let i = 0; i < nextIdx; i++) t += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
        MT.globalTime = t;
        mtLoadClipIntoPlayer(nextIdx, 0);
        if (MT.playing) v.play();
      } else {
        v.pause();
        MT.playing = false;
        document.getElementById('mt-playbtn').textContent = '▶';
      }
      mtUpdateTimeLabel();
      mtUpdatePlayhead();
      return;
    }
    // Translate local video time → global timeline time
    let t = 0;
    for (let i = 0; i < MT.curIdx; i++) t += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
    MT.globalTime = t + Math.max(0, v.currentTime - (c.in || 0));
    mtUpdatePlayhead();
    mtUpdateTimeLabel();
  });
  v._mtBound = true;
}

async function mtTrimToPlayhead(side /* 'left' | 'right' */) {
  if (MT.curIdx < 0 || !MT.clips[MT.curIdx]) { showToast('Поставь playhead на клип'); return; }
  const c = MT.clips[MT.curIdx];
  let pre = 0;
  for (let i = 0; i < MT.curIdx; i++) pre += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
  const at = MT.globalTime - pre;                 // offset inside trimmed clip
  const dur = (c.out || 0) - (c.in || 0);
  if (at < 0.1 || at > dur - 0.1) { showToast('Слишком близко к краю'); return; }
  const splitAbs = (c.in || 0) + at;              // absolute seconds in original media
  const newIn  = side === 'left'  ? splitAbs : (c.in  || 0);
  const newOut = side === 'right' ? splitAbs : (c.out || 0);
  try {
    const r = await fetch(
      `/api/series/${S.seriesId}/timeline/clips/${c.id}/trim`,
      { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ in: newIn, out: newOut }) }
    );
    if (!r.ok) throw new Error(await r.text());
    showToast(side === 'left' ? '✂ обрезано слева' : '✂ обрезано справа');
    await mtRefreshTimeline();
    const total = mtTotalDur();
    if (MT.globalTime > total) MT.globalTime = Math.max(0, total - 0.1);
    mtSeek(MT.globalTime);
  } catch (e) { showToast('✗ trim: ' + (e.message || e)); }
}

async function mtCutAtPlayhead() {
  if (MT.curIdx < 0 || !MT.clips[MT.curIdx]) { showToast('Поставь playhead на клип'); return; }
  const c = MT.clips[MT.curIdx];
  // Local offset (within trimmed clip): globalTime - sum of preceding durations
  let pre = 0;
  for (let i = 0; i < MT.curIdx; i++) pre += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
  const at = MT.globalTime - pre;
  const dur = (c.out || 0) - (c.in || 0);
  if (at < 0.1 || at > dur - 0.1) { showToast('Слишком близко к краю'); return; }
  try {
    await api.post(`/api/series/${S.seriesId}/timeline/clips/${c.id}/split`, { at });
    showToast('✂ разрезано');
    await mtRefreshTimeline();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
}

function mtDragStart(e, id) {
  MT.dragId = id;
  e.dataTransfer.effectAllowed = 'move';
  e.currentTarget.classList.add('dragging');
}
function mtDragOver(e, id) {
  // Allow drop if reordering an existing clip OR dropping a library item
  if (MT.libDragRef) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    e.currentTarget.classList.add('drop-target');
    return;
  }
  if (!MT.dragId || MT.dragId === id) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drop-target');
}
function mtDragLeave(e) { e.currentTarget.classList.remove('drop-target'); }
function mtDragEnd(e) {
  document.querySelectorAll('.mt-clip').forEach(el => {
    el.classList.remove('dragging','drop-target');
  });
  const strip = document.getElementById('mt-timeline');
  if (strip) strip.classList.remove('drop-target');
  MT.dragId = null;
}
async function mtDrop(e, targetId) {
  e.preventDefault();
  e.currentTarget.classList.remove('drop-target');
  // Library drop → insert before target clip
  if (MT.libDragRef) {
    const ref = MT.libDragRef;
    MT.libDragRef = null;
    await mtInsertFromLib(ref.ep, ref.idx, targetId);
    return;
  }
  if (!MT.dragId || MT.dragId === targetId) return;
  const order = MT.clips.map(c => c.id);
  const fromIdx = order.indexOf(MT.dragId);
  const toIdx = order.indexOf(targetId);
  if (fromIdx < 0 || toIdx < 0) return;
  order.splice(toIdx, 0, order.splice(fromIdx, 1)[0]);
  // optimistic reorder
  const byId = Object.fromEntries(MT.clips.map(c => [c.id, c]));
  MT.clips = order.map(id => byId[id]);
  mtRenderTimeline();
  try {
    await api.post(`/api/series/${S.seriesId}/timeline/reorder`, { order });
  } catch (err) {
    showToast('✗ reorder: ' + (err.message || err));
    await mtRefreshTimeline();
  }
}

// ─── Library → timeline drag ──────────────────────────────────────────────
function mtLibDragStart(e, ep, idx) {
  MT.libDragRef = { ep, idx };
  e.dataTransfer.effectAllowed = 'copy';
  try { e.dataTransfer.setData('text/plain', `lib:${ep}:${idx}`); } catch {}
  e.currentTarget.classList.add('dragging');
}
function mtLibDragEnd(e) {
  document.querySelectorAll('.mt-lib-item').forEach(el => el.classList.remove('dragging'));
  document.querySelectorAll('.mt-clip').forEach(el => el.classList.remove('drop-target'));
  const strip = document.getElementById('mt-timeline');
  if (strip) strip.classList.remove('drop-target');
  MT.libDragRef = null;
}
function mtStripDragOver(e) {
  if (!MT.libDragRef) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
  document.getElementById('mt-timeline')?.classList.add('drop-target');
}
function mtStripDragLeave(e) {
  document.getElementById('mt-timeline')?.classList.remove('drop-target');
}
async function mtStripDrop(e) {
  if (!MT.libDragRef) return;
  e.preventDefault();
  document.getElementById('mt-timeline')?.classList.remove('drop-target');
  const ref = MT.libDragRef;
  MT.libDragRef = null;
  // If drop landed on a child .mt-clip, the clip's own drop fired already; skip.
  if (e.target.closest && e.target.closest('.mt-clip')) return;
  await mtInsertFromLib(ref.ep, ref.idx, null); // append
}

async function mtInsertFromLib(ep, idx, targetClipId /* null = append */) {
  try {
    const r = await api.post(`/api/series/${S.seriesId}/timeline/clips/add`, {
      episode: ep, chunk_idx: idx,
    });
    const newId = r.clip?.id;
    if (newId && targetClipId) {
      const order = MT.clips.map(c => c.id);
      const tIdx = order.indexOf(targetClipId);
      // newly added clip is appended on the server; build new order:
      const newOrder = order.slice();
      if (tIdx >= 0) newOrder.splice(tIdx, 0, newId);
      else newOrder.push(newId);
      await api.post(`/api/series/${S.seriesId}/timeline/reorder`, { order: newOrder });
    }
    showToast('✓ Добавлено');
    await mtRefreshTimeline();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
}

async function mtRemoveClip(id) {
  await api.del(`/api/series/${S.seriesId}/timeline/clips/${id}`);
  await mtRefreshTimeline();
}


// Library: aggregate all completed seedance chunks across episodes
async function mtRefreshLibrary() {
  const lib = document.getElementById('mt-library');
  if (!lib) return;
  const filterSel = document.getElementById('mt-lib-filter');
  const countEl = document.getElementById('mt-lib-count');
  lib.innerHTML = '<div class="muted" style="font-size:12px">Загружаю...</div>';
  try {
    const eps = await api.get(`/api/series/${S.seriesId}/episodes`);
    const epList = (eps.episodes || eps || []);
    const allItems = [];
    for (const ep of epList) {
      try {
        const r = await api.get(`/api/series/${S.seriesId}/episodes/${ep.number}/seedance/list`);
        for (const c of (r.chunks || [])) {
          if (c.status === 'completed' && c.video_path) {
            allItems.push({ ep: ep.number, ep_title: ep.title || '', ...c });
          }
        }
      } catch {}
    }
    // Populate filter dropdown (preserve current selection)
    if (filterSel) {
      const prev = filterSel.value || 'all';
      const epsWithClips = [...new Set(allItems.map(c => c.ep))].sort((a,b) => a-b);
      const opts = ['<option value="all">Все эпизоды</option>']
        .concat(epsWithClips.map(n => {
          const t = (epList.find(e => e.number === n)?.title || '').slice(0, 24);
          return `<option value="${n}">Эпизод ${n}${t ? ' · ' + esc(t) : ''}</option>`;
        }));
      filterSel.innerHTML = opts.join('');
      // restore previous filter if still valid
      if ([...filterSel.options].some(o => o.value === prev)) filterSel.value = prev;
    }
    const filterVal = filterSel?.value || 'all';
    const items = filterVal === 'all'
      ? allItems
      : allItems.filter(c => String(c.ep) === String(filterVal));
    if (countEl) countEl.textContent = `${items.length} из ${allItems.length}`;
    if (!items.length) {
      lib.innerHTML = allItems.length
        ? '<div class="muted" style="font-size:12px">В этом эпизоде нет готовых клипов.</div>'
        : '<div class="muted" style="font-size:12px">Нет готовых видео. Сгенерируй что-нибудь в эпизодах через Seedance.</div>';
      return;
    }
    lib.innerHTML = items.map(c => {
      const url = `${assetUrl(c.video_path)}`;
      return `
        <div class="mt-lib-item" draggable="true"
             ondragstart="mtLibDragStart(event,${c.ep},${c.idx})"
             ondragend="mtLibDragEnd(event)"
             title="Клик — фуллскрин · перетащи на таймлайн">
          <video data-src="${url}" preload="none" muted onclick="mtLibFullscreen(event,'${url}')" style="cursor:zoom-in"></video>
          <div class="mt-lib-meta">
            <div class="ep">ep${c.ep}·#${c.idx} · ${c.duration}с</div>
            <div class="preview">${esc((c.prompt||'').slice(0,80))}</div>
            <button class="mt-lib-add" onclick="mtAddFromLib(${c.ep},${c.idx},this)">➕ На таймлайн</button>
          </div>
        </div>`;
    }).join('');
    // Hover-scrub: водишь мышью по превью — кадры пролистываются.
    // Lazy-load (preload=none → src on first hover) + throttle seeks to ~30 fps
    // so браузер не захлёбывался range-запросами при большой библиотеке.
    if (!lib._mtHoverBound) {
      let lastMove = 0;
      let pendingFrac = 0;
      let pendingVid  = null;
      const flush = () => {
        if (!pendingVid) return;
        const v = pendingVid;
        const dur = v.duration;
        if (isFinite(dur) && dur > 0) {
          try { v.currentTime = pendingFrac * dur; } catch {}
        }
        pendingVid = null;
      };
      const onMove = (e) => {
        const v = e.target.closest('.mt-lib-item video');
        if (!v) return;
        // Lazy-attach src on first hover so we don't fan out 20+ metadata requests
        if (!v.src && v.dataset.src) {
          v.src = v.dataset.src;
          v.preload = 'metadata';
        }
        const rect = v.getBoundingClientRect();
        const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        pendingFrac = frac;
        pendingVid  = v;
        const now = performance.now();
        if (now - lastMove >= 33) {       // ~30 fps cap
          lastMove = now;
          flush();
        }
      };
      const resetVid = (v) => { try { v.currentTime = 0; } catch {} };
      lib.addEventListener('mousemove', onMove);
      lib.addEventListener('mouseout', (e) => {
        const v = e.target.closest && e.target.closest('.mt-lib-item video');
        if (!v) return;
        const item = v.closest('.mt-lib-item');
        if (!item.contains(e.relatedTarget)) {
          if (pendingVid === v) pendingVid = null;
          resetVid(v);
        }
      });
      lib._mtHoverBound = true;
    }
  } catch (e) {
    lib.innerHTML = '<div class="muted">Ошибка загрузки</div>';
  }
}

function mtLibFullscreen(e, url) {
  // Stop the click from bubbling (e.g. starting a drag-related side-effect)
  e.stopPropagation();
  e.preventDefault();
  // Build a one-shot overlay with a big video player
  const old = document.getElementById('mt-lib-fs');
  if (old) old.remove();
  const overlay = document.createElement('div');
  overlay.id = 'mt-lib-fs';
  overlay.className = 'mt-lib-fs-overlay';
  overlay.innerHTML = `
    <button class="mt-lib-fs-close" title="Закрыть (Esc)">✕</button>
    <video src="${url}" controls autoplay playsinline></video>
  `;
  const close = () => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (ev) => { if (ev.key === 'Escape') close(); };
  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay || ev.target.classList.contains('mt-lib-fs-close')) close();
  });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(overlay);
}

async function mtAddFromLib(ep, idx, btn) {
  if (btn) btn.disabled = true;
  try {
    await api.post(`/api/series/${S.seriesId}/timeline/clips/add`, {
      episode: ep, chunk_idx: idx,
    });
    showToast('✓ Добавлено');
    await mtRefreshTimeline();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
  if (btn) btn.disabled = false;
}

async function mtRender() {
  if (!MT.clips.length) { showToast('Таймлайн пустой'); return; }
  const btn = document.getElementById('mt-render-btn');
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Рендерю...'; }
  try {
    const res = await api.post(`/api/series/${S.seriesId}/timeline/render`, {});
    showToast(`✓ Готово · ${res.size_mb} МБ · ${res.mode}`);
    await mtRefreshRenders();
    // Trigger immediate browser download of the freshly-rendered file.
    // User asked for this — previously they had to scroll down to the
    // renders list to grab the file. Now a download starts automatically
    // the moment the render completes.
    if (res.url) _mtTriggerDownload(res.url, res.path);
  } catch (e) {
    showToast('✗ ' + (e.message || e), 7000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old || '▶ Собрать MP4'; }
  }
}

// Triggers a browser download for an asset URL. Uses a hidden <a download>
// because window.open() / location.href would navigate away from the page.
// The `path` arg is the relative path inside the series — its basename
// becomes the suggested filename so the user gets a sensible name in
// their Downloads folder rather than the random "render-xxxxx.mp4" UUID.
function _mtTriggerDownload(url, path) {
  try {
    const a = document.createElement('a');
    a.href = url;
    if (path) {
      const fname = String(path).split('/').pop() || 'render.mp4';
      a.download = fname;
    } else {
      a.download = '';
    }
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 100);
  } catch (e) {
    console.warn('[mtRender] auto-download failed', e);
  }
}

async function mtRefreshRenders() {
  const el = document.getElementById('mt-renders');
  if (!el) return;
  try {
    const res = await api.get(`/api/series/${S.seriesId}/timeline/renders`);
    const items = res.renders || [];
    if (!items.length) { el.innerHTML = '<div class="muted" style="font-size:12px">Пока нет сборок.</div>'; return; }
    el.innerHTML = items.map(r => {
      const d = new Date(r.created_at*1000).toLocaleString();
      return `
        <div class="mt-render-row">
          <video src="${r.url}" controls preload="metadata"></video>
          <div class="grow">
            <div class="name">${r.name}</div>
            <div class="meta">${d} · ${r.size_mb} МБ</div>
          </div>
          <a class="btn-ghost btn-sm" href="${r.url}" download>⬇ Скачать</a>
          <button class="btn-ghost btn-sm" onclick="mtDeleteRender('${r.name}')">🗑</button>
        </div>`;
    }).join('');
  } catch {}
}

async function mtDeleteRender(name) {
  if (!confirm('Удалить эту сборку?')) return;
  await api.del(`/api/series/${S.seriesId}/timeline/renders/${name}`);
  await mtRefreshRenders();
}

// ─────────────────────────────────────────────────────────────────────────────
// Crop & Position modal
// ─────────────────────────────────────────────────────────────────────────────
const CROP = {
  clipId: null,
  videoNatW: 0, videoNatH: 0,
  // crop fractions [0..1] of source frame
  fx: 0, fy: 0, fw: 1, fh: 1,
  // locked aspect ratio = source ratio (so output frame keeps same shape)
  ar: 9/16,
};

function cropOpen(clipId) {
  const clip = MT.clips.find(c => c.id === clipId);
  if (!clip || !clip.video_path) { showToast('Нет видео'); return; }
  CROP.clipId = clipId;
  // Initial fractions from saved crop or default to full frame
  if (clip.crop) {
    CROP.fx = clip.crop.x; CROP.fy = clip.crop.y;
    CROP.fw = clip.crop.w; CROP.fh = clip.crop.h;
  } else {
    CROP.fx = 0; CROP.fy = 0; CROP.fw = 1; CROP.fh = 1;
  }
  const v = document.getElementById('crop-source');
  v.src = `${assetUrl(clip.video_path)}#t=${(clip.in||0)+0.1}`;
  v.onloadedmetadata = () => {
    CROP.videoNatW = v.videoWidth;
    CROP.videoNatH = v.videoHeight;
    CROP.ar = CROP.videoNatW / CROP.videoNatH;
    cropRender();
  };
  document.getElementById('crop-info').textContent = `ep${clip.episode}·#${clip.chunk_idx}`;
  document.getElementById('crop-modal').classList.remove('hidden');
  // Attach drag listeners once
  if (!CROP._bound) {
    cropAttachDrag();
    CROP._bound = true;
  }
}

function cropClose() {
  document.getElementById('crop-modal').classList.add('hidden');
  const v = document.getElementById('crop-source');
  v.pause?.(); v.removeAttribute('src'); v.load?.();
  CROP.clipId = null;
}

function cropRender() {
  const v = document.getElementById('crop-source');
  const rect = document.getElementById('crop-rect');
  if (!v || !rect) return;
  const w = v.clientWidth, h = v.clientHeight;
  if (!w || !h) return;
  rect.style.left   = (CROP.fx * w) + 'px';
  rect.style.top    = (CROP.fy * h) + 'px';
  rect.style.width  = (CROP.fw * w) + 'px';
  rect.style.height = (CROP.fh * h) + 'px';
  // Readout
  const px = Math.round(CROP.fw * CROP.videoNatW);
  const py = Math.round(CROP.fh * CROP.videoNatH);
  document.getElementById('crop-readout').innerHTML = `
    Source: ${CROP.videoNatW}×${CROP.videoNatH}<br>
    Crop:   ${px}×${py} px<br>
    Pos:    (${(CROP.fx*100).toFixed(0)}%, ${(CROP.fy*100).toFixed(0)}%)<br>
    Size:   ${(CROP.fw*100).toFixed(0)}% × ${(CROP.fh*100).toFixed(0)}%`;
}

function cropAttachDrag() {
  const stage = document.getElementById('crop-stage');
  const rect = document.getElementById('crop-rect');
  if (!stage || !rect) return;

  // Drag the whole rect (move)
  rect.addEventListener('mousedown', (e) => {
    if (e.target.classList.contains('crop-handle')) return;
    e.preventDefault();
    const v = document.getElementById('crop-source');
    const W = v.clientWidth, H = v.clientHeight;
    const startX = e.clientX, startY = e.clientY;
    const sfx = CROP.fx, sfy = CROP.fy;
    const onMove = (ev) => {
      const dx = (ev.clientX - startX) / W;
      const dy = (ev.clientY - startY) / H;
      CROP.fx = Math.max(0, Math.min(1 - CROP.fw, sfx + dx));
      CROP.fy = Math.max(0, Math.min(1 - CROP.fh, sfy + dy));
      cropRender();
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // Resize via corner handles (preserves source aspect)
  rect.querySelectorAll('.crop-handle').forEach(handle => {
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault(); e.stopPropagation();
      const corner = handle.classList.contains('nw') ? 'nw'
                   : handle.classList.contains('ne') ? 'ne'
                   : handle.classList.contains('sw') ? 'sw' : 'se';
      const v = document.getElementById('crop-source');
      const W = v.clientWidth, H = v.clientHeight;
      const startX = e.clientX, startY = e.clientY;
      const s = { fx: CROP.fx, fy: CROP.fy, fw: CROP.fw, fh: CROP.fh };
      const ar = CROP.ar; // preserve source AR

      const onMove = (ev) => {
        const dxFrac = (ev.clientX - startX) / W;
        const dyFrac = (ev.clientY - startY) / H;
        // For corner-relative resizing, we change fw based on dx, then fh = fw / ar (in fractions)
        // ar is W/H of source; same applies to fractions because both sides scale equally.
        let newFw = s.fw, newFh = s.fh, newFx = s.fx, newFy = s.fy;
        if (corner === 'se') {
          newFw = Math.max(0.05, Math.min(1 - s.fx, s.fw + dxFrac));
          newFh = newFw * (CROP.videoNatH / CROP.videoNatW) * (W / H * (CROP.videoNatW / CROP.videoNatH));
          // Simpler: keep crop AR == source AR (so final stays uniform).
          // Both natural and on-screen use same AR, so newFh = newFw  in source-space
          // = newFw * (Wnat/Hnat) / (Wnat/Hnat) = newFw. But we operate in *fractions* of W and H,
          // and W/H aspect on stage already matches source AR.
          newFh = Math.max(0.05, Math.min(1 - s.fy, newFw));
        } else if (corner === 'sw') {
          let nW = Math.max(0.05, Math.min(s.fx + s.fw, s.fw - dxFrac));
          newFx = s.fx + (s.fw - nW);
          newFw = nW;
          newFh = Math.max(0.05, Math.min(1 - s.fy, newFw));
        } else if (corner === 'ne') {
          newFw = Math.max(0.05, Math.min(1 - s.fx, s.fw + dxFrac));
          let nH = Math.max(0.05, Math.min(s.fy + s.fh, newFw));
          newFy = s.fy + (s.fh - nH);
          newFh = nH;
        } else { // nw
          let nW = Math.max(0.05, Math.min(s.fx + s.fw, s.fw - dxFrac));
          let nH = Math.max(0.05, Math.min(s.fy + s.fh, nW));
          newFx = s.fx + (s.fw - nW);
          newFy = s.fy + (s.fh - nH);
          newFw = nW; newFh = nH;
        }
        // Final clamp
        newFw = Math.min(newFw, 1 - newFx);
        newFh = Math.min(newFh, 1 - newFy);
        CROP.fx = newFx; CROP.fy = newFy; CROP.fw = newFw; CROP.fh = newFh;
        cropRender();
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });

  // Re-render on window resize so on-screen rect stays in sync with video size
  window.addEventListener('resize', cropRender);
}

function cropReset() {
  CROP.fx = 0; CROP.fy = 0; CROP.fw = 1; CROP.fh = 1;
  cropRender();
}

async function cropClear() {
  if (!CROP.clipId) return;
  try {
    await fetch(`/api/series/${S.seriesId}/timeline/clips/${CROP.clipId}/crop`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: true }),
    }).then(r => { if (!r.ok) throw new Error('PATCH failed'); });
    showToast('Crop убран');
    cropClose();
    await mtRefreshTimeline();
    mtApplyPreviewCrop();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
}

async function cropSave() {
  if (!CROP.clipId) return;
  // No-op if it's effectively the full frame
  if (CROP.fw > 0.999 && CROP.fh > 0.999 && CROP.fx < 0.001 && CROP.fy < 0.001) {
    return cropClear();
  }
  try {
    const r = await fetch(`/api/series/${S.seriesId}/timeline/clips/${CROP.clipId}/crop`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x: CROP.fx, y: CROP.fy, w: CROP.fw, h: CROP.fh }),
    });
    if (!r.ok) throw new Error(await r.text());
    showToast('✓ Crop сохранён');
    cropClose();
    await mtRefreshTimeline();
    mtApplyPreviewCrop();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
}

// ── Init: restore view from URL hash ─────────────────────────────────────────
(function bootRoute() { _navFromHash(); })();
// (hashchange listener already registered next to navigate())
