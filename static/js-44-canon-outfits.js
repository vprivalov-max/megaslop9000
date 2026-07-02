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
      // Bump global asset cache token — browser was serving the OLD outfit
      // image from HTTP cache because filename stayed the same. Without this
      // user clicks Generate, backend returns new image, but UI shows old.
      bumpAssetVersion();
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
    bumpAssetVersion();   // invalidate cached photos across all views
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const c = S.series.characters.find(x => x.id === currentCharId);
    renderCharAssetsGrid(c, true);
    renderOutfitsList(c);
    renderCharactersList();
    if (typeof renderEpCharacters === 'function') renderEpCharacters();  // episode-view sidebar
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

// Inline-style chips for the create-from-script modal. Renders the preset
// list as clickable pills (cinematic / pixar / anime / noir / photorealistic /
// custom). Selection writes into the hidden #import-style-type input that the
// submit handler reads. «Custom» reveals the description textarea below.
async function _hydrateImportStylePicker() {
  const container = document.getElementById('import-style-chips');
  if (!container) return;
  const customTa = document.getElementById('import-style-custom-desc');
  const hiddenInput = document.getElementById('import-style-type');
  if (!hiddenInput) return;
  const presets = await _loadStylePresets();
  // Always include 'custom' at the end of the list.
  const list = [...presets, { id: 'custom', name: 'Свой стиль', desc: '' }];
  hiddenInput.value = 'cinematic';  // default
  if (customTa) customTa.style.display = 'none';
  container.innerHTML = list.map(p => {
    const sel = (p.id === 'cinematic') ? ' selected' : '';
    return `<button type="button" class="import-style-chip${sel}" data-style="${p.id}"
      onclick="_pickImportStyle('${p.id}')"
      style="padding:5px 11px;font-size:0.82rem;border-radius:14px;border:1px solid var(--border);
             background:${p.id === 'cinematic' ? 'var(--accent)' : '#1a1a1f'};
             color:${p.id === 'cinematic' ? '#fff' : 'var(--text)'};
             cursor:pointer">${esc(p.name || p.id)}</button>`;
  }).join('');
}

function _pickImportStyle(id) {
  const hiddenInput = document.getElementById('import-style-type');
  const customTa = document.getElementById('import-style-custom-desc');
  if (hiddenInput) hiddenInput.value = id;
  document.querySelectorAll('.import-style-chip').forEach(btn => {
    const sel = (btn.dataset.style === id);
    btn.style.background = sel ? 'var(--accent)' : '#1a1a1f';
    btn.style.color = sel ? '#fff' : 'var(--text)';
  });
  if (customTa) customTa.style.display = (id === 'custom') ? '' : 'none';
}

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

