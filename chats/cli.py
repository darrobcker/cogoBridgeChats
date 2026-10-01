"""Operator commands. Configuration is environment variables: CHATS_HOME (where the database lives, default
~/.chats), CHATS_BASE_URL (this server's public https address), CHATS_OPERATOR (who runs it, named on every page and
to every assistant), CHATS_THEME (a directory with a look of its own: docs/OPERATIONS.md)."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import core

HOME = Path(os.environ.get("CHATS_HOME", Path.home() / ".chats"))


def _store():
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    return core.open_store(HOME / "chats.db")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="chats", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the server; leave it open")
    serve.add_argument("--port", type=int, default=int(os.environ.get("CHATS_PORT", 8771)))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--tunnel", action="store_true", help="a throwaway public https address from cloudflared, "
                                                             "for trying it out; it changes at every start")
    code = sub.add_parser("code", help="a code that gives a person back their account, ending every other "
                                       "connection of theirs (docs/OPERATIONS.md)")
    code.add_argument("person")
    sub.add_parser("stats", help="counts, never names")
    sub.add_parser("reports", help="what members reported, with the copies")
    args = parser.parse_args(argv)
    base = os.environ.get("CHATS_BASE_URL", "http://127.0.0.1:8771").rstrip("/")
    store = _store()
    if args.cmd == "serve":
        import uvicorn

        from . import notify
        from .app import create_app
        store.on_nudge = notify.send
        if args.tunnel:
            base = _quick_tunnel(args.port)
        print(f"Bridge Chats: {base}/mcp", file=sys.stderr)
        # No access log: a run link carries its secret in the URL, and sign-in its request.
        uvicorn.run(create_app(store, base_url=base, operator=os.environ.get("CHATS_OPERATOR", ""),
                               theme=os.environ.get("CHATS_THEME", "")),
                    host=args.host, port=args.port, log_level="info", access_log=False)
    elif args.cmd == "code":
        try:
            print(core.link_code(store, args.person, replace=True))
        except core.NotYours:
            sys.exit("no such person")
    elif args.cmd == "stats":
        week = store.now() - 7 * core.DAY
        out = {"people": store.one("SELECT COUNT(*) FROM people WHERE erased_t IS NULL")[0],
               "connections": store.one("SELECT COUNT(*) FROM grants WHERE revoked_t IS NULL AND person_id<>''")[0],
               "contacts": store.one("SELECT COUNT(*) FROM chats WHERE kind='dm'")[0],
               "groups": store.one("SELECT COUNT(*) FROM chats WHERE kind='group'")[0],
               "messages_7d": store.one("SELECT COUNT(*) FROM messages WHERE t > ?", week)[0],
               "by_voice_7d": {r[0]: r[1] for r in store.all("SELECT voice, COUNT(*) FROM messages WHERE t > ? "
                                                              "GROUP BY voice", week)}}
        print(json.dumps(out, indent=1))
    elif args.cmd == "reports":
        for r in store.all("SELECT * FROM reports ORDER BY t"):
            print(json.dumps(dict(r), indent=1))


def _quick_tunnel(port: int) -> str:
    """A throwaway trycloudflare.com address, for trying it out."""
    proc = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    found: list[str] = []

    def read() -> None:
        for line in proc.stderr:
            match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if match and not found:
                found.append(match.group())
    threading.Thread(target=read, daemon=True).start()
    for _ in range(60):
        if found:
            return found[0]
        time.sleep(0.5)
    sys.exit("cloudflared did not report an address in 30s")
