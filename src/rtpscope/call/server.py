# server.js --> Handles context for who is where

from aiohttp import web


async def index(request):
    return web.Response(text="This message indicates the server is up")


# build server app without starting it
def create_call_app():
    app = web.Application()
    app.router.add_get("/", index)
    return app


if __name__ == "__main__":
    web.run_app(create_call_app(), port=8087)
