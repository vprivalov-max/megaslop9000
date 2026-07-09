async function startAutoMode() {
  clog('INFO', 'auto.entry', { sid: S.seriesId || null, ep: S.episode?.number ?? null });
  if (!S.episode) {
    clog('WARN', 'auto.bail', { reason: 'no_episode' });
    showToast('⚠ Сначала открой эпизод');
    return;
  }
  const _sid = S.seriesId, _num = S.episode.number;
  // Already running on THIS episode → ignore (the button shows Stop in that
  // case; stopping is handled by _autoModeToggle, not here).
  if (_autoRunForEpisode(_sid, _num)) {
    clog('WARN', 'auto.bail', { reason: 'episode_already_running', ep: _num });
    showToast(`Auto-mode уже идёт на эпизоде ${_num}`);
    return;
  }
  // Global concurrency ceiling — at most MAX_CONCURRENT_AUTO live runs across
  // the series at once (matches range-gen; the backend handles 3 concurrent
  // per-episode pipelines safely).
  if (_autoActiveRunCount() >= MAX_CONCURRENT_AUTO) {
    clog('WARN', 'auto.bail', { reason: 'concurrency_cap', cap: MAX_CONCURRENT_AUTO });
    showToast(`⚠ Уже идёт ${MAX_CONCURRENT_AUTO} авторежима одновременно — дождись или останови один (⏹ в виджете)`, 6000);
    return;
  }
  // If the PRIMARY slot is busy on another episode, spin THIS episode up as an
  // additional parallel run via the self-contained standalone engine. The rich
  // primary path below (turbo / skip-filter / confirm) is reserved for the
  // first/only run; parallel runs are sequential + scene-parallel.
  if (AUTO.active) {
    return _startParallelAutoMode(_sid, _num);
  }
  // Pre-flight: warn if any active char/loc lacks ref before kicking off N
  // chunks of generation. User often regrets discovering missing assets only
  // after burning compute on chunks with placeholder/random faces.
  if (!await _confirmMissingAssetsBeforeGen('Auto-mode (видео-генерацию)')) {
    clog('WARN', 'auto.bail', { reason: 'missing_assets_cancelled' });
    return;
  }
  // Auto-mode hard requirement: duration MUST be 15s. The whole segmentation
  // logic (TARGET=12s, SOFT_MAX=13s, MIN=5s) is calibrated assuming 15s
  // Seedance chunks. If user picked 5/10s clips, segments won't fit and the
  // whole batch will be off-rhythm. Offer to auto-fix or cancel.
  const durEl = document.getElementById('sd-duration');
  const curDur = parseInt(durEl?.value, 10);
  if (curDur !== 15) {
    const confirmFix = await appConfirm({
      title: '⚠ Не та длительность Seedance',
      message:
        `Длительность Seedance стоит ${curDur || '?'}с, но Auto-mode калиброван под 15-секундные чанки.\n\n` +
        `Сегментация рассчитывала контент ≤13с с 2с буфером — короткие чанки порежут реплики, ` +
        `длинные дадут пустоту в конце.\n\n` +
        `Поставить 15с автоматически и продолжить?`,
      okText: 'Поставить 15с и запустить',
      cancelText: 'Отмена',
      okStyle: 'accent',
    });
    if (!confirmFix) {
      clog('WARN', 'auto.bail', { reason: 'duration_cancelled', curDur });
      return;
    }
    if (durEl) {
      durEl.value = '15';
      durEl.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  // ── INSTANT MUSIC KICK-OFF (single-episode auto-mode) ────────────────────
  // Fire music NOW — before blocking / batch-compose / any video chunk.
  // Script is already in the DOM, so we can compute the scene plan instantly.
  if (S.series?.settings?.enable_music !== false && S.seriesId && S.episode?.number != null) {
    (async () => {
      try {
        const epScript = (document.getElementById('ep-script')?.value || '').trim();
        if (epScript) {
          const scenes = _parseScriptScenes(epScript, _segmentOverrides());
          const planMap = new Map();
          scenes.forEach((sc, sIdx) => {
            for (let g = 0; g < sc.segCount; g++) {
              const lines = sc.lines.filter(l => l.segIdx === g);
              if (!lines.length) continue;
              const dur = _estimateChunkDurationSec(lines.map(l => l.text).join('\n'));
              planMap.set(sIdx, (planMap.get(sIdx) || 0) + dur);
            }
          });
          if (planMap.size) {
            const scenesPlan = [...planMap.entries()].map(([sceneIdx, totalSec]) => ({
              sceneIdx,
              target_duration_ms: Math.round(totalSec * 0.9 * 1000),
            }));
            const r = await api.post(
              `/api/series/${S.seriesId}/episodes/${S.episode.number}/music/generate`,
              { scenes_plan: scenesPlan }
            );
            if (r?.ok && r.scenes?.length) {
              showToast(`🎵 Музыка запущена — ${r.scenes.length} сцен (параллельно с видео)`, 3000);
            }
          }
        }
      } catch (e) {
        clog('WARN', 'auto.music_kickoff_fail', { err: (e?.message || String(e)).slice(0, 200) });
      }
    })();
  }
  // ─────────────────────────────────────────────────────────────────────────

  // Auto-mode behaviour is now driven by the mode toggle in the toolbar
  // (not by lastframe/cutframes checkboxes which are for manual one-shot use).
  //   sequential — last-frame + cut-frames ALWAYS on, chunks render serially
  //                with full video continuity. Best quality, slowest.
  //   turbo      — all chunks fire in parallel via single batch-compose,
  //                no continuity frames, relies on scene blocking + per-segment
  //                prompt for spatial consistency. Faster, less consistent.
  const autoModeKind = _autoGetModeKind();
  const isTurbo  = autoModeKind === 'turbo';
  const useLastframe = !isTurbo;        // sequential = always true
  const useCutframes = !isTurbo;        // sequential = always true
  const useStyle     = !!document.getElementById('sd-use-style')?.checked;
  const styleVal     = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly     = !!document.getElementById('sd-base-only')?.checked;
  const closeUpOnly  = !!document.getElementById('sd-close-up-only')?.checked;
  AUTO.parallel = isTurbo;
  // ── Turbo engine selector ─────────────────────────────────────────────────
  // Two implementations of Turbo live side-by-side, switchable in Settings:
  //   • 'parallel-sequential' (default, experimental) — per-chunk Claude
  //     compose like sequential mode, then ALL chunks fire in parallel
  //     without last-frame / cut-frames continuity. No batch JSON, no scene
  //     blocking, no character-position planning, no auto-revise. Cheap +
  //     simple. Hypothesis: per-chunk prompts are enough; the heavy batch
  //     prep doesn't pay off.
  //   • 'shadow' — full Shadow Founder–style pipeline ported from the
  //     colleague's project: scene blocking → batch-compose → auto-revise →
  //     parallel /start with prebuilt prompts. Heavier, more deterministic
  //     spatial continuity.
  // Sequential mode is unaffected by this toggle.
  const turboEngine = isTurbo ? _turboEngine() : 'n/a';
  const turboShadow = turboEngine === 'shadow';

  // Turbo prerequisites: episodeBlocking + batch_prompts must exist.
  // If empty — auto-fill them before starting the parallel run, so the user
  // doesn't have to hit two extra buttons every time.
  //
  // These two Claude calls take ~10-30s each. Without a visible widget the
  // user thinks nothing's happening between clicking «Auto-mode» and the
  // first chunk creating itself (~60s of silence). Show the floating widget
  // in a «preparing» state for the duration of these prereqs so progress is
  // always visible.
  //
  // NOTE: this entire prep block (scene blocking + batch JSON + auto-revise)
  // is shadow-engine-only. The experimental parallel-sequential engine skips
  // all prep and just lets the AUTO loop run per-chunk compose in parallel.
  if (isTurbo && turboShadow) {
    const blockingEl = document.getElementById('ep-scene-blocking');
    const blocking = (blockingEl?.value || '').trim();
    const hasBatchPrompts = !!(S.episode?.batch_prompts && Object.keys(S.episode.batch_prompts).length);
    const needPrep = !blocking || !hasBatchPrompts;
    if (needPrep) {
      // Mark AUTO as active so the floating widget appears immediately.
      // _epSid/_epNumber stash so the widget header navigates to this ep.
      AUTO.active = true;
      AUTO.parallel = true;
      AUTO.total = 0;
      AUTO.completedCount = 0;
      AUTO._epSid = S.seriesId;
      AUTO._epNumber = S.episode.number;
      AUTO.lastStatus = '⚙ Турбо: подготовка…';
      _autoRegisterRun(AUTO);
      _autoUpdateStatusUI();
    }
    try {
      if (!blocking) {
        AUTO.lastStatus = '⚙ Турбо 1/2: scene blocking (~10-30с)…';
        _autoUpdateFloatingWidget();
        showToast('⚙ Турбо 1/2: scene blocking…', 4000);
        try {
          if (typeof generateSceneBlocking === 'function') await generateSceneBlocking();
        } catch (e) {
          // Clean up the floating widget before bailing.
          AUTO.active = false;
          _autoUnregisterRun(AUTO);
          _autoUpdateStatusUI();
          showToast('✗ Не удалось сгенерить blocking: ' + (e.message || e), 6000);
          return;
        }
      }
      let batchFreshlyBuilt = false;
      if (!hasBatchPrompts) {
        AUTO.lastStatus = '⚙ Турбо 2/3: batch JSON (~15-30с)…';
        _autoUpdateFloatingWidget();
        showToast('⚙ Турбо 2/3: batch JSON эпизода…', 4000);
        try {
          if (typeof rebuildBatchPrompts === 'function') await rebuildBatchPrompts();
          batchFreshlyBuilt = true;
        } catch (e) {
          AUTO.active = false;
          _autoUnregisterRun(AUTO);
          _autoUpdateStatusUI();
          showToast('✗ Не удалось собрать batch JSON: ' + (e.message || e), 6000);
          return;
        }
      }
      // ─── Турбо шаг 3/3 — авто-правка ─────────────────────────────────────
      // Mirrors colleague's auto-pipeline step 9.5 («✨ План + правка + видео»):
      // ONE Claude call rewrites every main promptEn so characters don't jump
      // in space, dialogues stay readable, chunks splice cleanly. The exact
      // wording comes from per-user settings (Modal → 🎬 Автоматическая правка).
      //
      // Trigger policy:
      //   • если batch только что собрали в этом запуске Auto-mode — правим
      //     всегда (это поведение колеги: revise после plan по умолчанию).
      //   • если batch уже существовал (повторный запуск Auto-mode после
      //     refresh) — правим только если правки ещё не было (нет
      //     `batch_revised_at` на эпизоде) И флаг включён в настройках.
      //   • если пользователь отключил флаг в настройках — правку
      //     пропускаем целиком.
      try {
        let arEnabled = true;
        try {
          const ar = await api.get('/api/user/auto-revise');
          arEnabled = !!ar.auto_revise_enabled;
        } catch (_) { /* keep default */ }
        const alreadyRevised = !!(S.episode?.batch_revised_at);
        const shouldRevise = arEnabled && (batchFreshlyBuilt || !alreadyRevised);
        if (shouldRevise) {
          AUTO.lastStatus = '⚙ Турбо 3/3: авто-правка main-промптов (~15-40с)…';
          _autoUpdateFloatingWidget();
          showToast('⚙ Турбо 3/3: применяю автоматическую правку…', 4000);
          const resp = await api.post(
            `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/revise-batch`,
            {},
            { timeoutMs: 5 * 60 * 1000 }
          );
          // Reload episode so subsequent code (segment-collect, validators)
          // sees the revised promptEns. Best-effort: failure here is non-fatal
          // — original promptEns are still valid Seedance input.
          try {
            const fresh = await api.get(`/api/series/${S.seriesId}/episodes/${S.episode.number}`);
            if (fresh) S.episode = fresh;
          } catch (_) {}
          if (resp && resp.count != null) {
            showToast(`✓ Правка применена: ${resp.count}/${resp.expected} чанков`, 3500);
          }
        }
      } catch (e) {
        // Revise is a quality booster, not a hard requirement — log and continue.
        showToast('⚠ Авто-правка упала: ' + (e.message || e) + ' — продолжаю с исходными промптами', 6000);
      }
      // Final prep tick before segment-building / confirm dialog.
      if (needPrep) {
        AUTO.lastStatus = '⚙ Турбо: считаю сегменты…';
        _autoUpdateFloatingWidget();
      }
    } catch (e) {
      // Shouldn't reach here (inner try/catch handles per-step), but defense.
      AUTO.active = false;
      _autoUnregisterRun(AUTO);
      _autoUpdateStatusUI();
      throw e;
    }
  }
  AUTO.errorMode = (localStorage.getItem('auto_error_mode') || 'heal');
  const allSegs  = _autoCollectSegments();
  // Annotate scriptOrder BEFORE the skip-filter — the script slot a segment
  // owns is its position in the FULL list, not its position among the still-
  // selected ones. If we reindex after filtering, a user who skips seg 0..2
  // and keeps seg 3 ends up generating a chunk with script_order=0; that
  // collides with the original chunk #1 (also script_order=0), the auto-
  // assemble dedup picks the newer take and drops the real #1 from the
  // final cut. Bug repro: «My Roommate From Craigslist Is Hunting Me» ep 1
  // — uncheck AUTO on 1/2/3, keep 4, auto-mode spawned a phantom «#1 v2».
  allSegs.forEach((s, i) => { s.scriptOrder = i; });
  // Per-segment auto-mode skip filter. Uses the unique-per-episode
  // autoSkipKey (`s{sceneIdx}g{segIdx}`), NOT the first-line text — see
  // toggleSegmentAutoInclude for why.
  const skippedCount = allSegs.filter(s => _isSegmentAutoSkipped(s.autoSkipKey)).length;
  // Drop segments whose chunk is already finished on the backend (matched by
  // script_order). This is what makes resume-after-refresh non-destructive —
  // re-running auto-mode picks up exactly where the previous run died.
  // `completed` is the success status set when Seedance returns the mp4; QC
  // may still be pending but the chunk file exists, so re-generating it would
  // overwrite working output for no benefit.
  const existingChunks = (S.episode && S.episode.seedance_chunks) || [];
  const completedOrders = new Set(
    existingChunks
      .filter(c => c && c.status === 'completed' && typeof c.script_order === 'number')
      .map(c => c.script_order)
  );
  const alreadyDoneCount = allSegs.filter(s => completedOrders.has(s.scriptOrder)).length;
  AUTO.segments = allSegs.filter(s =>
    !_isSegmentAutoSkipped(s.autoSkipKey) && !completedOrders.has(s.scriptOrder)
  );
  AUTO.total    = AUTO.segments.length;
  AUTO.cursor   = 0;             // back-compat with status UI
  AUTO.completedCount = 0;       // atomic counter across chains
  AUTO.activeChains = 0;
  AUTO.cancelRequested = false;
  AUTO.lastStatus = '';

  if (!AUTO.total) {
    clog('WARN', 'auto.bail', {
      reason: 'no_segments',
      total_raw: allSegs.length,
      skipped: skippedCount,
      script_len: (document.getElementById('ep-script')?.value || '').length,
    });
    // If we registered the AUTO run for Turbo prep, unregister so the
    // floating widget disappears.
    AUTO.active = false;
    _autoUnregisterRun(AUTO);
    _autoUpdateStatusUI();
    showToast(`⚠ Нет сегментов для генерации${skippedCount ? ` (${skippedCount} помечены как skip)` : ''}`);
    return;
  }
  // Group by scene → tells us scene-parallel layout
  const sceneGroups = (() => {
    const m = new Map();
    for (const s of AUTO.segments) {
      if (!m.has(s.sceneIdx)) m.set(s.sceneIdx, []);
      m.get(s.sceneIdx).push(s);
    }
    return [...m.values()];
  })();
  // Confirm before kicking off
  const modeWord = AUTO.parallel
    ? 'все параллельно'
    : (sceneGroups.length > 1
        ? `${sceneGroups.length} сцен параллельно × последовательно внутри сцены`
        : 'последовательно (1 сцена)');
  const errWord  = AUTO.errorMode === 'heal' ? 'авто-лечение' : 'останов + сигнал';
  const skipNote = skippedCount ? `\nПропущено по чекбоксу: ${skippedCount}` : '';
  const doneNote = alreadyDoneCount ? `\nУже сгенерены (пропустим): ${alreadyDoneCount}` : '';
  const engineNote = isTurbo
    ? (turboShadow
        ? '\nТурбо-движок: Shadow (batch JSON + scene blocking + авто-правка)'
        : '\nТурбо-движок: Параллельный-последовательный (per-chunk compose, без batch JSON / blocking / правки)')
    : '';
  const _confirmMsg =
    `Сегментов: ${AUTO.total}${skipNote}${doneNote}\n` +
    `Режим: ${modeWord}${engineNote}\n` +
    `На ошибке модерации: ${errWord}\n\n` +
    (AUTO.parallel
      ? 'Параллельный режим: все сегменты отправляются в очередь Seedance подряд (~2с между запусками). Текстовый контекст между чанками сохраняется.'
      : sceneGroups.length > 1
        ? 'Внутри каждой сцены чанки идут последовательно (нужно для last-frame / cut-frames continuity). Сцены друг от друга не зависят и идут параллельно (cap = 3 одновременно).'
        : 'Последовательный режим: каждый чанк ждёт предыдущего.');
  if (!await appConfirm({
    title: '▶ Запустить Auto-mode?',
    message: _confirmMsg,
    okText: '▶ Запустить',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) {
    clog('WARN', 'auto.bail', { reason: 'main_confirm_cancelled', total: AUTO.total });
    // Unregister the Turbo-prep run if we registered one upstairs.
    AUTO.active = false;
    _autoUnregisterRun(AUTO);
    _autoUpdateStatusUI();
    return;
  }

  AUTO.active = true;
  _autoUpdateStatusUI();
  clog('INFO', 'auto.start', { total: AUTO.total, parallel: !!AUTO.parallel, scenes: sceneGroups.length });
  showToast(`▶ Auto-mode запущен · ${AUTO.total} сегмент${AUTO.total > 1 ? 'ов' : ''} (${modeWord})`, 4000);

  // CRITICAL: capture episode identity ONCE — every in-flight request must target
  // the episode the user pressed "auto-mode" on. If user navigates to another
  // episode mid-run, S.episode.number changes and pending writes leak into the
  // wrong episode (chunks ended up in the wrong file, episode 2's batch wrote
  // into episode 3 — May 2026 incident).
  const epSid    = S.seriesId;
  const epNumber = S.episode.number;
  // Stash on AUTO so the floating widget knows what episode this run targets.
  AUTO._epSid = epSid;
  AUTO._epNumber = epNumber;
  _autoRegisterRun(AUTO);

  // Read params from sd panel (used for /seedance/start)
  const duration = parseInt(document.getElementById('sd-duration').value) || 15;
  const resolution = document.getElementById('sd-resolution').value;
  const moderation_bypass = document.getElementById('sd-mod-bypass').value;
  const model_tier = document.getElementById('sd-model-tier')?.value || 'reference-fast';
  const POLL_INTERVAL_MS = 8000;
  const PARALLEL_DELAY_MS = 2000;
  const MAX_HEAL_RETRIES = 1;
  const MAX_PARALLEL_SCENES = 3;
  const sharedOpts = { useLastframe, useCutframes, useStyle, styleVal, baseOnly, closeUpOnly,
                       duration, resolution, moderation_bypass, model_tier, POLL_INTERVAL_MS, MAX_HEAL_RETRIES };

  // Compose + start one segment, returns startRes or throws.
  async function _autoComposeStart(seg, scriptOrder) {
    const segCloseUp = sharedOpts.closeUpOnly || !!seg.has_close_up;
    AUTO.lastStatus = segCloseUp ? '⚙ компоную (🎯 close-up)...' : '⚙ компоную...';
    _autoUpdateStatusUI();
    const composeRes = await api.post(
      `/api/series/${epSid}/episodes/${epNumber}/seedance/compose`,
      {
        chunk_text: seg.text,
        use_prev_lastframe: sharedOpts.useLastframe,
        use_prev_cutframes: sharedOpts.useCutframes,
        style: sharedOpts.useStyle ? sharedOpts.styleVal : '',
        base_outfits_only: sharedOpts.baseOnly,
        close_up_only: segCloseUp,
      }
    );
    AUTO.lastStatus = '▶ запускаю генерацию...';
    _autoUpdateStatusUI();
    const startRes = await api.post(
      `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
      {
        prompt: composeRes.prompt,
        chunk_text: seg.text,
        duration: seg.durationSec || sharedOpts.duration, resolution: sharedOpts.resolution,
        moderation_bypass: sharedOpts.moderation_bypass,
        mod_full_battery: true,   // auto-mode: on moderation block run the FULL bypass battery + wait
        model: sharedOpts.model_tier,
        script_order: (scriptOrder != null ? scriptOrder : (typeof seg.scriptOrder === 'number' ? seg.scriptOrder : null)),
        sceneIdx: seg.sceneIdx,
        segIdx: seg.segIdx,
        durationSec: seg.durationSec || sharedOpts.duration,
        refs: (composeRes.refs || []).map(r => ({
          kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
          source: r.source, prev_idx: r.prev_idx, name: r.name,
          cut_index: r.cut_index, cut_time: r.cut_time,
        })),
      }
    );
    return { composeRes, startRes };
  }

  // Poll one chunk until completed/failed, with optional heal+retry.
  // Returns { ok: bool, chunk?, error? }
  // Direct, episode-targeted poll. We must NOT use sdPollOnce() here — that one
  // reads `S.episode.number`, which changes the moment the user navigates to
  // another episode mid-generation. AUTO must always poll the episode it was
  // started on (epSid/epNumber from closure), so generation continues from the
  // background tab even when the user is browsing other parts of the app.
  // sdRenderList() is still called when the target episode IS the visible one,
  // so the chunk cards animate normally.
  async function _autoPollTargetEpisode() {
    try {
      const res = await api.post(
        `/api/series/${epSid}/episodes/${epNumber}/seedance/poll`, {}
      );
      const chunks = res.chunks || [];
      // Only update the on-screen chunk grid if THIS episode is the visible one.
      if (S.seriesId === epSid && S.episode?.number === epNumber) {
        try { _sdNotifyTransitions(chunks); } catch {}
        try { sdRenderList(chunks); } catch {}
      }
      return chunks;
    } catch (e) { return null; }
  }

  async function _autoPollUntilDone(chunkIdx, composeRes, segText, segDuration) {
    let healAttempts = 0;
    let curIdx = chunkIdx;
    while (true) {
      if (AUTO.cancelRequested) return { ok: false, error: 'cancelled' };
      await new Promise(r => setTimeout(r, sharedOpts.POLL_INTERVAL_MS));
      const polled = await _autoPollTargetEpisode();
      const chunk = (polled || []).find(c => c.idx === curIdx);
      if (!chunk) {
        AUTO.lastStatus = `… не вижу чанка #${curIdx}`;
        _autoUpdateStatusUI();
        continue;
      }
      const qcStatus = chunk.qc?.status || null;
      if (chunk.status === 'completed' && qcStatus == null) {
        AUTO.lastStatus = `🔍 #${curIdx} QC...`;
      } else {
        AUTO.lastStatus = chunk.status === 'processing' && chunk.progress != null
          ? `⏳ #${curIdx} ${chunk.progress}%`
          : `⏳ #${curIdx} ${chunk.status}${qcStatus ? ' · QC '+qcStatus : ''}`;
      }
      _autoUpdateStatusUI();
      // QC gate: completed alone isn't enough — wait for qc.status='pass'
      // (or 'retry_exhausted'; that means QC gave up and we accept as-is).
      if (chunk.status === 'completed' && qcStatus === 'pass') {
        return { ok: true, chunk };
      }
      if (chunk.status === 'completed' && qcStatus === 'retry_exhausted') {
        showToast(`⚠ QC сдался на чанке #${curIdx} после ${chunk.qc?.attempts || '?'} попыток (${(chunk.qc?.fails || []).join(', ')}) — принимаем как есть`, 8000);
        return { ok: true, chunk };
      }
      if (chunk.status === 'completed' && qcStatus === 'fail') {
        // Hard cap on retry-storm: if 3+ chunks already exist for this
        // segment text, accept and move on. Belt-and-suspenders for the
        // case where server `attempts` somehow didn't reach the cap.
        const segText2 = (segText || '').trim();
        const dupCount = (polled || []).reduce(
          (n, c) => n + ((c.chunk_text || '').trim() === segText2 ? 1 : 0), 0
        );
        if (dupCount >= 3) {
          showToast(`⚠ Уже ${dupCount} попыток для чанка #${curIdx} (${(chunk.qc?.fails || []).join(', ')}) — продолжаем дальше без ретрая`, 8000);
          return { ok: true, chunk };
        }
        // QC failed and we still have retry budget — trigger fresh start
        // with the same prompt + a server-injected hint about what to fix.
        AUTO.lastStatus = `🔁 #${curIdx} QC retry ${chunk.qc.attempts}/3 (${(chunk.qc.fails || []).slice(0, 2).join(',')})`;
        _autoUpdateStatusUI();
        const retryHint = _qcBuildPromptHint(chunk.qc.fails || []);
        const restart = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
          {
            prompt: (composeRes.prompt || chunk.prompt || '') + retryHint,
            chunk_text: segText,
            duration: segDuration || sharedOpts.duration, resolution: sharedOpts.resolution,
            moderation_bypass: sharedOpts.moderation_bypass,
            mod_full_battery: true,   // auto-mode: full bypass battery + wait
            model: sharedOpts.model_tier,
            refs: (composeRes.refs || []).map(r => ({
              kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
              source: r.source, prev_idx: r.prev_idx, name: r.name,
              cut_index: r.cut_index, cut_time: r.cut_time,
            })),
          }
        );
        if (restart?.chunk?.idx != null) curIdx = restart.chunk.idx;
        await sdRefreshList();
        continue;
      }
      if (chunk.status === 'failed') {
        // Moderation recovery is now owned by the SERVER poll ladder (classify
        // the block → cheapest LEGITIMATE fix → resubmit on the SAME chunk, no
        // evasion, no dialogue/appearance mangling). While it works the chunk
        // sits in 'moderation_blocked' (handled by the keep-polling fall-through
        // below), and it only reaches 'failed' once the ladder is exhausted — so
        // we must NOT re-heal here (that re-ran the old mangling path and spawned
        // duplicate chunks). Just stop and surface the server's message.
        return { ok: false, chunk, error: chunk.error || 'failed' };
      }
      // processing / submitting / pending → keep polling
    }
  }

  // Run one scene's segments sequentially: each waits for the previous video.
  // Increments AUTO.completedCount as it goes. Returns 'completed' | 'failed' | 'cancelled'.
  async function _autoRunSceneChain(sceneSegs) {
    AUTO.activeChains++;
    _autoUpdateStatusUI();
    try {
      for (const seg of sceneSegs) {
        if (AUTO.cancelRequested) return 'cancelled';
        let cs;
        try {
          cs = await _autoComposeStart(seg);
        } catch (e) {
          Sounds.playError();
          showToast(`✗ Compose/start упал на сегменте сц.${seg.sceneIdx + 1}.${seg.segIdx + 1}: ${e.message || e}`, 8000);
          return 'failed';
        }
        await sdRefreshList();
        const chunkIdx = cs.startRes?.chunk?.idx;
        if (chunkIdx == null) {
          showToast(`✗ Не получил chunk_idx от /start`, 6000);
          return 'failed';
        }
        const poll = await _autoPollUntilDone(chunkIdx, cs.composeRes, seg.text, seg.durationSec);
        if (poll.error === 'cancelled') return 'cancelled';
        if (!poll.ok) {
          Sounds.playError();
          const errBit = poll.error ? ` (${(poll.error || '').slice(0, 80)})` : '';
          showToast(`✗ Auto-mode остановлен на сегменте сц.${seg.sceneIdx + 1}.${seg.segIdx + 1}${errBit}`, 10000);
          return 'failed';
        }
        AUTO.completedCount++;
        AUTO.cursor = AUTO.completedCount;   // back-compat for status UI
        _autoUpdateStatusUI();
      }
      return 'completed';
    } finally {
      AUTO.activeChains--;
      _autoUpdateStatusUI();
    }
  }

  // Concurrency-capped runner — caps at MAX_PARALLEL_SCENES workers
  async function _runWithCap(items, cap, asyncFn) {
    const queue = items.slice();
    const results = [];
    async function worker() {
      while (queue.length) {
        if (AUTO.cancelRequested) return;
        const item = queue.shift();
        try { results.push(await asyncFn(item)); }
        catch (e) { results.push({ error: e }); }
      }
    }
    const workers = [];
    for (let i = 0; i < Math.min(cap, items.length); i++) workers.push(worker());
    await Promise.all(workers);
    return results;
  }

  try {
    if (AUTO.parallel) {
      // Linear-parallel — first try BATCH-COMPOSE (single Claude call for all
      // segments with shared episodeBlocking → guarantees consistent character
      // positioning across all chunks). Then fire /start for each pre-built
      // prompt. Falls back to per-chunk compose if batch-compose fails.
      //
      // Engine 'parallel-sequential' (default, experimental) skips batch-compose
      // entirely — `batchPrompts` stays null and every segment falls through to
      // `_autoComposeStart()` (per-chunk Claude compose, same path sequential
      // mode uses). Concurrency cap below still applies, so they all submit in
      // parallel. Easy rollback: flip Settings → «Турбо-движок» back to «Shadow».
      let batchPrompts = null;
      if (turboShadow) {
      AUTO.lastStatus = '🧠 batch-compose (один Claude call на всю серию)...';
      _autoUpdateStatusUI();
      try {
        const batchRes = await api.post(
          `/api/series/${epSid}/episodes/${epNumber}/seedance/batch-compose`,
          {
            segments: AUTO.segments.map(s => ({
              anchor: s.anchor, sceneIdx: s.sceneIdx, segIdx: s.segIdx,
              text: s.text, has_close_up: !!s.has_close_up, durationSec: s.durationSec,
            })),
            base_outfits_only: baseOnly,
            style: useStyle ? styleVal : '',
          },
          { timeoutMs: 900_000 }
        );
        // Pull batch_prompts from server response (count check only — full data on episode)
        const epRes = await api.get(`/api/series/${epSid}/episodes/${epNumber}`);
        batchPrompts = epRes.batch_prompts || {};
        if (batchRes.unresolved_anchors?.length) {
          showToast(`⚠ batch: не все сегменты в ответе (${batchRes.unresolved_anchors.length}). Откатимся на per-chunk compose для них.`, 6000);
        }
        showToast(`🧠 batch готов · ${Object.keys(batchPrompts).length}/${AUTO.segments.length} сегментов`, 4000);

        // ── Early-fire music (turbo mode) ──────────────────────────────────
        // batch-compose just ran: we have sceneIdx + durationSec for every
        // segment. Fire music now — in parallel with the upcoming /start calls.
        // Backend is idempotent: if music already generated it will be skipped.
        const musicEnabled = S.series?.settings?.enable_music !== false;
        if (musicEnabled) {
          // Build scene plan: group AUTO.segments by sceneIdx, sum durations.
          const planMap = new Map();
          for (const s of AUTO.segments) {
            const k = s.sceneIdx ?? 0;
            planMap.set(k, (planMap.get(k) || 0) + (s.durationSec || 15));
          }
          const scenesPlan = [...planMap.entries()].map(([sceneIdx, totalSec]) => ({
            sceneIdx,
            target_duration_ms: Math.round(totalSec * 0.9 * 1000),
          }));
          api.post(
            `/api/series/${epSid}/episodes/${epNumber}/music/generate`,
            { scenes_plan: scenesPlan }
          ).then(r => {
            if (r?.ok) showToast(`🎵 Музыка запущена — ${r.scenes?.length || 0} сцен (параллельно с видео)`, 4000);
          }).catch(() => {});
        }
        // ───────────────────────────────────────────────────────────────────
      } catch (e) {
        showToast(`⚠ batch-compose упал — использую per-chunk: ${e.message || e}`, 6000);
        batchPrompts = null;
      }
      } else {
        // parallel-sequential engine: no batch-compose. batchPrompts stays null;
        // every seg falls through to _autoComposeStart() below.
        AUTO.lastStatus = '⚙ Турбо (параллельно): per-chunk compose в параллель…';
        _autoUpdateStatusUI();
      }

      // Now fire /start for ALL segments SIMULTANEOUSLY (per the reference
      // pipeline: batch-compose pre-built prompts → all chunks queue at once).
      // No 2s delay, no per-chunk Claude call. Just N concurrent Seedance API
      // submissions. Concurrency capped at 5 to play nicely with provider
      // rate-limits (Seedance and AvAIGen tolerate small bursts well).
      const FIRE_CONCURRENCY = 5;
      AUTO.lastStatus = `▶ запускаю ${AUTO.segments.length} чанк(ов) одновременно...`;
      _autoUpdateStatusUI();

      const fireOne = async (seg, i) => {
        if (AUTO.cancelRequested) return;
        const prebuilt = batchPrompts && batchPrompts[seg.anchor];
        try {
          let startRes;
          if (prebuilt && prebuilt.prompt && (prebuilt.refs || []).length) {
            // Per-segment duration: seg.durationSec is the source of truth (computed
            // from line durations). prebuilt.plan.durationSec is just Claude echoing
            // input — and stale batches built before durationSec was passed all say 15.
            const segDur = seg.durationSec || (prebuilt.plan && prebuilt.plan.durationSec) || duration;
            // Use seg.scriptOrder (assigned BEFORE the skip-filter) instead of
            // the local index `i` — `i` is position in the filtered AUTO.segments
            // list and collides with existing chunks when the user skipped some
            // earlier segments. See the `allSegs.forEach((s, i) => { s.scriptOrder = i; })`
            // comment above for the repro.
            startRes = await api.post(
              `/api/series/${epSid}/episodes/${epNumber}/seedance/start`,
              {
                prompt: prebuilt.prompt,
                chunk_text: seg.text,
                duration: segDur, resolution, moderation_bypass,
                mod_full_battery: true,   // auto-mode (turbo): full bypass battery + wait
                script_order: seg.scriptOrder,
                sceneIdx: seg.sceneIdx,
                segIdx: seg.segIdx,
                durationSec: segDur,
                refs: (prebuilt.refs || []).map(r => ({
                  kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null,
                  source: r.source, prev_idx: r.prev_idx, name: r.name,
                })),
              }
            );
          } else {
            // Fallback per-chunk compose if batch missed this anchor.
            // Pass seg.scriptOrder (full-list position) — _autoComposeStart
            // forwards it to /seedance/start as the canonical slot.
            const cs = await _autoComposeStart(seg, seg.scriptOrder);
            startRes = cs.startRes;
          }
          AUTO.completedCount++;
          AUTO.cursor = AUTO.completedCount;
          AUTO.lastStatus = `▶ ${AUTO.completedCount}/${AUTO.total} в очереди (#${startRes?.chunk?.idx ?? '?'})`;
          _autoUpdateStatusUI();
        } catch (e) {
          console.warn(`[auto-mode] start failed for seg ${i}:`, e);
          throw e;
        }
      };

      // Run with concurrency cap so we don't fire 50 at once on huge episodes
      const failures = [];
      await _runWithCap(
        AUTO.segments.map((seg, i) => ({ seg, i })),
        FIRE_CONCURRENCY,
        async ({ seg, i }) => {
          try { await fireOne(seg, i); }
          catch (e) { failures.push({ i, error: e.message || String(e) }); }
        }
      );
      await sdRefreshList();
      sdEnsurePoll();
      if (failures.length) {
        Sounds.playError();
        showToast(`⚠ Auto-mode: ${failures.length} из ${AUTO.segments.length} сегментов не запустились — смотри карточки`, 8000);
      }
    } else {
      // Scene-parallel: each scene's chain is sequential, scenes run in parallel
      // with a concurrency cap. Each scene's first chunk doesn't depend on
      // previous scene's lastframe (different setting), so they're independent.
      await _runWithCap(sceneGroups, MAX_PARALLEL_SCENES, _autoRunSceneChain);
    }

    // All done
    AUTO.active = false;
    AUTO.lastStatus = '';
    _autoUnregisterRun(AUTO);
    if (!AUTO.cancelRequested) {
      // Voice-only announcement on whole-episode completion. Fanfare was
      // removed by request — too startling. The TTS phrase already tells the
      // user it's the BIG finish, not just a single segment.
      const epNum = S.episode?.number;
      Sounds.speak(`Episode ${epNum != null ? epNum + ' ' : ''}generation finished.`);
      const tail = AUTO.parallel
        ? ' (отправлены в очередь — следи за карточками)'
        : '';
      showToast(`✓ Auto-mode завершён · ${AUTO.completedCount}/${AUTO.total} сегмент${AUTO.completedCount === 1 ? '' : AUTO.completedCount < 5 ? 'а' : 'ов'}${tail}`, 6000);

      // Fire-and-forget music generation after a successful single-episode
      // auto-mode run. Same gate as range-gen — series.settings.enable_music.
      // We don't block on it (music takes ~30-90s per scene, separate UI poll
      // handles status). Skipped if cancelled or partial.
      const epForMusic = epNum;
      const musicEnabled = S.series?.settings?.enable_music !== false;
      if (musicEnabled && epForMusic != null && AUTO.completedCount === AUTO.total && AUTO.total > 0) {
        api.post(`/api/series/${S.seriesId}/episodes/${epForMusic}/music/generate`, {})
          .then(r => {
            if (r?.ok) {
              showToast(`🎵 Музыка запущена — ${r.scenes?.length || 0} сцен`, 4000);
              try { _sdEnsureMusicPoll(); _sdRefreshMusic(); } catch {}
            } else if (r?.error) {
              showToast(`🎵 ⚠ ${r.error}`, 5000);
            }
          })
          .catch(e => showToast(`🎵 ⚠ ${e.message || e}`, 5000));
      }
    } else {
      showToast(`⏸ Auto-mode остановлен · обработано ${AUTO.completedCount}/${AUTO.total}`, 5000);
    }
    _autoUpdateStatusUI();
  } catch (e) {
    AUTO.active = false;
    _autoUnregisterRun(AUTO);
    _autoUpdateStatusUI();
    Sounds.playError();
    clog('ERROR', 'auto.crash', { msg: (e?.message || String(e)).slice(0, 400) });
    showToast(`✗ Auto-mode упал: ${e.message || e}`, 8000);
  }
}

