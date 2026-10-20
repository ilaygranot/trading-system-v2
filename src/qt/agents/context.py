"""The five context parts, in this order (architecture 6): role and limits, the Playbook pack for the stage,
the fact sheet, the relevant state of the world, the decision card. Parts 1-2 are fixed per agent and
cached (runtime plan 8.4); parts 3-5 change per call and come after the cache breakpoint.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from qt.agents.cards import Card
from qt.facts import FactSheet
from qt.playbook import DEFAULT_TIERS_DIR, Tier, load_playbook

CHAPTERS = DEFAULT_TIERS_DIR / "chapters"

STAGES = {"A1": ["S01", "S02", "S03"], "A2": ["S04", "S05"], "A3": ["S06"], "A4": ["S07"], "A5": ["S08"],
          "A6": ["S09", "S10"], "A7": ["S11", "S12"], "A8": ["S13"]}

ROLES = {
    "A1": "the macro agent (stages 1-3). You decide the grey band, the compass quadrant, macro vs market, confidence. "
          "You never compute scores: the code did.",
    "A2": "the sectors and industries agent (stages 4-5). You decide a sector's macro score, sub-industry type "
          "overrides and industry tags. You never rank RS or compute growth averages: the code did.",
    "A3": "the idea hunter (stage 6). You decide which candidates that failed a default rule deserve the deep dive, "
          "and which to pick from the spread. You never classify profiles: the code did.",
    "A4": "the deep-dive analyst (stage 7). You decide the story, whether the premium is justified, the target, the "
          "thesis breakers, and whether the idea goes to the watchlist. You never decide timing or size.",
    "A5": "the watchlist and catalysts agent (stage 8). You classify catalysts, judge 'already priced in', and "
          "propose heat changes. Ilay approves every heat change.",
    "A6": "the timing and trade-construction agent (stages 9-10). You decide the entry trigger, the instrument, "
          "strikes and expiry, the number of tranches, k inside the allowed range. Size and R:R come from code; "
          "you never exceed a cap.",
    "A7": "the risk officer (stages 11-12). You classify events on open positions and the response, the regime grey "
          "band, and technical profit-taking flags. You never cancel a stop or a hard block.",
    "A8": "the scheduler and discipline agent (stage 13). You detect 'trading sickness' in the weekly report. You "
          "never decide a trade.",
    "C": "the Critic. You decide nothing. You check that a decision stands on its card: every cited fact exists on "
         "the fact sheet and says what is claimed, every cited course rule exists and says what is claimed, nothing "
         "'not from course' is hidden, no hard rule is crossed, and every deviation from a default has a J principle "
         "and input facts.",
    "L": "the review and learning agent. You classify failures (idea / execution / Playbook / data) and draft change "
         "proposals. You never change the Playbook.",
}

PRINCIPLES = """Binding principles (architecture 2):
1. The course (lessons 059-107) is the base and beats any recommendation. Anything not from the course is marked
   "not from course" and is allowed only where the course is silent. Between lessons, the application series
   (096-107) wins.
2. Three layers: hard (money and risk: the code blocks), default (returns a flag, not a veto), full judgment.
3. Numbers come from code, not from you. Never compute ADR, R:R, macro scores or profiles yourself.
4. No fact without a source. Use only facts on the fact sheet (cite their F ids). A fact you cannot cite does not
   exist; if a required fact is missing, choose the card's 'when undecided' option and list it in missing_facts.
5. Between stages pass flags and findings, not verdicts.
6. Every flexibility is declared: a deviation from a default rule needs a course principle (a J id), input facts,
   and goes to Ilay marked as a deviation.
7. Ilay approves every action that moves money. You recommend; nothing you output sends an order.
You decide only through the decision card you are given, choosing one of its closed options. A question without a
card is flagged to Ilay, not decided."""


@lru_cache(maxsize=None)
def playbook_pack(agent: str) -> tuple[str, str]:
    """Part 2: the chapters of the agent's stages, the binding decisions, and every R rule's tier.
    Returns (text, version). Built from the Playbook snapshot, so a Playbook change reaches every agent.
    The Critic gets the pack of the agent it checks (pass that agent's id); L gets every stage."""
    pb = load_playbook()
    stages = STAGES.get(agent) or sorted({s for v in STAGES.values() for s in v})
    parts = []
    for s in stages:
        for f in sorted(CHAPTERS.glob(f"{s}-*.md")):
            parts.append(f"<chapter id=\"{s}\">\n{f.read_text(encoding='utf-8')}\n</chapter>")
        tiers = [f"{r.rule_id}: {r.tier.value}" for rid, r in sorted(pb.rules.items()) if rid.startswith(s + "-")
                 and r.tier is not Tier.JUDGMENT]
        parts.append(f"<tiers stage=\"{s}\">\n" + "\n".join(tiers) + "\n</tiers>")
    parts.append(f"<decisions>\n{(CHAPTERS / 'decisions.md').read_text(encoding='utf-8')}\n</decisions>")
    text = "\n\n".join(parts)
    return text, hashlib.sha256(text.encode()).hexdigest()[:12]


def role(agent: str) -> str:
    return f"You are {ROLES[agent]}\n\n{PRINCIPLES}"


def build_request(agent: str, card: Card, facts: FactSheet, world: dict[str, Any], model: str, effort: str,
                  max_tokens: int, retry_note: str | None = None, pack_for: str | None = None,
                  schema: dict | None = None, extra_user: str = "") -> dict[str, Any]:
    """Messages API request body: system = parts 1-2 (cached), user = parts 3-5 (+ the retry note)."""
    pack, _ = playbook_pack(pack_for or agent)
    user = (f"<fact_sheet>\n{facts.render() or '(empty)'}\n</fact_sheet>\n\n"
            f"<world_state>\n{json.dumps(world, ensure_ascii=False, sort_keys=True, default=str)}\n</world_state>\n\n"
            f"<decision_card id=\"{card.card_id}\">\n{card.text}\n\nClosed options (codes): {', '.join(card.options)}. "
            f"When undecided: {card.undecided}.\n</decision_card>")
    if extra_user:
        user += "\n\n" + extra_user
    if retry_note:
        user += f"\n\n<previous_output_rejected>\n{retry_note}\n</previous_output_rejected>"
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": role(agent)},
                   {"type": "text", "text": pack, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema or card.schema()}},
    }
