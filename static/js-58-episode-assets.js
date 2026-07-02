async function _dropAssetUpload(event, kind, id) {
  event.preventDefault();
  event.stopPropagation();
  const target = event.currentTarget;
  if (target) target.classList.remove('drop-hover');
  const file = event.dataTransfer?.files?.[0];
  if (!file || !file.type.startsWith('image/')) return;
  const fd = new FormData();
  fd.append('photo', file);
  const endpoint = (
    kind === 'char' ? `/api/series/${S.seriesId}/characters/${id}/upload-photo` :
    kind === 'loc'  ? `/api/series/${S.seriesId}/locations/${id}/upload-photo` :
    kind === 'item' ? `/api/series/${S.seriesId}/items/${id}/upload-photo` :
    null
  );
  if (!endpoint) return;
  // Visual feedback: spinner inside any thumb in the dropped element.
  const thumb = target?.querySelector('img');
  if (thumb) thumb.style.opacity = '0.4';
  try {
    const res = await fetch(endpoint, { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    bumpAssetVersion();
    if (data.series) S.series = data.series;
    else {
      try { S.series = await api.get(`/api/series/${S.seriesId}`); } catch {}
    }
    // Re-render every list that might show this asset.
    renderCharactersList && renderCharactersList();
    renderLocationsList && renderLocationsList();
    renderItemsList && renderItemsList();
    if (typeof renderEpCharacters === 'function') renderEpCharacters();
    if (typeof renderEpLocations === 'function') renderEpLocations();
    if (typeof renderEpItems === 'function') renderEpItems();
    // Re-open the modal photo grid if it's currently showing for this entity.
    const modal = document.getElementById('modal-char-assets');
    if (kind === 'char' && modal && modal.classList.contains('open')) {
      const c = S.series.characters.find(x => x.id === id);
      if (c) renderCharAssetsGrid(c);
    }
    showToast(`✓ Фото обновлено`);
  } catch (e) {
    showToast('Ошибка загрузки: ' + (e.message || e));
  } finally {
    if (thumb) thumb.style.opacity = '';
  }
}

// Drop-on-modal-char-assets-zone: routes to currently-open character. The
// modal stores currentCharId, so we just delegate. Same for loc/item.
function _dropAssetOnCharFromModal(event) {
  if (!currentCharId) return _dropAssetLeave(event);
  return _dropAssetUpload(event, 'char', currentCharId);
}

// Standard ondragover handler — adds .drop-hover class so CSS can highlight.
function _dropAssetOver(event) {
  event.preventDefault();
  event.stopPropagation();
  // Show "copy" cursor (file drop) instead of default "no entry"
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  event.currentTarget?.classList.add('drop-hover');
}
function _dropAssetLeave(event) {
  event.currentTarget?.classList.remove('drop-hover');
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

