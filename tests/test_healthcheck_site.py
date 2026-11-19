"""SiteApp production checks of healthcheck.py, with fake probes. Run: python3 selfhost/tests/test_healthcheck_siteapp.py"""
import importlib.util, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("healthcheck", HERE / "healthcheck.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)

ok = lambda url: 200
assert h.siteapp_problems(ok, lambda host: 89) == []
assert h.siteapp_problems(lambda url: 502 if url.endswith("/up") else 200, lambda host: 89) == ["/up responde 502"]
assert h.siteapp_problems(lambda url: None, lambda host: 89) == ["/up responde nada", "/login responde nada"]
assert h.siteapp_problems(ok, lambda host: 13) == ["al certificado TLS le quedan 13 días"]
assert h.siteapp_problems(ok, lambda host: None) == ["el certificado TLS no valida"]
print("ok: siteapp checks")
