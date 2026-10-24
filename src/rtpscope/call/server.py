# server.js --> Handles context for who is where
# uuid for matching clients to "rooms"
import argparse
import base64
import hashlib
import hmac
import json
import os
import ssl
import time
import uuid
from contextlib import suppress
from pathlib import Path

# add websocket connection
from aiohttp import WSMsgType, web

STATIC = Path(__file__).parent / "static"
# mesh call --> everyone connects to everyone, so keep rooms small
MAX_PEERS = 4
# how long a turn login handed out on join stays good for
TURN_TTL = 12 * 60 * 60

# stun/turn settings --> handed to every browser when it joins
ICE = web.AppKey("ice", dict)


# client <> room data model.
# id | websocket | name
class PeerModel:
    # init instantiates a PeerModel (uuid) each time its called
    def __init__(self, ws, name):
        self.id = uuid.uuid4().hex[:4]
        self.ws = ws
        self.name = name


# room name client model id ->
rooms: dict[str, dict[str, PeerModel]] = {}
# room name --> when it started, for the lobby list
started: dict[str, float] = {}


async def index(request):
    return web.FileResponse(STATIC / "index.html")


# active meetings for the lobby --> who's in which room right now, oldest first
async def list_rooms(request):
    active = [
        {
            "room": name,
            "peers": [p.name for p in room.values()],
            "max": MAX_PEERS,
            "started": started.get(name),
        }
        for name, room in rooms.items()
        # skip rooms mid-join with nobody fully in yet
        if room
    ]
    active.sort(key=lambda r: r["started"] or 0)
    return web.json_response(active)


async def health(request):
    return web.Response(text="This message indicates the server is up")


# send but don't blow up if they already hung up
async def send(peer, message):
    with suppress(ConnectionResetError):
        await peer.ws.send_json(message)


# tell everyone in the room
async def broadcast(room, message):
    for peer in list(room.values()):
        await send(peer, message)


# ice servers for one browser --> stun as is, turn gets a short lived login.
# coturn use-auth-secret: username is "expiry:id", password is hmac(secret, username)
# so nothing has to be stored and a leaked login dies on its own
def ice_servers(ice, peer_id):
    servers = [{"urls": url} for url in ice.get("stun", [])]
    if ice.get("turn") and ice.get("turn_secret"):
        username = f"{int(time.time()) + TURN_TTL}:{peer_id}"
        digest = hmac.new(ice["turn_secret"].encode(), username.encode(), hashlib.sha1).digest()
        password = base64.b64encode(digest).decode()
        servers.append({"urls": ice["turn"], "username": username, "credential": password})
    return servers


# websocket handler
async def websocket_handler(request):
    # sdp + candidates are a few kb, no reason to accept the 4mb default
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
    await ws.prepare(request)
    # first message has to be the join --> {type: "join", room, name}
    try:
        join = await ws.receive_json()
    except (TypeError, ValueError):
        await ws.close()
        return ws
    if join.get("type") != "join" or not join.get("room"):
        await ws.send_json({"type": "error", "reason": "first message has to be a join"})
        await ws.close()
        return ws

    room_name = str(join["room"])[:64]
    room = rooms.setdefault(room_name, {})
    if len(room) >= MAX_PEERS:
        await ws.send_json({"type": "error", "reason": f"room is full ({MAX_PEERS} max)"})
        await ws.close()
        return ws

    peer = PeerModel(ws, str(join.get("name") or "anon")[:32])
    # 4 hex chars is plenty for 4 people, just reroll if it clashes
    while peer.id in room:
        peer.id = uuid.uuid4().hex[:4]

    started.setdefault(room_name, time.time())
    # everything from here is in the try --> if they drop mid-join the room still gets cleaned up
    try:
        # new peer gets who's already here --> it makes the offers, everyone else just answers
        await ws.send_json(
            {
                "type": "welcome",
                "id": peer.id,
                "peers": [{"id": p.id, "name": p.name} for p in room.values()],
                "iceServers": ice_servers(request.app[ICE], peer.id),
            }
        )
        await broadcast(room, {"type": "peer-joined", "id": peer.id, "name": peer.name})
        room[peer.id] = peer
        print(f"{peer.name} ({peer.id}) joined {room_name} [{len(room)}/{MAX_PEERS}]")

        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(message.data)
            except ValueError:
                continue
            # signal --> sdp offer/answer or ice candidate, just pass it on to who it's for
            if data.get("type") == "signal" and data.get("to") in room:
                relay = {"type": "signal", "from": peer.id, "data": data.get("data")}
                await send(room[data["to"]], relay)
    finally:
        room.pop(peer.id, None)
        if not room:
            rooms.pop(room_name, None)
            started.pop(room_name, None)
        await broadcast(room, {"type": "peer-left", "id": peer.id})
        print(f"{peer.name} ({peer.id}) left {room_name}")
    return ws


# build server app without starting it
def create_call_app(stun=None, turn=None, turn_secret=None):
    app = web.Application()
    app[ICE] = {"stun": stun or [], "turn": turn or [], "turn_secret": turn_secret}
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/rooms", list_rooms)
    # register websocket connection
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/static", STATIC)
    return app


# comma separated env var --> list
def env_list(name):
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def main():
    parser = argparse.ArgumentParser(description="rtpscope call server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8087)
    # camera only works on https or localhost --> cert for joining from another machine
    parser.add_argument("--cert", help="TLS cert (pem)")
    parser.add_argument("--key", help="TLS private key (pem)")
    # stun/turn urls come from flags or env (the vps sets them in /etc/rtpscope/call.env)
    parser.add_argument("--stun", action="append", default=env_list("STUN_URL"))
    parser.add_argument("--turn", action="append", default=env_list("TURN_URL"))
    args = parser.parse_args()
    # secret only from env so it doesn't show up in ps
    turn_secret = os.environ.get("TURN_SECRET")
    if args.turn and not turn_secret:
        parser.error("--turn needs TURN_SECRET set in the environment")

    ssl_context = None
    if args.cert:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(args.cert, args.key)

    app = create_call_app(stun=args.stun, turn=args.turn, turn_secret=turn_secret)
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
