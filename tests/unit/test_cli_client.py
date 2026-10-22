"""The agents through Claude Code headless (qt.agents.cli_client): the command line, the answer read back in the
API's shape, and a full decision through AgentRunner with the Critic."""
import json
import re
from types import SimpleNamespace as NS

from qt.agents import AgentRunner
from qt.agents import cli_client as CC
from qt.facts import FactSheet
from datetime import date


def _body(out, cost=0.01, error=False):
    return json.dumps({"type": "result", "subtype": "error_during_execution" if error else "success",
                       "is_error": error, "result": "x", "structured_output": out, "total_cost_usd": cost,
                       "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 50,
                                 "cache_creation_input_tokens": 0},
                       "modelUsage": {"claude-sonnet-5-5": {"costUSD": cost}}})


class FakeRun:
    """Answers `claude -p` by the card in stdin; the Critic (its schema has a verdict) passes."""
    def __init__(self, error_first=False):
        self.calls, self.error_first = [], error_first

    def __call__(self, cmd, input, capture_output, text, cwd, timeout):
        self.calls.append(dict(cmd=cmd, input=input, cwd=cwd,
                               system=open(cmd[cmd.index("--system-prompt-file") + 1], encoding="utf-8").read()))
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        if self.error_first and len(self.calls) == 1:
            return NS(returncode=1, stdout=_body(None, error=True), stderr="")
        if "verdict" in schema["properties"]:
            out = {"verdict": "pass", "issues": []}
        else:
            card = re.search(r'<decision_card id="(DC-\d\d)"', input).group(1)
            out = {"option": "hold", "confidence": "medium", "reasoning": f"r {card}", "fact_ids": ["F-001"],
                   "cited_rules": ["S12-R11"], "deviations": [], "missing_facts": [], "not_from_course": []}
        return NS(returncode=0, stdout=_body(out), stderr="")


def _facts():
    fs = FactSheet()
    fs.add("BA.close", 200.0, "fake", date(2026, 10, 21))
    return fs


def test_command_has_no_tools_no_settings_and_the_schema():
    c = CC.ClaudeCodeClient(run=FakeRun(), binary="/x/claude")
    req = {"model": "claude-opus-5-5", "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
           "messages": [], "output_config": {"effort": "high", "format": {"schema": {"type": "object"}}}}
    cmd = c.command(req, "/tmp/s.md")
    assert cmd[:3] == ["/x/claude", "-p", "--output-format"] and cmd[cmd.index("--system-prompt-file") + 1] == "/tmp/s.md"
    assert c.system_text(req) == "A\n\nB"
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in cmd and cmd[cmd.index("--effort") + 1] == "high"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}


def test_a_decision_runs_through_claude_code_with_the_critic(db):
    run = FakeRun()
    r = AgentRunner(db, CC.ClaudeCodeClient(run=run, binary="claude"))
    res = r.decide("A7", "DC-21", _facts(), {"position": {"symbol": "BA"}}, run_id="d", step="agents", subject="BA")
    assert res.status == "decided" and res.option == "hold" and len(run.calls) == 2      # the agent, then the Critic
    assert run.calls[0]["cwd"] != run.calls[1]["cwd"] and "BA.close" in run.calls[0]["input"]
    assert len(run.calls[0]["system"]) > 50_000                                           # the playbook pack, by file
    rows = db.all("SELECT model, input_tokens, cost_usd FROM agent_calls", json_cols=())
    assert rows[0]["model"] == "claude-sonnet-5-5" and rows[0]["input_tokens"] == 1200 and rows[0]["cost_usd"] > 0


def test_an_error_is_read_as_no_answer_and_retried(db):
    run = FakeRun(error_first=True)
    res = AgentRunner(db, CC.ClaudeCodeClient(run=run, binary="claude")).decide(
        "A7", "DC-21", _facts(), {}, run_id="d", step="agents", subject="BA")
    assert res.status == "decided" and len(run.calls) == 3                               # failed, retried, Critic


def test_agent_client_picks_the_api_key_first(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert CC.agent_client() is None
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    assert isinstance(CC.agent_client(), CC.ClaudeCodeClient)
