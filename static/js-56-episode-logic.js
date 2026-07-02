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

