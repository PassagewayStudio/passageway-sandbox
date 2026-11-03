"""Passageway Sandbox gateway.

- Verifies the Cloudflare Access JWT (signature, audience, issuer) and reads the user's email.
- Starts one hardened container per user, each on its own internal Docker network.
- Proxies the ttyd terminal (HTTP + WebSocket) to the browser under /term/.
- Tears sessions down on expiry and reports start/end events to an optional webhook (n8n).
"""
import asyncio
import html
import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import docker
import httpx
import jwt
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")

CF_TEAM_DOMAIN = os.environ["CF_TEAM_DOMAIN"].strip().removeprefix("https://").rstrip("/")
CF_AUD = os.environ["CF_ACCESS_AUD"].strip()
SESSION_MINUTES = int(os.getenv("SESSION_MINUTES", "45"))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "3"))
GATEWAY_CONTAINER = os.getenv("GATEWAY_CONTAINER", "gateway")
EVENT_WEBHOOK_URL = os.getenv("EVENT_WEBHOOK_URL", "")
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "sandbox.passagewaystudio.com").strip().lower()
TTYD_PORT = 7681
LABEL = "passageway.sandbox"

PORTALS = {
    "linux": {
        "title": "Linux basics",
        "blurb": "A disposable Linux terminal. Practice commands, permissions, and logs.",
        "image": "passageway/portal-linux:dev",
    },
}


@dataclass
class Session:
    email: str
    portal: str
    sid: str
    name: str
    network_name: str
    started: float
    expires: float


SESSIONS: dict[str, Session] = {}
LOCK = asyncio.Lock()
dk: docker.DockerClient | None = None
HTTP: httpx.AsyncClient | None = None
jwks = jwt.PyJWKClient(f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs")


# ---------- auth ----------
def _verify(token: str) -> str | None:
    key = jwks.get_signing_key_from_jwt(token).key
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=CF_AUD,
        issuer=f"https://{CF_TEAM_DOMAIN}",
    )
    email = claims.get("email")
    return email.lower() if email else None


async def who(headers) -> str | None:
    token = headers.get("cf-access-jwt-assertion")
    if not token:
        return None
    try:
        return await asyncio.to_thread(_verify, token)
    except Exception as e:  # bad signature, wrong audience, expired, JWKS unreachable
        log.warning("JWT rejected: %s", type(e).__name__)
        return None


def same_origin(headers) -> bool:
    origin = headers.get("origin")
    if not origin:
        return True
    netloc = urlparse(origin).netloc.lower()
    # Behind the tunnel the Host header may be rewritten, so trust the configured
    # public hostname as well as Host.
    ok = netloc == PUBLIC_HOST or netloc == (headers.get("host") or "").lower()
    if not ok:
        log.warning("origin check failed: origin=%r host=%r public=%r", origin, headers.get("host"), PUBLIC_HOST)
    return ok


# ---------- docker helpers (blocking; call via to_thread) ----------
def _remove_network(net) -> None:
    try:
        net.disconnect(GATEWAY_CONTAINER, force=True)
    except Exception:
        pass
    try:
        net.remove()
    except Exception as e:
        log.warning("network remove failed: %s", e)


def _spawn(email: str, portal: str) -> Session:
    cfg = PORTALS[portal]
    sid = secrets.token_hex(6)
    name = f"sbx-{sid}"
    net = dk.networks.create(f"sbxnet-{sid}", driver="bridge", internal=True, labels={LABEL: "1"})
    try:
        net.connect(GATEWAY_CONTAINER)  # only the gateway shares this network
        dk.containers.run(
            cfg["image"],
            detach=True,
            name=name,
            hostname="sandbox",
            network=net.name,
            user="1000:1000",
            read_only=True,
            tmpfs={"/home/learner": "uid=1000,gid=1000,mode=0755,size=64m", "/tmp": "size=32m"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="256m",
            memswap_limit="256m",
            nano_cpus=500_000_000,
            pids_limit=128,
            environment={"BASE_PATH": "/term"},
            labels={LABEL: "1"},
        )
    except Exception:
        _remove_network(net)
        raise
    now = time.time()
    return Session(email, portal, sid, name, net.name, now, now + SESSION_MINUTES * 60)


def _teardown(s: Session) -> None:
    try:
        dk.containers.get(s.name).remove(force=True)
    except Exception as e:
        log.warning("container remove failed: %s", e)
    try:
        _remove_network(dk.networks.get(s.network_name))
    except Exception as e:
        log.warning("network lookup failed: %s", e)


def _cleanup_orphans() -> None:
    for c in dk.containers.list(all=True, filters={"label": LABEL}):
        c.remove(force=True)
    for n in dk.networks.list(filters={"label": LABEL}):
        _remove_network(n)


async def emit(event: str, s: Session, reason: str = "") -> None:
    if not EVENT_WEBHOOK_URL:
        return
    payload = {
        "event": event,
        "email": s.email,
        "portal": s.portal,
        "session_id": s.sid,
        "started_at": s.started,
        "at": time.time(),
        "reason": reason,
    }
    try:
        await HTTP.post(EVENT_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        log.warning("webhook failed: %s", e)


async def end_session(email: str, reason: str) -> None:
    s = SESSIONS.pop(email, None)
    if s:
        await asyncio.to_thread(_teardown, s)
        await emit("end", s, reason)


async def wait_ready(s: Session) -> bool:
    for _ in range(40):
        try:
            r = await HTTP.get(f"http://{s.name}:{TTYD_PORT}/term/", timeout=2)
            if r.status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    return False


async def reaper() -> None:
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for email, s in list(SESSIONS.items()):
            if s.expires <= now:
                await end_session(email, "expired")


# ---------- app ----------
async def lifespan(app: FastAPI):
    global dk, HTTP
    HTTP = httpx.AsyncClient(timeout=10)
    for attempt in range(30):  # docker-proxy may still be starting
        try:
            dk = await asyncio.to_thread(docker.from_env)
            await asyncio.to_thread(_cleanup_orphans)
            break
        except Exception as e:
            log.warning("waiting for docker API (%s)", e)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("docker API unreachable")
    task = asyncio.create_task(reaper())
    yield
    task.cancel()
    await HTTP.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Passageway Sandbox</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f1419;color:#e6e6e6;margin:0;padding:2rem}
main{max-width:640px;margin:0 auto}
.card{background:#1a212b;border:1px solid #2a3440;border-radius:10px;padding:1.25rem;margin:1rem 0}
button,a.btn{background:#3b82f6;color:#fff;border:0;border-radius:8px;padding:.6rem 1rem;font-size:1rem;
cursor:pointer;text-decoration:none;display:inline-block}
button.sec{background:#374151}
small{color:#9aa4b2}
</style></head><body><main>{{BODY}}</main></body></html>"""


def render(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.replace("{{BODY}}", body), status_code=status)


def page_body(email: str, s: Session | None, note: str = "") -> str:
    out = ["<h1>Passageway Sandbox</h1>", f"<small>Signed in as {html.escape(email)}</small>"]
    if note:
        out.append(f'<div class="card">{html.escape(note)}</div>')
    if s:
        mins = max(1, int((s.expires - time.time()) // 60) + 1)
        out.append(
            f'<div class="card"><h3>{html.escape(PORTALS[s.portal]["title"])} session running</h3>'
            f"<p><small>About {mins} min left. It resets when it ends.</small></p>"
            '<a class="btn" href="/term/">Open terminal</a> '
            '<form method="post" action="/end" style="display:inline">'
            '<button class="sec">End session</button></form></div>'
        )
    else:
        for key, p in PORTALS.items():
            out.append(
                f'<div class="card"><h3>{html.escape(p["title"])}</h3><p>{html.escape(p["blurb"])}</p>'
                f'<form method="post" action="/start?portal={key}"><button>Start session</button></form>'
                f"<p><small>Sessions last {SESSION_MINUTES} minutes and are wiped when they end.</small></p></div>"
            )
    return "".join(out)


@app.get("/healthz")
async def healthz():
    return Response("ok")


@app.get("/")
async def index(request: Request):
    email = await who(request.headers)
    if not email:
        return Response("Forbidden", status_code=403)
    s = SESSIONS.get(email)
    if s and s.expires <= time.time():
        s = None
    return render(page_body(email, s))


@app.post("/start")
async def start(request: Request, portal: str = "linux"):
    email = await who(request.headers)
    if not email:
        return Response("Forbidden", status_code=403)
    if not same_origin(request.headers):
        return Response("Bad origin", status_code=403)
    if portal not in PORTALS:
        return Response("Unknown portal", status_code=400)

    new = False
    async with LOCK:
        s = SESSIONS.get(email)
        if s and s.expires <= time.time():
            await end_session(email, "expired")
            s = None
        if not s:
            if len(SESSIONS) >= MAX_SESSIONS:
                return render(page_body(email, None, "The sandbox is full right now. Try again in a few minutes."), 503)
            try:
                s = await asyncio.to_thread(_spawn, email, portal)
            except Exception as e:
                log.error("spawn failed: %s", e)
                return render(page_body(email, None, "Could not start a session. Please try again."), 502)
            SESSIONS[email] = s
            new = True
    if new:
        if not await wait_ready(s):
            await end_session(email, "failed_to_start")
            return render(page_body(email, None, "The session failed to start. Please try again."), 502)
        await emit("start", s)
    return RedirectResponse("/term/", status_code=303)


@app.post("/end")
async def end(request: Request):
    email = await who(request.headers)
    if not email:
        return Response("Forbidden", status_code=403)
    if not same_origin(request.headers):
        return Response("Bad origin", status_code=403)
    await end_session(email, "user_ended")
    return RedirectResponse("/", status_code=303)


@app.api_route("/term", methods=["GET"])
async def term_redirect():
    return RedirectResponse("/term/", status_code=307)


@app.api_route("/term/{path:path}", methods=["GET", "POST", "HEAD"])
async def term_http(request: Request, path: str = ""):
    email = await who(request.headers)
    if not email:
        return Response("Forbidden", status_code=403)
    s = SESSIONS.get(email)
    if not s or s.expires <= time.time():
        return RedirectResponse("/", status_code=303)
    fwd = {
        k: v
        for k, v in request.headers.items()
        if k.lower() in ("accept", "accept-language", "content-type", "user-agent")
    }
    try:
        r = await HTTP.request(
            request.method,
            f"http://{s.name}:{TTYD_PORT}/term/{path}",
            params=dict(request.query_params),
            headers=fwd,
            content=await request.body(),
        )
    except httpx.HTTPError:
        return Response("Terminal not reachable", status_code=502)
    drop = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    headers = {k: v for k, v in r.headers.items() if k.lower() not in drop}
    return Response(r.content, status_code=r.status_code, headers=headers)


@app.websocket("/term/ws")
async def term_ws(ws: WebSocket):
    email = await who(ws.headers)
    s = SESSIONS.get(email) if email else None
    if not s or s.expires <= time.time() or not same_origin(ws.headers):
        await ws.close(code=4403)
        return
    await ws.accept(subprotocol="tty")
    try:
        async with websockets.connect(
            f"ws://{s.name}:{TTYD_PORT}/term/ws", subprotocols=["tty"], max_size=None
        ) as up:

            async def client_to_upstream():
                while True:
                    m = await ws.receive()
                    if m["type"] == "websocket.disconnect":
                        return
                    if m.get("bytes") is not None:
                        await up.send(m["bytes"])
                    elif m.get("text") is not None:
                        await up.send(m["text"])

            async def upstream_to_client():
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception as e:
        log.info("ws closed: %s", type(e).__name__)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
