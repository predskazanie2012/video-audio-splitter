"""Local request boundary. Not a replacement for hosted authentication."""
from urllib.parse import urlsplit

LOOPBACK = {"localhost", "127.0.0.1", "::1", "testclient"}

def permitted(host, client, origin, fetch_site, extra_origins=()):
    try:
        hostname = urlsplit("http://" + host).hostname
    except ValueError:
        return False
    if hostname not in LOOPBACK or client not in LOOPBACK:
        return False
    if origin:
        try:
            value = urlsplit(origin)
            if not (value.scheme in {"http", "https"} and value.netloc == host) and origin not in extra_origins:
                return False
        except ValueError:
            return False
    if fetch_site == "cross-site" and origin not in extra_origins:
        return False
    return True

class LocalOnly:
    def __init__(self, app, extra_origins=()):
        self.app, self.extra_origins = app, tuple(extra_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        client = (scope.get("client") or ("",))[0]
        if not permitted(headers.get("host", ""), client, headers.get("origin", ""), headers.get("sec-fetch-site", ""), self.extra_origins):
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            from starlette.responses import JSONResponse
            return await JSONResponse({"detail": "This application accepts local requests only."}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)

def protect_flask(app):
    from flask import request, abort
    @app.before_request
    def _local_only():
        if not permitted(request.host, request.remote_addr or "", request.headers.get("Origin", ""), request.headers.get("Sec-Fetch-Site", "")):
            abort(403)
