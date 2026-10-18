/* PI Voice frontend: mic -> AudioWorklet -> PCM16 -> WebSocket -> Pi model. */
'use strict';

const $ = (id) => document.getElementById(id);

let ws = null;
let audioCtx = null;
let micOn = false;
let enrolling = false;
let enrollTimer = null;

// mic pipeline refs (needed to toggle AEC live)
let micStream = null;
let micTrack = null;
let micSource = null;
let workletNode = null;

// playback chain: converted audio -> gate -> limiter -> 75% cap -> speaker
const OUT_GAIN = 0.75;   // fixed cap: keeps phone-speaker feedback tame
const GATE_RMS = 0.004;  // last-stage noise gate (~-48 dBFS)
let limiter = null;
let outGain = null;
let nextPlayTime = 0;
let activeTarget = null;

// small display-only ring of recent samples for the waveform canvas
const VIS_LEN = 8192;
const visBuf = new Float32Array(VIS_LEN);
let visPos = 0;

/* ---------------- secure-context guard ---------------- */
if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
  $('ctx-warning').classList.remove('hidden');
  $('btn-mic').disabled = true;
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
  $('voice-orb').classList.toggle('live', live);
}

function connectWS() {
  if (!micOn) return;
  ws = new WebSocket(wsURL());
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    $('hud-ws').textContent = 'ON';
    $('hud-ws').dataset.state = 'on';
    setStageState('LIVE', true);
    nextPlayTime = 0;                 // drop stale schedule after reconnect
    if (audioCtx.state === 'suspended') audioCtx.resume();
    send({ type: 'start', sampleRate: audioCtx.sampleRate });
  };
  ws.onclose = () => {
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
    else if (msg.type === 'error') showError(msg.message);
  };
}

/* ---------------- results / HUD ---------------- */
function onResult(msg) {
  const el = $('speaker');
  const confidence = Math.round(msg.confidence * 100);
  if (msg.speaker === null) {
    el.textContent = 'Listening…';
    el.className = 'unknown';
    $('confidence-readout').textContent = 'Below voice threshold';
  } else {
    el.textContent = msg.speaker;
    el.className = msg.speaker === 'unknown' ? 'unknown' : '';
    $('confidence-readout').textContent = msg.speaker === 'unknown'
      ? `${confidence}% / no match`
      : `${confidence}% confidence`;
  }
  $('conf-fill').style.width = `${confidence}%`;
  $('match-readout').textContent = String(confidence).padStart(2, '0');
  $('hud-lat').textContent = `${msg.latency_ms} ms`;
  $('hud-rtf').textContent = msg.rtf;
  $('hud-voiced').textContent = msg.speaker === null ? 'NO' : 'YES';
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
  try {
    audioCtx = new AudioContext();
    await audioCtx.audioWorklet.addModule('audio-worklet.js');

    micStream = await navigator.mediaDevices.getUserMedia(
      micConstraints($('aec').checked));
    micTrack = micStream.getAudioTracks()[0];
    micSource = audioCtx.createMediaStreamSource(micStream);
    workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');
    workletNode.port.onmessage = (e) => onPCM(e.data);

    // playback chain: gate (per frame) -> limiter -> fixed 75% gain
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

    const mute = audioCtx.createGain(); // keep worklet alive, no feedback
    mute.gain.value = 0;
    micSource.connect(workletNode);
    workletNode.connect(mute);
    mute.connect(audioCtx.destination);

    micOn = true;
    connectWS();
    const micButton = $('btn-mic');
    micButton.querySelector('.control-copy strong').textContent = 'Voice session live';
    micButton.querySelector('.control-copy small').textContent = 'Audio is routing through the Pi';
    micButton.classList.add('live');
    micButton.disabled = true;
    $('speaker').textContent = 'Listening…';
    $('confidence-readout').textContent = 'Waiting for first window';
    $('btn-ptt').classList.remove('hidden');
    $('fx-row').classList.remove('hidden');
    $('enroll').classList.remove('hidden');
    drawWave();
  } catch (err) {
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
  const pcm = new Int16Array(arrayBuffer);
  let sum = 0;
  for (let i = 0; i < pcm.length; i += 4) {
    const v = pcm[i] / 32768;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / Math.max(1, pcm.length / 4));
  const f32 = new Float32Array(pcm.length);
  if (rms >= GATE_RMS) {                 // gate: silence below threshold
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
  }
  const buf = audioCtx.createBuffer(1, f32.length, audioCtx.sampleRate);
  buf.copyToChannel(f32, 0);
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(limiter);                  // limiter -> 75% cap -> speaker
  const t = Math.max(audioCtx.currentTime + 0.03, nextPlayTime);
  src.start(t);
  nextPlayTime = t + buf.duration;
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
}

/* ---------------- push-to-talk ---------------- */
const ptt = $('btn-ptt');
ptt.addEventListener('pointerdown', () => ptt.classList.add('held'));
const endPTT = () => {
  if (ptt.classList.contains('held')) {
    ptt.classList.remove('held');
    send({ type: 'final' });
  }
};
ptt.addEventListener('pointerup', endPTT);
ptt.addEventListener('pointercancel', endPTT);
ptt.addEventListener('pointerleave', endPTT);

/* ---------------- enrollment ---------------- */
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
  $('btn-enroll').disabled = true;
  let left = msg.seconds;
  const label = `Recording ${msg.name}… `;
  $('enroll-status').textContent = `${label}${left}s — talk normally`;
  enrollTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      clearInterval(enrollTimer);
      send({ type: 'enroll_stop' });
      $('enroll-status').textContent = 'Computing voice profile on the Pi…';
    } else {
      $('enroll-status').textContent = `${label}${left}s — talk normally`;
    }
  }, 1000);
}

function onEnrolled(msg) {
  enrolling = false;
  $('btn-enroll').disabled = false;
  $('enroll-status').textContent = msg.ok
    ? `Enrolled "${msg.name}".`
    : `Enroll failed: ${msg.error}`;
  if (msg.ok) {
    $('enroll-name').value = '';
    refreshProfiles();
  }
}

function onTargetSet(msg) {
  activeTarget = msg.name;
  $('target-label').textContent = msg.found ? `TARGET / ${msg.name}` : 'NO TARGET';
  refreshProfiles();
  if (!msg.found) showError(`No voice profile for "${msg.name}" — converting raw`);
}

/* ---------------- voice effects ---------------- */
function onEffectSet(msg) {
  $('hud-fx').textContent = msg.name === 'clone' && activeTarget
    ? `clone:${activeTarget}` : msg.name;
  document.querySelectorAll('.btn.fx').forEach((b) =>
    b.classList.toggle('active', b.dataset.effect === msg.name));
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
      badge.title = s.throttled ? 'Raspberry Pi thermal — THROTTLING' : 'Raspberry Pi thermal';
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
    gradient.addColorStop(0, 'rgba(100, 128, 62, 0)');
    gradient.addColorStop(.22, 'rgba(100, 128, 62, .85)');
    gradient.addColorStop(.78, 'rgba(35, 41, 31, .9)');
    gradient.addColorStop(1, 'rgba(35, 41, 31, 0)');
    g.strokeStyle = gradient;
    g.lineWidth = 2;
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

$('btn-mic').addEventListener('click', startMic);

function updateClock() {
  $('local-time').textContent = new Intl.DateTimeFormat([], {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date());
}
updateClock();
setInterval(updateClock, 30000);
