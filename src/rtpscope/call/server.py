# server.js --> Handles context for who is where
# add websocket connection
from aiohttp import WSMsgType, web


async def index(request):
    return web.Response(text="This message indicates the server is up")


# websocket handler
async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    print("web socket client connected")

    async for message in ws:
        if message.type != WSMsgType.TEXT:
            continue
        await ws.send_str(f"echo: {message.data}")

    print("web socket client disconnected")
    return ws


# build server app without starting it
def create_call_app():
    app = web.Application()
    app.router.add_get("/", index)
    # register websocket connection
    app.router.add_get("/ws", websocket_handler)
    return app


if __name__ == "__main__":
    web.run_app(create_call_app(), port=8087)
