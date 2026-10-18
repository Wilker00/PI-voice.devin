/* PI Voice frontend: mic -> AudioWorklet -> PCM16 -> WebSocket -> Pi model. */
'use strict';

const $ = (id) => document.getElementById(id);

let ws = null;
let audioCtx = null;
let micOn = false;
let enrolling = false;
let enrollTimer = null;

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

function connectWS() {
  if (!micOn) return;
  ws = new WebSocket(wsURL());
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    $('hud-ws').textContent = 'on';
    send({ type: 'start', sampleRate: audioCtx.sampleRate });
  };
  ws.onclose = () => {
    $('hud-ws').textContent = 'off';
    if (micOn) setTimeout(connectWS, 3000); // retry while mic is live
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'result') onResult(msg);
    else if (msg.type === 'enroll_started') onEnrollStarted(msg);
    else if (msg.type === 'enrolled') onEnrolled(msg);
    else if (msg.type === 'error') showError(msg.message);
  };
}

/* ---------------- results / HUD ---------------- */
function onResult(msg) {
  const el = $('speaker');
  if (msg.speaker === null) {
    el.textContent = 'listening…';
    el.className = 'unknown';
  } else {
    el.textContent = msg.speaker;
    el.className = msg.speaker === 'unknown' ? 'unknown' : '';
  }
  $('conf-fill').style.width = `${Math.round(msg.confidence * 100)}%`;
  $('hud-lat').textContent = `${msg.latency_ms} ms`;
  $('hud-rtf').textContent = msg.rtf;
  $('hud-voiced').textContent = msg.speaker === null ? 'no' : 'yes';
}

/* ---------------- mic pipeline ---------------- */
async function startMic() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: 1,
      },
    });
    audioCtx = new AudioContext();
    await audioCtx.audioWorklet.addModule('audio-worklet.js');
    const src = audioCtx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(audioCtx, 'pcm-processor');
    node.port.onmessage = (e) => onPCM(e.data);
    const mute = audioCtx.createGain(); // keep worklet alive, no feedback
    mute.gain.value = 0;
    src.connect(node);
    node.connect(mute);
    mute.connect(audioCtx.destination);
    micOn = true;
    connectWS();
    $('btn-mic').textContent = 'Mic live';
    $('btn-mic').disabled = true;
    $('btn-ptt').classList.remove('hidden');
    $('enroll').classList.remove('hidden');
    drawWave();
  } catch (err) {
    showError(`getUserMedia failed: ${err.name} ${err.message}`);
  }
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
  $('level-fill').style.width = `${Math.min(100, peak * 140)}%`;
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

function onEnrollStarted(msg) {
  enrolling = true;
  let left = msg.seconds;
  const label = `Recording ${msg.name}… `;
  $('enroll-status').textContent = `${label}${left}s — talk normally`;
  enrollTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      clearInterval(enrollTimer);
      send({ type: 'enroll_stop' });
      $('enroll-status').textContent = 'computing embedding on the Pi…';
    } else {
      $('enroll-status').textContent = `${label}${left}s — talk normally`;
    }
  }, 1000);
}

function onEnrolled(msg) {
  enrolling = false;
  $('enroll-status').textContent = msg.ok
    ? `Enrolled "${msg.name}".`
    : `Enroll failed: ${msg.error}`;
  if (msg.ok) refreshProfiles();
}

async function refreshProfiles() {
  try {
    const res = await fetch('/api/profiles');
    const { profiles } = await res.json();
    const ul = $('profiles');
    ul.innerHTML = '';
    for (const name of profiles) {
      const li = document.createElement('li');
      const span = document.createElement('span');
      span.textContent = name;
      const del = document.createElement('button');
      del.className = 'del';
      del.textContent = '×';
      del.onclick = async () => {
        await fetch(`/api/profiles/${encodeURIComponent(name)}`, { method: 'DELETE' });
        refreshProfiles();
      };
      li.append(span, del);
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
      badge.textContent = `Pi ${s.temp_c.toFixed(1)}°C`;
      badge.className = 'badge' + (s.throttled ? ' throttled' : s.temp_c > 70 ? ' hot' : '');
      if (s.throttled) badge.title += ' — THROTTLING';
    } else {
      badge.textContent = 'Pi n/a';
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
    g.fillStyle = '#161b22';
    g.fillRect(0, 0, w, h);
    g.strokeStyle = '#58a6ff';
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
