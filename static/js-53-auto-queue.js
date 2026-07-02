// ── Range-generation queue ─────────────────────────────────────────────────
//
// Picks episodes [from..to] in series-view, runs each through Auto-mode
// sequentially (one finishes → next starts), optionally auto-assembles
// completed chunks into final mp4 in OUT/ when each episode wraps.
//
// Lifecycle:
//   user fills inputs → startRangeGen() validates + builds queue
//   → for each ep: navigate('episode'), wait for load, suppress confirms,
//     await startAutoMode → poll AUTO.active = false → optional auto-assemble
//   → next; finishes when queue empty or stopRangeGen() pressed.
const RANGE = {
  active: false,
  queue: [],            // array of episode numbers, in order
  curIdx: -1,
  cancelRequested: false,
  autoAssemble: true,
  seriesId: null,
};

function _rangeGenStatusEl() { return document.getElementById('range-gen-status'); }
function _rangeGenStartBtn() { return document.getElementById('range-gen-start-btn'); }
function _rangeGenStopBtn()  { return document.getElementById('range-gen-stop-btn'); }
function _rangeGenSetStatus(html) {
  const el = _rangeGenStatusEl();
  if (el) el.innerHTML = html;
}
function _rangeGenUI() {
  const sb = _rangeGenStartBtn(), tb = _rangeGenStopBtn();
  if (sb) sb.style.display = RANGE.active ? 'none' : '';
  if (tb) tb.style.display = RANGE.active ? '' : 'none';
}
function stopRangeGen() {
  if (!RANGE.active) return;
  RANGE.cancelRequested = true;
  _rangeGenSetStatus('⏸ Останавливаю после текущей серии…');
  // Also cascade-stop the in-flight Auto-mode so we don't keep pumping segments.
  try { if (typeof stopAutoMode === 'function' && AUTO?.active) stopAutoMode(); } catch {}
}

async function startRangeGen() {
  if (RANGE.active) { showToast('Очередь уже идёт'); return; }
  if (!S.series) { showToast('Открой сериал'); return; }
  const aaEl = document.getElementById('range-gen-auto-assemble');
  const concEl = document.getElementById('range-gen-concurrency');
  const concurrency = Math.max(1, Math.min(3, parseInt(concEl?.value, 10) || 1));
  const eps = (S.episodes || S.series.episodes || []).slice().sort((a,b) => (a.number||0) - (b.number||0));
  if (!eps.length) { showToast('Нет эпизодов'); return; }
  if (!S._genSelected) _restoreGenSelection();
  // Build the queue from the user's checkbox selection. Filter to only those
  // that are actually ready (in case selection got stale — e.g. assets were
  // deleted after the user ticked the checkbox) and aren't already done /
  // generating right now.
  let queue = [];
  for (const num of [...S._genSelected].sort((a, b) => a - b)) {
    const ep = eps.find(e => e.number === num);
    if (!ep) continue;
    const info = _episodeReadyInfo(ep);
    const gs = ep.gen_status || '';
    if (info.ready && gs !== 'done' && gs !== 'generating') {
      queue.push(num);
    }
  }
  if (!queue.length) {
    showToast('⚠ Не выделено ни одной серии готовой к генерации', 6000);
    return;
  }
  const concWord = concurrency > 1 ? `параллельно по ${concurrency}` : 'последовательно';
  if (!await appConfirm({
    title: '▶ Пакетная генерация',
    message:
      `Серий в очереди: ${queue.length} (${queue.join(', ')})\n` +
      `Режим: ${concWord}\n` +
      `Авто-сборка финала: ${aaEl?.checked ? 'да' : 'нет'}\n\n` +
      (concurrency > 1
        ? `Серии будут стартовать одновременно (до ${concurrency}). Внутри каждой ` +
          `серии сцены тоже параллельные. Видеть прогресс — в виджете в правом нижнем углу.`
        : `Каждая серия по очереди прогонится через Auto-mode (Sequential).`) +
      `\nОстановить — кнопка «Остановить»: текущие серии добегут, дальше очередь встанет.`,
    okText: '▶ Запустить очередь',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;

  RANGE.active = true;
  RANGE.queue = queue;
  RANGE.curIdx = -1;
  RANGE.cancelRequested = false;
  RANGE.autoAssemble = !!aaEl?.checked;
  RANGE.seriesId = S.seriesId;
  RANGE.concurrency = concurrency;
  _rangeGenUI();
  _rangeGenSetStatus(`▶ В очереди: ${queue.length}${concurrency > 1 ? ` · параллельно ${concurrency}` : ''}`);
  _autoUpdateFloatingWidget();

  // Suppress modal confirms / missing-asset warnings inside the loop. We restore
  // the original `confirm` after finishing or on error.
  const origConfirm = window.confirm;
  const origAppConfirm = window.appConfirm;
  window.confirm = () => true;
  // Also short-circuit appConfirm() so the in-app modals from inner Auto-mode
  // calls don't block the queue. Only the outer queue-start confirm above
  // actually shows a dialog.
  window.appConfirm = () => Promise.resolve(true);

  // Track how many episodes have been claimed from the queue (next-up index).
  // Cursor is shared across workers; each worker grabs the next number atomic.
  let claimed = 0;
  let finished = 0;

  // Per-episode runner. Either uses the navigation+startAutoMode path
  // (concurrency=1, preserves visible episode-page UI) or the headless
  // standalone runner (concurrency>1, runs without changing S.episode).
  async function _rangeRunEpisode(epNum, idxInQueue) {
    _rangeGenSetStatus(`▶ ${idxInQueue + 1}/${queue.length} · серия ${epNum} · старт…`);

    // Mark as generating in series state so badges update.
    try {
      await api.put(`/api/series/${RANGE.seriesId}/episodes/${epNum}`, { gen_status: 'generating' });
      const ep = (S.series.episodes || []).find(e => e.number === epNum);
      if (ep) ep.gen_status = 'generating';
      if (S.episode?.number === epNum) S.episode.gen_status = 'generating';
      if (typeof renderEpisodesList === 'function') renderEpisodesList();
    } catch {}

    // ── INSTANT MUSIC KICK-OFF ───────────────────────────────────────────────
    // Fire music generation RIGHT NOW — before navigate, before batch-compose,
    // before a single video chunk is submitted. We parse the episode script
    // client-side (same logic as _runEpisodeAutoStandalone) to get scene groups
    // + duration estimates, then POST music/generate with scenes_plan.
    // The backend generates music in parallel with the entire video pipeline.
    const _musicEnabled = (S.series?.id === RANGE.seriesId)
      ? (S.series?.settings?.enable_music !== false)
      : true;
    if (_musicEnabled) {
      (async () => {
        try {
          // Fetch episode script (may already be in memory if same episode).
          let epScript = '';
          let epOverrides = [];   // MUST stay an array — _parseScriptScenes throws on {}
          if (S.episode?.number === epNum && S.seriesId === RANGE.seriesId) {
            epScript = document.getElementById('ep-script')?.value?.trim() || '';
            epOverrides = _segmentOverrides();
          }
          if (!epScript) {
            const epJson = await api.get(
              `/api/series/${RANGE.seriesId}/episodes/${epNum}`
            );
            epScript = (epJson?.script || '').trim();
            if (Array.isArray(epJson?.segment_overrides)) epOverrides = epJson.segment_overrides;
          }
          if (!epScript) return;

          // Parse script into scene groups (same helper as standalone runner).
          const scenes = _parseScriptScenes(epScript, epOverrides);
          const planMap = new Map();
          scenes.forEach((sc, sIdx) => {
            for (let g = 0; g < sc.segCount; g++) {
              const lines = sc.lines.filter(l => l.segIdx === g);
              if (!lines.length) continue;
              const segText = lines.map(l => l.text).join('\n');
              const dur = _estimateChunkDurationSec(segText);
              planMap.set(sIdx, (planMap.get(sIdx) || 0) + dur);
            }
          });
          if (!planMap.size) return;

          const scenesPlan = [...planMap.entries()].map(([sceneIdx, totalSec]) => ({
            sceneIdx,
            target_duration_ms: Math.round(totalSec * 0.9 * 1000),
          }));

          const r = await api.post(
            `/api/series/${RANGE.seriesId}/episodes/${epNum}/music/generate`,
            { scenes_plan: scenesPlan }
          );
          if (r?.ok && r.scenes?.length) {
            showToast(`🎵 Музыка запущена — эпизод ${epNum} · ${r.scenes.length} сцен`, 3000);
          }
        } catch (e) {
          // Non-fatal — video gen continues regardless.
          clog('WARN', 'range.music_kickoff_fail', {
            ep: epNum, err: (e?.message || String(e)).slice(0, 200),
          });
        }
      })();
    }
    // ────────────────────────────────────────────────────────────────────────

    let result = { ok: false, completed: 0, total: 0, errors: 0 };
    let assembleNote = '';
    let finalStatus = 'failed';   // default — if anything below throws, we still flip away from 'generating'

    try {
      if (concurrency === 1) {
        // Legacy path: navigate to the episode + use startAutoMode (so the user
        // sees the visible episode page with chunks ticking in).
        try {
          navigate('episode', { seriesId: RANGE.seriesId, episodeNum: epNum });
        } catch (e) { console.warn('[range-gen] navigate failed', e); }
        const navStart = Date.now();
        while (Date.now() - navStart < 30000) {
          if (S.seriesId === RANGE.seriesId && S.episode?.number === epNum) break;
          await new Promise(r => setTimeout(r, 250));
        }
        if (S.episode?.number !== epNum) {
          _rangeGenSetStatus(`⚠ серия ${epNum} не открылась — пропуск`);
          clog('WARN', 'range.ep_nav_fail', { sid: RANGE.seriesId, ep: epNum });
          result = { ok: false, completed: 0, total: 0, errors: 1 };
        } else {
          try { await startAutoMode(); }
          catch (e) {
            clog('ERROR', 'range.ep_startAuto_throw', { sid: RANGE.seriesId, ep: epNum, msg: (e?.message || String(e)).slice(0, 300) });
          }
          while (AUTO?.active) {
            if (RANGE.cancelRequested) { try { stopAutoMode(); } catch {} }
            await new Promise(r => setTimeout(r, 2000));
          }
          result = {
            ok: !RANGE.cancelRequested && (AUTO?.completedCount || 0) >= (AUTO?.total || 0) && (AUTO?.total || 0) > 0,
            completed: AUTO?.completedCount || 0,
            total: AUTO?.total || 0,
            errors: 0,
          };
        }
      } else {
        // Parallel path: standalone runner, no navigation. Multiple of these
        // can run concurrently because each has its own Run state.
        result = await _runEpisodeAutoStandalone(RANGE.seriesId, epNum, {
          useLastframe: true, useCutframes: true,
          useStyle: false, styleVal: '',
          baseOnly: false, closeUpOnly: false,
          duration: 15, resolution: '480p',
          moderation_bypass: document.getElementById('sd-mod-bypass')?.value || 'off',
          model_tier: document.getElementById('sd-model-tier')?.value || 'reference-fast',
          errorMode: 'heal',
          maxParallelScenes: 2,
          // Pass enable_music so music fires immediately after script segmentation,
          // in parallel with video generation (not waiting for videos to finish).
          enableMusic: (S.series?.id === RANGE.seriesId)
            ? (S.series?.settings?.enable_music !== false)
            : true,
        });
      }

      // Log near-instant returns — they're almost always a real bug (segment
      // count zero, episode JSON fetch failure, etc.) and we want to see
      // which one it was without DevTools.
      if (result.total === 0 && result.completed === 0) {
        clog('WARN', 'range.ep_empty_result', {
          sid: RANGE.seriesId, ep: epNum,
          errors: result.errors || 0, ok: !!result.ok,
        });
      }

      const completedAll = result.ok;
      finalStatus = completedAll ? 'done' : (RANGE.cancelRequested ? 'queued' : 'failed');

      if (RANGE.autoAssemble && completedAll && !RANGE.cancelRequested) {
        try {
          const r = await api.post(
            `/api/series/${RANGE.seriesId}/episodes/${epNum}/auto-assemble`,
            { require_all: true, expected_segments: result.total },
          );
          if (r && r.ok) assembleNote = ` · 🎬 ${r.filename} (${r.size_mb}MB)`;
          else if (r?.error) assembleNote = ` · ⚠ авто-сборка: ${r.error}`;
        } catch (e) {
          assembleNote = ` · ⚠ авто-сборка упала: ${e.message || e}`;
        }
      }

      // Music generation is INDEPENDENT of auto-assemble: it can fire even
      // when the user turned auto-assemble off. Gate is purely the per-series
      // enable_music flag + a successful run.
      if (completedAll && !RANGE.cancelRequested) {
        const musicEnabled = (S.series?.id === RANGE.seriesId)
          ? (S.series?.settings?.enable_music !== false)
          : true;
        if (musicEnabled) {
          try {
            const r = await api.post(
              `/api/series/${RANGE.seriesId}/episodes/${epNum}/music/generate`,
              {}
            );
            if (r?.ok) assembleNote += ` · 🎵 музыка запущена (${r.scenes?.length || 0} сцен)`;
            else if (r?.error) assembleNote += ` · 🎵 ⚠ ${r.error}`;
          } catch (e) {
            assembleNote += ` · 🎵 ⚠ ${e.message || e}`;
          }
        }
      }
    } catch (e) {
      // Anything thrown mid-run: log it and fall through to the finally that
      // still flips gen_status away from 'generating'. Without this guard,
      // a thrown exception would skip the status-update PUT below and leave
      // the episode permanently stuck.
      clog('ERROR', 'range.ep_run_throw', {
        sid: RANGE.seriesId, ep: epNum,
        msg: (e?.message || String(e)).slice(0, 400),
      });
      console.error('[range-gen] _rangeRunEpisode body threw', epNum, e);
      finalStatus = 'failed';
    } finally {
      // ALWAYS flip away from 'generating'. Use a fire-and-forget retry on
      // failure so a transient network blip doesn't leave the episode stuck.
      const tryUpdate = async () => {
        try {
          await api.put(`/api/series/${RANGE.seriesId}/episodes/${epNum}`, { gen_status: finalStatus });
          if (S.episode?.number === epNum) S.episode.gen_status = finalStatus;
          const ep = (S.series.episodes || []).find(e => e.number === epNum);
          if (ep) ep.gen_status = finalStatus;
          return true;
        } catch (e) {
          return false;
        }
      };
      const ok1 = await tryUpdate();
      if (!ok1) {
        // One retry after 1.5s — covers a transient 5xx during heavy load.
        await new Promise(r => setTimeout(r, 1500));
        await tryUpdate();
      }
      _rangeGenSetStatus(`✓ ${idxInQueue + 1}/${queue.length} · серия ${epNum}${assembleNote || (finalStatus === 'failed' ? ' · ⚠ упало' : '')}`);
    }
    return result;
  }

  try {
    // Worker pool: N workers each pull the next un-claimed episode off the
    // queue, run it, then loop. Total throughput = min(N, queue.length).
    async function worker() {
      while (!RANGE.cancelRequested && claimed < queue.length) {
        const idx = claimed++;
        const epNum = queue[idx];
        RANGE.curIdx = Math.max(RANGE.curIdx, idx);
        try { await _rangeRunEpisode(epNum, idx); }
        catch (e) { console.warn('[range-gen] episode crashed', epNum, e); }
        finished++;
      }
    }
    // Stagger worker startup so 3 concurrent compose calls don't fire in
    // the same JS turn. Anthropic API throttles bursts pretty aggressively
    // even with backoff retry — staggering 2s apart smooths the load. With
    // concurrency=3 the third worker starts at +4s, by which time the
    // first one's compose is already mid-flight.
    const STAGGER_MS = 2000;
    const workers = [];
    const workerCount = Math.min(concurrency, queue.length);
    for (let i = 0; i < workerCount; i++) {
      const delay = i * STAGGER_MS;
      workers.push((async () => {
        if (delay > 0) await new Promise(r => setTimeout(r, delay));
        await worker();
      })());
    }
    await Promise.all(workers);

    // Safety sweep: scan every episode in the queue and force-reset any still
    // sitting at gen_status='generating'. Belt-and-suspenders for cases where
    // _rangeRunEpisode's own finally-block managed to throw before its PUT.
    try {
      const fresh = await api.get(`/api/series/${RANGE.seriesId}/episodes`);
      for (const num of queue) {
        const ep = fresh.find(e => e.number === num);
        if (ep && ep.gen_status === 'generating') {
          clog('WARN', 'range.stuck_status_swept', { sid: RANGE.seriesId, ep: num });
          try {
            await api.put(`/api/series/${RANGE.seriesId}/episodes/${num}`, { gen_status: 'failed' });
          } catch {}
        }
      }
    } catch {}
  } finally {
    window.confirm = origConfirm;
    window.appConfirm = origAppConfirm;
    RANGE.active = false;
    _rangeGenUI();
    if (RANGE.cancelRequested) {
      _rangeGenSetStatus(`⏸ Остановлено · обработано ${RANGE.curIdx + 1}/${RANGE.queue.length}`);
      showToast('⏸ Очередь остановлена', 5000);
    } else {
      _rangeGenSetStatus(`✓ Очередь завершена · ${RANGE.queue.length} серий`);
      showToast(`✓ Очередь завершена · ${RANGE.queue.length} серий`, 6000);
      try { Sounds.speak('Range generation finished.'); } catch {}
    }
    // Refresh series view so badges show final state.
    try {
      if (S.seriesId === RANGE.seriesId && document.getElementById('view-series') && !document.getElementById('view-series').classList.contains('hidden')) {
        S.series = await api.get(`/api/series/${RANGE.seriesId}`);
        if (typeof renderEpisodesList === 'function') renderEpisodesList();
      }
    } catch {}
  }
}

async function doctorScript() {
  const btn = document.getElementById('ep-doctor-btn');
  if (!S.episode) { alert('Сначала открой эпизод'); return; }
  const script = (document.getElementById('ep-script').value || '').trim();
  if (!script) { alert('Сценарий пустой'); return; }
  // Save current script first
  try { await saveEpisode(); } catch(_){}
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Анализирую дыры...';
  try {
    // Stage 1: dry run — find issues, show user, ask permission
    const r1 = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/doctor-script`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: true }),
    });
    const d1 = await r1.json();
    if (!r1.ok) throw new Error(d1.error || 'audit failed');

    const vios = d1.violations || [];
    if (vios.length === 0) {
      if (d1.audit_error) {
        alert('⚠️ Auditor отработал, но JSON не распарсился: ' + d1.audit_error + '\n\nПопробуй ещё раз.');
      } else {
        alert('✅ Логических дыр не найдено — сценарий чист.');
      }
      return;
    }

    // Build a readable summary
    const TYPE_NAMES = {
      status: 'Статус/полномочия',
      hidden_position: 'Скрытая позиция без мотивации',
      enabling_condition: 'Нет объяснения почему действие возможно',
      legal_term: 'Юр. термин не соответствует фактам',
      unmotivated_delay: 'Немотивированная задержка',
      ambiguous_cliffhanger: 'Размытый клиффхэнгер',
    };
    const lines = vios.map((v, i) =>
      `${i + 1}. [${v.severity === 'critical' ? '🔴' : '🟡'} ${TYPE_NAMES[v.type] || v.type}]\n   📍 ${v.where || '?'}\n   ❗ ${v.explanation || ''}\n   💊 ${v.fix || ''}`
    ).join('\n\n');

    const userExtra = prompt(
      `Найдено дыр: ${d1.critical_count} критичных, ${d1.minor_count} минорных.\n\n${lines}\n\n— — —\nНажми OK чтобы применить исправления (минимальные правки в местах указанных выше).\nМожно дописать СВОИ замечания в поле ниже (по-русски, одной строкой) — Доктор тоже их учтёт.\nCancel — оставить как есть.`,
      ''
    );
    if (userExtra === null) return; // cancelled

    btn.innerHTML = '<span class="spinner"></span> Лечу сценарий...';
    const ctx = { seriesId: S.series.id, episodeNum: S.episode.number, seriesTitle: S.series?.title };
    const d2 = await trackTask('Доктор сценария', ctx, async () => {
      const r2 = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/doctor-script`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ extra_notes: userExtra || '' }),
      });
      const j = await r2.json();
      if (!r2.ok) throw new Error(j.error || 'doctor failed');
      return j;
    });
    if (!r2.ok) throw new Error(d2.error || 'doctor failed');

    if (d2.changed) {
      // Update textarea + save state
      document.getElementById('ep-script').value = d2.script;
      document.getElementById('script-char-count').textContent = d2.script.length;
      // Reload episode from server to refresh script_history etc.
      if (S.episode) {
        const epR = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}`);
        if (epR.ok) S.episode = await epR.json();
      }
      alert(`✅ Сценарий обновлён.\nИсправлено пунктов: ${d2.violations_fixed}\nПредыдущая версия сохранена в истории эпизода.`);
    } else {
      alert(d2.message || 'Без изменений.');
    }
  } catch (e) {
    alert('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

async function extractCharsFromScript() {
  const btn = document.getElementById('ep-extract-chars-btn');
  if (!S.episode) { alert('Сначала открой эпизод'); return; }
  const script = (document.getElementById('ep-script').value || '').trim();
  if (!script) { alert('Сценарий пустой — впиши или сгенерируй сначала'); return; }
  // Save script first so the server reads the latest version
  try { await saveEpisode(); } catch(_){}
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Анализирую...';
  try {
    const ctx = { seriesId: S.series.id, episodeNum: S.episode.number, seriesTitle: S.series?.title };
    const d = await trackTask('Извлечение персонажей + канон', ctx, async () => {
      const r = await fetch(`/api/series/${S.series.id}/episodes/${S.episode.number}/extract-characters`, { method: 'POST' });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'fail');
      return j;
    });
    S.series = d.series;
    const addedC  = d.added_characters || [];
    const addedL  = d.added_locations  || [];
    const dropC   = d.dropped_characters || [];
    const dropL   = d.dropped_locations  || [];
    const lines = [];
    if (addedC.length) lines.push(`✅ Добавлено персонажей: ${addedC.length}\n   • ${addedC.join('\n   • ')}`);
    if (addedL.length) lines.push(`✅ Добавлено локаций: ${addedL.length}\n   • ${addedL.join('\n   • ')}`);
    if (dropC.length)  lines.push(`☐ Сняты галочки персонажей (нет в сцене): ${dropC.length}\n   • ${dropC.join('\n   • ')}`);
    if (dropL.length)  lines.push(`☐ Сняты галочки локаций (нет в сцене): ${dropL.length}\n   • ${dropL.join('\n   • ')}`);

    // --- Appearance updates from director notes ---
    const updates = d.appearance_updates || [];
    if (updates.length) {
      const blocks = updates.map(u =>
        `   • ${u.name}\n     старое: ${u.previous || '—'}\n     новое:  ${u.new}\n     причина: ${u.reason || '—'}`
      ).join('\n\n');
      lines.push(`🎭 Обновлены образы по заметкам (${updates.length}) — портреты пересоздадутся автогеном:\n\n${blocks}`);
    } else if (d.director_notes_used) {
      // Notes existed but model didn't trigger any update — let user know it was considered
      lines.push('📝 Заметки учтены, но обновлений образов не потребовалось.');
    }

    // --- Synopsis update ---
    if (d.synopsis && d.synopsis.updated && d.synopsis.new) {
      // Only touch the textarea if user is still on the same episode page
      const stillOnEp = S.seriesId === ctx.seriesId && S.episodeNum === ctx.episodeNum
                     && !document.getElementById('view-episode')?.classList.contains('hidden');
      if (stillOnEp) {
        const synEl = document.getElementById('ep-synopsis');
        if (synEl) {
          synEl.value = d.synopsis.new;
          if (S.episode) S.episode.synopsis = d.synopsis.new;
        }
      }
      lines.push(`📝 Синопсис переписан на основе сценария (${d.synopsis.new.length} симв.).`);
    } else if (d.synopsis && d.synopsis.error) {
      lines.push(`⚠️ Синопсис не обновился: ${d.synopsis.error}`);
    }

    // --- Canon audit ---
    deliverCanonAudit(d.canon || null, ctx);
    if (d.canon) {
      if (d.canon.audited && !d.canon.passes) {
        const critCount = (d.canon.violations || []).filter(v => v.severity === 'critical').length;
        lines.push(`⚠️ Канон: найдено критических нарушений — ${critCount}. Смотри панель ниже сценария.`);
      } else if (d.canon.updated) {
        const sum = d.canon.update_summary || {};
        const facts = sum.new_facts != null ? sum.new_facts : '?';
        const day = sum.world_day != null ? sum.world_day : '?';
        lines.push(`📚 Канон обновлён: новых фактов — ${facts}, world_day — ${day}.`);
      } else if (d.canon.audit_error) {
        lines.push(`⚠️ Канон-аудит: ${d.canon.audit_error}`);
      }
    }

    if (lines.length === 0) {
      alert('Сценарий пере-сверен. Новых персонажей/локаций нет, активные галочки актуальны.');
    } else {
      lines.push('');
      lines.push('Картинки для новых персонажей/локаций запустятся автоматически (если включён автоген).');
      alert(lines.join('\n\n'));
    }
    // Refresh whatever views are currently shown
    if (typeof renderCharactersList === 'function') renderCharactersList();
    if (typeof renderLocationsList  === 'function') renderLocationsList();
    if (typeof renderItemsList      === 'function') renderItemsList();
    if (typeof loadEpisodeView === 'function' && S.episode) loadEpisodeView(S.episode.number);
  } catch (e) {
    alert('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Convergence-mode: regenerate synopsis from bridge beat + script for every episode
// from the first unwritten one through to the chosen landmark (finale | nearest checkpoint).
async function generateToLandmark(landmarkType) {
  if (!S.seriesId) return;
  const s = S.series || {};
  // Resolve target episode locally for confirm dialog
  let targetEp = null;
  if (landmarkType === 'finale') {
    const fin = s.finale;
    if (!fin || !(fin.description || '').trim()) {
      alert('Финал не прикреплён к сериалу. Открой «🏁 Финал» и опиши финальную серию.');
      return;
    }
    targetEp = fin.episode;
  } else if (landmarkType === 'checkpoint') {
    const cps = (s.checkpoints || []).filter(c => (c.description || '').trim());
    if (!cps.length) {
      alert('Нет контрольных точек. Создай хотя бы одну через кнопку «🎯 Контрольная точка».');
      return;
    }
    const currentEp = S.episodeNum || 1;
    const upcoming = cps.filter(c => c.episode >= currentEp).sort((a, b) => a.episode - b.episode);
    if (!upcoming.length) {
      alert('Все контрольные точки уже позади текущей серии.');
      return;
    }
    targetEp = upcoming[0].episode;
  } else {
    return;
  }

  const currentEp = S.episodeNum || 1;
  const span = targetEp - currentEp + 1;
  if (span < 1) {
    alert('Целевая серия раньше текущей.');
    return;
  }
  if (span > 8) {
    alert(`Слишком большой диапазон: ${span} серий. Максимум 8 за раз. Сгенерь сначала промежуточные.`);
    return;
  }
  const label = landmarkType === 'finale' ? 'финала' : 'контрольной точки';
  if (!confirm(
    `Запустить генерацию ${span} серии(й) — Ep ${currentEp}…${targetEp} — до ${label}?\n\n` +
    `Для каждой серии:\n` +
    `  1) Синопсис будет ПЕРЕЗАПИСАН из bridge-плана (старая версия уйдёт в history).\n` +
    `  2) Сценарий будет сгенерирован с максимальным весом конвергенции.\n\n` +
    `Это займёт несколько минут.`
  )) return;

  const ftBtn = document.getElementById('ep-gen-to-finale-btn');
  const cpBtn = document.getElementById('ep-gen-to-checkpoint-btn');
  const mainBtn = document.getElementById('ep-gen-script-btn');
  [ftBtn, cpBtn, mainBtn].forEach(b => { if (b) b.disabled = true; });
  const status = document.getElementById('ep-script-gen-status');
  if (status) {
    status.textContent = `🎯 Генерируем ${span} серий до ${label}…`;
    status.style.color = 'var(--accent)';
  }
  try {
    const taskCtx = { seriesId: S.seriesId, episodeNum: currentEp, seriesTitle: S.series?.title };
    const res = await trackTask(`До ${label} (Ep ${currentEp}…${targetEp})`, taskCtx, () =>
      api.post(`/api/series/${S.seriesId}/generate-to-landmark`, {
        landmark_type: landmarkType,
        landmark_episode: landmarkType === 'checkpoint' ? targetEp : undefined,
        model: _selectedWriterModel('writer-model-ep'),
      })
    );
    const ok = res.completed || 0;
    const total = res.span || span;
    const errLine = res.error ? `\n\nПерви́ ошибка: ${res.error}` : '';
    showToast(`🎯 Готово: ${ok} из ${total} серий сгенерировано${errLine}`);
    if (status) {
      status.textContent = `🎯 ${ok}/${total} серий до ${label} готово.`;
      status.style.color = ok === total ? 'var(--success)' : 'var(--warning)';
    }
    // Refresh current episode view + episode list
    if (typeof loadSeries === 'function') await loadSeries(S.seriesId);
    if (typeof renderEpisodesList === 'function') renderEpisodesList();
    // Re-load current episode to pick up the new synopsis + script
    if (S.episodeNum) {
      try {
        const ep = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
        S.episode = ep;
        setVal('ep-synopsis', ep.synopsis || '');
        setVal('ep-script', ep.script || '');
      } catch (e) { /* ignore */ }
    }
  } catch (e) {
    showToast('Ошибка: ' + (e?.message || e));
    if (status) {
      status.textContent = '❌ Ошибка: ' + (e?.message || e);
      status.style.color = 'var(--danger)';
    }
  } finally {
    [ftBtn, cpBtn, mainBtn].forEach(b => { if (b) b.disabled = false; });
  }
}

// Show/hide the "До чекпоинта / финала" buttons based on whether the current series
// has those landmarks pinned ahead of the current episode.
function refreshLandmarkButtonsVisibility() {
  const ftBtn = document.getElementById('ep-gen-to-finale-btn');
  const cpBtn = document.getElementById('ep-gen-to-checkpoint-btn');
  const s = S.series || {};
  const currentEp = S.episodeNum || 1;
  // Finale
  if (ftBtn) {
    const fin = s.finale;
    const hasFinAhead = fin && (fin.description || '').trim() && fin.episode >= currentEp;
    if (hasFinAhead) {
      ftBtn.style.display = '';
      ftBtn.textContent = `🏁 До финала (Ep ${fin.episode})`;
    } else {
      ftBtn.style.display = 'none';
    }
  }
  // Nearest checkpoint
  if (cpBtn) {
    const cps = (s.checkpoints || []).filter(c => (c.description || '').trim() && c.episode >= currentEp);
    if (cps.length) {
      const nearest = cps.sort((a, b) => a.episode - b.episode)[0];
      cpBtn.style.display = '';
      cpBtn.textContent = `📍 До чекпоинта (Ep ${nearest.episode})`;
    } else {
      cpBtn.style.display = 'none';
    }
  }
}

async function generateEpisodeScript() {
  const btn = document.getElementById('ep-gen-script-btn');
  const status = document.getElementById('ep-script-gen-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  status.textContent = 'Генерируем сценарий...';
  status.style.color = 'var(--warning)';
  const postgenEl = document.getElementById('ep-postgen-checks');
  if (postgenEl) postgenEl.innerHTML = '';
  const taskCtx = { seriesId: S.seriesId, episodeNum: S.episodeNum, seriesTitle: S.series?.title };
  try {
    await saveEpisodeSilent();
    const res = await trackTask('Сценарий эпизода', taskCtx, () =>
      api.post(`/api/series/${S.seriesId}/episodes/${S.episodeNum}/generate-script`, {
        model: _selectedWriterModel('writer-model-ep'),
      })
    );
    setVal('ep-script', res.script);
    S.episode.script = res.script;
    if (res.characters_used) S.episode.characters_used = res.characters_used;
    if (res.character_outfits) {
      // Backend returns lists; older episodes may still hold strings — normalize both sides
      const merged = { ...(S.episode.character_outfits || {}) };
      for (const [cid, raw] of Object.entries(res.character_outfits)) {
        const arr = Array.isArray(raw) ? raw.filter(Boolean).map(String) : (raw ? [String(raw)] : []);
        if (arr.length) merged[cid] = arr; else delete merged[cid];
      }
      S.episode.character_outfits = merged;
    }
    if (res.locations_used) S.episode.locations_used = res.locations_used;
    if (res.audit_report) S.episode.audit_report = res.audit_report;
    if (res.logic_brief) S.episode.logic_brief = res.logic_brief;
    // Surface audit result to user (informational — pipeline already auto-retried internally)
    const ar = res.audit_report;
    if (ar) {
      const crit = (ar.violations || []).filter(v => v.severity === 'critical').length;
      if (ar.passes && crit === 0) {
        console.log(`[logic-audit] ✓ ep ${S.episodeNum} clean (retries: ${ar.retries})`);
      } else {
        console.warn(`[logic-audit] ⚠ ep ${S.episodeNum} has ${crit} critical issues after ${ar.retries} retries`,
                     ar.violations);
      }
    }
    // Backend may have auto-added new characters / outfits parsed from the cast block
    if (res.series) {
      S.series = res.series;
      if (typeof renderCharactersList === 'function') renderCharactersList();
    }
    renderEpCharacters();
    renderEpLocations();
  renderEpItems();
    updateScriptCounter();
    updateGenScriptBtn();
    status.textContent = '✓ Сценарий готов. Жми «🤖 Извлечь персонажей и локации» когда будешь готов.';
    status.style.color = 'var(--success)';
    // Sound notification — gated by per-episode toggle (default ON, persisted)
    if (_scriptSoundsEnabled()) {
      try { Sounds.playSuccess(); } catch (e) {}
    }
    // Auto-run post-gen checks: surface logic audit violations + phrase scan
    _epPostGenChecks(res.script, res.audit_report);
    // Notify about auto-created outfits from SCENE_OPEN
    _notifyNewOutfits(res._new_outfits);
    // NOTE: auto-extraction of chars/locations is INTENTIONALLY skipped here.
    // User wants explicit control — they'll click "🤖 Извлечь персонажей и локации"
    // when ready. Backend also no longer auto-syncs cast block on script-save.
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
    if (_scriptSoundsEnabled()) {
      try { Sounds.playError(); } catch (err) {}
    }
    btn.disabled = false;
    updateGenScriptBtn();
  }
}

// Persisted toggle: controls success/error sound at end of script generation.
// Wired once when episode loads; user can toggle without affecting other sounds.
function _scriptSoundsEnabled() {
  const cb = document.getElementById('ep-script-sounds');
  if (!cb) return false;
  if (!cb.dataset.wired) {
    const saved = localStorage.getItem('script_gen_sounds');
    cb.checked = saved === null ? true : saved === '1';
    cb.addEventListener('change', () => {
      localStorage.setItem('script_gen_sounds', cb.checked ? '1' : '0');
    });
    cb.dataset.wired = '1';
  }
  return !!cb.checked;
}

