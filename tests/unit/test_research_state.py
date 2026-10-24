"""A weekly research that stops continues where it stopped (qt.research.state, Ilay 24.10): the stages and each
agent piece are kept in the database; a run of the same week asks the agents only for what is missing."""
import copy
from datetime import date

from qt.agents import AgentRunner
from qt.agents.runner import BudgetExceeded
from qt.research import judge as JG
from qt.research.state import State, seed_from_dossier
from test_judge import AS_OF, FIX, CardClient, _deep_dive, _timing


def _stages(n=2):
    return {"as_of": AS_OF, "macro": FIX["macro"], "timing": _timing(n)}


def test_state_keeps_json_and_blobs_by_week(db):
    s = State(db, AS_OF)
    s.put("stage:x.json", {"a": [1, 2], "d": date(2026, 1, 2)})
    s.put("stage:x.json", {"a": [3]})                                  # the last write wins
    s.put_blob("stage:u.pkl", {"quotes": {"X": (1, 2)}})
    assert s.get("stage:x.json") == {"a": [3]} and s.get_blob("stage:u.pkl") == {"quotes": {"X": (1, 2)}}
    assert State(db, date(2026, 10, 27)).get("stage:x.json") is None   # another week sees nothing
    s.clear()
    assert s.keys() == [] and s.get("stage:x.json") is None


def test_a_second_run_of_the_week_asks_nothing_again(db):
    st = State(db, AS_OF)
    first = JG.Judge(AgentRunner(db, CardClient()), state=st)(_stages(), _deep_dive([]))
    client = CardClient()
    again = JG.Judge(AgentRunner(db, client), state=st)(_stages(), _deep_dive([]))
    assert client.cards == [] and set(again["ideas"]) == {"S0", "S1"}
    assert all(j["article"] == first["ideas"][s]["article"] for s, j in again["ideas"].items())
    assert again["A1"] == first["A1"] and "incomplete" not in again and "A1" in again["continued"]


def test_a_budget_cut_marks_the_run_incomplete_and_the_resume_asks_only_what_is_missing(db):
    st, n = State(db, AS_OF), []

    class Broke(AgentRunner):                     # the budget runs out on the second idea
        def decide(self, agent, card_id, *a, subject=None, **k):
            if subject == "S1" or (card_id == "DC-25" and n):
                raise BudgetExceeded("agent budget $25 reached")
            if card_id == "DC-13":
                n.append(subject)
            return super().decide(agent, card_id, *a, subject=subject, **k)
    J = JG.Judge(Broke(db, CardClient()), state=st)(_stages(3), _deep_dive([]))
    assert set(J["ideas"]) == {"S0"} and J["incomplete"] == ["S1", "S2", "S0:article"]
    client = CardClient()
    J = JG.Judge(AgentRunner(db, client), state=st)(_stages(3), _deep_dive([]))
    per_idea = [c for c in client.cards if c not in ("DC-25",)]
    assert client.cards.count("DC-25") == 3 and per_idea[0] == "DC-10" and per_idea.count("DC-13") == 2   # S1, S2
    assert "DC-01" not in client.cards and set(J["ideas"]) == {"S0", "S1", "S2"} and "incomplete" not in J
    assert not J["A3"].get("cut_by_budget")


def test_a_report_from_before_the_state_seeds_the_week(db):
    d = copy.deepcopy(FIX)
    d["meta"]["as_of"] = AS_OF.isoformat()
    sym = next(iter(d["ideas"]))
    j = d["ideas"][sym]["judgment"]
    j.pop("article", None)
    d["judgments"]["A3"] = {"picked": [sym, "EOG"], "skipped": {"EOG": "הניתוח נכשל: BudgetExceeded"},
                            "cut_by_budget": []}
    d["judgments"]["errors"] = {"EOG": "BudgetExceeded: agent budget $25 reached"}
    st = State(db, AS_OF)
    assert seed_from_dossier(st, d) == list(d["ideas"])
    assert seed_from_dossier(st, d) == []                              # once: the state is the record after that
    assert st.get("agents:A3")["skipped"] == {} and st.get(f"agents:idea:{sym}")["j"]["decision"] == j["decision"]
    other = State(db, date(2026, 10, 30))
    assert seed_from_dossier(other, d) == [] and other.keys() == []   # another week's report is not this week's


def test_a_stage_in_the_state_is_not_fetched_again_in_a_new_container(db, tmp_path):
    from qt.research.run import _stage
    st, calls = State(db, AS_OF), []

    def fetch():
        calls.append(1)
        return {"quotes": {"X": 1}}
    assert _stage(tmp_path / "a", st, "universe.pkl", fetch, binary=True) == {"quotes": {"X": 1}}
    assert _stage(tmp_path / "a", st, "timing.json", lambda: [{"sym": "X"}]) == [{"sym": "X"}]
    # a new container: an empty work dir
    assert _stage(tmp_path / "b", st, "universe.pkl", fetch, binary=True) == {"quotes": {"X": 1}} and calls == [1]
    assert _stage(tmp_path / "b", st, "timing.json", lambda: 1 / 0) == [{"sym": "X"}]
    assert (tmp_path / "b" / "timing.json").exists()


def test_yahoo_waits_out_a_429_on_the_crumb(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from qt.research.yahoo import Yahoo
    y, codes = Yahoo(tmp_path), [429, 429, 200]
    monkeypatch.setattr(Yahoo, "CRUMB_WAITS", (0, 0, 0))

    def get(url, timeout):
        if "getcrumb" not in url:
            return NS(status_code=200)
        c = codes.pop(0)
        return NS(status_code=c, text="abc ", raise_for_status=lambda: None)
    monkeypatch.setattr(y.s, "get", get)
    assert y.crumb() == "abc" and codes == []
