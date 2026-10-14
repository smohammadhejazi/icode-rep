"""Host-side gateway between the instance containers and the Anthropic API.

The real API key never enters a container. iCode, inside the container, is
pointed at this gateway (plain HTTP on the internal network's gateway address)
with a random per-run token as its "API key". The gateway accepts only
`POST /v1/messages*` carrying that token, swaps the token for the real key and
streams the request to https://api.anthropic.com and the response back.

The agent can read the token (it sits in iCode's model profile in the
container), so during a run it could make model calls through the gateway, as
iCode does; it can never see the key, and the token is worthless after the run.
"""

import asyncio
import logging
import secrets
import threading

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

UPSTREAM = "https://api.anthropic.com"
# Hop-by-hop headers, plus the ones the gateway sets itself.
_DROP_REQUEST = {"host", "connection", "keep-alive", "proxy-authorization", "te", "trailer", "transfer-encoding",
                 "upgrade", "content-length", "x-api-key", "authorization"}
_DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "content-length", "trailer", "upgrade"}

log = logging.getLogger("swepro.gateway")


class Gateway:
    def __init__(self, listen_host: str, api_key: str):
        self.listen_host = listen_host
        self.token = "swepro-" + secrets.token_urlsafe(32)
        self._api_key = api_key
        self.port: int | None = None
        self.denied = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="swepro-gateway", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.listen_host}:{self.port}"

    def start(self) -> "Gateway":
        self._thread.start()
        self._ready.wait(30)
        if self._error or self.port is None:
            raise RuntimeError(f"Gateway did not start: {self._error}")
        return self

    def stop(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(30)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as e:  # reported to start() through _error
            self._error = e
            self._ready.set()

    async def _serve(self) -> None:
        self._stop = asyncio.Event()
        timeout = ClientTimeout(total=None, connect=30, sock_read=900)
        async with ClientSession(timeout=timeout, auto_decompress=False, connector=TCPConnector(limit=64)) as session:
            self._session = session
            app = web.Application(client_max_size=64 * 1024 * 1024)
            app.router.add_route("*", "/{tail:.*}", self._handle)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, self.listen_host, 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self._ready.set()
            try:
                await self._stop.wait()
            finally:
                await runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if not secrets.compare_digest(request.headers.get("x-api-key", ""), self.token):
            self.denied += 1
            log.warning("gateway: refused %s %s (bad token)", request.method, request.path)
            return web.json_response({"type": "error", "error": {"type": "authentication_error",
                                                                  "message": "invalid gateway token"}}, status=401)
        if request.method != "POST" or not request.path.startswith("/v1/messages"):
            self.denied += 1
            log.warning("gateway: refused %s %s (not allowed)", request.method, request.path)
            return web.json_response({"type": "error", "error": {"type": "permission_error",
                                                                  "message": "not allowed by the gateway"}}, status=403)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
        headers["x-api-key"] = self._api_key
        body = await request.read()
        async with self._session.post(UPSTREAM + request.path_qs, data=body, headers=headers) as upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for k, v in upstream.headers.items():
                if k.lower() not in _DROP_RESPONSE:
                    response.headers.add(k, v)
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
