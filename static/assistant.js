/*
 * Assistant extras for AlexaPlus:
 *   1. Mini music player (YouTube IFrame API) controlled by chat/voice: "play Kesariya", "stop the music"
 *   2. Action handler for things the server asks the browser to do (open a site, control music)
 *   3. Reminder alarms: polls /api/reminders/due and rings + speaks when a reminder is due
 *
 * Reminders ring only while this page is open in a browser tab.
 */
(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const notify = (m) => (typeof toast === 'function' ? toast(m) : console.log(m));

  // ---------------------------------------------------------------- music
  const box = $('#musicPlayer');
  const titleEl = $('#mpTitle');
  const toggleBtn = $('#mpToggle');
  const stopBtn = $('#mpStop');
  const openLink = $('#mpOpen');
  let player = null;
  let apiPromise = null;
  let currentId = '';
  let playing = false;
  let userVolume = 100;

  function loadApi() {
    if (window.YT && window.YT.Player) return Promise.resolve(true);
    if (apiPromise) return apiPromise;
    apiPromise = new Promise((resolve) => {
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => { if (prev) prev(); resolve(true); };
      const tag = document.createElement('script');
      tag.src = 'https://www.youtube.com/iframe_api';
      tag.onerror = () => { apiPromise = null; resolve(false); };
      document.head.appendChild(tag);
      setTimeout(() => resolve(!!(window.YT && window.YT.Player)), 8000);
    });
    return apiPromise;
  }

  const effectiveVolume = () => (window.voiceModeActive ? Math.min(userVolume, 30) : userVolume);
  function applyVolume() { try { if (player && player.setVolume) player.setVolume(effectiveVolume()); } catch (e) { /* ignore */ } }
  function refreshButtons() { if (toggleBtn) toggleBtn.textContent = playing ? '⏸' : '▶'; }

  async function openOnPc(url) {
    try {
      const r = await fetch('/api/open-external', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
      const d = await r.json();
      if (d.ok) return true;
    } catch (e) { /* fall through */ }
    const w = window.open(url, '_blank', 'noopener');
    return !!w;
  }

  function linkMessage(text, url) {
    if (typeof addMessage !== 'function') { notify(text); return; }
    const row = addMessage('assistant', text + ' ', false);
    const a = document.createElement('a');
    a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = 'Click here to open';
    const bubble = row.querySelector('.bubble');
    if (bubble) bubble.appendChild(a);
  }

  async function fallbackToYoutubeSite(id) {
    const url = 'https://www.youtube.com/watch?v=' + id;
    stopMusic(true);
    const ok = await openOnPc(url);
    if (!ok) linkMessage('Your browser blocked the automatic open.', url);
    else notify('That video cannot be embedded, so I opened it on YouTube.');
  }

  async function playYoutube(a) {
    if (!box) { await openOnPc('https://www.youtube.com/watch?v=' + a.video_id); return; }
    currentId = a.video_id;
    box.classList.remove('hidden');
    titleEl.textContent = a.title || 'Playing…';
    openLink.href = 'https://www.youtube.com/watch?v=' + a.video_id;
    const apiOk = await loadApi();
    if (!apiOk || !(window.YT && window.YT.Player)) { await fallbackToYoutubeSite(a.video_id); return; }
    if (player && player.loadVideoById) {
      player.loadVideoById(a.video_id);
      applyVolume();
    } else {
      player = new window.YT.Player('ytHost', {
        width: '300', height: '200', videoId: a.video_id,
        playerVars: { autoplay: 1, playsinline: 1, rel: 0, modestbranding: 1 },
        events: {
          onReady: (e) => { applyVolume(); e.target.playVideo(); },
          onStateChange: (e) => { playing = e.data === 1; refreshButtons(); },
          onError: () => { fallbackToYoutubeSite(currentId); },
        },
      });
    }
    // Browsers sometimes block autoplay until the page has been clicked once.
    setTimeout(() => {
      try {
        const st = player && player.getPlayerState ? player.getPlayerState() : -2;
        if (currentId === a.video_id && st !== 1 && st !== 3 && st !== 2) notify('Press ▶ on the mini player to start the music (your browser blocked autoplay).');
      } catch (e) { /* ignore */ }
    }, 6000);
  }

  function pauseMusic() { try { player && player.pauseVideo(); } catch (e) { /* ignore */ } }
  function resumeMusic() { try { player && player.playVideo(); } catch (e) { /* ignore */ } }
  function stopMusic(hide = true) {
    try { player && player.stopVideo(); } catch (e) { /* ignore */ }
    playing = false; refreshButtons();
    if (hide && box) box.classList.add('hidden');
  }
  function changeVolume(delta) {
    userVolume = Math.max(5, Math.min(100, userVolume + delta));
    applyVolume();
    notify('Volume ' + userVolume + '%');
  }

  toggleBtn?.addEventListener('click', () => { playing ? pauseMusic() : resumeMusic(); });
  stopBtn?.addEventListener('click', () => stopMusic(true));

  window.alexaMusic = {
    isPlaying: () => playing,
    duck: applyVolume,            // call after voiceModeActive changes
    stop: () => stopMusic(true),
  };

  // ---------------------------------------------------------------- actions from the server
  function handleAction(a) {
    if (!a || !a.type) return;
    switch (a.type) {
      case 'play_youtube': playYoutube(a); break;
      case 'music_pause': pauseMusic(); break;
      case 'music_resume': resumeMusic(); break;
      case 'music_stop': stopMusic(true); break;
      case 'music_volume': changeVolume(a.delta || 0); break;
      case 'switch_person': if (window.alexaSwitchPerson) window.alexaSwitchPerson(a.name); break;
      case 'request_location': if (window.alexaRequestLocation) window.alexaRequestLocation(); break;
      case 'open_url': {
        openOnPc(a.url).then((ok) => { if (!ok) linkMessage('Your browser blocked the popup.', a.url); });
        break;
      }
      default: break;
    }
  }
  window.alexaHandleAction = handleAction;

  window.alexaAfterChat = (d) => {
    if (!d) return;
    if (d.action) handleAction(d.action);
    const r = d.refresh || [];
    try {
      if (r.includes('inventory') && typeof loadInventory === 'function') loadInventory();
      if (r.includes('memories') && typeof loadMemories === 'function') loadMemories();
    } catch (e) { /* ignore */ }
  };

  // ---------------------------------------------------------------- reminder alarms
  function beep() {
    try {
      const C = window.AudioContext || window.webkitAudioContext;
      if (!C) return;
      const ctx = new C();
      for (let i = 0; i < 6; i++) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.value = i % 2 ? 988 : 784;
        gain.gain.value = 0.15;
        osc.connect(gain); gain.connect(ctx.destination);
        osc.start(ctx.currentTime + i * 0.22);
        osc.stop(ctx.currentTime + i * 0.22 + 0.16);
      }
      setTimeout(() => ctx.close(), 1800);
    } catch (e) { /* audio may be blocked until the first click */ }
  }

  let announcing = Promise.resolve();
  function announce(rem) {
    announcing = announcing.then(async () => {
      const text = rem.announcement || ('Reminder: ' + rem.title);
      beep();
      notify('⏰ ' + rem.title);
      try {
        if ('Notification' in window && Notification.permission === 'granted') new Notification('Alexa+ reminder', { body: rem.title });
      } catch (e) { /* ignore */ }
      if (typeof addMessage === 'function') addMessage('assistant', '⏰ ' + text, false);
      await new Promise((r) => setTimeout(r, 1500));   // let the beep finish
      try {
        if (window.voiceAnnounce) await window.voiceAnnounce(text);
        else if (window.alexaSpeak) await window.alexaSpeak(text);
      } catch (e) { /* ignore */ }
    });
    return announcing;
  }
  window.alexaAnnounce = announce;

  async function pollDue() {
    try {
      const r = await fetch('/api/reminders/due');
      if (!r.ok) return;
      const d = await r.json();
      (d.reminders || []).forEach(announce);
    } catch (e) { /* server not reachable right now */ }
  }
  setInterval(pollDue, 4000);
  setTimeout(pollDue, 1200);

  // Ask once (on the first click) so reminders can also pop up as desktop notifications.
  document.addEventListener('click', () => {
    try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission(); } catch (e) { /* ignore */ }
  }, { once: true });
})();
