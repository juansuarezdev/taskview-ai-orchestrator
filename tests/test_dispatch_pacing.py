"""Usage pacing of ai-dispatch.py: snapshot parsing, the pacing line and the statusline merge. Run: python3 selfhost/tests/test_dispatch_pacing.py"""
import importlib.util, json, os, sys, tempfile, time
from pathlib import Path

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
print(f"ok: {len(cases)} pacing cases, parsing, statusline merge")
