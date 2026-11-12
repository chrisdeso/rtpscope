# client.py --> joins a call room like a browser does, but only receives
#
# same signaling as call.js (see the README's "HTTP + websocket" section):
#   join --> welcome{peers}: we're the newcomer, so we offer to everyone already there
#   peer-joined: someone newer --> they'll offer to us, we answer
# our offers are recvonly (audio + video), so the browsers send and we never do.
# aiortc gathers all its ice candidates before setLocalDescription returns, so our sdp already
# has them. the browser trickles its candidates, we add them as they come.

import asyncio
import logging
import ssl

import aiohttp
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCIceTransportPolicy,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

from . import hooks

log = logging.getLogger(__name__)


class JoinError(Exception):
    pass


def ws_url(server):
    """http(s)://host:port --> ws(s)://host:port/ws"""
    url = server.rstrip("/")
    if url.startswith("http"):
        url = "ws" + url[4:]
    elif not url.startswith("ws"):
        url = "ws://" + url
    return url if url.endswith("/ws") else url + "/ws"


def ice_config(servers, relay_only=False):
    ice = [
        RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
        for s in servers
    ]
    policy = RTCIceTransportPolicy.RELAY if relay_only else RTCIceTransportPolicy.ALL
    return RTCConfiguration(iceServers=ice, iceTransportPolicy=policy)


def parse_candidate(data):
    """browser RTCIceCandidate json --> aiortc candidate, None for end-of-candidates"""
    line = data.get("candidate") or ""
    if not line:
        return None
    candidate = candidate_from_sdp(line.split(":", 1)[1] if line.startswith("candidate:") else line)
    candidate.sdpMid = data.get("sdpMid")
    candidate.sdpMLineIndex = data.get("sdpMLineIndex")
    return candidate


class CallClient:
    def __init__(self, server, room, name, receiver, relay_only=False, verify_tls=True):
        hooks.install()  # before any peer connection exists
        self.url = ws_url(server)
        self.room = room
        self.name = name
        self.receiver = receiver
        self.relay_only = relay_only
        self.verify_tls = verify_tls
        self.ws = None
        self.id = None
        self.ice_servers = []
        self.pcs: dict[str, RTCPeerConnection] = {}
        self.names: dict[str, str] = {}

    async def send(self, message):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_json(message)

    async def run(self):
        """join and handle signaling until the server goes away or we're cancelled"""
        ssl_context = None
        if self.url.startswith("wss") and not self.verify_tls:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(self.url, ssl=ssl_context if ssl_context else True) as ws,
        ):
            self.ws = ws
            await self.send({"type": "join", "room": self.room, "name": self.name})
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    await self.on_message(msg.json())
            finally:
                await self.close()

    async def on_message(self, message):
        kind = message.get("type")
        if kind == "welcome":
            self.id = message["id"]
            self.ice_servers = message.get("iceServers") or []
            log.info("joined %s as %s, %d already here", self.room, self.id, len(message["peers"]))
            for peer in message["peers"]:
                await self.offer(peer["id"], peer["name"])
        elif kind == "peer-joined":
            log.info("%s joined", message["name"])
            self.connect(message["id"], message["name"])
        elif kind == "signal":
            await self.on_signal(message["from"], message.get("data") or {})
        elif kind == "peer-left":
            log.info("%s left", self.names.get(message["id"], message["id"]))
            await self.hangup(message["id"])
        elif kind == "error":
            raise JoinError(message.get("reason", "server said no"))

    def connect(self, peer_id, name):
        pc = RTCPeerConnection(ice_config(self.ice_servers, self.relay_only))
        self.pcs[peer_id] = pc
        self.names[peer_id] = name

        @pc.on("track")
        def on_track(track):
            receiver = next(
                (t.receiver for t in pc.getTransceivers() if t.receiver.track is track), None
            )
            clock = hooks.clock_for(receiver) if receiver is not None else None
            log.info("receiving %s from %s", track.kind, name)
            self.receiver.peer(peer_id, name).attach(track, clock)

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("%s: %s", name, pc.connectionState)
            if pc.connectionState == "failed":
                log.warning("couldn't reach %s (might need TURN)", name)

        return pc

    async def offer(self, peer_id, name):
        pc = self.connect(peer_id, name)
        pc.addTransceiver("audio", direction="recvonly")
        pc.addTransceiver("video", direction="recvonly")
        await pc.setLocalDescription(await pc.createOffer())
        await self.send_sdp(peer_id, pc)

    async def send_sdp(self, peer_id, pc):
        sdp = pc.localDescription
        await self.send(
            {"type": "signal", "to": peer_id, "data": {"sdp": {"type": sdp.type, "sdp": sdp.sdp}}}
        )

    async def on_signal(self, peer_id, data):
        pc = self.pcs.get(peer_id)
        if pc is None:
            return
        if data.get("sdp"):
            sdp = data["sdp"]
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp["sdp"], type=sdp["type"]))
            if sdp["type"] == "offer":
                # their offer is sendrecv, aiortc answers recvonly since we add no tracks
                await pc.setLocalDescription(await pc.createAnswer())
                await self.send_sdp(peer_id, pc)
        elif data.get("candidate") is not None:
            candidate = parse_candidate(data["candidate"])
            if candidate is not None:
                await pc.addIceCandidate(candidate)

    async def hangup(self, peer_id):
        pc = self.pcs.pop(peer_id, None)
        self.names.pop(peer_id, None)
        if pc is not None:
            await pc.close()
        await self.receiver.remove(peer_id)

    async def close(self):
        await asyncio.gather(*(self.hangup(p) for p in list(self.pcs)), return_exceptions=True)
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
