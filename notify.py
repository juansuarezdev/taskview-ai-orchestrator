#!/usr/bin/env python3
"""Push a notification to the phone through the local ntfy server.

Usage: notify.py "<title>" "<message>" [priority 1-5]
The topic is the secret in selfhost/.ntfy-topic (tailnet-only server).
Never raises: notifications must not break the caller.
"""
import sys, urllib.request
from pathlib import Path

URL = "http://127.0.0.1:2586"
TOPIC = Path(__file__).with_name(".ntfy-topic")


def notify(title, message, priority=3):
    try:
        req = urllib.request.Request(f"{URL}/{TOPIC.read_text().strip()}", data=message.encode(), method="POST",
                                     headers={"Title": title.encode("ascii", "ignore").decode() or "TaskView",
                                              "Priority": str(priority)})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


if __name__ == "__main__":
    a = sys.argv[1:]
    notify(a[0] if a else "TaskView", a[1] if len(a) > 1 else "", int(a[2]) if len(a) > 2 else 3)
