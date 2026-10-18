/* PI Voice frontend: mic -> AudioWorklet -> PCM16 -> WebSocket -> Pi model. */
'use strict';

const $ = (id) => document.getElementById(id);
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const EFFECT_LABELS = Object.freeze({
  clean: 'Clean',
  deep: 'Vader',
  robot: 'Robot',
  chipmunk: 'Chipmunk',
  megaphone: 'Mega',
  clone: 'Clone',
});
const TAKEOVER_MS = reduceMotion ? 150 : 1200;
const PERSONA_MS = 4500;

// Identity-intercept running gag: each breach pulls a fresh roast alias
// for whoever the classifier caught. The HUD keeps the real name; only
// the takeover theater wears the joke.
const ROASTS = Object.freeze([
  'THREE KIDS IN A TRENCH COAT',
  'THE HUMAN DIAL TONE',
  'A PARROT WITH ROOT ACCESS',
  'MODEM NOISE WITH OPINIONS',
  'THE DIAL-UP BANSHEE',
  'CERTIFIED VOICE GREMLIN',
  'ONE (1) SUSPICIOUS VOCAL CORD',
  'MOSTLY HOLD MUSIC',
  'THE ECHO\'S MANAGER',
  '{name} (ALLEGEDLY)',
  'NOT {name} (PROBABLY)',
  '{name} FROM A PARALLEL DIMENSION',
]);
let lastRoast = '';
function pickRoast(name) {
  let gag = lastRoast;
  for (let tries = 0; tries < 8 && gag === lastRoast; tries += 1) {
    const t = ROASTS[(Math.random() * ROASTS.length) | 0];
    gag = t.replaceAll('{name}', name.toUpperCase());
  }
  lastRoast = gag;
  return gag;
}

let ws = null;
let audioCtx = null;
let micOn = false;
let enrolling = false;
let enrollTimer = null;
const appShell = $('app-shell');

// mic pipeline refs (needed to toggle AEC live)
let micStream = null;
let micTrack = null;
let micSource = null;
let workletNode = null;

// playback chain: converted audio -> jitter buffer + smooth gate (worklet)
// -> limiter -> 75% cap -> speaker
const OUT_GAIN = 0.75;   // fixed cap: keeps phone-speaker feedback tame
let limiter = null;
let outGain = null;
let playNode = null;
let activeTarget = null;
let activeEffect = 'clean';
let lastCharacterEffect = 'robot';
let transformPhase = 'dsp';
let transformFallback = 'robot';

// Visual identity choreography. It never touches the live audio connection.
let presentationState = 'console';
let identityCandidate = null;
let identityStreak = 0;
let lastBreachIdentity = null;
let personaIdentity = null;
let queuedIdentity = null;
let takeoverTimer = null;
let personaTimer = null;
let queuedLaunchTimer = null;

// small display-only ring of recent samples for the waveform canvas
const VIS_LEN = 8192;
const visBuf = new Float32Array(VIS_LEN);
let visPos = 0;

/* ---------------- secure-context guard ---------------- */
if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
  $('ctx-warning').classList.remove('hidden');
  $('btn-mic').disabled = true;
  appShell.dataset.session = 'blocked';
}

function showError(msg) {
  const el = $('error');
  el.textContent = msg;
  el.classList.remove('hidden');
  setTimeout(() => el.classList.add('hidden'), 6000);
}

/* ---------------- websocket ---------------- */
function wsURL() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${location.host}/ws`;
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function setStageState(label, live = false) {
  const stage = $('stage-state');
  stage.lastChild.textContent = ` ${label}`;
  stage.classList.toggle('live', live);
  stage.dataset.status = label.toLowerCase();
  const monitor = $('voice-orb');
  monitor.classList.toggle('live', live);
  appShell.dataset.connection = label.toLowerCase();
}

function unlockSessionModules() {
  appShell.dataset.session = 'active';
  document.querySelectorAll('[data-session-lock]').forEach((module) => {
    module.removeAttribute('inert');
    module.removeAttribute('aria-disabled');
    module.classList.remove('locked');
  });
}

let wsFailures = 0;

function connectWS() {
  if (!micOn) return;
  setStageState('CONNECTING', false);
  ws = new WebSocket(wsURL());
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    $('hud-ws').textContent = 'ON';
    $('hud-ws').dataset.state = 'on';
    setStageState('LIVE', true);
    playNode?.port.postMessage({ type: 'reset' }); // drop stale audio
    if (audioCtx.state === 'suspended') audioCtx.resume();
    send({ type: 'start', sampleRate: audioCtx.sampleRate });
  };
  const opened = { value: false };
  ws.addEventListener('open', () => { opened.value = true; wsFailures = 0; });
  ws.onclose = () => {
    // Say why instead of silently retrying: the usual cause is a page that
    // was opened as a file or from a preview server with no /ws behind it.
    if (!opened.value && micOn && wsFailures++ % 5 === 0) {
      showError(location.protocol === 'file:'
        ? 'No server: open the page from the PI Voice server (e.g. http://localhost:3000), not as a file.'
        : `Can't reach the PI Voice server at ${wsURL()} — is it running on this address/port?`);
    }
    $('hud-ws').textContent = 'OFF';
    $('hud-ws').dataset.state = 'off';
    setStageState(micOn ? 'RECONNECTING' : 'STANDBY', false);
    if (micOn) setTimeout(connectWS, 3000); // retry while mic is live
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') { onPlayback(ev.data); return; }
    const msg = JSON.parse(ev.data);
    if (msg.type === 'result') onResult(msg);
    else if (msg.type === 'enroll_started') onEnrollStarted(msg);
    else if (msg.type === 'enrolled') onEnrolled(msg);
    else if (msg.type === 'target_set') onTargetSet(msg);
    else if (msg.type === 'effect_set') onEffectSet(msg);
    else if (msg.type === 'transform_state') onTransformState(msg);
    else if (msg.type === 'error') showError(msg.message);
  };
}

/* ---------------- results / HUD ---------------- */
// Hold-to-identify: live results pause while held, and the final verdict
// stays on screen for FINAL_HOLD_MS before live updates resume.
const FINAL_HOLD_MS = 3000;
let pttHeld = false;
let finalUntil = 0;

function onResult(msg) {
  if (!msg.final && (pttHeld || performance.now() < finalUntil)) return;
  if (msg.final) finalUntil = performance.now() + FINAL_HOLD_MS;
  if (msg.error) {
    $('confidence-readout').textContent = `Final / ${msg.error}`;
    return;
  }
  const el = $('speaker');
  const monitor = $('voice-orb');
  const confidence = Math.round(msg.confidence * 100);
  if (msg.speaker === null) {
    el.textContent = 'Listening…';
    el.className = 'unknown';
    monitor.dataset.voiceState = 'listening';
    $('confidence-readout').textContent = 'Below voice threshold';
  } else {
    el.textContent = msg.speaker;
    el.className = msg.speaker === 'unknown' ? 'unknown' : '';
    monitor.dataset.voiceState = msg.speaker === 'unknown' ? 'unknown' : 'identified';
    $('confidence-readout').textContent = msg.speaker === 'unknown'
      ? `${confidence}% / no match`
      : `${confidence}% confidence`;
  }
  if (msg.final) {
    const span = msg.held_sec != null ? ` · ${msg.held_sec}s held` : '';
    $('confidence-readout').textContent = `FINAL / ${$('confidence-readout').textContent}${span}`;
  }
  $('conf-fill').style.width = `${confidence}%`;
  $('match-readout').textContent = String(confidence).padStart(2, '0');
  $('hud-lat').textContent = `${msg.latency_ms} ms`;
  $('hud-rtf').textContent = msg.rtf;
  $('hud-voiced').textContent = msg.speaker === null ? 'NO' : 'YES';
  observeIdentity(msg);
}

function announce(message) {
  const region = $('sr-status');
  region.textContent = '';
  requestAnimationFrame(() => { region.textContent = message; });
}

function observeIdentity(msg) {
  const name = typeof msg.speaker === 'string' ? msg.speaker.trim() : '';
  if (!name || name.toLowerCase() === 'unknown') {
    identityCandidate = null;
    identityStreak = 0;
    return;
  }
  if (name === identityCandidate) identityStreak += 1;
  else {
    identityCandidate = name;
    identityStreak = 1;
  }
  if (identityStreak < 2 || name === lastBreachIdentity) return;
  lastBreachIdentity = name;
  beginIdentitySequence({
    name,
    confidence: Math.round(msg.confidence * 100),
    latency: msg.latency_ms,
  });
}

function setPresentation(state) {
  presentationState = state;
  appShell.dataset.presentation = state;
  $('breach-takeover').dataset.active = state === 'crashing' ? 'true' : 'false';
  $('monitor-default').setAttribute('aria-hidden', state === 'persona' ? 'true' : 'false');
  $('persona-monitor').setAttribute('aria-hidden', state === 'persona' ? 'false' : 'true');
}

function beginIdentitySequence(identity) {
  if (presentationState !== 'console') {
    if (!personaIdentity || identity.name !== personaIdentity.name) queuedIdentity = identity;
    return;
  }
  clearTimeout(queuedLaunchTimer);
  clearTimeout(takeoverTimer);
  clearTimeout(personaTimer);
  identity.gag = pickRoast(identity.name);
  personaIdentity = identity;
  $('breach-identity').textContent = identity.gag;
  setPresentation('crashing');
  takeoverTimer = setTimeout(showPersona, TAKEOVER_MS);
}

function showPersona() {
  setPresentation('persona');
  updatePersona();
  const route = personaDestination();
  announce(`${personaIdentity.name} identified as ${personaIdentity.gag}. ${route} transformation active.`);
  restartPersonaTimer();
}

function restartPersonaTimer() {
  if (presentationState !== 'persona') return;
  clearTimeout(personaTimer);
  personaTimer = setTimeout(returnToConsole, PERSONA_MS);
}

function returnToConsole() {
  clearTimeout(personaTimer);
  setPresentation('console');
  personaIdentity = null;
  if (queuedIdentity) {
    const nextIdentity = queuedIdentity;
    queuedIdentity = null;
    queuedLaunchTimer = setTimeout(
      () => beginIdentitySequence(nextIdentity), reduceMotion ? 0 : 300);
  }
}

function effectLabel(name) {
  return EFFECT_LABELS[name] || String(name || 'Clean');
}

function personaDestination() {
  if ((transformPhase === 'ready' || activeEffect === 'clone') && activeTarget) {
    return `Clone: ${activeTarget}`;
  }
  const fallback = transformPhase === 'preparing' || transformPhase === 'fallback'
    ? transformFallback : activeEffect;
  return effectLabel(fallback);
}

function updatePersona() {
  if (!personaIdentity) return;
  $('persona-source').textContent = personaIdentity.gag || personaIdentity.name;
  $('persona-destination').textContent = personaDestination();
  $('persona-confidence').textContent = `${personaIdentity.confidence}%`;
  $('persona-latency').textContent = `${personaIdentity.latency} ms`;
  const status = transformPhase === 'preparing' ? 'CLONE PREPARING'
    : transformPhase === 'ready' ? 'CLONE READY'
      : transformPhase === 'fallback' ? 'DSP FALLBACK' : 'DSP ACTIVE';
  $('persona-status').textContent = status;
  appShell.dataset.transformPhase = transformPhase;
}

/* ---------------- mic pipeline ---------------- */
// AEC defaults ON (feedback plan): the phone speaker is the far-end
// reference, so converted audio gets cancelled while your voice survives.
// NS/AGC stay off — they mangle voice identity.
function micConstraints(aec) {
  return {
    audio: {
      echoCancellation: aec,
      noiseSuppression: false,
      autoGainControl: false,
      channelCount: 1,
    },
  };
}

async function startMic() {
  const micButton = $('btn-mic');
  micButton.disabled = true;
  micButton.classList.add('pending');
  setStageState('ARMING', false);
  appShell.dataset.session = 'arming';
  try {
    audioCtx = new AudioContext();
    await audioCtx.audioWorklet.addModule('audio-worklet.js');

    micStream = await navigator.mediaDevices.getUserMedia(
      micConstraints($('aec').checked));
    micTrack = micStream.getAudioTracks()[0];
    micSource = audioCtx.createMediaStreamSource(micStream);
    workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');
    workletNode.port.onmessage = (e) => onPCM(e.data);

    // playback chain: jitter buffer + gate -> limiter -> fixed 75% gain
    limiter = audioCtx.createDynamicsCompressor();
    limiter.threshold.value = -6;
    limiter.knee.value = 0;
    limiter.ratio.value = 20;
    limiter.attack.value = 0.003;
    limiter.release.value = 0.25;
    outGain = audioCtx.createGain();
    outGain.gain.value = OUT_GAIN;
    limiter.connect(outGain);
    outGain.connect(audioCtx.destination);
    playNode = new AudioWorkletNode(audioCtx, 'playback-processor', {
      numberOfInputs: 0, outputChannelCount: [1],
    });
    playNode.port.onmessage = (e) => {
      const s = e.data;   // ~1/s: buffer depth + gap counters
      $('hud-aud').title = `buffer ${s.bufferedMs}/${s.cushionMs} ms`
        + ` · gaps ${s.underruns} · pauses ${s.pauses} · latency trims ${s.trimmed}`;
    };
    playNode.connect(limiter);

    const mute = audioCtx.createGain(); // keep worklet alive, no feedback
    mute.gain.value = 0;
    micSource.connect(workletNode);
    workletNode.connect(mute);
    mute.connect(audioCtx.destination);

    micOn = true;
    unlockSessionModules();
    connectWS();
    micButton.querySelector('.control-copy strong').textContent = 'Voice session live';
    micButton.querySelector('.control-copy small').textContent = 'Audio is routing through the Pi';
    micButton.classList.remove('pending');
    micButton.classList.add('live');
    micButton.disabled = true;
    $('speaker').textContent = 'Listening…';
    $('confidence-readout').textContent = 'Waiting for first window';
    $('btn-ptt').classList.remove('hidden');
    $('fx-row').classList.remove('hidden');
    $('enroll').classList.remove('hidden');
    drawWave();
  } catch (err) {
    micOn = false;
    appShell.dataset.session = 'idle';
    micButton.disabled = false;
    micButton.classList.remove('pending');
    setStageState('ERROR', false);
    showError(`getUserMedia failed: ${err.name} ${err.message}`);
  }
}

// AEC toggle: try live applyConstraints; fall back to a mic restart.
async function setAEC(on) {
  if (!micTrack) return;
  try {
    await micTrack.applyConstraints({ echoCancellation: on });
    if (micTrack.getSettings().echoCancellation === on) return;
    throw new Error('constraint not applied');
  } catch {
    try {
      const stream = await navigator.mediaDevices.getUserMedia(
        micConstraints(on));
      const track = stream.getAudioTracks()[0];
      const src = audioCtx.createMediaStreamSource(stream);
      micSource.disconnect();
      micTrack.stop();
      micStream = stream;
      micTrack = track;
      micSource = src;
      src.connect(workletNode);
    } catch (err) {
      showError(`AEC toggle failed: ${err.message}`);
      $('aec').checked = !on;
    }
  }
}
$('aec').addEventListener('change', (e) => setAEC(e.target.checked));

/* ---------------- speaker playback ---------------- */
let rxFrames = 0;
function onPlayback(arrayBuffer) {
  if (!audioCtx) return;
  if (audioCtx.state === 'suspended') audioCtx.resume();
  rxFrames += 1;
  if (rxFrames % 10 === 0) $('hud-aud').textContent = `${rxFrames}f`;
  if (!playNode) return;
  const pcm = new Int16Array(arrayBuffer);
  const f32 = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
  playNode.port.postMessage(f32, [f32.buffer]);   // gate + timing in worklet
}

function onPCM(arrayBuffer) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(arrayBuffer);
  const pcm = new Int16Array(arrayBuffer);
  let peak = 0;
  for (let i = 0; i < pcm.length; i++) {
    const v = pcm[i] / 32768;
    visBuf[visPos] = v;
    visPos = (visPos + 1) % VIS_LEN;
    const a = Math.abs(v);
    if (a > peak) peak = a;
  }
  const level = Math.min(1, peak * 1.4);
  const levelPercent = Math.round(level * 100);
  document.documentElement.style.setProperty('--level', level.toFixed(3));
  $('level-fill').style.width = `${levelPercent}%`;
  $('level-readout').textContent = String(levelPercent).padStart(2, '0');
  $('persona-level').textContent = String(levelPercent).padStart(2, '0');
}

/* ---------------- push-to-talk ---------------- */
const ptt = $('btn-ptt');
ptt.addEventListener('pointerdown', (e) => {
  // capture: a finger drifting off the button mustn't end the hold early
  ptt.setPointerCapture(e.pointerId);
  ptt.classList.add('held');
  $('voice-orb').classList.add('identifying');
  pttHeld = true;
  send({ type: 'ptt_start' });
  $('confidence-readout').textContent = 'Listening… release to identify';
});
const endPTT = () => {
  if (ptt.classList.contains('held')) {
    ptt.classList.remove('held');
    $('voice-orb').classList.remove('identifying');
    pttHeld = false;
    send({ type: 'final' });
  }
};
ptt.addEventListener('pointerup', endPTT);
ptt.addEventListener('pointercancel', endPTT);
ptt.addEventListener('pointerleave', endPTT);

/* ---------------- enrollment ---------------- */
const ENROLL_IDLE_LABEL = $('btn-enroll').textContent;

function setEnrollButtonState(state, seconds = null) {
  const button = $('btn-enroll');
  if (state === 'recording') {
    button.dataset.state = 'recording';
    button.textContent = `Recording ${seconds}s`;
    button.setAttribute('aria-label', `Recording, ${seconds} seconds remaining`);
  } else if (state === 'processing') {
    button.dataset.state = 'processing';
    button.textContent = 'Processing…';
    button.setAttribute('aria-label', 'Recording complete, processing voice profile');
  } else {
    delete button.dataset.state;
    button.textContent = ENROLL_IDLE_LABEL;
    button.removeAttribute('aria-label');
  }
}

$('btn-enroll').addEventListener('click', () => {
  const name = $('enroll-name').value.trim();
  if (!name || enrolling) return;
  send({ type: 'enroll_start', name });
});
$('enroll-name').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') $('btn-enroll').click();
});

function onEnrollStarted(msg) {
  enrolling = true;
  $('enroll').classList.add('enrolling');
  $('btn-enroll').disabled = true;
  let left = Math.ceil(Number(msg.seconds) || 10);
  const label = `Recording ${msg.name}… `;
  setEnrollButtonState('recording', left);
  $('enroll-status').textContent = `${label}${left}s — talk normally`;
  enrollTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      clearInterval(enrollTimer);
      enrollTimer = null;
      send({ type: 'enroll_stop' });
      setEnrollButtonState('processing');
      $('enroll-status').textContent = 'Computing voice profile on the Pi…';
    } else {
      setEnrollButtonState('recording', left);
      $('enroll-status').textContent = `${label}${left}s — talk normally`;
    }
  }, 1000);
}

function onEnrolled(msg) {
  if (enrollTimer) clearInterval(enrollTimer);
  enrollTimer = null;
  enrolling = false;
  $('enroll').classList.remove('enrolling');
  $('btn-enroll').disabled = false;
  setEnrollButtonState('idle');
  $('enroll-status').textContent = msg.ok
    ? `Enrolled "${msg.name}".`
    : `Enroll failed: ${msg.error}`;
  if (msg.ok) {
    $('enroll-name').value = '';
    refreshProfiles();
  }
}

function onTargetSet(msg) {
  activeTarget = msg.found ? msg.name : null;
  appShell.dataset.target = msg.found ? 'selected' : 'none';
  $('target-label').textContent = msg.found ? `TARGET / ${msg.name}` : 'NO TARGET';
  refreshProfiles();
  if (!msg.found) showError(`No voice profile for "${msg.name}" — converting raw`);
}

/* ---------------- voice effects ---------------- */
function onEffectSet(msg) {
  activeEffect = msg.name;
  if (!['clean', 'clone'].includes(msg.name)) lastCharacterEffect = msg.name;
  if (msg.name === 'clone') transformPhase = 'ready';
  else transformPhase = 'dsp';
  appShell.dataset.effect = msg.name;
  $('hud-fx').textContent = msg.name === 'clone' && activeTarget
    ? `clone:${activeTarget}` : msg.name;
  document.querySelectorAll('.btn.fx').forEach((b) =>
    b.classList.toggle('active', b.dataset.effect === msg.name));
  updatePersona();
  restartPersonaTimer();
}

function onTransformState(msg) {
  transformPhase = msg.phase === 'cancelled' ? 'dsp' : msg.phase;
  if (msg.fallback) {
    transformFallback = msg.fallback;
    lastCharacterEffect = msg.fallback;
  }
  if (msg.phase === 'preparing') {
    $('target-label').textContent = `CLONE / ${msg.target} / PREPARING`;
  } else if (msg.phase === 'ready') {
    $('target-label').textContent = `TARGET / ${msg.target}`;
  } else if (msg.phase === 'fallback') {
    $('target-label').textContent = activeTarget
      ? `TARGET / ${msg.target} / DSP` : 'NO TARGET';
  }
  updatePersona();
  if (presentationState === 'persona') {
    if (msg.phase === 'ready') announce(`${msg.target} clone ready.`);
    else if (msg.phase === 'fallback') announce(`Clone unavailable. ${effectLabel(msg.fallback)} fallback remains active.`);
    restartPersonaTimer();
  }
}
document.querySelectorAll('.btn.fx').forEach((b) =>
  b.addEventListener('click', () =>
    send({ type: 'effect', name: b.dataset.effect })));

async function refreshProfiles() {
  try {
    const res = await fetch('/api/profiles');
    const { profiles } = await res.json();
    const ul = $('profiles');
    ul.innerHTML = '';
    if (!profiles.length) {
      const empty = document.createElement('li');
      empty.className = 'empty-profile';
      empty.textContent = 'No voices enrolled yet. Add a teammate below to create the first target.';
      ul.append(empty);
      return;
    }
    for (const name of profiles) {
      const li = document.createElement('li');
      if (name === activeTarget) li.classList.add('active');
      li.tabIndex = 0;
      li.setAttribute('role', 'button');
      li.setAttribute('aria-label', `Use ${name} as conversion target`);

      const avatar = document.createElement('span');
      avatar.className = 'profile-avatar';
      avatar.textContent = name.split(/\s+/).slice(0, 2)
        .map((part) => part.charAt(0)).join('');

      const copy = document.createElement('span');
      copy.className = 'profile-copy';
      const profileName = document.createElement('span');
      profileName.className = 'profile-name';
      profileName.textContent = name;
      const profileState = document.createElement('span');
      profileState.className = 'profile-state';
      profileState.textContent = name === activeTarget ? 'Live target' : 'Tap to select';
      copy.append(profileName, profileState);

      const del = document.createElement('button');
      del.className = 'del';
      del.type = 'button';
      del.textContent = '×';
      del.setAttribute('aria-label', `Delete ${name}`);
      del.onclick = async (e) => {
        e.stopPropagation();
        await fetch(`/api/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (name === activeTarget) {
          activeTarget = null;
          $('target-label').textContent = 'NO TARGET';
        }
        refreshProfiles();
      };
      const choose = () => send({ type: 'target', name });
      li.onclick = choose;
      li.onkeydown = (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          choose();
        }
      };
      li.append(avatar, copy, del);
      ul.append(li);
    }
  } catch { /* non-fatal */ }
}
refreshProfiles();

/* ---------------- thermal badge ---------------- */
async function pollSystem() {
  try {
    const res = await fetch('/api/system');
    const s = await res.json();
    const badge = $('temp-badge');
    if (s.on_pi && s.temp_c != null) {
      badge.textContent = `PI ${s.temp_c.toFixed(1)}°C`;
      badge.className = 'badge' + (s.throttled ? ' throttled' : s.temp_c > 70 ? ' hot' : '');
      badge.title = s.throttled ? 'Raspberry Pi thermal — THROTTLING'
        : s.throttled_since_boot ? `Raspberry Pi thermal — throttled earlier since boot (${s.throttled_hex})`
        : 'Raspberry Pi thermal';
    } else {
      badge.textContent = 'PI N/A';
      badge.className = 'badge';
      badge.title = 'Raspberry Pi not detected';
    }
  } catch { /* non-fatal */ }
}
pollSystem();
setInterval(pollSystem, 5000);

/* ---------------- waveform ---------------- */
function drawWave() {
  const cv = $('wave');
  const g = cv.getContext('2d');
  const w = cv.width, h = cv.height, mid = h / 2;
  (function frame() {
    g.clearRect(0, 0, w, h);
    const gradient = g.createLinearGradient(0, 0, w, 0);
    gradient.addColorStop(0, 'rgba(13, 17, 16, 0)');
    gradient.addColorStop(.18, 'rgba(13, 17, 16, .72)');
    gradient.addColorStop(.52, 'rgba(255, 107, 66, .96)');
    gradient.addColorStop(.82, 'rgba(13, 17, 16, .76)');
    gradient.addColorStop(1, 'rgba(13, 17, 16, 0)');
    g.strokeStyle = gradient;
    g.lineWidth = 2.4;
    g.beginPath();
    const step = Math.ceil(VIS_LEN / w);
    for (let x = 0; x < w; x++) {
      const i = (visPos + x * step) % VIS_LEN;
      const y = mid - visBuf[i] * mid * 0.9;
      x === 0 ? g.moveTo(x, y) : g.lineTo(x, y);
    }
    g.stroke();
    requestAnimationFrame(frame);
  })();
}

/* ---------------- matrix rain ---------------- */
(function drawRain() {
  const cv = $('rain');
  if (reduceMotion) return;
  const g = cv.getContext('2d');
  const GLYPHS = '01ﾊﾐﾋｰｳｼﾅﾓﾆｻﾜﾂｵﾘｱﾎﾃﾏｹﾌｺｴﾃｷﾑﾈﾗｯ<>/\\|=+*#';
  const CELL = 26;
  const TRAIL = 11;
  let cols = [];

  function resize() {
    cv.width = innerWidth;
    cv.height = innerHeight;
    const count = Math.ceil(innerWidth / CELL);
    cols = Array.from({ length: count }, () => ({
      y: Math.random() * innerHeight * 1.4 - innerHeight * .4,
      speed: .35 + Math.random() * 1.5,
      head: Math.random() < .12, // orange-tipped streaks
    }));
  }
  resize();
  addEventListener('resize', resize);

  let last = 0;
  (function frame(t) {
    requestAnimationFrame(frame);
    if (t - last < 50) return;   // ~20fps is plenty for ambience
    last = t;
    g.clearRect(0, 0, cv.width, cv.height);
    g.font = '13px "IBM Plex Mono", monospace';
    cols.forEach((c, i) => {
      const x = i * CELL + 8;
      for (let j = 0; j < TRAIL; j++) {
        const y = c.y - j * CELL;
        if (y < -CELL || y > cv.height + CELL) continue;
        const fade = 1 - j / TRAIL;
        g.fillStyle = j === 0 && c.head
          ? `rgba(255, 107, 66, ${(.5 * fade + .25).toFixed(3)})`
          : `rgba(199, 216, 201, ${(.34 * fade).toFixed(3)})`;
        g.fillText(GLYPHS[(Math.random() * GLYPHS.length) | 0], x, y);
      }
      c.y += c.speed * CELL * .5;
      if (c.y - TRAIL * CELL > cv.height) {
        c.y = -Math.random() * cv.height * .3;
        c.speed = .35 + Math.random() * 1.5;
        c.head = Math.random() < .12;
      }
    });
  })(0);
})();

$('btn-mic').addEventListener('click', startMic);

function updateClock() {
  $('local-time').textContent = new Intl.DateTimeFormat([], {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date());
}
updateClock();
setInterval(updateClock, 30000);
