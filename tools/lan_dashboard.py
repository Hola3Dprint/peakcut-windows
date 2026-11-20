"""Serve the full PeakCut dashboard on the current Wi-Fi subnet."""

import argparse
from ipaddress import IPv4Address, IPv4Interface

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, web
from multidict import CIMultiDict
from yarl import URL


UPSTREAM = "http://127.0.0.1:8765"
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection"}
PC_ONLY = {"/api/storage/open", "/api/storage/pick"}
SESSION = web.AppKey("upstream", ClientSession)


def proxy_headers(headers):
    connection = ",".join(headers.getall("Connection", []))
    excluded = HOP_HEADERS | {name.strip().lower() for name in connection.split(",")}
    return CIMultiDict((name, value) for name, value in headers.items() if name.lower() not in excluded)


def dashboard(interface, port):
    app = web.Application(handler_args={"auto_decompress": False})
    expected_host = f"{interface.ip}:{port}"

    async def upstream_session(application):
        async with ClientSession(auto_decompress=False, cookie_jar=DummyCookieJar(),
                                 timeout=ClientTimeout(total=None, sock_connect=5, sock_read=600)) as session:
            application[SESSION] = session
            yield

    async def relay(request):
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if not peer or IPv4Address(peer[0]) not in interface.network or request.host != expected_host:
            raise web.HTTPForbidden()
        if request.path.rstrip("/") in PC_ONLY:
            raise web.HTTPBadRequest(text="This opens a Windows folder on the PeakCut computer.")

        headers = proxy_headers(request.headers)
        for name in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Expect"):
            headers.popall(name, None)
        headers["Host"] = expected_host
        storage = request.path == "/api/storage" and request.method in ("GET", "PUT")
        if storage:
            headers["Accept-Encoding"] = "identity"
        target = URL(UPSTREAM + request.rel_url.raw_path_qs, encoded=True)
        data = request.content.iter_chunked(65536) if request.can_read_body else None
        response = None
        try:
            async with request.app[SESSION].request(request.method, target, headers=headers, data=data,
                                                    allow_redirects=False) as source:
                outgoing = proxy_headers(source.headers)
                if storage and source.status == 200:
                    values = await source.json()
                    values["local"] = False
                    outgoing.popall("Content-Type", None)
                    outgoing.popall("Content-Length", None)
                    return web.json_response(values, headers=outgoing)
                response = web.StreamResponse(status=source.status, headers=outgoing)
                await response.prepare(request)
                async for chunk in source.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (ClientError, TimeoutError) as error:
            if response is not None and response.prepared:
                raise
            raise web.HTTPBadGateway(text="PeakCut is temporarily unavailable on this computer.") from error

    app.cleanup_ctx.append(upstream_session)
    app.router.add_route("*", "/{path:.*}", relay)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interface", type=IPv4Interface, help="Wi-Fi address and prefix, for example 192.0.2.10/24")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    web.run_app(dashboard(args.interface, args.port), host=str(args.interface.ip), port=args.port, access_log=None,
                print=None)
