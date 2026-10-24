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

// test pattern --> canvas video for machines without a camera
function startPattern(label) {
  const canvas = Object.assign(document.createElement("canvas"), { width: 1280, height: 720 });
  const ctx = canvas.getContext("2d");
  const { width: w, height: h } = canvas;
  const bars = ["#c0c0c0", "#c0c000", "#00c0c0", "#00c000", "#c000c0", "#c00000", "#0000c0"];
  let frame = 0;
  const draw = () => {
    bars.forEach((color, i) => {
      ctx.fillStyle = color;
      ctx.fillRect((i * w) / 7, 0, w / 7 + 1, h * 0.75);
    });
    ctx.fillStyle = "#111";
    ctx.fillRect(0, h * 0.75, w, h * 0.25);
    // moving block so you can tell it's live (and spot freezes)
    ctx.fillStyle = "#fff";
    ctx.fillRect((frame * 8) % w, h * 0.75, 24, h * 0.25);
    // label bottom-right, the name chip covers bottom-left
    ctx.font = "bold 44px monospace";
    ctx.textAlign = "right";
    ctx.fillText(`${label}  #${frame}`, w - 40, h * 0.9);
    frame++;
  };
  draw();
  const timer = setInterval(draw, 1000 / 30);
  stopPattern = () => clearInterval(timer);
  return canvas.captureStream(30);
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
  // mic is optional with the pattern
  try {
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.addTrack(mic.getAudioTracks()[0]);
  } catch {}
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
