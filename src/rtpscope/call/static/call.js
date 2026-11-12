// call.js --> browser side of the call: media, signaling, one RTCPeerConnection per person, ui

// ask for 720p30, the browser falls back to whatever the camera can do
const VIDEO = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } };

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
// ?relay=1 --> force everything through turn, for checking the vps relay actually works
const RELAY_ONLY = params.has("relay");

// peer id --> { name, pc, tile }
const peers = new Map();
let ws = null;
let localStream = null;
let localTile = null;
let stopPattern = null;
let iceServers = [];
// handle server messages one at a time --> a candidate can't race ahead of its sdp
let inbox = Promise.resolve();

// ---- media

// ---- test pattern --> canvas video + beep audio for measured runs (and machines without a camera)
// every frame: a barcode of Date.now() across the top, and a square that flashes white for the
// first 100 ms of each second. audio: a 1 kHz beep for the first 100 ms of each second.
// the python receiver reads them: barcode --> end to end delay, flash vs beep --> a/v sync.
// layout + numbers must match src/rtpscope/receiver/pattern.py

const PATTERN = {
  guard: [1, 0],
  dataBits: 42, // ms since epoch
  crcBits: 8,
  barcodeY: [0, 0.06],
  flashBox: [0.86, 0.1, 0.98, 0.3], // x0, y0, x1, y1
  flashMs: 100,
  beepHz: 1000,
  beepMs: 100,
};
PATTERN.cells = PATTERN.guard.length + PATTERN.dataBits + PATTERN.crcBits;

// crc-8, poly 0x07, over bits msb first
function crc8(bits) {
  let crc = 0;
  for (const bit of bits) {
    const feedback = ((crc >> 7) & 1) ^ bit;
    crc = (crc << 1) & 0xff;
    if (feedback) crc ^= 0x07;
  }
  return crc;
}

function barcodeBits(ms) {
  // 42 bits doesn't fit in a 32 bit int --> divide instead of shifting
  const data = [];
  for (let i = PATTERN.dataBits - 1; i >= 0; i--) data.push(Math.floor(ms / 2 ** i) % 2);
  const crc = crc8(data);
  const check = [];
  for (let i = PATTERN.crcBits - 1; i >= 0; i--) check.push((crc >> i) & 1);
  return [...PATTERN.guard, ...data, ...check];
}

function startPattern(label) {
  const canvas = Object.assign(document.createElement("canvas"), { width: 1280, height: 720 });
  const ctx = canvas.getContext("2d");
  const { width: w, height: h } = canvas;
  const bars = ["#c0c0c0", "#c0c000", "#00c0c0", "#00c000", "#c000c0", "#c00000", "#0000c0"];
  const [by0, by1] = PATTERN.barcodeY.map((y) => Math.round(y * h));
  const [fx0, fy0, fx1, fy1] = PATTERN.flashBox.map((v, i) => Math.round(v * (i % 2 ? h : w)));
  let frame = 0;
  const draw = () => {
    const ms = Date.now();
    bars.forEach((color, i) => {
      ctx.fillStyle = color;
      ctx.fillRect((i * w) / 7, by1, w / 7 + 1, h * 0.75 - by1);
    });
    // barcode: one full-height column per bit, white = 1
    barcodeBits(ms).forEach((bit, i) => {
      const x0 = Math.round((i * w) / PATTERN.cells);
      const x1 = Math.round(((i + 1) * w) / PATTERN.cells);
      ctx.fillStyle = bit ? "#fff" : "#000";
      ctx.fillRect(x0, by0, x1 - x0, by1 - by0);
    });
    ctx.fillStyle = ms % 1000 < PATTERN.flashMs ? "#fff" : "#000";
    ctx.fillRect(fx0, fy0, fx1 - fx0, fy1 - fy0);
    ctx.fillStyle = "#111";
    ctx.fillRect(0, h * 0.75, w, h * 0.25);
    // moving block so you can tell it's live (and spot freezes)
    ctx.fillStyle = "#fff";
    ctx.fillRect((frame * 8) % w, h * 0.75, 24, h * 0.25);
    // clock + label bottom-right, the name chip covers bottom-left
    ctx.textAlign = "right";
    ctx.font = "bold 44px monospace";
    ctx.fillText(`${label}  #${frame}`, w - 40, h * 0.86);
    ctx.font = "32px monospace";
    ctx.fillText(new Date(ms).toISOString().slice(11, 23), w - 40, h * 0.95);
    frame++;
  };
  draw();
  const timer = setInterval(draw, 1000 / 30);
  stopPattern = () => clearInterval(timer);
  return canvas.captureStream(30);
}

// one beep at audio time `at`: open the gain for beepMs, with 2 ms ramps so it doesn't click
function beep(gain, at) {
  const seconds = PATTERN.beepMs / 1000;
  gain.setValueAtTime(0, at);
  gain.linearRampToValueAtTime(0.5, at + 0.002);
  gain.setValueAtTime(0.5, at + seconds - 0.002);
  gain.linearRampToValueAtTime(0, at + seconds);
}

// pattern audio track: beeps on each Date.now() second (same clock as the flash) + the mic if any.
// the audio clock and Date.now() aren't linked, so line them up again every time we schedule
async function patternAudio(stream) {
  // made before awaiting the mic prompt --> still inside the join click, so it's allowed to start
  const ctx = new AudioContext({ sampleRate: 48000 });
  const out = ctx.createMediaStreamDestination();
  // one oscillator that never stops, gated by a gain. a destination with nothing playing into it
  // stops producing audio at all, and the track would come out ~half as long as real time
  const osc = new OscillatorNode(ctx, { frequency: PATTERN.beepHz });
  const gate = new GainNode(ctx, { gain: 0 });
  osc.connect(gate).connect(out);
  osc.start();
  let next = Math.ceil(Date.now() / 1000) * 1000;
  const schedule = () => {
    const wall = Date.now();
    const audioNow = ctx.currentTime;
    // skip seconds we've already missed (tab was in the background)
    if (next < wall) next = Math.ceil(wall / 1000) * 1000;
    for (; next < wall + 500; next += 1000) beep(gate.gain, audioNow + (next - wall) / 1000);
  };
  schedule();
  const timer = setInterval(schedule, 200);
  let mic = null;
  try {
    mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    ctx.createMediaStreamSource(mic).connect(out);
  } catch {}
  ctx.resume().catch(() => {});
  stream.addTrack(out.stream.getAudioTracks()[0]);
  return () => {
    clearInterval(timer);
    mic?.getTracks().forEach((track) => track.stop());
    ctx.close();
  };
}

async function getMedia(usePattern, name) {
  if (!window.isSecureContext) {
    toast("camera needs https or localhost, using a test pattern");
    usePattern = true;
  }
  if (!usePattern) {
    try {
      return { stream: await navigator.mediaDevices.getUserMedia({ video: VIDEO, audio: true }), pattern: false };
    } catch (err) {
      toast(`camera unavailable (${err.name}), using a test pattern`);
    }
  }
  const stream = startPattern(name);
  const stopVideo = stopPattern;
  // beeps always, the mic mixed in if there is one
  const stopAudio = await patternAudio(stream);
  stopPattern = () => {
    stopVideo();
    stopAudio();
  };
  return { stream, pattern: true };
}

// ---- signaling

function send(message) {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
}

async function join(room, name, usePattern) {
  const media = await getMedia(usePattern, name);
  localStream = media.stream;
  localTile = addTile(`${name} (you)`, localStream, { local: true, pattern: media.pattern });

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  setConn("connecting");
  ws.onopen = () => send({ type: "join", room, name });
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    inbox = inbox.then(() => onMessage(message, room)).catch((err) => console.error(err));
  };
  ws.onclose = () => {
    setConn("closed");
    toast("lost connection to the server");
    leave();
  };
}

async function onMessage(message, room) {
  switch (message.type) {
    // we're in --> offer to everyone already here
    case "welcome":
      iceServers = message.iceServers ?? [];
      setConn("open");
      showCall(room);
      for (const p of message.peers) {
        const { pc } = connect(p.id, p.name);
        await pc.setLocalDescription();
        send({ type: "signal", to: p.id, data: { sdp: pc.localDescription } });
      }
      break;
    // someone new --> set up the pc and wait for their offer
    case "peer-joined":
      connect(message.id, message.name);
      toast(`${message.name} joined`);
      break;
    case "signal":
      await onSignal(message.from, message.data);
      break;
    case "peer-left":
      if (peers.has(message.id)) toast(`${peers.get(message.id).name} left`);
      hangup(message.id);
      break;
    case "error":
      showJoinError(message.reason);
      leave();
      break;
  }
}

async function onSignal(from, data) {
  const peer = peers.get(from);
  if (!peer || !data) return;
  const { pc } = peer;
  if (data.sdp) {
    await pc.setRemoteDescription(data.sdp);
    if (data.sdp.type === "offer") {
      await pc.setLocalDescription();
      send({ type: "signal", to: from, data: { sdp: pc.localDescription } });
    }
  } else if (data.candidate) {
    await pc.addIceCandidate(data.candidate);
  }
}

// ---- peer connections

function connect(id, name) {
  const pc = new RTCPeerConnection({ iceServers, iceTransportPolicy: RELAY_ONLY ? "relay" : "all" });
  for (const track of localStream.getTracks()) pc.addTrack(track, localStream);

  const tile = addTile(name, null);
  const peer = { name, pc, tile };
  peers.set(id, peer);

  pc.onicecandidate = ({ candidate }) => {
    if (candidate) send({ type: "signal", to: id, data: { candidate } });
  };
  pc.ontrack = ({ streams }) => {
    tile.querySelector("video").srcObject = streams[0];
  };
  pc.onconnectionstatechange = () => {
    tile.querySelector(".dot").className = `dot ${pc.connectionState}`;
    if (pc.connectionState === "failed") toast(`couldn't reach ${name} (might need TURN)`);
  };
  return peer;
}

function hangup(id) {
  const peer = peers.get(id);
  if (!peer) return;
  peer.pc.close();
  peer.tile.remove();
  peers.delete(id);
}

function leave() {
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
  for (const id of [...peers.keys()]) hangup(id);
  localStream?.getTracks().forEach((track) => track.stop());
  localStream = null;
  stopPattern?.();
  stopPattern = null;
  localTile?.remove();
  localTile = null;
  inbox = Promise.resolve();
  setConn("");
  $("call").hidden = true;
  $("lobby").hidden = false;
  $("room-pill").hidden = true;
  $("mic").classList.remove("off");
  $("cam").classList.remove("off");
  // back in the lobby --> don't wait for the next poll
  refreshMeetings();
}

// ---- ui

function addTile(name, stream, { local = false, pattern = false } = {}) {
  const tile = $("tile-template").content.firstElementChild.cloneNode(true);
  const video = tile.querySelector("video");
  tile.querySelector(".name").textContent = name;
  tile.classList.toggle("local", local);
  tile.classList.toggle("pattern", pattern);
  if (local) {
    // never play our own mic back
    video.muted = true;
    tile.querySelector(".dot").className = "dot connected";
  }
  if (stream) video.srcObject = stream;
  $("grid").append(tile);
  return tile;
}

function showCall(room) {
  history.replaceState(null, "", `?room=${encodeURIComponent(room)}${RELAY_ONLY ? "&relay=1" : ""}`);
  $("lobby").hidden = true;
  $("call").hidden = false;
  $("room-pill").textContent = `room ${room}${RELAY_ONLY ? " · relay only" : ""}`;
  $("room-pill").hidden = false;
}

function setConn(state) {
  $("conn").className = `dot ${state}`;
  $("conn").title = `server: ${state || "offline"}`;
}

function showJoinError(reason) {
  $("join-error").textContent = reason;
  $("join-error").hidden = false;
}

let toastTimer = null;
function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 3000);
}

// mute / camera off without renegotiating (disabled video just sends black frames)
function toggle(kind, button) {
  const tracks = localStream?.getTracks().filter((t) => t.kind === kind) ?? [];
  if (!tracks.length) return toast(`no ${kind === "audio" ? "mic" : "camera"}`);
  const enabled = !tracks[0].enabled;
  tracks.forEach((t) => (t.enabled = enabled));
  button.classList.toggle("off", !enabled);
}

// ---- active meetings --> poll /rooms while the lobby is showing

const MEETINGS_INTERVAL_MS = 3000;

// seconds since epoch --> "just started" / "12m" / "1h 5m"
function since(started) {
  const mins = Math.floor((Date.now() / 1000 - started) / 60);
  if (!started || mins < 1) return "just started";
  return mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// one row per room. textContent only --> room + people names come from other users
function meetingRow(meeting) {
  const full = meeting.peers.length >= meeting.max;
  const row = el("li", `meeting${full ? " full" : ""}`);
  // long name lists get cut off, full list on hover
  row.title = meeting.peers.join(", ");
  const info = el("div", "info");
  const who = el("div", "who");
  who.append(el("span", "count", `${meeting.peers.length}/${meeting.max} · ${since(meeting.started)}`), ` · ${meeting.peers.join(", ")}`);
  info.append(el("div", "room", meeting.room), who);
  const button = el("button", full ? "" : "primary", full ? "Full" : "Join");
  button.disabled = full;
  button.onclick = () => pickMeeting(meeting.room);
  row.append(el("span", "live"), info, button);
  return row;
}

async function refreshMeetings() {
  if ($("lobby").hidden) return;
  let meetings;
  try {
    const res = await fetch("/rooms", { cache: "no-store" });
    if (!res.ok) return;
    meetings = await res.json();
  } catch {
    return;
  }
  $("meetings").replaceChildren(...meetings.map(meetingRow));
  $("meeting-count").textContent = meetings.length;
  $("no-meetings").hidden = meetings.length > 0;
}

// click a meeting --> join straight away if we have a name, otherwise ask for one
function pickMeeting(room) {
  $("room").value = room;
  if ($("name").value.trim()) {
    $("join-form").requestSubmit();
  } else {
    $("name").focus();
    toast(`enter your name to join ${room}`);
  }
}

// ---- wiring

$("room").value = params.get("room") ?? "";
try {
  $("name").value = localStorage.getItem("rtpscope.name") ?? "";
} catch {}

$("join-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("join-error").hidden = true;
  const room = $("room").value.trim();
  const name = $("name").value.trim();
  try {
    localStorage.setItem("rtpscope.name", name);
  } catch {}
  // not event.submitter, it's null when a meeting row submits the form
  const button = $("join-form").querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await join(room, name, $("pattern").checked);
  } catch (err) {
    showJoinError(`could not join: ${err.message ?? err}`);
    leave();
  } finally {
    button.disabled = false;
  }
});

$("mic").onclick = (e) => toggle("audio", e.currentTarget);
$("cam").onclick = (e) => toggle("video", e.currentTarget);
$("copy").onclick = async () => {
  try {
    await navigator.clipboard.writeText(location.href);
    toast("invite link copied");
  } catch {
    toast(location.href);
  }
};
$("leave").onclick = () => leave();
window.addEventListener("beforeunload", () => leave());

refreshMeetings();
setInterval(refreshMeetings, MEETINGS_INTERVAL_MS);
