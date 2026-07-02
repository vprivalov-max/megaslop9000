// Apply manual edits to the script textarea — saves what's currently typed
// to the episode on disk, surfaces the result in the gen-status line.
// ──────────────────────────────────────────────────────────────────────────
// "Принять сценарий" — single-click flow that replaces the old multi-step
// dance (Edit script → 🤖 Извлечь → 🎬 Сцены → manual generate). Steps:
//   1. Save the script to disk.
//   2. Snapshot existing chars/locs/items IDs (the "before" set).
//   3. Run extract-characters (adds new chars + locs to series.json).
//   4. Run detect-items (adds new plot-relevant items + items_used).
//   5. Diff vs the snapshot → list of NEW entities.
//   6. If anything new → show modal with empty drop-cards for each new
//      entity. User can drag images. Buttons:
//        ← Назад к редактированию  |  ✅ Принять и сгенерировать недостающие
//   7. On accept: open scene view automatically, kick off the autogen
//      sweep, and show the prominent progress banner above the script.
//   8. If nothing new (or user declines drops): same — just sweep + banner.
//
// The progress banner polls /auto-generate/status and updates the counter,
// the bar, and the "сейчас: <name>" line every 2s. While it's visible, the
// user is warned not to start parallel generations (Reteller / Seedance /
// per-asset clicks) because they'd race the sweep.
// ──────────────────────────────────────────────────────────────────────────

async function acceptScript(opts = {}) {
  if (!S.episode) { alert('Сначала открой эпизод'); return; }
  const script = (document.getElementById('ep-script').value || '').trim();
  if (!script) { alert('Сценарий пустой — впиши или сгенерируй сначала'); return; }
  // Already-accepted shortcut: episode has cast_extracted=true → no need to
  // re-run LLM extraction. Just open scene view (which is what the user
  // expected after their first accept anyway). Pass opts.force=true to
  // override and re-analyse from scratch (button "🔄 Перепроанализировать").
  if (!opts.force && S.episode.cast_extracted === true) {
    const sceneView = document.getElementById('ep-script-scenes');
    if (sceneView && sceneView.classList.contains('hidden')) {
      toggleSceneView();
    }
    showToast('✓ Сценарий уже принят — открыл режим сцен', 3500);
    return;
  }
  const btn = document.getElementById('ep-accept-script-btn');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Анализирую...';
  try {
    // 1+2: Save script + snapshot before-IDs. Capture the PUT response so we
    // can surface any outfits the [BLOCKING] sync auto-created — without this
    // the toast/banner from _notifyNewOutfits never fires inside the accept
    // flow and the user has no feedback that new outfits were detected.
    const putResp = await api.put(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`, { script });
    if (putResp && putResp._new_outfits && putResp._new_outfits.length) {
      _notifyNewOutfits(putResp._new_outfits);
    }
    const beforeChars = new Set((S.series?.characters || []).map(c => c.id));
    const beforeLocs  = new Set((S.series?.locations  || []).map(l => l.id));
    const beforeItems = new Set((S.series?.items      || []).map(it => it.id));

    // 3: extract chars + locs.
    btn.innerHTML = '<span class="spinner"></span> Ищу персонажей и локации...';
    const ext = await fetch(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/extract-characters`, { method: 'POST' })
      .then(r => r.json());
    if (ext.error) throw new Error('extract-characters: ' + ext.error);
    if (ext.series) S.series = ext.series;

    // 4: detect items (plot-relevant). Best-effort — don't fail accept if items endpoint errs.
    btn.innerHTML = '<span class="spinner"></span> Ищу сюжетные предметы...';
    try {
      const det = await api.post(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/detect-items`, {});
      if (det && !det.error) {
        const fresh = await api.get(`/api/series/${S.seriesId}`);
        if (fresh) S.series = fresh;
      }
    } catch (e) {
      console.warn('[acceptScript] item detection failed (continuing):', e);
    }

    // Refresh episode (items_used etc may have changed).
    try {
      const ep = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
      if (ep) S.episode = ep;
    } catch {}

    // 5: diff.
    const newChars = (S.series.characters || []).filter(c => !beforeChars.has(c.id));
    const newLocs  = (S.series.locations  || []).filter(l => !beforeLocs.has(l.id));
    const newItems = (S.series.items      || []).filter(it => !beforeItems.has(it.id));

    // 6: modal or proceed.
    const total = newChars.length + newLocs.length + newItems.length;
    if (total > 0) {
      // Show modal with drop cards. User decides next step.
      _showAcceptScriptModal({ newChars, newLocs, newItems });
    } else {
      // Nothing new — straight to scene view + sweep.
      _proceedAfterAccept();
    }
  } catch (e) {
    alert('Ошибка приёма сценария: ' + (e?.message || e));
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Holds per-section "skip generation" flags during the accept modal lifecycle.
// When user ticks "Не генерить" for a section, those entity ids land in
// _acceptModalSkipIds[kind] and get filtered out when the autogen sweep
// inspects "what's missing". Implementation: we set a sentinel field
// `_skip_autogen=true` on the entity in series.json so the sweep skips it.
// (Persisted so accidental refresh doesn't lose the choice; user can flip
// it back later via the per-asset card.)
let _acceptModalSkipKinds = new Set();

function _showAcceptScriptModal({ newChars, newLocs, newItems }) {
  const sectionHtml = (title, kind, list) => list.length ? `
    <div class="accept-modal-section">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px">
        <h4 style="margin:0">${esc(title)} (${list.length})</h4>
        <label style="display:inline-flex;align-items:center;gap:6px;font-size:0.82rem;color:var(--muted);cursor:pointer"
               title="Не запускать автоген для этой группы — карточки создадутся, но фото нужно будет сгенерировать вручную позже.">
          <input type="checkbox" class="amc-skip-cb" data-kind="${kind}"
                 onchange="_toggleAcceptSkip('${kind}', this.checked)">
          🚫 Не генерить эту группу
        </label>
      </div>
      <div class="accept-modal-grid" id="acc-grid-${kind}">
        ${list.map(e => _acceptCellHtml(kind, e)).join('')}
      </div>
    </div>` : '';

  // Reset state from any previous open.
  _acceptModalSkipKinds = new Set();

  closeLightbox();
  const overlay = document.createElement('div');
  overlay.id = 'lightbox-overlay';
  overlay.className = 'lightbox-overlay';
  overlay.style.zIndex = 99998;
  overlay.dataset.acceptModal = '1';
  // Stash the entity lists on the DOM so confirmAcceptModal can consult them.
  overlay.dataset.newChars = JSON.stringify(newChars.map(c => c.id));
  overlay.dataset.newLocs  = JSON.stringify(newLocs.map(l  => l.id));
  overlay.dataset.newItems = JSON.stringify(newItems.map(it => it.id));
  overlay.innerHTML = `
    <button class="lb-close" onclick="closeAcceptModal()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()" style="max-width:880px;flex-direction:column;max-height:92vh">
      <div class="lightbox-panel" style="width:100%;max-height:92vh;overflow-y:auto">
        <h3>В сценарии нашлось новое</h3>
        <div class="hint">
          Можешь перетащить готовые фотки (drag &amp; drop) на любую карточку — те, на которые не закинешь, сгенерируются автоматически.
          Поставь 🚫 «Не генерить эту группу» рядом с заголовком чтобы пропустить генерацию (карточки останутся пустыми, сгенеришь позже вручную).
        </div>
        ${sectionHtml('🧑 Персонажи', 'char', newChars)}
        ${sectionHtml('📍 Локации',   'loc',  newLocs)}
        ${sectionHtml('🎒 Предметы',  'item', newItems)}
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;justify-content:flex-end">
          <button class="btn-ghost" onclick="closeAcceptModal()">← Назад к редактированию</button>
          <button class="btn-regen" onclick="confirmAcceptModal()">✅ Принять и сгенерировать недостающие →</button>
        </div>
      </div>
    </div>`;
  overlay.onclick = (e) => { if (e.target === overlay) closeAcceptModal(); };
  document.body.appendChild(overlay);
}

function _toggleAcceptSkip(kind, on) {
  if (on) _acceptModalSkipKinds.add(kind);
  else    _acceptModalSkipKinds.delete(kind);
  // Visually dim the cells in this section so user sees the state.
  const grid = document.getElementById(`acc-grid-${kind}`);
  if (grid) grid.style.opacity = on ? '0.35' : '1';
}

function _acceptCellHtml(kind, entity) {
  const sid = S.seriesId;
  const hasRef = entity.ref_images && entity.ref_images.length > 0;
  const photoUrl = hasRef ? `/assets/${sid}/${entity.ref_images[0]}` : null;
  const icon = kind === 'char' ? '👤' : (kind === 'loc' ? '📍' : '🎒');
  return `
    <div class="accept-modal-cell ${hasRef ? 'has-photo' : ''}"
         id="acc-cell-${kind}-${entity.id}"
         ondragover="event.preventDefault();this.classList.add('drop-hover')"
         ondragleave="this.classList.remove('drop-hover')"
         ondrop="event.preventDefault();this.classList.remove('drop-hover');acceptCellDrop(event,'${kind}','${entity.id}')">
      <div class="amc-thumb">
        ${photoUrl ? `<img src="${photoUrl}" alt="">` : icon}
      </div>
      <div class="amc-name">${esc(entity.name)}</div>
      <div class="amc-status">${hasRef ? '✓ есть фото' : 'будет сгенерировано'}</div>
    </div>`;
}

async function acceptCellDrop(event, kind, entityId) {
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const endpoint = kind === 'char'
    ? `/api/series/${S.seriesId}/assets/character/${entityId}`
    : kind === 'loc'
    ? `/api/series/${S.seriesId}/assets/location/${entityId}`
    : `/api/series/${S.seriesId}/assets/item/${entityId}`;
  const cell = document.getElementById(`acc-cell-${kind}-${entityId}`);
  if (cell) cell.querySelector('.amc-status').textContent = '⏳ загружаю...';
  try {
    const r = await fetch(endpoint, { method: 'POST', body: fd });
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    // Refresh series and re-render this cell with the new image.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    const collection = kind === 'char' ? S.series.characters : kind === 'loc' ? S.series.locations : S.series.items;
    const updated = (collection || []).find(x => x.id === entityId);
    if (updated && cell) {
      cell.outerHTML = _acceptCellHtml(kind, updated);
    }
  } catch (e) {
    if (cell) cell.querySelector('.amc-status').textContent = '✗ ' + (e?.message || e);
  }
}

function closeAcceptModal() {
  const ov = document.getElementById('lightbox-overlay');
  if (ov) ov.remove();
}

async function confirmAcceptModal() {
  // Persist per-section skip flags onto the new entities BEFORE closing the
  // modal — the autogen sweep reads `_skip_autogen=true` from series.json
  // and silently bypasses those entities. The user can flip the flag back
  // later via the per-asset "Сгенерировать" click (handled separately).
  const overlay = document.getElementById('lightbox-overlay');
  if (overlay && overlay.dataset.acceptModal === '1' && _acceptModalSkipKinds.size) {
    const payload = { skip: true, chars: [], locs: [], items: [] };
    if (_acceptModalSkipKinds.has('char')) payload.chars = JSON.parse(overlay.dataset.newChars || '[]');
    if (_acceptModalSkipKinds.has('loc'))  payload.locs  = JSON.parse(overlay.dataset.newLocs  || '[]');
    if (_acceptModalSkipKinds.has('item')) payload.items = JSON.parse(overlay.dataset.newItems || '[]');
    try {
      await api.post(`/api/series/${S.seriesId}/skip-autogen`, payload);
      bumpAssetVersion();
      // Refresh series to pick up the persisted flags.
      const fresh = await api.get(`/api/series/${S.seriesId}`);
      if (fresh) S.series = fresh;
    } catch (e) {
      console.warn('[acceptScript] failed to persist skip flags', e);
    }
  }
  closeAcceptModal();
  await _proceedAfterAccept();
}

async function _proceedAfterAccept() {
  // Switch to scene view automatically (replaces the manual 🎬 Сцены click).
  try {
    const view = document.getElementById('ep-script-scenes');
    const ta = document.getElementById('ep-script');
    const btn = document.getElementById('ep-scenes-btn');
    if (view?.classList.contains('hidden')) {
      // Reuse the existing toggle to keep all side-effects consistent.
      toggleSceneView();
    }
  } catch (e) { console.warn('[acceptScript] scene-view switch failed', e); }

  // Kick off the autogen sweep + show the progress banner.
  showAcceptProgressBanner();
  try {
    await fetch(`/api/series/${S.seriesId}/auto-generate/sweep`, { method: 'POST' });
  } catch (e) {
    console.warn('[acceptScript] sweep trigger failed', e);
  }
  // Reuse the existing poller — its UI updates the corner status; we mirror
  // the counts into the big banner.
  pollAutogenStatus();
  _startAcceptBannerPoll();
}

function showAcceptProgressBanner() {
  const el = document.getElementById('ep-accept-progress');
  if (el) el.classList.remove('hidden');
}
function hideAcceptProgressBanner() {
  const el = document.getElementById('ep-accept-progress');
  if (el) el.classList.add('hidden');
  if (_acceptBannerTimer) { clearInterval(_acceptBannerTimer); _acceptBannerTimer = null; }
}

let _acceptBannerTimer = null;
function _startAcceptBannerPoll() {
  if (_acceptBannerTimer) clearInterval(_acceptBannerTimer);
  let stillCount = 0;
  const tick = async () => {
    try {
      const st = await fetch(`/api/series/${S.seriesId}/auto-generate/status`).then(r => r.json());
      const counter = document.getElementById('eap-counter');
      const now = document.getElementById('eap-now');
      const fill = document.getElementById('eap-bar-fill');
      if (counter) counter.textContent = `${st.done || 0} / ${st.queue || 0}`;
      const ipBits = (st.in_progress || []).map(x => x.name).filter(Boolean).slice(0, 3).join(', ');
      if (now) now.textContent = ipBits ? `· сейчас: ${ipBits}` : '';
      if (fill && st.queue) fill.style.width = `${Math.min(100, Math.round(100 * (st.done || 0) / st.queue))}%`;
      if (!st.running) {
        stillCount += 1;
        // Wait a couple ticks of "not running" before hiding (avoids flicker
        // between back-to-back sweeps if the recursive re-run triggers).
        if (stillCount >= 2) {
          if (fill) fill.style.width = '100%';
          setTimeout(hideAcceptProgressBanner, 1500);
        }
      } else {
        stillCount = 0;
      }
    } catch {}
  };
  tick();
  _acceptBannerTimer = setInterval(tick, 2000);
}

async function applyScriptChanges() {
  if (!S.episode) return;
  const btn = document.getElementById('ep-script-apply-btn');
  const status = document.getElementById('ep-script-gen-status');
  const newScript = val('ep-script');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Сохраняю...'; }
  if (status) { status.textContent = ''; }
  try {
    const updated = await api.put(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}`,
      { script: newScript }
    );
    S.episode = updated;
    if (status) {
      status.textContent = '✓ Изменения сохранены';
      status.style.color = 'var(--success)';
      setTimeout(() => { if (status.textContent === '✓ Изменения сохранены') status.textContent = ''; }, 4000);
    }
    if (btn) {
      btn.style.display = 'none';
      btn.dataset.dirty = '';
    }
    // Notify about auto-created outfits from SCENE_OPEN
    _notifyNewOutfits(updated._new_outfits);
  } catch (e) {
    if (status) { status.textContent = '✗ ' + (e.message || e); status.style.color = 'var(--danger)'; }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '💾 Применить изменения'; }
  }
}

