"""Usage pacing of ai-dispatch.py: snapshot parsing, the pacing line and the statusline merge. Run: python3 selfhost/tests/test_dispatch_pacing.py"""
import importlib.util, json, os, sys, tempfile, time
from pathlib import Path
import os as _os, tempfile as _tf; _os.environ.setdefault("ORQ_CONFIG_FILE", str(Path(_tf.mkdtemp()) / "orq-config.json"))  # never the real file

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("ai_dispatch", Path(os.environ.get("AI_DISPATCH_FILE") or HERE / "ai-dispatch.py"))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

now = time.time()
snap = lambda used, elapsed_min: {"ts": now, "five_pct": used, "five_resets": now + (300 - elapsed_min) * 60}

# (used %, minutes into the window) -> hold?
cases = [
    ((5, 0), False),     # allowed 10
    ((11, 0), True),
    ((39, 23), True),    # the 40-minute burn: allowed 19.6
    ((35, 60), False),   # allowed 35
    ((36, 60), True),
    ((60, 120), False),  # allowed 60
    ((85, 200), False),  # allowed 93
    ((90, 200), True),   # hard stop
    ((89, 290), False),  # allowed capped at 100
    ((95, 299), True),
]
bad = [(c, w, d.pace(snap(*c), now)[0]) for c, w in cases if d.pace(snap(*c), now)[0] != w]
assert not bad, bad

# Weekly window: same straight line over 7 days (WEEK_SLACK on top, WEEK_HARD stop); a low 5h figure doesn't let it through.
wsnap = lambda used, elapsed_h: {**snap(5, 150), "week_pct": used, "week_resets": now + (168 - elapsed_h) * 3600}
week_cases = [
    ((10, 0), False),    # allowed 15
    ((16, 0), True),
    ((43, 48), False),   # allowed 43.6 = 48/168*100 + 15
    ((44, 48), True),
    ((81, 51), True),    # 17 nov: 81% two days in
    ((80, 120), False),  # allowed 86.4
    ((90, 160), True),   # hard stop
    ((89, 167), False),
]
bad = [(c, w, d.pace(wsnap(*c), now)[0]) for c, w in week_cases if d.pace(wsnap(*c), now)[0] != w]
assert not bad, bad
assert d.pace({**snap(5, 150), "week_pct": None, "week_resets": None}, now)[0] is False  # no weekly figure: 5h line only
assert d.pace({**snap(5, 150), "week_pct": 99, "week_resets": now - 1}, now)[0] is False  # weekly window over: ignored
assert d.week_pace(wsnap(81, 51), now)[0] and d.week_pace(snap(5, 150), now) is None

ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1794881400, "rateLimitType": "five_hour", "unifiedWindows": {
    "five_hour": {"utilization": 0.46, "resetsAt": 1794881400}, "seven_day": {"utilization": 0.44, "resetsAt": 1795374000}}}}
out = "\n".join([json.dumps({"type": "system"}), "not json", json.dumps(ev), json.dumps({"type": "result", "result": "ok"})])
p = d.parse_usage(out)
assert (p["five_pct"], p["five_resets"], p["week_pct"]) == (46.0, 1794881400.0, 44.0), p
assert d.parse_usage("") is None and d.parse_usage('{"type": "result"}') is None

with tempfile.TemporaryDirectory() as tmp:
    d.USAGE_FILE, d.BURN_TSV = Path(tmp) / "usage.json", Path(tmp) / "burn.tsv"
    assert d.usage_now() is None  # no file: no data
    d.USAGE_FILE.write_text(json.dumps({**snap(30, 60), "ts": now - 1200}))
    assert d.usage_now(now)["five_pct"] == 30
    d.BURN_TSV.write_text(f"{now - 2000}\t80\n{now - 600}\t41\n{now - 300}\t38\n")  # older than the snapshot is ignored; highest newer wins
    s = d.usage_now(now)
    assert (s["five_pct"], s["source"], s["ts"]) == (41, "statusline", now - 300), s
    d.USAGE_FILE.write_text(json.dumps({**snap(99, 60), "five_resets": now - 1}))
    assert d.usage_now(now) is None  # window over: nothing known

# OpenCode exhausted: until the date Magpie gives ("not asked again before ..."), 24 h for a monthly limit, else 6 h.
with tempfile.TemporaryDirectory() as tmp:
    d.ALERT_FILE = Path(tmp) / "agotado.json"
    t0 = 1794882365.0  # 2026-11-17 02:26 UTC
    put = lambda err, at=t0: d.ALERT_FILE.write_text(json.dumps({"ts": at, "last_event": at, "error": err}))
    put("quota (out of its allowance: not asked again before Nov 25 02:26 UTC+0)")
    assert d.opencode_exhausted(t0 + 7 * 86400) and not d.opencode_exhausted(t0 + 8 * 86400 + 60)
    put("OpenCode Go monthly usage limit reached.")
    assert d.opencode_exhausted(t0 + 20 * 3600) and not d.opencode_exhausted(t0 + 25 * 3600)
    put("rate limited")
    assert d.opencode_exhausted(t0 + 5 * 3600) and not d.opencode_exhausted(t0 + 7 * 3600)
    d.ALERT_FILE.unlink()
    assert not d.opencode_exhausted(t0)

# Free models only once the paid ones are out, never on sensitive boards; one alert going free, one when Go is back.
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    d.ALERT_FILE, d._OC_FREE_FILE, d.FREE_STATE = tmp / "agotado.json", tmp / "free", tmp / "free-state.json"
    d.LOG = tmp / "dispatch.log"  # keep test lines out of the real dispatcher log
    d._OC_SENSITIVE_FILE = tmp / "sensitive"; d._OC_SENSITIVE_FILE.write_text("Board V\n")
    sent = []
    d.notify = lambda title, body, prio="3": sent.append(title)
    t0 = 1794882365.0
    d._OC_FREE_FILE.write_text("opencode/deepseek-v4.1-flash-free\n")
    assert d.free_model_for("Board H") == "opencode/deepseek-v4.1-flash-free" and d.free_model_for("Board V") == ""
    assert d.opencode_usable("Board H", t0) and d.opencode_usable("Board V", t0)  # Go not exhausted: everyone uses Go
    d.ALERT_FILE.write_text(json.dumps({"ts": t0, "last_event": t0, "error": "(out of its allowance: not asked again before Nov 25 02:26 UTC+0)"}))
    assert d.opencode_usable("Board H", t0 + 3600) and not d.opencode_usable("Board V", t0 + 3600)
    d.enter_free_mode("opencode/deepseek-v4.1-flash-free", t0 + 3600)
    d.enter_free_mode("opencode/deepseek-v4.1-flash-free", t0 + 7200)  # only once per exhaustion
    assert len(sent) == 1 and d.FREE_STATE.exists()
    d.paid_release_check(t0 + 86400)  # still exhausted: nothing
    assert len(sent) == 1
    d.paid_release_check(t0 + 9 * 86400)  # past Nov 25: Go back, one alert, state cleared
    assert len(sent) == 2 and "Go" in sent[1] and not d.FREE_STATE.exists()
    d.paid_release_check(t0 + 9 * 86400)
    assert len(sent) == 2
    d._OC_FREE_FILE.write_text("")
    assert not d.opencode_usable("Board H", t0 + 3600)  # no free model configured: nothing usable while Go is out
# Reservations: inside a window Claude only runs for the boards in "solo"; outside it, no effect.
with tempfile.TemporaryDirectory() as tmp:
    d.RESERVE_FILE = Path(tmp) / "reserva.json"
    assert d.claude_reserved("X", now) == ""  # no file
    t = lambda s: time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))
    d.RESERVE_FILE.write_text(json.dumps([{"desde": "2026-11-19 10:00", "hasta": "2026-11-19 15:00", "solo": []},
                                          {"desde": "2026-11-19 15:00", "hasta": "2026-11-20 00:00", "solo": ["Board C"], "motivo": "tarde SiteApp"}]))
    assert d.claude_reserved("Board S", t("2026-11-19 09:59")) == ""
    assert d.claude_reserved("Board C", t("2026-11-19 12:00"))  # morning: nobody
    assert d.claude_reserved("Board S", t("2026-11-19 16:00")) == "tarde SiteApp"
    assert d.claude_reserved("Board C", t("2026-11-19 16:00")) == ""
    assert d.claude_reserved("Board S", t("2026-11-20 00:00")) == ""
    d.RESERVE_FILE.write_text("not json")
    assert d.claude_reserved("X", now) == ""
print(f"ok: {len(cases)} pacing cases, parsing, statusline merge")
