#!/usr/bin/env python3
"""Notify the owner: Web Push to the iPhone PWA via the TaskView API, plus a native macOS banner.

Usage: notify.py "<title>" "<message>" [priority 1-5]
Never raises: notifications must not break the caller.
"""
import json, subprocess, sys, urllib.request
from pathlib import Path

API = "http://127.0.0.1:1725"
TOKEN = Path(__file__).with_name(".api-token")


def web_push(title, message):
    """Returns how many devices accepted it (0 when none is subscribed or the API is down)."""
    try:
        req = urllib.request.Request(f"{API}/module/notifications/push/send",
                                     data=json.dumps({"title": title or "TaskView", "body": message}).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {TOKEN.read_text().strip()}"})
        data = json.load(urllib.request.urlopen(req, timeout=5))
        return int((data.get("response") or data).get("sent", 0))
    except Exception:
        return 0


def mac_banner(title, message):
    try:
        esc = lambda t: " ".join(t.split()).replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title or "TaskView")}"'],
                       timeout=5, capture_output=True)
    except Exception:
        pass


def notify(title, message, priority=3):
    mac_banner(title, message)
    web_push(title, message)


if __name__ == "__main__":
    a = sys.argv[1:]
    notify(a[0] if a else "TaskView", a[1] if len(a) > 1 else "", int(a[2]) if len(a) > 2 else 3)
