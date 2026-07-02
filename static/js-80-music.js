// ════════════════════════════════════════════════════════════════════════════
// 🎵 MUSIC — ElevenLabs per-scene music UI
// ════════════════════════════════════════════════════════════════════════════
// State cache for music poll. Keyed by `${sid}/${num}` → latest poll response.
const MUSIC = { last: {}, pollTimers: {} };

function _musicKey() {
  return `${S.seriesId}/${S.episode?.number}`;
}

async function seriesToggleMusic(input) {
  if (!S.series || !S.seriesId) return;
  const enabled = !!input.checked;
  S.series.settings = S.series.settings || {};
  S.series.settings.enable_music = enabled;
  try {
    S.series = await api.put(`/api/series/${S.seriesId}`, { settings: S.series.settings });
    showToast(enabled
      ? '🎵 Авто-музыка включена — новые эпизоды получат трек после сборки'
      : '🔇 Авто-музыка отключена — старые треки не трогаем', 4000);
  } catch (e) {
    input.checked = !enabled;
    showToast('Не удалось сохранить настройку: ' + (e.message || e), 4000);
  }
}

function _initSeriesMusicCheckbox() {
  const el = document.getElementById('series-enable-music');
  if (!el || !S.series) return;
  const cur = S.series?.settings?.enable_music;
  // Default ON if not explicitly set to false.
  el.checked = cur !== false;
}

function _sdUpdateMusicUI(chunks, music) {
  const btn = document.getElementById('sd-music-btn');
  const regen = document.getElementById('sd-music-regen-btn');
  const dl = document.getElementById('sd-music-dl-link');
  const counter = document.getElementById('sd-music-counter');
  if (!btn) return;

  const available = (music && music.available_scenes) || [];
  const scenes = (music && music.music_scenes) || [];
  const totalScenes = available.length;
  const byIdx = new Map(scenes.map(s => [s.sceneIdx, s]));
  const completed = available.filter(a => byIdx.get(a.sceneIdx)?.status === 'completed').length;
  const generating = available.filter(a => byIdx.get(a.sceneIdx)?.status === 'generating' || byIdx.get(a.sceneIdx)?.status === 'pending').length;
  const failed = available.filter(a => byIdx.get(a.sceneIdx)?.status === 'failed').length;

  // Show the «Сгенерировать музыку» button whenever the episode has at least
  // one completed chunk — even if the chunks lack sceneIdx (legacy data).
  // Clicking will hit the backend which returns a helpful error toast.
  const completedChunks = (chunks || []).filter(c => c.status === 'completed' && c.video_path).length;

  if (totalScenes === 0) {
    if (completedChunks > 0) {
      btn.style.display = '';
      btn.disabled = false;
      btn.innerHTML = '🎵 Сгенерировать музыку';
      btn.title = 'Сгенерировать инструментальную музыку под каждую сцену. Если кнопка ругается — старые чанки без sceneIdx, нужно их перегенерить.';
    } else {
      btn.style.display = 'none';
    }
    regen.style.display = 'none';
    dl.style.display = 'none';
    counter.style.display = 'none';
    return;
  }

  // Show «🎵 Сгенерировать музыку» if at least one scene is missing or failed.
  btn.style.display = (completed < totalScenes) ? '' : 'none';
  if (generating > 0) {
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span> Музыка ${completed}/${totalScenes}`;
  } else {
    btn.disabled = false;
    btn.innerHTML = '🎵 Сгенерировать музыку' + (failed > 0 ? ` · ⚠ ${failed} failed` : '');
  }

  regen.style.display = (completed > 0) ? '' : 'none';
  dl.style.display = (completed > 0) ? '' : 'none';
  if (dl.style.display) {
    dl.href = `/api/series/${S.seriesId}/episodes/${S.episode.number}/music/download`;
  }
  counter.style.display = '';
  counter.textContent = `🎵 ${completed}/${totalScenes}` + (generating > 0 ? ` · ${generating} gen` : '') + (failed > 0 ? ` · ${failed} fail` : '');

  const playBtn = document.getElementById('sd-music-play-btn');
  if (playBtn) {
    if (completed > 0) {
      playBtn.style.display = '';
      // The previewed track is the concatenated /music/download stream. The
      // signature changes whenever any scene was regenerated, so the audio
      // element src needs a cache-buster keyed on the latest generated_at.
      const latestGen = Math.max(0, ...(scenes || []).filter(s => s.generated_at).map(s => s.generated_at));
      playBtn.dataset.cacheKey = String(latestGen);
    } else {
      playBtn.style.display = 'none';
      _sdStopMusicPreview();
    }
  }
}

// Single hidden <audio> shared across the toolbar. Toggled by sdToggleMusicPreview.
// Plays the concatenated /music/download stream (all scenes back-to-back).
let _SD_MUSIC_AUDIO = null;
function _sdGetMusicAudio() {
  if (_SD_MUSIC_AUDIO) return _SD_MUSIC_AUDIO;
  const a = document.createElement('audio');
  a.preload = 'none';
  a.style.display = 'none';
  a.addEventListener('ended', _sdStopMusicPreview);
  a.addEventListener('pause', () => _sdUpdateMusicPlayBtn(false));
  a.addEventListener('play',  () => _sdUpdateMusicPlayBtn(true));
  document.body.appendChild(a);
  _SD_MUSIC_AUDIO = a;
  return a;
}

function _sdUpdateMusicPlayBtn(playing) {
  const btn = document.getElementById('sd-music-play-btn');
  if (!btn) return;
  btn.innerHTML = playing ? '⏸' : '▶';
  btn.title = playing ? 'Пауза' : 'Прослушать музыку этой серии (все сцены подряд)';
}

function _sdStopMusicPreview() {
  if (!_SD_MUSIC_AUDIO) return;
  try { _SD_MUSIC_AUDIO.pause(); } catch {}
  try { _SD_MUSIC_AUDIO.currentTime = 0; } catch {}
  _sdUpdateMusicPlayBtn(false);
}

function sdToggleMusicPreview(btn) {
  if (!S.seriesId || !S.episode) return;
  const audio = _sdGetMusicAudio();
  const url = `/api/series/${S.seriesId}/episodes/${S.episode.number}/music/download?v=${btn?.dataset?.cacheKey || ''}`;
  // First play, or src changed (regen happened) → set src and play.
  if (audio.paused) {
    if (audio.src !== url) {
      try { audio.src = url; } catch {}
    }
    audio.play().catch(e => {
      showToast('Не удалось запустить плеер: ' + (e.message || e), 4000);
      _sdUpdateMusicPlayBtn(false);
    });
  } else {
    audio.pause();
  }
}

async function _sdRefreshMusic() {
  if (!S.seriesId || !S.episode) return;
  try {
    const res = await api.get(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/music/poll`
    );
    MUSIC.last[_musicKey()] = res;
    _sdUpdateMusicUI(SD._lastChunks || [], res);
    return res;
  } catch (e) { return null; }
}

function _sdEnsureMusicPoll() {
  const key = _musicKey();
  if (MUSIC.pollTimers[key]) return;
  MUSIC.pollTimers[key] = setInterval(async () => {
    if (`${S.seriesId}/${S.episode?.number}` !== key) {
      clearInterval(MUSIC.pollTimers[key]);
      delete MUSIC.pollTimers[key];
      return;
    }
    const res = await _sdRefreshMusic();
    // Stop polling once nothing is in flight.
    const inFlight = (res?.music_scenes || []).some(s => s.status === 'generating' || s.status === 'pending');
    if (!inFlight) {
      clearInterval(MUSIC.pollTimers[key]);
      delete MUSIC.pollTimers[key];
    }
  }, 8000);
}

async function sdGenerateMusic(btn) {
  if (!S.seriesId || !S.episode) { showToast('Открой серию'); return; }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Запуск...';
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/music/generate`,
      {}
    );
    if (r.error) throw new Error(r.error);
    showToast(`▶ Музыка стартовала на ${r.scenes?.length || 0} сценах — ~30-90с на сцену`, 5000);

    // Optimistic UI: pre-fill cache so the counter switches to «🎵 0/N · gen»
    // immediately instead of waiting for the first poll round-trip.
    const key = _musicKey();
    if (!MUSIC.last[key]) MUSIC.last[key] = { music_scenes: [], available_scenes: [] };
    const cache = MUSIC.last[key];
    (r.scenes || []).forEach(s => {
      cache.music_scenes = (cache.music_scenes || []).filter(x => x.sceneIdx !== s.sceneIdx);
      cache.music_scenes.push({ sceneIdx: s.sceneIdx, status: 'pending' });
    });
    _sdUpdateMusicUI(SD._lastChunks || [], cache);

    _sdEnsureMusicPoll();
    _sdRefreshMusic();
  } catch (e) {
    showToast('Ошибка запуска музыки: ' + (e.message || e), 6000);
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

async function sdRegenMusicDialog() {
  if (!S.seriesId || !S.episode) return;
  const cur = MUSIC.last[_musicKey()] || await _sdRefreshMusic();
  const scenes = (cur?.available_scenes || []);
  if (!scenes.length) { showToast('Нет сцен для перегенерации', 3500); return; }

  document.getElementById('music-regen-modal')?.remove();
  const root = document.createElement('div');
  root.id = 'music-regen-modal';
  root.className = 'app-confirm-modal';
  const sceneOptions = scenes.map(s => {
    const m = (cur.music_scenes || []).find(x => x.sceneIdx === s.sceneIdx);
    const tag = m?.status === 'completed' ? '✓' : (m?.status === 'failed' ? '✗' : '·');
    return `<option value="${s.sceneIdx}">${tag} Сцена ${s.sceneIdx + 1} (~${Math.round(s.total_sec)}с, ${s.chunks} чанк${s.chunks === 1 ? '' : 'ов'})</option>`;
  }).join('');
  // Reuse the project's confirm-modal styling — same backdrop, same box.
  root.innerHTML = `
    <div class="app-confirm-backdrop"></div>
    <div class="app-confirm-box" role="dialog" aria-modal="true" style="max-width:520px">
      <div class="app-confirm-title">🎵 Перегенерировать музыку</div>
      <div class="app-confirm-body" style="text-align:left">
        <p style="color:var(--muted);font-size:0.88rem;margin:0 0 12px">Промпт переписывается под пожелание, генерится новый трек. Старый файл перезаписывается.</p>
        <label style="display:block;margin-bottom:6px;font-size:0.86rem">Сцена:</label>
        <select id="mr-scene" style="width:100%;padding:6px 8px;background:#1a1a1f;border:1px solid #333;border-radius:4px;color:#eee;margin-bottom:10px">
          ${sceneOptions}
        </select>
        <label style="display:block;margin-bottom:6px;font-size:0.86rem">Пожелание (на любом языке):</label>
        <textarea id="mr-hint" rows="3" placeholder="повеселей, подинамичнее, понапряжённее…" style="width:100%;padding:6px 8px;background:#1a1a1f;border:1px solid #333;border-radius:4px;color:#eee;resize:vertical;min-height:60px;font-family:inherit"></textarea>
      </div>
      <div class="app-confirm-actions">
        <button class="btn-ghost" data-act="cancel">Отмена</button>
        <button class="btn-accent" data-act="ok" autofocus>▶ Перегенерировать</button>
      </div>
    </div>`;
  document.body.appendChild(root);
  const close = () => { try { root.remove(); } catch {} };
  root.querySelector('[data-act="cancel"]').addEventListener('click', close);
  root.querySelector('.app-confirm-backdrop').addEventListener('click', close);
  root.querySelector('[data-act="ok"]').addEventListener('click', async () => {
    const sceneIdx = parseInt(root.querySelector('#mr-scene').value, 10);
    const hint = root.querySelector('#mr-hint').value.trim();
    close();

    // Optimistic UI: mark the scene as 'pending' in our cache so the toolbar
    // counter and «🎵 Сгенерировать музыку» button flip to spinner BEFORE the
    // first poll round-trip lands.
    const key = _musicKey();
    if (!MUSIC.last[key]) MUSIC.last[key] = { music_scenes: [], available_scenes: [] };
    const cache = MUSIC.last[key];
    cache.music_scenes = (cache.music_scenes || []).filter(s => s.sceneIdx !== sceneIdx);
    cache.music_scenes.push({ sceneIdx, status: 'pending', user_hint: hint });
    _sdUpdateMusicUI(SD._lastChunks || [], cache);

    showToast(`▶ Сцена ${sceneIdx + 1} перегенерируется` + (hint ? ` · «${hint}»` : ''), 5000);
    try {
      const r = await api.post(
        `/api/series/${S.seriesId}/episodes/${S.episode.number}/music/regenerate`,
        { sceneIdx, user_hint: hint }
      );
      if (r.error) throw new Error(r.error);
      _sdEnsureMusicPoll();
      // First poll right away so spinner reflects real backend state ASAP.
      _sdRefreshMusic();
    } catch (e) {
      showToast('Ошибка: ' + (e.message || e), 6000);
      // Revert optimistic state on error.
      cache.music_scenes = (cache.music_scenes || []).filter(s => s.sceneIdx !== sceneIdx);
      _sdUpdateMusicUI(SD._lastChunks || [], cache);
    }
  });
}

function _triggerHiddenDownload(url) {
  const a = document.createElement('a');
  a.href = url;
  // Force «save» semantics regardless of the server's Content-Disposition.
  // Without this attribute the browser sniffs video/mp4 from /assets/<...>.mp4
  // and opens an inline player in a new tab instead of downloading — user
  // reported «Скачать финал» suddenly previewing instead of saving after
  // the music-bundle handler replaced the original `<a download>` attribute.
  // Empty value = browser picks filename from URL last segment / CD header.
  a.download = '';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 800);
}

function downloadAssembledWithMusic(evt) {
  if (evt) evt.preventDefault();
  const link = document.getElementById('sd-assembled-link');
  if (!link || !S.episode?.assembled_path) return;
  const mp4Url = `/assets/${S.seriesId}/${S.episode.assembled_path}?v=${S.episode.assembled_at || ''}`;
  _triggerHiddenDownload(mp4Url);
  // Bundle music WAV if at least one scene completed.
  const music = MUSIC.last[_musicKey()];
  const hasMusic = (music?.music_scenes || []).some(s => s.status === 'completed');
  if (hasMusic) {
    setTimeout(() => _triggerHiddenDownload(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/music/download`
    ), 600);
  }
}

function downloadMusicOnly(evt) {
  if (evt) evt.preventDefault();
  if (!S.seriesId || !S.episode) return;
  _triggerHiddenDownload(`/api/series/${S.seriesId}/episodes/${S.episode.number}/music/download`);
}

async function sdRefreshList() {
  // Guard against the race where S.episode is set (e.g. by a prior route)
  // before S.seriesId — without this we hit /api/series/null/.../seedance/list
  // and log a 404 WARN. Pure UI refresh; safe to no-op until both are set.
  if (!S.episode || !S.seriesId) return;
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

// Chunk-list sort: 'chrono' (default, by script_order) | 'newest' (by idx
// DESC). Persisted in localStorage so the preference survives reloads and
// applies on every episode page.
function _sdGetSort() {
  try { return localStorage.getItem('sd_sort') === 'newest' ? 'newest' : 'chrono'; }
  catch { return 'chrono'; }
}
function sdSetSort(mode) {
  if (mode !== 'newest') mode = 'chrono';
  try { localStorage.setItem('sd_sort', mode); } catch {}
  // Re-render the strip from cached chunks — no network round-trip needed.
  if (SD._lastChunks) sdRenderList(SD._lastChunks);
}
function _sdSyncSortButtons() {
  const mode = _sdGetSort();
  const c = document.getElementById('sd-sort-chrono');
  const n = document.getElementById('sd-sort-newest');
  if (c) c.classList.toggle('active', mode === 'chrono');
  if (n) n.classList.toggle('active', mode === 'newest');
}

// Compute display labels for chunks. Three kinds:
//   • Regular (chunk has script_order N) → «#N+1» + «v2/v3» for retries
//   • Additional / orphan (no script_order, but chunk_text overlaps a known
//     parent segment) → «#N+1» + «доп.1/доп.2» appended to that parent
//   • True orphan (no script_order, no line-overlap match) → «#?idx»
// Orphan→parent attribution by counting how many trimmed non-empty lines of
// the orphan's chunk_text also appear in each parent's chunk_text. Parent
// with the most overlapping lines wins; ties go to the lower script_order.
function _sdComputeLabels(chunks) {
  // 1. Bucket by script_order — these are «regular» take-1 onwards entries.
  const byOrder = new Map();
  const orphans = [];
  for (const c of chunks) {
    if (typeof c.script_order === 'number') {
      if (!byOrder.has(c.script_order)) byOrder.set(c.script_order, []);
      byOrder.get(c.script_order).push(c);
    } else {
      orphans.push(c);
    }
  }
  for (const grp of byOrder.values()) {
    grp.sort((a, b) => (a.idx ?? 0) - (b.idx ?? 0) || (a.created_at ?? 0) - (b.created_at ?? 0));
  }
  // 2. Pre-compute line sets for every potential parent (one chunk per order
  //    is enough — pick the take-1 because retries share chunk_text).
  const parentLineSets = new Map();   // script_order → Set<string>
  for (const [order, group] of byOrder.entries()) {
    const head = group[0];
    const lines = (head?.chunk_text || '').split('\n')
      .map(l => l.trim()).filter(l => l.length >= 3);
    parentLineSets.set(order, new Set(lines));
  }
  // 3. Attribute each orphan to its best-matching parent script_order.
  const orphanByParent = new Map();   // parent_order → orphan chunks (creation order)
  const trulyOrphan = [];
  for (const o of orphans) {
    const oLines = (o.chunk_text || '').split('\n')
      .map(l => l.trim()).filter(l => l.length >= 3);
    if (!oLines.length) { trulyOrphan.push(o); continue; }
    let bestOrder = null, bestScore = 0;
    for (const [order, set] of parentLineSets.entries()) {
      let score = 0;
      for (const l of oLines) if (set.has(l)) score++;
      // Lower script_order wins ties so attribution is deterministic.
      if (score > bestScore || (score === bestScore && bestOrder !== null && order < bestOrder)) {
        bestScore = score; bestOrder = order;
      }
    }
    if (bestOrder !== null && bestScore > 0) {
      if (!orphanByParent.has(bestOrder)) orphanByParent.set(bestOrder, []);
      orphanByParent.get(bestOrder).push(o);
    } else {
      trulyOrphan.push(o);
    }
  }
  for (const grp of orphanByParent.values()) {
    grp.sort((a, b) => (a.idx ?? 0) - (b.idx ?? 0) || (a.created_at ?? 0) - (b.created_at ?? 0));
  }
  // 4. Emit labels.
  const labels = new Map();
  for (const [order, group] of byOrder.entries()) {
    group.forEach((c, takeIdx) => {
      labels.set(c.idx, {
        label: `#${order + 1}`,
        take: takeIdx > 0 ? `v${takeIdx + 1}` : '',
        order,
        takeNum: takeIdx + 1,
        isAdditional: false,
        parentOrder: order,
      });
    });
  }
  for (const [order, group] of orphanByParent.entries()) {
    group.forEach((c, addIdx) => {
      labels.set(c.idx, {
        label: `#${order + 1}`,
        take: `доп.${addIdx + 1}`,
        order,
        takeNum: addIdx + 1,
        isAdditional: true,
        parentOrder: order,
      });
    });
  }
  for (const c of trulyOrphan) {
    labels.set(c.idx, {
      label: `#?${c.idx}`, take: '',
      order: Infinity, takeNum: 1,
      isAdditional: true, parentOrder: null,
    });
  }
  return labels;
}

// Sort key for a chunk in chronological mode. Regular chunks of order N
// sit first, their retries (v2, v3) after, then additionals (доп.1, доп.2)
// of the same parent_order. Then the next parent_order, and so on. Truly
// orphan chunks (no parent overlap) drop to the end.
function _sdChronoKey(chunk, labelInfo) {
  const li = labelInfo || {};
  return [
    li.parentOrder ?? Infinity,
    li.isAdditional ? 1 : 0,
    li.takeNum ?? 0,
    chunk.idx ?? 0,
  ];
}

// Cache-bust the chunk's video URL with the chunk's created_at timestamp
// (or current time as fallback). After a delete+regen, the new chunk reuses
// the same idx (so the same filename `seedance_ep001_chunk000.mp4`), and
// without a cache-buster the browser keeps serving the OLD video from its
// HTTP cache — including footage of characters that have since been
// removed from the series. User-reported on «My Roommate From Craigslist…».
function _chunkVideoUrl(c) {
  if (!c || !c.video_path) return '';
  const bust = c.created_at || c.completed_at || Math.floor(Date.now() / 1000);
  return `${assetUrl(c.video_path)}?v=${bust}`;
}

function _sdCardHTML(c, labelInfo) {
  const stCls = `sd-status-${c.status || 'pending'}`;
  const videoUrl = _chunkVideoUrl(c);
  const cost = c.cost != null ? `$${Number(c.cost).toFixed(2)}` : '';
  let placeholderText;
  if (c.status === 'failed') placeholderText = '✗ failed';
  else if (c.status === 'submitting') placeholderText = '📤 отправляю…';
  else if (c.progress != null) placeholderText = c.progress + '%';
  else placeholderText = '⏳ генерируется';
  const isSelected = SD.selected && SD.selected.has(c.idx);
  const canRetry = !!(c.prompt && (c.refs || []).length && c.status !== 'submitting');
  const lbl = labelInfo || { label: `#?${c.idx}`, take: '' };
  const takeBit = lbl.take ? `<span class="sd-take" title="Повторная генерация той же сцены">${esc(lbl.take)}</span>` : '';
  return `
    <label class="sd-card-cb-wrap" title="Выбрать для bulk-действий">
      <input type="checkbox" class="sd-card-cb" ${isSelected ? 'checked' : ''}
        onclick="event.stopPropagation();sdToggleSelect(${c.idx})">
    </label>
    <div class="sd-thumb"
         onclick="sdOpenChunkModal(${c.idx})"
         onmouseenter="_sdThumbHoverPlay(this)"
         onmouseleave="_sdThumbHoverStop(this)">
      ${videoUrl
        ? `<video src="${videoUrl}" preload="metadata" playsinline></video>`
        : `<span>${esc(placeholderText)}</span>`}
      <span class="sd-thumb-hint">⛶ Открыть</span>
    </div>
    <div class="sd-gen-meta">
      <div class="sd-label">${esc(lbl.label)}${takeBit}</div>
      <div class="sd-meta-row">
        <span class="${stCls}">●</span>
        <span>${esc(c.status || '')}</span>
        <span>· ${c.duration}s</span>
        <span>· ${esc(c.resolution || '')}</span>
        ${cost ? `<span>· ${cost}</span>` : ''}
      </div>
      ${c.error && c.status !== 'completed' ? `<div class="sd-card-err" title="${esc(c.error)}">${esc(c.error)}</div>` : ''}
      ${c.continuity_reset_reason ? `<div class="sd-card-warn" title="${esc(c.continuity_reset_reason)}" style="background:rgba(251,191,36,0.12);border:1px solid rgba(251,191,36,0.4);border-radius:4px;padding:3px 6px;margin-top:3px;color:#fbbf24;font-size:0.74rem">⚠ континьюити сброшен</div>` : ''}
      ${_qcBadgeHTML(c)}
    </div>
    <div class="sd-gen-actions">
      ${videoUrl ? `<a class="btn-ghost btn-sm" href="${videoUrl}" download onclick="event.stopPropagation()">⬇ Скачать</a>` : '<span></span>'}
      ${videoUrl ? `<button class="btn-ghost btn-sm" onclick="event.stopPropagation();sdAddToTimeline(${c.idx}, this)">➕ На таймлайн</button>` : '<span></span>'}
      ${canRetry ? `<button class="btn-ghost btn-sm" onclick="event.stopPropagation();sdRetry(${c.idx}, this)" title="Retry: тот же промпт+refs, новый чанк">🔁 Retry</button>` : '<span></span>'}
      <button class="btn-ghost btn-sm" onclick="event.stopPropagation();sdShowInScript(${c.idx})" title="Прокрутить вверх к сценарию и подсветить сегмент, к которому относится этот чанк">📜 В сценарии</button>
      <button class="btn-ghost btn-sm" onclick="event.stopPropagation();sdReuse(${c.idx})" title="Подставить параметры в форму выше">↻ Reuse</button>
      ${c.status === 'failed' ? `<button class="btn-ghost btn-sm full-row" onclick="event.stopPropagation();sdHealAndReuse(${c.idx}, this)" title="Переписать промпт чтобы прошёл модерацию + Reuse">🩹 Лечить промпт</button>` : ''}
      ${(c.status === 'failed' && (c.heal_count || 0) >= 1) ? `<button class="btn-ghost btn-sm full-row" style="color:var(--accent)" onclick="event.stopPropagation();sdRewriteChunk(${c.idx}, this)" title="Лечение не помогло — переписать сцену с нуля с учётом всей серии">✍️ Переписать сцену</button>` : ''}
      <button class="btn-ghost btn-sm full-row" onclick="event.stopPropagation();sdDelete(${c.idx})">🗑 Удалить</button>
    </div>
  `;
}

// Thumbnail hover: play the preview from frame 0 WITH sound. Stops every
// other thumbnail first so only one preview is audible at a time. Leaving
// the thumb (or click to open the modal) stops playback completely and
// resets to frame 0 — full stop, not «paused», so next hover starts fresh.
function _sdThumbHoverPlay(el) {
  const v = el && el.querySelector('video');
  if (!v) return;
  // Single-audio guarantee: silence every OTHER thumbnail so the user never
  // hears two clips overlapping when the cursor races across the strip.
  document.querySelectorAll('.sd-thumb video').forEach(other => {
    if (other !== v) {
      try { other.pause(); other.currentTime = 0; } catch (_) {}
    }
  });
  try {
    v.muted = false;
    v.volume = 1.0;
    v.currentTime = 0;
    const p = v.play();
    if (p && p.catch) {
      // Some browsers block unmuted autoplay until the user has actually
      // clicked something on the page. Fall back to muted playback so the
      // preview at least animates — sound will start working on subsequent
      // hovers once the user clicks anywhere.
      p.catch(() => {
        try {
          v.muted = true;
          const p2 = v.play();
          if (p2 && p2.catch) p2.catch(() => {});
        } catch (_) {}
      });
    }
  } catch (_) {}
}
function _sdThumbHoverStop(el) {
  const v = el && el.querySelector('video');
  if (!v) return;
  try { v.pause(); v.currentTime = 0; } catch (_) {}
}

function _sdCardSig(c, labelInfo) {
  // Signature changes only on something user-visible. Include label so retry-
  // numbering shifts (v2/v3) trigger re-render. Playback isn't reset on a poll
  // when nothing user-visible changed.
  const lblKey = labelInfo ? `${labelInfo.label}|${labelInfo.take}` : '';
  // QC fields: include status/attempts/fails so transitions (null → pass /
  // null → fail / fail → retry_exhausted) trigger re-render of the QC badge.
  // Without this, server-side QC updates on disk but UI shows stale «🔍 QC...»
  // badge until hard reload (real bug 2026-05-19, user complaint).
  const qc = c.qc || {};
  const qcKey = `${qc.status || ''}|${qc.attempts || 0}|${(qc.fails || []).join(',')}`;
  return [
    c.idx, c.status, c.video_path || '',
    c.progress ?? '', c.error || '',
    c.prompt || '', c.duration, c.resolution, c.moderation_bypass,
    c.cost ?? '', c.heal_count ?? 0, lblKey, qcKey,
  ].join('|');
}

// State used by arrow-key navigation in the chunk-detail modal. Holds the
// idx currently shown and the ordered list of idxs that ←/→ walks through.
let _SD_MODAL_CTX = null;

// Open a chunk in a detail modal: large player + full prompt + all actions.
// Stops any thumbnail that was hover-playing first, so audio from the modal
// player is the only thing audible.
function sdOpenChunkModal(idx) {
  document.querySelectorAll('.sd-thumb video').forEach(v => {
    try { v.pause(); v.currentTime = 0; } catch (_) {}
  });
  const list = SD._lastChunks || [];
  const c = list.find(x => x.idx === idx);
  if (!c) return;
  // Snapshot the current DOM order so ←/→ walks chunks in whatever sort
  // mode the user is actually looking at (chrono / newest).
  _SD_MODAL_CTX = {
    idx,
    orderedIdxs: Array.from(
      document.querySelectorAll('#sd-gen-list .sd-gen-card[data-idx]')
    ).map(card => Number(card.getAttribute('data-idx'))),
  };
  // De-dupe: addEventListener with the same fn is a no-op, so this can run
  // every open without leaking handlers.
  document.addEventListener('keydown', _sdModalKeyHandler);
  const labels = _sdComputeLabels(list);
  const lbl = labels.get(c.idx) || { label: `#?${c.idx}`, take: '' };
  const videoUrl = _chunkVideoUrl(c);
  const stCls = `sd-status-${c.status || 'pending'}`;
  const cost = c.cost != null ? `$${Number(c.cost).toFixed(2)}` : '';
  const canRetry = !!(c.prompt && (c.refs || []).length && c.status !== 'submitting');
  // Neighbour navigation arrows — disabled at strip edges.
  const pos = _SD_MODAL_CTX.orderedIdxs.indexOf(c.idx);
  const hasPrev = pos > 0;
  const hasNext = pos >= 0 && pos < _SD_MODAL_CTX.orderedIdxs.length - 1;
  const modal = document.getElementById('modal-chunk-detail');
  if (!modal) return;
  const body = modal.querySelector('.sd-modal-body');
  body.innerHTML = `
    <div class="sd-modal-player">
      ${videoUrl
        ? `<video src="${videoUrl}" controls autoplay preload="metadata" playsinline></video>`
        : `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);text-align:center;padding:12px">Видео ещё нет — ${esc(c.status || 'pending')}</div>`}
    </div>
    <div class="sd-modal-side">
      <div style="display:flex;align-items:center;gap:8px">
        <button class="btn-icon sd-modal-nav" onclick="_sdModalNav(-1)" ${hasPrev ? '' : 'disabled'} title="Предыдущий чанк (←)">←</button>
        <div style="flex:1;font-size:1.15rem;font-weight:700">${esc(lbl.label)}${lbl.take ? `<span class="sd-take">  ${esc(lbl.take)}</span>` : ''}</div>
        <span style="font-size:0.72rem;color:var(--muted)">${pos + 1} / ${_SD_MODAL_CTX.orderedIdxs.length}</span>
        <button class="btn-icon sd-modal-nav" onclick="_sdModalNav(+1)" ${hasNext ? '' : 'disabled'} title="Следующий чанк (→)">→</button>
      </div>
      <div class="sd-modal-meta">
        <span class="sd-meta-pill"><span class="${stCls}">●</span> ${esc(c.status || '')}</span>
        <span class="sd-meta-pill">${c.duration}с</span>
        <span class="sd-meta-pill">${esc(c.resolution || '')}</span>
        <span class="sd-meta-pill">${esc(c.moderation_bypass || '')}</span>
        ${cost ? `<span class="sd-meta-pill">${cost}</span>` : ''}
      </div>
      ${c.error && c.status !== 'completed' ? `<div style="color:#e74c3c;font-size:0.84rem">${esc(c.error)}</div>` : ''}
      <div class="sd-modal-actions">
        ${videoUrl ? `<a class="btn-ghost btn-sm" href="${videoUrl}" download>⬇ Скачать</a>` : ''}
        ${videoUrl ? `<button class="btn-ghost btn-sm" onclick="sdAddToTimeline(${c.idx}, this)">➕ На таймлайн</button>` : ''}
        ${canRetry ? `<button class="btn-ghost btn-sm" onclick="sdRetry(${c.idx}, this)">🔁 Retry</button>` : ''}
        <button class="btn-ghost btn-sm" onclick="sdReuse(${c.idx});_sdCloseChunkModal()">↻ Reuse</button>
        ${c.status === 'failed' ? `<button class="btn-ghost btn-sm" onclick="sdHealAndReuse(${c.idx}, this)">🩹 Лечить</button>` : ''}
        ${(c.status === 'failed' && (c.heal_count || 0) >= 1) ? `<button class="btn-ghost btn-sm" style="color:var(--accent)" onclick="sdRewriteChunk(${c.idx}, this)" title="Переписать сцену с нуля с учётом всей серии">✍️ Переписать сцену</button>` : ''}
        <button class="btn-ghost btn-sm" onclick="if(confirm('Удалить эту генерацию?')){sdDelete(${c.idx});_sdCloseChunkModal()}" style="color:var(--danger)">🗑 Удалить</button>
      </div>
      <div style="font-size:0.78rem;color:var(--muted);margin-top:4px">Промпт, отправленный в Seedance:</div>
      <div class="sd-modal-prompt">${esc(c.prompt || '(промпт не сохранён)')}</div>
    </div>`;
  openModal('modal-chunk-detail');
}

// Close handler: stops the player so audio doesn't keep playing in the
// background after the user dismisses the modal. Also clears the body so
// the video element is destroyed (otherwise its decoder lingers).
function _sdCloseChunkModal() {
  const modal = document.getElementById('modal-chunk-detail');
  if (modal) {
    modal.querySelectorAll('video').forEach(v => {
      try { v.pause(); v.removeAttribute('src'); v.load(); } catch (_) {}
    });
    const body = modal.querySelector('.sd-modal-body');
    if (body) body.innerHTML = '';
  }
  document.removeEventListener('keydown', _sdModalKeyHandler);
  _SD_MODAL_CTX = null;
  closeModal('modal-chunk-detail');
}

// Walk neighbours in the order they appear on screen. `delta` = -1 for ←,
// +1 for →. Stops at strip edges. Re-uses sdOpenChunkModal which tears down
// the existing <video> via innerHTML rewrite, so no decoder leak.
function _sdModalNav(delta) {
  if (!_SD_MODAL_CTX) return;
  const ord = _SD_MODAL_CTX.orderedIdxs;
  const pos = ord.indexOf(_SD_MODAL_CTX.idx);
  if (pos < 0) return;
  const next = pos + delta;
  if (next < 0 || next >= ord.length) return;
  sdOpenChunkModal(ord[next]);
}

function _sdModalKeyHandler(e) {
  if (!_SD_MODAL_CTX) return;
  // Don't hijack arrow keys while user is typing in a focused control.
  const tag = (e.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target?.isContentEditable) return;
  if (e.key === 'ArrowLeft')      { e.preventDefault(); _sdModalNav(-1); }
  else if (e.key === 'ArrowRight'){ e.preventDefault(); _sdModalNav(+1); }
  else if (e.key === 'Escape')    { e.preventDefault(); _sdCloseChunkModal(); }
}

function sdRenderList(chunks) {
  const el = document.getElementById('sd-gen-list');
  if (!el) return;
  // cache the last server-state list so optimistic adds can stack on top
  if (!chunks.some(c => c._optimistic)) SD._lastChunks = chunks;
  // Show/hide the «🎬 Собрать серию» button + «⬇ Скачать финал» link based
  // on chunk state. Button visible if there's at least 1 completed chunk
  // with a video_path. Link visible if the episode already has an assembled
  // file on disk (assembled_path on S.episode).
  _sdUpdateAssembleUI(chunks);
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
  // Sort UI toggle. Sort mode is persisted in localStorage so reloads keep
  // the user's preferred view. Default: «chrono» — by script position.
  _sdSyncSortButtons();
  const sortMode = _sdGetSort();
  // Labels (and their parent-attribution for «доп.N») are pre-computed once
  // off the unsorted list — take/доп counters depend on the global view, not
  // on whichever sort mode the user is in.
  const labels = _sdComputeLabels(chunks);
  try {
    const ordered = chunks.slice().sort((a, b) => {
      if (sortMode === 'newest') {
        // Newest first — by idx DESC (idx is monotonically increasing per
        // /seedance/start), tiebreak by created_at for legacy data.
        return (b.idx ?? 0) - (a.idx ?? 0) || (b.created_at ?? 0) - (a.created_at ?? 0);
      }
      // Chronological: regulars of order N, then their additionals, then
      // order N+1. Encoded via _sdChronoKey for stability.
      const ka = _sdChronoKey(a, labels.get(a.idx));
      const kb = _sdChronoKey(b, labels.get(b.idx));
      for (let i = 0; i < ka.length; i++) {
        if (ka[i] !== kb[i]) return ka[i] - kb[i];
      }
      return 0;
    });
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
      const lbl = labels.get(c.idx);
      const sig = _sdCardSig(c, lbl);
      let node = existing.get(idx);
      if (!node) {
        node = document.createElement('div');
        node.className = 'sd-gen-card';
        node.setAttribute('data-idx', idx);
        node.setAttribute('data-sig', sig);
        node.innerHTML = _sdCardHTML(c, lbl);
      } else if (node.getAttribute('data-sig') !== sig) {
        node.setAttribute('data-sig', sig);
        node.innerHTML = _sdCardHTML(c, lbl);
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
    // Force-sync every visible checkbox to SD.selected. Bug fix 2026-05-12:
    // toggling a single chunk's CB was visually un-checking unrelated chunks
    // because innerHTML rewrites (triggered by sig changes on those other
    // chunks during poll) re-rendered their checkbox attribute from a stale
    // `isSelected` value. By re-asserting every CB's `.checked` after the
    // diff loop, we make CB state authoritative-from-SD.selected regardless
    // of when/why the card's HTML was last regenerated.
    el.querySelectorAll('.sd-gen-card[data-idx]').forEach(card => {
      const idxNum = Number(card.getAttribute('data-idx'));
      const cb = card.querySelector('input.sd-card-cb');
      if (cb) {
        const want = !!(SD.selected && SD.selected.has(idxNum));
        if (cb.checked !== want) cb.checked = want;
      }
    });
    // Refresh bulk bar in case server removed/added chunks
    _sdUpdateBulkBar();
  } catch (err) {
    console.error('[sdRenderList] diff failed, falling back to full render:', err);
    const mode = _sdGetSort();
    const ordered = chunks.slice().sort((a, b) => {
      if (mode === 'newest') {
        return (b.idx ?? 0) - (a.idx ?? 0) || (b.created_at ?? 0) - (a.created_at ?? 0);
      }
      const ka = _sdChronoKey(a, labels.get(a.idx));
      const kb = _sdChronoKey(b, labels.get(b.idx));
      for (let i = 0; i < ka.length; i++) {
        if (ka[i] !== kb[i]) return ka[i] - kb[i];
      }
      return 0;
    });
    el.innerHTML = ordered.map(c => `<div class="sd-gen-card" data-idx="${c.idx}">${_sdCardHTML(c, labels.get(c.idx))}</div>`).join('');
  }
}

async function sdDelete(idx) {
  if (!confirm('Удалить эту генерацию?')) return;
  await api.del(`/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/${idx}`);
  if (SD.selected) SD.selected.delete(idx);
  _sdUpdateBulkBar();
  await sdRefreshList();
}

// «📺 К видео» — opposite of sdShowInScript. From a scene-view segment,
// scroll DOWN to the seedance chunks panel and highlight ALL chunks (originals
// + retries / additionals) whose sceneIdx + segIdx match this segment.
// Falls back to chunk_text matching for legacy chunks without these fields.
function showChunksForSegment(sceneIdx, segIdx) {
  const list = SD._lastChunks || [];
  if (!list.length) {
    showToast('Нет сгенерированных чанков для этого эпизода', 4000);
    return;
  }
  // Primary match: server-stored sceneIdx/segIdx on chunks
  let matches = list.filter(c =>
    typeof c.sceneIdx === 'number' && typeof c.segIdx === 'number'
    && c.sceneIdx === sceneIdx && c.segIdx === segIdx
  );
  // Legacy fallback — match by chunk_text first-line overlap. Read the segment
  // text from DOM and look for chunks containing that line.
  if (!matches.length) {
    const seg = document.querySelector(
      `.ep-seg[data-scene-idx="${sceneIdx}"][data-seg-idx="${segIdx}"]`
    );
    if (seg) {
      const segText = (seg.textContent || '').trim();
      // Use a substring meaningful enough to disambiguate but not too long
      const probe = segText.split('\n').map(l => l.trim()).find(l => l.length > 8) || segText.slice(0, 80);
      if (probe) {
        matches = list.filter(c => (c.chunk_text || '').includes(probe.slice(0, 60)));
      }
    }
  }
  if (!matches.length) {
    showToast(`Для сегмента ${sceneIdx + 1}/${segIdx + 1} ещё нет сгенерированных чанков`, 4000);
    return;
  }
  // Scroll to the first matching chunk card. The list panel is `#sd-gen-list`.
  const firstIdx = matches[0].idx;
  const firstCard = document.querySelector(`.sd-gen-card[data-idx="${firstIdx}"]`);
  if (firstCard) {
    firstCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  // Highlight ALL matching cards with the same flash animation as
  // sdShowInScript (yellow outline → fade).
  for (const c of matches) {
    const card = document.querySelector(`.sd-gen-card[data-idx="${c.idx}"]`);
    if (!card) continue;
    const prevStyle = card.getAttribute('style') || '';
    card.setAttribute('style', prevStyle +
      ';transition:box-shadow 0.3s ease-out;' +
      'box-shadow:0 0 0 3px rgba(251,191,36,0.85),0 0 24px rgba(251,191,36,0.5);' +
      'border-radius:8px;'
    );
    setTimeout(() => {
      card.setAttribute('style', prevStyle +
        ';transition:box-shadow 1.5s ease-out;box-shadow:none;'
      );
    }, 1800);
    setTimeout(() => {
      card.setAttribute('style', prevStyle);
    }, 3400);
  }
  if (matches.length > 1) {
    showToast(`Подсвечено ${matches.length} чанков (оригинал + ретраи/доп)`, 3000);
  }
}

// «📜 В сценарии» — scroll up to the script section and highlight the segment
// this chunk was generated from. Matches by chunk.sceneIdx + chunk.segIdx
// (server-stored). Falls back to chunk_text first-line lookup for legacy
// chunks without these fields.
function sdShowInScript(idx) {
  const list = SD._lastChunks || [];
  const c = list.find(x => x.idx === idx);
  if (!c) { showToast(`Чанк #${idx} не найден в текущем списке`, 4000); return; }

  // Make sure scene-view is open (segments only render when scene-view is on).
  const sceneViewWrap = document.getElementById('ep-script-scenes');
  const isSceneViewVisible = sceneViewWrap && !sceneViewWrap.classList.contains('hidden');
  if (!isSceneViewVisible && typeof toggleSceneView === 'function') {
    toggleSceneView();
  }

  // Try direct match by scene/seg coordinates (stored on chunk).
  let target = null;
  if (typeof c.sceneIdx === 'number' && typeof c.segIdx === 'number') {
    target = document.querySelector(
      `.ep-seg[data-scene-idx="${c.sceneIdx}"][data-seg-idx="${c.segIdx}"]`
    );
  }
  // Legacy fallback — match by first 60 chars of chunk_text against rendered
  // segment bodies. Cheap text-contains check.
  if (!target && c.chunk_text) {
    const firstLine = (c.chunk_text.split('\n').find(l => l.trim().length > 5) || '').trim();
    const probe = firstLine.slice(0, 60);
    if (probe) {
      const segs = document.querySelectorAll('.ep-seg');
      for (const seg of segs) {
        if ((seg.textContent || '').includes(probe)) { target = seg; break; }
      }
    }
  }
  if (!target) {
    showToast(`Не нашёл сегмент в сценарии для чанка #${idx}. Возможно сценарий редактировался.`, 5000);
    return;
  }

  // Scroll + highlight. Smooth scroll first, then add a brief flash class
  // (CSS animation defined alongside in style.css; if missing the inline
  // box-shadow fallback still gives visible feedback).
  target.scrollIntoView({ behavior: 'smooth', block: 'center' });
  // Inline flash — works without CSS. Strong yellow outline + soft shadow,
  // fades over 2 seconds.
  const prevStyle = target.getAttribute('style') || '';
  target.setAttribute('style', prevStyle +
    ';transition:box-shadow 0.3s ease-out;' +
    'box-shadow:0 0 0 3px rgba(251,191,36,0.85),0 0 24px rgba(251,191,36,0.5);' +
    'border-radius:8px;'
  );
  setTimeout(() => {
    target.setAttribute('style', prevStyle +
      ';transition:box-shadow 1.5s ease-out;box-shadow:none;'
    );
  }, 1800);
  setTimeout(() => {
    target.setAttribute('style', prevStyle);
  }, 3400);
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
        // Recompute duration from chunk_text via the VO-aware estimator. The
        // stored `c.duration` may have been set BEFORE the voiceover-overlap
        // fix shipped, so a retry would inherit the inflated value (9s for a
        // 4s VO line) — user-reported on «Diner Opens at Midnight…» ep 1
        // even after the formula fix. Fallback to c.duration only if chunk_text
        // is empty (legacy chunk without stored script text).
        duration: c.chunk_text
          ? _estimateChunkDurationSec(c.chunk_text, { fallback: c.duration || 15 })
          : (c.duration || 15),
        resolution: c.resolution || '720p',
        moderation_bypass: c.moderation_bypass || 'off',
        // Carry the source chunk's canonical script_order. Without this the
        // retry creates a no-position orphan, and the backend's auto-assemble
        // dedup keeps the OLD chunk for that script_order slot AND appends
        // the retry to the tail of the timeline — producing a final video
        // that shows BOTH versions back-to-back. User-reported on series
        // «My Roommate From Craigslist Is Hunting Me» ep 1: clothing «скачет»
        // every shot because v1 and v2 were both in the assembled cut.
        script_order: (typeof c.script_order === 'number') ? c.script_order : null,
        // Also propagate scene coordinates so «📺 К видео» button finds the
        // retry chunk (it filters chunks by sceneIdx+segIdx). Without these
        // user can't navigate from script segment to its successful retry.
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

