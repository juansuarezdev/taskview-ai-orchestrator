#!/usr/bin/env python3
"""Notify the owner: Web Push to the iPhone PWA via the TaskView API, plus a native macOS banner.

Usage: notify.py "<title>" "<message>" [priority 1-5] [--task <id>]
With --task, clicking the banner (native Mac app) or the push (iPhone PWA) opens that task's kanban with the card open.
Never raises: notifications must not break the caller.
"""
import json, subprocess, sys, urllib.request
from pathlib import Path

API = "http://127.0.0.1:1725"
NATIVE = "taskview://"  # the Mac banner opens the native TaskView app (~/Applications, repo repo-tm) at this path
TOKEN = Path(__file__).with_name(".api-token")
BANNER = Path(__file__).with_name("mac-notifier") / "TaskViewNotify.app/Contents/MacOS/TaskViewNotify"


def api(path, body=None):
    req = urllib.request.Request(f"{API}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {TOKEN.read_text().strip()}"})
    data = json.load(urllib.request.urlopen(req, timeout=5))
    return data.get("response", data)


def task_path(task_id):
    """`/<orgSlug>/<projectId>/kanban?task=<id>`, or '' when the task cannot be resolved."""
    try:
        goal = api(f"/module/tasks/{int(task_id)}")["goalId"]
        slug = api("/module/organizations")[0]["slug"]
        return f"/{slug}/{goal}/kanban?task={int(task_id)}"
    except Exception:
        return ""


def web_push(title, message, path=""):
    """Returns how many devices accepted it (0 when none is subscribed or the API is down)."""
    try:
        data = api("/module/notifications/push/send", {"title": title or "TaskView", "body": message, "url": path or "/"})
        return int(data.get("sent", 0))
    except Exception:
        return 0


def mac_banner(title, message, path=""):
    try:
        clean = lambda t: " ".join((t or "").split())
        if BANNER.exists():  # clickable: opens the task in the native app
            subprocess.run([str(BANNER), clean(title) or "TaskView", clean(message), f"{NATIVE}{path or '/'}"],
                           timeout=10, capture_output=True)
            return
        esc = lambda t: clean(t).replace("\\", "\\\\").replace('"', '\\"')  # fallback: clicking opens Script Editor
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title or "TaskView")}"'],
                       timeout=5, capture_output=True)
    except Exception:
        pass


def notify(title, message, priority=3, task=None):
    path = task_path(task) if task else ""
    mac_banner(title, message, path)
    web_push(title, message, path)


if __name__ == "__main__":
    a = sys.argv[1:]
    task = None
    if "--task" in a:
        i = a.index("--task")
        task = a[i + 1] if i + 1 < len(a) else None
        a = a[:i] + a[i + 2:]
    notify(a[0] if a else "TaskView", a[1] if len(a) > 1 else "", int(a[2]) if len(a) > 2 else 3, task)
