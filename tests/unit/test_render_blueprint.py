"""render.yaml parses, every job gets the secrets it reads, secrets are asked once (on qt-web), and the IB Gateway stays private."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SECRETS = {"CLAUDE_CODE_OAUTH_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DASHBOARD_PASSWORD"}
# not set on qt-web yet: copying a variable qt-web lacks fails the whole Blueprint sync (22.10)
OPTIONAL = {"ANTHROPIC_API_KEY"}
# the second daily close and the price fallback; set on qt-web 24.10, copied to every cron
PRICE_KEYS = {"FMP_API_KEY", "ALPHAVANTAGE_API_KEY"}
# read by the macro stance and the weekly research only; set on qt-web before this file syncs
FRED_CRONS = {"qt-weekly", "qt-monthly"}


def _services():
    return {s["name"]: s for s in yaml.safe_load((ROOT / "render.yaml").read_text())["services"]}


def _env(service):
    return {e["key"]: e for e in service["envVars"]}


def test_blueprint_parses_and_every_cron_gets_the_secrets_from_qt_web():
    services = _services()
    web = _env(services["qt-web"])
    assert all(web[k].get("sync") is False for k in SECRETS | OPTIONAL | PRICE_KEYS | {"FRED_API_KEY"})
    crons = [s for s in services.values() if s["type"] == "cron"]
    assert {c["name"] for c in crons} == {"qt-daily", "qt-premarket", "qt-open", "qt-weekly", "qt-monthly"}
    for c in crons:
        env = _env(c)
        for k in SECRETS | PRICE_KEYS:
            assert env[k]["fromService"] == {"type": "web", "name": "qt-web", "envVarKey": k}, (c["name"], k)
        assert not OPTIONAL & env.keys(), c["name"]
        if c["name"] in FRED_CRONS:
            assert env["FRED_API_KEY"]["fromService"] == {"type": "web", "name": "qt-web", "envVarKey": "FRED_API_KEY"}
        else:
            assert "FRED_API_KEY" not in env, c["name"]
        assert env["QT_DATABASE_URL"]["fromDatabase"]["name"] == "qt-db"
        # the Telegram links must be the public https URL, not the internal hostname
        assert env["QT_PUBLIC_URL"]["fromService"]["envVarKey"] == "RENDER_EXTERNAL_URL"
        assert env["IBKR_HOST"]["fromService"] == {"type": "pserv", "name": "qt-ibgateway", "property": "host"}
        assert re.fullmatch(r"\S+ \S+ \S+ \S+ \S+", c["schedule"])
        assert env["QT_START_EQUITY"]["value"] == web["QT_START_EQUITY"]["value"]


def test_ib_gateway_is_private_pinned_paper_and_read_only():
    gw = _services()["qt-ibgateway"]
    assert gw["type"] == "pserv" and gw["runtime"] == "image"
    assert re.search(r":\d+\.\d+\.\w+$", gw["image"]["url"]), "pin a version, never latest/stable"
    env = _env(gw)
    assert env["TRADING_MODE"]["value"] == "paper" and env["READ_ONLY_API"]["value"] == "yes"
    assert env["TWS_USERID"].get("sync") is False and env["TWS_PASSWORD"].get("sync") is False
    assert env["EXISTING_SESSION_DETECTED_ACTION"]["value"] == "primaryoverride"     # a deploy overlaps old and new
    # a disk turns off zero-downtime deploys, so old and new never hold the IBKR session at once
    assert gw["disk"]["sizeGB"] >= 1 and not gw["disk"]["mountPath"].startswith("/home/ibgateway/Jts")


def test_every_service_that_runs_agents_installs_the_pinned_claude_cli():
    for s in _services().values():
        if s.get("runtime") == "python":
            assert "install.sh | bash -s 2.1.308" in s["buildCommand"] and "bin/claude" in s["buildCommand"], s["name"]
            assert _env(s)["DISABLE_AUTOUPDATER"]["value"] == "1"
