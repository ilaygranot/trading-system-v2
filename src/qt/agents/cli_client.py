"""The agents on Ilay's Claude subscription instead of the API: the same request body, answered by Claude Code headless.

CLAUDE_CODE_OAUTH_TOKEN works only through Claude Code, not the Messages API, so this client turns the request that
qt.agents.context builds into one `claude -p` call: the system blocks as the system prompt, the user turn on stdin,
the card's JSON schema as --json-schema, no tools, no settings, no MCP, no session saved, in an empty working
directory (no CLAUDE.md is read). It answers with the shape AgentRunner reads from the API (content, stop_reason,
model, usage). The cost the CLI reports is the list price of the call; on a subscription it is not billed, but the
runner keeps it so the daily and weekly caps still bound how much the agents run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

TIMEOUT_S = 600


def claude_bin() -> str:
    """QT_CLAUDE_BIN, else ./bin/claude (the Render build copies the binary there), else `claude` on PATH."""
    env = os.environ.get("QT_CLAUDE_BIN")
    if env:
        return env
    local = Path.cwd() / "bin" / "claude"
    return str(local) if local.exists() else (shutil.which("claude") or "claude")


class _Messages:
    def __init__(self, client: "ClaudeCodeClient"):
        self.client = client

    def create(self, **req: Any) -> NS:
        req.pop("betas", None)
        req.pop("fallbacks", None)                  # the server-side fallback is an API beta: not on this path
        return self.client.ask(req)


class ClaudeCodeClient:
    """Duck-types `anthropic.Anthropic()` for AgentRunner: .messages.create and .beta.messages.create."""

    def __init__(self, run=subprocess.run, binary: str | None = None):
        self._run = run
        self.binary = binary or claude_bin()
        self.messages = _Messages(self)
        self.beta = NS(messages=_Messages(self))

    @staticmethod
    def system_text(req: dict) -> str:
        return "\n\n".join(b["text"] for b in req["system"]) if isinstance(req["system"], list) else req["system"]

    def command(self, req: dict, system_file: str) -> list[str]:
        """The system prompt goes in a file: the playbook pack is over Linux's 128 KB limit for one argument."""
        oc = req.get("output_config") or {}
        cmd = [self.binary, "-p", "--output-format", "json", "--model", req["model"], "--system-prompt-file", system_file,
               "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
               "--disable-slash-commands"]
        if oc.get("format", {}).get("schema"):
            cmd += ["--json-schema", json.dumps(oc["format"]["schema"], ensure_ascii=False)]
        if oc.get("effort"):
            cmd += ["--effort", oc["effort"]]
        return cmd

    def ask(self, req: dict) -> NS:
        user = "\n\n".join(m["content"] if isinstance(m["content"], str) else
                           "\n".join(b.get("text", "") for b in m["content"]) for m in req["messages"])
        with tempfile.TemporaryDirectory(prefix="qt-agent-") as cwd:
            sys_file = Path(cwd) / "system.md"
            sys_file.write_text(self.system_text(req), encoding="utf-8")
            p = self._run(self.command(req, str(sys_file)), input=user, capture_output=True, text=True, cwd=cwd, timeout=TIMEOUT_S)
        try:
            body = json.loads(p.stdout or "{}")
        except json.JSONDecodeError:
            body = {}
        from qt.agents.runner import AgentUnavailable
        if p.returncode != 0 and not body:
            raise AgentUnavailable(f"claude -p exited {p.returncode}: {(p.stderr or '')[-300:]}")
        u = body.get("usage") or {}
        out = body.get("structured_output")
        error = body.get("is_error") or body.get("subtype") not in (None, "success") or out is None
        if error and not any(u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                 "cache_creation_input_tokens")):
            # no token read or written: the model was never reached (usage limit, token), not a refusal
            raise AgentUnavailable(f"Claude did not answer: {(body.get('result') or p.stderr or '')[:300]}")
        mu = body.get("modelUsage") or {}
        # Claude Code also calls a small model for its own housekeeping: the served model is the requested one when
        # it is listed, else the one that cost the most
        served = req["model"] if req["model"] in mu else max(mu, key=lambda m: mu[m].get("costUSD", 0), default=req["model"])
        return NS(content=[NS(type="text", text=json.dumps(out, ensure_ascii=False) if out is not None else "")],
                  stop_reason="refusal" if error else "end_turn",
                  model=served,
                  usage=NS(input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                           cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
                           cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0)),
                  cli_cost_usd=body.get("total_cost_usd"), error=None if not error else (body.get("result") or "")[:300])


def agent_client() -> Any | None:
    """The API when ANTHROPIC_API_KEY is set; else Claude Code on CLAUDE_CODE_OAUTH_TOKEN (Ilay's subscription, 22.10);
    else None: the agents are off."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic                            # optional dependency: pip install '.[agents]'
        return anthropic.Anthropic()
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return ClaudeCodeClient()
    return None
