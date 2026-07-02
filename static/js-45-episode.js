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

