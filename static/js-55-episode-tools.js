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

// ── Inline logic-check (after script gen, before reteller prompt) ────────────
// Stores the violations returned from the dry-run audit so we can re-send them
// to the doctor with the user's selection.
let _logicViolations = [];

function hideLogicPanel() {
  const panel = document.getElementById('ep-logic-panel');
  if (panel) panel.classList.add('hidden');
  _logicViolations = [];
}

async function runLogicCheckThenContinue() {
  const status = document.getElementById('ep-script-gen-status');
  const panel = document.getElementById('ep-logic-panel');
  hideLogicPanel();
  if (status) {
    status.textContent = '🩺 Проверяем логику с учётом предыдущей серии...';
    status.style.color = 'var(--warning)';
  }
  let report;
  try {
    report = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/doctor-script`,
      { dry_run: true },
      { timeoutMs: 180_000 }
    );
  } catch (e) {
    // If the audit itself blew up — don't block the user, just go to reteller.
    console.warn('[logic-check] audit failed → continuing to reteller', e);
    if (status) status.textContent = `⚠ Доктор не отработал (${e.message}) — продолжаем к Reteller`;
    triggerRetellerAfterLogic();
    return;
  }
  const v = (report.violations || []).filter(x => x && x.explanation);
  // Only block on real critical issues — minors are informational.
  const critical = v.filter(x => (x.severity || 'critical') === 'critical');
  if (critical.length === 0) {
    if (status) {
      status.textContent = '✓ Логика чиста — генерируем промпт Reteller...';
      status.style.color = 'var(--success)';
    }
    triggerRetellerAfterLogic();
    return;
  }
  // Show inline panel with checkboxes
  _logicViolations = critical;
  renderLogicViolations(critical);
  if (panel) panel.classList.remove('hidden');
  if (status) {
    status.textContent = `⚠ Найдено ${critical.length} логич. замечаний — отметь, что чинить, или игнорируй всё`;
    status.style.color = 'var(--warning)';
  }
  // Scroll panel into view so the user notices it
  panel?.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// Pending canon audits keyed by `${seriesId}:${episodeNum}` so that a result
// arriving while the user is on a different page shows up when they navigate
// back, not on whatever page they're currently looking at.
const _pendingCanonAudits = {};
let _canonViolations = [];   // current panel's violations (for fix flow)
let _canonPanelCtx = null;   // { seriesId, episodeNum } the visible panel belongs to

function _canonKey(seriesId, episodeNum) { return `${seriesId}:${episodeNum}`; }

// Called by extractCharsFromScript with full ctx (seriesId, episodeNum). Stores
// the result in pending and renders ONLY if the user is currently on the
// matching episode page.
function deliverCanonAudit(canon, ctx) {
  if (!canon || !canon.audited || canon.passes || !(canon.violations || []).length) {
    // Clean → drop any pending entry + any visible panel for this episode
    if (ctx) {
      delete _pendingCanonAudits[_canonKey(ctx.seriesId, ctx.episodeNum)];
      if (_canonPanelCtx
          && _canonPanelCtx.seriesId === ctx.seriesId
          && _canonPanelCtx.episodeNum === ctx.episodeNum) {
        _removeCanonPanel();
      }
    }
    return;
  }
  const key = _canonKey(ctx.seriesId, ctx.episodeNum);
  _pendingCanonAudits[key] = { canon, ctx };
  // Render only if we're on the matching episode right now
  const onEpisodeView = !document.getElementById('view-episode')?.classList.contains('hidden');
  if (onEpisodeView && S.seriesId === ctx.seriesId && S.episodeNum === ctx.episodeNum) {
    renderCanonAuditPanel(canon, ctx);
  }
  // Otherwise it stays in _pendingCanonAudits and gets rendered when the user
  // navigates to the right episode (see hook in loadEpisodeView below).
}

// Called from loadEpisodeView once the episode is loaded — surfaces any
// pending canon audit waiting for this episode.
function flushPendingCanonForCurrentEpisode() {
  if (!S.seriesId || !S.episodeNum) return;
  const key = _canonKey(S.seriesId, S.episodeNum);
  const pending = _pendingCanonAudits[key];
  if (pending) {
    renderCanonAuditPanel(pending.canon, pending.ctx);
  } else {
    _removeCanonPanel();
  }
}

function _removeCanonPanel() {
  const p = document.getElementById('ep-canon-panel');
  if (p) p.remove();
  _canonPanelCtx = null;
  _canonViolations = [];
}

function renderCanonAuditPanel(canon, ctx) {
  const violations = (canon.violations || []).filter(v => v);
  if (!violations.length) { _removeCanonPanel(); return; }

  let panel = document.getElementById('ep-canon-panel');
  if (!panel) {
    const anchor = document.getElementById('ep-script')?.closest('.field-group') || document.getElementById('ep-script')?.parentElement;
    if (!anchor) return;
    panel = document.createElement('div');
    panel.id = 'ep-canon-panel';
    panel.className = 'logic-panel';
    anchor.insertAdjacentElement('afterend', panel);
  }
  _canonViolations = violations;
  _canonPanelCtx = { seriesId: ctx.seriesId, episodeNum: ctx.episodeNum };

  const critCount = violations.filter(v => (v.severity || 'critical') === 'critical').length;
  panel.innerHTML = `
    <div class="logic-panel-head">
      <strong>📚 Канон-аудит: нарушения (${violations.length}${critCount && critCount !== violations.length ? `, критич. ${critCount}` : ''})</strong>
      <span class="logic-panel-hint" style="color:var(--muted);font-size:0.78rem">Канон НЕ обновлён до устранения проблем</span>
    </div>
    <div class="logic-panel-toolbar">
      <button type="button" class="btn-ghost btn-sm" id="ep-canon-toggle-all">Выделить всё</button>
      <span id="ep-canon-count" style="color:var(--muted);font-size:0.78rem">отмечено: 0</span>
    </div>
    <div class="logic-panel-body" id="ep-canon-list">
      ${violations.map((v, i) => `
        <label class="logic-violation" data-idx="${i}">
          <input type="checkbox" class="canon-violation-cb" data-idx="${i}">
          <div class="logic-violation-body">
            <div class="logic-violation-head">
              <span class="logic-tag logic-tag-${esc(v.severity || 'critical')}">${esc(v.severity || 'critical')}</span>
              <span class="logic-type">${esc(v.type || v.rule || '?')}</span>
              ${v.where ? `<span class="logic-where">@ ${esc(v.where)}</span>` : ''}
            </div>
            <div class="logic-explain">${esc(v.explanation || v.message || '')}</div>
            ${v.fix ? `<div class="logic-fix"><b>Решение:</b> ${esc(v.fix)}</div>` : ''}
          </div>
        </label>
      `).join('')}
    </div>
    <div class="logic-panel-actions">
      <button type="button" class="btn-primary" id="ep-canon-fix-btn" onclick="fixSelectedCanonViolations()">🩹 Внести изменения</button>
      <button type="button" class="btn-ghost" id="ep-canon-skip-btn" onclick="ignoreCanonViolations()">Игнорировать все несостыковки</button>
    </div>
    <div id="ep-canon-status" class="logic-explain" style="opacity:.75;margin-top:6px;font-size:12px"></div>
  `;
  // Wire toggle-all + checkbox counter
  const list = panel.querySelector('#ep-canon-list');
  const counter = panel.querySelector('#ep-canon-count');
  const updateCount = () => {
    const n = list.querySelectorAll('.canon-violation-cb:checked').length;
    counter.textContent = `отмечено: ${n}`;
  };
  list.addEventListener('change', e => {
    if (e.target.classList.contains('canon-violation-cb')) updateCount();
  });
  panel.querySelector('#ep-canon-toggle-all').addEventListener('click', () => {
    const boxes = list.querySelectorAll('.canon-violation-cb');
    const allChecked = [...boxes].every(b => b.checked);
    boxes.forEach(b => { b.checked = !allChecked; });
    panel.querySelector('#ep-canon-toggle-all').textContent = allChecked ? 'Выделить всё' : 'Снять выделение';
    updateCount();
  });
  panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function _getSelectedCanonViolations() {
  const out = [];
  document.querySelectorAll('#ep-canon-list .canon-violation-cb').forEach(cb => {
    if (cb.checked) {
      const i = parseInt(cb.dataset.idx, 10);
      if (!isNaN(i) && _canonViolations[i]) out.push(_canonViolations[i]);
    }
  });
  return out;
}

async function fixSelectedCanonViolations() {
  const ctx = _canonPanelCtx;
  if (!ctx) return;
  const picked = _getSelectedCanonViolations();
  const status = document.getElementById('ep-canon-status');
  if (!picked.length) {
    if (status) { status.textContent = 'Отметь хотя бы одно нарушение — или нажми «Игнорировать все несостыковки»'; status.style.color = 'var(--warning)'; }
    return;
  }
  const fixBtn = document.getElementById('ep-canon-fix-btn');
  const skipBtn = document.getElementById('ep-canon-skip-btn');
  const orig = fixBtn.innerHTML;
  fixBtn.disabled = true; if (skipBtn) skipBtn.disabled = true;
  fixBtn.innerHTML = '<span class="spinner"></span> Чиним...';
  if (status) { status.textContent = 'Доктор переписывает сцены...'; status.style.color = 'var(--muted)'; }

  try {
    const d = await trackTask(`Канон-фикс Эп. ${ctx.episodeNum}`, ctx, async () => {
      const r = await fetch(`/api/series/${ctx.seriesId}/episodes/${ctx.episodeNum}/doctor-script`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ violations: picked }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'doctor failed');
      return j;
    });

    if (d.changed) {
      // If user is currently on this same episode, refresh the textarea
      if (S.seriesId === ctx.seriesId && S.episodeNum === ctx.episodeNum) {
        const ta = document.getElementById('ep-script');
        if (ta) {
          ta.value = d.script;
          const cnt = document.getElementById('script-char-count');
          if (cnt) cnt.textContent = d.script.length;
        }
        // Reload episode meta
        try {
          const epR = await fetch(`/api/series/${ctx.seriesId}/episodes/${ctx.episodeNum}`);
          if (epR.ok) S.episode = await epR.json();
        } catch {}
      }
      if (status) { status.textContent = `✓ Сценарий обновлён, исправлено: ${d.violations_fixed || picked.length}. Запусти «Извлечь персонажей» ещё раз, чтобы канон обновился.`; status.style.color = 'var(--success)'; }
      // Drop pending: user must re-run extract to re-audit
      delete _pendingCanonAudits[_canonKey(ctx.seriesId, ctx.episodeNum)];
      setTimeout(() => _removeCanonPanel(), 2500);
    } else {
      if (status) { status.textContent = d.message || 'Без изменений.'; status.style.color = 'var(--warning)'; }
    }
  } catch (e) {
    if (status) { status.textContent = 'Ошибка: ' + e.message; status.style.color = 'var(--danger)'; }
  } finally {
    fixBtn.disabled = false; fixBtn.innerHTML = orig;
    if (skipBtn) skipBtn.disabled = false;
  }
}

function ignoreCanonViolations() {
  const ctx = _canonPanelCtx;
  if (ctx) delete _pendingCanonAudits[_canonKey(ctx.seriesId, ctx.episodeNum)];
  _removeCanonPanel();
}

function renderLogicViolations(items) {
  const list = document.getElementById('ep-logic-list');
  const counter = document.getElementById('ep-logic-count');
  if (!list) return;
  if (counter) counter.textContent = items.length;
  list.innerHTML = items.map((v, i) => `
    <label class="logic-violation" data-idx="${i}">
      <input type="checkbox" class="logic-violation-cb" data-idx="${i}">
      <div class="logic-violation-body">
        <div class="logic-violation-head">
          <span class="logic-tag logic-tag-${esc(v.severity || 'critical')}">${esc(v.severity || 'critical')}</span>
          <span class="logic-type">${esc(v.type || '?')}</span>
          ${v.where ? `<span class="logic-where">@ ${esc(v.where)}</span>` : ''}
        </div>
        <div class="logic-explain">${esc(v.explanation || '')}</div>
        ${v.fix ? `<div class="logic-fix"><b>Фикс:</b> ${esc(v.fix)}</div>` : ''}
      </div>
    </label>
  `).join('');
}

function toggleAllLogicViolations() {
  const boxes = document.querySelectorAll('.logic-violation-cb');
  const allChecked = [...boxes].every(b => b.checked);
  boxes.forEach(b => { b.checked = !allChecked; });
  document.getElementById('ep-logic-toggle-all').textContent = allChecked ? 'Выделить всё' : 'Снять выделение';
}

function getSelectedLogicViolations() {
  const picked = [];
  document.querySelectorAll('.logic-violation-cb').forEach(cb => {
    if (cb.checked) {
      const i = parseInt(cb.dataset.idx, 10);
      if (!isNaN(i) && _logicViolations[i]) picked.push(_logicViolations[i]);
    }
  });
  return picked;
}

async function fixSelectedLogicIssues() {
  const picked = getSelectedLogicViolations();
  const status = document.getElementById('ep-script-gen-status');
  if (!picked.length) {
    showToast('Отметь хотя бы одно замечание — или нажми «Игнорировать всё»');
    return;
  }
  const fixBtn = document.getElementById('ep-logic-fix-btn');
  const skipBtn = document.getElementById('ep-logic-skip-btn');
  const orig = fixBtn.innerHTML;
  fixBtn.disabled = true; if (skipBtn) skipBtn.disabled = true;
  fixBtn.innerHTML = '<span class="spinner"></span> Лечим...';
  if (status) {
    status.textContent = `🩹 Доктор чинит ${picked.length} замечаний...`;
    status.style.color = 'var(--warning)';
  }
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/doctor-script`,
      { violations: picked },
      { timeoutMs: 240_000 }
    );
    if (res.script) {
      setVal('ep-script', res.script);
      S.episode.script = res.script;
      updateScriptCounter();
    }
    hideLogicPanel();
    if (status) {
      status.textContent = `✓ Починено ${picked.length} — генерируем промпт Reteller...`;
      status.style.color = 'var(--success)';
    }
    triggerRetellerAfterLogic();
  } catch (e) {
    if (status) {
      status.textContent = 'Ошибка доктора: ' + e.message;
      status.style.color = 'var(--danger)';
    }
    fixBtn.disabled = false; if (skipBtn) skipBtn.disabled = false;
    fixBtn.innerHTML = orig;
  }
}

function skipLogicAndContinue() {
  hideLogicPanel();
  const status = document.getElementById('ep-script-gen-status');
  if (status) {
    status.textContent = 'Игнорируем замечания — генерируем промпт Reteller...';
    status.style.color = 'var(--success)';
  }
  triggerRetellerAfterLogic();
}

function triggerRetellerAfterLogic() {
  const status = document.getElementById('ep-script-gen-status');
  generateEpRettellerPrompt().then(() => {
    if (status) {
      status.textContent = '✓ Готово';
      setTimeout(() => { if (status) status.textContent = ''; }, 3000);
    }
  }).catch(() => {
    if (status) {
      status.textContent = '✓ Сценарий готов (промпт Reteller — нажми ⚡)';
      setTimeout(() => { if (status) status.textContent = ''; }, 5000);
    }
  });
}

function renderEpLocations() {
  const el = document.getElementById('ep-locations-list');
  if (!el) return;
  const locs = S.series.locations || [];
  if (!locs.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">Нет локаций</div>';
    return;
  }
  const used = S.episode.locations_used || [];
  const seedanceMode = document.body.classList.contains('seedance-mode');
  el.innerHTML = locs.map(l => {
    const hasRef = l.ref_images && l.ref_images.length > 0;
    const imgUrl = hasRef ? `${assetUrl(l.ref_images[0])}` : null;
    const inEp = used.includes(l.id);
    const dragAttrs = (seedanceMode && hasRef)
      ? `draggable="true" ondragstart="sdLocDragStart(event,'${l.id}')"` : '';
    return `
      <div class="ep-loc-row ${inEp ? 'in-episode' : ''}" id="ep-loc-${l.id}" ${dragAttrs}
           ondragover="event.preventDefault();this.classList.add('drop-hover')"
           ondragleave="this.classList.remove('drop-hover')"
           ondrop="event.preventDefault();this.classList.remove('drop-hover');dropLocPhoto(event,'${l.id}')">
        <div class="ep-loc-thumb"
             ${imgUrl ? `onclick="event.stopPropagation();openLocLightbox('${l.id}','${imgUrl}')" style="cursor:zoom-in"
                         title="Открыть локацию (можно перегенерировать с пожеланиями)"` : ''}>
          ${imgUrl ? `<img src="${imgUrl}" alt="" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : '📍'}
        </div>
        <div class="ep-loc-name">
          <div class="ep-char-name-row">
            <input type="checkbox" ${inEp ? 'checked' : ''} onchange="toggleEpLoc('${l.id}',this)">
            <span>${esc(l.name)}</span>
          </div>
          ${!hasRef ? `
            <div class="ep-char-gen-btns" id="ep-loc-btns-${l.id}">
              <button class="btn-prompt" onclick="showLocPrompt('${l.id}')">📋 Промпт</button>
              <button class="btn-generate" id="ep-loc-gen-btn-${l.id}" onclick="generateLocImage('${l.id}')">⚡ Сгенерировать</button>
            </div>
            <div class="ep-char-gen-status" id="ep-loc-status-${l.id}"></div>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function toggleEpLoc(locId, cb) {
  if (!S.episode.locations_used) S.episode.locations_used = [];
  if (cb.checked) {
    if (!S.episode.locations_used.includes(locId)) S.episode.locations_used.push(locId);
  } else {
    S.episode.locations_used = S.episode.locations_used.filter(x => x !== locId);
  }
  document.getElementById(`ep-loc-${locId}`)?.classList.toggle('in-episode', cb.checked);
}

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

