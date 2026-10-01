"""The only web pages: home, an invite and the connect page it leads to, the Allow page an app's sign-in opens, the
one-time page that shows a routine's run link, and one saying an address has no page.

A GET changes nothing, so a link preview cannot use an invite or a one-time page up (rule 2). An invite page makes
nobody: it remembers the invite in that browser, and the first call of the connection Allow makes there uses it.
Every page before a connection says who can read what (rule 16).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from mcp.server.auth.provider import construct_redirect_uri
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import core
from .store import Store

DAY, YEAR = "public, max-age=86400", "public, max-age=31536000, immutable"
# What a page may load and run: this server's own files, and the images a theme's script makes from them. Styles may
# be inline (the plain frame's are); scripts may not, so text that got past escaping could not run. No form-action:
# Chrome holds a form's redirect to it too, and Allow's answer goes on to the app that asked.
POLICY = ("default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; object-src 'none'; "
          "base-uri 'none'; frame-ancestors 'none'")
# Where each app's sign-in returns: an invite whose holder chose one of these goes only to a sign-in that returns
# there, so another app, allowed by someone tricked into it, cannot take it. "Another AI" can be anything.
APPS = {"claude": ("claude.ai", "claude.com"), "chatgpt": ("chatgpt.com", "openai.com")}
INVITE, NAME, AI = "chats_invite", "chats_name", "chats_ai"     # the cookies: the invite, the name, the app; no more


def _their_app(ai: str, redirect_uri: str) -> bool:
    host = urlparse(redirect_uri).hostname or ""
    return ai not in APPS or any(host == d or host.endswith("." + d) for d in APPS[ai])


class _Kept(StaticFiles):
    """/static, with how long a browser may keep each file: one asked for with ?v= (a theme's versioned asset) never
    changes under that name, so a year; the rest a day."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            versioned = "v" in parse_qs(scope.get("query_string", b"").decode())
            response.headers["Cache-Control"] = YEAR if versioned else DAY
        return response


def _version() -> str:
    """The commit this code is, so a deploy can tell the new code is the one answering."""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent, capture_output=True,
                              text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "dev"


def create_app(store: Store, *, base_url: str, operator: str = "", theme: str = "") -> Starlette:
    """`operator` is who runs this server, named on the pages because they can read everything. `theme`: a directory
    whose `templates/` are found before these and whose `static/` is served at /static, a server's own look; the
    words that say who can read what before anyone presses a button stay these."""
    here = Path(__file__).parent
    looks = [Path(theme)] if theme else []
    env = Environment(loader=ChoiceLoader([FileSystemLoader(str(d / "templates")) for d in [*looks, here]]),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    static = next(d / "static" for d in [*looks, here] if (d / "static").is_dir())
    base = base_url.rstrip("/")
    connector = f"{base}/mcp"
    version = _version()
    claude_add = ("https://claude.ai/customize/connectors?modal=add-custom-connector&connectorName="
                  f"{quote('Bridge Chats')}&connectorUrl={quote(connector, safe='')}")
    secure, own = base.startswith("https"), urlparse(base)

    def page(template: str, status: int = 200, **context) -> HTMLResponse:
        response = HTMLResponse(env.get_template(template).render(operator=operator, connector=connector,
                                                              claude_add=claude_add, **context), status_code=status)
        response.headers["Content-Security-Policy"] = POLICY
        response.headers["Cache-Control"] = "no-store"
        return response

    async def home(request: Request) -> Response:
        return page("home.html")

    async def health(request: Request) -> Response:
        try:
            store.one("SELECT 1 FROM people LIMIT 1")
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True, "version": version, "bridge_chats": core.VERSION})

    async def favicon(request: Request) -> Response:
        return FileResponse(static / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": DAY})

    async def invite(request: Request) -> Response:
        token = request.path_params["token"]
        if not core.invite_page(store, token):
            return page("invite_gone.html", 404)
        if request.method != "POST":        # chat apps fetch links to preview them, some by HEAD
            return page("invite.html")
        # Only a press on this page: another site posting this form would choose the visitor's invite and name.
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != f"{own.scheme}://"
                                                                     f"{own.netloc}"):
            return page("invite.html", 403)
        form = await request.form()
        ai = form.get("ai", "")
        ai = ai if ai in APPS else "other"
        response = page("connect.html", ai=ai, link=f"{base}/i/{token}")
        # Lax: an Allow form posted from another site does not carry them.
        for key, value in ((INVITE, token), (NAME, quote(str(form.get("name", "")).strip()[:core.NAME_MAX])),
                           (AI, ai)):
            response.set_cookie(key, value, max_age=86400, httponly=True, samesite="lax", secure=secure)
        return response

    async def allow(request: Request) -> Response:
        """An app's sign-in: one button. The page names the app and where it sends the person back; it asks for
        nothing and knows nobody."""
        form = await request.form() if request.method == "POST" else {}
        ref = form.get("r") or request.query_params.get("r", "")
        waiting = core.sign_in_waiting(store, ref) if ref else None
        if waiting is None:
            return page("expired.html", 400)
        token = request.cookies.get(INVITE, "")[:64]
        if not _their_app(request.cookies.get(AI, "other"), waiting["redirect_uri"]):
            token = ""
        invited = bool(token) and core.invite_page(store, token)
        name = unquote(request.cookies.get(NAME, ""))[:core.NAME_MAX] if invited else ""
        if request.method != "POST":
            info = json.loads(core.client(store, waiting["client_id"]) or "{}")
            return page("allow.html", ref=ref, app=(info.get("client_name") or "An app")[:60],
                        host=urlparse(waiting["redirect_uri"]).hostname or "", name=name)
        if form.get("choice") != "allow":
            gone = core.deny(store, ref) or waiting
            return RedirectResponse(construct_redirect_uri(gone["redirect_uri"], error="access_denied",
                                                           state=gone["state"]), status_code=303)
        allowed = core.allow(store, ref, token, name)
        if allowed is None:
            return page("expired.html", 400)
        data, code = allowed
        return RedirectResponse(construct_redirect_uri(data["redirect_uri"], code=code, state=data["state"]),
                                status_code=303)

    async def once(request: Request) -> Response:
        token = request.path_params["token"]
        if request.method != "POST":
            return page("once.html")
        secret = core.reveal_once(store, token)
        return page("once.html", run_link=f"{base}/c/{secret}/mcp") if secret else page("once_gone.html", 404)

    async def missing(request: Request, exc: Exception) -> Response:
        return page("missing.html", 404)

    return Starlette(exception_handlers={404: missing},
                     routes=[Route("/", home), Route("/health", health), Route("/favicon.ico", favicon),
                             Mount("/static", _Kept(directory=static)),
                             Route("/allow", allow, methods=["GET", "POST"]),
                             Route("/i/{token}", invite, methods=["GET", "POST"]),
                             Route("/once/{token}", once, methods=["GET", "POST"])])
