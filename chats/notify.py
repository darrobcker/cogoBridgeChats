"""The nudge: one fixed line to the ntfy.sh topic the server made for a person (rule 13). No content, no sender, no
link. Never waited on: a nudge that fails is a nudge not sent, printed to the log without the topic."""
from __future__ import annotations

import sys
import threading
import urllib.request

LINE = b"Something in Bridge Chats needs you. Open your AI and say: check my Bridge Chats."


def _post(topic: str) -> None:
    request = urllib.request.Request(f"https://ntfy.sh/{topic}", data=LINE, method="POST",
                                     headers={"Title": "Bridge Chats", "Content-Type": "text/plain"})
    try:
        urllib.request.urlopen(request, timeout=5).close()
    except Exception as exc:        # whatever went wrong, nothing waits on it
        print(f"chats: a nudge was not sent: {exc!r}", file=sys.stderr)


def send(topic: str) -> None:
    threading.Thread(target=_post, args=(topic,), daemon=True).start()
