/*
 * Extras: person profiles, live location, serious notifications, study-plan button.
 * Each person gets a separate database on the server (selected by the "person" cookie).
 */
(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const note = (m) => (typeof toast === 'function' ? toast(m) : console.log(m));
  const getPerson = () => { const m = document.cookie.match(/(?:^|; )person=([^;]*)/); return m ? decodeURIComponent(m[1]) : 'default'; };
  const setPerson = (p) => { document.cookie = 'person=' + encodeURIComponent(p) + '; path=/; max-age=31536000; SameSite=Lax'; };
  const label = (p) => (p === 'default' ? 'Main' : p.charAt(0).toUpperCase() + p.slice(1));

  // ---------------------------------------------------------------- people
  async function initPeople() {
    const sel = $('#personSelect');
    if (!sel) return;
    try {
      const d = await (await fetch('/api/people')).json();
      sel.innerHTML = '';
      d.people.forEach((p) => { const o = document.createElement('option'); o.value = p; o.textContent = label(p); sel.appendChild(o); });
      sel.value = getPerson();
    } catch (e) { /* offline */ }
    sel.addEventListener('change', () => { setPerson(sel.value); location.reload(); });
    $('#personAdd')?.addEventListener('click', async () => {
      const name = (prompt('Name of the new person:') || '').trim();
      if (!name) return;
      const r = await fetch('/api/people', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
      const d = await r.json();
      if (d.ok) { setPerson(d.person); location.reload(); } else note(d.message || 'Could not create profile.');
    });
  }
  window.alexaSwitchPerson = (name) => { setPerson(name || 'default'); setTimeout(() => location.reload(), 2500); };

  // ---------------------------------------------------------------- live location
  let watchId = null;
  let lastSent = { t: 0, lat: 0, lng: 0 };
  const pill = $('#locPill');
  function setPill(text, ok) { if (pill) { pill.textContent = text; pill.classList.toggle('on', !!ok); } }
  function dist(a, b, c, d) { const r = 6371000, p = Math.PI / 180; const x = (c - a) * p, y = (d - b) * p * Math.cos(a * p); return r * Math.hypot(x, y); }
  function send(pos) {
    const { latitude: lat, longitude: lng, accuracy } = pos.coords;
    setPill('📍 Location on', true);
    const now = Date.now();
    if (now - lastSent.t < 45000 && dist(lat, lng, lastSent.lat, lastSent.lng) < 50) return;
    lastSent = { t: now, lat, lng };
    fetch('/api/location', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lat, lng, accuracy }) }).catch(() => {});
  }
  function startLocation() {
    if (!('geolocation' in navigator)) { setPill('📍 Not supported', false); return; }
    if (watchId !== null) return;
    watchId = navigator.geolocation.watchPosition(send, (e) => {
      watchId = null;
      setPill(e.code === 1 ? '📍 Location blocked' : '📍 Location off', false);
    }, { enableHighAccuracy: true, maximumAge: 30000, timeout: 20000 });
  }
  window.alexaRequestLocation = () => { note('Please allow location access when the browser asks.'); startLocation(); };
  pill?.addEventListener('click', () => { watchId = null; startLocation(); });

  // ---------------------------------------------------------------- serious notifications
  function beep() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const o = ctx.createOscillator(); const g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination); o.frequency.value = 880; g.gain.value = 0.08;
      o.start(); setTimeout(() => { o.stop(); ctx.close(); }, 350);
    } catch (e) { /* ignore */ }
  }
  function show(n) {
    const text = n.title + (n.body ? ' — ' + n.body : '');
    note((n.severity === 'serious' ? '🚨 ' : '🔔 ') + text);
    if (typeof addMessage === 'function') addMessage('assistant', (n.severity === 'serious' ? '🚨 Serious: ' : '🔔 ') + text, false);
    try { if ('Notification' in window && Notification.permission === 'granted') new Notification(n.severity === 'serious' ? 'Alexa+: needs attention' : 'Alexa+', { body: text, tag: n.key }); } catch (e) { /* ignore */ }
    if (n.severity === 'serious') {
      beep();
      const say = window.voiceAnnounce || window.alexaSpeak;
      if (say) say('Heads up. ' + n.title + '.');
    }
  }
  async function pollNotifications() {
    try {
      const d = await (await fetch('/api/notifications')).json();
      (d.notifications || []).forEach(show);
    } catch (e) { /* ignore */ }
  }
  const bell = $('#notifBtn');
  function bellState() { if (bell) bell.style.display = ('Notification' in window && Notification.permission === 'default') ? '' : 'none'; }
  bell?.addEventListener('click', async () => { try { await Notification.requestPermission(); } catch (e) { /* ignore */ } bellState(); });

  // ---------------------------------------------------------------- study plan button
  $('#makePlanBtn')?.addEventListener('click', async () => {
    const out = $('#planResult');
    if (out) out.textContent = 'Building your plan from your history…';
    try {
      const r = await fetch('/api/study-plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ days: 7 }) });
      const d = await r.json();
      if (!out) return;
      const hr = d.best_hour % 12 || 12, ap = d.best_hour < 12 ? 'AM' : 'PM';
      const head = (d.has_history ? `You usually study around ${hr}:00 ${ap}, ~${d.daily_minutes} min/day.` : 'No history yet — using a gentle default; I will learn as you complete tasks.')
        + (d.weak_subjects.length ? ` Focus on: ${d.weak_subjects.join(', ')}.` : '') + (d.overdue ? ` ${d.overdue} overdue.` : '');
      out.innerHTML = '';
      const h = document.createElement('p'); h.className = 'muted'; h.textContent = head; out.appendChild(h);
      if (!d.plan.length) { const p = document.createElement('p'); p.textContent = 'No pending tasks to plan. Add a task first.'; out.appendChild(p); return; }
      d.plan.forEach((p) => { const row = document.createElement('div'); row.className = 'data-item'; row.textContent = `${p.date} · ${p.start} · ${p.minutes} min — ${p.subject ? p.subject + ': ' : ''}${p.title}`; out.appendChild(row); });
    } catch (e) { if (out) out.textContent = 'Could not build the plan.'; }
  });

  initPeople();
  bellState();
  startLocation();
  setTimeout(pollNotifications, 4000);
  setInterval(pollNotifications, 60000);
})();
