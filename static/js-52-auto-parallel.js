// ── Manual parallel launcher ───────────────────────────────────────────────
// Spins the currently-open episode up as an ADDITIONAL auto-mode run while
// another episode's run is already in flight. Runs the same pre-flight as the
// primary path (missing-asset check + 15s-duration enforcement + confirm),
// gathers params from the Seedance panel, then delegates to the proven
// standalone engine. Always sequential / scene-parallel — turbo submits
// everything at once and finishes fast, so it never needs a long-lived slot.
async function _startParallelAutoMode(sid, num) {
  // Pre-flight: don't burn compute on chunks with placeholder/random faces.
  if (!await _confirmMissingAssetsBeforeGen('Auto-mode (видео-генерацию)')) {
    clog('WARN', 'auto.parallel_bail', { reason: 'missing_assets_cancelled', ep: num });
    return;
  }
  // Duration must be 15s — segmentation is calibrated around 15s chunks.
  const durEl = document.getElementById('sd-duration');
  const curDur = parseInt(durEl?.value, 10);
  if (curDur !== 15) {
    const ok = await appConfirm({
      title: '⚠ Не та длительность Seedance',
      message:
        `Длительность Seedance стоит ${curDur || '?'}с, но Auto-mode калиброван под 15с.\n\n` +
        `Поставить 15с автоматически и продолжить?`,
      okText: 'Поставить 15с и запустить', cancelText: 'Отмена', okStyle: 'accent',
    });
    if (!ok) { clog('WARN', 'auto.parallel_bail', { reason: 'duration_cancelled', ep: num }); return; }
    if (durEl) { durEl.value = '15'; durEl.dispatchEvent(new Event('change', { bubbles: true })); }
  }
  // Params from the Seedance panel — same controls the primary path reads.
  const opts = {
    useLastframe: true,         // sequential continuity always on
    useCutframes: true,
    useStyle: !!document.getElementById('sd-use-style')?.checked,
    styleVal: (document.getElementById('sd-style')?.value || '').trim(),
    baseOnly: !!document.getElementById('sd-base-only')?.checked,
    closeUpOnly: !!document.getElementById('sd-close-up-only')?.checked,
    duration: parseInt(document.getElementById('sd-duration')?.value, 10) || 15,
    resolution: document.getElementById('sd-resolution')?.value || '720p',
    moderation_bypass: document.getElementById('sd-mod-bypass')?.value || 'off',
    model_tier: document.getElementById('sd-model-tier')?.value || 'reference-fast',
    errorMode: localStorage.getItem('auto_error_mode') || 'heal',
    enableMusic: S.series?.settings?.enable_music !== false,
    maxParallelScenes: 2,
    _manualParallel: true,
  };
  const liveCount = _autoActiveRunCount();
  if (!await appConfirm({
    title: '▶ Запустить ещё один Auto-mode параллельно?',
    message:
      `Эпизод ${num} запустится ПАРАЛЛЕЛЬНО с уже идущим(и) авторежимом(ами) (сейчас активно: ${liveCount}).\n` +
      `Движок: последовательный (сцены внутри эпизода — параллельно).\n` +
      `Уже готовые чанки и помеченные «skip» сегменты пропустятся.\n\n` +
      `Максимум одновременно: ${MAX_CONCURRENT_AUTO} эпизода. Прогресс — в виджете справа внизу (⏹ останавливает только свой эпизод).`,
    okText: '▶ Запустить', cancelText: 'Отмена', okStyle: 'accent',
  })) {
    clog('WARN', 'auto.parallel_bail', { reason: 'confirm_cancelled', ep: num });
    return;
  }

  showToast(`▶ Auto-mode эп.${num} запущен параллельно`, 4000);
  clog('INFO', 'auto.parallel_manual_start', { sid, ep: num, live: liveCount });
  let res;
  try {
    res = await _runEpisodeAutoStandalone(sid, num, opts);
  } catch (e) {
    Sounds.playError();
    showToast(`✗ Auto-mode эп.${num} упал: ${e.message || e}`, 8000);
    return;
  }
  if (res && res.ok) {
    Sounds.speak(`Episode ${num} generation finished.`);
    showToast(`✓ Auto-mode эп.${num} завершён · ${res.completed}/${res.total}`, 6000);
  } else if (res) {
    Sounds.playError();
    showToast(`⏸ Auto-mode эп.${num}: ${res.completed}/${res.total}${res.errors ? `, ошибок ${res.errors}` : ''}`, 7000);
  }
}

// ── Standalone parallel auto-run for range-gen (concurrency > 1) ──────────
// Self-contained per-episode auto-mode runner. Doesn't touch global AUTO so
// multiple episodes can run concurrently without state collisions. Mirrors
// the sequential-mode logic of startAutoMode (scene-parallel internally, up
// to 3 scene-chains per episode), but uses a fresh Run object per call and
// registers it into AUTO_RUNS for the floating widget.
//
// Inputs: sid (string), num (int), opts {
//   useLastframe, useCutframes, useStyle, styleVal, baseOnly, closeUpOnly,
//   duration, resolution, moderation_bypass, errorMode, maxParallelScenes
// }
// Returns: { ok: bool, completed: int, total: int, errors: int }
async function _runEpisodeAutoStandalone(sid, num, opts = {}) {
  const epSid = sid;
  const epNumber = num;
  // Per-run state, same shape as AUTO. Lives in AUTO_RUNS until done.
  const R = {
    active: true, parallel: false, cancelRequested: false,
    completedCount: 0, cursor: 0, total: 0, activeChains: 0,
    errorMode: opts.errorMode || 'heal',
    lastStatus: '⚙ загружаю серию...',
    segments: [],
    _epSid: epSid, _epNumber: epNumber,
  };
  _autoRegisterRun(R);

  let errorsCount = 0;

  try {
    // 1. Fetch episode JSON to get the script text.
    R.lastStatus = '⚙ загружаю серию...';
    _autoUpdateFloatingWidget();
    let ep;
    try {
      ep = await api.get(`/api/series/${epSid}/episodes/${epNumber}`);
    } catch (e) {
      clog('ERROR', 'parallel.fetch_fail', { sid: epSid, ep: epNumber, msg: (e?.message || String(e)).slice(0, 200) });
      return { ok: false, completed: 0, total: 0, errors: 1 };
    }
    const scriptText = (ep?.script || '').trim();
    if (!scriptText) {
      clog('WARN', 'parallel.no_script', { sid: epSid, ep: epNumber });
      return { ok: false, completed: 0, total: 0, errors: 1 };
    }

    // 2. Build segments from script text directly (bypass DOM-bound
    //    _autoCollectSegments). Reuses _parseScriptScenes which is pure.
    R.lastStatus = '⚙ строю сегменты...';
    _autoUpdateFloatingWidget();
    // Honor the episode's saved manual segment splits/merges if the fetched
    // episode carries them; otherwise no overrides. MUST be an array — passing
    // `{}` here used to crash the run with «(overrides || []) is not iterable».
    const _ovr = Array.isArray(ep?.segment_overrides) ? ep.segment_overrides : [];
    const scenes = _parseScriptScenes(scriptText, _ovr);
    const allSegs = [];
    scenes.forEach((sc, sIdx) => {
      for (let g = 0; g < sc.segCount; g++) {
        const lines = sc.lines.filter(l => l.segIdx === g);
        if (!lines.length) continue;
        const head = sc.heading ? sc.heading + '\n\n' : '';
        const text = head + lines.map(l => l.text).join('\n');
        const hasCloseUp = lines.some(l => _isLineCloseUp(l.text));
        const anchor = _lineAnchor(lines[0].text);
        // Same VO-aware shared helper used by sequential auto-mode and retry.
        const segmentText = lines.map(l => l.text).join('\n');
        const durationSec = _estimateChunkDurationSec(segmentText);
        allSegs.push({
          sceneIdx: sIdx, segIdx: g, text, anchor,
          has_close_up: hasCloseUp, durationSec,
          scriptOrder: allSegs.length,
        });
      }
    });
    if (!allSegs.length) {
      clog('WARN', 'parallel.no_segments', { sid: epSid, ep: epNumber, script_len: scriptText.length, scene_count: scenes.length });
      return { ok: false, completed: 0, total: 0, errors: 1 };
    }
    // Resume-safe + skip-aware filter. Drop segments whose chunk is already
    // completed on the backend (matched by script_order) so a re-launched run
    // (manual parallel on a partial episode, or range-gen after a refresh)
    // doesn't redo finished work, and honor the per-segment AUTO-skip flags
    // persisted on the episode. For a brand-new episode both sets are empty, so
    // this is a no-op — range-gen behaviour is unchanged.
    const _existingChunks = (ep && ep.seedance_chunks) || [];
    const _completedOrders = new Set(
      _existingChunks
        .filter(c => c && c.status === 'completed' && typeof c.script_order === 'number')
        .map(c => c.script_order)
    );
    const _skipKeys = new Set((ep && ep.segment_auto_skips) || []);
    const segs = allSegs.filter(s =>
      !_completedOrders.has(s.scriptOrder) &&
      !_skipKeys.has(`s${s.sceneIdx}g${s.segIdx}`)
    );
    if (!segs.length) {
      clog('INFO', 'parallel.nothing_to_do', {
        sid: epSid, ep: epNumber, total_raw: allSegs.length,
        done: _completedOrders.size, skipped: _skipKeys.size,
      });
      // Nothing left to generate counts as success, not an error — the episode
      // is already complete (or every remaining segment is skipped).
      return { ok: true, completed: 0, total: 0, errors: 0 };
    }
    R.segments = segs;
    R.total = segs.length;
    clog('INFO', 'parallel.built', {
      sid: epSid, ep: epNumber, total: segs.length, raw: allSegs.length,
      done: _completedOrders.size, skipped: _skipKeys.size, scenes: scenes.length,
    });

    // Group by scene for scene-parallel execution within the episode.
    const sceneGroups = (() => {
      const m = new Map();
      for (const s of segs) {
        if (!m.has(s.sceneIdx)) m.set(s.sceneIdx, []);
        m.get(s.sceneIdx).push(s);
      }
      return [...m.values()];
    })();

    // ── Early-fire music ────────────────────────────────────────────────────
    // We have the full scene structure with duration estimates right now —
    // before any video is generated. Fire music generation immediately so it
    // runs in parallel with video rendering (saves 30-60+ minutes of waiting).
    if (opts.enableMusic !== false) {
      const scenesPlan = sceneGroups.map(grp => ({
        sceneIdx: grp[0].sceneIdx,
        // sum of all segment durations × 0.9 headroom, in ms
        target_duration_ms: Math.round(
          grp.reduce((acc, s) => acc + (s.durationSec || 15), 0) * 0.9 * 1000
        ),
      }));
      api.post(
        `/api/series/${epSid}/episodes/${epNumber}/music/generate`,
        { scenes_plan: scenesPlan }
      ).then(r => {
        if (r?.ok) clog('INFO', 'parallel.music_early', { ep: epNumber, scenes: r.scenes?.length || 0 });
      }).catch(e => {
        clog('WARN', 'parallel.music_early_fail', { ep: epNumber, err: (e?.message || String(e)).slice(0, 200) });
      });
    }
    // ────────────────────────────────────────────────────────────────────────

    const POLL_INTERVAL_MS = 8000;
    const MAX_HEAL_RETRIES = 1;
    const MAX_PARALLEL_SCENES = opts.maxParallelScenes || 2;
    const shared = {
      useLastframe: opts.useLastframe !== false,
      useCutframes: opts.useCutframes !== false,
      useStyle: !!opts.useStyle,
      styleVal: opts.styleVal || '',
      baseOnly: !!opts.baseOnly,
      closeUpOnly: !!opts.closeUpOnly,
      duration: opts.duration || 15,
      resolution: opts.resolution || '720p',
      moderation_bypass: opts.moderation_bypass || 'off',
      model_tier: opts.model_tier || 'reference-fast',
    };

    async function pollUntilDone(chunkIdx, composeRes, segText, segDuration) {
      let healAttempts = 0;
      let curIdx = chunkIdx;
      // Hard ceiling on how long we wait for ONE chunk to terminate. Without
      // this, a chunk stuck in 'submitting' forever (server submit-thread
      // crashed silently, AVAI returned but our handler dropped the response,
      // user deleted the chunk) would spin the poll-loop indefinitely — runner
      // never returns → range-gen worker never exits → user sees endless
      // «Auto-mode крутится».
      const HARD_TIMEOUT_MS = 12 * 60 * 1000;   // 12 minutes per chunk
      const NO_CHUNK_TOLERANCE_MS = 90 * 1000;  // 90s grace if chunk disappears
      const t0 = Date.now();
      let firstMissingAt = 0;
      while (true) {
        if (R.cancelRequested) return { ok: false, error: 'cancelled' };
        if (Date.now() - t0 > HARD_TIMEOUT_MS) {
          clog('ERROR', 'parallel.poll_timeout', { sid: epSid, ep: epNumber, idx: curIdx });
          return { ok: false, error: 'poll timeout 12min' };
        }
        await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
        let polled;
        try {
          const res = await api.post(`/api/series/${epSid}/episodes/${epNumber}/seedance/poll`, {});
          polled = res.chunks || [];
          // If the user is currently viewing THIS episode, animate the chunk
          // grid live (parallel runs are watched manually, not just headless).
          if (S.seriesId === epSid && S.episode?.number === epNumber) {
            try { _sdNotifyTransitions(polled); } catch {}
            try { sdRenderList(polled); } catch {}
          }
        } catch (e) { polled = []; }
        const chunk = polled.find(c => c.idx === curIdx);
        if (!chunk) {
          if (!firstMissingAt) firstMissingAt = Date.now();
          if (Date.now() - firstMissingAt > NO_CHUNK_TOLERANCE_MS) {
            clog('ERROR', 'parallel.chunk_vanished', { sid: epSid, ep: epNumber, idx: curIdx });
            return { ok: false, error: 'chunk vanished from list' };
          }
          R.lastStatus = `… не вижу #${curIdx}`;
          _autoUpdateFloatingWidget();
          continue;
        }
        firstMissingAt = 0;
        const qcStatus2 = chunk.qc?.status || null;
        if (chunk.status === 'completed' && qcStatus2 == null) {
          R.lastStatus = `🔍 #${curIdx} QC...`;
        } else {
          R.lastStatus = chunk.status === 'processing' && chunk.progress != null
            ? `⏳ #${curIdx} ${chunk.progress}%`
            : `⏳ #${curIdx} ${chunk.status}${qcStatus2 ? ' · QC '+qcStatus2 : ''}`;
        }
        _autoUpdateFloatingWidget();
        if (chunk.status === 'completed' && qcStatus2 === 'pass') return { ok: true, chunk };
        if (chunk.status === 'completed' && qcStatus2 === 'retry_exhausted') {
          showToast(`⚠ QC сдался на чанке #${curIdx} (${(chunk.qc?.fails || []).join(', ')})`, 6000);
          return { ok: true, chunk };
        }
        if (chunk.status === 'completed' && qcStatus2 === 'fail') {
          R.lastStatus = `🔁 #${curIdx} QC retry ${chunk.qc.attempts}/3`;
          _autoUpdateFloatingWidget();
          const hint = _qcBuildPromptHint(chunk.qc.fails || []);
          try {
            const restart = await api.post(`/api/series/${epSid}/episodes/${epNumber}/seedance/start`, {
              prompt: (composeRes.prompt || chunk.prompt || '') + hint,
              chunk_text: segText,
              duration: segDuration || shared.duration,
              resolution: shared.resolution,
              moderation_bypass: shared.moderation_bypass,
              mod_full_battery: true,   // auto-mode: full bypass battery + wait
              model: shared.model_tier,
              refs: (composeRes.refs || []).map(r => ({
                kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
                source: r.source, prev_idx: r.prev_idx, name: r.name,
                cut_index: r.cut_index, cut_time: r.cut_time,
              })),
            });
            if (restart?.chunk?.idx != null) curIdx = restart.chunk.idx;
            continue;
          } catch (e) {
            return { ok: false, error: 'qc-retry-failed: ' + (e?.message || e) };
          }
        }
        if (chunk.status === 'failed') {
          // Moderation recovery is owned by the SERVER poll ladder now (see
          // js-51 note). Transient recovery shows as 'moderation_blocked' and
          // keeps polling; 'failed' means the ladder is exhausted — do NOT
          // re-heal here (old mangling path + duplicate chunks). Just stop.
          return { ok: false, chunk, error: chunk.error || 'failed' };
        }
      }
    }

    async function composeAndStart(seg) {
      const segCloseUp = shared.closeUpOnly || !!seg.has_close_up;
      R.lastStatus = segCloseUp ? '⚙ compose (close-up)...' : '⚙ compose...';
      _autoUpdateFloatingWidget();
      let composeRes;
      try {
        composeRes = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/compose`,
          {
            chunk_text: seg.text,
            use_prev_lastframe: shared.useLastframe,
            use_prev_cutframes: shared.useCutframes,
            style: shared.useStyle ? shared.styleVal : '',
            base_outfits_only: shared.baseOnly,
            close_up_only: segCloseUp,
          }, { timeoutMs: 300_000 }
        );
      } catch (e) {
        clog('ERROR', 'parallel.compose_throw', {
          sid: epSid, ep: epNumber, seg_anchor: (seg.anchor || '').slice(0, 60),
          msg: (e?.message || String(e)).slice(0, 300),
        });
        throw e;
      }
      if (composeRes?.error) {
        clog('ERROR', 'parallel.compose_error', { sid: epSid, ep: epNumber, err: String(composeRes.error).slice(0, 300) });
        throw new Error('compose: ' + composeRes.error);
      }
      R.lastStatus = '▶ start...';
      _autoUpdateFloatingWidget();
      let startRes;
      try {
        startRes = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
          {
            prompt: composeRes.prompt,
            chunk_text: seg.text,
            duration: seg.durationSec || shared.duration,
            resolution: shared.resolution,
            moderation_bypass: shared.moderation_bypass,
            mod_full_battery: true,   // auto-mode: full bypass battery + wait
            model: shared.model_tier,
            script_order: seg.scriptOrder,
            refs: (composeRes.refs || []).map(r => ({
              kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
              source: r.source, prev_idx: r.prev_idx, name: r.name,
              cut_index: r.cut_index, cut_time: r.cut_time,
            })),
          }, { timeoutMs: 60_000 }
        );
      } catch (e) {
        clog('ERROR', 'parallel.start_throw', {
          sid: epSid, ep: epNumber, seg_anchor: (seg.anchor || '').slice(0, 60),
          msg: (e?.message || String(e)).slice(0, 300),
        });
        throw e;
      }
      if (startRes?.error) {
        clog('ERROR', 'parallel.start_error', { sid: epSid, ep: epNumber, err: String(startRes.error).slice(0, 300) });
        throw new Error('start: ' + startRes.error);
      }
      if (startRes?.chunk?.idx == null) {
        clog('ERROR', 'parallel.start_no_idx', { sid: epSid, ep: epNumber, startRes_keys: Object.keys(startRes || {}).join(',') });
        throw new Error('start вернул пустой chunk_idx');
      }
      return { composeRes, startRes };
    }

    async function runSceneChain(sceneSegs) {
      R.activeChains++;
      _autoUpdateFloatingWidget();
      try {
        for (const seg of sceneSegs) {
          if (R.cancelRequested) return 'cancelled';
          let cs;
          try { cs = await composeAndStart(seg); }
          catch (e) { errorsCount++; return 'failed'; }
          const chunkIdx = cs.startRes?.chunk?.idx;
          if (chunkIdx == null) { errorsCount++; return 'failed'; }
          const poll = await pollUntilDone(chunkIdx, cs.composeRes, seg.text, seg.durationSec);
          if (poll.error === 'cancelled') return 'cancelled';
          if (!poll.ok) { errorsCount++; return 'failed'; }
          R.completedCount++;
          R.cursor = R.completedCount;
          _autoUpdateFloatingWidget();
        }
        return 'completed';
      } finally {
        R.activeChains--;
        _autoUpdateFloatingWidget();
      }
    }

    // Concurrency-capped runner for scene chains.
    const queue = sceneGroups.slice();
    async function worker() {
      while (queue.length) {
        if (R.cancelRequested) return;
        const item = queue.shift();
        await runSceneChain(item);
      }
    }
    const workers = [];
    for (let i = 0; i < Math.min(MAX_PARALLEL_SCENES, sceneGroups.length); i++) workers.push(worker());
    await Promise.all(workers);

    clog('INFO', 'parallel.finished', {
      sid: epSid, ep: epNumber,
      completed: R.completedCount, total: R.total, errors: errorsCount,
      cancelled: !!R.cancelRequested,
    });
    return {
      ok: !R.cancelRequested && errorsCount === 0,
      completed: R.completedCount,
      total: R.total,
      errors: errorsCount,
    };
  } catch (e) {
    // Anything that bubbled past the per-scene catches above lands here.
    // Without this branch a throw would skip finally→cleanup and leave R in
    // AUTO_RUNS spinning forever.
    clog('ERROR', 'parallel.crash', {
      sid: epSid, ep: epNumber, msg: (e?.message || String(e)).slice(0, 400),
    });
    return { ok: false, completed: R.completedCount, total: R.total, errors: errorsCount + 1 };
  } finally {
    R.active = false;
    _autoUnregisterRun(R);
  }
}

function _stopAllAutoRuns() {
  if (AUTO.active) AUTO.cancelRequested = true;
  for (const r of AUTO_RUNS.values()) r.cancelRequested = true;
}

