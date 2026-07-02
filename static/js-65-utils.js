// ── Prompt modal ──────────────────────────────────────────────────────────────
function showPromptModal(prompt) {
  document.getElementById('prompt-text').value = prompt;
  openModal('modal-prompt');
}

async function translateField(fieldId, label) {
  const text = (document.getElementById(fieldId)?.value || '').trim();
  if (!text) { showToast('Нет текста для перевода'); return; }
  document.getElementById('translate-modal-title').textContent = `Перевод — ${label}`;
  const contentEl = document.getElementById('translate-content');
  contentEl.textContent = '⏳ Переводим...';
  openModal('modal-translate');
  try {
    const res = await api.post('/api/translate', { text });
    contentEl.textContent = res.translation;
  } catch(e) {
    contentEl.textContent = 'Ошибка: ' + e.message;
  }
}

function copyTranslation() {
  const text = document.getElementById('translate-content')?.textContent || '';
  navigator.clipboard.writeText(text).then(() => showToast('Скопировано!'));
}

function copyPrompt() {
  const ta = document.getElementById('prompt-text');
  ta.select();
  navigator.clipboard.writeText(ta.value).then(() => showToast('Скопировано!')).catch(() => {
    document.execCommand('copy');
    showToast('Скопировано!');
  });
}

// ── Settings ──────────────────────────────────────────────────────────────────
// Instant-apply for the sounds toggle so user can test it from modal-settings
// without having to hit "Save" (uncovered API keys still need Save).
function onSoundsToggleChanged(on) {
  Sounds.setEnabled(!!on);
  // Tiny audible confirmation that toggle works (only when turning ON)
  if (on) {
    try { Sounds.playSuccess(); } catch (e) {}
  }
}

async function openSettings() {
  // Server returns masked status — never the raw key. We show "•••••abcd" as
  // placeholder; user types a fresh key only when rotating. Empty input on
  // save = no change (we only POST non-empty fields).
  const cfg = await api.get('/api/config');
  const rtlInp = document.getElementById('settings-rtl-key');
  const avaiInp = document.getElementById('settings-avai-key');
  if (rtlInp) {
    rtlInp.value = '';
    rtlInp.placeholder = cfg.reteller_key_masked
      ? `Текущий: ${cfg.reteller_key_masked} (оставь пустым чтобы не менять)`
      : (cfg.is_primary && cfg.has_reteller_key ? 'Используется глобальный env-ключ' : 'rtl_sk_...');
  }
  if (avaiInp) {
    avaiInp.value = '';
    avaiInp.placeholder = cfg.avai_key_masked
      ? `Текущий: ${cfg.avai_key_masked} (оставь пустым чтобы не менять)`
      : (cfg.is_primary && cfg.has_avai_key ? 'Используется глобальный env-ключ' : 'avai-...');
  }
  // Anthropic key стайс global — поле спрятано/не нужно (operator-only)
  const soundsCb = document.getElementById('settings-sounds-enabled');
  if (soundsCb) soundsCb.checked = Sounds.isEnabled();
  const voiceCb = document.getElementById('settings-voice-enabled');
  if (voiceCb) voiceCb.checked = Sounds.isVoiceEnabled();
  const mlgCb = document.getElementById('settings-mlg-hitmarker');
  if (mlgCb) mlgCb.checked = Sounds.isHitmarkerEnabled();
  // Auto-pipeline toggles (default ON).
  const apA = document.getElementById('settings-auto-pipe-adapt');
  const apL = document.getElementById('settings-auto-pipe-logic');
  const apD = document.getElementById('settings-auto-pipe-add');
  if (apA) apA.checked = autoPipeEnabled('adapt');
  if (apL) apL.checked = autoPipeEnabled('logic');
  if (apD) apD.checked = autoPipeEnabled('add');
  // Turbo-engine radio — sync with current localStorage choice.
  const teCur = _turboEngine();
  const teP = document.getElementById('turbo-engine-parallel');
  const teS = document.getElementById('turbo-engine-shadow');
  if (teP) teP.checked = (teCur === 'parallel-sequential');
  if (teS) teS.checked = (teCur === 'shadow');
  // Auto-revise (Turbo-mode automatic edit) — fetch per-user setting.
  try {
    const ar = await api.get('/api/user/auto-revise');
    AUTO_REVISE_DEFAULT_TEXT = ar.default || AUTO_REVISE_DEFAULT_TEXT;
    const enCb = document.getElementById('settings-auto-revise-enabled');
    const txt  = document.getElementById('settings-auto-revise-instruction');
    if (enCb) enCb.checked = !!ar.auto_revise_enabled;
    if (txt)  txt.value    = ar.auto_revise_instruction || ar.default || '';
  } catch (_) { /* non-blocking */ }
  openModal('modal-settings');
}

// Captured from /api/user/auto-revise on first openSettings() so the "Вернуть
// дефолт" button has the server-side default text without an extra round-trip.
let AUTO_REVISE_DEFAULT_TEXT = '';
function resetAutoReviseDefault() {
  const txt = document.getElementById('settings-auto-revise-instruction');
  if (!txt) return;
  if (AUTO_REVISE_DEFAULT_TEXT) {
    txt.value = AUTO_REVISE_DEFAULT_TEXT;
  } else {
    // Fallback: ask the server.
    api.get('/api/user/auto-revise').then(ar => {
      AUTO_REVISE_DEFAULT_TEXT = ar.default || '';
      if (AUTO_REVISE_DEFAULT_TEXT) txt.value = AUTO_REVISE_DEFAULT_TEXT;
    }).catch(()=>{});
  }
}

async function saveSettings() {
  const payload = {};
  const rtlVal = val('settings-rtl-key').trim();
  const avaiVal = (val('settings-avai-key') || '').trim();
  if (rtlVal) payload.reteller_key = rtlVal;
  if (avaiVal) payload.avai_key = avaiVal;
  if (Object.keys(payload).length) {
    await api.post('/api/config', payload);
  }
  // Turbo-engine — persist locally (no server roundtrip).
  const teS = document.getElementById('turbo-engine-shadow');
  const teP = document.getElementById('turbo-engine-parallel');
  if (teS && teS.checked) _setTurboEngine('shadow');
  else if (teP && teP.checked) _setTurboEngine('parallel-sequential');
  // Auto-revise (Turbo-mode automatic edit) — persist server-side.
  try {
    const enCb = document.getElementById('settings-auto-revise-enabled');
    const txt  = document.getElementById('settings-auto-revise-instruction');
    if (enCb || txt) {
      await api.post('/api/user/auto-revise', {
        auto_revise_enabled: enCb ? !!enCb.checked : true,
        auto_revise_instruction: (txt?.value || '').trim(),
      });
    }
  } catch (e) {
    showToast('⚠ Не удалось сохранить «Автоматическую правку»: ' + (e.message || e), 4000);
  }
  // Local-only settings (no server roundtrip needed)
  const soundsCb = document.getElementById('settings-sounds-enabled');
  if (soundsCb) Sounds.setEnabled(!!soundsCb.checked);
  const voiceCb = document.getElementById('settings-voice-enabled');
  if (voiceCb) Sounds.setVoiceEnabled(!!voiceCb.checked);
  const mlgCb = document.getElementById('settings-mlg-hitmarker');
  if (mlgCb) Sounds.setHitmarkerEnabled(!!mlgCb.checked);
  // Auto-pipeline toggles.
  const apA = document.getElementById('settings-auto-pipe-adapt');
  const apL = document.getElementById('settings-auto-pipe-logic');
  const apD = document.getElementById('settings-auto-pipe-add');
  if (apA) setAutoPipeEnabled('adapt', !!apA.checked);
  if (apL) setAutoPipeEnabled('logic', !!apL.checked);
  if (apD) setAutoPipeEnabled('add', !!apD.checked);
  closeModal('modal-settings');
  loadBalance();
}

async function loadBalance() {
  try {
    const data = await api.get('/api/reteller/balance');
    const el = document.getElementById('balance-badge');
    if (data.tokens !== undefined) el.textContent = `${data.tokens} токенов`;
    else el.textContent = '';
  } catch (_) {}
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
function closeModal(id) { document.getElementById(id).classList.add('hidden'); }

// Close modal on backdrop click — skip modals marked no-backdrop-close
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal') && !e.target.classList.contains('no-backdrop-close'))
    closeModal(e.target.id);
});

// ── DOM helpers ───────────────────────────────────────────────────────────────
function val(id) { return (document.getElementById(id)?.value || '').trim(); }
function setVal(id, v) { const el = document.getElementById(id); if (el) el.value = v ?? ''; }
function clearFields(ids) { ids.forEach(id => setVal(id, '')); }
function esc(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function showToast(msg) {
  const t = document.createElement('div');
  t.textContent = msg;
  Object.assign(t.style, {
    position:'fixed', bottom:'24px', right:'24px', background:'var(--surface3)',
    border:'1px solid var(--border)', color:'var(--text)', padding:'10px 20px',
    borderRadius:'8px', zIndex:'999', fontSize:'0.9rem', boxShadow:'var(--shadow)',
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2000);
}

// ── Long-running operation banner ──────────────────────────────────────────
// Sticky top-of-page banner that stays visible during Claude's 30-180s work.
// Solves real complaint 2026-05-25: "нажал полечить — никакого процесса не
// видно". Button spinner was the only feedback; if user scrolled away, they
// saw nothing happening for a minute and thought it broke.
//
// Usage:
//   const banner = LongOpBanner.show('🩹 Лечим логику', ['Читаем сценарий…', 'Анализируем противоречия…', 'Переписываем диалоги…']);
//   try { /* await long op */ } finally { banner.close(); }
const LongOpBanner = (() => {
  let _el = null;
  let _timerInt = null;
  let _stageInt = null;
  let _startedAt = 0;

  function show(title, stages) {
    close();   // never stack
    _startedAt = Date.now();
    const stageList = (stages && stages.length) ? stages : ['Работаем…'];
    _el = document.createElement('div');
    Object.assign(_el.style, {
      position:'fixed', top:'0', left:'0', right:'0', zIndex:'9999',
      background:'linear-gradient(90deg, rgba(16,185,129,0.95), rgba(59,130,246,0.95))',
      color:'#fff', padding:'10px 16px', boxShadow:'0 2px 12px rgba(0,0,0,0.4)',
      fontSize:'0.92rem', display:'flex', alignItems:'center', gap:'14px',
      fontWeight:'600',
    });
    _el.innerHTML = `
      <span style="font-size:1.1rem">⏳</span>
      <span style="flex:0 0 auto">${title}</span>
      <span data-role="stage" style="flex:1;opacity:0.92;font-weight:400">${stageList[0]}</span>
      <span data-role="elapsed" style="flex:0 0 auto;font-family:ui-monospace,monospace;background:rgba(0,0,0,0.25);padding:3px 8px;border-radius:4px">0:00</span>
    `;
    document.body.appendChild(_el);
    // Elapsed-time counter — updates every second so user sees process IS
    // running (not frozen).
    _timerInt = setInterval(() => {
      if (!_el) return;
      const sec = Math.floor((Date.now() - _startedAt) / 1000);
      const m = Math.floor(sec / 60), s = sec % 60;
      const elapsedEl = _el.querySelector('[data-role="elapsed"]');
      if (elapsedEl) elapsedEl.textContent = `${m}:${s.toString().padStart(2, '0')}`;
    }, 1000);
    // Rotating stage labels — every 8 seconds bumps to next stage so the
    // user feels narrative progress even when backend is still on one call.
    let stageIdx = 0;
    _stageInt = setInterval(() => {
      if (!_el) return;
      stageIdx = Math.min(stageIdx + 1, stageList.length - 1);
      const stageEl = _el.querySelector('[data-role="stage"]');
      if (stageEl) stageEl.textContent = stageList[stageIdx];
    }, 8000);
    return { close };
  }
  function close() {
    if (_timerInt) { clearInterval(_timerInt); _timerInt = null; }
    if (_stageInt) { clearInterval(_stageInt); _stageInt = null; }
    if (_el) { _el.remove(); _el = null; }
  }
  return { show, close };
})();

// ── Sound effects ───────────────────────────────────────────────────────────
// Synthesized via Web Audio API — no external assets to bundle/serve.
// User toggle stored in localStorage as 'sounds_enabled' (default ON).
const Sounds = (() => {
  let ctx = null;
  function _ensureCtx() {
    if (ctx) return ctx;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    ctx = new Ctx();
    return ctx;
  }
  function isEnabled() {
    // Default ON: only OFF when user explicitly stored 'false'
    return localStorage.getItem('sounds_enabled') !== 'false';
  }
  function setEnabled(on) {
    localStorage.setItem('sounds_enabled', on ? 'true' : 'false');
  }
  // Schedule a tone at offset `t0`, freq, duration (sec), gain.
  // Sine + small attack/release envelope so it doesn't click.
  function _tone(t0, freq, dur, gain = 0.18) {
    const c = _ensureCtx();
    if (!c) return;
    const osc = c.createOscillator();
    const g = c.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, t0);
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(gain, t0 + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g).connect(c.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.05);
  }
  // Pleasant ascending major-third — used on completion of a generation
  function playSuccess() {
    if (!isEnabled()) return;
    const c = _ensureCtx();
    if (!c) return;
    if (c.state === 'suspended') c.resume().catch(() => {});
    const t = c.currentTime;
    _tone(t,        523.25, 0.18, 0.20); // C5
    _tone(t + 0.10, 659.25, 0.18, 0.20); // E5
    _tone(t + 0.20, 783.99, 0.32, 0.22); // G5
  }
  // Lower descending dissonant pair — used on generation failure
  function playError() {
    if (!isEnabled()) return;
    const c = _ensureCtx();
    if (!c) return;
    if (c.state === 'suspended') c.resume().catch(() => {});
    const t = c.currentTime;
    _tone(t,        311.13, 0.22, 0.22); // Eb4
    _tone(t + 0.14, 233.08, 0.40, 0.22); // Bb3
  }
  // Distinct 6-note fanfare for whole-episode completion (auto-mode all-done).
  // Differentiates from per-chunk playSuccess so user knows the BIG finish vs
  // a single segment finishing.
  function playFanfare() {
    if (!isEnabled()) return;
    const c = _ensureCtx();
    if (!c) return;
    if (c.state === 'suspended') c.resume().catch(() => {});
    const t = c.currentTime;
    _tone(t,        523.25, 0.14, 0.20); // C5
    _tone(t + 0.08, 659.25, 0.14, 0.20); // E5
    _tone(t + 0.16, 783.99, 0.14, 0.20); // G5
    _tone(t + 0.24, 1046.5, 0.14, 0.22); // C6
    _tone(t + 0.40, 783.99, 0.14, 0.20); // G5
    _tone(t + 0.48, 1046.5, 0.55, 0.26); // C6 — long
  }
  // Speak a phrase via the browser's SpeechSynthesis API (free, offline,
  // works in Chrome/Safari/Firefox). Voice is picked by language; falls
  // back to default if no English voice available. Independent of the
  // chime-sounds toggle — has its own setting.
  function isVoiceEnabled() {
    return localStorage.getItem('voice_announce_enabled') !== 'false';
  }
  function setVoiceEnabled(on) {
    localStorage.setItem('voice_announce_enabled', on ? 'true' : 'false');
  }
  function speak(text, opts = {}) {
    if (!isVoiceEnabled()) return;
    if (!('speechSynthesis' in window)) return;
    try {
      // Cancel any pending/in-flight utterance by default. Browser's
      // SpeechSynthesis.speak() queues utterances, so calling speak() three
      // times in a row reads all three back-to-back. For status alerts we
      // want the LATEST event to interrupt the previous one. Opt-out with
      // `opts.queue=true` if a flow legitimately needs sequential reading.
      if (!opts.queue) {
        try { window.speechSynthesis.cancel(); } catch (_) {}
      }
      const u = new SpeechSynthesisUtterance(text);
      u.lang = opts.lang || 'en-US';
      u.rate = opts.rate || 1.0;
      u.pitch = opts.pitch || 1.0;
      u.volume = opts.volume != null ? opts.volume : 0.9;
      // Pick an English voice when available
      const voices = window.speechSynthesis.getVoices();
      const en = voices.find(v => /^en[-_]/i.test(v.lang)) || voices[0];
      if (en) u.voice = en;
      window.speechSynthesis.speak(u);
    } catch (e) { /* ignore */ }
  }
  // MLG hitmarker — real CoD/Halo "tink!" sample. Pre-loaded on first call
  // and reused for every click. Cloned per-play so rapid clicks overlap
  // (HTMLAudio can only play one stream at a time per element).
  // Audio sample loader — uses Web Audio API AudioBuffer when pre-decoded
  // (zero-latency, fully in-memory), falls back to HTMLAudioElement clone
  // for samples that haven't finished decoding yet. The preload phase
  // (_preloadMlgAssets) decodes all known MLG sounds into AudioBuffers up-front,
  // so by the first user click everything plays instantly.
  const _audioElCache = {};
  function _playSample(path, volume = 0.7) {
    // Hot path: AudioBuffer is decoded and ready in window._mlgBufferCache
    const buf = window._mlgBufferCache && window._mlgBufferCache[path];
    if (buf) {
      const c = _ensureCtx();
      if (!c) return;
      if (c.state === 'suspended') c.resume().catch(() => {});
      try {
        const src = c.createBufferSource();
        const g = c.createGain();
        src.buffer = buf;
        g.gain.value = volume;
        src.connect(g).connect(c.destination);
        src.start(0);
      } catch {}
      return;
    }
    // Fallback: HTMLAudioElement (used until AudioBuffer finishes decoding)
    if (!_audioElCache[path]) {
      const preloaded = window._mlgAudioPreload && window._mlgAudioPreload[path];
      if (preloaded) {
        _audioElCache[path] = preloaded;
      } else {
        try {
          _audioElCache[path] = new Audio(path);
          _audioElCache[path].preload = 'auto';
          _audioElCache[path].load();
        } catch { return; }
      }
    }
    try {
      const clone = _audioElCache[path].cloneNode(true);
      clone.volume = volume;
      clone.play().catch(() => {});
    } catch {}
  }
  function playHitmarker() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/hitmarker.mp3', 0.7);
  }
  function playGunshot() {
    if (!isHitmarkerEnabled()) return;   // gated by same MLG toggle
    _playSample('/static/sounds/gunshot.mp3', 0.85);
  }
  // "Oh baby a triple!" — plays on every 3rd Snoop kill. Louder than the
  // others because the original sample is mixed quiet.
  function playTriple() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/triple.mp3', 1.0);
  }
  // "WOW" Owen Wilson — random chance on Snoop kills (when not a triple).
  function playWow() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/wow.mp3', 0.95);
  }
  // "DAMN SON! WHERE'D YOU FIND THIS!" — alternate random reaction.
  function playDamnSon() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/damnson.mp3', 0.95);
  }
  // "GET NO-SCOPED!" — alternate random reaction.
  function playNoScoped() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/noscoped.mp3', 0.95);
  }
  // "OH MY GOD" — plays on the 2nd kill of a DOUBLE event (1/25 spawn).
  // File path is the standard /static/sounds/omg.mp3 — drop the asset there
  // when ready. Gracefully no-ops if the file 404s (Sounds._playSample's
  // try/catch handles that already).
  function playOmg() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/omg.mp3', 1.0);
  }
  // "WOBO COMBO" — plays starting on the 2nd kill of a WOBO event (1/100).
  // Loops through the rest of the chain (5 → 2 → 1 spawns) as ambience.
  function playWoboCombo() {
    if (!isHitmarkerEnabled()) return;
    _playSample('/static/sounds/wobo-combo.mp3', 1.0);
  }
  function isHitmarkerEnabled() {
    return localStorage.getItem('mlg_hitmarker_enabled') === '1';
  }
  function setHitmarkerEnabled(on) {
    localStorage.setItem('mlg_hitmarker_enabled', on ? '1' : '0');
  }
  return { playSuccess, playError, playFanfare, speak,
           isEnabled, setEnabled, isVoiceEnabled, setVoiceEnabled,
           playHitmarker, playGunshot, playTriple, playWow, playDamnSon, playNoScoped,
           playOmg, playWoboCombo,
           isHitmarkerEnabled, setHitmarkerEnabled };
})();

