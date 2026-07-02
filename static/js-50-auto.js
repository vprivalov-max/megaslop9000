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

// ── Multi-episode parallel auto-mode registry ──────────────────────────────
// `AUTO` is the primary (single-episode page button) run. `AUTO_RUNS` adds
// extra concurrent runs spawned by range-gen with concurrency > 1. Each entry
// has the same shape as AUTO; the floating widget aggregates across all.
// Key = `${sid}:${epNum}`.
const AUTO_RUNS = new Map();
function _autoRegisterRun(R) {
  if (!R || !R._epSid || R._epNumber == null) return;
  AUTO_RUNS.set(`${R._epSid}:${R._epNumber}`, R);
  _autoUpdateFloatingWidget();
}
function _autoUnregisterRun(R) {
  if (!R) return;
  AUTO_RUNS.delete(`${R._epSid}:${R._epNumber}`);
  // Run completed / stopped → drop its persisted snapshot so the next page
  // load doesn't pop the «▶ Продолжить» widget for a long-finished episode.
  try { _autoClearPersistedRun(R._epSid, R._epNumber); } catch {}
  _autoUpdateFloatingWidget();
}
function _autoAllRuns() {
  // Primary AUTO + any extras. Filtered to active=true.
  // Includes "suspended" placeholders restored from localStorage after a page
  // refresh — they have active=true but a flag indicating they need a manual
  // resume click before the JS loop spins up again.
  const out = [];
  if (AUTO.active) out.push(AUTO);
  for (const r of AUTO_RUNS.values()) {
    if (r === AUTO) continue;
    if (r.active) out.push(r);
  }
  return out;
}

// ── Manual parallel auto-mode — up to 3 episodes at once ───────────────────
// The user can launch Auto-mode on several episodes of the same series
// simultaneously (e.g. open ep.1 → ▶, open ep.2 → ▶). The PRIMARY run keeps
// using the global AUTO singleton (full turbo / skip-filter / confirm path);
// each ADDITIONAL run on another episode spins up as a self-contained
// standalone run (`_runEpisodeAutoStandalone`) registered in AUTO_RUNS — the
// same engine range-gen uses, so the backend is already proven safe with up to
// 3 concurrent per-episode pipelines of one series. The ceiling matches the
// range-gen concurrency cap.
const MAX_CONCURRENT_AUTO = 3;

// The live (non-suspended) run targeting a specific episode, or null. Makes the
// toolbar button per-episode aware (start vs stop THIS episode) and refuses
// double-launching the same episode.
function _autoRunForEpisode(sid, num) {
  if (!sid || num == null) return null;
  if (AUTO.active && AUTO._epSid === sid && AUTO._epNumber === num) return AUTO;
  const r = AUTO_RUNS.get(`${sid}:${num}`);
  if (r && r.active && !r.suspended && r !== AUTO) return r;
  return null;
}

// Count of live (non-suspended) auto-mode runs across the whole app — primary
// AUTO + every standalone run (manual-parallel and range-gen). Gates the cap.
function _autoActiveRunCount() {
  return _autoAllRuns().filter(r => !r.suspended).length;
}

// Stop just ONE episode's run (per-episode toolbar / widget button). Other
// parallel runs keep going. Returns true if a run was found and signalled.
function _stopAutoRunForEpisode(sid, num) {
  const r = _autoRunForEpisode(sid, num);
  if (!r) return false;
  r.cancelRequested = true;
  showToast(`⏸ Auto-mode эп.${num} остановится после текущего шага…`, 3000);
  _autoUpdateStatusUI();
  return true;
}

// Refresh the in-episode toolbar button + inline status so they reflect the
// CURRENTLY-OPEN episode's run (not the global singleton). Called from the
// floating-widget update so it stays in sync for both primary and standalone
// runs, including after navigation between episodes.
function _autoRefreshToolbarBtn() {
  const sid = (typeof S !== 'undefined' && S) ? S.seriesId : null;
  const num = (typeof S !== 'undefined' && S && S.episode) ? S.episode.number : null;
  const myRun = _autoRunForEpisode(sid, num);
  const btn = document.getElementById('auto-mode-btn');
  if (btn) {
    btn.innerHTML = myRun
      ? '⏸ Стоп — остановить генерацию серии'
      : '🎬 Сгенерировать всю серию в Seedance';
    btn.className = myRun ? 'btn-danger' : 'btn-accent';
  }
  const el = document.getElementById('auto-status');
  if (el) {
    if (!myRun) {
      el.textContent = (!AUTO.active && (AUTO.completedCount || AUTO.cursor) > 0 && AUTO.total > 0)
        ? `Завершено: ${AUTO.completedCount || AUTO.cursor}/${AUTO.total}`
        : '';
    } else {
      const done = myRun.completedCount || 0;
      const mode = myRun.parallel
        ? 'паралл.'
        : ((myRun.activeChains || 0) > 1 ? `сцены × ${myRun.activeChains}` : 'последов.');
      el.textContent = `Auto-mode (${mode}) · ${done}/${myRun.total || 0} · ${myRun.lastStatus || '...'}`;
    }
  }
}

// ── Persistence — survive page refresh ─────────────────────────────────────
// The JS loop dies on refresh (any open Seedance jobs still finish on the
// backend), so we serialize active-run state to localStorage. On next page
// load `_autoRestoreSuspendedRuns` re-injects them as suspended placeholders
// in AUTO_RUNS, the floating widget reappears with a «▶ Продолжить» button,
// and the user can resume from where it stopped — already-completed chunks
// (matched by script_order) are skipped to avoid duplicating work.
const _AUTO_LS_KEY = 'auto_runs_state_v1';
const _AUTO_LS_TTL_MS = 24 * 60 * 60 * 1000;   // 24h — stale states age out

function _autoSerializeRun(r) {
  return {
    epSid: r._epSid,
    epNumber: r._epNumber,
    total: r.total || 0,
    completedCount: r.completedCount || 0,
    parallel: !!r.parallel,
    errorMode: r.errorMode || 'heal',
    lastStatus: r.lastStatus || '',
    timestamp: Date.now(),
  };
}

function _autoPersistRuns() {
  try {
    const runs = _autoAllRuns().filter(r => r._epSid && r._epNumber != null && !r.suspended);
    if (!runs.length) {
      localStorage.removeItem(_AUTO_LS_KEY);
      return;
    }
    localStorage.setItem(_AUTO_LS_KEY, JSON.stringify(runs.map(_autoSerializeRun)));
  } catch {}
}

function _autoClearPersistedRun(epSid, epNumber) {
  try {
    const raw = localStorage.getItem(_AUTO_LS_KEY);
    if (!raw) return;
    const arr = JSON.parse(raw).filter(s => !(s.epSid === epSid && s.epNumber === epNumber));
    if (arr.length) localStorage.setItem(_AUTO_LS_KEY, JSON.stringify(arr));
    else localStorage.removeItem(_AUTO_LS_KEY);
  } catch {}
}

// Restore suspended runs from localStorage, SCOPED to the currently-open
// series. The widget must never show «▶ Продолжить» for a series the user
// isn't currently inside — clicking it would yank them away to a different
// project. So:
//   • If no series is open (e.g. /projects page) → no placeholders, widget
//     stays clean. Snapshots for OTHER series remain in localStorage and
//     will pop back up when the user navigates into their series.
//   • If user is in series X → only X's runs become suspended placeholders;
//     any previously-injected placeholders for other series are cleared.
// Called on DOMContentLoaded AND on every navigate() into a series view.
function _autoRestoreSuspendedRuns() {
  // 1. Drop existing suspended placeholders — about to recompute from scratch.
  for (const [k, r] of [...AUTO_RUNS.entries()]) {
    if (r && r.suspended) AUTO_RUNS.delete(k);
  }
  const currentSid = (typeof S !== 'undefined' && S) ? S.seriesId : null;
  let snapshots;
  try {
    const raw = localStorage.getItem(_AUTO_LS_KEY);
    if (!raw) {
      if (typeof _autoUpdateFloatingWidget === 'function') _autoUpdateFloatingWidget();
      return;
    }
    snapshots = JSON.parse(raw);
    if (!Array.isArray(snapshots) || !snapshots.length) return;
  } catch { return; }
  const fresh = snapshots.filter(s => (Date.now() - (s.timestamp || 0)) < _AUTO_LS_TTL_MS);
  // Trim stale entries from storage so it doesn't grow forever.
  if (fresh.length !== snapshots.length) {
    if (fresh.length) localStorage.setItem(_AUTO_LS_KEY, JSON.stringify(fresh));
    else localStorage.removeItem(_AUTO_LS_KEY);
  }
  // Without an open series, nothing to render — but keep snapshots alive so
  // navigating into their series later picks them back up.
  if (!currentSid) {
    if (typeof _autoUpdateFloatingWidget === 'function') _autoUpdateFloatingWidget();
    return;
  }
  for (const snap of fresh) {
    if (!snap.epSid || snap.epNumber == null) continue;
    if (snap.epSid !== currentSid) continue;     // ← series-scope filter
    const key = `${snap.epSid}:${snap.epNumber}`;
    if (AUTO_RUNS.has(key)) continue;            // live run already exists
    AUTO_RUNS.set(key, {
      active: true,
      suspended: true,
      _epSid: snap.epSid,
      _epNumber: snap.epNumber,
      total: snap.total,
      completedCount: snap.completedCount,
      parallel: snap.parallel,
      errorMode: snap.errorMode,
      lastStatus: '⏸ Прервано обновлением страницы — нажми «▶ Продолжить»',
    });
  }
  if (typeof _autoUpdateFloatingWidget === 'function') _autoUpdateFloatingWidget();
}

// Resume a suspended run. Navigates to the target episode if not already
// there, then triggers startAutoMode — its built-in pre-flight will
// re-collect segments from the current script and the new completed-chunks
// filter (added in startAutoMode) skips anything already finished on the
// backend so we don't redo successful work.
async function _autoResumeRun(epSid, epNumber) {
  if (!epSid || epNumber == null) return;
  const key = `${epSid}:${epNumber}`;
  // Mark as cleared so the user immediately sees we picked up the request.
  AUTO_RUNS.delete(key);
  _autoClearPersistedRun(epSid, epNumber);
  _autoUpdateFloatingWidget();
  if (S.seriesId !== epSid || !S.episode || S.episode.number !== epNumber) {
    try { navigate('episode', { seriesId: epSid, episodeNum: epNumber }); } catch {}
    // Give the episode-open render cycle a moment to land before kicking off.
    await new Promise(r => setTimeout(r, 800));
  }
  try { startAutoMode(); } catch (e) { console.warn('[auto resume] start failed', e); }
}
window._autoResumeRun = _autoResumeRun;   // exposed for inline onclick

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

// Turbo-engine selector. Two implementations of Turbo live side-by-side:
//   • 'parallel-sequential' (default, experimental) — per-chunk Claude
//     compose like sequential mode + parallel /start, no last-frame /
//     cut-frames. Bypasses scene blocking, batch JSON, auto-revise entirely.
//   • 'shadow' — colleague-ported pipeline: scene blocking → batch-compose
//     → auto-revise → parallel /start with prebuilt prompts.
// Stored in localStorage so rollback is one click in Settings. Sequential
// mode is unaffected.
function _turboEngine() {
  const v = localStorage.getItem('turbo_engine');
  return v === 'shadow' ? 'shadow' : 'parallel-sequential';
}
function _setTurboEngine(v) {
  if (v !== 'shadow' && v !== 'parallel-sequential') return;
  localStorage.setItem('turbo_engine', v);
}
function _applyAutoModeUI() {
  const kind = _autoGetModeKind();
  const sec = document.getElementById('scene-blocking-section');
  if (sec) sec.style.display = (kind === 'turbo') ? '' : 'none';
}
// Run on episode-view render so visibility matches saved choice
document.addEventListener('DOMContentLoaded', _applyAutoModeUI);
// Restore any auto-mode runs that were active when the page was refreshed —
// they re-appear as suspended placeholders in the floating widget with a
// «▶ Продолжить» button. Wrapped in setTimeout so AUTO_RUNS / widget code
// is fully parsed (defensive — function declarations are hoisted but the
// floating widget DOM root is created lazily, so we wait until ticking).
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(_autoRestoreSuspendedRuns, 0);
});

function _autoCollectSegments(opts = {}) {
  const ta = document.getElementById('ep-script');
  if (!ta) return [];
  const scenes = _parseScriptScenes(ta.value || '', _segmentOverrides());
  // NOTE: the legacy «🏛️ заставка локации (2с)» feature was removed once
  // per-location facades started auto-generating as standalone clips
  // shown during assembly. The in-chunk 2-second wide-shot pre-roll used
  // to eat dialogue budget for nothing.
  const out = [];
  scenes.forEach((sc, sIdx) => {
    for (let g = 0; g < sc.segCount; g++) {
      const lines = sc.lines.filter(l => l.segIdx === g);
      if (!lines.length) continue;
      const head = sc.heading ? sc.heading + '\n\n' : '';
      const text = head + lines.map(l => l.text).join('\n');
      const hasCloseUp = lines.some(l => _isLineCloseUp(l.text));
      const anchor = _lineAnchor(lines[0].text);
      // VO overlaps action visuals (you HEAR narration WHILE seeing the camera
      // move) — so we don't ADD action+VO durations, we take the max. Regular
      // sequential dialogue still adds on top (lipsync = must play in order).
      // Same logic powers `_estimateChunkDurationSec` for retries/reuses.
      const segmentText = lines.map(l => l.text).join('\n');
      const durationSec = _estimateChunkDurationSec(segmentText);
      out.push({
        sceneIdx: sIdx, segIdx: g, text, anchor,
        // Stable identity used by the AUTO-skip filter. Must match the key
        // written by toggleSegmentAutoInclude (`s{sceneIdx}g{segIdx}`) so that
        // segments with duplicate first-line text don't get collapsed onto
        // one skip flag.
        autoSkipKey: `s${sIdx}g${g}`,
        has_close_up: hasCloseUp, durationSec,
      });
    }
  });
  return out;
}

function _autoUpdateStatusUI() {
  // Floating widget — visible everywhere on the site while any run is active,
  // shows progress + percent + current status + stop button. Survives
  // navigation (each run continues on its captured epSid/epNumber regardless of
  // which page is currently rendered). It also refreshes the per-episode
  // toolbar button + inline status via _autoRefreshToolbarBtn().
  _autoUpdateFloatingWidget();
}

// ── Floating Auto-mode progress widget ─────────────────────────────────────
// Pinned to the bottom-right, always-on while AUTO.active. Lazily injected so
// it doesn't appear in the DOM until first activation. Click on it to navigate
// back to the source episode; «⏸» button stops AUTO from anywhere.
function _autoEnsureFloatingWidget() {
  let w = document.getElementById('auto-float');
  if (w) return w;
  w = document.createElement('div');
  w.id = 'auto-float';
  w.className = 'auto-float';
  document.body.appendChild(w);
  return w;
}
function _autoUpdateFloatingWidget() {
  // Keep the in-episode toolbar button + status in sync with the current
  // episode's run (per-episode, not the global singleton).
  try { _autoRefreshToolbarBtn(); } catch {}
  const rangeActive = (typeof RANGE !== 'undefined') && RANGE.active;
  const runs = _autoAllRuns();
  const active = runs.length > 0 || rangeActive;
  if (!active) {
    const w = document.getElementById('auto-float');
    if (w) w.remove();
    return;
  }
  const w = _autoEnsureFloatingWidget();
  // Aggregate totals across all active runs (range-gen with parallel
  // episodes shows N rows). When only one run is active, looks like the
  // old single-line widget.
  const totalAgg = runs.reduce((s, r) => s + (r.total || 0), 0);
  const doneAgg  = runs.reduce((s, r) => s + (r.completedCount || 0), 0);
  const pctAgg   = totalAgg ? Math.round((doneAgg / totalAgg) * 100) : 0;
  const queueBit = (rangeActive && Array.isArray(RANGE.queue) && RANGE.queue.length)
    ? ` · очередь ${Math.min(RANGE.curIdx + 1, RANGE.queue.length)}/${RANGE.queue.length}`
    : '';
  const headLabel = runs.length > 1
    ? `Auto-mode × ${runs.length}`
    : 'Auto-mode';
  const rowsHtml = runs.map(r => {
    const total = r.total || 0;
    const done = r.completedCount || 0;
    const pct  = total ? Math.round((done / total) * 100) : 0;
    let mode;
    if (r.parallel) mode = 'паралл.';
    else if ((r.activeChains || 0) > 1) mode = `сцены × ${r.activeChains}`;
    else mode = 'последов.';
    const ep = r._epNumber != null ? `Эп.${r._epNumber}` : '';
    // Suspended (restored from localStorage after a refresh) → show resume btn
    // instead of the live spinner, and freeze the status string so it doesn't
    // pretend the loop is still chugging.
    const resumeBtn = r.suspended
      ? `<button class="auto-float-resume" data-sid="${esc(r._epSid || '')}" data-ep="${esc(String(r._epNumber || ''))}" title="Возобновить — пропустит уже готовые чанки">▶ Продолжить</button>`
      : '';
    // Per-run stop — kills just this episode, leaving other parallel runs alive.
    const stopBtn = r.suspended
      ? ''
      : `<button class="auto-float-stop-row" data-sid="${esc(r._epSid || '')}" data-ep="${esc(String(r._epNumber || ''))}" title="Остановить только этот эпизод" style="margin-left:6px;background:none;border:0;color:inherit;cursor:pointer;opacity:.7;font-size:0.85rem">⏹</button>`;
    const spinHtml = r.suspended ? '⏸' : '<span class="auto-float-spin"></span>';
    return `
      <div class="auto-float-row${r.suspended ? ' auto-float-row-suspended' : ''}" data-sid="${esc(r._epSid || '')}" data-ep="${esc(String(r._epNumber || ''))}">
        <div class="auto-float-row-head">
          ${spinHtml}
          <span class="auto-float-row-label">${esc(ep)}</span>
          <span class="auto-float-row-count">${total ? `${done}/${total} · ${pct}%` : '…'}</span>
          ${resumeBtn}${stopBtn}
        </div>
        <div class="auto-float-bar"><div class="auto-float-bar-fill" style="width:${pct}%"></div></div>
        <div class="auto-float-status">${esc(mode)} · ${esc(r.lastStatus || '...')}</div>
      </div>`;
  }).join('');
  w.innerHTML = `
    <div class="auto-float-head">
      <span class="auto-float-spin"></span>
      <span class="auto-float-title">${esc(headLabel)}</span>
      <span class="auto-float-count">${totalAgg ? `${doneAgg}/${totalAgg} · ${pctAgg}%${queueBit}` : (queueBit || '…')}</span>
      <button class="auto-float-stop" title="Остановить генерацию">⏸</button>
    </div>
    ${runs.length > 0 ? `<div class="auto-float-rows">${rowsHtml}</div>` : ''}
  `;
  // Wire stop + click-to-navigate. Each row's head click navigates to that ep.
  w.querySelector('.auto-float-stop')?.addEventListener('click', (ev) => {
    ev.stopPropagation();
    try { if (typeof stopRangeGen === 'function' && (typeof RANGE !== 'undefined') && RANGE.active) stopRangeGen(); } catch {}
    // Stop EVERY run (primary AUTO + all standalone parallel runs), not just the
    // singleton — otherwise the ⏸ in the header would leave parallel runs going.
    try { _stopAllAutoRuns(); } catch {}
    showToast('⏸ Останавливаю все авторежимы после текущего шага…', 3000);
  });
  // Per-row stop — cancels just that episode's run, leaving siblings running.
  // Bound before the row-level navigation handler so the click isn't swallowed.
  w.querySelectorAll('.auto-float-stop-row').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const sid = btn.dataset.sid;
      const ep  = parseInt(btn.dataset.ep, 10);
      if (sid && !Number.isNaN(ep)) _stopAutoRunForEpisode(sid, ep);
    });
  });
  // Resume buttons on suspended rows — must run BEFORE the row-level
  // navigation handler binds, otherwise the row click intercepts the button.
  w.querySelectorAll('.auto-float-resume').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const sid = btn.dataset.sid;
      const ep  = parseInt(btn.dataset.ep, 10);
      if (sid && !Number.isNaN(ep)) _autoResumeRun(sid, ep);
    });
  });
  w.querySelectorAll('.auto-float-row').forEach(row => {
    row.addEventListener('click', () => {
      const sid = row.dataset.sid, ep = row.dataset.ep;
      if (sid && ep) {
        try { navigate('episode', { seriesId: sid, episodeNum: parseInt(ep, 10) }); } catch {}
      }
    });
  });
  // Keep the old single-run dataset fields populated for backward-compat
  // navigation (when widget is shown for RANGE only without active runs).
  if (runs.length === 1) {
    w.dataset.sid = runs[0]._epSid || '';
    w.dataset.ep  = String(runs[0]._epNumber || '');
  }
  // Persist active runs to localStorage so a page refresh can restore them.
  _autoPersistRuns();
}
function _autoUpdateFloatingWidget_LEGACY_HEAD_NOT_USED() {
  // Stub — original code path that referenced AUTO directly. Kept for safety
  // in case any external caller pokes at it; the real widget is above.
  let mode;
  if (AUTO.parallel) mode = 'паралл.';
  else if ((AUTO.activeChains || 0) > 1) mode = `сцены × ${AUTO.activeChains}`;
  else mode = 'последов.';
  const epLabel = ep ? `Эп.${ep}` : '';
  // Append range-gen queue progress if a multi-episode queue is in flight.
  let rangeBit = '';
  if (rangeActive && Array.isArray(RANGE.queue) && RANGE.queue.length) {
    const cur = (RANGE.curIdx >= 0 ? RANGE.curIdx + 1 : 0);
    rangeBit = ` · очередь ${cur}/${RANGE.queue.length}`;
  }
  if (statusEl) statusEl.textContent = `${epLabel} · ${mode} · ${AUTO.lastStatus || '...'}${rangeBit}`;
}

// ── In-app confirm modal ───────────────────────────────────────────────────
// Returns Promise<bool>. Replaces native window.confirm() for high-traffic
// user flows (Auto-mode, range-gen) — Chrome silently auto-rejects native
// dialogs after several confirms in a single tab session («Don't show more
// dialogs»), which manifested as «AUTO не запускается, ничего не происходит».
// In-app modal lives in our own DOM, immune to the browser's throttle.
function appConfirm(opts) {
  const {
    title = 'Подтверждение',
    message = '',
    okText = 'OK',
    cancelText = 'Отмена',
    okStyle = 'primary',     // 'primary' | 'danger' | 'accent'
  } = (typeof opts === 'string') ? { message: opts } : (opts || {});
  return new Promise((resolve) => {
    // Tear down any prior instance so rapid consecutive calls don't stack.
    document.getElementById('app-confirm-modal')?.remove();
    const root = document.createElement('div');
    root.id = 'app-confirm-modal';
    root.className = 'app-confirm-modal';
    const okClass = okStyle === 'danger' ? 'btn-danger' : okStyle === 'accent' ? 'btn-accent' : 'btn-primary';
    root.innerHTML = `
      <div class="app-confirm-backdrop"></div>
      <div class="app-confirm-box" role="dialog" aria-modal="true">
        <div class="app-confirm-title">${esc(title)}</div>
        <div class="app-confirm-body"></div>
        <div class="app-confirm-actions">
          <button class="btn-ghost" data-act="cancel">${esc(cancelText)}</button>
          <button class="${okClass}" data-act="ok" autofocus>${esc(okText)}</button>
        </div>
      </div>
    `;
    // Body via textContent so newlines preserve and we don't HTML-eval.
    root.querySelector('.app-confirm-body').textContent = message;
    document.body.appendChild(root);
    const close = (val) => {
      try { root.remove(); } catch {}
      document.removeEventListener('keydown', onKey);
      resolve(val);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
      else if (e.key === 'Enter') { e.preventDefault(); close(true); }
    };
    document.addEventListener('keydown', onKey);
    root.querySelector('[data-act="cancel"]').addEventListener('click', () => close(false));
    root.querySelector('[data-act="ok"]').addEventListener('click', () => close(true));
    root.querySelector('.app-confirm-backdrop').addEventListener('click', () => close(false));
    // Focus OK so Enter works.
    setTimeout(() => root.querySelector('[data-act="ok"]')?.focus(), 50);
  });
}

function _autoModeToggle() {
  // Per-episode toggle: if THIS episode has a live run, stop just it; otherwise
  // start a run (primary if the singleton is free, else a parallel standalone).
  const sid = (typeof S !== 'undefined' && S) ? S.seriesId : null;
  const num = (typeof S !== 'undefined' && S && S.episode) ? S.episode.number : null;
  if (_autoRunForEpisode(sid, num)) { _stopAutoRunForEpisode(sid, num); return; }
  startAutoMode();
}

function stopAutoMode() {
  if (!AUTO.active) return;
  AUTO.cancelRequested = true;
  showToast('⏸ Auto-mode остановится после текущего шага...', 3000);
  _autoUpdateStatusUI();
}

// Fire-and-forget client → backend log bridge. Used to record AUTO milestones
// so debugging "почему ничего не запустилось" doesn't require a DevTools
// session — sequence shows up directly in admin Logs viewer.
function clog(level, event, fields = {}) {
  try {
    fetch('/api/client-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level, event, ...fields }),
      keepalive: true,
    }).catch(() => {});
  } catch {}
}

