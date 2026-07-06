// ── Auto-pipeline after «✨ Сгенерировать новые» ──────────────────────────────
// After batch-generation fills the textarea, optionally run the exact chain a
// user does by hand: 🔧 под стандарт → 🧠 проверка логики → 🩹 лечение всех
// проблем → 📜 добавить серии. Each stage is independently toggleable in
// Настройки and defaults ON. While the chain runs, a full-screen overlay blocks
// every click/keypress so nothing can be touched mid-process, and shows live
// per-stage status.
const AUTO_PIPE_KEYS = { adapt: 'auto_pipe_adapt', logic: 'auto_pipe_logic', add: 'auto_pipe_add' };
function autoPipeEnabled(key) { return localStorage.getItem(AUTO_PIPE_KEYS[key]) !== 'false'; }
function setAutoPipeEnabled(key, on) { try { localStorage.setItem(AUTO_PIPE_KEYS[key], on ? 'true' : 'false'); } catch {} }

// Full-screen blocking overlay with a live checklist of pipeline stages.
const AppendPipelineOverlay = (() => {
  let _el = null, _timer = null, _start = 0, _running = false, _keyBlock = null, _steps = [];
  const ICON = {
    pending: '<span style="color:var(--muted);font-size:1.05rem">○</span>',
    run:     '<span class="spinner" style="width:15px;height:15px;border-width:2px;display:inline-block;vertical-align:middle"></span>',
    done:    '<span style="color:#4ade80;font-weight:700">✓</span>',
    skip:    '<span style="color:var(--muted)">–</span>',
    error:   '<span style="color:#f87171;font-weight:700">✗</span>',
  };
  function rowsHtml() {
    return _steps.map(s => {
      const dim = (s.status === 'pending' || s.status === 'skip') ? 'opacity:0.55' : '';
      const note = s.note ? `<span style="color:var(--muted);font-size:0.8rem;margin-left:auto">${esc(s.note)}</span>` : '';
      return `<div style="display:flex;align-items:center;gap:12px;padding:9px 4px;border-bottom:1px solid var(--border);${dim}">
        <span style="flex:0 0 20px;text-align:center">${ICON[s.status] || ICON.pending}</span>
        <span style="font-size:0.92rem">${s.label}</span>${note}
      </div>`;
    }).join('');
  }
  function render() {
    if (!_el) return;
    const body = _el.querySelector('[data-role="rows"]');
    if (body) body.innerHTML = rowsHtml();
  }
  function show(steps) {
    close();
    _start = Date.now(); _running = true;
    _steps = steps.map(s => ({ ...s, status: 'pending', note: '' }));
    _el = document.createElement('div');
    Object.assign(_el.style, {
      position: 'fixed', inset: '0', zIndex: '50000', padding: '20px',
      background: 'rgba(8,10,18,0.82)', backdropFilter: 'blur(3px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    });
    _el.innerHTML = `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);width:480px;max-width:92vw;padding:22px 24px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
          <span style="font-size:1.3rem">🤖</span>
          <div style="flex:1">
            <div style="font-weight:700;font-size:1.02rem">Автоматическая обработка сценария</div>
            <div data-role="sub" style="font-size:0.8rem;color:var(--muted)">Не трогай ничего — идёт процесс…</div>
          </div>
          <span data-role="elapsed" style="font-family:ui-monospace,monospace;font-size:0.85rem;background:var(--surface2);border:1px solid var(--border);padding:3px 8px;border-radius:6px">0:00</span>
        </div>
        <div data-role="rows"></div>
        <div data-role="foot" style="margin-top:16px;display:none;justify-content:flex-end">
          <button class="btn-primary" data-role="close-btn">Закрыть</button>
        </div>
      </div>`;
    document.body.appendChild(_el);
    render();
    // Swallow every keystroke (esp. Escape, which would close the modal) while
    // the pipeline is running.
    _keyBlock = (e) => { if (_running) { e.stopPropagation(); if (e.key === 'Escape') e.preventDefault(); } };
    document.addEventListener('keydown', _keyBlock, true);
    _timer = setInterval(() => {
      if (!_el) return;
      const sec = Math.floor((Date.now() - _start) / 1000);
      const el = _el.querySelector('[data-role="elapsed"]');
      if (el) el.textContent = `${Math.floor(sec / 60)}:${(sec % 60).toString().padStart(2, '0')}`;
    }, 1000);
    return { set, finish, fail, close };
  }
  function set(key, status, note) {
    const s = _steps.find(x => x.key === key);
    if (s) { s.status = status; if (note != null) s.note = note; }
    render();
  }
  function _stopRunning() {
    _running = false;
    if (_timer) { clearInterval(_timer); _timer = null; }
  }
  function _showFoot(label) {
    if (!_el) return;
    const foot = _el.querySelector('[data-role="foot"]');
    const btn = _el.querySelector('[data-role="close-btn"]');
    if (btn) { btn.textContent = label || 'Закрыть'; btn.onclick = close; }
    if (foot) foot.style.display = 'flex';
  }
  // Pipeline ran to the end. autoClose=true means episodes were already added
  // and the modal closed — fade the overlay out on its own.
  function finish(autoClose, msg) {
    _stopRunning();
    if (_el) {
      const sub = _el.querySelector('[data-role="sub"]');
      if (sub) sub.textContent = msg || '✅ Готово';
    }
    if (autoClose) { setTimeout(close, 1100); }
    else { _showFoot('Закрыть'); }
  }
  function fail(msg) {
    _stopRunning();
    if (_el) {
      const sub = _el.querySelector('[data-role="sub"]');
      if (sub) { sub.innerHTML = `<span style="color:#f87171">✗ ${esc(msg || 'Ошибка')}</span>`; }
    }
    _showFoot('Закрыть');
  }
  function close() {
    _stopRunning();
    if (_keyBlock) { document.removeEventListener('keydown', _keyBlock, true); _keyBlock = null; }
    if (_el) { _el.remove(); _el = null; }
  }
  return { show };
})();

// Runs adapt → logic → heal → add against the textarea, driving the overlay.
// Throws on the first failing stage (caller marks the overlay failed).
async function _runAppendPipeline(overlay) {
  const ta = document.getElementById('append-script-text');
  const getScript = () => (ta?.value || '').trim();
  // 1) ADAPT TO STANDARD
  if (autoPipeEnabled('adapt')) {
    overlay.set('adapt', 'run');
    const r = await api.post('/api/adapt-script-to-standard', { script: getScript() }, { timeoutMs: 600000 });
    if (r.error) throw new Error('Подгонка под стандарт: ' + r.error);
    if (r.script) { ta.dataset.preAdaptSnapshot = ta.value; ta.value = r.script; appendUpdateStats(); }
    overlay.set('adapt', 'done');
  } else { overlay.set('adapt', 'skip', 'выключено'); }
  // 2) LOGIC CHECK + 3) HEAL ALL FOUND ISSUES
  if (autoPipeEnabled('logic')) {
    overlay.set('logic', 'run');
    const lc = await api.post('/api/series/import-from-script/logic-check', { script: getScript() }, { timeoutMs: 120000 });
    if (lc.error) throw new Error('Проверка логики: ' + lc.error);
    const issues = lc.issues || [];
    if (!issues.length) {
      overlay.set('logic', 'done', 'чисто');
      overlay.set('heal', 'skip', 'нет проблем');
    } else {
      overlay.set('logic', 'done', `${issues.length} замеч.`);
      overlay.set('heal', 'run');
      const fx = await api.post('/api/series/import-from-script/apply-fixes', { script: getScript(), issues }, { timeoutMs: 180000 });
      if (fx.error) throw new Error('Лечение проблем: ' + fx.error);
      if (fx.script) { ta.dataset.preFixSnapshot = ta.value; ta.value = fx.script; appendUpdateStats(); }
      overlay.set('heal', 'done', `${fx.applied_count ?? issues.length} правок`);
    }
  } else {
    overlay.set('logic', 'skip', 'выключено');
    overlay.set('heal', 'skip', 'выключено');
  }
  // 4) ADD EPISODES
  if (autoPipeEnabled('add')) {
    overlay.set('add', 'run');
    await _appendCommitScript();   // closes the modal + refreshes series on success
    overlay.set('add', 'done');
    overlay.finish(true);
  } else {
    overlay.set('add', 'skip', 'выключено');
    overlay.finish(false, 'Готово — проверь сценарий и нажми «📜 Добавить серии».');
  }
}

async function appendGenerateScript(btn) {
  if (!S.seriesId) { showToast('Открой сериал'); return; }
  const count = parseInt(document.getElementById('append-gen-count')?.value, 10) || 5;
  if (count < 1 || count > 20) { showToast('Количество серий: 1-20', 4000); return; }
  const direction = (document.getElementById('append-gen-direction')?.value || '').trim();
  // Optional advanced parameters — empty = let Claude decide.
  const durationSecRaw = (document.getElementById('append-gen-duration-sec')?.value || '').trim();
  const linesCountRaw  = (document.getElementById('append-gen-lines-count')?.value || '').trim();
  const maxCharsRaw    = (document.getElementById('append-gen-max-chars')?.value || '').trim();
  const styleVal = (document.getElementById('append-gen-style')?.value || '').trim();
  const durationSec = durationSecRaw ? Math.max(30, Math.min(240, parseInt(durationSecRaw, 10))) : null;
  const linesCount  = linesCountRaw  ? Math.max(3, Math.min(40, parseInt(linesCountRaw, 10)))   : null;
  const maxChars    = maxCharsRaw    ? Math.max(1, Math.min(6, parseInt(maxCharsRaw, 10)))      : null;
  const noInterruptions = document.getElementById('append-gen-no-interruptions')?.checked !== false;
  const ta = document.getElementById('append-script-text');
  const statusEl = document.getElementById('append-gen-status');
  if (ta && ta.value.trim() && !await appConfirm({
    title: 'Перезаписать содержимое textarea?',
    message: `В textarea ниже уже есть текст (${ta.value.length} символов). Сгенерированный сценарий заменит его. Продолжить?`,
    okText: 'Да, перезаписать',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;

  // Decide whether the post-generation chain runs. If every stage is off in
  // Настройки, we keep the old manual behavior (preview + manual buttons).
  const usePipeline = autoPipeEnabled('adapt') || autoPipeEnabled('logic') || autoPipeEnabled('add');
  const overlay = usePipeline ? AppendPipelineOverlay.show([
    { key: 'generate', label: '✨ Генерация сценария' },
    { key: 'adapt',    label: '🔧 Подгонка под стандарт' },
    { key: 'logic',    label: '🧠 Проверка логики' },
    { key: 'heal',     label: '🩹 Лечение проблем' },
    { key: 'add',      label: '📜 Добавление серий' },
  ]) : null;

  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Claude пишет…';
  if (statusEl) statusEl.innerHTML = `<span class="spinner"></span> Генерирую ${count} серий… ~${30 * count}-${90 * count}с`;

  try {
    if (overlay) overlay.set('generate', 'run');
    const r = await api.post(
      `/api/series/${S.seriesId}/generate-script-batch`,
      { count, direction, duration_sec: durationSec, lines_count: linesCount, style: styleVal, max_main_chars_per_scene: maxChars, no_interruptions: noInterruptions },
      { timeoutMs: 600_000 },
    );
    if (r.error) throw new Error(r.error);
    if (!r.script) throw new Error('пустой ответ');
    if (ta) {
      ta.value = r.script;
      appendUpdateStats();
    }
    if (statusEl) {
      statusEl.innerHTML = `<span style="color:#4ade80">✓ Сгенерировано ${r.count} серий (№${r.first_episode}–${r.last_episode}). ` +
                          `Можно: 🧠 Проверить логику · 👁 Превью разбивки · 📜 Добавить серии</span>`;
    }
    // Auto-switch to paste mode so user sees the textarea + preview/logic buttons.
    setAppendMode('paste');
    if (overlay) {
      overlay.set('generate', 'done', `№${r.first_episode}–${r.last_episode}`);
      // Re-enable the generate button now — the overlay blocks input anyway, and
      // the chain may close the whole modal on success.
      btn.disabled = false;
      btn.innerHTML = orig;
      await _runAppendPipeline(overlay);
      return;
    }
    showToast(`✓ Сгенерировано ${r.count} серий — проверяй и добавляй`, 6000);
    // Auto-fire preview so the user immediately sees the episode list AND
    // any non-English-dialogue warning (Claude occasionally drifts to RU
    // when the bible/direction are in Russian). The preview re-runs lang
    // detection — it's regex-cheap (~10ms) so the extra call is fine.
    if (typeof appendPreviewSplit === 'function') {
      setTimeout(() => appendPreviewSplit(), 50);
    }
  } catch (e) {
    if (overlay) overlay.fail(e?.message || e);
    if (statusEl) statusEl.innerHTML = `<span style="color:#f87171">✗ Ошибка: ${esc(e?.message || e)}</span>`;
    showToast('Ошибка: ' + (e?.message || e), 6000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}
function appendDropFile(ev) {
  const file = ev.dataTransfer?.files?.[0];
  if (!file) return;
  _appendReadFile(file);
}
function appendPickFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  _appendReadFile(file);
}
function _appendReadFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    setVal('append-script-text', reader.result || '');
    appendUpdateStats();
  };
  reader.readAsText(file);
}
function appendUpdateStats() {
  const txt = (document.getElementById('append-script-text')?.value || '');
  const stats = document.getElementById('append-script-stats');
  if (stats) stats.textContent = `${txt.length.toLocaleString('ru-RU')} символов`;
}
async function appendPreviewSplit() {
  const script = (document.getElementById('append-script-text')?.value || '').trim();
  const previewEl = document.getElementById('append-script-preview');
  if (!script) { previewEl.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  delete _LANG_WARN_KEPT['append-script-text'];
  previewEl.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Анализирую разбивку...</div>';
  try {
    const r = await api.post('/api/series/import-from-script/preview', { script });
    if (r.error) throw new Error(r.error);
    const eps = r.episodes || [];
    const startNum = ((S.episodes || []).reduce((m, e) => Math.max(m, e.number || 0), 0)) + 1;
    if (!eps.length) {
      previewEl.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Не удалось разбить — будет добавлена 1 серия со всем текстом</div>';
      return;
    }
    const warnHtml = _renderLangWarning(r.dialogue_lang_warning, 'append-script-text');
    previewEl.innerHTML = `
      ${warnHtml}
      <div style="font-size:0.85rem;color:var(--success);margin-bottom:6px">
        ✓ Найдено эпизодов: <strong>${eps.length}</strong> · станут сериями <strong>№${startNum}–${startNum + eps.length - 1}</strong>
      </div>
      <div style="max-height:240px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px;background:var(--surface2)">
        ${eps.map((e, i) => `
          <div style="padding:5px 4px;border-bottom:1px solid var(--border);font-size:0.82rem">
            <strong>№${startNum + i}</strong>
            ${e.title ? `<span style="color:var(--muted)"> · ${esc(e.title)}</span>` : ''}
            <span style="color:var(--muted);margin-left:8px">(${e.length.toLocaleString('ru-RU')} симв.)</span>
            <div style="color:var(--muted);font-size:0.75rem;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(e.preview)}</div>
          </div>`).join('')}
      </div>`;
  } catch (e) {
    previewEl.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  }
}
// Last logic-check result, held in module scope so the «Применить» button can
// look up which issues are ticked. Cleared every time check re-runs.
let _appendLogicIssues = [];

async function appendLogicCheck() {
  const script = (document.getElementById('append-script-text')?.value || '').trim();
  const out = document.getElementById('append-script-logic');
  if (!script) { out.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  out.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Claude читает все серии и ищет противоречия… ~15-40 сек</div>';
  _appendLogicIssues = [];
  try {
    const r = await api.post('/api/series/import-from-script/logic-check', { script }, { timeoutMs: 120000 });
    if (r.error) throw new Error(r.error);
    const issues = r.issues || [];
    _appendLogicIssues = issues;
    if (!issues.length) {
      out.innerHTML = `<div style="padding:10px;background:rgba(74,222,128,0.12);border:1px solid rgba(74,222,128,0.35);border-radius:6px;color:#4ade80;font-size:0.85rem">✅ Логика чистая — проанализировано серий: <strong>${r.episodes_analyzed}</strong>. Противоречий не найдено.</div>`;
      return;
    }
    const sevColor = { critical: '#f87171', high: '#fbbf24', medium: '#a78bfa', low: '#9ca3af' };
    const sevLabel = { critical: 'CRIT', high: 'HIGH', medium: 'MED', low: 'LOW' };
    const typeLabel = {
      contradiction:    '⚡ Противоречие',
      plot_hole:        '🕳 Плот-хол',
      forgotten_thread: '🧵 Забытая линия',
      continuity:       '🔗 Continuity',
      timeline:         '⏱ Таймлайн',
    };
    out.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;font-size:0.85rem">
        <span style="color:var(--muted)">🧠 Найдено проблем:</span>
        <strong style="color:#fbbf24">${issues.length}</strong>
        <span style="color:var(--muted)">(серий: ${r.episodes_analyzed})</span>
        <button class="btn-ghost btn-sm" onclick="appendLogicSelectAll(true)" style="margin-left:auto">✓ Все</button>
        <button class="btn-ghost btn-sm" onclick="appendLogicSelectAll(false)">✕ Снять</button>
        <button class="btn-accent btn-sm" onclick="appendLogicApply(this)" title="Claude перепишет сценарий минимально, только исправив выделенные проблемы. Результат подставится в textarea — после этого можно снова «Проверить логику».">🩹 Полечить выбранные</button>
      </div>
      <div style="max-height:340px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;background:var(--surface2)">
        ${issues.map((it, i) => {
          const presetChecked = (it.severity === 'critical' || it.severity === 'high') ? 'checked' : '';
          return `
          <label style="display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--border);font-size:0.82rem;cursor:pointer" data-logic-issue="${i}">
            <input type="checkbox" class="logic-issue-cb" data-idx="${i}" ${presetChecked} style="margin-top:3px;width:16px;height:16px;flex:0 0 16px;accent-color:#10b981">
            <div style="flex:1;min-width:0">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap">
                <span style="background:${sevColor[it.severity] || '#9ca3af'};color:#000;padding:2px 6px;border-radius:4px;font-weight:700;font-size:0.7rem">${sevLabel[it.severity] || it.severity || '?'}</span>
                <span style="color:var(--muted)">${esc(typeLabel[it.type] || it.type || '')}</span>
                <span style="color:var(--accent);font-weight:600;margin-left:auto">Эп. ${(it.episodes || []).join(', ')}</span>
              </div>
              <div style="color:var(--text);font-weight:600;margin-bottom:3px">${esc(it.summary || '')}</div>
              ${it.evidence ? `<div style="color:var(--muted);font-style:italic;font-size:0.78rem;margin-bottom:3px">«${esc(it.evidence)}»</div>` : ''}
              ${it.fix ? `<div style="color:#4ade80;font-size:0.78rem">→ ${esc(it.fix)}</div>` : ''}
            </div>
          </label>`;
        }).join('')}
      </div>
      <div style="margin-top:6px;font-size:0.78rem;color:var(--muted)">
        💡 Поставь галочки на тех проблемах что хочешь починить → «🩹 Полечить выбранные». Critical/High по умолчанию уже отмечены. Можно править textarea вручную и снова жать «Проверить логику».
      </div>`;
  } catch (e) {
    out.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  }
}

function appendLogicSelectAll(val) {
  document.querySelectorAll('.logic-issue-cb').forEach(cb => { cb.checked = val; });
}

async function appendLogicApply(btn) {
  const ta = document.getElementById('append-script-text');
  const script = (ta?.value || '').trim();
  if (!script) { showToast('Сценарий пустой', 3000); return; }
  const selected = [...document.querySelectorAll('.logic-issue-cb:checked')]
    .map(cb => _appendLogicIssues[parseInt(cb.dataset.idx, 10)])
    .filter(Boolean);
  if (!selected.length) {
    showToast('Не выделено ни одной проблемы для лечения', 3000);
    return;
  }
  if (!await appConfirm({
    title: '🩹 Полечить выделенные проблемы?',
    message: `Будет переписано: ${selected.length} проблем(ы).\n\n` +
             `Claude сделает минимальные правки — оставит всё остальное как есть. ` +
             `Получившийся сценарий заменит текущий в textarea (но в любой момент можно нажать Cmd+Z).`,
    okText: '🩹 Полечить',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Claude переписывает… ~30-60 сек';
  const banner = LongOpBanner.show(
    `🩹 Лечим ${selected.length} проблем(ы) логики`,
    [
      'Читаем сценарий и список проблем…',
      'Анализируем затронутые сцены…',
      'Переписываем минимальными правками…',
      'Сохраняем сценарий…',
    ],
  );
  try {
    const r = await api.post('/api/series/import-from-script/apply-fixes',
      { script, issues: selected },
      { timeoutMs: 180000 });
    if (r.error) throw new Error(r.error);
    if (!r.script) throw new Error('пустой ответ');
    // Save undo snapshot (Cmd+Z would only undo characters typed by user; this
    // is a programmatic replace and bypasses native undo). Stash in dataset.
    ta.dataset.preFixSnapshot = script;
    ta.value = r.script;
    appendUpdateStats();
    const changes = r.changes || [];
    const changesHtml = changes.length
      ? changes.map(c => `<li><strong>#${c.issue_index || '?'}:</strong> ${esc(c.summary || '')}</li>`).join('')
      : '<li>(no per-issue summary returned)</li>';
    const out = document.getElementById('append-script-logic');
    if (out) {
      out.innerHTML = `
        <div style="padding:10px 12px;background:rgba(16,185,129,0.10);border:1px solid rgba(16,185,129,0.35);border-radius:6px;font-size:0.82rem">
          <div style="color:#10b981;font-weight:700;margin-bottom:6px">✓ Применено ${r.applied_count} правок. Что изменилось:</div>
          <ul style="margin:6px 0 6px 18px;color:var(--text)">${changesHtml}</ul>
          <div style="display:flex;gap:8px;margin-top:8px">
            <button class="btn-ghost btn-sm" onclick="appendLogicUndo()" title="Вернуть текст до правок">↶ Отменить</button>
            <button class="btn-accent btn-sm" onclick="appendLogicCheck()" title="Перепроверить новый текст на оставшиеся проблемы">🧠 Проверить ещё раз</button>
          </div>
        </div>`;
    }
    showToast(`✓ Применено ${r.applied_count} правок`, 5000);
  } catch (e) {
    showToast('Ошибка лечения: ' + (e?.message || e), 6000);
  } finally {
    banner.close();
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

function appendLogicUndo() {
  const ta = document.getElementById('append-script-text');
  if (!ta || !ta.dataset.preFixSnapshot) {
    showToast('Нет снапшота для отката', 3000);
    return;
  }
  ta.value = ta.dataset.preFixSnapshot;
  delete ta.dataset.preFixSnapshot;
  appendUpdateStats();
  showToast('↶ Откачено к версии до правок', 3000);
  const out = document.getElementById('append-script-logic');
  if (out) out.innerHTML = '';
}

// ── Logic-check for the "New Series → Import" modal ────────────────────────
// Mirror of appendLogicCheck / appendLogicApply / appendLogicUndo but wired
// to import-series-script + import-series-logic instead of append-* elements.

let _importLogicIssues = [];

async function importLogicCheck(btn) {
  const script = (document.getElementById('import-series-script')?.value || '').trim();
  const out = document.getElementById('import-series-logic');
  if (!script) { if (out) out.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Читаю…'; }
  if (out) out.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Claude читает все серии и ищет противоречия… ~15-40 сек</div>';
  _importLogicIssues = [];
  try {
    const r = await api.post('/api/series/import-from-script/logic-check', { script }, { timeoutMs: 120000 });
    if (r.error) throw new Error(r.error);
    const issues = r.issues || [];
    _importLogicIssues = issues;
    if (!issues.length) {
      if (out) out.innerHTML = `<div style="padding:10px;background:rgba(74,222,128,0.12);border:1px solid rgba(74,222,128,0.35);border-radius:6px;color:#4ade80;font-size:0.85rem">✅ Логика чистая — проанализировано серий: <strong>${r.episodes_analyzed}</strong>. Противоречий не найдено.</div>`;
      return;
    }
    const sevColor = { critical: '#f87171', high: '#fbbf24', medium: '#a78bfa', low: '#9ca3af' };
    const sevLabel = { critical: 'CRIT', high: 'HIGH', medium: 'MED', low: 'LOW' };
    const typeLabel = {
      contradiction:    '⚡ Противоречие',
      plot_hole:        '🕳 Плот-хол',
      forgotten_thread: '🧵 Забытая линия',
      continuity:       '🔗 Continuity',
      timeline:         '⏱ Таймлайн',
    };
    if (out) out.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;font-size:0.85rem">
        <span style="color:var(--muted)">🧠 Найдено проблем:</span>
        <strong style="color:#fbbf24">${issues.length}</strong>
        <span style="color:var(--muted)">(серий: ${r.episodes_analyzed})</span>
        <button class="btn-ghost btn-sm" onclick="importLogicSelectAll(true)" style="margin-left:auto">✓ Все</button>
        <button class="btn-ghost btn-sm" onclick="importLogicSelectAll(false)">✕ Снять</button>
        <button class="btn-accent btn-sm" onclick="importLogicApply(this)" title="Claude перепишет сценарий минимально, только исправив выделенные проблемы">🩹 Полечить выбранные</button>
      </div>
      <div style="max-height:300px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;background:var(--surface2)">
        ${issues.map((it, i) => {
          const presetChecked = (it.severity === 'critical' || it.severity === 'high') ? 'checked' : '';
          return `
          <label style="display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--border);font-size:0.82rem;cursor:pointer">
            <input type="checkbox" class="import-logic-cb" data-idx="${i}" ${presetChecked} style="margin-top:3px;width:16px;height:16px;flex:0 0 16px;accent-color:#10b981">
            <div style="flex:1;min-width:0">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap">
                <span style="background:${sevColor[it.severity] || '#9ca3af'};color:#000;padding:2px 6px;border-radius:4px;font-weight:700;font-size:0.7rem">${sevLabel[it.severity] || it.severity || '?'}</span>
                <span style="color:var(--muted)">${esc(typeLabel[it.type] || it.type || '')}</span>
                <span style="color:var(--accent);font-weight:600;margin-left:auto">Эп. ${(it.episodes || []).join(', ')}</span>
              </div>
              <div style="color:var(--text);font-weight:600;margin-bottom:3px">${esc(it.summary || '')}</div>
              ${it.evidence ? `<div style="color:var(--muted);font-style:italic;font-size:0.78rem;margin-bottom:3px">«${esc(it.evidence)}»</div>` : ''}
              ${it.fix ? `<div style="color:#4ade80;font-size:0.78rem">→ ${esc(it.fix)}</div>` : ''}
            </div>
          </label>`;
        }).join('')}
      </div>
      <div style="margin-top:6px;font-size:0.78rem;color:var(--muted)">
        💡 Поставь галочки на тех проблемах что хочешь починить → «🩹 Полечить выбранные». Critical/High по умолчанию уже отмечены.
      </div>`;
  } catch (e) {
    if (out) out.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🧠 Проверить логику'; }
  }
}

function importLogicSelectAll(val) {
  document.querySelectorAll('.import-logic-cb').forEach(cb => { cb.checked = val; });
}

async function importLogicApply(btn) {
  const ta = document.getElementById('import-series-script');
  const script = (ta?.value || '').trim();
  if (!script) { showToast('Сценарий пустой', 3000); return; }
  const selected = [...document.querySelectorAll('.import-logic-cb:checked')]
    .map(cb => _importLogicIssues[parseInt(cb.dataset.idx, 10)])
    .filter(Boolean);
  if (!selected.length) { showToast('Не выделено ни одной проблемы для лечения', 3000); return; }
  if (!await appConfirm({
    title: '🩹 Полечить выделенные проблемы?',
    message: `Будет переписано: ${selected.length} проблем(ы).\n\nClaude сделает минимальные правки — оставит всё остальное как есть.`,
    okText: '🩹 Полечить', cancelText: 'Отмена', okStyle: 'accent',
  })) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Claude переписывает…';
  const banner = LongOpBanner.show(
    `🩹 Лечим ${selected.length} проблем(ы) логики`,
    [
      'Читаем сценарий и список проблем…',
      'Анализируем затронутые сцены…',
      'Переписываем минимальными правками…',
      'Сохраняем сценарий…',
    ],
  );
  try {
    const r = await api.post('/api/series/import-from-script/apply-fixes',
      { script, issues: selected }, { timeoutMs: 180000 });
    if (r.error) throw new Error(r.error);
    if (!r.script) throw new Error('пустой ответ');
    ta.dataset.preFixSnapshot = script;
    ta.value = r.script;
    // Update char counter
    const stats = document.getElementById('import-series-script-stats');
    if (stats) stats.textContent = r.script.length.toLocaleString('ru') + ' символов';
    const changes = r.changes || [];
    const changesHtml = changes.length
      ? changes.map(c => `<li><strong>#${c.issue_index || '?'}:</strong> ${esc(c.summary || '')}</li>`).join('')
      : '<li>(no per-issue summary returned)</li>';
    const out = document.getElementById('import-series-logic');
    if (out) out.innerHTML = `
      <div style="padding:10px 12px;background:rgba(16,185,129,0.10);border:1px solid rgba(16,185,129,0.35);border-radius:6px;font-size:0.82rem">
        <div style="color:#10b981;font-weight:700;margin-bottom:6px">✓ Применено ${r.applied_count} правок. Что изменилось:</div>
        <ul style="margin:6px 0 6px 18px;color:var(--text)">${changesHtml}</ul>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn-ghost btn-sm" onclick="importLogicUndo()" title="Вернуть текст до правок">↶ Отменить</button>
          <button class="btn-accent btn-sm" onclick="importLogicCheck(this)">🧠 Проверить ещё раз</button>
        </div>
      </div>`;
    showToast(`✓ Применено ${r.applied_count} правок`, 5000);
  } catch (e) {
    showToast('Ошибка лечения: ' + (e?.message || e), 6000);
  } finally {
    banner.close();
    btn.disabled = false; btn.innerHTML = orig;
  }
}

function importLogicUndo() {
  const ta = document.getElementById('import-series-script');
  if (!ta || !ta.dataset.preFixSnapshot) { showToast('Нет снапшота для отката', 3000); return; }
  ta.value = ta.dataset.preFixSnapshot;
  delete ta.dataset.preFixSnapshot;
  const stats = document.getElementById('import-series-script-stats');
  if (stats) stats.textContent = ta.value.length.toLocaleString('ru') + ' символов';
  showToast('↶ Откачено к версии до правок', 3000);
  const out = document.getElementById('import-series-logic');
  if (out) out.innerHTML = '';
}

