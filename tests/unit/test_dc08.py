"""DC-08 (A3): the screen's pattern list for stocks that failed a default rule, and the research judge that asks
the card and puts the agent's long and short back into the screen as a flagged deviation."""
import json
import re
from datetime import date
from types import SimpleNamespace as NS

from qt.agents import AgentRunner
from qt.research import sector_screen
from qt.research.judge import MAX_RESCUE, Judge
from qt import screening as S
from qt.screening import StockRow

AS_OF = date(2023, 4, 3)


def row(t, f0, f1, f2, s0, s1, cap=5e9, price=50, ex="NASDAQ", **kw):
    return StockRow(t, ex, cap, price, s0, s1, f0, f1, f2, **kw)


ROWS = [
    row("AAA", 1.0, 1.2, 1.6, 1e9, 1.3e9),                   # L1
    row("CCC", 1.0, 0.95, 0.9, 1e9, 1.0e9, cap=30e9, short_float=5, days_to_cover=2),
    row("DDD", 1.0, 1.05, 1.1, 1e9, 1.1e9),
    row("ATR", 1.0, -0.5, 0.8, 1e9, 1.3e9),                  # profit -> loss -> profit: S06-R41 (ATRO)
    row("JMP", 0.2, 0.3, 1.2, 1e9, 1.0e9),                   # flat sales (S06-R22), EG2 300%: extreme expectations
    row("TNY", 0.2, 0.3, 1.2, 1e9, 1.0e9, cap=1e8),          # the same, under the hard market-cap floor
    row("BAX", -3.0, 0.05, 3.0, 1e9, 1.0e9, cap=100e9),      # loss -> near zero -> jump (BA 2023)
]


def test_pattern_list_on_default_failures_only():
    r = S.run(ROWS)
    res = {x.ticker: x for x in r.rescue}
    assert "S06-R41" in r.rejected["ATR"] and res["ATR"].patterns == ["no_profile"]
    assert "extreme_expectations" in res["JMP"].patterns
    assert "TNY" not in res                                   # failed a hard floor: never reaches the card
    assert "AAA" not in res and "BAX" not in res              # passed / already on the short side flagged
    assert set(S.PATTERNS.values()) == {"S06-J6", "S06-R42", "S06-J15"}


class RescueClient:
    """DC-08 answers by symbol; the Critic passes."""
    def __init__(self, by_sym):
        self.by_sym, self.asked = by_sym, []
        self.beta = NS(messages=NS(create=self._create))
        self.messages = NS(create=self._create)

    def _create(self, **req):
        msg = req["messages"][0]["content"]
        if "verdict" in req["output_config"]["format"]["schema"]["properties"]:
            payload = {"verdict": "pass", "issues": []}
        else:
            assert re.search(r'<decision_card id="DC-08"', msg)
            world = json.loads(msg.split("<world_state>\n", 1)[1].split("\n</world_state>", 1)[0])
            self.asked.append((world["symbol"], msg))
            payload = {"option": self.by_sym[world["symbol"]], "confidence": "medium", "reasoning": "נימוק",
                       "fact_ids": ["F-001"], "cited_rules": ["S06-J6"], "deviations": [], "missing_facts": [],
                       "not_from_course": []}
        return NS(content=[NS(type="text", text=json.dumps(payload, ensure_ascii=False))], stop_reason="end_turn",
                  model=req["model"], usage=NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                                               cache_creation_input_tokens=0))


MACRO = {"stage2": {"stance": {"zone": "cold", "bias": "bearish"}}}


def test_judge_rescue_adds_long_and_short_and_drops_a_turned_down_override(db):
    screens = sector_screen.screens({"XLI": ROWS}, ["XLI"], ["XLI"])
    client = RescueClient({"ATR": "no", "JMP": "long", "BAX": "watch_only"})
    out = Judge(AgentRunner(db, client), run_id="r").rescue(screens, MACRO, {"long": ["XLI"], "short": ["XLI"]},
                                                            AS_OF, lambda s: {"3m": 12.0, "6m": 30.0})
    x = out["XLI"]
    assert sorted(s for s, _ in client.asked) == ["ATR", "BAX", "JMP"]
    longs = {c["ticker"]: c for c in x["long"]}
    assert "dc08" in longs["JMP"]["tags"] and "S06-R42" in longs["JMP"]["conflicts"][0] and "ATR" not in longs
    assert "BAX" not in {c["ticker"] for c in x["short"]}                       # watch_only: off the short side
    assert x["dc08"]["BAX"]["option"] == "watch_only" and x["dc08"]["ATR"]["option"] == "no"
    jmp = dict(client.asked)["JMP"]
    assert "JMP.move_pct.3m = 12.0" in jmp and "JMP.patterns" in jmp and "XLI.group_avg.eg1" in jmp
    assert screens["XLI"]["short"] and {c["ticker"] for c in screens["XLI"]["short"]} >= {"BAX"}   # input untouched


def test_judge_rescue_keeps_an_override_on_short_and_caps_the_calls(db):
    rows = ROWS + [row(f"J{i}", 0.2, 0.3, 1.2, 1e9, 1.0e9) for i in range(MAX_RESCUE)]
    screens = sector_screen.screens({"XLI": rows}, ["XLI"], ["XLI"])
    client = RescueClient({**{f"J{i}": "no" for i in range(MAX_RESCUE)}, "ATR": "no", "JMP": "no", "BAX": "short"})
    out = Judge(AgentRunner(db, client)).rescue(screens, MACRO, {"long": ["XLI"]}, AS_OF, lambda s: {})
    assert len(client.asked) == MAX_RESCUE
    skipped = [s for s, v in out["XLI"]["dc08"].items() if v["option"] is None]
    assert len(skipped) == 3 and "BAX" in {c["ticker"] for c in out["XLI"]["short"]}
    assert all("תקרה" in out["XLI"]["dc08"][s]["reason"] for s in skipped)


def test_report_shows_the_dc08_answers():
    from qt.research import report
    d = {"timing": [], "ideas": {}, "screens": {"XLI": {"n": 3, "long": [], "short": [], "rejected_count": 2,
         "rejected_by_rule": {"S06-R41": 1}, "dc08": {"ATR": {"option": "no", "reason": "אין עקביות", "failed": ["S06-R41"],
                                                           "patterns": ["no_profile"]}}}}}
    html = report._screen(d)
    assert "DC-08" in html and "אין עקביות" in html and "S06-R41" in html
