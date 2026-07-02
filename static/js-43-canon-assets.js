// ── Character assets modal ────────────────────────────────────────────────────
let currentCharId = null;

async function openCharAssets(charId) {
  currentCharId = charId;
  // Refresh series state so any outfits added by recent script-gen / autogen
  // show up — without this, the modal renders stale `S.series.characters[i]`
  // and "no other outfits" looks like a bug even when they exist on disk.
  try {
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh && fresh.characters) S.series = fresh;
  } catch {}
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  document.getElementById('char-assets-title').textContent = `Фото: ${c.name}`;
  renderCharAssetsGrid(c);
  renderOutfitsList(c);
  renderCharCanonicalDesc(c);

  const inp = document.getElementById('char-asset-file-input');
  inp.onchange = () => uploadCharacterRefs(charId, inp.files);
  openModal('modal-char-assets');
}

// Build the canonical description shown in the card and copied into Seedance prompts.
// Mirrors backend _canonical_char_description() — appearance + base outfit description.
function _buildCanonicalCharDesc(char) {
  if (!char) return '';
  const appearance = (char.appearance || '').trim();
  const outfits = char.outfits || [];
  const base = outfits.find(o => o.is_base) || outfits[0];
  const outfitDesc = (base && (base.description || '').trim()) || '';
  return [appearance, outfitDesc].filter(Boolean).join('; ');
}

function renderCharCanonicalDesc(char) {
  const el = document.getElementById('char-canonical-desc');
  if (el) {
    const text = _buildCanonicalCharDesc(char);
    el.textContent = text || '(описание не задано — заполни appearance персонажа и/или базовый outfit)';
    el.dataset.text = text;
  }
  // Populate the editable appearance textarea + remember original for revert / dirty-check.
  const ta = document.getElementById('char-appearance-edit');
  if (ta && char) {
    const original = (char.appearance || '');
    ta.value = original;
    ta.dataset.original = original;
    ta.dataset.charId = char.id;
    const btn = document.getElementById('char-appearance-save-btn');
    if (btn) btn.disabled = true;
    const st = document.getElementById('char-appearance-save-status');
    if (st) st.textContent = '';
  }
}

// Called on every keystroke in the appearance textarea — enables/disables
// the Save button based on whether content differs from original.
function _markCharAppearanceDirty() {
  const ta = document.getElementById('char-appearance-edit');
  if (!ta) return;
  const dirty = ta.value !== (ta.dataset.original || '');
  const btn = document.getElementById('char-appearance-save-btn');
  if (btn) btn.disabled = !dirty;
  const st = document.getElementById('char-appearance-save-status');
  if (st) st.textContent = dirty ? '● несохранённые изменения' : '';
}

// Revert textarea to last saved value (from dataset.original).
function _revertCharAppearance() {
  const ta = document.getElementById('char-appearance-edit');
  if (!ta) return;
  ta.value = ta.dataset.original || '';
  _markCharAppearanceDirty();
}

// Persist appearance to backend via PUT, then refresh the canonical preview.
async function saveCharAppearance() {
  const ta = document.getElementById('char-appearance-edit');
  if (!ta) return;
  const charId = ta.dataset.charId;
  if (!charId) return;
  const newText = (ta.value || '').trim();
  const btn = document.getElementById('char-appearance-save-btn');
  const st = document.getElementById('char-appearance-save-status');
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Сохраняю...'; }
  if (st) st.textContent = '';
  try {
    const res = await api.put(
      `/api/series/${S.seriesId}/characters/${charId}`,
      { appearance: newText },
    );
    if (res && res.error) throw new Error(res.error);
    // Update in-memory state + re-render preview
    const ch = S.series.characters.find(x => x.id === charId);
    if (ch) ch.appearance = newText;
    ta.dataset.original = newText;
    renderCharCanonicalDesc(ch || res);
    // Also refresh sidebar list so any description-derived preview updates.
    renderCharactersList && renderCharactersList();
    if (st) st.textContent = '✓ сохранено';
    setTimeout(() => { if (st) st.textContent = ''; }, 2500);
  } catch (e) {
    if (st) st.textContent = '✗ ' + (e?.message || e);
  } finally {
    if (btn) btn.innerHTML = '💾 Сохранить';
  }
}

async function copyCanonicalDescription() {
  const el = document.getElementById('char-canonical-desc');
  const text = el?.dataset.text || el?.textContent || '';
  if (!text || text.startsWith('(')) {
    showToast('⚠ Описание пустое', 2500);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    showToast('✓ Скопировано', 1800);
  } catch {
    // Fallback for older browsers
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    ta.remove();
    showToast('✓ Скопировано', 1800);
  }
}

function renderCharAssetsGrid(char, bustCache) {
  const grid = document.getElementById('char-assets-grid');
  const refs = char.ref_images || [];
  const cacheSuffix = bustCache ? `?t=${Date.now()}` : '';
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `${assetUrl(r)}${cacheSuffix}`;
    return `
      <div class="photo-thumb-wrap" onclick="openCharLightbox('${char.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteCharRef('${char.id}','${fname}')">✕</button>
      </div>
    `;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
}

// Builds an inline DOM node that replaces a broken <img>. Shows a clearly-
// broken visual + click-to-debug. Used everywhere asset paths might point at
// a file that vanished (concurrent-write JSON corruption used to do this; the
// atomic-write fix should prevent it now, but the placeholder stays as a
// safety net + diagnostic tool).
function _brokenImagePlaceholder(url) {
  const div = document.createElement('div');
  div.className = 'broken-image-placeholder';
  div.title = 'Картинка не загрузилась — кликни для дебага';
  div.dataset.assetUrl = url;
  div.innerHTML = `
    <div class="bip-icon">🚫</div>
    <div class="bip-label">Фото утеряно</div>
    <div class="bip-hint">Клик — дебаг</div>`;
  div.onclick = (e) => { e.stopPropagation(); debugAsset(url); };
  return div;
}

// Click-handler for broken-image placeholders. Hits a debug endpoint that
// reports filesystem state for the asset path so the user can paste the JSON
// blob back to support / dev. Resilient: never throws into the UI.
async function debugAsset(url) {
  try {
    // url looks like '/assets/<sid>/<rel_path>?v=<cache-buster>' — strip query
    // string before feeding rel_path to the debug API. Without this strip the
    // backend tries to open `file.jpg?v=1778...` literally, always reports
    // exists=false even when the file IS on disk. (User-reported false alarm.)
    const cleanUrl = url.split('?')[0].split('#')[0];
    const m = cleanUrl.match(/^\/assets\/([^/]+)\/(.+)$/);
    if (!m) {
      alert('Не разобрать путь к ассету: ' + url);
      return;
    }
    const [, sid, relPath] = m;
    const r = await api.get(`/api/series/${encodeURIComponent(sid)}/debug-asset?path=${encodeURIComponent(relPath)}`);
    const dump = JSON.stringify(r, null, 2);
    // Modal-ish: open a textarea-in-prompt so user can copy.
    const overlay = document.createElement('div');
    overlay.className = 'lightbox-overlay';
    overlay.style.zIndex = 99999;
    overlay.innerHTML = `
      <button class="lb-close" onclick="this.parentElement.remove()">✕</button>
      <div class="lightbox-content" onclick="event.stopPropagation()" style="max-width:720px">
        <div class="lightbox-panel" style="width:100%">
          <h3>🔍 Дебаг ассета</h3>
          <div style="font-size:0.78rem;color:var(--muted);word-break:break-all">${esc(url)}</div>
          <textarea readonly rows="18" style="width:100%;font-family:monospace;font-size:0.78rem">${esc(dump)}</textarea>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
            <button class="btn-regen" onclick="navigator.clipboard.writeText(this.parentElement.previousElementSibling.value);this.textContent='✓ Скопировано'">📋 Скопировать дебаг</button>
            <button class="btn-regen" style="background:linear-gradient(135deg,#10b981,#059669)" onclick="relinkOrphanedAssets(this)">🔗 Найти и привязать потерянные</button>
          </div>
          <div id="relink-result" style="font-size:0.82rem;color:var(--muted);margin-top:8px;white-space:pre-wrap"></div>
        </div>
      </div>`;
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    document.body.appendChild(overlay);
  } catch (e) {
    alert('Дебаг не удался: ' + (e?.message || e));
  }
}

// Calls /relink-assets which scans the assets/ folder and re-attaches orphan
// files to characters/locations/items whose ref_images is empty. Runs from
// the broken-image debug modal so the user can self-heal a series after a
// JSON-corruption incident wiped refs.
async function relinkOrphanedAssets(btn) {
  if (!S.seriesId) { alert('Открой сериал сначала'); return; }
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ Сканирую...';
  const out = document.getElementById('relink-result');
  if (out) out.textContent = '';
  try {
    const r = await api.post(`/api/series/${S.seriesId}/relink-assets`, {});
    bumpAssetVersion();
    if (r?.error) throw new Error(r.error);
    const lines = (r.relinked || []).map(x =>
      `✓ ${x.kind} «${x.name}» → ${x.files.join(', ')}`
    ).join('\n');
    if (out) out.textContent = r.count
      ? `Привязано: ${r.count}\n${lines}`
      : 'Орфанов не найдено — все ассеты на местах.';
    // Refresh series state and re-render lists.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    renderCharactersList && renderCharactersList();
    renderLocationsList && renderLocationsList();
    renderItemsList && renderItemsList();
    renderEpCharacters && renderEpCharacters();
    renderEpLocations && renderEpLocations();
    renderEpItems && renderEpItems();
    btn.textContent = '✓ Готово';
  } catch (e) {
    if (out) out.textContent = '✗ ' + (e?.message || e);
    btn.disabled = false; btn.textContent = orig;
  }
}

// Plain image lightbox (used for outfit photos — no regen panel)
function openLightbox(url) {
  closeLightbox();
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

// Outfit lightbox: shows the outfit photo with prev/next nav across ALL of the
// character's photos (base portrait as item 0, then each outfit with a photo).
// Lets the user browse the wardrobe full-size without closing the lightbox
// between picks. Arrow keys + on-screen arrows + regenerate-with-wishes.
let _outfitLightboxState = null;  // {charId, items: [{kind, id, label, photoUrl}], idx}

function openOutfitLightbox(charId, outfitId) {
  closeLightbox();
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  const items = [];
  if (c.ref_images?.length) {
    items.push({
      kind: 'base', id: 'base', label: 'База',
      photoUrl: `${assetUrl(c.ref_images[0])}`,
    });
  }
  for (const o of (c.outfits || [])) {
    if (o.photo) {
      items.push({
        kind: 'outfit', id: o.id, label: o.label || '(без названия)',
        photoUrl: `${assetUrl(o.photo)}`,
      });
    }
  }
  if (!items.length) { showToast('Нет фото для просмотра'); return; }
  let idx = items.findIndex(it => it.kind === 'outfit' && it.id === outfitId);
  if (idx < 0) idx = items.findIndex(it => it.kind === 'base' && outfitId === 'base');
  if (idx < 0) idx = 0;
  _outfitLightboxState = { charId, items, idx };
  _renderOutfitLightbox();
}

function _renderOutfitLightbox() {
  const st = _outfitLightboxState;
  if (!st) return;
  const cur = st.items[st.idx];
  const c = S.series.characters.find(x => x.id === st.charId);
  const total = st.items.length;
  const prevDisabled = total < 2 ? 'disabled' : '';
  const nextDisabled = total < 2 ? 'disabled' : '';
  let div = document.getElementById('lightbox-overlay');
  if (!div) {
    div = document.createElement('div');
    div.id = 'lightbox-overlay';
    div.className = 'lightbox-overlay';
    div.onclick = (e) => { if (e.target === div) closeLightbox(); };
    document.body.appendChild(div);
  }
  // Re-render only the inner content; keep the overlay (avoids flicker on nav).
  const isOutfit = cur.kind === 'outfit';
  const outfitObj = isOutfit ? (c?.outfits || []).find(o => o.id === cur.id) : null;
  const regenLabel = isOutfit
    ? `↻ Перегенерировать образ «${esc(cur.label)}»`
    : '↻ Перегенерировать базу персонажа';
  const wishesPlaceholder = isOutfit
    ? 'Например:\n«Цвет более тёмный»\n«Без сумки»\n«Длиннее юбка»'
    : 'Например:\n«Без шрама на лице»\n«Глаза карие, не голубые»';
  const initialWishes = isOutfit ? '' : (c?.image_constraints || '');
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <button class="lb-nav lb-prev" ${prevDisabled} onclick="event.stopPropagation();outfitLightboxNav(-1)" title="Предыдущий (←)">‹</button>
    <button class="lb-nav lb-next" ${nextDisabled} onclick="event.stopPropagation();outfitLightboxNav(1)" title="Следующий (→)">›</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap">
        <img src="${cur.photoUrl}" alt="${esc(cur.label)}"
             onerror="this.replaceWith(_brokenImagePlaceholder('${cur.photoUrl}'))">
        <div class="lb-caption">
          <strong>${esc(cur.label)}</strong>
          <span style="color:var(--muted);margin-left:8px">${st.idx + 1} / ${total}</span>
        </div>
      </div>
      <div class="lightbox-panel">
        <h3>${regenLabel}</h3>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-wishes" rows="5"
            placeholder="${esc(wishesPlaceholder)}">${esc(initialWishes)}</textarea>
        </div>
        ${!isOutfit ? `
          <label class="cb">
            <input type="checkbox" id="lb-regen-outfits" checked>
            <span>Также перегенерировать все костюмы</span>
          </label>
        ` : ''}
        <div id="lb-regen-status" class="lb-status"></div>
        <button id="lb-regen-btn" class="btn-regen" onclick="${isOutfit
            ? `regenerateOutfitFromLightbox('${cur.id}')`
            : 'regenerateCharacterFromLightbox()'}">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
}

function outfitLightboxNav(delta) {
  const st = _outfitLightboxState;
  if (!st || st.items.length < 2) return;
  st.idx = (st.idx + delta + st.items.length) % st.items.length;
  _renderOutfitLightbox();
}

async function regenerateOutfitFromLightbox(outfitId) {
  const st = _outfitLightboxState;
  if (!st) return;
  const wishes = (document.getElementById('lb-regen-wishes')?.value || '').trim();
  const btn = document.getElementById('lb-regen-btn');
  const status = document.getElementById('lb-regen-status');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерирую...'; }
  if (status) status.textContent = '';
  try {
    // Persist wishes onto the outfit's description so future i2i bakes them in.
    if (wishes) {
      await api.put(`/api/series/${S.seriesId}/characters/${st.charId}/outfits/${outfitId}`, {
        description: wishes,
      });
    }
    // Trigger generation (uses existing endpoint that the outfit row uses).
    const r = await api.post(`/api/series/${S.seriesId}/characters/${st.charId}/outfits/${outfitId}/generate`, {});
    if (r?.error) throw new Error(r.error);
    // Force-bump asset version so every image URL gets a fresh `?v=N` token.
    // Without this, browser HTTP cache serves the OLD outfit image even
    // after AVAI returned a new one (out_path stays the same filename).
    // User-reported 2026-05-23: «перегенерация костюмов не работает».
    bumpAssetVersion();
    // Refresh series state and re-render the lightbox with the new image.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    if (typeof renderCharactersList === 'function') renderCharactersList();
    if (typeof openCharAssets === 'function' && document.getElementById('modal-char-assets')?.classList.contains('open')) {
      const c = S.series.characters.find(x => x.id === st.charId);
      if (c) renderOutfitsList(c);
    }
    // Rebuild item list from fresh state and stay on the same outfit.
    openOutfitLightbox(st.charId, outfitId);
    if (status) status.textContent = '✓ Готово';
  } catch (e) {
    if (status) status.textContent = '✗ ' + (e?.message || e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Перегенерировать'; }
  }
}

// Character-photo lightbox: image + regenerate panel
function openCharLightbox(charId, url) {
  closeLightbox();
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  currentCharId = charId;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать персонажа</h3>
        <div class="hint">
          Сначала перегенерируется <strong style="color:var(--text)">основной образ</strong>,
          затем по нему как референсу — все костюмы. Старые фото удалятся.
        </div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-wishes" rows="5"
            placeholder="Например:&#10;«Без шрама на лице»&#10;«Глаза карие, не голубые»&#10;«Волосы чуть короче»">${esc(c.image_constraints || '')}</textarea>
          <div style="font-size:0.76rem;color:var(--muted);margin-top:4px">
            Сохранится в персонаже и будет применяться при всех будущих генерациях.
          </div>
        </div>
        <label class="cb">
          <input type="checkbox" id="lb-regen-outfits" checked>
          <span>Также перегенерировать все костюмы</span>
        </label>
        <label class="cb" title="Claude перепишет поле appearance с нуля — помогает если текущее описание содержит эмоции/действия вместо внешности">
          <input type="checkbox" id="lb-rewrite-appearance">
          <span>Переписать описание персонажа заново</span>
        </label>
        <div style="margin-top:10px">
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Image generator
          </label>
          <select id="lb-regen-provider" style="width:100%;padding:6px 8px;border-radius:6px;background:var(--bg-input);color:var(--text);border:1px solid var(--border)" title="Выбери движок генерации изображения. Если выбранный упадёт — автоматический fallback на банан">
            <option value="">Auto (по умолчанию серии)</option>
            <option value="banana">🍌 Banana (Gemini Image Pro)</option>
            <option value="seedream">🌱 Seedream (ByteDance)</option>
            <option value="openai">🤖 OpenAI (gpt-image-1)</option>
          </select>
        </div>
        <div id="lb-regen-status" class="lb-status"></div>
        <button id="lb-regen-btn" class="btn-regen" onclick="regenerateCharacterFromLightbox()">
          ↻ Перегенерировать
        </button>
        <button class="btn-ghost" style="color:var(--danger);margin-top:8px;width:100%" onclick="deleteCharacterFromLightbox()">
          🗑 Удалить персонажа
        </button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

function closeLightbox() {
  const old = document.getElementById('lightbox-overlay');
  if (old) old.remove();
  _outfitLightboxState = null;
}
document.addEventListener('keydown', (e) => {
  if (!document.getElementById('lightbox-overlay')) return;
  if (e.key === 'Escape') return closeLightbox();
  if (_outfitLightboxState) {
    if (e.key === 'ArrowLeft')  { e.preventDefault(); outfitLightboxNav(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); outfitLightboxNav(1);  }
  }
});

async function deleteCharacterFromLightbox() {
  if (!currentCharId) return;
  if (!confirm('Удалить персонажа полностью? Это действие необратимо.')) return;
  closeLightbox();
  await deleteCharacter(currentCharId);
}

async function regenerateCharacterFromLightbox() {
  if (!currentCharId) return;
  const wishes = (document.getElementById('lb-regen-wishes').value || '').trim();
  const regenOutfits = document.getElementById('lb-regen-outfits').checked;
  const rewriteAppearance = document.getElementById('lb-rewrite-appearance')?.checked || false;
  // Provider override from dropdown — '' = auto (server uses series default).
  const provider = (document.getElementById('lb-regen-provider')?.value || '').trim();
  const status = document.getElementById('lb-regen-status');
  const btn = document.getElementById('lb-regen-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.className = 'lb-status';
  const providerLabel = provider ? ` через ${provider}` : '';
  status.textContent = (rewriteAppearance ? 'Переписываем описание + ' : '') +
    'Перегенерируем основной образ' + (regenOutfits ? ' + костюмы' : '') + providerLabel + ' (~15-30 сек на каждый)...';

  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/characters/${currentCharId}/regenerate`,
      { wishes, regenerate_outfits: regenOutfits, rewrite_appearance: rewriteAppearance, provider }
    );
    if (res.error) {
      status.className = 'lb-status err';
      status.textContent = 'Ошибка: ' + res.error;
      btn.disabled = false;
      btn.innerHTML = '↻ Перегенерировать';
      return;
    }
    const totalOutfits = (res.regenerated_outfits || []).length;
    const failed = res.failed_outfits || [];
    status.className = failed.length ? 'lb-status' : 'lb-status ok';
    let msg = '✓ Основной образ обновлён';
    if (regenOutfits) msg += ` · костюмов: ${totalOutfits}`;
    if (failed.length) msg += ` · ошибок: ${failed.length} (${failed.join(', ')})`;
    status.textContent = msg;

    // Bump the global asset version BEFORE re-rendering so every URL
    // built by `assetUrl()` (sidebar miniatures, outfit grid, episode
    // thumbnails) carries the new `?v=` token. Without this, the canonical
    // filename stays the same across regenerations and the browser's HTTP
    // cache serves the OLD image to every view except the lightbox (which
    // builds its own ad-hoc cache-buster below). User saw new variant in
    // lightbox, then sidebar miniature kept showing the old one and on
    // re-open the cached old image came back.
    bumpAssetVersion();
    // Refresh state
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const c = S.series.characters.find(x => x.id === currentCharId);
    renderCharAssetsGrid(c, true);
    renderOutfitsList(c);
    renderCharactersList();
    if (typeof renderEpCharacters === 'function') renderEpCharacters();  // episode-view sidebar

    // Swap lightbox image to the freshly generated base (cache-bust)
    const freshUrl = res.base_url + '?t=' + Date.now();
    const lbImg = document.querySelector('#lightbox-overlay .lb-img-wrap img');
    if (lbImg) lbImg.src = freshUrl;

    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать ещё раз';
  } catch (e) {
    status.className = 'lb-status err';
    status.textContent = 'Ошибка: ' + e.message;
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

async function uploadCharacterRefs(charId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/character/${charId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const c = S.series.characters.find(x => x.id === charId);
  renderCharAssetsGrid(c);
  renderCharactersList();
}

async function deleteCharRef(charId, filename) {
  await api.del(`/api/series/${S.seriesId}/assets/character/${charId}/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const c = S.series.characters.find(x => x.id === charId);
  renderCharAssetsGrid(c);
  renderCharactersList();
}

