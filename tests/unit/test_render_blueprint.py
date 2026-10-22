"""render.yaml parses, every job gets the secrets it reads, secrets are asked once (on qt-web), and the IB Gateway stays private."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SECRETS = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "FRED_API_KEY", "FMP_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
           "DASHBOARD_PASSWORD"}


def _services():
    return {s["name"]: s for s in yaml.safe_load((ROOT / "render.yaml").read_text())["services"]}


def _env(service):
    return {e["key"]: e for e in service["envVars"]}


def test_blueprint_parses_and_every_cron_gets_the_secrets_from_qt_web():
    services = _services()
    web = _env(services["qt-web"])
    assert all(web[k].get("sync") is False for k in SECRETS)
    crons = [s for s in services.values() if s["type"] == "cron"]
    assert {c["name"] for c in crons} == {"qt-daily", "qt-premarket", "qt-weekly", "qt-monthly"}
    for c in crons:
        env = _env(c)
        for k in SECRETS:
            assert env[k]["fromService"] == {"type": "web", "name": "qt-web", "envVarKey": k}, (c["name"], k)
        assert env["QT_DATABASE_URL"]["fromDatabase"]["name"] == "qt-db"
        # the Telegram links must be the public https URL, not the internal hostname
        assert env["QT_PUBLIC_URL"]["fromService"]["envVarKey"] == "RENDER_EXTERNAL_URL"
        assert env["IBKR_HOST"]["fromService"] == {"type": "pserv", "name": "qt-ibgateway", "property": "host"}
        assert re.fullmatch(r"\S+ \S+ \S+ \S+ \S+", c["schedule"])


def test_ib_gateway_is_private_pinned_paper_and_read_only():
    gw = _services()["qt-ibgateway"]
    assert gw["type"] == "pserv" and gw["runtime"] == "image"
    assert re.search(r":\d+\.\d+\.\w+$", gw["image"]["url"]), "pin a version, never latest/stable"
    env = _env(gw)
    assert env["TRADING_MODE"]["value"] == "paper" and env["READ_ONLY_API"]["value"] == "yes"
    assert env["TWS_USERID"].get("sync") is False and env["TWS_PASSWORD"].get("sync") is False


def test_every_service_that_runs_agents_installs_the_pinned_claude_cli():
    for s in _services().values():
        if s.get("runtime") == "python":
            assert "install.sh | bash -s 2.1.308" in s["buildCommand"] and "bin/claude" in s["buildCommand"], s["name"]
            assert _env(s)["DISABLE_AUTOUPDATER"]["value"] == "1"
