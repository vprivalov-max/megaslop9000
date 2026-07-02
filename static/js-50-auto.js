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

async function startAutoMode() {
  clog('INFO', 'auto.entry', { sid: S.seriesId || null, ep: S.episode?.number ?? null });
  if (!S.episode) {
    clog('WARN', 'auto.bail', { reason: 'no_episode' });
    showToast('⚠ Сначала открой эпизод');
    return;
  }
  const _sid = S.seriesId, _num = S.episode.number;
  // Already running on THIS episode → ignore (the button shows Stop in that
  // case; stopping is handled by _autoModeToggle, not here).
  if (_autoRunForEpisode(_sid, _num)) {
    clog('WARN', 'auto.bail', { reason: 'episode_already_running', ep: _num });
    showToast(`Auto-mode уже идёт на эпизоде ${_num}`);
    return;
  }
  // Global concurrency ceiling — at most MAX_CONCURRENT_AUTO live runs across
  // the series at once (matches range-gen; the backend handles 3 concurrent
  // per-episode pipelines safely).
  if (_autoActiveRunCount() >= MAX_CONCURRENT_AUTO) {
    clog('WARN', 'auto.bail', { reason: 'concurrency_cap', cap: MAX_CONCURRENT_AUTO });
    showToast(`⚠ Уже идёт ${MAX_CONCURRENT_AUTO} авторежима одновременно — дождись или останови один (⏹ в виджете)`, 6000);
    return;
  }
  // If the PRIMARY slot is busy on another episode, spin THIS episode up as an
  // additional parallel run via the self-contained standalone engine. The rich
  // primary path below (turbo / skip-filter / confirm) is reserved for the
  // first/only run; parallel runs are sequential + scene-parallel.
  if (AUTO.active) {
    return _startParallelAutoMode(_sid, _num);
  }
  // Pre-flight: warn if any active char/loc lacks ref before kicking off N
  // chunks of generation. User often regrets discovering missing assets only
  // after burning compute on chunks with placeholder/random faces.
  if (!await _confirmMissingAssetsBeforeGen('Auto-mode (видео-генерацию)')) {
    clog('WARN', 'auto.bail', { reason: 'missing_assets_cancelled' });
    return;
  }
  // Auto-mode hard requirement: duration MUST be 15s. The whole segmentation
  // logic (TARGET=12s, SOFT_MAX=13s, MIN=5s) is calibrated assuming 15s
  // Seedance chunks. If user picked 5/10s clips, segments won't fit and the
  // whole batch will be off-rhythm. Offer to auto-fix or cancel.
  const durEl = document.getElementById('sd-duration');
  const curDur = parseInt(durEl?.value, 10);
  if (curDur !== 15) {
    const confirmFix = await appConfirm({
      title: '⚠ Не та длительность Seedance',
      message:
        `Длительность Seedance стоит ${curDur || '?'}с, но Auto-mode калиброван под 15-секундные чанки.\n\n` +
        `Сегментация рассчитывала контент ≤13с с 2с буфером — короткие чанки порежут реплики, ` +
        `длинные дадут пустоту в конце.\n\n` +
        `Поставить 15с автоматически и продолжить?`,
      okText: 'Поставить 15с и запустить',
      cancelText: 'Отмена',
      okStyle: 'accent',
    });
    if (!confirmFix) {
      clog('WARN', 'auto.bail', { reason: 'duration_cancelled', curDur });
      return;
    }
    if (durEl) {
      durEl.value = '15';
      durEl.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  // ── INSTANT MUSIC KICK-OFF (single-episode auto-mode) ────────────────────
  // Fire music NOW — before blocking / batch-compose / any video chunk.
  // Script is already in the DOM, so we can compute the scene plan instantly.
  if (S.series?.settings?.enable_music !== false && S.seriesId && S.episode?.number != null) {
    (async () => {
      try {
        const epScript = (document.getElementById('ep-script')?.value || '').trim();
        if (epScript) {
          const scenes = _parseScriptScenes(epScript, _segmentOverrides());
          const planMap = new Map();
          scenes.forEach((sc, sIdx) => {
            for (let g = 0; g < sc.segCount; g++) {
              const lines = sc.lines.filter(l => l.segIdx === g);
              if (!lines.length) continue;
              const dur = _estimateChunkDurationSec(lines.map(l => l.text).join('\n'));
              planMap.set(sIdx, (planMap.get(sIdx) || 0) + dur);
            }
          });
          if (planMap.size) {
            const scenesPlan = [...planMap.entries()].map(([sceneIdx, totalSec]) => ({
              sceneIdx,
              target_duration_ms: Math.round(totalSec * 0.9 * 1000),
            }));
            const r = await api.post(
              `/api/series/${S.seriesId}/episodes/${S.episode.number}/music/generate`,
              { scenes_plan: scenesPlan }
            );
            if (r?.ok && r.scenes?.length) {
              showToast(`🎵 Музыка запущена — ${r.scenes.length} сцен (параллельно с видео)`, 3000);
            }
          }
        }
      } catch (e) {
        clog('WARN', 'auto.music_kickoff_fail', { err: (e?.message || String(e)).slice(0, 200) });
      }
    })();
  }
  // ─────────────────────────────────────────────────────────────────────────

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
  // ── Turbo engine selector ─────────────────────────────────────────────────
  // Two implementations of Turbo live side-by-side, switchable in Settings:
  //   • 'parallel-sequential' (default, experimental) — per-chunk Claude
  //     compose like sequential mode, then ALL chunks fire in parallel
  //     without last-frame / cut-frames continuity. No batch JSON, no scene
  //     blocking, no character-position planning, no auto-revise. Cheap +
  //     simple. Hypothesis: per-chunk prompts are enough; the heavy batch
  //     prep doesn't pay off.
  //   • 'shadow' — full Shadow Founder–style pipeline ported from the
  //     colleague's project: scene blocking → batch-compose → auto-revise →
  //     parallel /start with prebuilt prompts. Heavier, more deterministic
  //     spatial continuity.
  // Sequential mode is unaffected by this toggle.
  const turboEngine = isTurbo ? _turboEngine() : 'n/a';
  const turboShadow = turboEngine === 'shadow';

  // Turbo prerequisites: episodeBlocking + batch_prompts must exist.
  // If empty — auto-fill them before starting the parallel run, so the user
  // doesn't have to hit two extra buttons every time.
  //
  // These two Claude calls take ~10-30s each. Without a visible widget the
  // user thinks nothing's happening between clicking «Auto-mode» and the
  // first chunk creating itself (~60s of silence). Show the floating widget
  // in a «preparing» state for the duration of these prereqs so progress is
  // always visible.
  //
  // NOTE: this entire prep block (scene blocking + batch JSON + auto-revise)
  // is shadow-engine-only. The experimental parallel-sequential engine skips
  // all prep and just lets the AUTO loop run per-chunk compose in parallel.
  if (isTurbo && turboShadow) {
    const blockingEl = document.getElementById('ep-scene-blocking');
    const blocking = (blockingEl?.value || '').trim();
    const hasBatchPrompts = !!(S.episode?.batch_prompts && Object.keys(S.episode.batch_prompts).length);
    const needPrep = !blocking || !hasBatchPrompts;
    if (needPrep) {
      // Mark AUTO as active so the floating widget appears immediately.
      // _epSid/_epNumber stash so the widget header navigates to this ep.
      AUTO.active = true;
      AUTO.parallel = true;
      AUTO.total = 0;
      AUTO.completedCount = 0;
      AUTO._epSid = S.seriesId;
      AUTO._epNumber = S.episode.number;
      AUTO.lastStatus = '⚙ Турбо: подготовка…';
      _autoRegisterRun(AUTO);
      _autoUpdateStatusUI();
    }
    try {
      if (!blocking) {
        AUTO.lastStatus = '⚙ Турбо 1/2: scene blocking (~10-30с)…';
        _autoUpdateFloatingWidget();
        showToast('⚙ Турбо 1/2: scene blocking…', 4000);
        try {
          if (typeof generateSceneBlocking === 'function') await generateSceneBlocking();
        } catch (e) {
          // Clean up the floating widget before bailing.
          AUTO.active = false;
          _autoUnregisterRun(AUTO);
          _autoUpdateStatusUI();
          showToast('✗ Не удалось сгенерить blocking: ' + (e.message || e), 6000);
          return;
        }
      }
      let batchFreshlyBuilt = false;
      if (!hasBatchPrompts) {
        AUTO.lastStatus = '⚙ Турбо 2/3: batch JSON (~15-30с)…';
        _autoUpdateFloatingWidget();
        showToast('⚙ Турбо 2/3: batch JSON эпизода…', 4000);
        try {
          if (typeof rebuildBatchPrompts === 'function') await rebuildBatchPrompts();
          batchFreshlyBuilt = true;
        } catch (e) {
          AUTO.active = false;
          _autoUnregisterRun(AUTO);
          _autoUpdateStatusUI();
          showToast('✗ Не удалось собрать batch JSON: ' + (e.message || e), 6000);
          return;
        }
      }
      // ─── Турбо шаг 3/3 — авто-правка ─────────────────────────────────────
      // Mirrors colleague's auto-pipeline step 9.5 («✨ План + правка + видео»):
      // ONE Claude call rewrites every main promptEn so characters don't jump
      // in space, dialogues stay readable, chunks splice cleanly. The exact
      // wording comes from per-user settings (Modal → 🎬 Автоматическая правка).
      //
      // Trigger policy:
      //   • если batch только что собрали в этом запуске Auto-mode — правим
      //     всегда (это поведение колеги: revise после plan по умолчанию).
      //   • если batch уже существовал (повторный запуск Auto-mode после
      //     refresh) — правим только если правки ещё не было (нет
      //     `batch_revised_at` на эпизоде) И флаг включён в настройках.
      //   • если пользователь отключил флаг в настройках — правку
      //     пропускаем целиком.
      try {
        let arEnabled = true;
        try {
          const ar = await api.get('/api/user/auto-revise');
          arEnabled = !!ar.auto_revise_enabled;
        } catch (_) { /* keep default */ }
        const alreadyRevised = !!(S.episode?.batch_revised_at);
        const shouldRevise = arEnabled && (batchFreshlyBuilt || !alreadyRevised);
        if (shouldRevise) {
          AUTO.lastStatus = '⚙ Турбо 3/3: авто-правка main-промптов (~15-40с)…';
          _autoUpdateFloatingWidget();
          showToast('⚙ Турбо 3/3: применяю автоматическую правку…', 4000);
          const resp = await api.post(
            `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/revise-batch`,
            {},
            { timeoutMs: 5 * 60 * 1000 }
          );
          // Reload episode so subsequent code (segment-collect, validators)
          // sees the revised promptEns. Best-effort: failure here is non-fatal
          // — original promptEns are still valid Seedance input.
          try {
            const fresh = await api.get(`/api/series/${S.seriesId}/episodes/${S.episode.number}`);
            if (fresh) S.episode = fresh;
          } catch (_) {}
          if (resp && resp.count != null) {
            showToast(`✓ Правка применена: ${resp.count}/${resp.expected} чанков`, 3500);
          }
        }
      } catch (e) {
        // Revise is a quality booster, not a hard requirement — log and continue.
        showToast('⚠ Авто-правка упала: ' + (e.message || e) + ' — продолжаю с исходными промптами', 6000);
      }
      // Final prep tick before segment-building / confirm dialog.
      if (needPrep) {
        AUTO.lastStatus = '⚙ Турбо: считаю сегменты…';
        _autoUpdateFloatingWidget();
      }
    } catch (e) {
      // Shouldn't reach here (inner try/catch handles per-step), but defense.
      AUTO.active = false;
      _autoUnregisterRun(AUTO);
      _autoUpdateStatusUI();
      throw e;
    }
  }
  AUTO.errorMode = (localStorage.getItem('auto_error_mode') || 'heal');
  const allSegs  = _autoCollectSegments();
  // Annotate scriptOrder BEFORE the skip-filter — the script slot a segment
  // owns is its position in the FULL list, not its position among the still-
  // selected ones. If we reindex after filtering, a user who skips seg 0..2
  // and keeps seg 3 ends up generating a chunk with script_order=0; that
  // collides with the original chunk #1 (also script_order=0), the auto-
  // assemble dedup picks the newer take and drops the real #1 from the
  // final cut. Bug repro: «My Roommate From Craigslist Is Hunting Me» ep 1
  // — uncheck AUTO on 1/2/3, keep 4, auto-mode spawned a phantom «#1 v2».
  allSegs.forEach((s, i) => { s.scriptOrder = i; });
  // Per-segment auto-mode skip filter. Uses the unique-per-episode
  // autoSkipKey (`s{sceneIdx}g{segIdx}`), NOT the first-line text — see
  // toggleSegmentAutoInclude for why.
  const skippedCount = allSegs.filter(s => _isSegmentAutoSkipped(s.autoSkipKey)).length;
  // Drop segments whose chunk is already finished on the backend (matched by
  // script_order). This is what makes resume-after-refresh non-destructive —
  // re-running auto-mode picks up exactly where the previous run died.
  // `completed` is the success status set when Seedance returns the mp4; QC
  // may still be pending but the chunk file exists, so re-generating it would
  // overwrite working output for no benefit.
  const existingChunks = (S.episode && S.episode.seedance_chunks) || [];
  const completedOrders = new Set(
    existingChunks
      .filter(c => c && c.status === 'completed' && typeof c.script_order === 'number')
      .map(c => c.script_order)
  );
  const alreadyDoneCount = allSegs.filter(s => completedOrders.has(s.scriptOrder)).length;
  AUTO.segments = allSegs.filter(s =>
    !_isSegmentAutoSkipped(s.autoSkipKey) && !completedOrders.has(s.scriptOrder)
  );
  AUTO.total    = AUTO.segments.length;
  AUTO.cursor   = 0;             // back-compat with status UI
  AUTO.completedCount = 0;       // atomic counter across chains
  AUTO.activeChains = 0;
  AUTO.cancelRequested = false;
  AUTO.lastStatus = '';

  if (!AUTO.total) {
    clog('WARN', 'auto.bail', {
      reason: 'no_segments',
      total_raw: allSegs.length,
      skipped: skippedCount,
      script_len: (document.getElementById('ep-script')?.value || '').length,
    });
    // If we registered the AUTO run for Turbo prep, unregister so the
    // floating widget disappears.
    AUTO.active = false;
    _autoUnregisterRun(AUTO);
    _autoUpdateStatusUI();
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
  const doneNote = alreadyDoneCount ? `\nУже сгенерены (пропустим): ${alreadyDoneCount}` : '';
  const engineNote = isTurbo
    ? (turboShadow
        ? '\nТурбо-движок: Shadow (batch JSON + scene blocking + авто-правка)'
        : '\nТурбо-движок: Параллельный-последовательный (per-chunk compose, без batch JSON / blocking / правки)')
    : '';
  const _confirmMsg =
    `Сегментов: ${AUTO.total}${skipNote}${doneNote}\n` +
    `Режим: ${modeWord}${engineNote}\n` +
    `На ошибке модерации: ${errWord}\n\n` +
    (AUTO.parallel
      ? 'Параллельный режим: все сегменты отправляются в очередь Seedance подряд (~2с между запусками). Текстовый контекст между чанками сохраняется.'
      : sceneGroups.length > 1
        ? 'Внутри каждой сцены чанки идут последовательно (нужно для last-frame / cut-frames continuity). Сцены друг от друга не зависят и идут параллельно (cap = 3 одновременно).'
        : 'Последовательный режим: каждый чанк ждёт предыдущего.');
  if (!await appConfirm({
    title: '▶ Запустить Auto-mode?',
    message: _confirmMsg,
    okText: '▶ Запустить',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) {
    clog('WARN', 'auto.bail', { reason: 'main_confirm_cancelled', total: AUTO.total });
    // Unregister the Turbo-prep run if we registered one upstairs.
    AUTO.active = false;
    _autoUnregisterRun(AUTO);
    _autoUpdateStatusUI();
    return;
  }

  AUTO.active = true;
  _autoUpdateStatusUI();
  clog('INFO', 'auto.start', { total: AUTO.total, parallel: !!AUTO.parallel, scenes: sceneGroups.length });
  showToast(`▶ Auto-mode запущен · ${AUTO.total} сегмент${AUTO.total > 1 ? 'ов' : ''} (${modeWord})`, 4000);

  // CRITICAL: capture episode identity ONCE — every in-flight request must target
  // the episode the user pressed "auto-mode" on. If user navigates to another
  // episode mid-run, S.episode.number changes and pending writes leak into the
  // wrong episode (chunks ended up in the wrong file, episode 2's batch wrote
  // into episode 3 — May 2026 incident).
  const epSid    = S.seriesId;
  const epNumber = S.episode.number;
  // Stash on AUTO so the floating widget knows what episode this run targets.
  AUTO._epSid = epSid;
  AUTO._epNumber = epNumber;
  _autoRegisterRun(AUTO);

  // Read params from sd panel (used for /seedance/start)
  const duration = parseInt(document.getElementById('sd-duration').value) || 15;
  const resolution = document.getElementById('sd-resolution').value;
  const moderation_bypass = document.getElementById('sd-mod-bypass').value;
  const model_tier = document.getElementById('sd-model-tier')?.value || 'reference-fast';
  const POLL_INTERVAL_MS = 8000;
  const PARALLEL_DELAY_MS = 2000;
  const MAX_HEAL_RETRIES = 1;
  const MAX_PARALLEL_SCENES = 3;
  const sharedOpts = { useLastframe, useCutframes, useStyle, styleVal, baseOnly, closeUpOnly,
                       duration, resolution, moderation_bypass, model_tier, POLL_INTERVAL_MS, MAX_HEAL_RETRIES };

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
        model: sharedOpts.model_tier,
        script_order: (scriptOrder != null ? scriptOrder : (typeof seg.scriptOrder === 'number' ? seg.scriptOrder : null)),
        sceneIdx: seg.sceneIdx,
        segIdx: seg.segIdx,
        durationSec: seg.durationSec || sharedOpts.duration,
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
  // Direct, episode-targeted poll. We must NOT use sdPollOnce() here — that one
  // reads `S.episode.number`, which changes the moment the user navigates to
  // another episode mid-generation. AUTO must always poll the episode it was
  // started on (epSid/epNumber from closure), so generation continues from the
  // background tab even when the user is browsing other parts of the app.
  // sdRenderList() is still called when the target episode IS the visible one,
  // so the chunk cards animate normally.
  async function _autoPollTargetEpisode() {
    try {
      const res = await api.post(
        `/api/series/${epSid}/episodes/${epNumber}/seedance/poll`, {}
      );
      const chunks = res.chunks || [];
      // Only update the on-screen chunk grid if THIS episode is the visible one.
      if (S.seriesId === epSid && S.episode?.number === epNumber) {
        try { _sdNotifyTransitions(chunks); } catch {}
        try { sdRenderList(chunks); } catch {}
      }
      return chunks;
    } catch (e) { return null; }
  }

  async function _autoPollUntilDone(chunkIdx, composeRes, segText, segDuration) {
    let healAttempts = 0;
    let curIdx = chunkIdx;
    while (true) {
      if (AUTO.cancelRequested) return { ok: false, error: 'cancelled' };
      await new Promise(r => setTimeout(r, sharedOpts.POLL_INTERVAL_MS));
      const polled = await _autoPollTargetEpisode();
      const chunk = (polled || []).find(c => c.idx === curIdx);
      if (!chunk) {
        AUTO.lastStatus = `… не вижу чанка #${curIdx}`;
        _autoUpdateStatusUI();
        continue;
      }
      const qcStatus = chunk.qc?.status || null;
      if (chunk.status === 'completed' && qcStatus == null) {
        AUTO.lastStatus = `🔍 #${curIdx} QC...`;
      } else {
        AUTO.lastStatus = chunk.status === 'processing' && chunk.progress != null
          ? `⏳ #${curIdx} ${chunk.progress}%`
          : `⏳ #${curIdx} ${chunk.status}${qcStatus ? ' · QC '+qcStatus : ''}`;
      }
      _autoUpdateStatusUI();
      // QC gate: completed alone isn't enough — wait for qc.status='pass'
      // (or 'retry_exhausted'; that means QC gave up and we accept as-is).
      if (chunk.status === 'completed' && qcStatus === 'pass') {
        return { ok: true, chunk };
      }
      if (chunk.status === 'completed' && qcStatus === 'retry_exhausted') {
        showToast(`⚠ QC сдался на чанке #${curIdx} после ${chunk.qc?.attempts || '?'} попыток (${(chunk.qc?.fails || []).join(', ')}) — принимаем как есть`, 8000);
        return { ok: true, chunk };
      }
      if (chunk.status === 'completed' && qcStatus === 'fail') {
        // Hard cap on retry-storm: if 3+ chunks already exist for this
        // segment text, accept and move on. Belt-and-suspenders for the
        // case where server `attempts` somehow didn't reach the cap.
        const segText2 = (segText || '').trim();
        const dupCount = (polled || []).reduce(
          (n, c) => n + ((c.chunk_text || '').trim() === segText2 ? 1 : 0), 0
        );
        if (dupCount >= 3) {
          showToast(`⚠ Уже ${dupCount} попыток для чанка #${curIdx} (${(chunk.qc?.fails || []).join(', ')}) — продолжаем дальше без ретрая`, 8000);
          return { ok: true, chunk };
        }
        // QC failed and we still have retry budget — trigger fresh start
        // with the same prompt + a server-injected hint about what to fix.
        AUTO.lastStatus = `🔁 #${curIdx} QC retry ${chunk.qc.attempts}/3 (${(chunk.qc.fails || []).slice(0, 2).join(',')})`;
        _autoUpdateStatusUI();
        const retryHint = _qcBuildPromptHint(chunk.qc.fails || []);
        const restart = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
          {
            prompt: (composeRes.prompt || chunk.prompt || '') + retryHint,
            chunk_text: segText,
            duration: segDuration || sharedOpts.duration, resolution: sharedOpts.resolution,
            moderation_bypass: sharedOpts.moderation_bypass,
            model: sharedOpts.model_tier,
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
              model: sharedOpts.model_tier,
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
      //
      // Engine 'parallel-sequential' (default, experimental) skips batch-compose
      // entirely — `batchPrompts` stays null and every segment falls through to
      // `_autoComposeStart()` (per-chunk Claude compose, same path sequential
      // mode uses). Concurrency cap below still applies, so they all submit in
      // parallel. Easy rollback: flip Settings → «Турбо-движок» back to «Shadow».
      let batchPrompts = null;
      if (turboShadow) {
      AUTO.lastStatus = '🧠 batch-compose (один Claude call на всю серию)...';
      _autoUpdateStatusUI();
      try {
        const batchRes = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/batch-compose`,
          {
            segments: AUTO.segments.map(s => ({
              anchor: s.anchor, sceneIdx: s.sceneIdx, segIdx: s.segIdx,
              text: s.text, has_close_up: !!s.has_close_up, durationSec: s.durationSec,
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

        // ── Early-fire music (turbo mode) ──────────────────────────────────
        // batch-compose just ran: we have sceneIdx + durationSec for every
        // segment. Fire music now — in parallel with the upcoming /start calls.
        // Backend is idempotent: if music already generated it will be skipped.
        const musicEnabled = S.series?.settings?.enable_music !== false;
        if (musicEnabled) {
          // Build scene plan: group AUTO.segments by sceneIdx, sum durations.
          const planMap = new Map();
          for (const s of AUTO.segments) {
            const k = s.sceneIdx ?? 0;
            planMap.set(k, (planMap.get(k) || 0) + (s.durationSec || 15));
          }
          const scenesPlan = [...planMap.entries()].map(([sceneIdx, totalSec]) => ({
            sceneIdx,
            target_duration_ms: Math.round(totalSec * 0.9 * 1000),
          }));
          api.post(
            `/api/series/${epSid}/episodes/${epNumber}/music/generate`,
            { scenes_plan: scenesPlan }
          ).then(r => {
            if (r?.ok) showToast(`🎵 Музыка запущена — ${r.scenes?.length || 0} сцен (параллельно с видео)`, 4000);
          }).catch(() => {});
        }
        // ───────────────────────────────────────────────────────────────────
      } catch (e) {
        showToast(`⚠ batch-compose упал — использую per-chunk: ${e.message || e}`, 6000);
        batchPrompts = null;
      }
      } else {
        // parallel-sequential engine: no batch-compose. batchPrompts stays null;
        // every seg falls through to _autoComposeStart() below.
        AUTO.lastStatus = '⚙ Турбо (параллельно): per-chunk compose в параллель…';
        _autoUpdateStatusUI();
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
            // Use seg.scriptOrder (assigned BEFORE the skip-filter) instead of
            // the local index `i` — `i` is position in the filtered AUTO.segments
            // list and collides with existing chunks when the user skipped some
            // earlier segments. See the `allSegs.forEach((s, i) => { s.scriptOrder = i; })`
            // comment above for the repro.
            startRes = await api.post(
              `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
              {
                prompt: prebuilt.prompt,
                chunk_text: seg.text,
                duration: segDur, resolution, moderation_bypass,
                script_order: seg.scriptOrder,
                sceneIdx: seg.sceneIdx,
                segIdx: seg.segIdx,
                durationSec: segDur,
                refs: (prebuilt.refs || []).map(r => ({
                  kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
                  source: r.source, prev_idx: r.prev_idx, name: r.name,
                })),
              }
            );
          } else {
            // Fallback per-chunk compose if batch missed this anchor.
            // Pass seg.scriptOrder (full-list position) — _autoComposeStart
            // forwards it to /seedance/start as the canonical slot.
            const cs = await _autoComposeStart(seg, seg.scriptOrder);
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
    _autoUnregisterRun(AUTO);
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

      // Fire-and-forget music generation after a successful single-episode
      // auto-mode run. Same gate as range-gen — series.settings.enable_music.
      // We don't block on it (music takes ~30-90s per scene, separate UI poll
      // handles status). Skipped if cancelled or partial.
      const epForMusic = epNum;
      const musicEnabled = S.series?.settings?.enable_music !== false;
      if (musicEnabled && epForMusic != null && AUTO.completedCount === AUTO.total && AUTO.total > 0) {
        api.post(`/api/series/${S.seriesId}/episodes/${epForMusic}/music/generate`, {})
          .then(r => {
            if (r?.ok) {
              showToast(`🎵 Музыка запущена — ${r.scenes?.length || 0} сцен`, 4000);
              try { _sdEnsureMusicPoll(); _sdRefreshMusic(); } catch {}
            } else if (r?.error) {
              showToast(`🎵 ⚠ ${r.error}`, 5000);
            }
          })
          .catch(e => showToast(`🎵 ⚠ ${e.message || e}`, 5000));
      }
    } else {
      showToast(`⏸ Auto-mode остановлен · обработано ${AUTO.completedCount}/${AUTO.total}`, 5000);
    }
    _autoUpdateStatusUI();
  } catch (e) {
    AUTO.active = false;
    _autoUnregisterRun(AUTO);
    _autoUpdateStatusUI();
    Sounds.playError();
    clog('ERROR', 'auto.crash', { msg: (e?.message || String(e)).slice(0, 400) });
    showToast(`✗ Auto-mode упал: ${e.message || e}`, 8000);
  }
}

// ── Manual parallel launcher ───────────────────────────────────────────────
// Spins the currently-open episode up as an ADDITIONAL auto-mode run while
// another episode's run is already in flight. Runs the same pre-flight as the
// primary path (missing-asset check + 15s-duration enforcement + confirm),
// gathers params from the Seedance panel, then delegates to the proven
// standalone engine. Always sequential / scene-parallel — turbo submits
// everything at once and finishes fast, so it never needs a long-lived slot.
async function _startParallelAutoMode(sid, num) {
  // Pre-flight: don't burn compute on chunks with placeholder/random faces.
  if (!await _confirmMissingAssetsBeforeGen('Auto-mode (видео-генерацию)')) {
    clog('WARN', 'auto.parallel_bail', { reason: 'missing_assets_cancelled', ep: num });
    return;
  }
  // Duration must be 15s — segmentation is calibrated around 15s chunks.
  const durEl = document.getElementById('sd-duration');
  const curDur = parseInt(durEl?.value, 10);
  if (curDur !== 15) {
    const ok = await appConfirm({
      title: '⚠ Не та длительность Seedance',
      message:
        `Длительность Seedance стоит ${curDur || '?'}с, но Auto-mode калиброван под 15с.\n\n` +
        `Поставить 15с автоматически и продолжить?`,
      okText: 'Поставить 15с и запустить', cancelText: 'Отмена', okStyle: 'accent',
    });
    if (!ok) { clog('WARN', 'auto.parallel_bail', { reason: 'duration_cancelled', ep: num }); return; }
    if (durEl) { durEl.value = '15'; durEl.dispatchEvent(new Event('change', { bubbles: true })); }
  }
  // Params from the Seedance panel — same controls the primary path reads.
  const opts = {
    useLastframe: true,         // sequential continuity always on
    useCutframes: true,
    useStyle: !!document.getElementById('sd-use-style')?.checked,
    styleVal: (document.getElementById('sd-style')?.value || '').trim(),
    baseOnly: !!document.getElementById('sd-base-only')?.checked,
    closeUpOnly: !!document.getElementById('sd-close-up-only')?.checked,
    duration: parseInt(document.getElementById('sd-duration')?.value, 10) || 15,
    resolution: document.getElementById('sd-resolution')?.value || '720p',
    moderation_bypass: document.getElementById('sd-mod-bypass')?.value || 'off',
    model_tier: document.getElementById('sd-model-tier')?.value || 'reference-fast',
    errorMode: localStorage.getItem('auto_error_mode') || 'heal',
    enableMusic: S.series?.settings?.enable_music !== false,
    maxParallelScenes: 2,
    _manualParallel: true,
  };
  const liveCount = _autoActiveRunCount();
  if (!await appConfirm({
    title: '▶ Запустить ещё один Auto-mode параллельно?',
    message:
      `Эпизод ${num} запустится ПАРАЛЛЕЛЬНО с уже идущим(и) авторежимом(ами) (сейчас активно: ${liveCount}).\n` +
      `Движок: последовательный (сцены внутри эпизода — параллельно).\n` +
      `Уже готовые чанки и помеченные «skip» сегменты пропустятся.\n\n` +
      `Максимум одновременно: ${MAX_CONCURRENT_AUTO} эпизода. Прогресс — в виджете справа внизу (⏹ останавливает только свой эпизод).`,
    okText: '▶ Запустить', cancelText: 'Отмена', okStyle: 'accent',
  })) {
    clog('WARN', 'auto.parallel_bail', { reason: 'confirm_cancelled', ep: num });
    return;
  }

  showToast(`▶ Auto-mode эп.${num} запущен параллельно`, 4000);
  clog('INFO', 'auto.parallel_manual_start', { sid, ep: num, live: liveCount });
  let res;
  try {
    res = await _runEpisodeAutoStandalone(sid, num, opts);
  } catch (e) {
    Sounds.playError();
    showToast(`✗ Auto-mode эп.${num} упал: ${e.message || e}`, 8000);
    return;
  }
  if (res && res.ok) {
    Sounds.speak(`Episode ${num} generation finished.`);
    showToast(`✓ Auto-mode эп.${num} завершён · ${res.completed}/${res.total}`, 6000);
  } else if (res) {
    Sounds.playError();
    showToast(`⏸ Auto-mode эп.${num}: ${res.completed}/${res.total}${res.errors ? `, ошибок ${res.errors}` : ''}`, 7000);
  }
}

// ── Standalone parallel auto-run for range-gen (concurrency > 1) ──────────
// Self-contained per-episode auto-mode runner. Doesn't touch global AUTO so
// multiple episodes can run concurrently without state collisions. Mirrors
// the sequential-mode logic of startAutoMode (scene-parallel internally, up
// to 3 scene-chains per episode), but uses a fresh Run object per call and
// registers it into AUTO_RUNS for the floating widget.
//
// Inputs: sid (string), num (int), opts {
//   useLastframe, useCutframes, useStyle, styleVal, baseOnly, closeUpOnly,
//   duration, resolution, moderation_bypass, errorMode, maxParallelScenes
// }
// Returns: { ok: bool, completed: int, total: int, errors: int }
async function _runEpisodeAutoStandalone(sid, num, opts = {}) {
  const epSid = sid;
  const epNumber = num;
  // Per-run state, same shape as AUTO. Lives in AUTO_RUNS until done.
  const R = {
    active: true, parallel: false, cancelRequested: false,
    completedCount: 0, cursor: 0, total: 0, activeChains: 0,
    errorMode: opts.errorMode || 'heal',
    lastStatus: '⚙ загружаю серию...',
    segments: [],
    _epSid: epSid, _epNumber: epNumber,
  };
  _autoRegisterRun(R);

  let errorsCount = 0;

  try {
    // 1. Fetch episode JSON to get the script text.
    R.lastStatus = '⚙ загружаю серию...';
    _autoUpdateFloatingWidget();
    let ep;
    try {
      ep = await api.get(`/api/series/${epSid}/episodes/${epNumber}`);
    } catch (e) {
      clog('ERROR', 'parallel.fetch_fail', { sid: epSid, ep: epNumber, msg: (e?.message || String(e)).slice(0, 200) });
      return { ok: false, completed: 0, total: 0, errors: 1 };
    }
    const scriptText = (ep?.script || '').trim();
    if (!scriptText) {
      clog('WARN', 'parallel.no_script', { sid: epSid, ep: epNumber });
      return { ok: false, completed: 0, total: 0, errors: 1 };
    }

    // 2. Build segments from script text directly (bypass DOM-bound
    //    _autoCollectSegments). Reuses _parseScriptScenes which is pure.
    R.lastStatus = '⚙ строю сегменты...';
    _autoUpdateFloatingWidget();
    // Honor the episode's saved manual segment splits/merges if the fetched
    // episode carries them; otherwise no overrides. MUST be an array — passing
    // `{}` here used to crash the run with «(overrides || []) is not iterable».
    const _ovr = Array.isArray(ep?.segment_overrides) ? ep.segment_overrides : [];
    const scenes = _parseScriptScenes(scriptText, _ovr);
    const allSegs = [];
    scenes.forEach((sc, sIdx) => {
      for (let g = 0; g < sc.segCount; g++) {
        const lines = sc.lines.filter(l => l.segIdx === g);
        if (!lines.length) continue;
        const head = sc.heading ? sc.heading + '\n\n' : '';
        const text = head + lines.map(l => l.text).join('\n');
        const hasCloseUp = lines.some(l => _isLineCloseUp(l.text));
        const anchor = _lineAnchor(lines[0].text);
        // Same VO-aware shared helper used by sequential auto-mode and retry.
        const segmentText = lines.map(l => l.text).join('\n');
        const durationSec = _estimateChunkDurationSec(segmentText);
        allSegs.push({
          sceneIdx: sIdx, segIdx: g, text, anchor,
          has_close_up: hasCloseUp, durationSec,
          scriptOrder: allSegs.length,
        });
      }
    });
    if (!allSegs.length) {
      clog('WARN', 'parallel.no_segments', { sid: epSid, ep: epNumber, script_len: scriptText.length, scene_count: scenes.length });
      return { ok: false, completed: 0, total: 0, errors: 1 };
    }
    // Resume-safe + skip-aware filter. Drop segments whose chunk is already
    // completed on the backend (matched by script_order) so a re-launched run
    // (manual parallel on a partial episode, or range-gen after a refresh)
    // doesn't redo finished work, and honor the per-segment AUTO-skip flags
    // persisted on the episode. For a brand-new episode both sets are empty, so
    // this is a no-op — range-gen behaviour is unchanged.
    const _existingChunks = (ep && ep.seedance_chunks) || [];
    const _completedOrders = new Set(
      _existingChunks
        .filter(c => c && c.status === 'completed' && typeof c.script_order === 'number')
        .map(c => c.script_order)
    );
    const _skipKeys = new Set((ep && ep.segment_auto_skips) || []);
    const segs = allSegs.filter(s =>
      !_completedOrders.has(s.scriptOrder) &&
      !_skipKeys.has(`s${s.sceneIdx}g${s.segIdx}`)
    );
    if (!segs.length) {
      clog('INFO', 'parallel.nothing_to_do', {
        sid: epSid, ep: epNumber, total_raw: allSegs.length,
        done: _completedOrders.size, skipped: _skipKeys.size,
      });
      // Nothing left to generate counts as success, not an error — the episode
      // is already complete (or every remaining segment is skipped).
      return { ok: true, completed: 0, total: 0, errors: 0 };
    }
    R.segments = segs;
    R.total = segs.length;
    clog('INFO', 'parallel.built', {
      sid: epSid, ep: epNumber, total: segs.length, raw: allSegs.length,
      done: _completedOrders.size, skipped: _skipKeys.size, scenes: scenes.length,
    });

    // Group by scene for scene-parallel execution within the episode.
    const sceneGroups = (() => {
      const m = new Map();
      for (const s of segs) {
        if (!m.has(s.sceneIdx)) m.set(s.sceneIdx, []);
        m.get(s.sceneIdx).push(s);
      }
      return [...m.values()];
    })();

    // ── Early-fire music ────────────────────────────────────────────────────
    // We have the full scene structure with duration estimates right now —
    // before any video is generated. Fire music generation immediately so it
    // runs in parallel with video rendering (saves 30-60+ minutes of waiting).
    if (opts.enableMusic !== false) {
      const scenesPlan = sceneGroups.map(grp => ({
        sceneIdx: grp[0].sceneIdx,
        // sum of all segment durations × 0.9 headroom, in ms
        target_duration_ms: Math.round(
          grp.reduce((acc, s) => acc + (s.durationSec || 15), 0) * 0.9 * 1000
        ),
      }));
      api.post(
        `/api/series/${epSid}/episodes/${epNumber}/music/generate`,
        { scenes_plan: scenesPlan }
      ).then(r => {
        if (r?.ok) clog('INFO', 'parallel.music_early', { ep: epNumber, scenes: r.scenes?.length || 0 });
      }).catch(e => {
        clog('WARN', 'parallel.music_early_fail', { ep: epNumber, err: (e?.message || String(e)).slice(0, 200) });
      });
    }
    // ────────────────────────────────────────────────────────────────────────

    const POLL_INTERVAL_MS = 8000;
    const MAX_HEAL_RETRIES = 1;
    const MAX_PARALLEL_SCENES = opts.maxParallelScenes || 2;
    const shared = {
      useLastframe: opts.useLastframe !== false,
      useCutframes: opts.useCutframes !== false,
      useStyle: !!opts.useStyle,
      styleVal: opts.styleVal || '',
      baseOnly: !!opts.baseOnly,
      closeUpOnly: !!opts.closeUpOnly,
      duration: opts.duration || 15,
      resolution: opts.resolution || '720p',
      moderation_bypass: opts.moderation_bypass || 'off',
      model_tier: opts.model_tier || 'reference-fast',
    };

    async function pollUntilDone(chunkIdx, composeRes, segText, segDuration) {
      let healAttempts = 0;
      let curIdx = chunkIdx;
      // Hard ceiling on how long we wait for ONE chunk to terminate. Without
      // this, a chunk stuck in 'submitting' forever (server submit-thread
      // crashed silently, AVAI returned but our handler dropped the response,
      // user deleted the chunk) would spin the poll-loop indefinitely — runner
      // never returns → range-gen worker never exits → user sees endless
      // «Auto-mode крутится».
      const HARD_TIMEOUT_MS = 12 * 60 * 1000;   // 12 minutes per chunk
      const NO_CHUNK_TOLERANCE_MS = 90 * 1000;  // 90s grace if chunk disappears
      const t0 = Date.now();
      let firstMissingAt = 0;
      while (true) {
        if (R.cancelRequested) return { ok: false, error: 'cancelled' };
        if (Date.now() - t0 > HARD_TIMEOUT_MS) {
          clog('ERROR', 'parallel.poll_timeout', { sid: epSid, ep: epNumber, idx: curIdx });
          return { ok: false, error: 'poll timeout 12min' };
        }
        await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
        let polled;
        try {
          const res = await api.post(`/api/series/${epSid}/episodes/${epNumber}/seedance/poll`, {});
          polled = res.chunks || [];
          // If the user is currently viewing THIS episode, animate the chunk
          // grid live (parallel runs are watched manually, not just headless).
          if (S.seriesId === epSid && S.episode?.number === epNumber) {
            try { _sdNotifyTransitions(polled); } catch {}
            try { sdRenderList(polled); } catch {}
          }
        } catch (e) { polled = []; }
        const chunk = polled.find(c => c.idx === curIdx);
        if (!chunk) {
          if (!firstMissingAt) firstMissingAt = Date.now();
          if (Date.now() - firstMissingAt > NO_CHUNK_TOLERANCE_MS) {
            clog('ERROR', 'parallel.chunk_vanished', { sid: epSid, ep: epNumber, idx: curIdx });
            return { ok: false, error: 'chunk vanished from list' };
          }
          R.lastStatus = `… не вижу #${curIdx}`;
          _autoUpdateFloatingWidget();
          continue;
        }
        firstMissingAt = 0;
        const qcStatus2 = chunk.qc?.status || null;
        if (chunk.status === 'completed' && qcStatus2 == null) {
          R.lastStatus = `🔍 #${curIdx} QC...`;
        } else {
          R.lastStatus = chunk.status === 'processing' && chunk.progress != null
            ? `⏳ #${curIdx} ${chunk.progress}%`
            : `⏳ #${curIdx} ${chunk.status}${qcStatus2 ? ' · QC '+qcStatus2 : ''}`;
        }
        _autoUpdateFloatingWidget();
        if (chunk.status === 'completed' && qcStatus2 === 'pass') return { ok: true, chunk };
        if (chunk.status === 'completed' && qcStatus2 === 'retry_exhausted') {
          showToast(`⚠ QC сдался на чанке #${curIdx} (${(chunk.qc?.fails || []).join(', ')})`, 6000);
          return { ok: true, chunk };
        }
        if (chunk.status === 'completed' && qcStatus2 === 'fail') {
          R.lastStatus = `🔁 #${curIdx} QC retry ${chunk.qc.attempts}/3`;
          _autoUpdateFloatingWidget();
          const hint = _qcBuildPromptHint(chunk.qc.fails || []);
          try {
            const restart = await api.post(`/api/series/${epSid}/episodes/${epNumber}/seedance/start`, {
              prompt: (composeRes.prompt || chunk.prompt || '') + hint,
              chunk_text: segText,
              duration: segDuration || shared.duration,
              resolution: shared.resolution,
              moderation_bypass: shared.moderation_bypass,
              model: shared.model_tier,
              refs: (composeRes.refs || []).map(r => ({
                kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
                source: r.source, prev_idx: r.prev_idx, name: r.name,
                cut_index: r.cut_index, cut_time: r.cut_time,
              })),
            });
            if (restart?.chunk?.idx != null) curIdx = restart.chunk.idx;
            continue;
          } catch (e) {
            return { ok: false, error: 'qc-retry-failed: ' + (e?.message || e) };
          }
        }
        if (chunk.status === 'failed') {
          if (R.errorMode === 'heal' && healAttempts < MAX_HEAL_RETRIES) {
            healAttempts++;
            R.lastStatus = `🩹 лечу #${curIdx}...`;
            _autoUpdateFloatingWidget();
            try {
              const healRes = await api.post(`/api/series/${epSid}/episodes/${epNumber}/seedance/${curIdx}/heal-prompt`, {});
              const restart = await api.post(`/api/series/${epSid}/episodes/${epNumber}/seedance/start`, {
                prompt: healRes.prompt || composeRes.prompt,
                chunk_text: healRes.chunk_text || segText,
                duration: segDuration || shared.duration,
                resolution: shared.resolution,
                moderation_bypass: shared.moderation_bypass,
                model: shared.model_tier,
                refs: (composeRes.refs || []).map(r => ({
                  kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
                  source: r.source, prev_idx: r.prev_idx, name: r.name,
                  cut_index: r.cut_index, cut_time: r.cut_time,
                })),
              });
              if (restart?.chunk?.idx != null) curIdx = restart.chunk.idx;
              continue;
            } catch (e) {
              return { ok: false, error: e?.message || 'heal-failed' };
            }
          }
          return { ok: false, chunk, error: chunk.error || 'failed' };
        }
      }
    }

    async function composeAndStart(seg) {
      const segCloseUp = shared.closeUpOnly || !!seg.has_close_up;
      R.lastStatus = segCloseUp ? '⚙ compose (close-up)...' : '⚙ compose...';
      _autoUpdateFloatingWidget();
      let composeRes;
      try {
        composeRes = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/compose`,
          {
            chunk_text: seg.text,
            use_prev_lastframe: shared.useLastframe,
            use_prev_cutframes: shared.useCutframes,
            style: shared.useStyle ? shared.styleVal : '',
            base_outfits_only: shared.baseOnly,
            close_up_only: segCloseUp,
          }, { timeoutMs: 300_000 }
        );
      } catch (e) {
        clog('ERROR', 'parallel.compose_throw', {
          sid: epSid, ep: epNumber, seg_anchor: (seg.anchor || '').slice(0, 60),
          msg: (e?.message || String(e)).slice(0, 300),
        });
        throw e;
      }
      if (composeRes?.error) {
        clog('ERROR', 'parallel.compose_error', { sid: epSid, ep: epNumber, err: String(composeRes.error).slice(0, 300) });
        throw new Error('compose: ' + composeRes.error);
      }
      R.lastStatus = '▶ start...';
      _autoUpdateFloatingWidget();
      let startRes;
      try {
        startRes = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
          {
            prompt: composeRes.prompt,
            chunk_text: seg.text,
            duration: seg.durationSec || shared.duration,
            resolution: shared.resolution,
            moderation_bypass: shared.moderation_bypass,
            model: shared.model_tier,
            script_order: seg.scriptOrder,
            refs: (composeRes.refs || []).map(r => ({
              kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
              source: r.source, prev_idx: r.prev_idx, name: r.name,
              cut_index: r.cut_index, cut_time: r.cut_time,
            })),
          }, { timeoutMs: 60_000 }
        );
      } catch (e) {
        clog('ERROR', 'parallel.start_throw', {
          sid: epSid, ep: epNumber, seg_anchor: (seg.anchor || '').slice(0, 60),
          msg: (e?.message || String(e)).slice(0, 300),
        });
        throw e;
      }
      if (startRes?.error) {
        clog('ERROR', 'parallel.start_error', { sid: epSid, ep: epNumber, err: String(startRes.error).slice(0, 300) });
        throw new Error('start: ' + startRes.error);
      }
      if (startRes?.chunk?.idx == null) {
        clog('ERROR', 'parallel.start_no_idx', { sid: epSid, ep: epNumber, startRes_keys: Object.keys(startRes || {}).join(',') });
        throw new Error('start вернул пустой chunk_idx');
      }
      return { composeRes, startRes };
    }

    async function runSceneChain(sceneSegs) {
      R.activeChains++;
      _autoUpdateFloatingWidget();
      try {
        for (const seg of sceneSegs) {
          if (R.cancelRequested) return 'cancelled';
          let cs;
          try { cs = await composeAndStart(seg); }
          catch (e) { errorsCount++; return 'failed'; }
          const chunkIdx = cs.startRes?.chunk?.idx;
          if (chunkIdx == null) { errorsCount++; return 'failed'; }
          const poll = await pollUntilDone(chunkIdx, cs.composeRes, seg.text, seg.durationSec);
          if (poll.error === 'cancelled') return 'cancelled';
          if (!poll.ok) { errorsCount++; return 'failed'; }
          R.completedCount++;
          R.cursor = R.completedCount;
          _autoUpdateFloatingWidget();
        }
        return 'completed';
      } finally {
        R.activeChains--;
        _autoUpdateFloatingWidget();
      }
    }

    // Concurrency-capped runner for scene chains.
    const queue = sceneGroups.slice();
    async function worker() {
      while (queue.length) {
        if (R.cancelRequested) return;
        const item = queue.shift();
        await runSceneChain(item);
      }
    }
    const workers = [];
    for (let i = 0; i < Math.min(MAX_PARALLEL_SCENES, sceneGroups.length); i++) workers.push(worker());
    await Promise.all(workers);

    clog('INFO', 'parallel.finished', {
      sid: epSid, ep: epNumber,
      completed: R.completedCount, total: R.total, errors: errorsCount,
      cancelled: !!R.cancelRequested,
    });
    return {
      ok: !R.cancelRequested && errorsCount === 0,
      completed: R.completedCount,
      total: R.total,
      errors: errorsCount,
    };
  } catch (e) {
    // Anything that bubbled past the per-scene catches above lands here.
    // Without this branch a throw would skip finally→cleanup and leave R in
    // AUTO_RUNS spinning forever.
    clog('ERROR', 'parallel.crash', {
      sid: epSid, ep: epNumber, msg: (e?.message || String(e)).slice(0, 400),
    });
    return { ok: false, completed: R.completedCount, total: R.total, errors: errorsCount + 1 };
  } finally {
    R.active = false;
    _autoUnregisterRun(R);
  }
}

function _stopAllAutoRuns() {
  if (AUTO.active) AUTO.cancelRequested = true;
  for (const r of AUTO_RUNS.values()) r.cancelRequested = true;
}

// ── Range-generation queue ─────────────────────────────────────────────────
//
// Picks episodes [from..to] in series-view, runs each through Auto-mode
// sequentially (one finishes → next starts), optionally auto-assembles
// completed chunks into final mp4 in OUT/ when each episode wraps.
//
// Lifecycle:
//   user fills inputs → startRangeGen() validates + builds queue
//   → for each ep: navigate('episode'), wait for load, suppress confirms,
//     await startAutoMode → poll AUTO.active = false → optional auto-assemble
//   → next; finishes when queue empty or stopRangeGen() pressed.
const RANGE = {
  active: false,
  queue: [],            // array of episode numbers, in order
  curIdx: -1,
  cancelRequested: false,
  autoAssemble: true,
  seriesId: null,
};

function _rangeGenStatusEl() { return document.getElementById('range-gen-status'); }
function _rangeGenStartBtn() { return document.getElementById('range-gen-start-btn'); }
function _rangeGenStopBtn()  { return document.getElementById('range-gen-stop-btn'); }
function _rangeGenSetStatus(html) {
  const el = _rangeGenStatusEl();
  if (el) el.innerHTML = html;
}
function _rangeGenUI() {
  const sb = _rangeGenStartBtn(), tb = _rangeGenStopBtn();
  if (sb) sb.style.display = RANGE.active ? 'none' : '';
  if (tb) tb.style.display = RANGE.active ? '' : 'none';
}
function stopRangeGen() {
  if (!RANGE.active) return;
  RANGE.cancelRequested = true;
  _rangeGenSetStatus('⏸ Останавливаю после текущей серии…');
  // Also cascade-stop the in-flight Auto-mode so we don't keep pumping segments.
  try { if (typeof stopAutoMode === 'function' && AUTO?.active) stopAutoMode(); } catch {}
}

async function startRangeGen() {
  if (RANGE.active) { showToast('Очередь уже идёт'); return; }
  if (!S.series) { showToast('Открой сериал'); return; }
  const aaEl = document.getElementById('range-gen-auto-assemble');
  const concEl = document.getElementById('range-gen-concurrency');
  const concurrency = Math.max(1, Math.min(3, parseInt(concEl?.value, 10) || 1));
  const eps = (S.episodes || S.series.episodes || []).slice().sort((a,b) => (a.number||0) - (b.number||0));
  if (!eps.length) { showToast('Нет эпизодов'); return; }
  if (!S._genSelected) _restoreGenSelection();
  // Build the queue from the user's checkbox selection. Filter to only those
  // that are actually ready (in case selection got stale — e.g. assets were
  // deleted after the user ticked the checkbox) and aren't already done /
  // generating right now.
  let queue = [];
  for (const num of [...S._genSelected].sort((a, b) => a - b)) {
    const ep = eps.find(e => e.number === num);
    if (!ep) continue;
    const info = _episodeReadyInfo(ep);
    const gs = ep.gen_status || '';
    if (info.ready && gs !== 'done' && gs !== 'generating') {
      queue.push(num);
    }
  }
  if (!queue.length) {
    showToast('⚠ Не выделено ни одной серии готовой к генерации', 6000);
    return;
  }
  const concWord = concurrency > 1 ? `параллельно по ${concurrency}` : 'последовательно';
  if (!await appConfirm({
    title: '▶ Пакетная генерация',
    message:
      `Серий в очереди: ${queue.length} (${queue.join(', ')})\n` +
      `Режим: ${concWord}\n` +
      `Авто-сборка финала: ${aaEl?.checked ? 'да' : 'нет'}\n\n` +
      (concurrency > 1
        ? `Серии будут стартовать одновременно (до ${concurrency}). Внутри каждой ` +
          `серии сцены тоже параллельные. Видеть прогресс — в виджете в правом нижнем углу.`
        : `Каждая серия по очереди прогонится через Auto-mode (Sequential).`) +
      `\nОстановить — кнопка «Остановить»: текущие серии добегут, дальше очередь встанет.`,
    okText: '▶ Запустить очередь',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;

  RANGE.active = true;
  RANGE.queue = queue;
  RANGE.curIdx = -1;
  RANGE.cancelRequested = false;
  RANGE.autoAssemble = !!aaEl?.checked;
  RANGE.seriesId = S.seriesId;
  RANGE.concurrency = concurrency;
  _rangeGenUI();
  _rangeGenSetStatus(`▶ В очереди: ${queue.length}${concurrency > 1 ? ` · параллельно ${concurrency}` : ''}`);
  _autoUpdateFloatingWidget();

  // Suppress modal confirms / missing-asset warnings inside the loop. We restore
  // the original `confirm` after finishing or on error.
  const origConfirm = window.confirm;
  const origAppConfirm = window.appConfirm;
  window.confirm = () => true;
  // Also short-circuit appConfirm() so the in-app modals from inner Auto-mode
  // calls don't block the queue. Only the outer queue-start confirm above
  // actually shows a dialog.
  window.appConfirm = () => Promise.resolve(true);

  // Track how many episodes have been claimed from the queue (next-up index).
  // Cursor is shared across workers; each worker grabs the next number atomic.
  let claimed = 0;
  let finished = 0;

  // Per-episode runner. Either uses the navigation+startAutoMode path
  // (concurrency=1, preserves visible episode-page UI) or the headless
  // standalone runner (concurrency>1, runs without changing S.episode).
  async function _rangeRunEpisode(epNum, idxInQueue) {
    _rangeGenSetStatus(`▶ ${idxInQueue + 1}/${queue.length} · серия ${epNum} · старт…`);

    // Mark as generating in series state so badges update.
    try {
      await api.put(`/api/series/${RANGE.seriesId}/episodes/${epNum}`, { gen_status: 'generating' });
      const ep = (S.series.episodes || []).find(e => e.number === epNum);
      if (ep) ep.gen_status = 'generating';
      if (S.episode?.number === epNum) S.episode.gen_status = 'generating';
      if (typeof renderEpisodesList === 'function') renderEpisodesList();
    } catch {}

    // ── INSTANT MUSIC KICK-OFF ───────────────────────────────────────────────
    // Fire music generation RIGHT NOW — before navigate, before batch-compose,
    // before a single video chunk is submitted. We parse the episode script
    // client-side (same logic as _runEpisodeAutoStandalone) to get scene groups
    // + duration estimates, then POST music/generate with scenes_plan.
    // The backend generates music in parallel with the entire video pipeline.
    const _musicEnabled = (S.series?.id === RANGE.seriesId)
      ? (S.series?.settings?.enable_music !== false)
      : true;
    if (_musicEnabled) {
      (async () => {
        try {
          // Fetch episode script (may already be in memory if same episode).
          let epScript = '';
          let epOverrides = [];   // MUST stay an array — _parseScriptScenes throws on {}
          if (S.episode?.number === epNum && S.seriesId === RANGE.seriesId) {
            epScript = document.getElementById('ep-script')?.value?.trim() || '';
            epOverrides = _segmentOverrides();
          }
          if (!epScript) {
            const epJson = await api.get(
              `/api/series/${RANGE.seriesId}/episodes/${epNum}`
            );
            epScript = (epJson?.script || '').trim();
            if (Array.isArray(epJson?.segment_overrides)) epOverrides = epJson.segment_overrides;
          }
          if (!epScript) return;

          // Parse script into scene groups (same helper as standalone runner).
          const scenes = _parseScriptScenes(epScript, epOverrides);
          const planMap = new Map();
          scenes.forEach((sc, sIdx) => {
            for (let g = 0; g < sc.segCount; g++) {
              const lines = sc.lines.filter(l => l.segIdx === g);
              if (!lines.length) continue;
              const segText = lines.map(l => l.text).join('\n');
              const dur = _estimateChunkDurationSec(segText);
              planMap.set(sIdx, (planMap.get(sIdx) || 0) + dur);
            }
          });
          if (!planMap.size) return;

          const scenesPlan = [...planMap.entries()].map(([sceneIdx, totalSec]) => ({
            sceneIdx,
            target_duration_ms: Math.round(totalSec * 0.9 * 1000),
          }));

          const r = await api.post(
            `/api/series/${RANGE.seriesId}/episodes/${epNum}/music/generate`,
            { scenes_plan: scenesPlan }
          );
          if (r?.ok && r.scenes?.length) {
            showToast(`🎵 Музыка запущена — эпизод ${epNum} · ${r.scenes.length} сцен`, 3000);
          }
        } catch (e) {
          // Non-fatal — video gen continues regardless.
          clog('WARN', 'range.music_kickoff_fail', {
            ep: epNum, err: (e?.message || String(e)).slice(0, 200),
          });
        }
      })();
    }
    // ────────────────────────────────────────────────────────────────────────

    let result = { ok: false, completed: 0, total: 0, errors: 0 };
    let assembleNote = '';
    let finalStatus = 'failed';   // default — if anything below throws, we still flip away from 'generating'

    try {
      if (concurrency === 1) {
        // Legacy path: navigate to the episode + use startAutoMode (so the user
        // sees the visible episode page with chunks ticking in).
        try {
          navigate('episode', { seriesId: RANGE.seriesId, episodeNum: epNum });
        } catch (e) { console.warn('[range-gen] navigate failed', e); }
        const navStart = Date.now();
        while (Date.now() - navStart < 30000) {
          if (S.seriesId === RANGE.seriesId && S.episode?.number === epNum) break;
          await new Promise(r => setTimeout(r, 250));
        }
        if (S.episode?.number !== epNum) {
          _rangeGenSetStatus(`⚠ серия ${epNum} не открылась — пропуск`);
          clog('WARN', 'range.ep_nav_fail', { sid: RANGE.seriesId, ep: epNum });
          result = { ok: false, completed: 0, total: 0, errors: 1 };
        } else {
          try { await startAutoMode(); }
          catch (e) {
            clog('ERROR', 'range.ep_startAuto_throw', { sid: RANGE.seriesId, ep: epNum, msg: (e?.message || String(e)).slice(0, 300) });
          }
          while (AUTO?.active) {
            if (RANGE.cancelRequested) { try { stopAutoMode(); } catch {} }
            await new Promise(r => setTimeout(r, 2000));
          }
          result = {
            ok: !RANGE.cancelRequested && (AUTO?.completedCount || 0) >= (AUTO?.total || 0) && (AUTO?.total || 0) > 0,
            completed: AUTO?.completedCount || 0,
            total: AUTO?.total || 0,
            errors: 0,
          };
        }
      } else {
        // Parallel path: standalone runner, no navigation. Multiple of these
        // can run concurrently because each has its own Run state.
        result = await _runEpisodeAutoStandalone(RANGE.seriesId, epNum, {
          useLastframe: true, useCutframes: true,
          useStyle: false, styleVal: '',
          baseOnly: false, closeUpOnly: false,
          duration: 15, resolution: '480p',
          moderation_bypass: document.getElementById('sd-mod-bypass')?.value || 'off',
          model_tier: document.getElementById('sd-model-tier')?.value || 'reference-fast',
          errorMode: 'heal',
          maxParallelScenes: 2,
          // Pass enable_music so music fires immediately after script segmentation,
          // in parallel with video generation (not waiting for videos to finish).
          enableMusic: (S.series?.id === RANGE.seriesId)
            ? (S.series?.settings?.enable_music !== false)
            : true,
        });
      }

      // Log near-instant returns — they're almost always a real bug (segment
      // count zero, episode JSON fetch failure, etc.) and we want to see
      // which one it was without DevTools.
      if (result.total === 0 && result.completed === 0) {
        clog('WARN', 'range.ep_empty_result', {
          sid: RANGE.seriesId, ep: epNum,
          errors: result.errors || 0, ok: !!result.ok,
        });
      }

      const completedAll = result.ok;
      finalStatus = completedAll ? 'done' : (RANGE.cancelRequested ? 'queued' : 'failed');

      if (RANGE.autoAssemble && completedAll && !RANGE.cancelRequested) {
        try {
          const r = await api.post(
            `/api/series/${RANGE.seriesId}/episodes/${epNum}/auto-assemble`,
            { require_all: true, expected_segments: result.total },
          );
          if (r && r.ok) assembleNote = ` · 🎬 ${r.filename} (${r.size_mb}MB)`;
          else if (r?.error) assembleNote = ` · ⚠ авто-сборка: ${r.error}`;
        } catch (e) {
          assembleNote = ` · ⚠ авто-сборка упала: ${e.message || e}`;
        }
      }

      // Music generation is INDEPENDENT of auto-assemble: it can fire even
      // when the user turned auto-assemble off. Gate is purely the per-series
      // enable_music flag + a successful run.
      if (completedAll && !RANGE.cancelRequested) {
        const musicEnabled = (S.series?.id === RANGE.seriesId)
          ? (S.series?.settings?.enable_music !== false)
          : true;
        if (musicEnabled) {
          try {
            const r = await api.post(
              `/api/series/${RANGE.seriesId}/episodes/${epNum}/music/generate`,
              {}
            );
            if (r?.ok) assembleNote += ` · 🎵 музыка запущена (${r.scenes?.length || 0} сцен)`;
            else if (r?.error) assembleNote += ` · 🎵 ⚠ ${r.error}`;
          } catch (e) {
            assembleNote += ` · 🎵 ⚠ ${e.message || e}`;
          }
        }
      }
    } catch (e) {
      // Anything thrown mid-run: log it and fall through to the finally that
      // still flips gen_status away from 'generating'. Without this guard,
      // a thrown exception would skip the status-update PUT below and leave
      // the episode permanently stuck.
      clog('ERROR', 'range.ep_run_throw', {
        sid: RANGE.seriesId, ep: epNum,
        msg: (e?.message || String(e)).slice(0, 400),
      });
      console.error('[range-gen] _rangeRunEpisode body threw', epNum, e);
      finalStatus = 'failed';
    } finally {
      // ALWAYS flip away from 'generating'. Use a fire-and-forget retry on
      // failure so a transient network blip doesn't leave the episode stuck.
      const tryUpdate = async () => {
        try {
          await api.put(`/api/series/${RANGE.seriesId}/episodes/${epNum}`, { gen_status: finalStatus });
          if (S.episode?.number === epNum) S.episode.gen_status = finalStatus;
          const ep = (S.series.episodes || []).find(e => e.number === epNum);
          if (ep) ep.gen_status = finalStatus;
          return true;
        } catch (e) {
          return false;
        }
      };
      const ok1 = await tryUpdate();
      if (!ok1) {
        // One retry after 1.5s — covers a transient 5xx during heavy load.
        await new Promise(r => setTimeout(r, 1500));
        await tryUpdate();
      }
      _rangeGenSetStatus(`✓ ${idxInQueue + 1}/${queue.length} · серия ${epNum}${assembleNote || (finalStatus === 'failed' ? ' · ⚠ упало' : '')}`);
    }
    return result;
  }

  try {
    // Worker pool: N workers each pull the next un-claimed episode off the
    // queue, run it, then loop. Total throughput = min(N, queue.length).
    async function worker() {
      while (!RANGE.cancelRequested && claimed < queue.length) {
        const idx = claimed++;
        const epNum = queue[idx];
        RANGE.curIdx = Math.max(RANGE.curIdx, idx);
        try { await _rangeRunEpisode(epNum, idx); }
        catch (e) { console.warn('[range-gen] episode crashed', epNum, e); }
        finished++;
      }
    }
    // Stagger worker startup so 3 concurrent compose calls don't fire in
    // the same JS turn. Anthropic API throttles bursts pretty aggressively
    // even with backoff retry — staggering 2s apart smooths the load. With
    // concurrency=3 the third worker starts at +4s, by which time the
    // first one's compose is already mid-flight.
    const STAGGER_MS = 2000;
    const workers = [];
    const workerCount = Math.min(concurrency, queue.length);
    for (let i = 0; i < workerCount; i++) {
      const delay = i * STAGGER_MS;
      workers.push((async () => {
        if (delay > 0) await new Promise(r => setTimeout(r, delay));
        await worker();
      })());
    }
    await Promise.all(workers);

    // Safety sweep: scan every episode in the queue and force-reset any still
    // sitting at gen_status='generating'. Belt-and-suspenders for cases where
    // _rangeRunEpisode's own finally-block managed to throw before its PUT.
    try {
      const fresh = await api.get(`/api/series/${RANGE.seriesId}/episodes`);
      for (const num of queue) {
        const ep = fresh.find(e => e.number === num);
        if (ep && ep.gen_status === 'generating') {
          clog('WARN', 'range.stuck_status_swept', { sid: RANGE.seriesId, ep: num });
          try {
            await api.put(`/api/series/${RANGE.seriesId}/episodes/${num}`, { gen_status: 'failed' });
          } catch {}
        }
      }
    } catch {}
  } finally {
    window.confirm = origConfirm;
    window.appConfirm = origAppConfirm;
    RANGE.active = false;
    _rangeGenUI();
    if (RANGE.cancelRequested) {
      _rangeGenSetStatus(`⏸ Остановлено · обработано ${RANGE.curIdx + 1}/${RANGE.queue.length}`);
      showToast('⏸ Очередь остановлена', 5000);
    } else {
      _rangeGenSetStatus(`✓ Очередь завершена · ${RANGE.queue.length} серий`);
      showToast(`✓ Очередь завершена · ${RANGE.queue.length} серий`, 6000);
      try { Sounds.speak('Range generation finished.'); } catch {}
    }
    // Refresh series view so badges show final state.
    try {
      if (S.seriesId === RANGE.seriesId && document.getElementById('view-series') && !document.getElementById('view-series').classList.contains('hidden')) {
        S.series = await api.get(`/api/series/${RANGE.seriesId}`);
        if (typeof renderEpisodesList === 'function') renderEpisodesList();
      }
    } catch {}
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

// Convergence-mode: regenerate synopsis from bridge beat + script for every episode
// from the first unwritten one through to the chosen landmark (finale | nearest checkpoint).
async function generateToLandmark(landmarkType) {
  if (!S.seriesId) return;
  const s = S.series || {};
  // Resolve target episode locally for confirm dialog
  let targetEp = null;
  if (landmarkType === 'finale') {
    const fin = s.finale;
    if (!fin || !(fin.description || '').trim()) {
      alert('Финал не прикреплён к сериалу. Открой «🏁 Финал» и опиши финальную серию.');
      return;
    }
    targetEp = fin.episode;
  } else if (landmarkType === 'checkpoint') {
    const cps = (s.checkpoints || []).filter(c => (c.description || '').trim());
    if (!cps.length) {
      alert('Нет контрольных точек. Создай хотя бы одну через кнопку «🎯 Контрольная точка».');
      return;
    }
    const currentEp = S.episodeNum || 1;
    const upcoming = cps.filter(c => c.episode >= currentEp).sort((a, b) => a.episode - b.episode);
    if (!upcoming.length) {
      alert('Все контрольные точки уже позади текущей серии.');
      return;
    }
    targetEp = upcoming[0].episode;
  } else {
    return;
  }

  const currentEp = S.episodeNum || 1;
  const span = targetEp - currentEp + 1;
  if (span < 1) {
    alert('Целевая серия раньше текущей.');
    return;
  }
  if (span > 8) {
    alert(`Слишком большой диапазон: ${span} серий. Максимум 8 за раз. Сгенерь сначала промежуточные.`);
    return;
  }
  const label = landmarkType === 'finale' ? 'финала' : 'контрольной точки';
  if (!confirm(
    `Запустить генерацию ${span} серии(й) — Ep ${currentEp}…${targetEp} — до ${label}?\n\n` +
    `Для каждой серии:\n` +
    `  1) Синопсис будет ПЕРЕЗАПИСАН из bridge-плана (старая версия уйдёт в history).\n` +
    `  2) Сценарий будет сгенерирован с максимальным весом конвергенции.\n\n` +
    `Это займёт несколько минут.`
  )) return;

  const ftBtn = document.getElementById('ep-gen-to-finale-btn');
  const cpBtn = document.getElementById('ep-gen-to-checkpoint-btn');
  const mainBtn = document.getElementById('ep-gen-script-btn');
  [ftBtn, cpBtn, mainBtn].forEach(b => { if (b) b.disabled = true; });
  const status = document.getElementById('ep-script-gen-status');
  if (status) {
    status.textContent = `🎯 Генерируем ${span} серий до ${label}…`;
    status.style.color = 'var(--accent)';
  }
  try {
    const taskCtx = { seriesId: S.seriesId, episodeNum: currentEp, seriesTitle: S.series?.title };
    const res = await trackTask(`До ${label} (Ep ${currentEp}…${targetEp})`, taskCtx, () =>
      api.post(`/api/series/${S.seriesId}/generate-to-landmark`, {
        landmark_type: landmarkType,
        landmark_episode: landmarkType === 'checkpoint' ? targetEp : undefined,
        model: _selectedWriterModel('writer-model-ep'),
      })
    );
    const ok = res.completed || 0;
    const total = res.span || span;
    const errLine = res.error ? `\n\nПерви́ ошибка: ${res.error}` : '';
    showToast(`🎯 Готово: ${ok} из ${total} серий сгенерировано${errLine}`);
    if (status) {
      status.textContent = `🎯 ${ok}/${total} серий до ${label} готово.`;
      status.style.color = ok === total ? 'var(--success)' : 'var(--warning)';
    }
    // Refresh current episode view + episode list
    if (typeof loadSeries === 'function') await loadSeries(S.seriesId);
    if (typeof renderEpisodesList === 'function') renderEpisodesList();
    // Re-load current episode to pick up the new synopsis + script
    if (S.episodeNum) {
      try {
        const ep = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
        S.episode = ep;
        setVal('ep-synopsis', ep.synopsis || '');
        setVal('ep-script', ep.script || '');
      } catch (e) { /* ignore */ }
    }
  } catch (e) {
    showToast('Ошибка: ' + (e?.message || e));
    if (status) {
      status.textContent = '❌ Ошибка: ' + (e?.message || e);
      status.style.color = 'var(--danger)';
    }
  } finally {
    [ftBtn, cpBtn, mainBtn].forEach(b => { if (b) b.disabled = false; });
  }
}

// Show/hide the "До чекпоинта / финала" buttons based on whether the current series
// has those landmarks pinned ahead of the current episode.
function refreshLandmarkButtonsVisibility() {
  const ftBtn = document.getElementById('ep-gen-to-finale-btn');
  const cpBtn = document.getElementById('ep-gen-to-checkpoint-btn');
  const s = S.series || {};
  const currentEp = S.episodeNum || 1;
  // Finale
  if (ftBtn) {
    const fin = s.finale;
    const hasFinAhead = fin && (fin.description || '').trim() && fin.episode >= currentEp;
    if (hasFinAhead) {
      ftBtn.style.display = '';
      ftBtn.textContent = `🏁 До финала (Ep ${fin.episode})`;
    } else {
      ftBtn.style.display = 'none';
    }
  }
  // Nearest checkpoint
  if (cpBtn) {
    const cps = (s.checkpoints || []).filter(c => (c.description || '').trim() && c.episode >= currentEp);
    if (cps.length) {
      const nearest = cps.sort((a, b) => a.episode - b.episode)[0];
      cpBtn.style.display = '';
      cpBtn.textContent = `📍 До чекпоинта (Ep ${nearest.episode})`;
    } else {
      cpBtn.style.display = 'none';
    }
  }
}

async function generateEpisodeScript() {
  const btn = document.getElementById('ep-gen-script-btn');
  const status = document.getElementById('ep-script-gen-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  status.textContent = 'Генерируем сценарий...';
  status.style.color = 'var(--warning)';
  const postgenEl = document.getElementById('ep-postgen-checks');
  if (postgenEl) postgenEl.innerHTML = '';
  const taskCtx = { seriesId: S.seriesId, episodeNum: S.episodeNum, seriesTitle: S.series?.title };
  try {
    await saveEpisodeSilent();
    const res = await trackTask('Сценарий эпизода', taskCtx, () =>
      api.post(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/generate-script`, {
        model: _selectedWriterModel('writer-model-ep'),
      })
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
    // Auto-run post-gen checks: surface logic audit violations + phrase scan
    _epPostGenChecks(res.script, res.audit_report);
    // Notify about auto-created outfits from SCENE_OPEN
    _notifyNewOutfits(res._new_outfits);
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

