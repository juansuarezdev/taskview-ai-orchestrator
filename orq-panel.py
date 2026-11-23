#!/usr/bin/env python3
"""Panel web del orquestador (ai-dispatch.py): estado, cola, preguntas, ejecuciones, uso y acciones seguras.

Solo escucha en 127.0.0.1:8790 (Tailscale Serve lo publica en la tailnet, nunca en internet).
Lanzado por el LaunchAgent com.example.taskview-orq-panel. Cada 30 s empuja un resumen a PhoneBox (/api/push/orquestador).

CLI:  orq-panel.py            servidor
      orq-panel.py --pause | --resume    crea / borra logs/.pausa-orquestador (lo usan los atajos de PhoneBox)
      orq-panel.py --summary             imprime el resumen JSON (lo mismo que recibe PhoneBox)
"""
import html
import importlib.util
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8790
TS_HOST = "your-mac.your-tailnet.ts.net"
TS_PORT = 8455
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"{TS_HOST}:{TS_PORT}"}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", f"https://{TS_HOST}:{TS_PORT}"}
LOG = HERE / "logs/ai-dispatch.log"
LOCK = HERE / "logs/.ai-dispatch.lock"
PAUSE = HERE / "logs/.pausa-orquestador"
WEB_TASKS = HERE / "web-tasks.txt"
PANEL_LOG = HERE / "logs/orq-panel.log"
PHONEBOX = "http://127.0.0.1:8765"
ESTADO = Path.home() / ".local/bin/estado-orquestador"
AGENTS = ["com.example.taskview-ai", "com.example.taskview-supervisor", "com.example.taskview-orq-panel", "com.example.taskview-daily",
          "com.example.taskview-health", "com.example.taskview-sync", "com.example.taskview-backup", "com.example.taskview-weekly"]


def _dispatcher():
    """ai-dispatch.py as a module: same token mechanism, REPOS and merge list, without running it."""
    spec = importlib.util.spec_from_file_location("ai_dispatch", HERE / "ai-dispatch.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HERE))
    spec.loader.exec_module(mod)
    return mod


D = None


def disp():
    global D
    if D is None:
        D = _dispatcher()
    return D


def plog(msg):
    PANEL_LOG.parent.mkdir(exist_ok=True)
    with PANEL_LOG.open("a") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")


def api(method, path, body=None):
    d = disp()
    h = {"content-type": "application/json", "x-taskview-client": "orq-panel", "authorization": "Bearer " + d.token({})}
    req = urllib.request.Request(d.API + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=10))


# ---------------------------------------------------------------- log parsing
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)$")
START = re.compile(r"^start t(\d+) (\w+) \[(.+?)\] (.*)$")
USAGE = re.compile(r"^usage t(\d+) (\w+) (\S+) cost=\$([\d.]+)")
END = re.compile(r"^end t(\d+) ok=(\w+) (\S+) runner=(\S+) model=(\S+) secs=(\d+)")
INTEG = re.compile(r"^t(\d+) integrar main: (.*)$")


def ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def parse_log(lines):
    """Runs (newest last) and the last integration message per task, from ai-dispatch.log lines."""
    runs, open_runs, integ = [], {}, {}
    for raw in lines:
        m = LINE.match(raw.rstrip("\n"))
        if not m:
            continue
        at, msg = m.groups()
        if s := START.match(msg):
            r = {"id": int(s[1]), "mode": s[2], "board": s[3], "title": s[4], "start": ts(at), "cost": 0.0, "steps": 0, "end": None,
                 "ok": None, "result": None, "runner": None, "model": None, "secs": None}
            open_runs[r["id"]] = r
            runs.append(r)
        elif u := USAGE.match(msg):
            r = open_runs.get(int(u[1]))
            if r:
                r["cost"] = round(r["cost"] + float(u[4]), 2)
                r["steps"] += 1
                r["model"] = u[3]
        elif e := END.match(msg):
            r = open_runs.pop(int(e[1]), None)
            if r:
                r.update(end=ts(at), ok=e[2] == "True", result=e[3], runner=e[4], model=e[5], secs=int(e[6]))
        elif i := INTEG.match(msg):
            text = i[2]
            ok = bool(re.search(r" en main \(|ya está contenida en main", text))
            if ok or not re.match(r"tests \(|restaurados ", text):  # progress lines are neither success nor failure
                integ[int(i[1])] = {"at": ts(at), "ok": ok, "text": text[:400]}
    return runs, integ


def tail_lines(path, n=4000):
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 1_500_000))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


# ---------------------------------------------------------------- web-tasks.txt
def web_ids(text):
    return [l.split("#")[0].strip() for l in text.splitlines() if l.split("#")[0].strip()]


def web_set(text, tid, on, note=""):
    """web-tasks.txt with task `tid` granted (appended with a comment) or removed; comments and other lines kept."""
    tid = str(int(tid))
    lines = [l for l in text.splitlines() if l.split("#")[0].strip() != tid]
    if on:
        lines.append(f"{tid}  # {note}".rstrip() if note else tid)
    return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------- usage / queue / agents
def parse_usage(text):
    out = {"raw": text.strip()}
    if m := re.search(r"5h window: (\d+)% used, resets (\S+)", text):
        out["h5"], out["h5_reset"] = int(m[1]), m[2]
    if m := re.search(r"7d window: (\d+)% \(resets ([^)]+)\)", text):
        out["d7"], out["d7_reset"] = int(m[1]), m[2]
    if m := re.search(r"^decision: (.*)$", text, re.M):
        out["decision"] = m[1]
    if m := re.search(r"^claude: (.*?) · opencode: (.*)$", text, re.M):
        out["claude"], out["opencode"] = m[1], m[2]
    if m := re.search(r"^pacing: allowed (\d+)%", text, re.M):
        out["h5_allowed"] = int(m[1])
    if m := re.search(r"^week pacing: allowed (\d+)%", text, re.M):
        out["d7_allowed"] = int(m[1])
    return out


def parse_dry(text):
    rows = []
    for l in text.splitlines():
        if m := re.match(r"^\[(.+?)\] t(\d+) (espera|continuar): (.*)$", l):
            rows.append({"board": m[1], "id": int(m[2]), "kind": m[3], "text": m[4]})
        elif m := re.match(r"^\[(.+?)\] t(\d+) (\w+)/(\w+) base=(\S+): (.*)$", l):
            rows.append({"board": m[1], "id": int(m[2]), "kind": "lista", "text": f"{m[3]} con {m[4]} (base {m[5]})", "title": m[6]})
        elif m := re.match(r"^\[(.+?)\] (.*)$", l):
            rows.append({"board": m[1], "id": None, "kind": "aviso", "text": m[2]})
    return rows


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=HERE)
        return p.returncode, p.stdout + p.stderr
    except (subprocess.TimeoutExpired, OSError) as e:
        return -1, str(e)


def launch_agents():
    _, out = sh(["launchctl", "list"], 10)
    seen = {}
    for l in out.splitlines():
        p = l.split("\t")
        if len(p) == 3 and p[2] in AGENTS:
            seen[p[2]] = {"pid": None if p[0] == "-" else int(p[0]), "last_exit": p[1]}
    return [{"label": a, "loaded": a in seen, **seen.get(a, {"pid": None, "last_exit": None})} for a in AGENTS]


def lock_held():
    """Whether a dispatcher process holds the flock, seen with lsof (never takes the lock itself)."""
    rc, out = sh(["/usr/sbin/lsof", "-t", str(LOCK)], 10)
    return bool(out.strip()) and rc == 0


def question_text(tail):
    t = html.unescape(tail or "")
    found = list(re.finditer(r"PREGUNTA:\s*", t))
    q = t[found[-1].end():] if found else t[-600:]
    q = re.split(r"Sesión IA: \S+ mode=", q)[0]
    return re.sub(r"\s+", " ", q).strip()[:1200]


# ---------------------------------------------------------------- state
CACHE = {"dry": None, "dry_at": 0, "usage": None, "usage_at": 0, "estado": "", "estado_at": 0, "inbox": None, "inbox_at": 0,
         "titles": {}, "jobs": {}}
CLOCK = threading.Lock()


def refresh(force=False):
    now = time.time()
    if force or now - CACHE["usage_at"] > 60:
        rc, out = sh([sys.executable, str(HERE / "ai-dispatch.py"), "--usage"], 40)
        if rc == 0:
            CACHE["usage"], CACHE["usage_at"] = parse_usage(out), now
    if force or now - CACHE["dry_at"] > 60:
        rc, out = sh([sys.executable, str(HERE / "ai-dispatch.py"), "--dry-run"], 90)
        if rc == 0 and (out.strip() or not lock_held()):  # empty output while the lock is held = busy tick, keep the last one
            CACHE["dry"], CACHE["dry_at"] = out, now
    if force or now - CACHE["inbox_at"] > 30:
        try:
            CACHE["inbox"], CACHE["inbox_at"] = api("GET", "/module/about/inbox")["response"], now
        except Exception as e:  # API down: keep the last answer
            plog(f"inbox: {e}")
    if force or now - CACHE["estado_at"] > 120:
        if ESTADO.exists():
            _, CACHE["estado"] = sh(["/bin/zsh", str(ESTADO), "8"], 60)
            CACHE["estado_at"] = now


def title_of(tid):
    tid = int(tid)
    if tid not in CACHE["titles"]:
        try:
            CACHE["titles"][tid] = api("GET", f"/module/tasks/{tid}")["response"].get("description", "")
        except Exception:
            return ""
    return CACHE["titles"][tid]


def state():
    with CLOCK:
        refresh()
    runs, integ = parse_log(tail_lines(LOG))
    now = time.time()
    held = lock_held()
    running = next((r for r in reversed(runs) if r["end"] is None), None) if held else None
    if running:
        running = {**running, "elapsed": int(now - running["start"])}
    inbox = CACHE["inbox"] or {}
    for r in runs:
        CACHE["titles"].setdefault(r["id"], r["title"])
    d = disp()
    board_repo = d.REPOS
    retry = []
    for tid, it in sorted(integ.items(), key=lambda x: -x[1]["at"]):
        if it["ok"] or now - it["at"] > 14 * 86400:
            continue
        board = next((r["board"] for r in reversed(runs) if r["id"] == tid), "")
        repo = board_repo.get(board, "")
        root = d.EXTRA_ROOTS.get(repo, d.CODE / repo) if repo else None
        if root and sh(["git", "-C", str(root), "merge-base", "--is-ancestor", f"ai/t{tid}", "main"], 10)[0] in (0, 128):
            continue  # already in main (integrated later by hand) or the branch is gone
        retry.append({"id": tid, "title": title_of(tid), "board": board, "repo": repo, "branch": f"ai/t{tid}", "at": it["at"],
                      "text": it["text"], "allowed": repo in d.MERGE_TO_MAIN_REPOS})
    queue = [{"id": t["id"], "title": t["description"], "board": t.get("goalName", ""), "tags": t.get("tags", []), "ai": t.get("ai"),
              "status": t.get("status")} for t in inbox.get("queued", [])]
    try:
        wtext = WEB_TASKS.read_text()
    except OSError:
        wtext = ""
    paused = PAUSE.exists()
    return {
        "now": now,
        "paused": paused,
        "paused_since": PAUSE.stat().st_mtime if paused else None,
        "lock": held,
        "running": running,
        "queue": queue,
        "dry": parse_dry(CACHE["dry"] or ""),
        "dry_at": CACHE["dry_at"],
        "questions": [{"id": q["id"], "title": q["description"], "board": q.get("goalName", ""), "goalId": q.get("goalId"),
                       "ai": q.get("ai"), "text": question_text(q.get("noteTail"))} for q in inbox.get("questions", [])],
        "working": [{"id": t["id"], "title": t["description"], "board": t.get("goalName", "")} for t in inbox.get("working", [])],
        "runs": [{k: v for k, v in r.items()} for r in reversed(runs[-40:])],
        "integrations": retry[:10],
        "usage": CACHE["usage"] or {},
        "usage_at": CACHE["usage_at"],
        "agents": launch_agents(),
        "web": [{"id": int(i), "title": title_of(i)} for i in web_ids(wtext) if i.isdigit()],
        "estado": CACHE["estado"],
        "jobs": CACHE["jobs"],
    }


def summary(s=None):
    """Small payload for PhoneBox (Pushed value + Custom HTML)."""
    s = s or state()
    u = s["usage"]
    run = s["running"]
    nq = len(s["questions"])
    sub = (f"5 h {u.get('h5', '?')}% · semana {u.get('d7', '?')}%")
    value = ("En pausa" if s["paused"] else (f"t{run['id']} · {run['elapsed'] // 60} min" if run else "Libre"))
    return {
        "title": "Orquestador",
        "value": value,
        "sub": sub,
        "level": "warn" if s["paused"] or nq else "ok",
        "items": [{"label": "Cola", "value": str(len(s["queue"]))}, {"label": "Preguntas", "value": str(nq), "level": "warn" if nq else "ok"}],
        "orq": {"paused": s["paused"], "running": {"id": run["id"], "title": run["title"], "board": run["board"], "elapsed": run["elapsed"],
                                                    "mode": run["mode"]} if run else None,
                "queue": len(s["queue"]), "questions": nq, "h5": u.get("h5"), "d7": u.get("d7"), "h5_reset": u.get("h5_reset"),
                "claude": u.get("claude"), "opencode": u.get("opencode"), "at": int(s["now"])},
    }


def push_phonebox(s=None):
    body = json.dumps(summary(s)).encode()
    for key, data in (("orquestador", body), ("orquestador-pausa", b"1" if PAUSE.exists() else b"0")):
        try:
            req = urllib.request.Request(f"{PHONEBOX}/api/push/{key}", data=data, method="POST", headers={"content-type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass  # PhoneBox off: the panel keeps working


# ---------------------------------------------------------------- actions
def set_pause(on, who="panel"):
    if on:
        PAUSE.parent.mkdir(exist_ok=True)
        PAUSE.write_text(f"{time.strftime('%F %T')} {who}\n")
    else:
        PAUSE.unlink(missing_ok=True)
    plog(f"{'pausa' if on else 'reanudar'} ({who})")
    msg = f"{'pausa manual' if on else 'fin de pausa manual'} ({who})"
    with LOG.open("a") as f:  # same line format as ai-dispatch.log
        f.write(f"{time.strftime('%F %T')} {msg}\n")


def answer(tid, text):
    text = text.strip()
    if not text:
        raise ValueError("respuesta vacía")
    inbox = api("GET", "/module/about/inbox")["response"]
    q = next((x for x in inbox.get("questions", []) if x["id"] == int(tid)), None)
    if not q:
        raise ValueError(f"t{tid} ya no tiene ia·pregunta")
    gid = q["goalId"]
    tag = next((t for t in api("GET", "/module/tags")["response"] if t["goalId"] == gid and t["name"] == "ia·continuar"), None)
    if not tag:
        raise ValueError("falta la etiqueta ia·continuar en ese proyecto")
    task = api("GET", f"/module/tasks/{int(tid)}")["response"]
    cur = task.get("note") or ""
    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    api("PATCH", "/module/tasks", {"id": int(tid), "note": f"{cur}<p>RESPUESTA: {esc}</p>"})
    if tag["id"] not in set(task.get("tags") or []):
        api("PATCH", "/module/tags/toggle", {"tagId": tag["id"], "taskId": int(tid)})
    plog(f"respuesta t{tid} ({len(text)} car.)")
    CACHE["inbox_at"] = 0


def web(tid, on):
    tid = int(tid)
    try:
        text = WEB_TASKS.read_text()
    except OSError:
        text = ""
    WEB_TASKS.write_text(web_set(text, tid, on, f"web concedido desde el panel {time.strftime('%F')}"))
    plog(f"web t{tid} {'concedida' if on else 'retirada'}")


def integrate(repo, branch):
    d = disp()
    if repo not in d.MERGE_TO_MAIN_REPOS or not re.fullmatch(r"ai/t\d+", branch):
        raise ValueError("repo o rama no permitidos")
    key = f"{repo}:{branch}"
    if CACHE["jobs"].get(key, {}).get("state") == "corriendo":
        raise ValueError("ya hay un reintento en marcha")
    CACHE["jobs"][key] = {"state": "corriendo", "at": time.time(), "out": ""}

    def go():  # ai-dispatch takes the flock itself and refuses (exit 1) while a card is running
        rc, out = sh([sys.executable, str(HERE / "ai-dispatch.py"), "--integrate-main", repo, branch], 30 * 60)
        CACHE["jobs"][key] = {"state": "ok" if rc == 0 else "fallo", "at": time.time(), "out": out[-1500:]}
        plog(f"integrar {key}: rc={rc}")
    threading.Thread(target=go, daemon=True).start()
    plog(f"integrar {key}: lanzado")


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "orq-panel"

    def log_message(self, *a):
        pass

    def _ok_host(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._ok_host():
            return self._send(403, {"error": "host"})
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "orq-panel.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, state())
        if self.path == "/api/summary":
            return self._send(200, summary())
        self._send(404, {"error": "no existe"})

    def do_POST(self):
        # CSRF: a page from another site cannot set this header without a preflight we never answer, and Origin must be ours
        if not self._ok_host() or self.headers.get("X-Orq-Panel") != "1" or self.headers.get("Origin", "") not in ALLOWED_ORIGINS:
            return self._send(403, {"error": "origen no permitido"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
            p = self.path
            if p == "/api/pause":
                set_pause(bool(b.get("on")), "panel web")
            elif p == "/api/answer":
                answer(b["id"], str(b.get("text", ""))[:4000])
            elif p == "/api/web":
                web(b["id"], bool(b.get("on")))
            elif p == "/api/integrate":
                integrate(str(b["repo"]), str(b["branch"]))
            else:
                return self._send(404, {"error": "no existe"})
            s = state()
            push_phonebox(s)
            self._send(200, {"ok": True, "state": s})
        except (ValueError, KeyError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            plog(f"error {self.path}: {e!r}")
            self._send(500, {"error": "error interno"})


def pusher():
    while True:
        try:
            push_phonebox()
        except Exception as e:
            plog(f"push: {e!r}")
        time.sleep(30)


def main():
    a = sys.argv[1:]
    if "--pause" in a or "--resume" in a:
        set_pause("--pause" in a, "PhoneBox" if "--phonebox" in a else "terminal")
        push_phonebox()
        return
    if "--summary" in a:
        print(json.dumps(summary(), ensure_ascii=False, indent=1))
        return
    threading.Thread(target=pusher, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    plog(f"escuchando en 127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
