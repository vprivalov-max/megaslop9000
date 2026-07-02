// ── Init: restore view from URL hash ─────────────────────────────────────────
(function bootRoute() { _navFromHash(); })();
// (hashchange listener already registered next to navigate())

// ── Help video modal ──────────────────────────────────────────────────────────
const HELP_VIDEO_DRIVE_ID = '1RvrO3jaJDNXvgXOusUZxSvg1McoqpUwe';

function openHelpVideo() {
  const modal = document.getElementById('help-video-modal');
  const iframe = document.getElementById('help-video-iframe');
  if (!modal || !iframe) return;
  iframe.src = `https://drive.google.com/file/d/${HELP_VIDEO_DRIVE_ID}/preview`;
  modal.classList.remove('hidden');
}

function closeHelpVideo() {
  const modal = document.getElementById('help-video-modal');
  const iframe = document.getElementById('help-video-iframe');
  if (iframe) iframe.src = '';   // stop playback
  if (modal) modal.classList.add('hidden');
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeHelpVideo();
});
