import asyncio
import base64
import hashlib
import hmac
import time

from aiohttp.test_utils import TestClient, TestServer

from rtpscope.call import server
from rtpscope.call.server import MAX_PEERS, create_call_app


# spin the app up on a random port for one test, no pytest-aiohttp needed
def run(test, app=None):
    async def main():
        async with TestClient(TestServer(app or create_call_app())) as client:
            await test(client)

    asyncio.run(main())


async def join(client, room="lab", name="a"):
    ws = await client.ws_connect("/ws")
    await ws.send_json({"type": "join", "room": room, "name": name})
    return ws, await ws.receive_json(timeout=2)


def test_pages_served():
    async def test(client):
        assert (await client.get("/health")).status == 200
        assert "rtpscope" in await (await client.get("/")).text()
        assert (await client.get("/static/call.js")).status == 200

    run(test)


def test_join_relay_leave():
    async def test(client):
        a, welcome_a = await join(client, name="alice")
        assert welcome_a["type"] == "welcome"
        assert welcome_a["peers"] == []

        b, welcome_b = await join(client, name="bob")
        assert welcome_b["peers"] == [{"id": welcome_a["id"], "name": "alice"}]
        joined = await a.receive_json(timeout=2)
        assert joined == {"type": "peer-joined", "id": welcome_b["id"], "name": "bob"}

        # bob offers to alice --> server passes it on with bob's id as "from"
        offer = {"sdp": {"type": "offer", "sdp": "v=0"}}
        await b.send_json({"type": "signal", "to": welcome_a["id"], "data": offer})
        relayed = await a.receive_json(timeout=2)
        assert relayed == {"type": "signal", "from": welcome_b["id"], "data": offer}

        await b.close()
        assert await a.receive_json(timeout=2) == {"type": "peer-left", "id": welcome_b["id"]}
        await a.close()

    run(test)
    assert server.rooms == {}


def test_rooms_are_separate():
    async def test(client):
        a, _ = await join(client, room="one")
        b, welcome_b = await join(client, room="two")
        assert welcome_b["peers"] == []
        await a.close()
        await b.close()

    run(test)


def test_room_full():
    async def test(client):
        sockets = [(await join(client, name=str(i)))[0] for i in range(MAX_PEERS)]
        _, reply = await join(client, name="late")
        assert reply["type"] == "error"
        for ws in sockets:
            await ws.close()

    run(test)


def test_first_message_must_be_join():
    async def test(client):
        ws = await client.ws_connect("/ws")
        await ws.send_json({"type": "signal"})
        assert (await ws.receive_json(timeout=2))["type"] == "error"

    run(test)


def test_no_ice_servers_by_default():
    async def test(client):
        a, welcome = await join(client)
        assert welcome["iceServers"] == []
        await a.close()

    run(test)


# turn login has to be what coturn's use-auth-secret expects or browsers silently can't relay
def test_turn_credentials():
    async def test(client):
        a, welcome = await join(client)
        stun, turn = welcome["iceServers"]
        assert stun == {"urls": "stun:192.0.2.1:3478"}
        assert turn["urls"] == ["turn:192.0.2.1:3478"]

        expiry, peer_id = turn["username"].split(":")
        assert peer_id == welcome["id"]
        assert int(expiry) > time.time()
        digest = hmac.new(b"s3cret", turn["username"].encode(), hashlib.sha1).digest()
        assert turn["credential"] == base64.b64encode(digest).decode()
        await a.close()

    app = create_call_app(
        stun=["stun:192.0.2.1:3478"], turn=["turn:192.0.2.1:3478"], turn_secret="s3cret"
    )
    run(test, app)
