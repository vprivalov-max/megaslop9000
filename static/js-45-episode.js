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
// Compute generation state for the neighbours-pill dot:
//   'empty'       — no Seedance chunks yet (грей точка)
//   'in-progress' — at least one chunk is pending/processing/submitting (пульсирующая фиолетовая)
//   'failed'      — has failed chunks and nothing in-flight (красная)
//   'partial'     — есть completed но не все/не собрано (амбер)
//   'done'        — assembled_path OR gen_status==='done' (зелёная)
function _episodeGenState(ep) {
  if (!ep) return 'empty';
  if (ep.gen_status === 'done' || ep.assembled_path) return 'done';
  const chunks = ep.seedance_chunks || [];
  if (!chunks.length) return 'empty';
  const inFlight = chunks.some(c => ['pending', 'processing', 'submitting'].includes(c.status));
  if (inFlight || ep.gen_status === 'generating') return 'in-progress';
  const anyCompleted = chunks.some(c => c.status === 'completed');
  const anyFailed = chunks.some(c => c.status === 'failed');
  if (anyFailed && !anyCompleted) return 'failed';
  if (anyCompleted && anyFailed) return 'partial';
  if (anyCompleted) return 'partial';   // нет assembled_path → не «done»
  return 'empty';
}

const _GEN_STATE_DOT = {
  'empty':       { color: 'rgba(160,160,170,0.45)', label: 'нет генераций' },
  'in-progress': { color: '#a78bfa',                 label: 'идёт генерация',     pulse: true },
  'failed':      { color: '#f87171',                 label: 'есть упавшие чанки' },
  'partial':     { color: '#fbbf24',                 label: 'не все чанки готовы' },
  'done':        { color: '#4ade80',                 label: 'полностью сгенерирована' },
};

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
    const state = _episodeGenState(ep);
    const dotMeta = _GEN_STATE_DOT[state];
    const dotHtml = `<span class="epn-dot epn-dot-${state}" style="background:${dotMeta.color}"></span>`;
    const titleParts = [ep.title || '', `${dotMeta.label}`].filter(Boolean);
    const title = esc(titleParts.join(' · '));
    parts.push(`<a class="epn-pill ${isCur ? 'current' : ''}" ${isCur ? '' : `onclick="navigate('episode',{seriesId:'${S.seriesId}',episodeNum:${ep.number}})"`} title="${title}">${dotHtml}${label}</a>`);
  }
  if (end < eps.length - 1) parts.push(`<span class="epn-arrow" title="Есть ещё эпизоды после этих">…</span>`);
  nav.innerHTML = parts.join('');
}

// Background refresh of the neighbours-pill state. While AUTO is running the
// chunk statuses on disk change every few seconds — re-pull S.episodes and
// re-render so the dots reflect current state without forcing a navigation.
let _epnRefreshTimer = null;
function _epnEnsureRefresh() {
  if (_epnRefreshTimer) return;
  _epnRefreshTimer = setInterval(async () => {
    const onEpView = document.getElementById('view-episode') &&
                     !document.getElementById('view-episode').classList.contains('hidden');
    if (!onEpView || !S.seriesId) return;
    // Only fetch when something is plausibly happening: AUTO running OR
    // range-gen running OR any episode has in-flight chunks at last snapshot.
    const anyHot = (typeof AUTO !== 'undefined' && AUTO.active) ||
                   (typeof RANGE !== 'undefined' && RANGE.active) ||
                   (S.episodes || []).some(e => (e.seedance_chunks || []).some(c =>
                     ['pending', 'processing', 'submitting'].includes(c.status)));
    if (!anyHot) return;
    try {
      S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
      renderEpisodeNeighbours();
    } catch {}
  }, 12000);
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
               { episode_number: S.episode.number, model: _selectedWriterModel('writer-model-ep') })
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
      api.post(`/api/series/${S.seriesId}/generate-next-episode-synopsis`, { episode_number: epNum, model: _selectedWriterModel('writer-model-ep') })
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
  // Sync the per-episode writer-model chip to whatever the series saved as
  // its default. User can still override per-click before they hit «Сгенерировать».
  _renderWriterModelChip('writer-model-ep', S.series?.writer_model);
  renderEpisodeNeighbours();
  _epnEnsureRefresh();
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
  // cast_extracted may be missing on legacy episodes; treat ANY episode with
  // a script + at least one used entity as already-accepted (covers imports
  // that pre-date the cast_extracted=True write in _import_worker).
  const inferAccepted = !!(S.episode.cast_extracted) ||
    !!((S.episode.script || '').trim() &&
       ((S.episode.characters_used || []).length +
        (S.episode.locations_used || []).length +
        (S.episode.items_used || []).length) > 0);
  if (inferAccepted && (S.episode.script || '').trim()) {
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
  updateApplyRevisionsBtn();
}

// Show the «Применить правки к серии» button only for cloned-with-edits series.
// Pending episodes (plot/age changes) get a 🔴 marker; pure name/look edits
// don't queue, so the button stays available but un-marked (optional rewrite).
function updateApplyRevisionsBtn() {
  const btn = document.getElementById('ep-apply-revisions-btn');
  if (!btn) return;
  const ri = (S.series && (S.series.revision_instructions || '').trim()) || '';
  const hasScript = !!(val('ep-script'));
  if (!ri || !hasScript) { btn.style.display = 'none'; return; }
  btn.style.display = '';
  const pending = (S.series.revision_plan && S.series.revision_plan.pending_episodes) || [];
  const isPending = pending.map(Number).includes(Number(S.episodeNum));
  btn.textContent = isPending ? '🔴 Переписать серию под правки' : '🧬 Применить правки к серии';
}

async function applyEpisodeRevisions() {
  if (!S.seriesId || !S.episodeNum) { alert('Сначала открой эпизод'); return; }
  if (!confirm(`Переписать сценарий серии ${S.episodeNum} под правки сериала?\n\nХук, структура, клиффхэнгер и длина сохранятся — изменится только то, что требуют правки и логика мира. Текущий текст будет заменён.`)) return;
  const btn = document.getElementById('ep-apply-revisions-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Переписываю…'; }
  try {
    const r = await api.post(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/apply-revisions`, {});
    if (r && r.script != null) {
      setVal('ep-script', r.script);
      if (S.episode) { S.episode.script = r.script; S.episode.cast_extracted = false; }
      // Keep local series copy's pending list in sync so the marker clears.
      if (S.series && S.series.revision_plan) S.series.revision_plan.pending_episodes = r.pending_episodes || [];
      updateGenScriptBtn();
      showToast('🧬 Сценарий серии переписан под правки. Проверь и «Прими сценарий», чтобы пере-извлечь персонажей.');
    } else {
      showToast('Готово, но ответ без сценария — обнови страницу.');
    }
  } catch (e) {
    showToast('Ошибка: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; updateApplyRevisionsBtn(); }
  }
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
// — but the line still clearly opens a new scene. Sub-patterns:
//   • "Локация: ..." or "LOCATION: ..." context preamble
//   • Numbered: "СЦЕНА 5", "Сцена 5.", "SCENE 12"
//   • Time-coded beat: "0:00—0:05 — Hook" / "0:05–0:15 — Arrival" / "1:30 —
//     On the way" — common in short-drama / vertical TikTok formats where
//     the writer marks beats by timestamp instead of slug. Each beat is
//     typically a new shot/location, so treat as scene break.
const SCENE_HEADING_INFER_RE = /^[\s*_#>]*(Локация\s*[:：]|Location\s*[:：]|СЦЕНА\s*\d|Сцена\s*\d|SCENE\s*\d|\d{1,2}:\d{2}\s*[—–\-])/i;

// Control / structural tokens that LOOK slug-ish but aren't scene starts.
const _SLUG_BLOCKLIST_RE = /^(REVERSAL|END|FIN|КОНЕЦ|TBD|TBC|БИТ|BIT|HOOK|TWIST|CLIFFHANGER|КЛИФФХЭНГЕР|РАЗВОРОТ|ПАУЗА|ТИШИНА|FLASHBACK|FLASH BACK|MONTAGE|МОНТАЖ|VOICE OVER|V\.O\.|O\.S\.)$/i;

// Standalone ALL-CAPS slug like "ДОМ АННЫ — НОЧЬ" or "OFFICE — DAY".
// Must look like a place/time tag: 5..80 chars, no lowercase letters, no colon.
// CRITICAL: bare single-word ALL-CAPS lines (SOPHIE, ISABELLE, MIA, etc.) are
// speaker labels in standard screenplay format — NOT scene slugs. Real scene
// slugs almost always have at least a dash (location + time-of-day) or 2+
// words. Single token = name cue. This guard fixes the common bug where
// every speaker label opened a new "scene".
function _isAllCapsSlug(t) {
  if (!t) return false;
  if (t.length < 5 || t.length > 80) return false;
  if (/[:：\[\]#]/.test(t)) return false;       // character cues, dialogue, brackets, markdown #
  if (/[a-zа-яё]/.test(t)) return false;        // any lowercase → not a slug
  if (!/[A-ZА-ЯЁ]/.test(t)) return false;       // need at least one letter
  if (/^(FADE|CUT|DISSOLVE|SMASH|MATCH)\b/i.test(t)) return false;
  if (_SLUG_BLOCKLIST_RE.test(t.replace(/[\.\—\-\s]+$/, ''))) return false;
  // Need either a dash (—/–/-) OR 2+ space-separated tokens. Otherwise
  // it's most likely a speaker cue like "SOPHIE" / "ISABELLE".
  const hasDash = /[—–\-]/.test(t);
  const tokens = t.split(/\s+/).filter(Boolean);
  if (!hasDash && tokens.length < 2) return false;
  // Even with 2 tokens, both can be a name+lastname («JANE DOE»). Require
  // at least one «time-of-day» / «scene-context» keyword OR a dash to avoid
  // false-positives for char cues with surnames.
  const hasContext = /\b(DAY|NIGHT|MORNING|EVENING|DAWN|DUSK|AFTERNOON|MIDNIGHT|ДЕНЬ|НОЧЬ|УТРО|ВЕЧЕР|ПОЛДЕНЬ|РАССВЕТ|ЗАКАТ|СУМЕРКИ|ПОЛНОЧЬ|CONTINUOUS|LATER|MOMENTS LATER|FLASHBACK|ROOM|HOUSE|OFFICE|STREET|КОМНАТА|ДОМ|ОФИС|УЛИЦА|ИНТ|ЭКСТ|КВАРТИРА|КАФЕ|САД|БАР|ПАЛАЦ|ДВОРЕЦ|БАЛЬНАЯ|СПАЛЬНЯ|КУХНЯ|ГОСТИНАЯ|КОРИДОР|ЛЕСТНИЦА|САЛОН|ХОЛЛ|КАБИНЕТ|ВАННАЯ)\b/i.test(t);
  if (!hasDash && !hasContext) return false;
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
  // Episode synopsis prefixes — these are meta-description that scriptwriters
  // (and our own /generate-script-batch) put at the top of each episode body.
  // They are NOT screen content and must not become Seedance segments.
  // Captures both «Кратко: ...» (one-liner) and any continuation lines
  // until the next script-style line — handled in the loop via a tracker
  // flag, not just regex match here.
  /^(?:кратко|синопсис|summary|brief|logline|premise|tldr)\s*[:\-—]/i,
  // Episode meta-header block — short-drama scripts often start with:
  //   Episode 1 — Welcome to Palm City
  //     o Length: ~60 seconds
  //     o Dialogue: English
  //     o Style: animated crime drama, tropical 80s city, anthropomorphic
  // These are metadata, NOT visible screen content. Without skipping they
  // turn into a phantom chunk-0 with ~5s duration that renders nothing
  // meaningful. Added 2026-05-19 after user-reported empty chunk in Vice Beasts ep 1.
  /^Episode\s+\d+\s*[—–\-:]/i,
  /^Эпизод\s+\d+\s*[—–\-:]/i,
  // Bulleted meta keys (with `o`, `○`, `•`, `-`, `*` prefix OR no prefix):
  /^\s*[o○•·]\s+(Length|Duration|Dialogue|Language|Style|Tone|Theme|Mood|Genre|Setting|Format|Logline|Pacing)\s*[:：]/i,
  /^(Length|Duration|Dialogue|Language|Style|Tone|Theme|Mood|Genre|Setting|Format|Pacing)\s*[:：]\s*\S/i,
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
const SPEECH_WPS = 2.65;  // ~159 wpm — calibrated to external pro chronometer on actual script dialogue

// Helpers shared between _lineDuration and the segment-builder loop.
// All quote/dash chars short-drama AI scripts use in practice.
const _QUOTE_OPEN_RE  = /^[\s]*["'«»“”„‟‘’‚‛‹›「『]/;
const _QUOTE_CLOSE_RE = /["'«»“”„‟‘’‚‛‹›」』]\s*[.!?…]?\s*$/;
const _DASH_DIALOGUE_RE = /^[—–]\s+\S/;

// Strip wrapping markdown (`**bold**`, `*italic*`, `__bold__`, `_italic_`)
// from the line. AI-generated screenplays often emit `**Clara:**` for cues,
// `**"Hello"**` for emphasized dialogue, or italic-wrapped parentheticals
// like `*(she winks)*` / `**MAYA** *(irritated):*`. Without stripping,
// downstream regexes that anchor on a letter all fail and the line falls
// into generic prose (wrong duration).
function _stripMarkdownWrappers(s) {
  if (!s) return s;
  // Strip a leading **…** that covers the start of the line.
  s = s.replace(/^\*\*([^*]+)\*\*/, '$1');
  s = s.replace(/^__([^_]+)__/, '$1');
  // Strip italic-wrapped parentheticals ANYWHERE in the line:
  //   *(text)*  → (text)
  //   *(text):* → (text):    (italic wraps paren AND its colon — common
  //                            in `**MAYA** *(irritated):* Oh really?`)
  s = s.replace(/\*(\([^)]+\):?)\*/g, '$1');
  s = s.replace(/_(\([^)]+\):?)_/g, '$1');
  // Strip a leading *…* (single asterisk italic) if it wraps a name+colon
  // or a name only — narrow pattern to avoid stripping mid-sentence emphasis.
  s = s.replace(/^\*([A-Za-zА-Яа-яЁё][^*]{0,60})\*/, '$1');
  s = s.replace(/^_([A-Za-zА-Яа-яЁё][^_]{0,60})_/, '$1');
  return s.trim();
}

// Count dialogue words after stripping non-spoken decorations.
function _countDialogueWords(text) {
  if (!text) return 0;
  const stripped = text
    .replace(/^[\s]*["'«»“”„‟‘’‚‛‹›「『]+/, '')        // leading quotes
    .replace(/["'«»“”„‟‘’‚‛‹›」』]+\s*[.!?…]?\s*$/, '')// trailing quotes
    .replace(/\([^)]*\)/g, ' ')                       // (parens — tone notes)
    .replace(/\[[^\]]*\]/g, ' ')                      // [brackets — stage]
    .replace(/\*[^*]*\*/g, ' ')                       // *italics — emphasis*
    .trim();
  return stripped ? stripped.split(/\s+/).filter(Boolean).length : 0;
}

function _lineDuration(line) {
  let t = (line || '').trim();
  if (!t) return 0;
  // Strip leading line-numbering prefix: "1. ", "2) ", "12: ", "3 - " — common
  // when scripts come back with enumerated dialogue/action.
  t = t.replace(/^\d+[.\):\-—–]\s+/, '');
  // Strip wrapping markdown (`**Clara:**`, `*Sofia*`) so downstream regexes
  // anchored on a letter still match.
  t = _stripMarkdownWrappers(t);
  if (!t) return 0;

  if (_matchSceneHeading(t).match) return 0;
  if (/^[-—=]{3,}\s*$/.test(t)) return 0;
  if (/^\[REVERSAL\]\s*$/i.test(t)) return 0;
  if (/^[\s—-]*(FADE|CUT|DISSOLVE|SMASH|MATCH)\s+(IN|OUT|TO|BACK)\b/i.test(t)) return 0;
  // Beat-time headings: "[0:00 — 0:15] HOOK" / "[0:15 - 0:35] BUILD" — meta
  // markers not visible as on-screen content.
  if (/^\[\s*\d+:\d+\s*[—\-–]\s*\d+:\d+\s*\]/.test(t)) return 0;

  // Action line: bracketed prose `[Волк входит и...]`. Scale by length —
  // real action takes time proportional to what's described; a one-liner
  // ≈1.5s, a long sentence ≈4-6s.
  if (/^\[/.test(t) && /\]\s*$/.test(t)) {
    const inner = t.replace(/[\[\]]/g, '').trim();
    if (!inner) return 0;
    return _scaleActionDuration(inner);
  }

  // Dialogue line: "Name: (parens) [stage] actual text" — count by speech rate.
  // Accepts BOTH all-caps (AVA:, MAYA:) and Title-case (Adrian:, Clara:) — modern
  // short-drama scripts use Title-case for character cues, classic screenplay
  // format uses all-caps. Discriminator is the colon — prose lines don't have one.
  const m = t.match(/^([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-'.#]{0,40})\s*[:：]\s*(.*)$/);
  if (m && /^[A-ZА-ЯЁ]/.test(m[1])) {
    const rest = m[2] || '';
    const words = _countDialogueWords(rest);
    if (!words) return 0.5;
    return 0.4 + words / SPEECH_WPS;
  }

  // European/Russian literary dialogue: «— Привет!» / "— How are you?"
  // Em-dash or en-dash followed by space and dialogue text. Each dash line
  // is a separate beat (speaker alternates by convention).
  if (_DASH_DIALOGUE_RE.test(t)) {
    const inner = t.replace(/^[—–]\s+/, '');
    const words = _countDialogueWords(inner);
    if (words > 0) return 0.4 + words / SPEECH_WPS;
  }

  // Novel-style fully-quoted dialogue line: «"Hello there."» / «"Привет."»
  // No explicit cue — the whole line is just the quoted utterance. Count as
  // dialogue.
  if (_QUOTE_OPEN_RE.test(t) && _QUOTE_CLOSE_RE.test(t)) {
    const words = _countDialogueWords(t);
    if (words > 0) return 0.4 + words / SPEECH_WPS;
  }

  // Novel-style dialogue + attribution: «"I love you," she said.» Line starts
  // with a quote, has a closing quote, then space + more text (attribution).
  // The comma usually sits INSIDE the closing quote («"I love you,"»), so we
  // can't anchor on a comma after — instead we just require the close-quote
  // be followed by space + any non-space char (the attribution continues).
  // We extract just the quoted utterance; the attribution is amortized by
  // the per-line 0.4s pre-pause.
  const quoteAttrMatch = t.match(/^[\s]*["'«»“”„‟‘’‚‛‹›「『](.+?)["'«»“”„‟‘’‚‛‹›」』]\s+\S/);
  if (quoteAttrMatch) {
    const words = _countDialogueWords(quoteAttrMatch[1]);
    if (words > 0) return 0.4 + words / SPEECH_WPS;
  }

  // Standalone parenthetical: prefer event detection over length-based scaling
  // so descriptive parens read as 0s and action parens get 1.5s, consistent
  // with how unwrapped prose lines are billed.
  //   (angry)                       → 0.4s  (short tone-note, no event)
  //   (she winks)                   → 1.5s  (motion verb)
  //   (Adrian laughs in surprise)   → 1.5s  (motion verb, short paren)
  //   (a hot summer afternoon...)   → 0s    (no event, pure description)
  const parenMatch = t.match(/^\((.+)\)\.?$/);
  if (parenMatch) {
    const inner = parenMatch[1].trim();
    const evDur = _scaleProseDuration(inner);
    if (evDur > 0) return evDur;
    return inner.length <= 25 ? 0.4 : 0;
  }

  // Directorial markers — «Pause.» / «Beat.» / «Silence.» / «Тишина.»
  // These are screenplay timing notes, not narrative events. Give a small
  // beat (the cut/cross-fade between actions) but don't bill as an event.
  if (/^(pause|beat|silence|тишина|пауза)[.!]?\s*$/i.test(t)) return 0.5;

  // Orphan screenplay speaker cue — standalone name with no dialogue on the
  // same line ("VIVIENNE", "ETHAN (V.O.)"). 0s; payload line carries time.
  if (_isOrphanSpeakerCue(t)) return 0;

  // Plain prose narration — describes a visual state. 0s unless it contains
  // an event/motion verb (handled in _scaleProseDuration).
  return _scaleProseDuration(t);
}

// Bracketed stage-direction duration: explicit physical actions [Wolf enters].
// These are timed sequences, so scale with complexity; max 6s.
function _scaleActionDuration(text) {
  const chars = (text || '').length;
  return Math.max(ACTION_BEAT_SEC, Math.min(6, 1 + chars / 35));
}

// Unbracketed prose lines fall into two categories:
//   • Scene/character DESCRIPTION — "A car stands on the road.",
//     "Vivienne is lying halfway under it.", "Cash lies on the pavement."
//     These describe a visual state. The camera captures it in one frame
//     regardless of description length → 0 screen-time.
//   • EVENT lines — "A raccoon walks by.", "The raccoon freezes."
//     Something actually HAPPENS on screen → ACTION_BEAT_SEC (1.5s).
//
// Distinction: does the line contain a motion / change-of-state verb?
// If yes → event → 1.5s. If no → pure description → 0s.
// Motion / change-of-state / event verbs. Presence in a prose line means
// something HAPPENS on screen (vs static scene description). Roots only —
// the trailing `\w*` matches all conjugations (walks, walked, walking).
// Cyrillic roots are appended without `\b` (JS `\b` doesn't bracket Cyrillic).
const _PROSE_EVENT_RE = /\b(walk|run|enter|exit|approach|come|go|leave|depart|arrive|return|rush|dart|dash|sprint|flee|escape|jump|leap|hop|spring|bounc|fall|fell|stumbl|trip|slip|slid|roll|crawl|kneel|crouch|squat|grab|snatch|seiz|reach|extend|stretch|pull|push|shov|throw|toss|hurl|fling|catch|hit|punch|kick|slap|smack|whack|strike|swing|spin|twist|whirl|turn|rotat|pivot|freez|halt|stop|paus|brak|mov|cross|step|drop|lift|raise|lower|hoist|climb|mount|descend|slam|burst|smash|crash|crack|shatter|break|tear|rip|snap|bend|fold|crumple|lung|stagger|wobble|sway|sway|collaps|topple|tumbl|nod|shak|tremb|shiver|quiv|wav|gestur|point|wink|blink|stare|gaz|glanc|peek|peer|squint|ogle|watch|observ|spot|notic|appear|emerg|surfac|materializ|manifest|disappear|vanish|fade|dissolv|reveal|expos|hid|conceal|wince|grimac|flinch|jerk|recoil|shudder|smil|grin|smirk|frown|scowl|glare|sneer|beam|chuckl|giggl|snicker|laugh|cry|cri|sob|weep|wail|moan|groan|grunt|gasp|sigh|pant|wheez|huff|puff|breath|exhal|inhal|cough|sneeze|hiccup|sniff|hiss|growl|roar|bark|yelp|yowl|whimper|whin|whisper|murmur|mumbl|mutter|stammer|stutter|exclaim|shout|scream|yell|holler|bellow|call|hum|whistl|sing|chant|recit|spill|leak|drip|trickl|spray|spurt|gush|flow|stream|pour|splash|squirt|soak|drench|coat|cover|wrap|wind|fasten|tie|untie|button|zip|unzip|knock|tap|rap|drum|bang|thump|kiss|hug|embrac|peck|nuzzl|grip|clutch|squeez|crush|hold|carr|drag|tow|haul|press|lean|recline|rest|lay|laid|sit|stand|ris|set|plac|put|drop|tak|grasp|deliver|give|hand|pass|offer|extend|accept|receive|gather|collect|stack|pil|spread|scatter|sprinkl|dust|sweep|brush|wip|polish|scrub|clean|wash|rins|drink|sip|gulp|swallow|chew|bit|nibbl|gnaw|lick|tast|eat|swallow|read|writ|typ|click|sign|stamp|mark|draw|paint|sketch|ride|driv|board|park|wear|don|remov|strip|undress|dress|clip|scrap|scratch|polish|shav|shed|stride|march|pac|wander|amble|stroll|saunter|trudg|trot|gallop|charg|stomp|tiptoe|sneak|creep|slither|wriggl|float|hover|fly|soar|swoop|div|plung|sink|drown|swim|paddl|wad|surf|sail|cruis|drift|reflect|los|win|find|chas|persu|defend|attack|block|dodg|guard|shield|protect|aim|fir|shoot|spotlight|shine|light|ignit|dim|stares?|glanc|admir|inspect|examin|study|scan|surv|focus)\w*\b|\blooks?\s+(at|up|down|over|around|toward|away|back|forward|inside|outside|within)\b|(?:^|[^А-Яа-яЁё])(идт|идёт|идут|шёл|шла|шли|шед|приш|приход|подойд|подойт|подош|подход|подход|уход|ушёл|ушла|ушли|войт|вош|вход|выйт|выш|вых|сел|сел|сядь|сядет|сидел|вста|встал|встаёт|встан|повор|поверн|поверт|посмотр|смотр|смотрел|смотрит|поглянул|глядит|глянул|взял|берёт|брал|бер|откр|закр|подн|опуст|пов|потян|тян|толкн|толка|удар|ударил|двинул|сказа|говор|шепну|шепч|крикн|крич|восклик|улыб|обня|поцелова|обнимат|кивн|кивает|махн|маша|махал|упал|пад|подым|раскр|закры|вышел|вышла|пришёл|пришла|ушёл|ушла|идя|бежа|бежал|бежит|бегут|схвати|схватил|хватает|схватив|поднял|подним|опустил|подош|подошёл|подходит|ткнул|тыкает|тычет|стало|стал|стала|сделал|делает|сделав|загляну|глядя|залез|залазит|пишет|написал|читает|прочёл|прочит|плач|плакал|плачет|смеет|смеёт|смея|засмеял|улыбнул|улыбается|ухмыл)/i;

function _scaleProseDuration(text) {
  if (!text || !text.trim()) return 0;
  return _PROSE_EVENT_RE.test(text) ? ACTION_BEAT_SEC : 0;
}

// Find a chunk's [start, end] byte-offset in the script via long-line anchors.
// Mirrors the server-side logic in app.py compose endpoint.
function _findChunkRange(scriptText, chunkText) {
  if (!chunkText || !scriptText) return [-1, -1];
  const chunkLines = chunkText.split('\n');
  // Pick anchor candidates: long-enough script-content lines whose chunk-line
  // index we remember so we can expand the range from there.
  const cands = [];
  const candChunkIdx = [];
  for (let i = 0; i < chunkLines.length; i++) {
    const s = chunkLines[i].trim();
    if (s.length < 25) continue;
    if (_matchSceneHeading(s).match) continue;
    cands.push(s);
    candChunkIdx.push(i);
    if (cands.length >= 6) break;
  }
  // Locate anchor in scriptText: prefer unique match, fall back to first.
  let anchorOffset = -1;
  let anchorChunkIdx = -1;
  for (let k = 0; k < cands.length; k++) {
    const idx = scriptText.indexOf(cands[k]);
    if (idx === -1) continue;
    if (scriptText.indexOf(cands[k], idx + 1) === -1) {
      anchorOffset = idx;
      anchorChunkIdx = candChunkIdx[k];
      break;
    }
  }
  if (anchorOffset < 0) {
    for (let k = 0; k < cands.length; k++) {
      const idx = scriptText.indexOf(cands[k]);
      if (idx !== -1) { anchorOffset = idx; anchorChunkIdx = candChunkIdx[k]; break; }
    }
  }
  if (anchorOffset < 0) return [-1, -1];
  // Build (start, end) offsets for every script line — used to step line-by-
  // line in either direction while expanding the matched range.
  const scriptLines = scriptText.split('\n');
  const lineRanges = [];
  {
    let off = 0;
    for (const ln of scriptLines) {
      lineRanges.push([off, off + ln.length]);
      off += ln.length + 1;
    }
  }
  // Find the script-line whose offset matches the anchor.
  let anchorScriptIdx = -1;
  for (let i = 0; i < lineRanges.length; i++) {
    if (lineRanges[i][0] === anchorOffset) { anchorScriptIdx = i; break; }
    if (lineRanges[i][0] > anchorOffset) break;
  }
  if (anchorScriptIdx < 0) {
    // Anchor not on a line boundary (shouldn't happen for line-anchored finds).
    // Fall back to the heuristic end-of-chunkText length.
    return [anchorOffset, anchorOffset + chunkText.length];
  }
  // Expand BACKWARDS / FORWARDS line-by-line. Blank lines in EITHER stream
  // (chunk_text packs lines tightly, scriptText has blank rows between
  // speaker groups) are skipped without breaking the walk. Any non-blank
  // mismatch stops the expansion.
  const _trim = (a) => (a || '').trim();
  // Backwards
  let firstScriptIdx = anchorScriptIdx;
  {
    let ci = anchorChunkIdx - 1, si = anchorScriptIdx - 1;
    while (ci >= 0 && si >= 0) {
      const ct = _trim(chunkLines[ci]);
      const st = _trim(scriptLines[si]);
      if (st === '' && ct !== '') { si--; continue; }   // blank in script
      if (ct === '' && st !== '') { ci--; continue; }   // blank in chunk
      if (ct === '' && st === '') { ci--; si--; continue; }
      if (st === ct) { firstScriptIdx = si; ci--; si--; }
      else break;
    }
  }
  // Forwards
  let lastScriptIdx = anchorScriptIdx;
  {
    let ci = anchorChunkIdx + 1, si = anchorScriptIdx + 1;
    while (ci < chunkLines.length && si < scriptLines.length) {
      const ct = _trim(chunkLines[ci]);
      const st = _trim(scriptLines[si]);
      if (st === '' && ct !== '') { si++; continue; }
      if (ct === '' && st !== '') { ci++; continue; }
      if (ct === '' && st === '') { ci++; si++; continue; }
      if (st === ct) { lastScriptIdx = si; ci++; si++; }
      else break;
    }
  }
  return [lineRanges[firstScriptIdx][0], lineRanges[lastScriptIdx][1]];
}

function _parseScriptScenes(scriptText, overrides) {
  const rawLines = (scriptText || '').split('\n');
  // Two override maps:
  //   pairMap: keyed by `s${sceneIdx}a${autoSeg}` — canonical post-2026-05-15
  //            entries. Matches exactly one segment break, immune to
  //            duplicate-anchor scripts (dialogue-heavy back-and-forth).
  //   anchorMap: legacy entries without sceneIdx/autoSeg — keyed by anchor.
  //            Still respected so old projects don't lose their tweaks.
  const overridePairMap = new Map();   // 's0a3' → 'merge'|'break'
  const overrideAnchorMap = new Map(); // 'Ethan:' → 'merge'|'break'  (legacy)
  // Defensive: callers must pass an ARRAY of override entries. Some headless
  // paths (standalone parallel runner, music kick-off) historically passed `{}`
  // meaning "no overrides" — but `{}` is truthy and NOT iterable, so the for-of
  // below threw «(overrides || []) is not iterable» and crashed the whole run.
  // Coerce any non-array (incl. {} / null / undefined) to an empty list.
  for (const o of (Array.isArray(overrides) ? overrides : [])) {
    if (!o || !(o.action === 'break' || o.action === 'merge')) continue;
    if (typeof o.sceneIdx === 'number' && typeof o.autoSeg === 'number') {
      overridePairMap.set(`s${o.sceneIdx}a${o.autoSeg}`, o.action);
    } else if (o.anchor) {
      overrideAnchorMap.set(o.anchor, o.action);
    }
  }
  // Combined view for the «do we have any overrides at all?» short-circuit.
  const overrideMap = overrideAnchorMap;
  const scenes = [];
  let inCast = false;
  let inNotes = false;
  let inSynopsis = false;   // tracks multi-line «Кратко: …» / «Summary: …» blocks
  let inBlocking = false;   // tracks [BLOCKING]…[/BLOCKING] and [BLOCKING_OUT]…[/BLOCKING_OUT]
                            // — visual/spatial setup metadata, NOT story content.
                            // Lines inside contribute 0 chrono, never form a segment.
                            // Server-side compose reads them from the raw script
                            // and injects them into the Seedance prompt.
  let blockingKind = '';    // 'in' or 'out' — appended to current scene's metadata
  let pendingEpisodeBlocking = [];  // [BLOCKING] before any scene heading lands here
  let cur = null;
  let runningOffset = 0;
  // Heuristic for «is this line a real script-style content line?» — used to
  // decide when to exit a multi-line synopsis block. Dialogue («NAME:») /
  // bracketed action / parenthetical / dash-led line all count.
  const _isScriptLine = (s) => (
    /^[A-ZА-ЯЁ][A-ZА-ЯЁ\s\.\-']{1,40}:\s/.test(s) ||   // CHAR:
    /^\[[^\]]+\]/.test(s) ||                            // [stage direction]
    /^\([^)]+\)/.test(s) ||                             // (parenthetical)
    /^[-–—]\s/.test(s) ||                               // — line
    /^(?:int\.|ext\.|инт\.|экст\.|нат\.|сцена)\s/i.test(s)  // scene heading
  );
  // Tracks the screenplay-format orphan-cue → payload handoff across lines.
  // True after we've just seen a bare speaker cue ("SOFIA", "DANTE (V.O.)")
  // and are waiting for the dialogue text line that belongs to it.
  let expectingDialogue = false;

  // IMPLICIT-BLOCKING PRE-PASS. Some writer-LLMs emit a setup block WITHOUT
  // fence markers ([BLOCKING_START] / etc.), relying on content patterns like
  // `NAME: position :: OUTFIT: clothes` or standalone `LOCATION:`. Without
  // explicit fences the segmenter happily turns "LOCATION:" into a scene
  // heading and the `:: OUTFIT:` lines into bogus dialogue. Detect those
  // signatures and mark indices to skip from segmentation (they still stay
  // in the raw script so server-side compose injects them).
  const implicitBlockingIdx = new Set();
  const _isOutfitLine = (t) => /::\s*(?:OUTFIT|WEARING|WEAR|CLOTHES|COSTUME)\s*[:：]/i.test(t);
  const _isBlockingMetaKey = (t) =>
    /^(?:LOCATION|MOOD|LIGHTING|PROPS|CAMERA|FRAMING|SETTING|TIME|WEATHER|ATMOSPHERE|ATMOSFERA|ОСВЕЩЕНИЕ|РЕКВИЗИТ|ЛОКАЦИЯ|АТМОСФЕРА)\s*[:：]\s*\S/i.test(t);
  // Stative position verbs (RU + EN) at the START of `NAME: <text>` distinguish
  // a "blocking position line" from real dialogue. Real dialogue starts with
  // pronouns ("I/Я/Ты"), quote, or action — not "стоит/sits".
  // Use a lookahead boundary instead of \b because JS \b doesn't recognise
  // Cyrillic letters as word characters → \b after "стоит" never matches.
  const _POSITION_VERB_RE = /^(?:стои[тю]|стоят|сиди[тю]|сидят|лежи[тшю]|лежат|держи[тшю]|держат|смотри[тшю]|смотрят|одет[аоы]?|оперевш\w*|прислон\w*|сжима\w*|стиска\w*|наблюда\w*|замер\w*|опуст\w*|поднят\w*|опущен\w*|облокот\w*|прижим\w*|нависа\w*|нагиба\w*|склон\w*|присел\w*|развалил\w*|wears?|stands?|sits?|lies?|holds?|looks?\s+at|watches?|leans?|grips?|clenches?|presses?|tilts?|rests?|stays?|crouches?|kneels?|squats?|positions?)(?=[\s,.;:!?]|$)/i;
  // A `NAME: text` line whose text starts with a stative-position verb.
  const _isPositionLine = (t) => {
    const m = t.match(/^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 \-'.#]{0,40}\s*[:：]\s*(.+)$/);
    if (!m) return false;
    return _POSITION_VERB_RE.test(m[1].trim());
  };
  const _isBlockingContent = (t) => _isOutfitLine(t) || _isPositionLine(t) || _isBlockingMetaKey(t);
  for (let i = 0; i < rawLines.length; i++) {
    const t = rawLines[i].trim();
    // Primary trigger: outfit-line OR position-NAME line. Don't trigger on a
    // lone LOCATION: — it might be a real (inferred) scene heading.
    if (!_isOutfitLine(t) && !_isPositionLine(t)) continue;
    implicitBlockingIdx.add(i);
    // Walk BACKWARDS through blanks + blocking-content lines (meta/outfit/pos).
    for (let j = i - 1; j >= 0; j--) {
      const tj = rawLines[j].trim();
      if (!tj) continue;
      if (_isBlockingContent(tj)) { implicitBlockingIdx.add(j); continue; }
      break;
    }
    // Walk FORWARDS too — pick up subsequent blocking-content lines.
    for (let j = i + 1; j < rawLines.length; j++) {
      const tj = rawLines[j].trim();
      if (!tj) continue;
      if (_isBlockingContent(tj)) { implicitBlockingIdx.add(j); continue; }
      break;
    }
  }

  for (let _lineIdx = 0; _lineIdx < rawLines.length; _lineIdx++) {
    const rawLine = rawLines[_lineIdx];
    const lineStart = runningOffset;
    const lineEnd = runningOffset + rawLine.length;
    runningOffset = lineEnd + 1; // +1 for the \n we removed
    const t = rawLine.trim();
    // CAST block — skip entirely
    if (/^={3,}\s*EPISODE CAST/i.test(t)) { inCast = true;  continue; }
    if (/^={3,}\s*END CAST/i.test(t))     { inCast = false; continue; }
    if (inCast) continue;

    // Implicit blocking — line is part of a fence-less setup block detected
    // by the pre-pass (`:: OUTFIT:` pattern or LOCATION:/MOOD:/etc adjacent
    // to one). Treat as blockingIn for the current scene (or pending if no
    // scene open yet), then continue.
    if (implicitBlockingIdx.has(_lineIdx)) {
      if (cur) {
        (cur.blockingIn = cur.blockingIn || []).push(rawLine);
      } else {
        pendingEpisodeBlocking.push(rawLine);
      }
      continue;
    }

    // BLOCKING fence — visual/spatial setup metadata. Lines inside the fence
    // are NOT story content: 0 chrono, no segment, no scene heading detection.
    // Ultra-permissive marker detection: accepts ALL common variants different
    // LLMs / writers tend to emit:
    //   [BLOCKING]       [/BLOCKING]
    //   [BLOCKING_START] [BLOCKING_END]
    //   [BLOCKING_BEGIN] [BLOCKING_CLOSE]
    //   [BLOCKING_OPEN]  [/BLOCKING_END]
    //   plus _OUT variants for closing-scene mise-en-scène.
    // Classification: a fence is a CLOSER if it has `[/` prefix OR ends with
    // `_END]` / `_CLOSE]`. Otherwise it's an OPENER. `_OUT` anywhere → maps
    // to scene's `blockingOut` instead of `blockingIn`.
    const _isBlockingFence = /^\[\/?\s*BLOCKING(?:_OUT)?(?:_(?:START|BEGIN|OPEN|END|CLOSE))?\s*\]\s*$/i.test(t);
    if (_isBlockingFence) {
      const isClose = /^\[\s*\//.test(t) || /_(?:END|CLOSE)\s*\]/i.test(t);
      const isOut   = /BLOCKING_OUT/i.test(t);
      if (isClose) {
        inBlocking = false;
        blockingKind = '';
      } else {
        inBlocking = true;
        blockingKind = isOut ? 'out' : 'in';
      }
      continue;
    }
    if (inBlocking) {
      if (!t) continue;
      if (cur) {
        const key = blockingKind === 'out' ? 'blockingOut' : 'blockingIn';
        (cur[key] = cur[key] || []).push(rawLine);
      } else {
        pendingEpisodeBlocking.push(rawLine);
      }
      continue;
    }
    // Synopsis block — enter on «Кратко:» / «Summary:» / «Brief:» / etc.
    // Stay inside until we hit either a blank line OR a real script-style
    // line (CHAR: / [action] / scene heading). Meant for the 1-3 sentences
    // of episode synopsis that wrap onto multiple lines without re-prefix.
    if (/^(?:кратко|синопсис|summary|brief|logline|premise|tldr)\s*[:\-—]/i.test(t)) {
      inSynopsis = true;
      continue;
    }
    if (inSynopsis) {
      if (!t) { inSynopsis = false; continue; }   // blank line ends synopsis
      if (_isScriptLine(t)) { inSynopsis = false; /* fall through, process this line */ }
      else continue;   // still inside synopsis, skip
    }
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
    //   **LOCATION:** Manhattan, **IN THE FRAME:** MAYA, ADRIAN
    //   - **Cliffhanger:** "..."
    //   - **Emotional peak:** Vivian's admission of forcing Clara…
    //   - **Setup for Episode 5:** Legal confrontation begins…
    //
    // CRITICAL: must NOT catch dialogue cues like `**ADRIAN:** Damn it.`
    // The old open pattern `^\*\*[letters]+:\*\*` swallowed every screenplay
    // cue and dropped its dialogue line silently. Now we whitelist the actual
    // meta keywords (with optional 1-5 trailing words to cover `Setup for
    // Episode 5`). Bullet-prefixed lines stay broadly matched — by convention
    // ANY `- **Label:**` is meta, never dialogue.
    if (/^[-*•]\s+\*\*[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9\s]*:\s*\*\*/.test(t)) continue;
    if (/^\*\*\s*(?:cliffhanger|setup|summary|brief|logline|premise|tl;?dr|synopsis|hook(?:\s+type)?|reversal(?:\s+type)?|escalation(?:\s+rung)?|spoken\s+word\s+count|estimated\s+runtime|emotional(?:\s+peak)?|theme|notes?|location|setting|locale|in\s+the\s+frame|scene\s+brief|кратко(?:е\s+содержание)?|синопсис|содержание|суммари|клиффхэнгер|разворот|хук|локация|в\s+кадре)(?:\s+[\w\d]+){0,5}\s*[:：]\s*\*\*/i.test(t)) continue;
    // Horizontal separators
    if (/^[-—=]{3,}\s*$/.test(t)) continue;

    // Scene heading: open a new scene (formal INT./EXT. or inferred)
    const headMatch = _matchSceneHeading(t);
    if (headMatch.match) {
      cur = { id: scenes.length, heading: t, lines: [], totalSec: 0, inferred: headMatch.inferred };
      // Hand off any [BLOCKING] block that appeared BEFORE this heading to the
      // new scene — it was setup for THIS scene, just placed early.
      if (pendingEpisodeBlocking.length) {
        cur.blockingIn = pendingEpisodeBlocking.slice();
        pendingEpisodeBlocking = [];
      }
      scenes.push(cur);
      expectingDialogue = false;  // reset cross-scene state
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
      if (pendingEpisodeBlocking.length) {
        cur.blockingIn = pendingEpisodeBlocking.slice();
        pendingEpisodeBlocking = [];
      }
      scenes.push(cur);
    }

    // Screenplay multi-line dialogue. Two flavors of "cue waiting for payload":
    //   (a) Orphan cue, no colon:        SOFIA / DANTE (V.O.) / **Clara**
    //                                    Three nights...
    //   (b) Name-colon, empty tail:      Clara: / Mrs. Vale: / **Sofia:**
    //                                    "The moon..."
    // `_lineDuration` is stateless and cannot tell that the bare line below
    // a cue is its dialogue payload — it falls through to plain prose and
    // gets billed at 0-1.5s instead of word-count rate. Track the cue→payload
    // handoff here so dialogue is computed correctly. Markdown wrappers
    // (`**Clara:**`, `*Sofia*`) are stripped before matching.
    const _t = _stripMarkdownWrappers(t);
    const _isCueAwaitingPayload = (text) => {
      if (_isOrphanSpeakerCue(text)) return true;
      // "Clara:" / "Mrs. Vale:" / "DANTE (V.O.):" with empty after the colon
      const cm = text.match(/^([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-'.#]{0,40})\s*[:：]\s*(.*)$/);
      if (!cm || !/^[A-ZА-ЯЁ]/.test(cm[1])) return false;
      const tail = (cm[2] || '').replace(/\([^)]*\)/g, ' ').replace(/\[[^\]]*\]/g, ' ').replace(/\*[^*]*\*/g, ' ').trim();
      return tail === '';
    };
    let dur;
    if (expectingDialogue) {
      if (_isCueAwaitingPayload(_t)) {
        // Another cue in a row — keep waiting for payload.
        dur = 0;
      } else if (/^\([^)]+\)\.?$/.test(_t)) {
        // Parenthetical tone note between cue and payload ("(barely breathing)") —
        // not spoken, kept as 0s; payload still pending on next line.
        dur = 0;
      } else {
        // This line IS the dialogue payload. Compute by speech rate.
        const words = _countDialogueWords(_t);
        dur = words > 0 ? 0.4 + words / SPEECH_WPS : 0.5;
        expectingDialogue = false;
      }
    } else {
      if (_isCueAwaitingPayload(_t)) {
        // Cue on its own line — 0s, expect dialogue payload next.
        dur = 0;
        expectingDialogue = true;
      } else {
        dur = _lineDuration(rawLine);
      }
    }
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
  const TARGET = 14.0;          // aim around this (informational)
  const LUFT_MAX_SEC = 18.5;    // Hard ceiling everywhere (Pass 1 break threshold,
                                // Pass 3 luft-merge, Pass 5 line-pull, Pass 7
                                // final sweep). Seedance hard-caps at 15s, but
                                // a 15-18.5s content chunk renders fine —
                                // characters just speak ~20% faster. Trading
                                // up to 3.5s of pacing tightness for fewer
                                // generation cycles is a clear net win.
                                // Was 17 (2s luft) → bumped to 18.5 (3.5s luft).
  const SOFT_MAX = LUFT_MAX_SEC; // Pack directly to luft from Pass 1 — no
                                // separate "soft" threshold. Pass 3/5/7 only
                                // touch up edge cases (small leftover tails).
  const MIN_SEGMENT_SEC = 5.0;  // smaller than this = wasted Seedance chunk
  const HARD_MAX_SEC = 14.5;    // absolute ceiling for Pass-2 small-segment merges
  // Speaker cue detector — a line that's ONLY a character-name cue, no
  // dialogue text. Two formats supported:
  //   (a) Classic screenplay ALL-CAPS:  ETHAN  / ETHAN (CONT'D) / ETHAN (V.O.)
  //   (b) Modern Title-case + colon:    Ethan:  / Ava (V.O.):
  // Optional parenthetical qualifier «(CONT'D)» / «(V.O.)» / «(to X)».
  // These must NEVER be separated from the dialogue line that follows —
  // otherwise the chunk starts with an orphan dialogue line whose speaker
  // landed in the previous chunk, and ends with a dangling cue whose
  // dialogue landed in the next chunk. User-reported real bug: chunk
  // contained «Yes. / Ethan: / You said you stole my access codes. / Ava:».
  const _isSpeakerCue = (text) => {
    const t = (text || '').trim();
    if (!t || t.length > 40) return false;
    // Format (b): «Name:» or «Name (V.O.):» — only a name + optional
    // qualifier, ending in a colon, NO dialogue text after the colon.
    const colonMatch = t.match(/^([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 \-']{0,28})(\s*\([^)]{0,20}\))?\s*[:：]\s*$/);
    if (colonMatch) {
      const name = colonMatch[1].trim();
      // Reject heading/meta tokens. (JS \b doesn't work for Cyrillic, so
      // we use explicit alternation with optional non-letter suffix.)
      const META_TOKENS = /^(INT|EXT|FADE|CUT|MATCH|DISSOLVE|TIME|END|FIN|SCENE|FLASHBACK|Локация|Сцена|Time|Day|Night|Morning|Evening)(?:[^A-Za-zА-Яа-яЁё]|$)/i;
      if (/^[A-ZА-ЯЁ]/.test(name) && !META_TOKENS.test(name)) {
        return true;
      }
    }
    // Format (a): bare ALL-CAPS cue without colon.
    const base = t.replace(/\s*\([^)]+\)\s*$/, '').trim();   // strip (CONT'D) etc
    if (!base || base.length > 30) return false;
    if (/[a-zа-яё]/.test(base)) return false;                // no lowercase
    if (!/[A-ZА-ЯЁ]/.test(base)) return false;
    const tokens = base.split(/\s+/);
    if (tokens.length > 4) return false;
    if (/^(INT|EXT|FADE|CUT|MATCH|DISSOLVE|TIME|END|FIN|SCENE|FLASHBACK)\b/i.test(base)) return false;
    return true;
  };
  for (const sc of scenes) {
    // Pass 1: greedy, never break before MIN_SEGMENT. When a break would
    // land BETWEEN a speaker cue and its dialogue, pull the cue forward
    // into the new segment along with the dialogue.
    let acc = 0, seg = 0;
    for (let i = 0; i < sc.lines.length; i++) {
      const l = sc.lines[i];
      const prev = i > 0 ? sc.lines[i - 1] : null;
      if (acc >= MIN_SEGMENT_SEC && acc + l.duration > SOFT_MAX) {
        // If the previous line was a speaker cue and is currently in the
        // CURRENT segment, move it forward to the new segment so cue+dialogue
        // stay together. Decrement acc accordingly.
        if (prev && _isSpeakerCue(prev.text) && prev.segIdx === seg) {
          prev.segIdx = seg + 1;
          acc -= prev.duration;
          seg += 1;
          acc = prev.duration;   // new segment already has the cue
        } else {
          seg += 1;
          acc = 0;
        }
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
    // Pass 3 (luft-merge): walk left-to-right and merge any adjacent pair
    // whose combined duration ≤ LUFT_MAX_SEC (17s). The intent: a stretch of
    // dialogue that totals 16-17s gets stuck as [10s][6.5s] because Pass-1's
    // SOFT_MAX(13) forces a break. Sending those as a single 15s Seedance
    // call (with slightly compressed pacing) costs us one generation cycle
    // instead of two. Keep merging the same index while it still has a
    // mergeable right-neighbour.
    let _lm = 0;
    while (_lm < segTotals.length - 1) {
      if (segTotals[_lm] + segTotals[_lm + 1] <= LUFT_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === _lm + 1) l.segIdx = _lm;
          else if (l.segIdx > _lm + 1) l.segIdx -= 1;
        }
        segTotals[_lm] += segTotals[_lm + 1];
        segTotals.splice(_lm + 1, 1);
        // stay on _lm to attempt another merge with the new right-neighbour
      } else {
        _lm++;
      }
    }

    // Pass 5 (line-level fill-from-next): when Pass-3 can't merge whole
    // chunks (combined > LUFT), still try to PULL the first dialogue/action
    // unit from the next chunk into this one. Tightens every chunk toward
    // 15s without breaking dialogue lines apart. "Unit" = all leading
    // zero-duration lines (speaker cues, parentheticals) + the next single
    // substantive line. Keeps cue+payload paired.
    const _pullUnitFromNext = (fromSegIdx) => {
      const idxs = [];
      let dur = 0;
      let firstAt = -1;
      for (let li = 0; li < sc.lines.length; li++) {
        if (sc.lines[li].segIdx === fromSegIdx) {
          if (firstAt < 0) firstAt = li;
          idxs.push(li);
          dur += sc.lines[li].duration;
          if (sc.lines[li].duration > 0.01) break;  // got the substantive line
        } else if (firstAt >= 0) {
          break;
        }
      }
      return { idxs, dur, hasSubstantive: dur > 0.01 };
    };
    let _pi = 0;
    while (_pi < segTotals.length - 1) {
      const unit = _pullUnitFromNext(_pi + 1);
      if (!unit.idxs.length || !unit.hasSubstantive) { _pi++; continue; }
      const newCur  = segTotals[_pi]     + unit.dur;
      const newNext = segTotals[_pi + 1] - unit.dur;
      // Two guard rails:
      //   (a) Current chunk must stay within luft.
      //   (b) Next chunk must either DRAIN to zero (fully absorbed) OR keep
      //       enough content to remain valid (≥ MIN_SEGMENT_SEC). Otherwise
      //       we'd create a 1-2s orphan tail that wastes a Seedance call.
      const fits = newCur <= LUFT_MAX_SEC && (newNext < 0.01 || newNext >= MIN_SEGMENT_SEC);
      if (fits) {
        for (const li of unit.idxs) sc.lines[li].segIdx = _pi;
        segTotals[_pi]     = newCur;
        segTotals[_pi + 1] = newNext;
        if (newNext <= 0.01) {
          for (const l of sc.lines) {
            if (l.segIdx > _pi + 1) l.segIdx -= 1;
          }
          segTotals.splice(_pi + 1, 1);
        }
        // stay on _pi; try pulling another unit
      } else {
        _pi++;
      }
    }

    // Pass 6 (cleanup): re-run the tiny-merge pass on the new layout.
    // After Pass 5 some chunks may have shrunk under MIN_SEGMENT — try to
    // fold them back into a neighbour if combined ≤ LUFT_MAX_SEC (more
    // generous than Pass-2's HARD_MAX since we already accepted luft).
    for (let i = segTotals.length - 1; i >= 0; i--) {
      if (segTotals[i] >= MIN_SEGMENT_SEC) continue;
      if (i > 0 && segTotals[i-1] + segTotals[i] <= LUFT_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === i) l.segIdx = i - 1;
          else if (l.segIdx > i) l.segIdx -= 1;
        }
        segTotals[i-1] += segTotals[i];
        segTotals.splice(i, 1);
        continue;
      }
      if (i < segTotals.length - 1 && segTotals[i] + segTotals[i+1] <= LUFT_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === i+1) l.segIdx = i;
          else if (l.segIdx > i+1) l.segIdx -= 1;
        }
        segTotals[i] += segTotals[i+1];
        segTotals.splice(i+1, 1);
      }
    }

    // Pass 7 (final luft sweep): re-run Pass-3-style pairwise merge AFTER
    // line-level shuffles. Pass-5 may have pulled lines around in a way that
    // newly-adjacent pairs are now ≤ LUFT_MAX_SEC and weren't before. Also
    // catches the case where Pass 3's first sweep was blocked by intermediate
    // chunk sizes that Pass-5 has since redistributed.
    let _lm2 = 0;
    while (_lm2 < segTotals.length - 1) {
      if (segTotals[_lm2] + segTotals[_lm2 + 1] <= LUFT_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === _lm2 + 1) l.segIdx = _lm2;
          else if (l.segIdx > _lm2 + 1) l.segIdx -= 1;
        }
        segTotals[_lm2] += segTotals[_lm2 + 1];
        segTotals.splice(_lm2 + 1, 1);
      } else {
        _lm2++;
      }
    }

    // Pass 8 (de-dangle speaker cues): after every merge/shuffle above, a
    // chunk may END with a speaker cue (+ trailing tone-note parentheticals)
    // whose actual dialogue line landed in the NEXT chunk. That orphans the
    // cue — the next chunk opens with a dialogue line and nobody knows who's
    // speaking. Real user bug: chunk ends «VERA / (coldly)» and the next chunk
    // starts «Helen signed the documents…».
    //
    // Pass-1 already pulls a cue forward, but ONLY when the cue sits
    // IMMEDIATELY before the break. A parenthetical between cue and dialogue
    // («VERA / (coldly) / Helen…») defeats it, and later merge passes can
    // re-separate a cue from its payload. So we sweep one more time here,
    // after the layout is final.
    //
    // "Glue" lines = speaker cues + parenthetical tone-notes (these attach to
    // the FOLLOWING dialogue). We collect the trailing glue run at each
    // chunk's tail; if it contains a speaker cue, we move everything from the
    // first such cue onward into the next chunk. Moving these (≤0.4s each)
    // never meaningfully affects luft but guarantees cue+payload stay paired.
    {
      const _isParenOnly = (text) => /^\(.+\)\.?$/.test((text || '').trim());
      // Snapshot per-segment line indices in document order.
      const segLineIdxs = [];
      for (let li = 0; li < sc.lines.length; li++) {
        const s = sc.lines[li].segIdx;
        while (segLineIdxs.length <= s) segLineIdxs.push([]);
        segLineIdxs[s].push(li);
      }
      // Last segment has no "next" chunk to host the cue — skip it.
      for (let s = 0; s < segLineIdxs.length - 1; s++) {
        const lis = segLineIdxs[s];
        if (lis.length < 2) continue;
        // Walk backward over the trailing glue run (cues + parentheticals).
        let k = lis.length - 1;
        while (k >= 0) {
          const txt = sc.lines[lis[k]].text;
          if (_isSpeakerCue(txt) || _isParenOnly(txt) || sc.lines[lis[k]].duration <= 0.01) {
            k--;
          } else {
            break;
          }
        }
        // Trailing glue run is lis[k+1 .. end]. Find the first speaker cue in it.
        let cueAt = -1;
        for (let m = k + 1; m < lis.length; m++) {
          if (_isSpeakerCue(sc.lines[lis[m]].text)) { cueAt = m; break; }
        }
        if (cueAt <= 0) continue;            // no dangling cue, or it would empty the chunk
        // Push cue + everything after it into the next chunk (prepends in
        // correct document order since these are the segment's last lines).
        for (let m = cueAt; m < lis.length; m++) {
          sc.lines[lis[m]].segIdx = s + 1;
        }
      }
    }
    sc.segCount = sc.lines.length ? (sc.lines[sc.lines.length - 1].segIdx + 1) : 0;
  }

  // Apply manual overrides (force-break / force-merge per line anchor) AFTER
  // auto-segmentation so user's choices win. Skipped lines (meta, headings)
  // never reach lines[] so they can't carry overrides — fine, those aren't
  // displayed segments anyway.
  if (overridePairMap.size || overrideAnchorMap.size) {
    let sIdx = 0;
    for (const sc of scenes) {
      // Snapshot auto-segmentation (transitions = points where auto wanted a break)
      for (const l of sc.lines) l._autoSeg = l.segIdx;
      // For each line, pick action: prefer (sceneIdx, autoSeg) match.
      // Anchor-only fallback (legacy data) is applied at most ONCE per
      // anchor per scene — without this guard, a script where many
      // segments start with the same speaker cue ("Ethan:") would
      // re-trigger the merge on every same-anchor segment break and
      // collapse the rest of the scene into one chunk.
      const usedLegacyAnchors = new Set();
      let curSeg = 0;
      for (let i = 0; i < sc.lines.length; i++) {
        const l = sc.lines[i];
        const pairKey = `s${sIdx}a${l._autoSeg}`;
        let action = overridePairMap.get(pairKey) || null;
        if (!action) {
          const anc = _lineAnchor(l.text);
          if (!usedLegacyAnchors.has(anc)) {
            const legacyAction = overrideAnchorMap.get(anc);
            if (legacyAction) {
              action = legacyAction;
              usedLegacyAnchors.add(anc);
            }
          }
        }
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
      sIdx += 1;
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
  // Find all ranges covering this line. Multiple chunks can share the same
  // chunk_text (original + retries) — they all cover the same line range.
  // OLD bug: used strict `>` on start, so the FIRST-iterated range won and
  // stayed even if a later retry succeeded. Result: failed-then-succeeded
  // segments stayed red in UI.
  // FIX: prefer status by priority (completed > pending > failed) regardless
  // of which chunk came first.
  const covering = coverage.filter(r =>
    r.start <= line.offset + 5 && r.end >= line.offsetEnd - 5
  );
  if (!covering.length) return null;
  // 1) Any completed range wins. Pick latest by idx if multiple completed.
  const completed = covering.filter(r => r.status === 'completed');
  if (completed.length) {
    return completed.reduce((best, r) => (r.idx ?? 0) > (best.idx ?? 0) ? r : best);
  }
  // 2) Any in-progress (running/pending/submitted/etc.) wins over failed.
  const inProgress = covering.filter(r => r.status !== 'failed');
  if (inProgress.length) {
    return inProgress.reduce((best, r) => (r.idx ?? 0) > (best.idx ?? 0) ? r : best);
  }
  // 3) Everything failed — show the latest failed for user info.
  return covering.reduce((best, r) => (r.idx ?? 0) > (best.idx ?? 0) ? r : best);
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
  // Auto-mode state for the button label — per-episode aware so the button on
  // episode N reflects N's own run, even when other episodes run in parallel.
  const autoActive = (typeof _autoRunForEpisode === 'function')
    ? !!_autoRunForEpisode(S?.seriesId, S?.episode?.number)
    : ((typeof AUTO !== 'undefined') && AUTO.active);
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
        // Escape BOTH single quotes (JS string delim inside onclick) AND
        // double quotes (HTML attribute delim) AND backslashes/ampersand.
        // Real production bug: dialogue line starting with «"The child …»
        // broke the onclick attribute at the first " — click silently
        // did nothing. _attrSafe handles all four meta-chars in order.
        const safeAnchor = _attrSafe(anchor);
        const autoSegArg = (typeof l._autoSeg === 'number') ? l._autoSeg : l.segIdx;
        html += `<div class="ep-split-handle ${isOvr ? 'ovr' : ''}"
          onclick="toggleSegmentBreak('${safeAnchor}', ${sIdx}, ${autoSegArg})"
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
        // Two-tier warning. Segments in (15, 18.5]s were intentionally luft-merged
        // by Pass 3/5/7 (one fewer Seedance call, characters speak ~20% faster
        // — accepted up to 3.5s overflow). Only >18.5s is a real overflow that
        // won't fit cleanly and needs manual splitting.
        const overflowWarn = segTotal > 18.5
          ? `<span class="ep-seg-warn" title="Содержимое сильно выходит за 15-сек лимит Seedance — нужна ручная разбивка">${segTotal.toFixed(1)}с ⚠</span>`
          : segTotal > 15.0
          ? `<span class="ep-seg-dur ep-seg-luft" title="Luft-merge: 15-18.5с контента в 15-сек чанке — пацинг будет слегка ускоренный">${segTotal.toFixed(1)}с ⚡</span>`
          : `<span class="ep-seg-dur" title="Расчётная длительность сегмента">${segTotal.toFixed(1)}с</span>`;
        // First-line anchor of this segment for the MERGE button (per-line, OK
        // if it shifts when the user edits text — merge is a local operation).
        // See _attrSafe note above: must escape both " (HTML attr delim) and
        // ' (JS string delim) so a dialogue line starting with «"» doesn't
        // truncate the onclick handler.
        const firstAnchor = _lineAnchor(l.text);
        const safeFirstAnchor = _attrSafe(firstAnchor);
        // Per-segment AUTO-SKIP anchor MUST be unique across the episode.
        // Bug 2026-05-12: using firstAnchor here collapsed all segments whose
        // first line started with the same text (e.g. several segments
        // starting with «Elena:» or «MARCUS») onto a single skip key — un-
        // ticking one's AUTO checkbox flipped them all. Use the scene/segment
        // coordinate as the stable identity. Old skip entries written with
        // the previous (text-based) format silently fall through — segments
        // default to «included» and the user can re-skip if needed.
        const autoSkipKey = `s${sIdx}g${l.segIdx}`;
        const isMergedHere = l._override === 'merge';
        const isAutoSkipped = _isSegmentAutoSkipped(autoSkipKey);
        const mergeAutoSeg = (typeof l._autoSeg === 'number') ? l._autoSeg : l.segIdx;
        const editBtns = editMode && lIdx > 0
          ? `<button class="ep-seg-mini" onclick="toggleSegmentMerge('${safeFirstAnchor}', ${sIdx}, ${mergeAutoSeg})"
              title="${isMergedHere ? 'Восстановить разделение' : 'Объединить с предыдущим сегментом'}"
            >${isMergedHere ? '↩ разъединить' : '🔗 ↑ объединить'}</button>`
          : '';
        const autoCb = `<label class="ep-seg-auto-cb" title="${isAutoSkipped ? 'Сегмент пропускается в Auto-mode — клик включит обратно' : 'Сегмент будет сгенерён при запуске Auto-mode — клик исключит его'}">
            <input type="checkbox" ${isAutoSkipped ? '' : 'checked'} onchange="toggleSegmentAutoInclude('${autoSkipKey}')">
            <span>auto</span>
          </label>`;
        html += `<div class="ep-seg${isAutoSkipped ? ' auto-skipped' : ''}" data-seg="${l.segIdx + 1}" data-scene-idx="${sIdx}" data-seg-idx="${l.segIdx}">
          <div class="ep-seg-bracket" title="Seedance-сегмент ${l.segIdx + 1}">${l.segIdx + 1}</div>
          <div class="ep-seg-body">
            <div class="ep-seg-toolbar">
              <button class="ep-seg-send" onclick="sendSceneSegmentToSeedance(${sIdx}, ${l.segIdx})"
                title="Скопировать сегмент в Seedance compose и прокрутить вниз">
                🎬 в Сиданс
              </button>
              <button class="ep-seg-send" onclick="showChunksForSegment(${sIdx}, ${l.segIdx})"
                title="Прокрутить вниз к сгенерированным видео-чанкам для этого сегмента и подсветить их">
                📺 К видео
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

// Make a string safe for embedding as a JS-string argument inside an HTML
// onclick="..." attribute. Order matters:
//   1) ampersand FIRST so we don't double-encode the entities we add next
//   2) double-quote → &quot; so the surrounding attribute (which uses ") doesn't terminate early
//   3) single-quote → \' so the inner JS string (delim ') doesn't terminate early
//   4) backslash → escape so existing \ in the string doesn't become a JS escape sequence
function _attrSafe(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '&quot;')
    .replace(/'/g, "\\'");
}

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
// Voice-over / off-screen / internal-monologue marker detector. These
// lines play as audio narration overlaid ON TOP of the visual action —
// the listener can hear them while seeing whatever the camera is doing.
// For chunk-duration calc this means we don't ADD action duration on
// top of VO duration; the two overlap, max() not sum.
//
// Self-contained on purpose — _isDialogueLine rejects periods in the
// speaker cue («SOPHIE (V.O.)» has dots) so we'd miss exactly the most
// common VO formatting. We just look for «<name>(<vo-marker>):» shape.
function _isVoiceOverLine(text) {
  if (!text) return false;
  const t = text.trim();
  // Must start with an uppercase-letter name, have a parenthetical with
  // a VO marker, and end the cue with a colon.
  return /^[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё_0-9 .\-']{0,40}\s*\((?:v\.?o\.?|voiceover|voice[\s\-]over|o\.?s\.?|off[\s\-]screen|narration|закадр\w*|голос за кадром|внутренний голос|мысленно|про себя)\)\s*[:：]/i
    .test(t);
}

// Detect orphan speaker cues — standalone screenplay-format speaker names
// that precede their dialogue on the NEXT line. Examples:
//   FRANK
//   I'll be in the office doing books.
//
//   Sofia (V.O.)
//   Three nights since I left.
// Without this detection the cue line is mis-billed as a short action beat
// (floored to 1.5s) AND the dialogue below is mis-billed as prose-action
// (chars/35 rate) instead of speech (words/wps) — inflating chunk duration.
function _isOrphanSpeakerCue(line) {
  if (!line) return false;
  const t = line.trim();
  if (!t) return false;
  if (t.length > 40) return false;
  // Reject scene headings / transitions (those start with INT./EXT./FADE/CUT)
  if (_matchSceneHeading(t).match) return false;
  if (/^[\s—-]*(FADE|CUT|DISSOLVE|SMASH|MATCH)\s+(IN|OUT|TO|BACK)\b/i.test(t)) return false;
  // No sentence punctuation (period, exclamation, question) — those indicate
  // a real action/dialogue line, not a cue. Colons disqualify too (that's
  // inline "NAME: text" handled by _lineDuration directly).
  if (/[.!?:]/.test(t.replace(/\([^)]*\)/g, ''))) return false;
  // Cue body shape: starts with uppercase, optional parenthetical
  // (V.O. / O.S. / CONT'D / age tag / etc.), allowed chars: letters,
  // spaces, hyphens, apostrophes. Either ALL-CAPS classic screenplay
  // form OR Title-case modern form.
  return /^[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9 \-']{0,38}(?:\s*\([^)]{0,30}\))?\s*$/.test(t);
}

// Single source of truth for chunk duration estimation from raw chunk text.
// Replicates the VO-aware logic used in _autoCollectSegments + parallel range-
// gen so retries / reuses / heal flows compute consistent durations instead of
// inheriting the (potentially inflated) duration of the chunk being replaced.
// Returns an integer in [5, 15] seconds.
function _estimateChunkDurationSec(chunkText, opts) {
  opts = opts || {};
  const text = (chunkText || '').trim();
  if (!text) return Math.max(5, Math.min(15, opts.fallback || 5));
  // Split into raw lines, skip empties and scene heading / fade markers
  // (_lineDuration returns 0 for those).
  const rawLines = text.split('\n').map(l => l.trim()).filter(Boolean);
  // Pre-pass: merge multi-line screenplay-format dialogue. Two cue flavors:
  //   (a) Orphan cue (no colon):       FRANK / DANTE (V.O.)
  //   (b) Name-colon, empty tail:      Clara: / Mrs. Vale: / **Sofia:**
  // Both merge with the next non-empty line (skipping a single parenthetical
  // tone note) so `_lineDuration` bills the payload as dialogue (words/wps)
  // instead of mis-classifying it as prose action.
  const _isCueLineForMerge = (s) => {
    const stripped = _stripMarkdownWrappers(s);
    if (_isOrphanSpeakerCue(stripped)) return true;
    const m = stripped.match(/^([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-'.#]{0,40})\s*[:：]\s*(.*)$/);
    if (!m || !/^[A-ZА-ЯЁ]/.test(m[1])) return false;
    const tail = (m[2] || '').replace(/\([^)]*\)/g, ' ').replace(/\[[^\]]*\]/g, ' ').replace(/\*[^*]*\*/g, ' ').trim();
    return tail === '';
  };
  const mergedLines = [];
  for (let i = 0; i < rawLines.length; i++) {
    const cur = rawLines[i];
    if (_isCueLineForMerge(cur)) {
      // Optional parenthetical beat between cue and dialogue
      let parenBeat = '';
      let j = i + 1;
      if (j < rawLines.length && /^\([^)]+\)\.?$/.test(rawLines[j])) {
        parenBeat = ' ' + rawLines[j];
        j++;
      }
      // Next non-empty line is the dialogue payload — unless it's ANOTHER
      // speaker cue or a scene heading (then current cue is a stray, fall
      // through to normal action billing).
      if (j < rawLines.length
          && !_isCueLineForMerge(rawLines[j])
          && !_matchSceneHeading(rawLines[j]).match) {
        const curStripped = _stripMarkdownWrappers(cur);
        // If cue already ends with a colon (Clara: / Mrs. Vale:), don't add
        // another — just append the payload. Otherwise insert ':'.
        const sep = /[:：]\s*$/.test(curStripped) ? '' : ':';
        mergedLines.push(curStripped + sep + parenBeat + ' ' + rawLines[j]);
        i = j;  // consume both (or three with paren) lines
        continue;
      }
    }
    mergedLines.push(cur);
  }
  let voSec = 0, dialogSec = 0, actionSec = 0;
  let hasVo = false;
  for (const l of mergedLines) {
    const dur = (typeof _lineDuration === 'function') ? _lineDuration(l) : 0;
    if (!dur) continue;
    if (_isVoiceOverLine(l))      { voSec += dur; hasVo = true; }
    else if (_isDialogueLine(l))  { dialogSec += dur; }
    else                          { actionSec += dur; }
  }
  const contentSec = Math.max(voSec, actionSec) + dialogSec;
  const buffer = (hasVo && dialogSec === 0) ? 0.6 : 1.5;
  const target = Math.ceil(contentSec + buffer);
  return Math.max(5, Math.min(15, target || 5));
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
// Manual force-setter exposed for console debugging. Type into DevTools:
//    forceSdDuration(12)
// and watch the [sd-duration WRITE] log fire. If the slider visually changes,
// listener-based code is working. If not, something else is rendering the UI.
window.forceSdDuration = function(n) {
  const el = document.getElementById('sd-duration');
  const label = document.getElementById('sd-duration-val');
  if (!el) { console.error('sd-duration element not found'); return false; }
  const v = Math.max(5, Math.min(15, Math.round(Number(n) || 5)));
  console.log('[forceSdDuration] writing', v, 'to slider (currently', el.value, ')');
  el.value = String(v);
  if (label) label.textContent = v + 'с';
  el.dataset.autoVal = String(v);
  el.dispatchEvent(new Event('input',  { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  console.log('[forceSdDuration] after writes, slider reads', el.value, '/ label reads', label?.textContent);
  return el.value;
};

function sendSegmentToSeedance(segText) {
  const ta = document.getElementById('sd-chunk-text');
  if (!ta) {
    showToast('⚠ Seedance-панель не найдена на этом эпизоде', 4000);
    return;
  }
  // Lock the slider FIRST, before the textarea even gets the new text. This
  // way listener-driven auto-set (which fires on textarea `input`) sees the
  // dataset.autoVal already matching our recDur and stays out of the way.
  const durEl  = document.getElementById('sd-duration');
  const durVal = document.getElementById('sd-duration-val');
  let recDur = null;
  try {
    recDur = _estimateChunkDurationSec(segText);
  } catch (e) {
    console.warn('[sendSegmentToSeedance] duration estimate threw:', e);
  }
  console.log('[sendSegmentToSeedance] segText length=' + segText.length + ' recDur=' + recDur + ' durEl=' + !!durEl);
  if (durEl && recDur != null && Number.isFinite(recDur) && recDur > 0) {
    // Clamp to the slider's min/max so the browser doesn't silently reject.
    const min = parseInt(durEl.min || '5', 10) || 5;
    const max = parseInt(durEl.max || '15', 10) || 15;
    const v = Math.max(min, Math.min(max, Math.round(recDur)));
    durEl.value = String(v);
    durEl.setAttribute('value', String(v));   // some browsers need attr too
    durEl.dataset.autoVal = String(v);
    if (durVal) durVal.textContent = v + 'с';
    durEl.dispatchEvent(new Event('input',  { bubbles: true }));
    durEl.dispatchEvent(new Event('change', { bubbles: true }));
    console.log('[sendSegmentToSeedance] slider set to ' + v + ' (was=' + durEl.value + ' after=' + durEl.value + ')');
  }
  // Now set the textarea. Any cascading input listener that sets durEl will
  // see dataset.autoVal === recDur and bail out of the override-respect path.
  ta.value = segText;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  // Re-affirm slider once more AFTER the textarea event chain, defensively —
  // covers any listener that decided to write durEl despite our handshake.
  if (durEl && recDur != null && Number.isFinite(recDur) && recDur > 0) {
    const min = parseInt(durEl.min || '5', 10) || 5;
    const max = parseInt(durEl.max || '15', 10) || 15;
    const v = Math.max(min, Math.min(max, Math.round(recDur)));
    if (String(durEl.value) !== String(v)) {
      console.warn('[sendSegmentToSeedance] slider drifted after textarea input; restoring', durEl.value, '→', v);
      durEl.value = String(v);
      durEl.dataset.autoVal = String(v);
      if (durVal) durVal.textContent = v + 'с';
      durEl.dispatchEvent(new Event('input',  { bubbles: true }));
      durEl.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  document.getElementById('seedance-panel')?.scrollIntoView({behavior:'smooth', block:'start'});
  setTimeout(() => ta.focus(), 350);
  const durMsg = recDur != null ? ` (⏱ ${recDur}с)` : '';
  showToast('✓ Сегмент отправлен в Сиданс' + durMsg + ' — нажми "⚙ Скомпоновать (LLM)"', 4000);
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
// Override key: (sceneIdx, autoSeg) uniquely identifies a segment-break
// across script re-parses. Anchor (first 60 chars) is kept as a sanity
// hint so an edited script can self-heal — if anchor diverges from the
// recorded one, the override silently drops on next render.
// Bug 2026-05-15: previously matched ONLY by anchor; in dialogue-heavy
// scripts where many segments start with the same speaker cue ("Ethan:",
// "Sofia:"), one merge click suppressed segment breaks on EVERY same-
// anchor line → the rest of the episode collapsed into one chunk.
function _findOverrideIdx(overrides, sceneIdx, autoSeg, anchor) {
  // Prefer exact (sceneIdx, autoSeg) match — that's the canonical key.
  let i = overrides.findIndex(o =>
    o && o.sceneIdx === sceneIdx && o.autoSeg === autoSeg
  );
  if (i >= 0) return i;
  // Legacy overrides recorded by anchor only — match if anchor agrees AND
  // the entry has no scene/autoSeg fields. This back-compat path only
  // fires for pre-2026-05-15 saved overrides.
  i = overrides.findIndex(o =>
    o && o.anchor === anchor
    && (o.sceneIdx === undefined && o.autoSeg === undefined)
  );
  return i;
}

async function toggleSegmentBreak(anchor, sceneIdx, autoSeg) {
  if (!anchor) return;
  const overrides = _segmentOverrides().slice();
  const idx = _findOverrideIdx(overrides, sceneIdx, autoSeg, anchor);
  if (idx < 0) {
    overrides.push({ anchor, sceneIdx, autoSeg, action: 'break' });
  } else if (overrides[idx].action === 'break') {
    overrides.splice(idx, 1);                      // toggle off
  } else {
    overrides[idx] = { anchor, sceneIdx, autoSeg, action: 'break' };
  }
  await _persistSegmentOverrides(overrides);
  _renderSceneViewBody();
}

async function toggleSegmentMerge(anchor, sceneIdx, autoSeg) {
  if (!anchor) return;
  const overrides = _segmentOverrides().slice();
  const idx = _findOverrideIdx(overrides, sceneIdx, autoSeg, anchor);
  if (idx < 0) {
    overrides.push({ anchor, sceneIdx, autoSeg, action: 'merge' });
  } else if (overrides[idx].action === 'merge') {
    overrides.splice(idx, 1);                      // toggle off
  } else {
    overrides[idx] = { anchor, sceneIdx, autoSeg, action: 'merge' };
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
  const segs = (typeof _autoCollectSegments === 'function') ? _autoCollectSegments() : [];
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

// Manual «✨ Авто-правка» button — applies the per-user
// «🎬 Автоматическая правка» instruction (Settings modal) to every main
// promptEn of the current episode in one Claude call. Mirrors what
// turbo Auto-mode does automatically as step 3/3.
async function applyAutoRevise(btn) {
  if (!S.episode) { showToast('⚠ Сначала открой эпизод', 3000); return; }
  const hasBatch = !!(S.episode?.batch_prompts && Object.keys(S.episode.batch_prompts).length);
  if (!hasBatch) {
    showToast('⚠ Сначала запусти batch-compose (📋 batch JSON или Auto-mode турбо)', 5000);
    return;
  }
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> правлю…'; }
  try {
    const resp = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/seedance/revise-batch`,
      {},
      { timeoutMs: 5 * 60 * 1000 }
    );
    // Refresh episode so any panel that reads batch_prompts shows new text.
    try {
      const fresh = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
      if (fresh) S.episode = fresh;
    } catch (_) {}
    showToast(`✓ Правка применена: ${resp.count}/${resp.expected} чанков`, 4000);
  } catch (e) {
    showToast('✗ Не удалось применить правку: ' + (e.message || e), 6000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
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

