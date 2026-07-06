// ── Shared adapt-to-standard rendering ────────────────────────────────────────
// Suggestion store: avoids encoding issues with onclick + JSON.stringify in HTML attrs
const _adaptStore = {};   // key → { ta, original, replacement }

// Normalize a dialogue line for fuzzy matching: lowercase, drop every non
// letter/digit. Mirrors the backend's _modkey so quote/spacing/punctuation
// differences (the script uses `Name: text`, the backend reconstructs
// `Name: "text"`) don't break the match.
function _normMod(s) {
  return (s || '').toLowerCase().replace(/[^a-z0-9а-яё]+/g, '');
}

// Split "SPEAKER: spoken" (tolerating a (parenthetical) and surrounding
// quotes on the spoken part) into {speaker, spoken}.
function _splitSpeakerLine(s) {
  const m = (s || '').match(/^([^:]{1,40}):([\s\S]*)$/);
  if (!m) return { speaker: '', spoken: (s || '').trim() };
  const spoken = m[2].trim().replace(/^["“«»]+|["”«»]+$/g, '').trim();
  return { speaker: m[1].trim(), spoken };
}

function _adaptApplyKey(key) {
  const d = _adaptStore[key];
  if (!d) return;
  const btn = document.querySelector(`[data-adapt-key="${key}"]`);
  const before = d.ta.value;

  const finish = (newVal) => {
    d.ta.value = newVal;
    d.ta.dispatchEvent(new Event('input'));
    if (btn) { btn.disabled = true; btn.style.opacity = '0.4'; btn.textContent = '✓ Применено'; }

    // Episode editor: the textarea alone is not the source of truth. The visible
    // script is the scene view, and nothing is persisted until "Применить
    // изменения" is clicked — so without this the replacement looked like it did
    // nothing and reverted on refresh. Re-render the scene view (if open) and
    // auto-save to the backend immediately.
    if (d.ta.id === 'ep-script') {
      try {
        const view = document.getElementById('ep-script-scenes');
        if (view && !view.classList.contains('hidden') && typeof _renderSceneViewBody === 'function') {
          _renderSceneViewBody();
        }
      } catch {}
      if (typeof saveEpisodeSilent === 'function' && S.seriesId && S.episodeNum) {
        showToast('✓ Реплика заменена, сохраняю…', 2000);
        saveEpisodeSilent()
          .then(() => {
            const applyBtn = document.getElementById('ep-script-apply-btn');
            if (applyBtn) { applyBtn.style.display = 'none'; applyBtn.dataset.dirty = ''; }
            showToast('✓ Сохранено', 1800);
          })
          .catch(e => showToast('⚠ Замена применена, но не сохранена: ' + (e?.message || e), 5000));
        return;
      }
    }
    showToast('✓ Реплика заменена', 2500);
  };

  // 1) Fast path — exact substring (works only if backend `original` happens
  //    to byte-match the script).
  if (before.includes(d.original)) { finish(before.replace(d.original, d.replacement)); return; }

  // 2) The backend reconstructs `original` as SPEAKER: "spoken" with forced
  //    double-quotes, but scripts store dialogue as `Name: text` (no quotes).
  //    Match line-wise on normalized text and rewrite that single line,
  //    preserving the script's own indentation + quote style.
  const lines = before.split('\n');
  const want = _normMod(d.original);
  let idx = lines.findIndex(l => _normMod(l) === want);

  // 3) Tolerate parentheticals / stage directions: match on speaker + spoken.
  if (idx === -1) {
    const o = _splitSpeakerLine(d.original);
    const wantSpk = _normMod(o.speaker), wantSpoken = _normMod(o.spoken);
    if (wantSpoken) {
      idx = lines.findIndex(l => {
        const p = _splitSpeakerLine(l);
        return _normMod(p.speaker) === wantSpk && _normMod(p.spoken).includes(wantSpoken);
      });
    }
  }

  // 4) Screenplay layout: the speaker name sits ALONE on its own line
  //    (optionally with a `(parenthetical)` or `(CONT'D)`), and the spoken text
  //    follows on the next line(s) until a blank line / scene header / cue.
  //    The backend always emits `original` as `SPEAKER: "spoken"`, which never
  //    byte-matches this layout, so none of paths 1-3 can find it. Match the
  //    cue line + the dialogue body separately, then rewrite the body in place.
  if (idx === -1) {
    const o = _splitSpeakerLine(d.original);
    const wantSpk = _normMod(o.speaker), wantSpoken = _normMod(o.spoken);
    // A line that is JUST an uppercase speaker cue (name + optional parenthetical).
    const cueRe = /^(\s*)([A-ZА-ЯЁ][A-ZА-ЯЁ0-9 .'\-]{0,30})\s*(\([^)]*\))?\s*$/;
    if (wantSpk && wantSpoken) {
      for (let i = 0; i < lines.length; i++) {
        const cue = lines[i].match(cueRe);
        if (!cue || _normMod(cue[2]) !== wantSpk) continue;
        // Gather the dialogue body: following non-blank lines, stopping at a
        // blank line, scene header (INT./EXT.) or a [BLOCKING]-style tag.
        const body = [];
        let j = i + 1;
        while (j < lines.length && lines[j].trim() !== '' &&
               !/^(INT\.|EXT\.|\[)/.test(lines[j].trim()) && !cueRe.test(lines[j])) {
          body.push(j); j++;
        }
        if (!body.length) continue;
        const bodyNorm = _normMod(body.map(k => lines[k]).join(' '));
        if (!(bodyNorm.includes(wantSpoken) || wantSpoken.includes(bodyNorm))) continue;
        // Preserve the first body line's indentation + any leading inline
        // parenthetical (e.g. "(whispers)"), then swap the spoken text.
        const first = lines[body[0]];
        const lead = (first.match(/^\s*(?:\([^)]*\)\s*)?/) || [''])[0];
        const repl = _splitSpeakerLine(d.replacement);
        const newSpoken = (repl.spoken || d.replacement).trim();
        lines.splice(body[0], body.length, lead + newSpoken);
        finish(lines.join('\n'));
        return;
      }
    }
    // 5) Last-resort fallback: match on the spoken text alone (speaker may have
    //    been paraphrased by the advisor). Only for sufficiently distinctive
    //    text to avoid false positives, and only a single best line.
    if (wantSpoken && wantSpoken.length >= 12) {
      const k = lines.findIndex(l => _normMod(l).includes(wantSpoken));
      if (k !== -1) {
        const repl = _splitSpeakerLine(d.replacement);
        const newSpoken = (repl.spoken || d.replacement).trim();
        const lead = (lines[k].match(/^\s*(?:\([^)]*\)\s*)?/) || [''])[0];
        lines[k] = lead + newSpoken;
        finish(lines.join('\n'));
        return;
      }
    }
  }

  if (idx === -1) {
    showToast('Строка не найдена в сценарии — формат отличается или уже заменена', 3500);
    if (btn) { btn.disabled = true; btn.style.opacity = '0.4'; }
    return;
  }

  // Keep the original line's "SPEAKER: " prefix (incl. any parenthetical) and
  // swap only the spoken text, mirroring whether the script quotes dialogue.
  const origLine = lines[idx];
  const colon = origLine.indexOf(':');
  const repl = _splitSpeakerLine(d.replacement);
  if (colon !== -1 && repl.spoken) {
    const prefix = origLine.slice(0, colon + 1);
    const tail = origLine.slice(colon + 1);
    const leadWs = (tail.match(/^\s*/) || [''])[0];
    const hadQuote = /^["“«]/.test(tail.trim());
    lines[idx] = prefix + leadWs + (hadQuote ? `"${repl.spoken}"` : repl.spoken);
  } else {
    const indent = (origLine.match(/^\s*/) || [''])[0];
    lines[idx] = indent + d.replacement.trim();
  }
  finish(lines.join('\n'));
}

// Renders the result box (changes list + moderation warnings) into `out` element.
// undoFn: string name of undo function. ta: the textarea that was adapted.
function _renderAdaptResult(out, r, undoFn, ta) {
  if (!out) return;
  const changes = r.changes || [];
  const warnings = r.moderation_warnings || [];

  // Register suggestions in store and build HTML
  const ts = Date.now();
  // Only show warnings that have actual suggestions
  const actionableWarnings = warnings.filter(w => (w.suggestions || []).length > 0);
  const warnHtml = actionableWarnings.length ? `
    <div style="margin-top:10px;padding:10px 12px;background:rgba(245,158,11,0.10);border:1px solid rgba(245,158,11,0.40);border-radius:6px;font-size:0.82rem">
      <div style="color:#f59e0b;font-weight:700;margin-bottom:8px">⚠️ Возможные проблемы с модерацией Seedance (${actionableWarnings.length}):</div>
      ${actionableWarnings.map((w, wi) => {
        const suggBtns = (w.suggestions || []).map((s, si) => {
          const key = `as_${ts}_${wi}_${si}`;
          _adaptStore[key] = { ta, original: w.original, replacement: s };
          return `<button class="btn-ghost btn-sm" data-adapt-key="${key}"
            style="display:block;width:100%;text-align:left;margin-bottom:3px;font-size:0.79rem;padding:4px 8px"
            onclick="_adaptApplyKey('${key}')">↩ ${esc(s)}</button>`;
        }).join('');
        return `
        <div style="margin-bottom:10px;padding:8px;background:rgba(0,0,0,0.15);border-radius:5px">
          <div style="color:var(--muted);margin-bottom:4px;font-size:0.79rem">🚩 <em>${esc(w.reason)}</em></div>
          <div style="color:var(--text);margin-bottom:6px;word-break:break-word"><code style="font-size:0.8rem;background:rgba(255,255,255,0.06);padding:2px 5px;border-radius:3px">${esc(w.original)}</code></div>
          <div style="font-size:0.79rem;color:var(--muted);margin-bottom:4px">Варианты замены:</div>
          ${suggBtns}
        </div>`;
      }).join('')}
    </div>` : '';

  out.innerHTML = `
    <div style="padding:10px 12px;background:rgba(16,185,129,0.10);border:1px solid rgba(16,185,129,0.35);border-radius:6px;font-size:0.82rem">
      <div style="color:#10b981;font-weight:700;margin-bottom:6px">✓ Адаптировано. Изменения:</div>
      <ul style="margin:4px 0 4px 16px;color:var(--text)">
        ${changes.map(c => `<li>${esc(c)}</li>`).join('') || '<li>(нет деталей)</li>'}
      </ul>
      <button class="btn-ghost btn-sm" onclick="${undoFn}()" style="margin-top:6px">↶ Отменить</button>
    </div>
    ${warnHtml}`;
}

// ── Import modal adapt ─────────────────────────────────────────────────────────
async function importAdaptToStandard(btn) {
  const ta = document.getElementById('import-series-script');
  const script = (ta?.value || '').trim();
  const out = document.getElementById('import-adapt-result');
  if (!script) { if (out) out.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Адаптирую…'; }
  if (out) out.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Claude добавляет позиции, адаптирует диалоги и сканирует модерацию… ~1-4 мин (большой сценарий — дольше)</div>';
  try {
    const r = await api.post('/api/adapt-script-to-standard', { script }, { timeoutMs: 600000 });
    if (r.error) throw new Error(r.error);
    ta.dataset.preAdaptSnapshot = script;
    ta.value = r.script;
    const stats = document.getElementById('import-series-script-stats');
    if (stats) stats.textContent = r.script.length.toLocaleString('ru') + ' символов';
    _renderAdaptResult(out, r, 'importAdaptUndo', ta);
    showToast('🔧 Сценарий адаптирован под стандарт', 4000);
  } catch (e) {
    if (out) out.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🔧 Адаптировать под стандарт'; }
  }
}

function importAdaptUndo() {
  const ta = document.getElementById('import-series-script');
  if (!ta || !ta.dataset.preAdaptSnapshot) { showToast('Нет снапшота для отката', 3000); return; }
  ta.value = ta.dataset.preAdaptSnapshot;
  delete ta.dataset.preAdaptSnapshot;
  const stats = document.getElementById('import-series-script-stats');
  if (stats) stats.textContent = ta.value.length.toLocaleString('ru') + ' символов';
  showToast('↶ Откачено к оригиналу', 3000);
  const out = document.getElementById('import-adapt-result');
  if (out) out.innerHTML = '';
}

// ── Append modal adapt ─────────────────────────────────────────────────────────
async function appendAdaptToStandard(btn) {
  const ta = document.getElementById('append-script-text');
  const script = (ta?.value || '').trim();
  const out = document.getElementById('append-adapt-result');
  if (!script) { if (out) out.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Адаптирую…'; }
  if (out) out.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Claude добавляет позиции, адаптирует диалоги и сканирует модерацию… ~1-4 мин (большой сценарий — дольше)</div>';
  try {
    const r = await api.post('/api/adapt-script-to-standard', { script }, { timeoutMs: 600000 });
    if (r.error) throw new Error(r.error);
    ta.dataset.preAdaptSnapshot = script;
    ta.value = r.script;
    appendUpdateStats();
    _renderAdaptResult(out, r, 'appendAdaptUndo', ta);
    showToast('🔧 Сценарий адаптирован под стандарт', 4000);
  } catch (e) {
    if (out) out.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🔧 Адаптировать под стандарт'; }
  }
}

function appendAdaptUndo() {
  const ta = document.getElementById('append-script-text');
  if (!ta || !ta.dataset.preAdaptSnapshot) { showToast('Нет снапшота для отката', 3000); return; }
  ta.value = ta.dataset.preAdaptSnapshot;
  delete ta.dataset.preAdaptSnapshot;
  appendUpdateStats();
  showToast('↶ Откачено к оригиналу', 3000);
  const out = document.getElementById('append-adapt-result');
  if (out) out.innerHTML = '';
}

// ── Episode editor adapt ───────────────────────────────────────────────────────
async function epAdaptToStandard(btn) {
  const ta = document.getElementById('ep-script');
  const script = (ta?.value || '').trim();
  const out = document.getElementById('ep-adapt-result');
  if (!script) { if (out) out.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>…'; }
  if (out) out.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Адаптирую под стандарт и сканирую модерацию… ~1-4 мин (большой сценарий — дольше)</div>';
  try {
    const r = await api.post('/api/adapt-script-to-standard', { script }, { timeoutMs: 600000 });
    if (r.error) throw new Error(r.error);
    ta.dataset.preAdaptSnapshot = script;
    ta.value = r.script;
    // trigger char count update
    ta.dispatchEvent(new Event('input'));
    // show save button
    const applyBtn = document.getElementById('ep-script-apply-btn');
    if (applyBtn) applyBtn.style.display = '';
    _renderAdaptResult(out, r, 'epAdaptUndo', ta);
    showToast('🔧 Сценарий адаптирован под стандарт', 4000);
  } catch (e) {
    if (out) out.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🔧 Под стандарт'; }
  }
}

function epAdaptUndo() {
  const ta = document.getElementById('ep-script');
  if (!ta || !ta.dataset.preAdaptSnapshot) { showToast('Нет снапшота для отката', 3000); return; }
  ta.value = ta.dataset.preAdaptSnapshot;
  delete ta.dataset.preAdaptSnapshot;
  ta.dispatchEvent(new Event('input'));
  showToast('↶ Откачено к оригиналу', 3000);
  const out = document.getElementById('ep-adapt-result');
  if (out) out.innerHTML = '';
}

// ── Episode phrase check (moderation scan only) ────────────────────────────────
// Renders moderation warnings into `outEl` using the shared _adaptStore system.
// `ta` is the textarea (ep-script). Returns true if any warnings found.
function _renderPhraseWarnings(outEl, warnings, ta) {
  if (!outEl) return false;
  if (!warnings || warnings.length === 0) {
    outEl.innerHTML = `
      <div style="padding:8px 12px;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.3);border-radius:6px;font-size:0.82rem;color:#10b981;margin-top:6px">
        ✓ Проблемных фраз не обнаружено
      </div>`;
    return false;
  }
  const ts = Date.now();
  // Filter out warnings with no suggestions (model flagged but couldn't suggest — skip)
  const actionable = warnings.filter(w => (w.suggestions || []).length > 0);
  if (actionable.length === 0) {
    outEl.innerHTML = `
      <div style="padding:8px 12px;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.3);border-radius:6px;font-size:0.82rem;color:#10b981;margin-top:6px">
        ✓ Проблемных фраз не обнаружено
      </div>`;
    return false;
  }
  const items = actionable.map((w, wi) => {
    const suggBtns = (w.suggestions || []).map((s, si) => {
      const key = `pc_${ts}_${wi}_${si}`;
      _adaptStore[key] = { ta, original: w.original, replacement: s };
      return `<button class="btn-ghost btn-sm" data-adapt-key="${key}"
        style="display:block;width:100%;text-align:left;margin-bottom:3px;font-size:0.79rem;padding:4px 8px"
        onclick="_adaptApplyKey('${key}')">↩ ${esc(s)}</button>`;
    }).join('');
    return `
      <div style="margin-bottom:10px;padding:8px;background:rgba(0,0,0,0.15);border-radius:5px">
        <div style="color:var(--muted);margin-bottom:4px;font-size:0.79rem">🚩 <em>${esc(w.reason)}</em></div>
        <div style="color:var(--text);margin-bottom:6px;word-break:break-word"><code style="font-size:0.8rem;background:rgba(255,255,255,0.06);padding:2px 5px;border-radius:3px">${esc(w.original)}</code></div>
        <div style="font-size:0.79rem;color:var(--muted);margin-bottom:4px">Варианты замены:</div>
        ${suggBtns}
      </div>`;
  }).join('');
  outEl.innerHTML = `
    <div style="margin-top:6px;padding:10px 12px;background:rgba(245,158,11,0.10);border:1px solid rgba(245,158,11,0.40);border-radius:6px;font-size:0.82rem">
      <div style="color:#f59e0b;font-weight:700;margin-bottom:8px">⚠️ Возможные проблемы с модерацией Seedance (${warnings.length}):</div>
      ${items}
    </div>`;
  return true;
}

// Manual phrase-check button handler
async function epCheckPhrases(btn) {
  const ta = document.getElementById('ep-script');
  const script = (ta?.value || '').trim();
  const out = document.getElementById('ep-postgen-checks');
  if (!script) { showToast('Сценарий пустой', 2000); return; }
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; }
  if (out) out.innerHTML = '<div style="font-size:0.82rem;color:var(--muted);margin-top:6px"><span class="spinner"></span> Сканирую диалоги на проблемные фразы…</div>';
  try {
    const r = await api.post('/api/check-moderation', { script }, { timeoutMs: 60000 });
    if (r.error) throw new Error(r.error);
    _renderPhraseWarnings(out, r.moderation_warnings || [], ta);
  } catch (e) {
    if (out) out.innerHTML = `<div style="color:var(--danger);font-size:0.82rem;margin-top:6px">Ошибка скана: ${esc(e?.message || e)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🚨 Фразы'; }
  }
}

// Auto-run after script generation: show audit violations + run phrase scan in bg
function _epPostGenChecks(script, auditReport) {
  const ta = document.getElementById('ep-script');
  const out = document.getElementById('ep-postgen-checks');
  if (!out) return;

  // 1. Surface logic audit violations (from backend, already available)
  let auditHtml = '';
  if (auditReport) {
    const violations = (auditReport.violations || []).filter(v => v.severity === 'critical' || v.severity === 'warning');
    if (violations.length > 0) {
      const rows = violations.map(v => `
        <div style="margin-bottom:6px;padding:6px 8px;background:rgba(0,0,0,0.15);border-radius:4px">
          <span style="color:${v.severity === 'critical' ? '#ef4444' : '#f59e0b'};font-weight:600;font-size:0.79rem">${v.severity === 'critical' ? '🔴' : '🟡'} ${esc(v.type || v.severity)}</span>
          <div style="font-size:0.79rem;color:var(--text);margin-top:2px">${esc(v.description || v.message || '')}</div>
        </div>`).join('');
      auditHtml = `
        <div style="margin-top:6px;padding:10px 12px;background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.30);border-radius:6px;font-size:0.82rem">
          <div style="color:#ef4444;font-weight:700;margin-bottom:6px">🧠 Логика — найдены проблемы (${violations.length}):</div>
          ${rows}
        </div>`;
    } else if (auditReport.passes !== false) {
      auditHtml = `<div style="margin-top:6px;padding:6px 12px;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.3);border-radius:6px;font-size:0.82rem;color:#10b981">✓ Логика — нарушений не найдено</div>`;
    }
  }

  // 2. Show audit result + phrase scan spinner
  out.innerHTML = auditHtml + `<div id="ep-phrase-scan-inline" style="font-size:0.82rem;color:var(--muted);margin-top:6px"><span class="spinner"></span> Сканирую фразы на модерацию Seedance…</div>`;

  // 3. Run phrase scan asynchronously (don't block the UI)
  api.post('/api/check-moderation', { script }, { timeoutMs: 60000 })
    .then(r => {
      const inlineEl = document.getElementById('ep-phrase-scan-inline');
      if (!inlineEl) return;
      const warnDiv = document.createElement('div');
      inlineEl.replaceWith(warnDiv);
      _renderPhraseWarnings(warnDiv, r.moderation_warnings || [], ta);
    })
    .catch(e => {
      const inlineEl = document.getElementById('ep-phrase-scan-inline');
      if (inlineEl) inlineEl.innerHTML = `<span style="color:var(--muted)">Скан фраз: ${esc(e?.message || e)}</span>`;
    });
}

// Show toast + postgen-checks banner when backend auto-created outfits from [BLOCKING].
// Then immediately refresh the in-memory series + character sidebar so the new
// outfits appear on the cards, and poll autogen status so when the avai i2i
// call completes the new outfit photo shows up without a manual reload.
function _notifyNewOutfits(newOutfits) {
  if (!newOutfits || !newOutfits.length) return;
  const names = newOutfits.map(o => `${esc(o.char_name)}: «${esc(o.outfit.label)}»`).join(', ');
  showToast(`👗 Новых образов: ${newOutfits.length} — ${names} · картинки генерируются в фоне`, 8000);
  const out = document.getElementById('ep-postgen-checks');
  if (out) {
    const banner = document.createElement('div');
    banner.innerHTML = `
      <div style="margin-top:6px;padding:8px 12px;background:rgba(132,94,247,0.10);border:1px solid rgba(132,94,247,0.35);border-radius:6px;font-size:0.82rem">
        <div style="color:var(--accent);font-weight:700;margin-bottom:4px">👗 Созданы новые образы из [BLOCKING] (${newOutfits.length}):</div>
        <ul style="margin:2px 0 2px 16px;color:var(--text)">
          ${newOutfits.map(o => `<li><strong>${esc(o.char_name)}</strong> — ${esc(o.outfit.label)}${o.outfit.description && o.outfit.description !== o.outfit.label ? `: <span style="color:var(--muted)">${esc(o.outfit.description)}</span>` : ''} <span style="color:var(--accent);font-size:0.75rem;margin-left:6px">⚙ генерируется...</span></li>`).join('')}
        </ul>
        <div style="font-size:0.78rem;color:var(--muted);margin-top:4px">Картинки появятся в карточках персонажей автоматически как только AVAI i2i завершится (обычно 20-60 сек).</div>
      </div>`;
    out.prepend(banner);
  }
  // Pull fresh series state so the sidebar character cards reflect the new
  // outfit objects right away (badge count, click-through). Without this the
  // user has to F5 to see the change.
  _refreshSeriesAfterOutfitSync();
}

// After backend reports `_new_outfits`, immediately reload the series so the
// sidebar shows the newly-created outfit chips, then arm the existing autogen
// poller (pollAutogenStatus) which already handles spinner overlays, live
// refresh on each item completion, and final image swap-in.
async function _refreshSeriesAfterOutfitSync() {
  try {
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh && !fresh.error) {
      S.series = fresh;
      if (typeof renderCharactersList === 'function') renderCharactersList();
    }
  } catch (e) { /* swallow — best-effort refresh */ }
  // Arm the canonical poller; it handles in_progress spinners + per-item refresh.
  try {
    if (typeof pollAutogenStatus === 'function') pollAutogenStatus();
  } catch (e) { /* swallow */ }
}

// Core commit: POST the textarea script as new episodes, close the modal and
// refresh the series view. Throws on error (caller handles UI). Extracted from
// appendScriptGo so the auto-pipeline can reuse it without the alert()/button
// plumbing.
async function _appendCommitScript() {
  const script = (document.getElementById('append-script-text')?.value || '').trim();
  const extract = !!document.getElementById('append-extract-entities')?.checked;
  if (!script) throw new Error('Сценарий пустой — вставь текст или подгрузи файл');
  const langHint = _LANG_WARN_KEPT['append-script-text'] || '';
  const body = { script, extract_entities: extract };
  if (langHint) body.dialogue_language_hint = langHint;
  const r = await api.post(`/api/series/${S.seriesId}/append-from-script`, body);
  if (r.error) throw new Error(r.error);
  closeModal('modal-append-script');
  _appendDraftClear();
  const range = (r.first_episode === r.last_episode)
    ? `№${r.first_episode}`
    : `№${r.first_episode}–${r.last_episode}`;
  showToast(`✓ Добавлено ${r.episodes_appended} серий (${range})${extract ? ' · извлечение запущено в фоне' : ''}`, 6000);
  // Refresh series view so episodes show up in the list.
  try {
    S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
    S.series = await api.get(`/api/series/${S.seriesId}`);
    renderEpisodesList();
  } catch {}
  if (extract) setTimeout(() => pollImportStatus(S.seriesId), 600);
  return r;
}

async function appendScriptGo() {
  const script = (document.getElementById('append-script-text')?.value || '').trim();
  if (!script) { alert('Сценарий пустой — вставь текст или подгрузи файл'); return; }
  const btn = document.getElementById('append-script-go-btn');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Добавляю...';
  try {
    await _appendCommitScript();
  } catch (e) {
    alert('Ошибка: ' + (e?.message || e));
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Polls /import-status while extraction is running. Renders a banner on the
// series page (#import-progress-banner) with X/Y counter + progress bar +
// current episode label.
let _importStatusTimer = null;
// Pipeline progress banner. Three-stage flow:
//   1. ANALYZING — _import_worker extracts chars/locs/items per episode
//   2. GENERATING — autogen sweep creates portraits/outfits/locations/items
//   3. READY     — all done, episode list rebuilds with «▶ Готова» badges
// Each stage owns its own progress bar; banner transitions automatically.
async function pollImportStatus(sid) {
  if (_importStatusTimer) { clearInterval(_importStatusTimer); _importStatusTimer = null; }
  // Stage tracking — captured here so _renderImportBanner can drive proper
  // state transitions across many polls.
  const PIPE = {
    sid,
    stage: 'analyzing',         // 'analyzing' | 'generating' | 'ready'
    importDoneCount: 0,
    importTotal: 0,
    importErrors: 0,
    autogenDone: 0,
    autogenTotal: 0,
    autogenErrors: 0,
    autogenSeen: false,         // becomes true on first running=true tick
  };
  const tick = async () => {
    try {
      // Phase 1 / 2 query depending on current stage
      if (PIPE.stage === 'analyzing') {
        const st = await fetch(`/api/series/${sid}/import-status`).then(r => r.json());
        PIPE.importDoneCount = st.done || 0;
        PIPE.importTotal = st.total || 0;
        PIPE.importErrors = (st.errors || []).length;
        PIPE.importCurrent = st.current || '';
        PIPE.canonFacts = (st.canon && st.canon.facts) || 0;
        PIPE.canonThreads = (st.canon && st.canon.threads) || 0;
        PIPE.canonErrors = (st.canon && st.canon.errors) || 0;
        if (!st.running && st.done > 0) {
          // Phase 1 done. Refresh series state so chars/locs/items show in
          // the sidebar, then transition to phase 2.
          try {
            const fresh = await api.get(`/api/series/${sid}`);
            if (fresh && S.seriesId === sid) {
              S.series = fresh;
              renderCharactersList && renderCharactersList();
              renderLocationsList && renderLocationsList();
              renderItemsList && renderItemsList();
            }
          } catch {}
          PIPE.stage = 'generating';
        }
        _renderPipelineBanner(PIPE);
      }
      if (PIPE.stage === 'generating') {
        let agst = null;
        try {
          agst = await fetch(`/api/series/${sid}/auto-generate/status`).then(r => r.json());
        } catch {}
        if (agst) {
          PIPE.autogenDone = agst.done || 0;
          PIPE.autogenTotal = agst.queue || 0;
          PIPE.autogenErrors = (agst.errors || []).length;
          if (agst.running) PIPE.autogenSeen = true;
          // Stage 2 → stage 3:
          //   • we saw autogen running at some point AND it has now stopped,
          //   • OR we never saw it running but >5s passed since stage entry
          //     (no autogen at all — e.g. all assets were pre-generated, or
          //     auto_generate_assets is off). Don't hang in stage 2 forever.
          PIPE.generatingEnteredAt = PIPE.generatingEnteredAt || Date.now();
          const enoughGrace = Date.now() - PIPE.generatingEnteredAt > 6000;
          if ((PIPE.autogenSeen && !agst.running) || (!PIPE.autogenSeen && enoughGrace)) {
            PIPE.stage = 'ready';
          }
        }
        _renderPipelineBanner(PIPE);
        if (PIPE.stage === 'ready') {
          // Final refresh so «▶ Готова» badges + chars-with-photos render.
          try {
            const fresh = await api.get(`/api/series/${sid}`);
            if (fresh && S.seriesId === sid) {
              S.series = fresh;
              renderCharactersList && renderCharactersList();
              renderLocationsList && renderLocationsList();
              renderItemsList && renderItemsList();
            }
            if (S.seriesId === sid) {
              S.episodes = await api.get(`/api/series/${sid}/episodes`);
              if (typeof renderEpisodesList === 'function') renderEpisodesList();
            }
          } catch {}
        }
      }
      if (PIPE.stage === 'ready') {
        // Auto-hide after 12s (the banner already shows ✅ summary).
        if (_importStatusTimer) { clearInterval(_importStatusTimer); _importStatusTimer = null; }
      }
    } catch {}
  };
  await tick();
  _importStatusTimer = setInterval(tick, 3000);
}

// Auto-hide timer (single instance — multiple ticks would otherwise queue
// multiple removals).
let _importBannerHideTimer = null;
function _renderPipelineBanner(P) {
  let el = document.getElementById('import-progress-banner');
  if (!el) {
    el = document.createElement('div');
    el.id = 'import-progress-banner';
    el.className = 'import-progress-banner';
    el.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);max-width:760px;width:92vw;z-index:1500;background:var(--surface,#1a1a1f);border:1px solid var(--border,#333);border-radius:12px;padding:14px 18px;box-shadow:0 12px 36px rgba(0,0,0,0.5)';
    document.body.appendChild(el);
  }
  // Step descriptors — color & icon per stage.
  const step1Done = P.stage !== 'analyzing';
  const step2Done = P.stage === 'ready';
  const step3Done = P.stage === 'ready';

  const importPct = P.importTotal ? Math.round(100 * P.importDoneCount / P.importTotal) : (step1Done ? 100 : 0);
  const autogenPct = P.autogenTotal ? Math.round(100 * P.autogenDone / P.autogenTotal) : (step2Done ? 100 : 0);

  const importBar = P.stage === 'analyzing'
    ? `<div class="ipb-bar"><div class="ipb-bar-fill" style="width:${importPct}%"></div></div>`
    : '';
  const autogenBar = P.stage === 'generating'
    ? `<div class="ipb-bar"><div class="ipb-bar-fill" style="width:${autogenPct}%"></div></div>`
    : '';

  const importErrsBit = P.importErrors ? ` · <span style="color:var(--warning,#fbbf24)">ошибок ${P.importErrors}</span>` : '';
  const autogenErrsBit = P.autogenErrors ? ` · <span style="color:var(--warning,#fbbf24)">ошибок ${P.autogenErrors}</span>` : '';

  const icon = (active, done) => active ? '<span class="ipb-spinner"></span>'
                                        : (done ? '<span style="color:#10b981;font-weight:700">✓</span>'
                                                : '<span style="color:var(--muted,#888)">·</span>');

  // Final-ready summary stats from S.series.
  let readyStats = '';
  if (P.stage === 'ready' && S.series && S.seriesId === P.sid) {
    const totalEps = (S.episodes || []).length;
    const newEps = P.importTotal;
    const chars = (S.series.characters || []).filter(c => (c.ref_images || []).length).length;
    const locs  = (S.series.locations  || []).filter(l => (l.ref_images || []).length).length;
    const items = (S.series.items      || []).filter(i => (i.ref_images || []).length).length;
    const totalChars = (S.series.characters || []).length;
    const totalLocs  = (S.series.locations  || []).length;
    const totalItems = (S.series.items      || []).length;
    readyStats = `
      <div style="margin-top:10px;padding:10px 12px;background:rgba(16,185,129,0.10);border:1px solid rgba(16,185,129,0.35);border-radius:8px;font-size:0.85rem">
        <div style="color:#10b981;font-weight:700;margin-bottom:4px">✅ Серии готовы к видео-генерации</div>
        <div style="color:var(--muted)">
          Добавлено: <strong>${newEps}</strong> сер · в сериале сейчас: <strong>${totalEps}</strong> сер<br>
          Ассетов сгенерировано: 👤 ${chars}/${totalChars} персонажей · 🏛 ${locs}/${totalLocs} локаций · 📦 ${items}/${totalItems} предметов
        </div>
      </div>`;
  }

  el.innerHTML = `
    <div style="font-size:0.92rem;font-weight:700;margin-bottom:10px">🎬 Подготовка серий</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;align-items:center;gap:10px;font-size:0.85rem">
        <span style="width:18px;display:inline-flex;justify-content:center">${icon(P.stage === 'analyzing', step1Done)}</span>
        <div style="flex:1;min-width:0">
          <div><strong>1.</strong> 🧠 Анализ сценариев — персонажи, локации, предметы + канон сериала
            ${P.stage === 'analyzing' ? `<span style="color:var(--muted)"> · ${P.importDoneCount}/${P.importTotal}${importErrsBit}</span>` : ''}
            ${step1Done && P.stage !== 'analyzing' ? `<span style="color:var(--muted)"> · обработано ${P.importDoneCount} сер${P.importErrors ? ` (ошибок ${P.importErrors})` : ''}</span>` : ''}
          </div>
          ${P.stage === 'analyzing' && P.importCurrent ? `<div style="color:var(--muted);font-size:0.76rem;margin-top:2px">сейчас: ${esc(P.importCurrent)}</div>` : ''}
          ${(P.canonFacts || P.canonThreads) ? `<div style="color:var(--muted);font-size:0.76rem;margin-top:2px">📚 канон: <strong>${P.canonFacts}</strong> фактов · <strong>${P.canonThreads}</strong> сюжетных линий${P.canonErrors ? ` · <span style="color:var(--warning)">ошибок ${P.canonErrors}</span>` : ''}</div>` : ''}
          ${importBar}
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;font-size:0.85rem">
        <span style="width:18px;display:inline-flex;justify-content:center">${icon(P.stage === 'generating', step2Done)}</span>
        <div style="flex:1;min-width:0">
          <div><strong>2.</strong> 🎨 Генерация ассетов — портреты, костюмы, локации, предметы
            ${P.stage === 'generating'
              ? (P.autogenTotal
                  ? `<span style="color:var(--muted)"> · ${P.autogenDone}/${P.autogenTotal}${autogenErrsBit}</span>`
                  : `<span style="color:var(--muted)"> · ждём очередь…</span>`)
              : ''}
            ${step2Done && P.autogenTotal ? `<span style="color:var(--muted)"> · сгенерировано ${P.autogenDone} ассет${P.autogenDone === 1 ? '' : (P.autogenDone < 5 ? 'а' : 'ов')}${P.autogenErrors ? ` (ошибок ${P.autogenErrors})` : ''}</span>` : ''}
            ${step2Done && !P.autogenTotal ? `<span style="color:var(--muted)"> · ничего не понадобилось</span>` : ''}
          </div>
          ${autogenBar}
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;font-size:0.85rem">
        <span style="width:18px;display:inline-flex;justify-content:center">${icon(false, step3Done)}</span>
        <div style="flex:1;min-width:0">
          <div><strong>3.</strong> ${step3Done ? '✅ Готово — серии можно отправить в видео-генерацию' : 'Готовность серий'}</div>
        </div>
      </div>
    </div>
    ${readyStats}
  `;

  if (P.stage === 'ready') {
    if (_importBannerHideTimer) clearTimeout(_importBannerHideTimer);
    _importBannerHideTimer = setTimeout(() => {
      const cur = document.getElementById('import-progress-banner');
      if (cur) cur.remove();
      _importBannerHideTimer = null;
    }, 18000);
  } else if (_importBannerHideTimer) {
    clearTimeout(_importBannerHideTimer);
    _importBannerHideTimer = null;
  }
}

// Back-compat shim — anyone still calling the old name gets the new pipeline.
function _renderImportBanner(st) {
  _renderPipelineBanner({
    sid: S.seriesId,
    stage: st.running ? 'analyzing' : 'ready',
    importDoneCount: st.done || 0,
    importTotal: st.total || 0,
    importErrors: (st.errors || []).length,
    autogenDone: 0, autogenTotal: 0, autogenErrors: 0,
  });
}

// Writer-model dropdown helper. Two chip-pickers exist:
//   #writer-model-create   — in the «Create series» modal (drives ideas + initial fill)
//   #writer-model-ep       — in the episode view (drives per-episode synopsis/script)
// Returns 'claude-sonnet-4-5' or 'gpt-5.5'. Default = claude.
const WRITER_MODELS = [
  { id: 'claude-sonnet-4-5', label: 'Claude Sonnet 4.5', short: 'Claude' },
  { id: 'gpt-5.5',           label: 'GPT-5.5',           short: 'GPT-5.5' },
];
function _selectedWriterModel(elementId) {
  const el = document.getElementById(elementId);
  if (!el) return 'claude-sonnet-4-5';
  const v = (el.value || '').trim().toLowerCase();
  return WRITER_MODELS.some(m => m.id === v) ? v : 'claude-sonnet-4-5';
}
function _renderWriterModelChip(elementId, defaultModel) {
  const sel = document.getElementById(elementId);
  if (!sel) return;
  sel.innerHTML = WRITER_MODELS.map(m =>
    `<option value="${m.id}">${m.short}</option>`
  ).join('');
  sel.value = (defaultModel && WRITER_MODELS.some(m => m.id === defaultModel)) ? defaultModel : 'claude-sonnet-4-5';
}

async function generateFromIdea() {
  const idea = val('series-idea-input');
  if (!idea) return alert('Опиши идею для сериала');
  const btn = document.getElementById('btn-gen-from-idea');
  const status = document.getElementById('series-gen-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = '';
  try {
    const genres = getSelectedGenres();
    const model = _selectedWriterModel('writer-model-create');
    const data = await trackTask('Сериал по идее', {}, () =>
      api.post('/api/generate-series-from-idea', { idea, genres, model, beats: getSelectedBeats(), format_mode: getFormatMode(), ...getEraSetting() })
    );
    fillSeriesForm(data);
    status.textContent = '✓ Поля заполнены — проверь и отредактируй если нужно';
    status.style.color = 'var(--success)';
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '✨ Сгенерировать по идее';
  }
}

const GENRES = [
  { value: 'Romance',          label: 'Romance',          checked: false, desc: 'Любовная история с эмоциональным напряжением, притяжением и препятствиями между героями. Сердце — отношения.' },
  { value: 'Revenge Drama',    label: 'Revenge',          checked: false, desc: 'Главная героиня была унижена или предана — и теперь методично разрушает жизни обидчиков. Катарсис через справедливость.' },
  { value: 'Cinderella',       label: 'Cinderella',       checked: false, desc: 'Девушка из низов попадает в мир богатых и влиятельных. Классический подъём через любовь, случай или скрытый талант.' },
  { value: 'Enemies to Lovers',label: 'Enemies→Lovers',  checked: false, desc: 'Герои ненавидят друг друга с первой сцены — и именно это притяжение переходит в страсть. Медленное горение.' },
  { value: 'Thriller',         label: 'Thriller',         checked: false, desc: 'Постоянное напряжение, угроза жизни или тайна, которую надо раскрыть. Зритель всегда на краю.' },
  { value: 'Melodrama',        label: 'Melodrama',        checked: false, desc: 'Семейные тайны, рождения, смерти, измены. Высокие эмоции, слёзы, прощения. Акцент на чувствах, не экшене.' },
  { value: 'Dark Drama',       label: 'Dark Drama',       checked: false, desc: 'Мрачный реализм без хэппи-энда. Герои морально неоднозначны, мир жесток, выборы — без правильного ответа.' },
  { value: 'Mystery',          label: 'Mystery',          checked: false, desc: 'Загадка, которую герои (и зритель) распутывают по кусочкам. Каждый эпизод — новая деталь головоломки.' },
  { value: 'Supernatural',     label: 'Supernatural',     checked: false, desc: 'Магия, судьба, реинкарнация, духи или сверхъестественные силы вплетены в бытовой конфликт.' },
  { value: 'Comedy',           label: 'Comedy',           checked: false, desc: 'Лёгкий тон, ситуативный юмор, недопонимания и неловкие моменты. Конфликт смешной, а не травмирующий.' },
  { value: 'Power Struggle',   label: 'Power Struggle',   checked: false, desc: 'Война за власть — в корпорации, семье, или политике. Кто наверху, кто внизу — и как это меняется.' },
  { value: 'Forbidden Love',   label: 'Forbidden Love',   checked: false, desc: 'Отношения, которые запрещены — из-за семьи, класса, закона или обстоятельств. Страсть против правил.' },
  { value: 'Coming of Age',    label: 'Coming of Age',    checked: false, desc: 'Молодая героиня взрослеет через боль, ошибки и открытия. История становления характера.' },
  { value: 'Scandal',          label: 'Scandal',          checked: false, desc: 'Тайная жизнь богатых и влиятельных разрушается под давлением огласки. Ложь, измены, секреты.' },
  { value: 'Second Chance',    label: 'Second Chance',    checked: false, desc: 'Бывшие влюблённые или старые враги встречаются снова. Старые раны открываются — но есть шанс исправить прошлое.' },
  { value: 'Obsession',        label: 'Obsession',        checked: false, desc: 'Один персонаж одержим другим — романтически или мстительно. Граница между страстью и опасностью размыта.' },
  { value: 'Rags to Riches',   label: 'Rags to Riches',  checked: false, desc: 'Героиня поднимается из бедности к власти и богатству своими силами. Триумф через лишения.' },
  { value: 'Hidden Identity',  label: 'Hidden Identity',  checked: false, desc: 'Герой скрывает кто он на самом деле. Когда правда выйдет — всё изменится.' },
  { value: 'Betrayal',         label: 'Betrayal',         checked: false, desc: 'Предательство близкого человека — главный двигатель сюжета. Кому верить, когда все лгут?' },
  { value: 'Family Secrets',   label: 'Family Secrets',   checked: false, desc: 'Семья хранит тёмные тайны. Когда они всплывают — рушится всё, что герои считали правдой.' },
  { value: 'Pregnancy Drama',  label: '🤰 Pregnancy',     checked: false, desc: 'Беременность как двигатель сюжета: скрытый ребёнок, отцовство под вопросом, шантаж, воссоединение или финальный реванш через наследника.' },
  { value: 'Cinderella Revenge', label: '👑 Cinderella Revenge', checked: false, desc: 'Унижают официантку — а она уже владелец компании. Скрытый статус + мгновенная карма: чем больше унижение, тем сокрушительнее реванш. Взлёт → падение → взлёт с союзником.' },
  { value: 'Mafia Romance',    label: '🔫 Mafia',          checked: false, desc: 'Лидер мафии или криминального мира как любовный интерес или союзник. Опасность и защита в одном человеке. Власть через страх.' },
  { value: 'CEO Drama',        label: '💼 CEO Drama',      checked: false, desc: 'Корпоративная власть, враждебные поглощения, наследники и самозванцы. Офис как поле боя, деловые переговоры как война.' },
];

function buildGenreFilters() {
  const block = document.getElementById('genre-filter-block');
  if (!block) return;
  const label = block.querySelector('.genre-filter-label');
  block.innerHTML = '';
  block.appendChild(label);
  GENRES.forEach(g => {
    const lbl = document.createElement('label');
    lbl.className = 'genre-chip';
    if (g.checked) lbl.classList.add('genre-chip-checked');
    lbl.innerHTML = `<input type="checkbox" value="${g.value}" ${g.checked ? 'checked' : ''}> ${g.label}`;
    lbl.addEventListener('mouseenter', (e) => showGenreTooltip(g.desc, e));
    lbl.addEventListener('mouseleave', hideGenreTooltip);
    lbl.querySelector('input').addEventListener('change', (e) => {
      lbl.classList.toggle('genre-chip-checked', e.target.checked);
    });
    block.appendChild(lbl);
  });
}

function showGenreTooltip(desc, e) {
  const tip = document.getElementById('genre-tooltip');
  if (!tip) return;
  tip.textContent = desc;
  tip.classList.remove('hidden');
  const rect = e.target.closest('.genre-chip').getBoundingClientRect();
  const blockRect = document.getElementById('genre-filter-block').getBoundingClientRect();
  tip.style.top = (rect.bottom - blockRect.top + 6) + 'px';
  tip.style.left = Math.max(0, rect.left - blockRect.left) + 'px';
}

function hideGenreTooltip() {
  document.getElementById('genre-tooltip')?.classList.add('hidden');
}

function getSelectedGenres() {
  return [...document.querySelectorAll('.genre-chip input:checked')].map(cb => cb.value);
}

