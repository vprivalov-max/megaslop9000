// ── MLG hitmarker — visual overlay + sound on button clicks ─────────────────
// 4-line cross expanding from click point, fades over 220ms. Pure CSS
// transform animation, no library. Only fires when toggle is on.
(function _initHitmarker() {
  document.addEventListener('click', (e) => {
    if (!Sounds.isHitmarkerEnabled()) return;
    // Catch all clickable controls — buttons, anything with onclick handler,
    // common row-style cards (episode list, character/loc rows, breadcrumb
    // crumb-links). The previous selector was buttons-only and missed
    // navigation rows / breadcrumbs / cards.
    const target = e.target.closest(
      'button, [onclick], [role="button"], a, ' +
      '.btn-primary, .btn-accent, .btn-ghost, .btn-icon, .btn-sm, .btn-danger, .btn-idea-gen, .btn-idea-random, ' +
      '.episode-row, .char-item, .crumb.link, .nav-logo, .idea-card, .sd-card, .outfit-card'
    );
    if (!target || target.disabled) return;
    // Exclude form inputs that bubble up clicks — clicking a checkbox shouldn't fire hitmarker
    const tag = target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'LABEL') return;
    // Big "create / open editor" actions get the gunshot sound; everything
    // else gets the regular hitmarker tink. Visual X overlay always plays.
    const action = target.getAttribute('data-mlg-action');
    if (action === 'gunshot') {
      Sounds.playGunshot();
    } else {
      Sounds.playHitmarker();
    }
    _spawnHitmarkerVisual(e.clientX, e.clientY);
  }, true);
}());

function _spawnHitmarkerVisual(x, y) {
  // PNG sprite — simpler and looks right (CoD/Halo X with rounded ends).
  // CSS centers it on (x, y) via negative margins; pure animation does the rest.
  const wrap = document.createElement('div');
  wrap.className = 'mlg-hitmarker';
  wrap.style.left = x + 'px';
  wrap.style.top = y + 'px';
  document.body.appendChild(wrap);
  setTimeout(() => wrap.remove(), 360);
}

// ── MLG Snoop — random-spawn dancing target ──────────────────────────────
// Pops in at a random screen position every ~25-60s (only when MLG 420 MODE
// is on). Clicking him plays gunshot + hitmarker visual + despawns him.
// Self-removes after ~12s if nobody shoots him.
let _snoopSpawnTimer = null;
function _scheduleSnoop() {
  if (_snoopSpawnTimer) return;
  const tick = () => {
    if (!Sounds.isHitmarkerEnabled()) {
      _snoopSpawnTimer = setTimeout(tick, 5000);  // re-check soon
      return;
    }
    // Don't spawn if one is already on screen. _rollAndSpawn (defined below)
    // rolls the rare-combo dice (1/100 wobo, 1/25 double) and falls through
    // to a regular single spawn otherwise.
    if (!document.querySelector('.mlg-snoop')) {
      if (typeof _rollAndSpawn === 'function') _rollAndSpawn();
      else _spawnSnoop();
    }
    const nextDelay = 25000 + Math.random() * 35000;   // 25-60s
    _snoopSpawnTimer = setTimeout(tick, nextDelay);
  };
  // First spawn 8-15s after page load (only if MLG mode on)
  _snoopSpawnTimer = setTimeout(tick, 8000 + Math.random() * 7000);
}

// Kill-streak counter for MLG targets (currently Snoop, more characters to
// come). Every 3rd kill triggers "Triple!" voice. Stored both locally (for
// instant streak math) AND server-side (per-user, drives the leaderboard).
function _mlgKillCount() {
  return parseInt(localStorage.getItem('mlg_kills') || '0', 10) || 0;
}
function _bumpMlgKill(targetKind = 'snoop') {
  const next = _mlgKillCount() + 1;
  localStorage.setItem('mlg_kills', String(next));
  // Fire-and-forget POST — don't block the gunshot UI on network. Auth is
  // implicit via session cookie. Failure is silent (kill still counts locally).
  try {
    fetch('/api/mlg/kill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: targetKind }),
    }).catch(() => {});
  } catch {}
  return next;
}
// Migrate legacy key (was 'mlg_snoop_kills' before character pool generalized).
(function _migrateKillCounter() {
  const old = localStorage.getItem('mlg_snoop_kills');
  if (old != null && localStorage.getItem('mlg_kills') == null) {
    localStorage.setItem('mlg_kills', old);
  }
})();

// Pretty-print a slugged user dir back into something readable.
// e.g. "v_privalov_gamegears_online" → "v.privalov@gamegears.online"
// Best-effort — slug is irreversible, but for our org pattern it's close.
function _prettifyUserSlug(slug) {
  if (!slug) return '?';
  // Replace last "_" group (domain) with "@"
  // e.g. v_privalov_gamegears_online → v.privalov@gamegears.online
  // Heuristic: find " _gamegears_online" or "_dev" suffix
  const m = slug.match(/^(.+?)_([a-z0-9]+(?:_[a-z]{2,4})?)$/);
  if (m && /^(gamegears|com|org|net|dev|ru|io)/.test(m[2])) {
    return m[1].replace(/_/g, '.') + '@' + m[2].replace(/_/g, '.');
  }
  return slug.replace(/_/g, '.');
}

// Enable MLG mode from inside the leaderboard modal — flips the toggle, plays
// a confirmation gunshot so the user immediately knows it's live, then
// re-renders the leaderboard without the explainer.
function _enableMlgFromLeaderboard() {
  Sounds.setHitmarkerEnabled(true);
  // Sync the Settings checkbox if it's been opened previously
  const cb = document.getElementById('settings-mlg-hitmarker');
  if (cb) cb.checked = true;
  // Confirmation shot
  Sounds.playGunshot();
  // Re-render leaderboard sans explainer
  setTimeout(openMlgLeaderboard, 200);
}

async function openMlgLeaderboard() {
  const list = document.getElementById('mlg-leaderboard-list');
  if (!list) return;
  list.innerHTML = '<div style="color:var(--muted)">⏳ Загружаю...</div>';
  openModal('modal-mlg-leaderboard');

  // If MLG mode is OFF — show explainer + activation CTA above the table.
  // Builds the user's understanding of what they're opting into without
  // forcing them to dig through Settings.
  const mlgOff = !Sounds.isHitmarkerEnabled();
  const explainerHtml = mlgOff ? `
    <div style="background:linear-gradient(135deg, rgba(124,92,252,0.18), rgba(0,212,170,0.12));
                border:1px solid rgba(124,92,252,0.4); border-radius:8px;
                padding:14px 16px; margin-bottom:14px; font-size:0.9rem; line-height:1.55">
      <div style="font-weight:700;font-size:1rem;margin-bottom:6px">🌿 MLG 420 MODE — что это?</div>
      <div style="color:var(--text)">
        Дизайнерский прикол + анти-стресс во время работы. Когда включён:
      </div>
      <ul style="margin:8px 0 8px 18px;padding:0;color:var(--text)">
        <li>На каждый клик по кнопке — хитмаркер «tink!» + крестик-вспышка (как в CoD/Halo)</li>
        <li>На больших действиях («Создать сериал», «+ Эпизод», «🎬 Редактор») — звук выстрела</li>
        <li>Иногда вылазит Snoop Dogg или MLG-лягушка в случайном углу — кликни их и стреляй</li>
        <li>За убийства начисляются киллы. На каждом 10-м — большая радужная надпись OMG!!! / DAMN SON!!</li>
        <li>Случайные голосовые комментарии: WOW / NO SCOPED / DAMN SON / TRIPLE на 3-м килле</li>
        <li>Все киллы попадают в эту таблицу лидеров среди коллег</li>
      </ul>
      <div style="color:var(--muted);font-size:0.84rem;margin-bottom:10px">
        Помогает разрядиться когда сценарий не пишется или генерация падает 5 раз подряд.
        Можно выключить в любой момент в ⚙ Настройках.
      </div>
      <button class="btn-primary" onclick="_enableMlgFromLeaderboard()" style="width:100%">
        🌿 Включить MLG 420 MODE и начать стрелять
      </button>
    </div>
  ` : '';

  try {
    const r = await fetch('/api/mlg/leaderboard').then(r => r.json());
    const rows = r.leaderboard || [];
    if (!rows.length) {
      list.innerHTML = explainerHtml + `
        <div style="text-align:center;color:var(--muted);padding:24px 0">
          <div style="font-size:36px;margin-bottom:8px">🎯</div>
          ${mlgOff
            ? 'В таблице пока пусто. Включи MLG MODE — будешь первым.'
            : "Пока никто не убил ни одного. Стреляй по Snoop'у/лягушке — попадёшь в таблицу первым."}
        </div>`;
      return;
    }
    const me = (window._currentUser && window._currentUser.email) || '';
    const meSlug = me.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
    const medals = ['🥇', '🥈', '🥉'];
    list.innerHTML = explainerHtml + `
      <table style="width:100%;border-collapse:collapse;font-size:0.92rem">
        <thead>
          <tr style="border-bottom:1px solid var(--border);color:var(--muted);text-align:left">
            <th style="padding:6px 8px;width:40px"></th>
            <th style="padding:6px 8px">Игрок</th>
            <th style="padding:6px 8px;text-align:right">Киллы</th>
            <th style="padding:6px 8px;color:var(--muted);font-weight:normal" title="Когда последний кил">когда</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row, i) => {
            const isMe = row.user === meSlug;
            const rank = medals[i] || `#${i + 1}`;
            const when = row.last_kill
              ? new Date(row.last_kill * 1000).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })
              : '—';
            return `
              <tr style="border-bottom:1px solid var(--border);${isMe ? 'background:rgba(124,92,252,0.10);font-weight:600' : ''}">
                <td style="padding:8px;font-size:1.1rem">${rank}</td>
                <td style="padding:8px;${isMe ? 'color:var(--accent)' : ''}">${esc(_prettifyUserSlug(row.user))}${isMe ? ' (это ты)' : ''}</td>
                <td style="padding:8px;text-align:right;font-family:monospace">${row.kills}</td>
                <td style="padding:8px;color:var(--muted);font-size:0.82rem">${when}</td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>
      <div style="margin-top:10px;font-size:0.78rem;color:var(--muted);text-align:center">
        ${rows.length} игрок${rows.length === 1 ? '' : (rows.length < 5 ? 'а' : 'ов')} с убийствами
      </div>`;
  } catch (e) {
    list.innerHTML = `<div style="color:var(--danger)">✗ ${e.message || e}</div>`;
  }
}

// MLG target skins. Each entry is rolled at spawn time. To add another
// character — drop GIF into static/img, append { kind, src, width? } here.
const _MLG_SKINS = [
  { kind: 'snoop', src: '/static/img/snoop.gif', width: 110 },
  { kind: 'frog',  src: '/static/img/frog.gif',  width: 110 },
];

// ── Combo events (rare scheduled multi-spawns) ──────────────────────────────
// Combo events are NOT rolled per-spawn-tick — they live on independent
// schedulers so we can preload their sound files ~20s before the event
// fires (rather than bloating the page-load budget with audio for events
// the user might never see in a session).
//
//   DOUBLE  (every 8-20 min): 2 spawn → kill #2 fires OMG (random of two
//                             sound variants) → 3rd target sneaks in 300ms
//                             after the 2nd kill. Per-kill text overlay
//                             picks a hype line.
//   WOBO    (every 30-90 min): 3 spawn → 2nd kill of wave-1 starts the
//                              wobo-combo loop sound → wave-1 cleared →
//                              2 spawn → wave-2 cleared → 1 final spawn.
//
// State: { type, soundUrl, kills, stage, ... }
const _MLG_COMBOS = {};

// Hype lines flashed during DOUBLE combos. Each kill flashes one at random.
const _OMG_TEXT_LINES = [
  'MY GOD!', 'OH MY GOD!!', 'JESUS!', 'HOLY SHIT!', 'FUCKING HELL!',
  'INSANE!', 'NO WAY!', 'WHAT THE—!', 'GOD DAMN!', 'UNREAL!!',
];
const _OMG_SOUND_URLS = [
  '/static/sounds/omg-fuckn.mp3',
  '/static/sounds/omg-mlg.mp3',
];
const _WOBO_SOUND_URL = '/static/sounds/wobo-combo.mp3';

// Lazy preloader — fetches the URL into HTTP cache + Web Audio buffer, but
// only when called. Used by combo schedulers ~20s before each event.
async function _lazyPreloadSound(url) {
  if (!url) return;
  if (window._mlgBufferCache && window._mlgBufferCache[url]) return; // already loaded
  try { await fetch(url, { credentials: 'same-origin', cache: 'force-cache' }); } catch {}
  // HTMLAudioElement fallback
  if (!window._mlgAudioPreload) window._mlgAudioPreload = {};
  if (!window._mlgAudioPreload[url]) {
    try { const a = new Audio(url); a.preload = 'auto'; a.load(); window._mlgAudioPreload[url] = a; } catch {}
  }
  // AudioBuffer for zero-latency playback
  try {
    if (!window._mlgBufferCache) window._mlgBufferCache = {};
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = window._mlgPreloadCtx || (window._mlgPreloadCtx = new Ctx());
    const res = await fetch(url, { credentials: 'same-origin', cache: 'force-cache' });
    if (!res.ok) return;
    const ab = await res.arrayBuffer();
    const buf = await new Promise((resolve, reject) => {
      try {
        const p = ctx.decodeAudioData(ab, resolve, reject);
        if (p && typeof p.then === 'function') p.then(resolve, reject);
      } catch (e) { reject(e); }
    });
    window._mlgBufferCache[url] = buf;
  } catch {}
}

// Plays a sample BY URL (not by hardcoded path inside Sounds.*) — needed for
// random-variant OMG + the on-demand wobo. Mirrors Sounds._playSample but
// scoped to a URL passed in at runtime. Honours the same MLG-toggle gate.
function _playSampleByUrl(url, vol = 1.0) {
  if (!Sounds.isHitmarkerEnabled()) return;
  // Buffer path
  try {
    const buf = window._mlgBufferCache && window._mlgBufferCache[url];
    if (buf) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = window._mlgPlaybackCtx || (window._mlgPlaybackCtx = new Ctx());
      if (ctx.state === 'suspended') ctx.resume();
      const src = ctx.createBufferSource(); src.buffer = buf;
      const gain = ctx.createGain(); gain.gain.value = vol;
      src.connect(gain); gain.connect(ctx.destination); src.start(0);
      return;
    }
  } catch {}
  // HTMLAudioElement fallback (works without buffer cache).
  try {
    const cached = window._mlgAudioPreload && window._mlgAudioPreload[url];
    const a = cached ? cached.cloneNode() : new Audio(url);
    a.volume = vol;
    a.play().catch(() => {});
  } catch {}
}

// Spawn a combo. soundUrl is the pre-chosen + pre-loaded sound; required
// because the scheduler picks the variant at scheduling time so it can
// preload the right one.
function _spawnCombo(type, soundUrl) {
  const id = `${type}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  if (type === 'double') {
    _MLG_COMBOS[id] = { type, kills: 0, total: 2, followUpQueued: false, soundUrl };
    for (let i = 0; i < 2; i++) {
      setTimeout(() => _spawnSnoop({ comboId: id }), i * 120);
    }
    _spawnMlgVoiceText('DOUBLE SPAWN!');
  } else if (type === 'wobo') {
    _MLG_COMBOS[id] = { type, kills: 0, total: 3, stage: 1, soundStarted: false, soundUrl };
    for (let i = 0; i < 3; i++) {
      setTimeout(() => _spawnSnoop({ comboId: id }), i * 120);
    }
    _spawnMlgVoiceText('★ WOBO COMBO ★');
  }
}

function _onComboKill(combo, comboId) {
  if (!combo) return;
  combo.kills += 1;
  // ── DOUBLE event ───────────────────────────────────────────────────────
  if (combo.type === 'double') {
    // Per-kill hype text overlay — fires after EVERY kill in a DOUBLE combo.
    const line = _OMG_TEXT_LINES[Math.floor(Math.random() * _OMG_TEXT_LINES.length)];
    setTimeout(() => _spawnMlgVoiceText(line), 80);
    if (combo.kills === 2 && !combo.followUpQueued) {
      combo.followUpQueued = true;
      // OMG sound + followup spawn appears suddenly (300ms gap).
      setTimeout(() => _playSampleByUrl(combo.soundUrl, 1.0), 100);
      setTimeout(() => _spawnSnoop({ comboId }), 300);
      combo.total = 3;
    }
    if (combo.kills >= 3) {
      // Combo cleared: drop a chunky finish line overlay, then queue the next.
      const finish = _DOUBLE_FINISH_LINES[Math.floor(Math.random() * _DOUBLE_FINISH_LINES.length)];
      setTimeout(() => _spawnMlgFlash({ big: finish, small: 'DOUBLE COMBO COMPLETE' }), 300);
      delete _MLG_COMBOS[comboId];
      _scheduleNextDoubleCombo();
    }
  }
  // ── WOBO event: 3 → 2 → 1 ──────────────────────────────────────────────
  else if (combo.type === 'wobo') {
    if (combo.stage === 1 && combo.kills === 2 && !combo.soundStarted) {
      combo.soundStarted = true;
      _playSampleByUrl(combo.soundUrl, 1.0);
    }
    if (combo.stage === 1 && combo.kills >= 3) {
      combo.stage = 2; combo.kills = 0; combo.total = 2;
      setTimeout(() => _spawnMlgVoiceText('+2 INCOMING!'), 100);
      for (let i = 0; i < 2; i++) {
        setTimeout(() => _spawnSnoop({ comboId }), 250 + i * 120);
      }
    } else if (combo.stage === 2 && combo.kills >= 2) {
      combo.stage = 3; combo.kills = 0; combo.total = 1;
      setTimeout(() => _spawnMlgVoiceText('FINISH HIM!'), 100);
      setTimeout(() => _spawnSnoop({ comboId }), 300);
    } else if (combo.stage === 3 && combo.kills >= 1) {
      // Crown moment — chunky three-line overlay celebrates clearing the
      // entire 3+2+1 chain. Comes in 250ms after the kill so it doesn't
      // visually collide with the gunshot+hitmarker animation.
      setTimeout(() => _spawnMlgFlash({
        big: '★ WOBO COMBO MEISTER ★',
        small: 'CHAIN CLEARED · 6 IN A ROW',
      }), 250);
      delete _MLG_COMBOS[comboId];
      _scheduleNextWoboCombo();
    }
  }
}

// Overlay headlines used when a DOUBLE combo finishes successfully. Each
// completion picks one at random — variety so the user doesn't see the same
// line every time.
const _DOUBLE_FINISH_LINES = [
  'TRIPLE THREAT', 'GG EZ', 'TOO EASY', 'GET REKT',
  'NO MERCY', 'STAY DOWN', 'OWNED', '3X DOWN',
  'CLEAN SWEEP', 'GODLIKE',
];

// ── Combo schedulers ────────────────────────────────────────────────────────
// Independent timers per combo type. The same flow on each tick:
//   1. Pick the sound variant (DOUBLE has two — pick at schedule time).
//   2. Pick a delay (T) within range.
//   3. Schedule a preload at T-20s (so the file is in cache + AudioBuffer).
//   4. Schedule the actual event firing at T.
// MLG-toggle is checked at firing time; if off, the event is silently
// dropped and rescheduled for the next slot.
let _doubleComboTimer = null, _doublePreloadTimer = null;
let _woboComboTimer   = null, _woboPreloadTimer   = null;

function _scheduleNextDoubleCombo() {
  if (_doubleComboTimer) clearTimeout(_doubleComboTimer);
  if (_doublePreloadTimer) clearTimeout(_doublePreloadTimer);
  // Range: 8-20 minutes between DOUBLE events.
  const delayMs = (8 * 60 * 1000) + Math.random() * (12 * 60 * 1000);
  const soundUrl = _OMG_SOUND_URLS[Math.floor(Math.random() * _OMG_SOUND_URLS.length)];
  // Preload 20s before fire (or right now if delay < 20s, which it never is here).
  const preloadAt = Math.max(0, delayMs - 20000);
  _doublePreloadTimer = setTimeout(() => { _lazyPreloadSound(soundUrl); }, preloadAt);
  const fire = () => {
    if (!Sounds.isHitmarkerEnabled()) { _scheduleNextDoubleCombo(); return; }
    // Don't fire if a target is already on screen — let it clear, retry in 30s.
    if (document.querySelector('.mlg-snoop')) {
      _doubleComboTimer = setTimeout(fire, 30000);
      return;
    }
    _spawnCombo('double', soundUrl);
  };
  _doubleComboTimer = setTimeout(fire, delayMs);
}

function _scheduleNextWoboCombo() {
  if (_woboComboTimer) clearTimeout(_woboComboTimer);
  if (_woboPreloadTimer) clearTimeout(_woboPreloadTimer);
  // Range: 30-90 minutes between WOBO events (rare).
  const delayMs = (30 * 60 * 1000) + Math.random() * (60 * 60 * 1000);
  const preloadAt = Math.max(0, delayMs - 20000);
  _woboPreloadTimer = setTimeout(() => { _lazyPreloadSound(_WOBO_SOUND_URL); }, preloadAt);
  const fire = () => {
    if (!Sounds.isHitmarkerEnabled()) { _scheduleNextWoboCombo(); return; }
    if (document.querySelector('.mlg-snoop')) {
      _woboComboTimer = setTimeout(fire, 60000);
      return;
    }
    _spawnCombo('wobo', _WOBO_SOUND_URL);
  };
  _woboComboTimer = setTimeout(fire, delayMs);
}

// NOTE: combo schedulers boot was moved to _bootMlgFeatureWhenIdle() so
// MLG never starts working until window 'load' + idle callback fired —
// keeps page-load lightweight.

function _spawnSnoop(opts = {}) {
  const comboId = opts.comboId || null;
  // Pick a random skin from the pool — same kill-logic for all of them.
  const skin = _MLG_SKINS[Math.floor(Math.random() * _MLG_SKINS.length)];
  const sn = document.createElement('img');
  sn.src = skin.src;
  sn.className = 'mlg-snoop';
  sn.dataset.kind = skin.kind;
  if (comboId) sn.dataset.comboId = comboId;
  sn.alt = skin.kind;
  sn.style.width = (skin.width || 110) + 'px';
  // Random position — keep him fully on-screen, away from edges
  const W = window.innerWidth, H = window.innerHeight;
  const sw = skin.width || 110;
  const sh = 160;
  sn.style.left = (40 + Math.random() * (W - sw - 80)) + 'px';
  sn.style.top  = (60 + Math.random() * (H - sh - 120)) + 'px';
  sn.addEventListener('click', (e) => {
    e.stopPropagation();
    Sounds.playGunshot();
    _spawnHitmarkerVisual(e.clientX, e.clientY);
    const kills = _bumpMlgKill(skin.kind);

    // Combo-event hook: drives DOUBLE / WOBO follow-up spawns and sounds.
    // Suppresses the regular voice-line lottery so combo announcer voices
    // don't talk over each other.
    let suppressRegularAnnouncer = false;
    if (comboId && _MLG_COMBOS[comboId]) {
      _onComboKill(_MLG_COMBOS[comboId], comboId);
      suppressRegularAnnouncer = true;
    }

    // Kill announcer logic — sound + matching text overlay (skipped during combo):
    let played = false;
    if (!suppressRegularAnnouncer) {
      if (kills === 3) {
        setTimeout(() => { Sounds.playTriple(); _spawnMlgVoiceText('TRIPLE!!!'); }, 180);
        played = true;
      } else if (kills > 3 && kills % 3 === 0 && Math.random() < 0.35) {
        setTimeout(() => { Sounds.playTriple(); _spawnMlgVoiceText('TRIPLE!!!'); }, 180);
        played = true;
      }
      if (!played && Math.random() < 0.32) {
        const voiceLines = [
          { play: Sounds.playWow,      text: 'WOW!!!' },
          { play: Sounds.playDamnSon,  text: 'DAMN SON!!' },
          { play: Sounds.playNoScoped, text: 'NO SCOPED!!' },
        ];
        const pick = voiceLines[Math.floor(Math.random() * voiceLines.length)];
        setTimeout(() => { pick.play(); _spawnMlgVoiceText(pick.text); }, 220);
      }
    }
    // Round-number milestone overlay — every 5 kills (always shown, even mid-combo).
    if (kills > 0 && kills % 5 === 0) {
      _spawnMlgMilestone(kills);
    }
    sn.classList.add('mlg-snoop-shot');
    setTimeout(() => sn.remove(), 280);
  }, { once: false });
  document.body.appendChild(sn);
  // Auto-despawn after 12s if user ignores him. Combo targets get 18s grace
  // because the user might be busy clicking siblings.
  const despawnMs = comboId ? 18000 : 12000;
  setTimeout(() => {
    if (sn.parentNode) {
      sn.classList.add('mlg-snoop-fade');
      setTimeout(() => sn.remove(), 600);
    }
  }, despawnMs);
}

// Per-tick spawn: always a single target. Combo events live on their own
// schedulers (see _scheduleNextDoubleCombo / _scheduleNextWoboCombo) so
// their sound files can be preloaded ~20s before firing rather than at
// page load — keeps initial page weight down.
function _rollAndSpawn() {
  _spawnSnoop();
}

// MLG entire feature is gated behind page-fully-loaded. User reported page
// load was tracking sluggish — pushing all MLG asset preload + spawn
// schedulers to fire ONLY after window 'load' (which waits for all images,
// CSS, fonts, deferred scripts to settle). Inside that handler we further
// defer to requestIdleCallback so we don't compete with the first
// interactive paint for the user. Combined effect: page is responsive
// before any MLG fetch/decode work begins.

let _mlgBootStarted = false;
function _bootMlgFeatureWhenIdle() {
  if (_mlgBootStarted) return;
  _mlgBootStarted = true;
  const start = () => {
    // Order matters: assets first (so when scheduler fires its 12.5-min
    // interval the buffers are ready), then the spawn-tick scheduler, then
    // combo schedulers. _preloadMlgAssets is async — combo schedulers don't
    // need to await it (they preload their own sound at T-20s anyway).
    _preloadMlgAssets().finally(() => {
      _scheduleSnoop();
      _scheduleNextDoubleCombo();
      _scheduleNextWoboCombo();
    });
  };
  // Prefer requestIdleCallback so we yield to the browser's first paint /
  // post-load layout work. Fallback to a generous setTimeout in browsers
  // that don't have it (Safari < 17 etc).
  if (typeof window.requestIdleCallback === 'function') {
    window.requestIdleCallback(start, { timeout: 4000 });
  } else {
    setTimeout(start, 1500);
  }
}
if (document.readyState === 'complete') {
  // Page already loaded by the time this script ran (rare with our defer
  // pattern, but possible with cached pages / back-button restore).
  _bootMlgFeatureWhenIdle();
} else {
  // 'load' fires after every <img>, <link>, deferred script settles —
  // strictly later than DOMContentLoaded.
  window.addEventListener('load', _bootMlgFeatureWhenIdle, { once: true });
}

// Preload ALL MLG assets up-front so first-play has zero latency.
// Strategy: Web Audio AudioBuffer (fully decoded, plays instantly) +
// HTMLAudioElement fallback for samples still decoding. Without buffers,
// HTMLAudioElement.play() has 100-500ms first-call latency even with
// preload='auto' because browsers defer actual buffer fill until play().
const _MLG_AUDIO_URLS = [
  '/static/sounds/hitmarker.mp3',
  '/static/sounds/gunshot.mp3',
  '/static/sounds/triple.mp3',
  '/static/sounds/wow.mp3',
  '/static/sounds/damnson.mp3',
  '/static/sounds/noscoped.mp3',
  '/static/sounds/wait-a-minute.mp3',
];
const _MLG_IMAGE_URLS = [
  '/static/img/hitmarker.png',
  '/static/img/snoop.gif',
  '/static/img/frog.gif',
];

async function _preloadMlgAssets() {
  // Phase 1: HTTP cache warmup — fetch into browser cache so subsequent
  // requests are instant. Doesn't require user-gesture or audio context.
  for (const url of _MLG_AUDIO_URLS.concat(_MLG_IMAGE_URLS)) {
    try { fetch(url, { credentials: 'same-origin', cache: 'force-cache' }).catch(() => {}); }
    catch {}
  }
  // Phase 2: HTMLAudioElement fallback cache (for the ~50ms window before
  // AudioBuffers finish decoding).
  if (!window._mlgAudioPreload) window._mlgAudioPreload = {};
  for (const url of _MLG_AUDIO_URLS) {
    try {
      const a = new Audio(url);
      a.preload = 'auto';
      a.load();
      window._mlgAudioPreload[url] = a;
    } catch {}
  }
  // Phase 3: image prefetch + decode
  for (const url of _MLG_IMAGE_URLS) {
    try {
      const img = new Image();
      img.decoding = 'async';
      img.src = url;
    } catch {}
  }
  // Phase 4: decode each MP3 into an AudioBuffer for true zero-latency
  // playback. AudioContext can be created without user-gesture (it'll be
  // 'suspended' until first play, but decodeAudioData() works regardless).
  if (!window._mlgBufferCache) window._mlgBufferCache = {};
  let ctx = null;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) ctx = new Ctx();
  } catch {}
  if (!ctx) return;
  // Decode in parallel — fetch + arrayBuffer + decodeAudioData are all async.
  // Use force-cache so we re-use Phase 1's downloads.
  await Promise.all(_MLG_AUDIO_URLS.map(async (url) => {
    try {
      const res = await fetch(url, { credentials: 'same-origin', cache: 'force-cache' });
      if (!res.ok) return;
      const ab = await res.arrayBuffer();
      // Safari needs the callback signature; modern browsers accept Promise.
      const buf = await new Promise((resolve, reject) => {
        try {
          const p = ctx.decodeAudioData(ab, resolve, reject);
          if (p && typeof p.then === 'function') p.then(resolve, reject);
        } catch (e) { reject(e); }
      });
      window._mlgBufferCache[url] = buf;
    } catch (e) { /* fallback path will handle it */ }
  }));
}
// NOTE: _preloadMlgAssets is now invoked from _bootMlgFeatureWhenIdle()
// (which fires on window 'load' + requestIdleCallback). Direct firing here
// was moved out so MLG asset bytes don't compete with first-paint resources.

// Big rainbow MLG text overlay — used for both round-number milestones
// and voice-line reactions. `opts.big` is the top giant line, `opts.small`
// (optional) is the secondary line. Random tilt + shake + 2.4s lifetime.
const _MLG_PHRASES = [
  'OMG!!!', 'DAMN SON!!', 'NO SCOPED!!', 'WOW!!!',
  '420 BLAZE IT', 'GET REKT', 'SAVAGE!!', 'OWNED!!',
  'INSANE!!!', 'RAMPAGE!', 'UNREAL!!', 'FROGGED!!',
  'YOU MAD?', 'GG EZ', 'GODLIKE', 'MLG PRO',
];
function _spawnMlgFlash(opts) {
  const wrap = document.createElement('div');
  wrap.className = 'mlg-milestone';
  // The big line is the headline; small (when present) sits below.
  const bigHtml   = `<div class="mlg-milestone-num">${opts.big}</div>`;
  const smallHtml = opts.small ? `<div class="mlg-milestone-phrase">${opts.small}</div>` : '';
  wrap.innerHTML = bigHtml + smallHtml;
  const tilt = (Math.random() * 12 - 6).toFixed(1);
  wrap.style.setProperty('--mlg-tilt', `${tilt}deg`);
  document.body.appendChild(wrap);
  // Spawn a rainbow vignette around the screen edges for the same lifetime
  // as the popup. Multiple stacked popups → one shared vignette (re-uses the
  // existing DOM node and bumps its expiry timestamp). CSS handles the
  // rainbow hue-rotation animation on the inset box-shadow.
  _spawnMlgVignette(2400);
  setTimeout(() => wrap.remove(), 2400);
}

let _mlgVignetteTimer = null;
function _spawnMlgVignette(durationMs) {
  let v = document.getElementById('mlg-vignette');
  if (!v) {
    v = document.createElement('div');
    v.id = 'mlg-vignette';
    v.className = 'mlg-vignette';
    document.body.appendChild(v);
  }
  // Restart the animation by force-reflowing (so new popups extend the show).
  v.classList.remove('on');
  // eslint-disable-next-line no-unused-expressions
  v.offsetWidth;  // reflow
  v.classList.add('on');
  if (_mlgVignetteTimer) clearTimeout(_mlgVignetteTimer);
  _mlgVignetteTimer = setTimeout(() => {
    v.classList.remove('on');
    _mlgVignetteTimer = null;
  }, durationMs);
}
// Round-number kill milestone — shown every 5 kills.
function _spawnMlgMilestone(killCount) {
  const phrase = _MLG_PHRASES[Math.floor(Math.random() * _MLG_PHRASES.length)];
  _spawnMlgFlash({ big: `${killCount} KILLS`, small: phrase });
}
// Voice-line text overlay — shown together with the matching audio so the
// phrase you hear is also the phrase you see flashing center-screen.
function _spawnMlgVoiceText(label) {
  _spawnMlgFlash({ big: label });
}

