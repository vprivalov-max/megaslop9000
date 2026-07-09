// ── Bulk-select for chunk cards ────────────────────────────────────────────
function sdToggleSelect(idx) {
  if (!SD.selected) SD.selected = new Set();
  // Normalize to Number so the Set is type-consistent — otherwise mixing
  // string/number variants of the same idx silently splits selection state.
  const n = Number(idx);
  if (SD.selected.has(n)) SD.selected.delete(n);
  else SD.selected.add(n);
  // Toggle visual class on the card
  const card = document.querySelector(`.sd-gen-card[data-idx="${n}"]`);
  if (card) card.classList.toggle('sd-card-selected', SD.selected.has(n));
  // Sync this card's CB to authoritative state (handles the case where the
  // user clicked the wrapping <label> twice or a re-render landed between
  // browser-native toggle and our handler).
  const cb = card && card.querySelector('input.sd-card-cb');
  if (cb) cb.checked = SD.selected.has(n);
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
  for (const c of list) SD.selected.add(Number(c.idx));
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

// One-click «Попробовать обойти модерацию» for a failed chunk. Fires the
// server-side driver that runs the FULL legitimate arsenal AUTOMATICALLY
// (re-describe action → aggressive re-describe + reframe → deep series-aware
// scene rewrite), resubmitting and waiting until the chunk passes — with NO
// evasion (никаких сеток/cartoon) and the voice kept. No manual composer step:
// progress + result show right on the chunk card via the normal poll loop.
async function sdPassModeration(idx, btn) {
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ обхожу…'; }
  try {
    await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/${idx}/pass-moderation`,
      {}
    );
    showToast('🛡 Провожу через модерацию — переписываю и пересабмичу, статус обновится сам…', 6000);
    if (typeof sdRefreshList === 'function') await sdRefreshList();
    // Start the shared singleton 8s poll loop so the passed video + live status
    // appear WITHOUT a manual page refresh. This loop only READS /poll (which
    // downloads finished videos + re-renders) and auto-stops when the chunk is
    // completed/failed. It CANNOT trigger a generation: the chunk carries
    // mod_driver_active=True, so the server's /poll skips all escalation — only
    // the single bounded driver thread submits (capped by the per-chunk limit +
    // 6-rung ladder + circuit breaker). No new loop, no $600 replay.
    if (typeof sdEnsurePoll === 'function') sdEnsurePoll();
  } catch (e) {
    showToast('✗ ' + (e.message || e), 6000);
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🛡 Попробовать обойти модерацию'; }
  }
}

// Reset the per-chunk lifetime submit counter so «Попробовать обойти модерацию»
// can run again — the server re-applies the same cap (8) from zero.
async function sdResetSubmitLimit(idx, btn) {
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳…'; }
  try {
    await api.post(`/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/${idx}/reset-submit-limit`, {});
    showToast('🔓 Лимит сброшен — снова доступно до 8 генераций на этот чанк', 5000);
    if (typeof sdRefreshList === 'function') await sdRefreshList();
  } catch (e) {
    showToast('✗ ' + (e.message || e), 6000);
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🔓 Сбросить лимит'; }
  }
}

async function sdReuse(idx) {
  const res = await api.get(`/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/list`);
  const c = (res.chunks || []).find(x => x.idx === idx);
  if (!c) return;
  document.getElementById('sd-chunk-text').value = c.chunk_text || '';
  document.getElementById('sd-prompt').value = c.prompt || '';
  // Recompute duration via the VO-aware estimator instead of inheriting the
  // stored value — legacy chunks generated before the VO-overlap fix had
  // inflated durations (VO + action summed). Form field stays editable, user
  // can override if needed.
  document.getElementById('sd-duration').value = c.chunk_text
    ? _estimateChunkDurationSec(c.chunk_text, { fallback: c.duration || 15 })
    : (c.duration || 15);
  document.getElementById('sd-resolution').value = c.resolution || '720p';
  document.getElementById('sd-mod-bypass').value = c.moderation_bypass || 'off';
  // Rebuild refs from stored descriptors. Must cover every kind that compose/
  // start can emit (char, loc, item, lastframe, cutframe) — otherwise reuse
  // for the heal-prompt flow drops the photo and the user sees "no photo" with
  // an unbound @ImageN tag.
  SD.refs = (c.refs || []).map(r => {
    let name = '', photoUrl = '';
    if (r.kind === 'char') {
      const ch = (S.series.characters || []).find(x => x.id === r.id);
      if (ch) {
        name = ch.name;
        if (r.outfit) {
          // Outfits sometimes get referenced by id in older data; fall back so
          // we don't silently lose the photo when the label was renamed.
          const o = (ch.outfits || []).find(o => o.label === r.outfit)
                 || (ch.outfits || []).find(o => o.id === r.outfit);
          if (o?.photo) photoUrl = `${assetUrl(o.photo)}`;
        }
        if (!photoUrl && ch.ref_images?.[0]) photoUrl = `${assetUrl(ch.ref_images[0])}`;
      }
    } else if (r.kind === 'loc') {
      const l = (S.series.locations || []).find(x => x.id === r.id);
      if (l) { name = l.name; if (l.ref_images?.[0]) photoUrl = `${assetUrl(l.ref_images[0])}`; }
    } else if (r.kind === 'item') {
      const it = (S.series.items || []).find(x => x.id === r.id);
      if (it) { name = it.name; if (it.ref_images?.[0]) photoUrl = `${assetUrl(it.ref_images[0])}`; }
    } else if (r.kind === 'lastframe') {
      // Last-frame ref: URL is already a public AVAI image — use it as the thumbnail too.
      name = r.name || `last frame · prev #${r.prev_idx ?? '?'}`;
      photoUrl = r.url || '';
    } else if (r.kind === 'cutframe') {
      name = r.name || `pre-cut frame${r.cut_index != null ? ' #' + r.cut_index : ''}`;
      photoUrl = r.url || '';
    }
    // Fallback: if we still have nothing useful but the stored descriptor
    // carries a remote URL (e.g. legacy chunks where the kind was an unknown
    // tag like 'url' / 'item' before this branch existed) — use it directly.
    if (!photoUrl && r.url) photoUrl = r.url;
    if (!name && r.name) name = r.name;
    return { ...r, name, photoUrl };
  });
  sdRenderRefs();
  showToast('↻ Параметры подставлены');
  document.getElementById('seedance-panel')?.scrollIntoView({behavior:'smooth', block:'start'});
}

async function sdPollOnce() {
  if (!S.episode || !S.seriesId) return null;
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/poll`, {}
    );
    // CRITICAL: kill switch check — show full-screen warning if tripped.
    // This runs on every poll tick so user sees the warning within ~8 sec
    // of the switch tripping, regardless of which page they're on. Also
    // auto-dismisses if server reports cleared (e.g. operator deleted the
    // file via shell or another tab clicked the clear button).
    if (res.avai_kill_switch) {
      if (res.avai_kill_switch.active) {
        _showAvaiKillSwitchOverlay(res.avai_kill_switch);
      } else {
        _dismissAvaiKillSwitchOverlay();
      }
    }
    const chunks = res.chunks || [];
    _sdNotifyTransitions(chunks);  // beep + toast on completed/failed transitions
    sdRenderList(chunks);
    // Manual-mode auto-retry: if any chunk has qc.status='fail' AND retry
    // budget left, fire a fresh start with QC-feedback hint appended. Skipped
    // when AUTO mode is running — auto-mode handles its own QC retry loop.
    if (!AUTO.active) {
      _qcAutoRetryManualMode(chunks).catch(e =>
        console.warn('[qc] manual auto-retry failed:', e));
    }
    return chunks;
  } catch (e) { return null; }
}

// Visual badge for a chunk card summarizing QC status. Returns '' for non-
// completed chunks (status badge already shown elsewhere). Colour codes:
//   gray   — QC pending (server bg run not finished yet)
//   green  — pass
//   yellow — retrying (fail with attempts<3)
//   red    — retry_exhausted (gave up, accepted as-is)
function _qcBadgeHTML(c) {
  if (c.status !== 'completed') return '';
  const qc = c.qc;
  if (!qc) {
    return '<div class="sd-card-qc" style="background:rgba(120,120,120,0.18);border:1px solid rgba(120,120,120,0.35);border-radius:4px;padding:3px 6px;margin-top:3px;color:#aaa;font-size:0.74rem">🔍 QC...</div>';
  }
  const fails = qc.fails || [];
  if (qc.status === 'pass') {
    return '<div class="sd-card-qc" style="background:rgba(34,197,94,0.10);border:1px solid rgba(34,197,94,0.35);border-radius:4px;padding:3px 6px;margin-top:3px;color:#22c55e;font-size:0.74rem">✓ QC pass</div>';
  }
  if (qc.status === 'retry_exhausted') {
    return `<div class="sd-card-qc" style="background:rgba(239,68,68,0.10);border:1px solid rgba(239,68,68,0.40);border-radius:4px;padding:3px 6px;margin-top:3px;color:#ef4444;font-size:0.74rem" title="${esc(fails.join(', '))}">⛔ QC ${qc.attempts}/3 exhausted (${esc(fails.slice(0, 2).join(', '))})</div>`;
  }
  if (qc.status === 'fail') {
    return `<div class="sd-card-qc" style="background:rgba(251,191,36,0.10);border:1px solid rgba(251,191,36,0.40);border-radius:4px;padding:3px 6px;margin-top:3px;color:#fbbf24;font-size:0.74rem" title="${esc(fails.join(', '))}">🔁 QC retry ${qc.attempts}/3 (${esc(fails.slice(0, 2).join(', '))})</div>`;
  }
  return '';
}

// Build a short hint to append to the prompt when retrying a chunk whose
// QC failed. Each failure type gets a targeted instruction so the next
// generation has the best chance of passing. Shared between auto-mode and
// manual-mode retries.
function _qcBuildPromptHint(fails) {
  if (!fails || !fails.length) return '';
  const bits = [];
  if (fails.some(f => f === 'prompt_non_english' || f.startsWith('lang:'))) {
    bits.push(
      'ALL spoken dialogue MUST be performed in clear standard American '
      + 'English (General American accent). No Russian, no other language. '
      + 'Translate any non-English lines from the prompt into natural English '
      + 'before vocalizing.'
    );
  }
  if (fails.some(f => f.startsWith('grid:'))) {
    bits.push(
      'Final output MUST be a single continuous full-frame composition. '
      + 'NO visible grid lines, NO cell borders, NO tiling artifacts, NO '
      + 'panel separators, NO visible seams between regions. Any internal '
      + 'moderation-bypass grid must be fully removed from the rendered output.'
    );
  }
  if (fails.some(f => f.startsWith('subs:'))) {
    bits.push(
      'NO subtitles, NO captions, NO burned-in text overlay anywhere in '
      + 'the frame. Speech is delivered as audio only — there are no visible '
      + 'subtitles on the bottom of the screen or anywhere else.'
    );
  }
  if (!bits.length) return '';
  return '\n\n[QC RETRY HINTS — previous take failed on: ' + fails.join(', ') + ']\n'
    + bits.map(b => '• ' + b).join('\n');
}

// In-flight set guards against duplicate retry posts when poll ticks fire
// faster than a retry can complete. Keyed by `${epNumber}#${chunkText}` —
// chunkIdx changes each retry (new chunk created), so keying by idx never
// blocked the re-fire. ChunkText is stable across retries of same segment.
const _QC_RETRY_INFLIGHT = new Set();

// ─── AVAI Kill-Switch Full-Screen Overlay ──────────────────────────────────
// When the server's circuit breaker auto-trips (>20 AVAI submits in 5 min),
// show an UNDISMISSABLE full-screen red overlay so the user immediately sees
// that something went wrong and AVAI submits are blocked.
//
// Real prod incident 2026-05-19: a server bug caused ~hundreds of duplicate
// AVAI submits over hours costing ~$600. User didn't notice until checking
// AVAI dashboard. This overlay makes it impossible to miss.
let _AVAI_KS_SHOWN = false;
function _showAvaiKillSwitchOverlay(state) {
  // De-dupe: render the overlay once even if poll keeps firing.
  if (_AVAI_KS_SHOWN) {
    // Update timestamp in case state changed (e.g. reason updated)
    const reasonEl = document.getElementById('avai-ks-reason');
    if (reasonEl && state.reason) reasonEl.textContent = state.reason;
    return;
  }
  _AVAI_KS_SHOWN = true;
  // Stop any background polls / autonomous loops dead — the server will refuse
  // anyway, but no point asking.
  try { if (typeof AUTO !== 'undefined' && AUTO.active) AUTO.active = false; } catch (e) {}
  try { if (typeof RANGE !== 'undefined') RANGE.active = false; } catch (e) {}
  try { if (typeof SD !== 'undefined' && SD.pollTimer) { clearInterval(SD.pollTimer); SD.pollTimer = null; } } catch (e) {}

  const sinceStr = state.since
    ? new Date(state.since * 1000).toLocaleString()
    : 'недавно';
  const overlay = document.createElement('div');
  overlay.id = 'avai-ks-overlay';
  overlay.style.cssText = `
    position: fixed; inset: 0; z-index: 99999;
    background: rgba(120,0,0,0.96);
    display: flex; align-items: center; justify-content: center;
    padding: 24px;
    backdrop-filter: blur(12px);
  `;
  overlay.innerHTML = `
    <div style="max-width: 720px; background: #1a0000; border: 3px solid #ff3030;
                border-radius: 16px; padding: 32px; color: #fff; font-family: system-ui;
                box-shadow: 0 20px 60px rgba(0,0,0,0.8);">
      <div style="font-size: 80px; text-align: center; line-height: 1; margin-bottom: 16px">🚨</div>
      <h1 style="text-align: center; color: #ff4040; margin: 0 0 16px; font-size: 1.7rem; font-weight: 900">
        AVAI KILL SWITCH СРАБОТАЛ
      </h1>
      <p style="font-size: 1.05rem; line-height: 1.5; margin: 0 0 16px; color: #fbd0d0">
        <b>Сервер заблокировал ВСЕ AVAI submits</b> потому что было отправлено
        <b>больше 20 генераций за 5 минут</b>. Это автоматическая защита от
        runaway-баг'ов после прод-инцидента на $600.
      </p>
      <div style="background: #2a0000; border-left: 4px solid #ff3030; padding: 12px 16px;
                  margin: 16px 0; font-family: ui-monospace, monospace; font-size: 0.85rem;
                  color: #ffaaaa; white-space: pre-wrap; max-height: 200px; overflow-y: auto;">
        <div style="font-weight: 700; margin-bottom: 4px">Причина:</div>
        <div id="avai-ks-reason">${(state.reason || '(нет деталей)').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]))}</div>
        <div style="margin-top: 8px; opacity: 0.7">Сработал: ${sinceStr}</div>
      </div>
      <h2 style="color: #ffcc40; font-size: 1.1rem; margin: 20px 0 8px">Что делать:</h2>
      <ol style="margin: 0 0 16px; padding-left: 20px; line-height: 1.6; color: #fbd0d0">
        <li>Проверь AVAI dashboard — посмотри сколько submits улетело и оцени ущерб.</li>
        <li>Открой Flask лог: <code style="background:#2a0000;padding:2px 6px;border-radius:3px">tail -100 /tmp/series-writer.log | grep avai-cb</code> — увидишь что именно триггерило.</li>
        <li>Когда разберёшься — нажми кнопку «Снять kill switch» ниже ИЛИ удали файл вручную: <code style="background:#2a0000;padding:2px 6px;border-radius:3px">rm "${(state.file_path || '').replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]))}"</code></li>
      </ol>
      <div style="display: flex; gap: 12px; margin-top: 24px; justify-content: center">
        <button onclick="_avaiKillSwitchClear()" style="background: #ff3030; color: white;
                border: none; padding: 12px 24px; border-radius: 8px; font-weight: 700;
                font-size: 1rem; cursor: pointer">
          🔓 Снять kill switch (после разбирательства)
        </button>
        <button onclick="window.location.reload()" style="background: #444; color: white;
                border: 1px solid #666; padding: 12px 24px; border-radius: 8px;
                font-weight: 600; font-size: 1rem; cursor: pointer">
          ↻ Перезагрузить страницу
        </button>
      </div>
      <div style="margin-top: 20px; padding-top: 16px; border-top: 1px solid #4a0000;
                  font-size: 0.78rem; opacity: 0.6; text-align: center">
        Лимит: больше 20 AVAI submits за 5 минут = автоматический kill switch.
        Без этой защиты loop-баг 2026-05-19 сжёг $600.
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  // Also try a system beep
  try { Sounds && Sounds.playError && Sounds.playError(); } catch (e) {}
}

async function _avaiKillSwitchClear() {
  if (!confirm('Снять kill switch? Делай это только после того как разобрался с причиной.\n\nЕсли источник проблемы не устранён — kill switch снова сработает через несколько секунд.')) {
    return;
  }
  try {
    await api.del('/api/avai/kill-switch');
    const overlay = document.getElementById('avai-ks-overlay');
    if (overlay) overlay.remove();
    _AVAI_KS_SHOWN = false;
    location.reload();
  } catch (e) {
    alert('Не удалось снять kill switch: ' + (e.message || e));
  }
}

// Global poller — checks kill switch state every 10s regardless of which page
// the user is on. Catches the case where Seedance poll isn't running (e.g.
// user is on the script page) but a background process tripped the switch.
// ALSO auto-dismisses the overlay if server reports active=false (e.g. file
// was deleted externally or via the clear button on another tab).
async function _avaiKillSwitchGlobalPoll() {
  try {
    const res = await fetch('/api/avai/kill-switch', { credentials: 'include' });
    if (res.ok) {
      const state = await res.json();
      if (state.active) {
        _showAvaiKillSwitchOverlay(state);
      } else {
        _dismissAvaiKillSwitchOverlay();
      }
    }
  } catch (e) { /* ignore network blips */ }
}

function _dismissAvaiKillSwitchOverlay() {
  const overlay = document.getElementById('avai-ks-overlay');
  if (overlay) overlay.remove();
  _AVAI_KS_SHOWN = false;
}

setInterval(_avaiKillSwitchGlobalPoll, 10_000);
// Also fire once at page load so user sees overlay immediately on refresh
window.addEventListener('load', () => setTimeout(_avaiKillSwitchGlobalPoll, 500));

// Auto-retry policy (re-enabled 2026-05-19 with circuit breaker as safety net):
//   • Trigger: completed chunk with qc.status='fail' AND qc.attempts < 3
//   • Max: 2 retries per chunk (server-side QC_MAX_RETRIES=3 = 1 original + 2 retries)
//   • Guards: 5-min in-flight TTL keyed by chunk_text, skip if a sibling
//     submission for the same chunk_text is already running
//   • Hard ceiling: AVAICircuitBreaker (server) enforces ≤20 submits per 5 min,
//     auto-trips kill switch on excess. Worst-case bug damage ≤$12.
//   • On retry_exhausted (2 retries also failed): server marks chunk as such,
//     frontend fires voice alert via _sdNotifyTransitions.
//
// Real prod incident 2026-05-19 ($230 burn): previous version used 10s in-flight
// guard keyed by chunk idx — too short, wrong key, fired retries every 8s for
// hours. Fixed by chunk_text key + 5-min TTL + breaker safety net.
async function _qcAutoRetryManualMode(chunks) {
  return _qcAutoRetryStorm(chunks);
}

async function _qcAutoRetryStorm(chunks) {
  if (!chunks || !chunks.length) return;
  const textCounts = {};
  const runningTexts = new Set();
  for (const c of chunks) {
    const k = (c.chunk_text || '').trim();
    if (!k) continue;
    textCounts[k] = (textCounts[k] || 0) + 1;
    // Any in-flight sibling for the same chunk_text means a retry is
    // already running. Don't pile on.
    if (c.status === 'submitted' || c.status === 'running' ||
        c.status === 'queued'    || c.status === 'processing') {
      runningTexts.add(k);
    }
  }
  for (const c of chunks) {
    if (c.status !== 'completed') continue;
    const qc = c.qc;
    if (!qc || qc.status !== 'fail') continue;
    // Server gates the global cap via qc.status='retry_exhausted' when
    // attempts >= QC_MAX_RETRIES (3). Frontend just acts on 'fail' status.
    // We're seeing 'fail' here → server says retry is allowed.
    const txt = (c.chunk_text || '').trim();
    if (!txt) continue;
    if (runningTexts.has(txt)) continue;       // sibling already running
    if ((textCounts[txt] || 0) >= 3) continue; // 3 attempts total (1 original + 2 retries) reached
    const key = `${S.episode.number}#${txt}`;
    if (_QC_RETRY_INFLIGHT.has(key)) continue;
    _QC_RETRY_INFLIGHT.add(key);
    try {
      const hint = _qcBuildPromptHint(qc.fails || []);
      const prompt = (c.prompt || '') + hint;
      showToast(`🔁 QC retry #${c.idx} (${(qc.fails || []).slice(0, 2).join(', ')}) — попытка ${(qc.attempts||0) + 1}/3`, 5000);
      await api.post(
        `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/start`,
        {
          prompt,
          chunk_text: c.chunk_text,
          duration: c.duration || c.durationSec || 15,
          resolution: c.resolution || '480p',
          moderation_bypass: c.moderation_bypass || 'off',
          model: c.model || 'reference-fast',
          // Propagate canonical script position so retries get the «v2/v3»
          // label rather than orphan «доп.N». Same fix as sdRetry (manual
          // retry button) — auto-retry was missing this. User complaint
          // 2026-05-19: auto-retries showed as «#0 доп.1» instead of «#0 v2».
          script_order: (typeof c.script_order === 'number') ? c.script_order : null,
          sceneIdx: (typeof c.sceneIdx === 'number') ? c.sceneIdx : null,
          segIdx: (typeof c.segIdx === 'number') ? c.segIdx : null,
          durationSec: c.durationSec || c.duration || 15,
          refs: (c.refs || []).map(r => ({
            kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
            source: r.source, prev_idx: r.prev_idx, name: r.name,
            cut_index: r.cut_index, cut_time: r.cut_time,
          })),
        }
      );
    } catch (e) {
      console.warn(`[qc] manual retry post for chunk ${c.idx} failed:`, e);
    } finally {
      // 5-minute TTL — enough for a Seedance chunk to actually finish so
      // the next poll sees the new state. 10s was way too short — the
      // original chunk still showed qc.fail and re-triggered.
      setTimeout(() => _QC_RETRY_INFLIGHT.delete(key), 300_000);
    }
  }
}

function sdEnsurePoll() {
  if (SD.pollTimer) return;
  const tick = async () => {
    const chunks = await sdPollOnce();
    // «pending» = generation not final OR QC still running / mid-retry.
    // We keep polling so background QC + auto-retry are visible to the UI.
    const anyPending = (chunks || []).some(c => {
      if (c.status !== 'completed' && c.status !== 'failed') return true;
      if (c.status !== 'completed') return false;
      const qs = c.qc?.status;
      // No qc entry yet → QC hasn't run; we expect a server-side bg run.
      // qc.status='fail' → manual-mode retry will be triggered.
      // qc.status='pass'/'retry_exhausted' → terminal, can stop polling.
      return qs == null || qs === 'fail';
    });
    const st = document.getElementById('sd-poll-status');
    if (st) {
      st.textContent = anyPending
        ? ((chunks || []).some(c => c.status === 'completed' && (c.qc?.status == null || c.qc?.status === 'fail'))
            ? '🔍 QC + ретраи...'
            : '⏳ ждём генерации...')
        : '';
    }
    if (!anyPending) {
      clearInterval(SD.pollTimer);
      SD.pollTimer = null;
    }
  };
  SD.pollTimer = setInterval(tick, 8000);
  tick();
}

// Recompute the duration the chunk_text would naturally need, clamped to
// Seedance's 5-15s window. Delegates to `_estimateChunkDurationSec` so the
// auto-set slider value matches the badge shown next to each segment AND
// the Seedance API target — single source of truth across UI, manual paste,
// segmentation, and the compose→generate handoff.
function _sdRecommendedDurationFromChunkText(text) {
  if (!text || !text.trim()) return null;
  return _estimateChunkDurationSec(text);
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
  // DIAGNOSTIC: trace every write to sd-duration.value with stack trace, once.
  // Drop this once the duration-slider behaviour is confirmed stable.
  try {
    const _sliderEl = document.getElementById('sd-duration');
    if (_sliderEl && !_sliderEl._sdDbgProxyInstalled) {
      const proto = Object.getPrototypeOf(_sliderEl);
      const desc = Object.getOwnPropertyDescriptor(proto, 'value') ||
                   Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      if (desc && desc.set && desc.get) {
        Object.defineProperty(_sliderEl, 'value', {
          configurable: true,
          enumerable: true,
          get() { return desc.get.call(this); },
          set(v) {
            const before = desc.get.call(this);
            desc.set.call(this, v);
            const after = desc.get.call(this);
            // Capture caller via Error().stack — shows which function wrote it.
            const stack = (new Error()).stack || '';
            const caller = stack.split('\n').slice(2, 5).join(' | ').replace(/\s+at\s+/g, ' ');
            console.log('[sd-duration WRITE] ' + before + ' → ' + v + ' (final=' + after + ')  ← ' + caller);
          },
        });
        _sliderEl._sdDbgProxyInstalled = true;
        console.log('[sd-duration DEBUG] value-setter proxy installed. Build:', new Date().toISOString());
      }
    }
  } catch (e) {
    console.warn('[sd-duration DEBUG] proxy install failed:', e);
  }
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
