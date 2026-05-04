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
  try { const j = JSON.parse(text); return j.error || text; } catch { return text; }
}

const api = {
  async get(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(await parseApiError(r));
    return r.json();
  },
  async post(url, body, { timeoutMs } = {}) {
    const opts = { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) };
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

// Wire up monitor controls once DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  // Random ironic slogan under the logo, picked fresh each page load
  const SLOGANS = [
    "Because deadlines don’t care about taste.",
    "For creators with vision, deadlines, and no shame.",
    "Write it. Cut it. Deny responsibility.",
    "Making “somehow it works” a business model.",
    "Make dramas faster than you can regret them.",
  ];
  const slEl = document.getElementById('nav-slogan');
  if (slEl) slEl.textContent = SLOGANS[Math.floor(Math.random() * SLOGANS.length)];

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
  openModal('modal-create-series');
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
  { value: 'Romance',          label: 'Romance',          checked: true,  desc: 'Любовная история с эмоциональным напряжением, притяжением и препятствиями между героями. Сердце — отношения.' },
  { value: 'Revenge Drama',    label: 'Revenge',          checked: true,  desc: 'Главная героиня была унижена или предана — и теперь методично разрушает жизни обидчиков. Катарсис через справедливость.' },
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
  if (!genres.length) { showToast('Выбери хотя бы один жанр'); return; }
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
  const btn = document.getElementById('autogen-sweep-btn');
  const status = document.getElementById('autogen-sweep-status');
  if (!btn || !status) return;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> запускаю…';
  try {
    await fetch(`/api/series/${S.seriesId}/auto-generate/sweep`, {method: 'POST'});
    pollAutogenStatus();
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = '🎨 Сгенерировать недостающее';
    status.textContent = 'Ошибка: ' + e.message;
  }
}

async function pollAutogenStatus() {
  const btn = document.getElementById('autogen-sweep-btn');
  const status = document.getElementById('autogen-sweep-status');
  if (!status) return;
  if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }

  const tick = async () => {
    try {
      const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
      const st = await r.json();
      if (st.running) {
        status.textContent = `генерация… ${st.done}/${st.queue}` + (st.errors.length ? ` · ошибок: ${st.errors.length}` : '');
        if (btn) btn.innerHTML = '<span class="spinner"></span> ' + st.done + '/' + st.queue;
      } else {
        if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }
        if (btn) { btn.disabled = false; btn.innerHTML = '🎨 Сгенерировать недостающее'; }
        if (st.queue === 0 && st.done === 0) {
          status.textContent = '— ничего не нужно генерить';
        } else if (st.errors && st.errors.length) {
          status.innerHTML = `<span style="color:var(--danger,#f87171)">готово ${st.done}/${st.queue} · ошибок ${st.errors.length}</span>`;
          showToast('Авто-генерация: ошибок — ' + st.errors.length + '. Подробности в консоли сервера.');
          console.warn('[autogen errors]', st.errors);
        } else {
          status.innerHTML = `<span style="color:var(--success,#4ade80)">✓ готово ${st.done}/${st.queue}</span>`;
        }
        // Refresh series state to show new images
        try {
          const fresh = await fetch(`/api/series/${S.seriesId}`).then(r => r.json());
          S.series = fresh;
          renderCharactersList();
          renderLocationsList();
        } catch {}
        setTimeout(() => { if (status) status.textContent = ''; }, 6000);
      }
    } catch (e) {
      if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }
      if (btn) { btn.disabled = false; btn.innerHTML = '🎨 Сгенерировать недостающее'; }
      status.textContent = 'Ошибка опроса: ' + e.message;
    }
  };
  await tick();
  _autogenPollTimer = setInterval(tick, 2500);
}

// Auto-resume polling if a sweep is running when the user opens the page
async function checkAutogenOnLoad() {
  try {
    const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
    const st = await r.json();
    if (st.running) pollAutogenStatus();
  } catch {}
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
    status.textContent = 'Извлекаем персонажей и локации из сюжета...';
    status.className = 'pipeline-status';
    const extracted = await api.post(`/api/series/${S.seriesId}/extract-from-story`, {});
    S.series = extracted.series;
    renderCharactersList();
    renderLocationsList();
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
        <div class="pipeline-row" style="margin-top:-6px">
          <button class="btn-idea-random" id="pl-extract-btn" onclick="plExtractFromStory()">🤖 Извлечь персонажей и локации из сюжета</button>
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
}

function applyVideoProviderMode() {
  const provider = (S.series && S.series.video_provider) || 'reteller';
  document.body.classList.toggle('seedance-mode', provider === 'seedance');
  const sel = document.getElementById('video-provider-select');
  if (sel) sel.value = provider;
}

async function setVideoProvider(provider) {
  if (!S.series) return;
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
      <div class="char-item" onclick="openCharAssets('${c.id}')">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(c.name)}">` : esc(c.name[0])}
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
      <div class="char-item" onclick="openLocAssets('${l.id}')">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(l.name)}">` : '📍'}
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
    return `
      <div class="photo-thumb-wrap">
        <img src="/assets/${S.seriesId}/${r}" alt="">
        <button class="del-btn" onclick="deleteLocRef('${loc.id}','${fname}')">✕</button>
      </div>`;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
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

// ── Character assets modal ────────────────────────────────────────────────────
let currentCharId = null;

function openCharAssets(charId) {
  currentCharId = charId;
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  document.getElementById('char-assets-title').textContent = `Фото: ${c.name}`;
  renderCharAssetsGrid(c);
  renderOutfitsList(c);

  const inp = document.getElementById('char-asset-file-input');
  inp.onchange = () => uploadCharacterRefs(charId, inp.files);
  openModal('modal-char-assets');
}

function renderCharAssetsGrid(char) {
  const grid = document.getElementById('char-assets-grid');
  const refs = char.ref_images || [];
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `/assets/${S.seriesId}/${r}`;
    return `
      <div class="photo-thumb-wrap" onclick="openCharLightbox('${char.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteCharRef('${char.id}','${fname}')">✕</button>
      </div>
    `;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
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
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.getElementById('lightbox-overlay')) closeLightbox();
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
    const photoUrl = hasPhoto ? `/assets/${S.seriesId}/${o.photo}` : '';
    const photoHtml = hasPhoto
      ? `<img src="${photoUrl}" alt="">`
      : `<div style="font-size:2.2rem">👗</div>`;
    const photoClick = hasPhoto ? `onclick="openLightbox('${photoUrl}')"` : '';
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
function openStyleEditor() {
  setVal('style-type', S.series.style.type);
  setVal('style-custom-desc', S.series.style.custom_description);
  openModal('modal-style');
}

async function saveStyle() {
  await api.put(`/api/series/${S.seriesId}`, {
    style: { type: val('style-type'), custom_description: val('style-custom-desc') }
  });
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-style');
  renderStyleSection();
}

// ── Episodes ──────────────────────────────────────────────────────────────────
function openCreateEpisode() {
  // Find first number that either doesn't exist OR exists without a synopsis
  const existing = new Map(S.episodes.map(e => [e.number, e]));
  const maxNum = existing.size > 0 ? Math.max(...existing.keys()) : 0;
  let defaultNum = maxNum + 1;
  for (let n = 1; n <= maxNum + 1; n++) {
    if (!existing.has(n)) { defaultNum = n; break; }
    const ep = existing.get(n);
    if (!ep.synopsis || !ep.synopsis.trim()) { defaultNum = n; break; }
  }
  document.getElementById('new-ep-number').value = defaultNum;
  document.getElementById('new-ep-synopsis').value = '';
  document.getElementById('new-ep-synopsis-status').textContent = '';
  const titleEl = document.getElementById('new-ep-modal-title');
  if (titleEl) titleEl.textContent = isBatchMode(S.series) ? `Новый чанк (${chunkLabel(S.series, defaultNum)})` : 'Новый эпизод';
  openModal('modal-create-episode');
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

  try {
    const epNum = parseInt(document.getElementById('new-ep-number').value) || null;
    const ep = await api.post(`/api/series/${S.seriesId}/episodes`, {
      synopsis: val('new-ep-synopsis'),
      number: epNum,
    });
    closeModal('modal-create-episode');
    S.episodes.push(ep);
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

  document.getElementById('ep-number-badge').textContent = chunkLabel(S.series, S.episodeNum, { short: true });
  setVal('ep-title-input', S.episode.title);
  setVal('ep-synopsis', S.episode.synopsis);
  setVal('ep-script', S.episode.script);
  setVal('ep-notes', S.episode.notes);
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

  // Auto-extract characters/locations from story if series has none yet
  const hasStory = !!(S.series.arc || S.series.synopsis || Object.keys(S.series.milestone_synopses || {}).length);
  const hasChars = (S.series.characters || []).length > 0;
  const hasLocs  = (S.series.locations  || []).length > 0;
  if (hasStory && (!hasChars || !hasLocs)) {
    autoExtractFromStory();
  }

  renderEpCharacters();
  renderEpLocations();
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
const SCENE_HEADING_RE = /^\s*(INT\.|EXT\.|INT\.?\s*\/\s*EXT\.?|I\/E\.|ИНТ\.|ИНТА\.|ЭКСТ\.|ЭКС\.|НАТ\.|НАТУРА\.|ВНУТР\.|ИНТЕРЬЕР|ВНЕ\.|СНАРУЖИ)\s+/i;

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
function _lineDuration(line) {
  const t = (line || '').trim();
  if (!t) return 0;
  if (SCENE_HEADING_RE.test(t)) return 0;
  if (/^[-—=]{3,}\s*$/.test(t)) return 0;
  if (/^\[REVERSAL\]\s*$/i.test(t)) return 0;
  if (/^[\s—-]*(FADE|CUT|DISSOLVE|SMASH|MATCH)\s+(IN|OUT|TO|BACK)\b/i.test(t)) return 0;

  // Action line: bracketed prose `[Волк входит и...]`
  if (/^\[/.test(t) && /\]\s*$/.test(t)) {
    const words = t.replace(/[\[\]]/g, '').split(/\s+/).filter(Boolean).length;
    if (words === 0) return 0;
    return 1 + words / 1.8;          // 1s overhead + ~1.8 wps
  }

  // Dialogue line: "CHAR_NAME: (parens) actual text"
  const m = t.match(/^([A-ZА-ЯЁ_][A-ZА-ЯЁ_0-9 ()\-']{0,40})\s*[:：]\s*(.*)$/);
  if (m && m[1].toUpperCase() === m[1]) {
    let rest = m[2] || '';
    // Drop parenthetical tone notes — they're not spoken
    rest = rest.replace(/\([^)]*\)/g, ' ').trim();
    if (!rest) return 0.5;
    const words = rest.split(/\s+/).filter(Boolean).length;
    return 0.4 + words / 2.4;        // tiny pre-pause + ~2.4 wps speech
  }

  // Standalone parenthetical "(angry)" — small
  if (/^\(.+\)$/.test(t)) return 0.4;

  // Plain prose action (no brackets)
  const words = t.split(/\s+/).filter(Boolean).length;
  return Math.max(0.4, words / 1.8);
}

// Find a chunk's [start, end] byte-offset in the script via long-line anchors.
// Mirrors the server-side logic in app.py compose endpoint.
function _findChunkRange(scriptText, chunkText) {
  if (!chunkText || !scriptText) return [-1, -1];
  const cands = [];
  for (const ln of chunkText.split('\n')) {
    const s = ln.trim();
    if (s.length < 25) continue;
    if (SCENE_HEADING_RE.test(s)) continue;
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
    if (SCENE_HEADING_RE.test(s)) continue;
    const idx = scriptText.indexOf(s, start);
    if (idx >= 0) { end = idx + s.length; break; }
  }
  return [start, end];
}

function _parseScriptScenes(scriptText) {
  const rawLines = (scriptText || '').split('\n');
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
    // Markdown title (#)
    if (/^#\s/.test(t)) continue;
    // Horizontal separators
    if (/^[-—=]{3,}\s*$/.test(t)) continue;

    // Scene heading: open a new scene
    if (SCENE_HEADING_RE.test(t)) {
      cur = { id: scenes.length, heading: t, lines: [], totalSec: 0 };
      scenes.push(cur);
      continue;
    }

    // Lines BEFORE first scene heading are dropped (title block, etc.)
    if (!cur) continue;

    const dur = _lineDuration(rawLine);
    // Empty / structural lines aren't visual time AND not visible body either
    if (!t) continue;
    cur.lines.push({
      text: rawLine, duration: dur, segIdx: 0,
      offset: lineStart, offsetEnd: lineEnd,
    });
    cur.totalSec += dur;
  }

  // Assign 15-second segment indices based on REAL on-screen time only.
  const TARGET = 14.0;     // aim for ≤15s per chunk
  const SOFT_MAX = 15.5;   // hard cap before forcing a break
  for (const sc of scenes) {
    let acc = 0, seg = 0;
    for (const l of sc.lines) {
      if (acc > 0 && acc + l.duration > SOFT_MAX) {
        seg += 1;
        acc = 0;
      }
      l.segIdx = seg;
      acc += l.duration;
      // If we just exactly hit/exceeded TARGET, allow the next line to start a new segment
      if (acc >= TARGET && acc <= SOFT_MAX) {
        // Defer: next iteration's check will decide based on next line's duration
      }
    }
    sc.segCount = sc.lines.length ? (sc.lines[sc.lines.length - 1].segIdx + 1) : 0;
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
    return '<div class="muted" style="padding:16px">Сценарий пустой или не содержит ни одного scene heading (INT./EXT./ИНТ./ЭКСТ.).</div>';
  }
  const showCov = !!SCENE_VIEW_STATE.showCoverage && coverage.length;
  let html = '';
  if (coverage.length) {
    const stats = { completed: 0, pending: 0, failed: 0 };
    for (const r of coverage) {
      if (r.status === 'completed') stats.completed++;
      else if (r.status === 'failed') stats.failed++;
      else stats.pending++;
    }
    html += `<div class="ep-scene-toolbar">
      <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="ep-cov-toggle" ${showCov ? 'checked' : ''} onchange="toggleCoverage(this.checked)">
        Подсветить что уже сгенерено в Seedance
      </label>
      <span class="muted" style="font-size:0.78rem">
        · ${stats.completed} ✓  · ${stats.pending} ⏳  · ${stats.failed} ✗
      </span>
    </div>`;
  }
  scenes.forEach((sc, sIdx) => {
    const bg = SCENE_COLORS[sIdx % SCENE_COLORS.length];
    const bdr = SCENE_BORDERS[sIdx % SCENE_BORDERS.length];
    const heading = sc.heading || `(без scene heading) — сцена #${sIdx + 1}`;
    html += `<div class="ep-scene" style="background:${bg};border-left:4px solid ${bdr}">`;
    html += `<div class="ep-scene-header">
        <span class="ep-scene-tag">Сцена ${sIdx + 1}</span>
        <span class="ep-scene-loc">${esc(heading.slice(0, 120))}</span>
        <span class="ep-scene-meta">~${sc.totalSec.toFixed(0)}с · ${sc.segCount} сегмент(ов) ×15с</span>
      </div>`;
    let lastSeg = -1;
    let segAcc = 0;
    sc.lines.forEach((l, lIdx) => {
      if (l.segIdx !== lastSeg) {
        if (lastSeg !== -1) html += `</div></div>`; // close prev seg-body + ep-seg
        lastSeg = l.segIdx;
        segAcc = 0;
        html += `<div class="ep-seg" data-seg="${l.segIdx + 1}">
          <div class="ep-seg-bracket" title="Seedance-сегмент ${l.segIdx + 1}">${l.segIdx + 1}</div>
          <div class="ep-seg-body">`;
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
      html += `<div class="${_cls}">${_tag}${esc(l.text || ' ')}</div>`;
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

const SCENE_VIEW_STATE = { showCoverage: true };

function _renderSceneViewBody() {
  const ta = document.getElementById('ep-script');
  const view = document.getElementById('ep-script-scenes');
  if (!ta || !view) return;
  const scriptText = ta.value || '';
  const scenes = _parseScriptScenes(scriptText);
  const chunks = (S.episode && S.episode.seedance_chunks) || [];
  const coverage = _buildSeedanceCoverage(scriptText, chunks);
  view.innerHTML = _renderScenesHTML(scenes, coverage);
}

function toggleCoverage(on) {
  SCENE_VIEW_STATE.showCoverage = !!on;
  try { localStorage.setItem('sceneCov', SCENE_VIEW_STATE.showCoverage ? '1' : '0'); } catch {}
  _renderSceneViewBody();
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
    updateScriptCounter();
    updateGenScriptBtn();
    status.textContent = '✓ Сценарий готов — проверяем логику...';
    status.style.color = 'var(--success)';
    // Kick off polling so user sees autogen progress for any new chars/outfits introduced
    setTimeout(() => {
      fetch(`/api/series/${S.seriesId}/auto-generate/status`)
        .then(r => r.json())
        .then(st => { if (st.running || st.queue > 0) pollAutogenStatus(); })
        .catch(() => {});
    }, 800);
    // GATE: run logic check (with previous-episode context) BEFORE reteller prompt.
    // If clean → auto-continue to reteller. If issues → show inline panel and wait.
    runLogicCheckThenContinue();
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
    btn.disabled = false;
    updateGenScriptBtn();
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
    const imgUrl = hasRef ? `/assets/${S.seriesId}/${l.ref_images[0]}` : null;
    const inEp = used.includes(l.id);
    const dragAttrs = (seedanceMode && hasRef)
      ? `draggable="true" ondragstart="sdLocDragStart(event,'${l.id}')"` : '';
    return `
      <div class="ep-loc-row ${inEp ? 'in-episode' : ''}" id="ep-loc-${l.id}" ${dragAttrs}
           ondragover="event.preventDefault();this.classList.add('drop-hover')"
           ondragleave="this.classList.remove('drop-hover')"
           ondrop="event.preventDefault();this.classList.remove('drop-hover');dropLocPhoto(event,'${l.id}')">
        <div class="ep-loc-thumb">
          ${imgUrl ? `<img src="${imgUrl}" alt="">` : '📍'}
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
    if (res.added_characters.length || res.added_locations.length) {
      showToast(`Найдено: ${res.added_characters.length} перс., ${res.added_locations.length} лок.`);
    }
  } catch(e) {
    renderEpCharacters();
    renderEpLocations();
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
    photoUrl = `/assets/${S.seriesId}/${primaryOutfit.photo}`;
  } else if (c.ref_images?.length) {
    photoUrl = `/assets/${S.seriesId}/${c.ref_images[0]}`;
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
      <div class="ep-char-photo" ${photoUrl ? `onclick="event.stopPropagation();openCharLightbox('${c.id}','${photoUrl}')" style="cursor:zoom-in"` : ''}
           ondragover="event.preventDefault();this.classList.add('drop-hover')"
           ondragleave="this.classList.remove('drop-hover')"
           ondrop="event.preventDefault();this.classList.remove('drop-hover');dropCharPhoto(event,'${c.id}')"
           title="${photoUrl ? 'Открыть фото крупно (можно перегенерировать)' : ''}">
        ${photoUrl
          ? `<img src="${photoUrl}" alt="${esc(c.name)}" ${needsOutfitGen ? 'style="opacity:0.45;filter:grayscale(0.6)"' : ''}>`
          : `<div class="no-photo">${primaryOutfit ? '👗' : '👤'}</div>`}
        ${needsOutfitGen ? `
          <div class="outfit-gen-overlay" id="ep-outfit-overlay-${c.id}" onclick="event.stopPropagation();generateEpOutfit('${c.id}','${primaryOutfit.id}')"
               title="Переодеть в образ «${esc(primaryOutfit.label)}» (i2i из базового фото)"
               style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6);color:#fff;cursor:pointer;font-size:1.2rem;font-weight:700;">
            ⚡
          </div>` : ''}
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
                const stateIcon = o.photo ? '✓' : (o.is_base ? '★' : '⚡');
                return `<button type="button"
                  class="ep-outfit-chip ${on ? 'on' : ''} ${isPrimary ? 'primary' : ''}"
                  title="${on ? 'Снять выбор' : 'Добавить образ для этой серии'} — ${esc(o.label)}${o.photo ? ' (фото готово)' : (o.is_base ? ' (базовое фото)' : ' (фото не готово — кликни ⚡)')}"
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
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    S.series = data.series;
    renderEpLocations();
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
    if (res.ready) {
      statusEl.textContent = '';
      S.series = await api.get(`/api/series/${S.seriesId}`);
      renderEpLocations();
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
    notes: val('ep-notes'),
    reteller_prompt: val('ep-reteller-prompt'),
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
async function openSettings() {
  const cfg = await api.get('/api/config');
  setVal('settings-rtl-key', cfg.reteller_key || '');
  setVal('settings-anthropic-key', cfg.anthropic_key || '');
  openModal('modal-settings');
}

async function saveSettings() {
  await api.post('/api/config', {
    reteller_key: val('settings-rtl-key'),
    anthropic_key: val('settings-anthropic-key'),
  });
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

// ════════════════════════════════════════════════════════════════════════════
// SEEDANCE — video generation panel
// ════════════════════════════════════════════════════════════════════════════
const SD = { refs: [], pollTimer: null };

function sdLocDragStart(ev, locId) {
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  const photoUrl = l.ref_images?.[0] ? `/assets/${S.seriesId}/${l.ref_images[0]}` : '';
  ev.dataTransfer.setData('application/json', JSON.stringify({
    kind: 'loc', id: locId, name: l.name, photoUrl
  }));
}

async function sdHandleDrop(ev) {
  ev.preventDefault();
  if (SD.refs.length >= 9) { showToast('Максимум 9 референсов'); return; }

  // 1. File from desktop / external app
  const files = Array.from(ev.dataTransfer.files || []).filter(f => f.type.startsWith('image/'));
  if (files.length) {
    for (const file of files) {
      if (SD.refs.length >= 9) break;
      await sdUploadCustomFile(file);
    }
    return;
  }

  // 2. Image URL dragged from another browser tab
  const uri = ev.dataTransfer.getData('text/uri-list') || ev.dataTransfer.getData('text/plain');
  if (uri && /^https?:\/\//.test(uri.trim()) && /\.(png|jpe?g|webp|gif)(\?|$)/i.test(uri.trim())) {
    await sdUploadCustomUrl(uri.trim());
    return;
  }

  // 3. Internal payload (char/loc card)
  let payload;
  try { payload = JSON.parse(ev.dataTransfer.getData('application/json') || '{}'); }
  catch { return; }
  if (!payload.kind || !payload.id) return;
  if (SD.refs.some(r => r.kind === payload.kind && r.id === payload.id && r.outfit === payload.outfit)) return;
  SD.refs.push(payload);
  sdRenderRefs();
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
      name: res.name || file.name, photoUrl: res.url, url: res.url
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
      name: res.name || 'custom', photoUrl: res.url, url: res.url
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
  slot.innerHTML = SD.refs.map((r, i) => {
    const subtitle = r.outfit ? `<div class="ref-sub">${esc(r.outfit)}</div>` : '';
    const kindIcon = r.kind === 'loc' ? '🏛'
                  : r.kind === 'lastframe' ? '🎞'
                  : r.kind === 'char' ? '👤' : '🖼';
    return `
    <div class="sd-ref-chip" data-i="${i}"
         ondragover="sdSlotDragOver(event)"
         ondragleave="sdSlotDragLeave(event)"
         ondrop="sdSlotDrop(event,${i})"
         title="@Image${i+1}: ${esc(r.name)}${r.outfit ? ' / '+esc(r.outfit) : ''} — перетащи сюда другую карточку чтобы заменить">
      <div class="ref-top">@Image${i+1}</div>
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
          if (o?.photo) photoUrl = `/assets/${S.seriesId}/${o.photo}`;
        }
        if (!photoUrl && c.ref_images?.[0]) photoUrl = `/assets/${S.seriesId}/${c.ref_images[0]}`;
      }
    } else if (payload.kind === 'loc') {
      const l = (S.series.locations || []).find(x => x.id === payload.id);
      if (l) { name = l.name; if (l.ref_images?.[0]) photoUrl = `/assets/${S.seriesId}/${l.ref_images[0]}`; }
    }
  }
  SD.refs[idx] = { ...payload, name, photoUrl };
  sdRenderRefs();
}

function sdRemoveRef(i) {
  SD.refs.splice(i, 1);
  sdRenderRefs();
}

async function sdCompose() {
  const chunk = document.getElementById('sd-chunk-text').value.trim();
  if (!chunk) { showToast('Вставь кусок сценария'); return; }
  const st = document.getElementById('sd-compose-status');
  st.textContent = '⚙ компоную через Claude...';
  const useLastframe = !!document.getElementById('sd-use-lastframe')?.checked;
  const useStyle     = !!document.getElementById('sd-use-style')?.checked;
  const styleVal     = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly     = !!document.getElementById('sd-base-only')?.checked;
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/compose`,
      {
        chunk_text: chunk,
        use_prev_lastframe: useLastframe,
        style: useStyle ? styleVal : '',
        base_outfits_only: baseOnly,
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
            if (o?.photo) photoUrl = `/assets/${S.seriesId}/${o.photo}`;
          }
          if (!photoUrl && c.ref_images?.[0]) photoUrl = `/assets/${S.seriesId}/${c.ref_images[0]}`;
        }
      } else if (r.kind === 'loc') {
        const l = (S.series.locations || []).find(x => x.id === r.id);
        if (l) {
          name = l.name;
          if (l.ref_images?.[0]) photoUrl = `/assets/${S.seriesId}/${l.ref_images[0]}`;
        }
      } else if (r.kind === 'lastframe') {
        // Server-attached continuity frame from previous chunk
        name = r.name || 'last frame';
        photoUrl = r.url || '';
      }
      return { ...r, name, photoUrl };
    });
    sdRenderRefs();
    let msg = res.scene_continuity ? '✓ продолжение прошлой сцены' : '✓ скомпоновано';
    if (res.lastframe_attached) msg += ' · 🎞 last frame прицеплен';
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

function sdSavePrefs() {
  try {
    localStorage.setItem('sd_prefs', JSON.stringify({
      duration: document.getElementById('sd-duration').value,
      resolution: document.getElementById('sd-resolution').value,
      moderation_bypass: document.getElementById('sd-mod-bypass').value,
      use_prev_lastframe: !!document.getElementById('sd-use-lastframe')?.checked,
      use_style: !!document.getElementById('sd-use-style')?.checked,
      style: (document.getElementById('sd-style')?.value || '').trim(),
      base_outfits_only: !!document.getElementById('sd-base-only')?.checked,
    }));
  } catch (e) {}
}

function sdLoadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('sd_prefs') || '{}');
    if (p.duration) document.getElementById('sd-duration').value = p.duration;
    if (p.resolution) document.getElementById('sd-resolution').value = p.resolution;
    if (p.moderation_bypass) document.getElementById('sd-mod-bypass').value = p.moderation_bypass;
    const cb = document.getElementById('sd-use-lastframe');
    if (cb && typeof p.use_prev_lastframe === 'boolean') cb.checked = p.use_prev_lastframe;
    const sc = document.getElementById('sd-use-style');
    const si = document.getElementById('sd-style');
    if (sc && typeof p.use_style === 'boolean') sc.checked = p.use_style;
    if (si && typeof p.style === 'string') si.value = p.style;
    if (si && sc) si.classList.toggle('hidden', !sc.checked);
    const bo = document.getElementById('sd-base-only');
    if (bo && typeof p.base_outfits_only === 'boolean') bo.checked = p.base_outfits_only;
  } catch (e) {}
}

async function sdGenerate() {
  console.log('[sdGenerate] click');
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
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/start`,
      {
        prompt, chunk_text: chunk, duration, resolution, moderation_bypass,
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
    sdRenderList(res.chunks || []);
  } catch (e) { /* ignore */ }
}

function _sdCardHTML(c) {
  const stCls = `sd-status-${c.status || 'pending'}`;
  const videoUrl = c.video_path ? `/assets/${S.seriesId}/${c.video_path}` : '';
  const cost = c.cost != null ? `· $${Number(c.cost).toFixed(2)}` : '';
  let placeholderText;
  if (c.status === 'failed') placeholderText = '✗ failed';
  else if (c.status === 'submitting') placeholderText = '📤 отправляю...';
  else if (c.progress != null) placeholderText = c.progress + '%';
  else placeholderText = '⏳ генерируется';
  return `
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
        <button class="btn-ghost btn-sm" onclick="sdReuse(${c.idx})">↻ Reuse</button>
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
      // Place node at correct DOM slot
      if (prevNode) {
        if (node.previousSibling !== prevNode) prevNode.after(node);
      } else {
        if (el.firstChild !== node) el.prepend(node);
      }
      prevNode = node;
    }
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
  await sdRefreshList();
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
          if (o?.photo) photoUrl = `/assets/${S.seriesId}/${o.photo}`;
        }
        if (!photoUrl && ch.ref_images?.[0]) photoUrl = `/assets/${S.seriesId}/${ch.ref_images[0]}`;
      }
    } else if (r.kind === 'loc') {
      const l = (S.series.locations || []).find(x => x.id === r.id);
      if (l) { name = l.name; if (l.ref_images?.[0]) photoUrl = `/assets/${S.seriesId}/${l.ref_images[0]}`; }
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
    sdRenderList(res.chunks || []);
    return res.chunks || [];
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
    const url = c.video_path ? `/assets/${S.seriesId}/${c.video_path}` : '';
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
  const url = `/assets/${S.seriesId}/${c.video_path}`;
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
      const url = `/assets/${S.seriesId}/${c.video_path}`;
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
  } catch (e) {
    showToast('✗ ' + (e.message || e), 7000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old || '▶ Собрать MP4'; }
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
  v.src = `/assets/${S.seriesId}/${clip.video_path}#t=${(clip.in||0)+0.1}`;
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
