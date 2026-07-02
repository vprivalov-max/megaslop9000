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
      // Bump global asset cache token — same reason as generateOutfit:
      // filename stays the same on regen, browser HTTP cache serves the
      // OLD image without this. Critical for ep-level outfit cards
      // («карточки персов внутри серии») user explicitly mentioned.
      bumpAssetVersion();
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

// Generic drag-and-drop upload handler for character / location / item cards.
// Routes to the correct /upload-photo endpoint based on `kind`. Used from
// any DOM node that wants to accept image-file drops as a "replace ref photo"
// gesture (cards in series sidebar, modal photo grids, episode-side photo
// containers). The user-supplied photo replaces existing refs (server-side
// /upload-photo endpoints all use replace-semantics now).
