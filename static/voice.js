/*
 * Hands-free voice conversation ("Voice mode"), similar to talking to Alexa.
 *
 * Loop:  listen -> understand -> ask the assistant (/api/chat) -> speak the reply -> listen again
 *
 * Uses the browser's built-in Web Speech API (SpeechRecognition + speechSynthesis).
 * Works best in Google Chrome or Microsoft Edge on http://127.0.0.1 or localhost.
 * No audio is sent anywhere except to the browser's own speech service.
 *
 * Extras: spoken reminders (window.voiceAnnounce), music ducking, better wake-word matching,
 * smarter voice selection (Edge "Natural" voices), and a minimise button.
 */
(() => {
  'use strict';
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const $ = (s) => document.querySelector(s);
  const overlay = $('#voiceOverlay');
  if (!overlay) return;
  const orb = $('#voiceOrb');
  const stateEl = $('#voiceState');
  const heardEl = $('#voiceHeard');
  const replyEl = $('#voiceReply');
  const wakeToggle = $('#voiceWakeToggle');
  const notify = (m) => (typeof toast === 'function' ? toast(m) : console.log(m));

  const WAKE_KEY = 'alexaPlusVoiceWake';
  const WAKE_RE = /\b(?:hey |ok |okay |hi |hello )?(?:alexa\+?|alexa plus|alexia|alexsa|alexis|elexa|aleksa|alexxa|lexa|alexander)\b[,.!?]*|^\s*alex\b[,.!?]*|(?:एलेक्सा|अलेक्सा|एलेक्सिया)[,.!?]*/i;
  const STOP_RE = /^(?:stop|cancel|exit|quit|goodbye|good bye|bye|bye bye|that'?s all|that is all|never ?mind|go to sleep|band karo|bas karo|bas|ruk ?jao|बंद करो|रुको|बस|बस करो|अलविदा)[.! ]*$/i;
  const FOLLOW_UP_MS = 12000;   // after a reply, no wake word is needed for this long
  const IDLE_CLOSE_MS = 120000; // close voice mode after 2 minutes of silence

  let active = false;
  let listening = false;
  let state = 'idle';
  let rec = null;
  let recToken = 0;
  let speakToken = 0;
  let awake = false;
  let awakeTimer = null;
  let restartTimer = null;
  let idleTimer = null;
  let netRetries = 0;
  let voices = [];

  function loadVoices() { voices = window.speechSynthesis ? speechSynthesis.getVoices() : []; }
  if ('speechSynthesis' in window) { loadVoices(); speechSynthesis.onvoiceschanged = loadVoices; }

  const needWake = () => !!wakeToggle.checked;  // music is ducked while listening, so no wake word is forced

  function lang() {
    const v = $('#languageSelect')?.value || 'auto';
    return v === 'hi-IN' ? 'hi-IN' : v === 'en' ? 'en-US' : 'en-IN';
  }

  function setState(st, label) {
    state = st;
    orb.dataset.state = st;
    stateEl.textContent = label;
  }

  function resetIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (active) { notify('Voice mode closed after a period of silence.'); closeVoice(); } }, IDLE_CLOSE_MS);
  }

  function openAwakeWindow() {
    awake = true;
    clearTimeout(awakeTimer);
    awakeTimer = setTimeout(() => {
      awake = false;
      if (active && state === 'listening' && needWake()) setState('listening', 'Say “Alexa”…');
    }, FOLLOW_UP_MS);
  }

  // ---------- speaking ----------
  function cleanForSpeech(t) {
    return String(t || '')
      .replace(/https?:\/\/\S+/g, ' link ')
      .replace(/[*_`#>~|]/g, ' ')
      .replace(/[•▪◦·→←↗➤✦◉⌂☼▤⌛♨]/g, ', ')
      .replace(/\p{Extended_Pictographic}/gu, '')
      .replace(/\s*\n+\s*/g, '. ')
      .replace(/\s{2,}/g, ' ')
      .trim();
  }
  function chunkText(t) {
    // Chrome silently cuts off long utterances, so speak in short pieces.
    const sentences = t.match(/[^.!?।]+[.!?।]*/g) || [t];
    const out = [];
    let cur = '';
    for (const s of sentences) {
      if ((cur + s).length > 180 && cur) { out.push(cur.trim()); cur = s; } else cur += s;
    }
    if (cur.trim()) out.push(cur.trim());
    return out;
  }
  function pickVoice(l) {
    if (!voices.length) loadVoices();
    const prefix = l.slice(0, 2).toLowerCase();
    const pool = voices.filter((v) => v.lang && v.lang.toLowerCase().startsWith(prefix));
    if (!pool.length) return null;
    const score = (v) => {
      let sc = 0;
      if (v.lang.toLowerCase() === l.toLowerCase()) sc += 4;
      if (/natural|online/i.test(v.name)) sc += 6;                     // Edge neural voices sound much more human
      if (/(heera|neerja|swara|aria|jenny|zira|samantha|google (uk english female|हिन्दी|hindi|us english)|female)/i.test(v.name)) sc += 3;
      if (/google/i.test(v.name)) sc += 1;
      return sc;
    };
    return pool.slice().sort((a, b) => score(b) - score(a))[0];
  }
  function stopSpeaking() {
    speakToken++;
    if ('speechSynthesis' in window) speechSynthesis.cancel();
  }
  function speakCore(text, force) {
    return new Promise((resolve) => {
      const chunks = chunkText(cleanForSpeech(text));
      if (!('speechSynthesis' in window) || !chunks.length) { resolve(); return; }
      if (active) { stopRecognition(); setState('speaking', 'Speaking… (tap to interrupt)'); }
      const token = ++speakToken;
      speechSynthesis.cancel();
      let i = 0;
      const next = () => {
        if (token !== speakToken || (!active && !force)) { resolve(); return; }
        if (i >= chunks.length) { resolve(); return; }
        const u = new SpeechSynthesisUtterance(chunks[i++]);
        u.lang = lang();
        const v = pickVoice(u.lang);
        if (v) u.voice = v;
        u.rate = 1.02;
        u.onend = next;
        u.onerror = () => resolve();
        speechSynthesis.speak(u);
      };
      next();
    });
  }
  const speak = (text) => speakCore(text, false);
  window.alexaSpeak = (text) => speakCore(text, true);   // used for typed chat + reminder alarms

  // Spoken reminders / alarms. In voice mode: pause listening, speak, then listen again.
  window.voiceAnnounce = async (text) => {
    if (!active) { await speakCore(text, true); return; }
    const wasState = state;
    stopRecognition();
    replyEl.textContent = text;
    await speakCore(text, true);
    if (!active) return;
    if (wasState !== 'thinking') { setState('idle', 'Listening…'); startListening(); }
  };

  // ---------- listening ----------
  function stopRecognition() {
    recToken++;
    listening = false;
    clearTimeout(restartTimer);
    if (rec) {
      rec.onresult = rec.onerror = rec.onend = rec.onstart = null;
      try { rec.abort(); } catch (e) { /* already stopped */ }
      rec = null;
    }
  }

  function fatal(message) {
    stopRecognition();
    setState('error', message);
  }

  function startListening() {
    if (!active || listening || rec || state === 'thinking') return;
    if (!SR) { fatal('Voice input is not supported in this browser. Use Chrome or Edge.'); return; }
    const token = ++recToken;
    let finalText = '';
    let gotFinal = false;
    let errorCode = '';
    rec = new SR();
    rec.lang = lang();
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 3;
    try { navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }).then((s) => s.getTracks().forEach((t) => t.stop())).catch(() => {}); } catch (e) { /* ignore */ }
    rec.onstart = () => {
      listening = true;
      netRetries = 0;
      setState('listening', needWake() && !awake ? 'Say “Alexa”…' : 'Listening…');
    };
    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) {
          // If the best guess missed the wake word but another guess has it (e.g. "Alexia"), prefer that one.
          let best = r[0].transcript;
          if (needWake() && !awake && !WAKE_RE.test(best)) {
            for (let k = 1; k < r.length; k++) { if (WAKE_RE.test(r[k].transcript)) { best = r[k].transcript; break; } }
          }
          finalText += best + ' '; gotFinal = true;
        } else interim += r[0].transcript;
      }
      const shown = (finalText + interim).trim();
      if (shown) { heardEl.textContent = '“' + shown + '”'; resetIdle(); }
    };
    rec.onerror = (e) => { errorCode = e.error || 'unknown'; };
    rec.onend = () => {
      if (token !== recToken) return;
      listening = false;
      rec = null;
      if (!active) return;
      if (errorCode === 'not-allowed' || errorCode === 'service-not-allowed') {
        fatal('Microphone is blocked. Click the 🔒 icon in the address bar, allow the microphone, then tap the orb.');
        return;
      }
      if (errorCode === 'audio-capture') { fatal('No microphone was found. Plug one in, then tap the orb.'); return; }
      if (errorCode === 'network') {
        if (++netRetries > 3) { fatal('The browser speech service is unreachable. Check your internet connection, then tap the orb.'); return; }
      }
      if (gotFinal && finalText.trim()) { handleUtterance(finalText.trim()); return; }
      if (state === 'listening' || state === 'idle') restartTimer = setTimeout(startListening, 350);
    };
    try { rec.start(); } catch (e) { restartTimer = setTimeout(startListening, 500); }
  }

  // ---------- understanding + answering ----------
  async function handleUtterance(raw) {
    resetIdle();
    let cmd = raw.trim();
    const withoutWake = cmd.replace(WAKE_RE, '').replace(/^[\s,.!?]+/, '').trim();

    if (STOP_RE.test(withoutWake) && (awake || !needWake() || WAKE_RE.test(cmd))) {
      await speak('Goodbye!');
      closeVoice();
      return;
    }

    if (needWake() && !awake) {
      if (!WAKE_RE.test(cmd)) { heardEl.textContent = ''; startListening(); return; } // ignore background talk / the song playing
      if (!withoutWake) { openAwakeWindow(); await speak('Yes?'); startListening(); return; }
      cmd = withoutWake;
    } else if (withoutWake) {
      cmd = withoutWake;
    }

    heardEl.textContent = '“' + cmd + '”';
    replyEl.textContent = '';
    setState('thinking', 'Thinking…');
    let reply = '';
    try {
      reply = await window.sendChat(cmd, { voice: true });
    } catch (e) {
      reply = 'Sorry, something went wrong.';
    }
    if (!active) return;
    reply = reply || 'Sorry, I did not get a reply.';
    replyEl.textContent = reply;
    const data = window.lastChatData;
    await speak((data && data.speech) || reply);
    if (!active) return;
    openAwakeWindow();
    setState('idle', 'Listening…');
    startListening();
  }

  // ---------- open / close ----------
  async function openVoiceMode() {
    if (active) return;
    if (!SR) { notify('Voice conversation needs Google Chrome or Microsoft Edge (Firefox does not support it).'); return; }
    if (!window.isSecureContext) { notify('Open the app at http://127.0.0.1:5000 (not a network IP) so the browser allows the microphone.'); return; }
    active = true;
    window.voiceModeActive = true;
    if (window.alexaMusic) window.alexaMusic.duck();
    wakeToggle.checked = localStorage.getItem(WAKE_KEY) === '1';
    overlay.classList.remove('hidden');
    document.body.classList.add('voice-open');
    overlay.classList.remove('mini');
    heardEl.textContent = '';
    replyEl.textContent = '';
    resetIdle();
    // Speak the greeting straight from the click so the browser allows audio.
    setState('speaking', 'Starting…');
    await speak(wakeToggle.checked ? 'Voice mode on. Say Alexa when you need me.' : "I'm listening.");
    if (!active) return;
    openAwakeWindow();
    setState('idle', 'Listening…');
    startListening();
  }

  function closeVoice() {
    active = false;
    window.voiceModeActive = false;
    overlay.classList.remove('mini');
    if (window.alexaMusic) window.alexaMusic.duck();
    clearTimeout(awakeTimer);
    clearTimeout(idleTimer);
    awake = false;
    stopSpeaking();
    stopRecognition();
    setState('idle', 'Voice mode off');
    overlay.classList.add('hidden');
    document.body.classList.remove('voice-open');
  }

  orb.addEventListener('click', () => {
    if (state === 'speaking') { stopSpeaking(); return; }            // interrupt; the loop then starts listening
    if (state === 'listening' && rec) { try { rec.stop(); } catch (e) { /* ignore */ } return; } // submit what was heard
    if (state === 'error' || state === 'idle') { setState('idle', 'Listening…'); startListening(); }
  });
  $('#voiceClose').addEventListener('click', closeVoice);
  function setMini(on) {
    overlay.classList.toggle('mini', on);
    document.body.classList.toggle('voice-open', active && !on);
  }
  $('#voiceMini')?.addEventListener('click', () => setMini(!overlay.classList.contains('mini')));
  overlay.addEventListener('click', (e) => { if (overlay.classList.contains('mini') && !e.target.closest('button,label,input')) setMini(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && active) closeVoice(); });
  wakeToggle.addEventListener('change', () => {
    localStorage.setItem(WAKE_KEY, wakeToggle.checked ? '1' : '0');
    awake = false;
    if (state === 'listening') setState('listening', wakeToggle.checked ? 'Say “Alexa”…' : 'Listening…');
  });
  $('#voiceModeBtn')?.addEventListener('click', openVoiceMode);
  window.addEventListener('beforeunload', () => { try { speechSynthesis.cancel(); } catch (e) { /* ignore */ } });

  window.openVoiceMode = openVoiceMode;
  window.closeVoiceMode = closeVoice;
})();
