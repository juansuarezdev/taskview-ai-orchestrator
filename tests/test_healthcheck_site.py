"""Production-site checks of healthcheck.py, with fake probes. Run: python3 tests/test_healthcheck_site.py"""
import importlib.util, sys
from pathlib import Path
import os as _os2, tempfile as _tf2; _os2.environ.setdefault("ORQ_SITE_FILE", str(Path(_tf2.mkdtemp()) / "orq-site.json"))  # never the real site file

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("healthcheck", HERE / "healthcheck.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

ok = lambda url: 200
H = "app.example.com"
assert h.site_problems(ok, lambda host: 89, host=H) == []
assert h.site_problems(lambda url: 502 if url.endswith("/up") else 200, lambda host: 89, host=H) == ["/up responde 502"]
assert h.site_problems(lambda url: None, lambda host: 89, host=H) == ["/up responde nada", "/login responde nada"]
assert h.site_problems(ok, lambda host: 13, host=H) == ["al certificado TLS le quedan 13 días"]
assert h.site_problems(ok, lambda host: None, host=H) == ["el certificado TLS no valida"]
assert h.site_problems(lambda url: 1 / 0, lambda host: 1 / 0, host="") == []  # no host configured: nothing probed
print("ok: site checks")
