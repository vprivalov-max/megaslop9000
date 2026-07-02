// ── Items (story-relevant props: handbag, gun, locket, ...) ───────────────────
let currentItemId = null;

function renderItemsList() {
  const s = S.series;
  const el = document.getElementById('items-list');
  if (!el) return;
  const items = s.items || [];
  if (!items.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.83rem">Нет предметов</div>';
    return;
  }
  el.innerHTML = items.map(it => {
    const hasRefs = it.ref_images && it.ref_images.length > 0;
    const v = it.image_version ? `?v=${it.image_version}` : '';
    const imgUrl = hasRefs ? `/assets/${s.id}/${it.ref_images[0]}${v}` : null;
    return `
      <div class="char-item" data-autogen-kind="item" data-autogen-id="${it.id}" onclick="openItemAssets('${it.id}')"
           ondragover="_dropAssetOver(event)" ondragleave="_dropAssetLeave(event)"
           ondrop="_dropAssetUpload(event, 'item', '${it.id}')"
           title="Клик — открыть карточку. Перетащи картинку с компа — заменит фото.">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(it.name)}" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : '🎒'}
          <div class="autogen-overlay" hidden><span class="spinner"></span></div>
        </div>
        <div class="char-info">
          <div class="char-name">${esc(it.name)}</div>
          <div class="char-role">${esc(it.description?.slice(0,30) || '—')}</div>
        </div>
        <div class="char-ref-dot ${hasRefs ? 'has-refs' : 'no-refs'}" title="${hasRefs ? 'Есть фото' : 'Нет фото'}"></div>
        <button class="btn-icon" onclick="event.stopPropagation();openEditItem('${it.id}')" title="Редактировать">✎</button>
        <button class="btn-icon" onclick="event.stopPropagation();deleteItem('${it.id}')" title="Удалить" style="color:var(--danger)">✕</button>
      </div>
    `;
  }).join('');
}

function openAddItem() {
  S.editingItemId = null;
  document.getElementById('item-modal-title').textContent = 'Новый предмет';
  clearFields(['item-name','item-description']);
  openModal('modal-item');
}

// One-click cleanup of duplicate items in the current series. Hits the
// /dedupe-items endpoint which runs fuzzy + LLM-synonym pass over series.items
// and merges synonym groups (dictaphone/voice recorder, locket/pendant). All
// episode.items_used arrays are rewritten to point at canonical ids.
async function dedupeSeriesItems() {
  if (!S.seriesId) return;
  const items = S.series?.items || [];
  if (items.length < 2) { showToast('Меньше 2 предметов — нечего объединять'); return; }
  if (!confirm(`Найти и склеить дубликаты среди ${items.length} предметов? Claude проверит синонимы (диктофон/recorder и т.п.) и объединит. Действие необратимо, но не теряет данные — берётся самое полное описание.`)) return;
  showToast('🧹 Анализирую дубликаты…');
  try {
    const r = await api.post(`/api/series/${S.seriesId}/dedupe-items`, {});
    bumpAssetVersion();
    if (r.error) throw new Error(r.error);
    if (!r.merged) {
      showToast('✓ Дубликатов не найдено');
      return;
    }
    const groups = (r.groups || []).map(g =>
      `  • ${g.canonical.name} ← ${g.merged.map(m => m.name).join(', ')}`
    ).join('\n');
    alert(`✓ Склеено ${r.merged} дубликатов в ${r.groups?.length || 0} группах:\n\n${groups}`);
    // Refresh state and re-render lists.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    if (typeof renderItemsList === 'function') renderItemsList();
    if (typeof renderEpItems === 'function') renderEpItems();
  } catch (e) {
    alert('Ошибка дедупликации: ' + (e?.message || e));
  }
}

function openEditItem(itemId) {
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  S.editingItemId = itemId;
  document.getElementById('item-modal-title').textContent = 'Редактировать предмет';
  setVal('item-name', it.name);
  setVal('item-description', it.description);
  openModal('modal-item');
}

async function saveItem() {
  const data = { name: val('item-name'), description: val('item-description') };
  if (!data.name) return alert('Введи название предмета');
  if (S.editingItemId) {
    await api.put(`/api/series/${S.seriesId}/items/${S.editingItemId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/items`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-item');
  renderItemsList();
}

async function deleteItem(itemId) {
  if (!confirm('Удалить предмет?')) return;
  await api.del(`/api/series/${S.seriesId}/items/${itemId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  renderItemsList();
}

function openItemAssets(itemId) {
  currentItemId = itemId;
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  document.getElementById('item-assets-title').textContent = `Фото: ${it.name}`;
  renderItemAssetsGrid(it);
  setVal('regen-item-wishes', it.image_constraints || '');
  const inp = document.getElementById('item-asset-file-input');
  inp.onchange = () => uploadItemRefs(itemId, inp.files);
  openModal('modal-item-assets');
}

function renderItemAssetsGrid(item) {
  const grid = document.getElementById('item-assets-grid');
  const refs = item.ref_images || [];
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `${assetUrl(r)}`;
    return `
      <div class="photo-thumb-wrap" onclick="openItemLightbox('${item.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteItemRef('${item.id}','${fname}')">✕</button>
      </div>`;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
}

async function uploadItemRefs(itemId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/item/${itemId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const it = (S.series.items || []).find(x => x.id === itemId);
  renderItemAssetsGrid(it);
  renderItemsList();
}

async function deleteItemRef(itemId, filename) {
  await api.del(`/api/series/${S.seriesId}/assets/item/${itemId}/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const it = (S.series.items || []).find(x => x.id === itemId);
  renderItemAssetsGrid(it);
  renderItemsList();
}

async function regenerateItem() {
  if (!currentItemId) return;
  const wishes = (val('regen-item-wishes') || '').trim();
  const btn = document.getElementById('regen-item-btn');
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ генерирую...'; }
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/items/${currentItemId}/regenerate`,
      { wishes }
    );
    if (!r.ready) throw new Error(r.error || 'unknown');
    bumpAssetVersion();   // refresh ALL assetUrl()-rendered images
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const it = (S.series.items || []).find(x => x.id === currentItemId);
    if (it) renderItemAssetsGrid(it);
    renderItemsList();
    if (typeof renderEpItems === 'function') renderEpItems();  // episode-view sidebar
    showToast('✓ Предмет перегенерирован');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old; }
  }
}

// Item-photo lightbox: image + regenerate panel
function openItemLightbox(itemId, url) {
  closeLightbox();
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  currentItemId = itemId;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать предмет</h3>
        <div class="hint">
          Старое фото удалится, новое сгенерируется по описанию + пожеланиям.
          Пожелания сохранятся в предмете и будут применяться при всех будущих генерациях.
        </div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-item-wishes" rows="5"
            placeholder="Например:&#10;«потёртая кожа, не новая»&#10;«золотая фурнитура»&#10;«царапина на боку»&#10;«без бренда»">${esc(it.image_constraints || '')}</textarea>
        </div>
        <div id="lb-regen-item-status" class="lb-status"></div>
        <button id="lb-regen-item-btn" class="btn-regen" onclick="regenerateItemFromLightbox()">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

async function regenerateItemFromLightbox() {
  if (!currentItemId) return;
  const wishes = (document.getElementById('lb-regen-item-wishes').value || '').trim();
  const status = document.getElementById('lb-regen-item-status');
  const btn = document.getElementById('lb-regen-item-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.className = 'lb-status';
  status.textContent = 'Перегенерируем фото предмета (~15-30 сек)...';
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/items/${currentItemId}/regenerate`,
      { wishes }
    );
    if (!res.ready) throw new Error(res.error || 'unknown');
    bumpAssetVersion();   // refresh ALL assetUrl()-rendered images
    status.className = 'lb-status ok';
    status.textContent = '✓ Предмет обновлён';
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const it = (S.series.items || []).find(x => x.id === currentItemId);
    if (it) renderItemAssetsGrid(it);
    renderItemsList();
    if (typeof renderEpItems === 'function') renderEpItems();  // episode-view sidebar
    setTimeout(() => closeLightbox(), 800);
  } catch (e) {
    status.className = 'lb-status err';
    status.textContent = 'Ошибка: ' + (e.message || e);
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

