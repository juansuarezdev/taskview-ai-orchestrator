#!/usr/bin/env python3
"""Watch interactive Claude Code sessions; on 5h usage limit, hand off to OpenCode.

Runs from launchd every minute. Skips non-interactive (TaskView -p) sessions.
Dedupes per sessionId + limit-timestamp.
"""
import json, os, re, subprocess, time
from pathlib import Path

HOME = Path.home()
SESSIONS = HOME / ".claude/sessions"
PROJECTS = HOME / ".claude/projects"
STATE = HOME / ".claude/hooks/state/interactive-handoff"
LOG = HOME / ".claude/hooks/state/interactive-handoff.log"
ORQ_DIR = Path(os.environ.get("ORQ_DIR") or Path(__file__).resolve().parent)  # the orchestrator folder
DISPATCH_LIMIT = ORQ_DIR / "logs/.claude-limit-until"
OC_BIN = HOME / ".opencode/bin/opencode"
OC_MODEL_FILE = ORQ_DIR / ".opencode-model"
NOTIFY = HOME / ".claude/ClaudeNotify.app/Contents/MacOS/notify"
LIMIT_HIT = re.compile(
    r"usage limit reached|5-hour limit|hit your limit|Goal paused · usage limit",
    re.I,
)
LIMIT_RESET = re.compile(r"usage limit has reset|limit has reset", re.I)


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%F %T')} {msg}\n")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def transcript_for(sid):
    hits = list(PROJECTS.rglob(f"{sid}.jsonl"))
    return hits[0] if hits else None


def last_limit_signal(path, max_lines=120):
    """Return ('hit'|'reset'|None, timestamp, snippet) from the end of the transcript."""
    try:
        lines = path.read_text(errors="ignore").splitlines()[-max_lines:]
    except Exception:
        return None, None, None
    last = None
    for line in lines:
        try:
            o = json.loads(line)
        except Exception:
            continue
        # Limit notices are written by Claude Code itself (assistant message with apiError, or system).
        # User prompts / api-request blobs may quote an old limit text (e.g. a TaskView note): ignore them.
        # An assistant message only counts when Claude Code flagged it as an API error: a normal
        # assistant reply can quote the limit text (it did on 2026-11-16 09:18 explaining this bug).
        if o.get("type") == "assistant" and not (o.get("apiError") or o.get("isApiErrorMessage")):
            continue
        if o.get("type") not in ("assistant", "system"):
            continue
        parts = [str(o.get("content") or "")]
        msg = o.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "text":
                    parts.append(x.get("text") or "")
        blob = " ".join(parts)
        ts = o.get("timestamp")
        if LIMIT_RESET.search(blob):
            last = ("reset", ts, blob[:240])
        elif LIMIT_HIT.search(blob):
            last = ("hit", ts, blob[:240])
    if not last:
        return None, None, None
    return last


def ts_epoch(ts):
    """ISO-8601 transcript timestamp ('2026-11-14T21:59:03.123Z') -> unix seconds, or None."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def is_fresh_hit(ts, started_ms, max_age=3600):
    """A limit message only counts if it was written by this process run and recently.
    A resumed session (claude --resume / -r) carries old limit messages in its transcript:
    those must not trigger a new cooldown (false positives 2026-11-16 07:54 and 08:46)."""
    t = ts_epoch(ts)
    if t is None:
        return False
    if started_ms and t < started_ms / 1000 - 60:
        return False
    return time.time() - t <= max_age


def last_user_text(path, max_lines=400):
    try:
        lines = path.read_text(errors="ignore").splitlines()[-max_lines:]
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        msg = o.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()[:2000]
        if isinstance(c, list):
            texts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
            t = "\n".join(texts).strip()
            if t:
                return t[:2000]
    return ""


def reset_epoch(text):
    """Parse 'resets 11:10am' (local wall clock) from a limit notice -> unix seconds, or None."""
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text or "", re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    now = time.localtime()
    cand = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, mi, 0, 0, 0, -1))
    if cand <= time.time() - 300:  # already passed (small grace for clock skew): it is tomorrow
        cand += 24 * 3600
    return cand


def mark_dispatch_cooldown(text=None, hours=5):
    """Cooldown until the reset time the notice states; +5h only if it states none.
    (Before 2026-11-16 it always wrote now+5h, so each limit notice while already limited
    pushed the dispatcher pause hours past the real reset.)"""
    until = reset_epoch(text) or time.time() + hours * 3600
    DISPATCH_LIMIT.parent.mkdir(parents=True, exist_ok=True)
    DISPATCH_LIMIT.write_text(str(until))


def oc_model():
    if OC_MODEL_FILE.exists():
        return OC_MODEL_FILE.read_text().strip() or "opencode/deepseek-v4.1-flash-free"
    return "opencode/deepseek-v4.1-flash-free"


def write_handoff(cwd, name, sid, user_txt, snippet):
    safe = re.sub(r"[^\w.-]+", "-", name or sid[:8]).strip("-") or sid[:8]
    path = Path(cwd) / f".claude-handoff-{safe}.md"
    body = (
        f"# Handoff Claude → OpenCode\n\n"
        f"- Sesión: `{name}` (`{sid}`)\n"
        f"- Cuando: {time.strftime('%F %T %Z')}\n"
        f"- Motivo: límite 5h de Claude\n"
        f"- Señal: {snippet}\n\n"
        f"## Último mensaje del usuario\n\n{user_txt or '(sin texto)'}\n\n"
        f"## Instrucciones para OpenCode\n\n"
        f"Continúa el trabajo de esta sesión de Claude en este directorio. "
        f"Lee el estado del repo (`git status`, ramas, diffs) y el último mensaje del usuario. "
        f"No repitas trabajo ya hecho. No hagas push. Resume en español al terminar.\n"
    )
    path.write_text(body)
    return path


def notify(title, body):
    if NOTIFY.exists():
        subprocess.run([str(NOTIFY), title, "Límite Claude → OpenCode", body[:180], "Glass"], check=False)
    else:
        subprocess.run(
            ["osascript", "-e", f'display notification {json.dumps(body[:120])} with title {json.dumps(title)}'],
            check=False,
        )


def launch_opencode(cwd, handoff, name):
    if not OC_BIN.exists():
        log(f"opencode bin missing: {OC_BIN}")
        return False

    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    prompt = (
        f"Claude Code pegó el límite 5h en la sesión '{name}'. "
        f"Lee {handoff.name} y continúa ese trabajo en este repo. "
        f"No hagas push. Commits pequeños si hace falta."
    )
    cmd = f'cd {esc(str(cwd))} && {esc(str(OC_BIN))} run --model {esc(oc_model())} --dir . "{esc(prompt)}"'
    script = f'tell application "Terminal"\n  activate\n  do script "{cmd}"\nend tell'
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    log(f"launch Terminal rc={p.returncode} {(p.stderr or '')[:200]}")
    return p.returncode == 0


def already_done(sid, ts):
    STATE.mkdir(parents=True, exist_ok=True)
    stamp = STATE / f"{sid}.done"
    key = ts or "unknown"
    if stamp.exists() and stamp.read_text().strip() == key:
        return True
    stamp.write_text(key)
    return False


def main():
    if not SESSIONS.exists():
        return
    for f in SESSIONS.glob("*.json"):
        try:
            o = json.loads(f.read_text())
        except Exception:
            continue
        if o.get("kind") != "interactive":
            continue
        pid = o.get("pid")
        if not pid or not pid_alive(pid):
            continue
        sid = o.get("sessionId")
        name = o.get("name") or f"pid-{pid}"
        cwd = o.get("cwd") or str(HOME)
        tp = transcript_for(sid)
        if not tp:
            continue
        kind, ts, snippet = last_limit_signal(tp)
        if kind != "hit":
            continue
        if not is_fresh_hit(ts, o.get("startedAt")):
            continue
        if already_done(sid, ts):
            continue
        user_txt = last_user_text(tp)
        handoff = write_handoff(cwd, name, sid, user_txt, snippet or "")
        mark_dispatch_cooldown(snippet)
        notify(f"Claude · {name}", f"Límite 5h → OpenCode en {Path(cwd).name}")
        ok = launch_opencode(cwd, handoff, name)
        log(f"handoff name={name} sid={sid} cwd={cwd} launch_ok={ok} handoff={handoff}")


if __name__ == "__main__":
    main()
