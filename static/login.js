(() => {
  const trigger = document.querySelector('.login-demo-trigger');
  const dialog = document.getElementById('login-demo-dialog');
  const video = document.getElementById('login-demo-video');
  const error = document.getElementById('login-demo-error');
  if (!trigger || !dialog || !video || !error) return;

  const showPlaybackError = () => { error.hidden = false; };

  trigger.addEventListener('click', () => {
    error.hidden = true;
    if (typeof dialog.showModal !== 'function') {
      window.location.assign(video.querySelector('source').src);
      return;
    }
    dialog.showModal();
    // Start from the same user gesture that opened the demo so browsers allow its
    // soundtrack. The native video controls remain available for pause and volume.
    const playback = video.play();
    if (playback && typeof playback.catch === 'function') playback.catch(showPlaybackError);
  });

  dialog.querySelector('.login-demo-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => {
    video.pause();
    try { video.currentTime = 0; } catch (_) { /* metadata may not have loaded */ }
    trigger.focus();
  });
  video.addEventListener('playing', () => { error.hidden = true; });
  video.addEventListener('error', showPlaybackError);
})();
