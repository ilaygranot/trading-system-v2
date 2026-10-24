"""The last earnings call from roic.ai and DC-26, the course's questions to it: the turns and who speaks, the latest
call before the run date, excerpts kept only word for word, and the kept excerpts reaching the cards and the article."""
import copy
from datetime import date

from qt.agents import AgentRunner
from qt.data import transcripts as TR
from qt.data.transcripts import RoicTranscripts, parse_turns
from qt.facts import FactSheet
from qt.research import article as AR
from qt.research import earnings_call as EC
from qt.research import judge as JG
from qt.research import magazine, report
from test_judge import AS_OF, FIX, SYM, CardClient, _answer, _dc, _deep_dive, _timing, _world

CONTENT = """Operator: Welcome to Shell's Second Quarter 2026 Financial Results Announcement. [Operator Instructions]
Wael Sawan: Welcome, everyone. Our refineries achieved a record 102% utilization in a high-margin period. We expect Q3 production between 2.6 million and 2.8 million barrels a day.
Sinead Gorman: Thank you, Wael. Adjusted earnings for the quarter were $7.1 billion. We are announcing a $3.5 billion buyback for the next 3 months.
Operator: Our first caller is Biraj Borkhataria from RBC.
Biraj Borkhataria: Can you talk about the LNG Canada ramp-up and the lost volumes from Qatar?
Wael Sawan: Thanks, Biraj. LNG Canada achieved full capacity this quarter. We continue to see strong demand for LNG in Asia.
Operator: Our next caller is Kim Fustier from HSBC.
Kim Fustier: On the Downstream, how sustainable are these margins?
Sinead Gorman: Margins will normalize in Q3 as turnarounds come back.
"""
TR_SHEL = {"date": "2026-07-30", "year": 2026, "quarter": 2, "source": "roic:earnings-call SHEL 2026Q2",
           "url": "https://www.roic.ai/quote/SHEL/transcripts/2026-year/2-quarter", "turns": parse_turns(CONTENT)}


def test_turns_know_the_speaker_their_role_and_the_section():
    t = parse_turns(CONTENT)
    who = [(x["speaker"], x["role"], x["section"]) for x in t]
    assert who[1] == ("Wael Sawan", "management", "prepared") and who[2] == ("Sinead Gorman", "management", "prepared")
    assert who[4] == ("Biraj Borkhataria", "analyst", "qa") and who[5] == ("Wael Sawan", "management", "qa")
    assert who[7] == ("Kim Fustier", "analyst", "qa") and who[8] == ("Sinead Gorman", "management", "qa")
    assert who[0][1] == "operator" and [x["n"] for x in t] == list(range(9))


def test_roic_latest_takes_the_last_call_known_on_the_run_date(monkeypatch):
    urls = []
    calls = [{"symbol": "SHEL", "year": 2026, "quarter": 3, "date": "2026-10-30T00:00:00.000Z"},
             {"symbol": "SHEL", "year": 2026, "quarter": 2, "date": "2026-07-30T00:00:00.000Z"},
             {"symbol": "SHEL", "year": 2025, "quarter": 1, "date": "2025-05-02 10:00:00"}]

    def fake(url, store=None, max_age_s=0, **k):
        urls.append(url)
        return calls if "/list/" in url else {"symbol": "SHEL", "year": 2026, "quarter": 2,
                                               "date": "2026-07-30T00:00:00.000Z", "content": CONTENT}
    monkeypatch.setattr(TR, "get_json", fake)
    from qt.data.textfetch import RateLimiter
    t = RoicTranscripts(key="k", limiter=RateLimiter(0)).latest("shel", date(2026, 10, 24))
    assert (t["year"], t["quarter"], t["date"]) == (2026, 2, "2026-07-30")       # Q3 was after the run date
    assert t["url"] == "https://www.roic.ai/quote/SHEL/transcripts/2026-year/2-quarter"
    assert t["source"] == "roic:earnings-call SHEL 2026Q2" and len(t["turns"]) == 9
    assert "transcript/SHEL?year=2026&quarter=2" in urls[1]
    assert all("api.roic.ai" in u for u in urls)                              # never FMP or Alpha Vantage
    assert RoicTranscripts(key="").latest("SHEL", date(2026, 10, 24)) is None


def test_every_question_is_from_the_course_and_asked_once():
    assert len(EC.QUESTIONS) == 12 and len(set(EC.QUESTION_IDS)) == 12
    for q, he, look, rules, feeds in EC.QUESTIONS:
        assert he and look and rules and set(feeds) <= set(AR.SECTION_KEYS), q
    w = EC.world(SYM, TR_SHEL)
    assert [x["id"] for x in w["questions"]] == list(EC.QUESTION_IDS)


def _sheet():
    fs, by_id = EC.call_sheet(SYM, TR_SHEL, AS_OF)
    fid = {t["n"]: f for f, t in by_id.items()}
    return fs, by_id, fid


def test_only_word_for_word_excerpts_survive(monkeypatch):
    fs, by_id, fid = _sheet()
    assert "Operator" not in fs.render() and fs.get(fid[1]).source == "roic:earnings-call SHEL 2026Q2 Wael Sawan"
    out = {"answers": [
        {"question": "guidance", "found": True, "he": "ההנהלה צופה תפוקה של 2.6 עד 2.8 מיליון חביות ביום.",
         "excerpts": [{"fact_id": fid[1], "quote": "We expect Q3 production between 2.6 million and 2.8 million barrels a day."}]},
        {"question": "capital", "found": True, "he": "באייבק של 5 מיליארד.",            # 5 is not in the excerpt
         "excerpts": [{"fact_id": fid[2], "quote": "We are announcing a $3.5 billion buyback for the next 3 months."}]},
        {"question": "demand", "found": True, "he": "ביקוש חזק.",
         "excerpts": [{"fact_id": fid[5], "quote": "We see very strong demand for LNG everywhere."}]},     # paraphrase
        {"question": "analyst_pushback", "found": True, "he": "",
         "excerpts": [{"fact_id": fid[7], "quote": "how sustainable are these margins?"},
                      {"fact_id": "F-999", "quote": "Margins will normalize in Q3"}]}]}
    call = EC.verify(out, by_id, TR_SHEL)
    a = {x["q"]: x for x in call["answers"]}
    assert a["guidance"]["found"] and a["guidance"]["he"] and a["guidance"]["excerpts"][0]["speaker"] == "Wael Sawan"
    assert a["capital"]["found"] and a["capital"]["he"] == ""                 # the quote stays, the summary goes
    assert not a["demand"]["found"] and a["demand"]["excerpts"] == []
    assert [x["role"] for x in a["analyst_pushback"]["excerpts"]] == ["analyst"]
    assert not a["tone"]["found"] and call["found"] == 3 and call["asked"] == 12
    assert {d["why"] for d in call["dropped"]} == {"not word for word in the turn", "not a turn of the call",
                                                   "a number in the summary is not in the excerpts"}

    sheet = FactSheet()
    EC.call_facts(sheet, SYM, call, AS_OF)
    keys = {f["key"]: f for f in sheet.to_list()}
    g = keys["SHEL.call.guidance[0]"]
    assert g["source"] == "roic:earnings-call 2026Q2 Wael Sawan" and g["url"] == TR_SHEL["url"]
    assert g["as_of"] == "2026-07-30" and keys["SHEL.call.guidance.summary"]["source"] == "agent:DC-26"
    assert "SHEL.call.not_found" in keys
    # the article cites the call like a document: its numbers stand, a number the call never said does not
    gid = next(f["fact_id"] for f in sheet.to_list() if f["key"] == "SHEL.call.guidance[0]")
    ok = {"text": "ההנהלה צופה תפוקה של 2.6 מיליון עד 2.8 מיליון חביות ביום ברבעון השלישי.", "fact_ids": [gid], "quotes": [
        {"fact_id": gid, "text": "We expect Q3 production between 2.6 million and 2.8 million", "he": "תחזית"}]}
    assert AR.check_sentence(ok, sheet) is None
    assert AR.check_sentence(dict(ok, text="ההנהלה צופה 3.1 מיליון חביות ביום.", quotes=[]), sheet)
    assert AR.source_label(g["source"]).startswith("roic.ai")


def test_dc26_runs_first_and_its_excerpts_reach_the_later_cards(db):
    fs, by_id, fid = _sheet()
    answers = [{"question": q, "found": False, "he": "", "excerpts": []} for q in EC.QUESTION_IDS]
    answers[0] = {"question": "guidance", "found": True, "he": "תחזית תפוקה לרבעון השלישי.",
                  "excerpts": [{"fact_id": fid[1], "quote": "We expect Q3 production between 2.6 million and 2.8 million barrels a day."}]}
    client = CardClient({"DC-26": _answer("DC-26", "answers", answers=answers)})
    docs = copy.deepcopy(FIX["ideas"][SYM].get("documents") or {}) | {"transcript": copy.deepcopy(TR_SHEL)}
    j = JG.Judge(AgentRunner(db, client), run_id="weekly-x").idea(
        _timing(1)[0] | {"sym": SYM}, _deep_dive([], documents=docs), FIX["macro"], AS_OF)
    assert client.cards[0] == "DC-26" and client.cards[1] == "DC-10" and client.cards[-1] == "DC-25"
    w = _world(_dc(client, "DC-26")[0])
    assert len(w["questions"]) == 12 and w["call"] == "2026Q2 (2026-07-30)"
    assert "Biraj Borkhataria (אנליסט, שאלות ותשובות)" in _dc(client, "DC-26")[0]
    assert j["call"]["found"] == 1 and j["call"]["answers"][0]["excerpts"][0]["speaker"] == "Wael Sawan"
    for card in ("DC-10", "DC-13", "DC-25"):
        assert "SHEL.call.guidance[0]" in _dc(client, card)[0], card
    assert "SHEL.call.turn[" not in _dc(client, "DC-10")[0]                 # the full call stays on DC-26's sheet
    # the page shows the questions and the verified excerpt next to the management chapter
    i = copy.deepcopy(FIX["ideas"][SYM]) | {"judgment": FIX["ideas"][SYM]["judgment"] | {"call": j["call"]}}
    html = report.call_box(i)
    assert "מה שאלנו את שיחת הוועידה האחרונה" in html and "1 מתוך 12" in html and "2.8 million barrels" in html
    assert "לא נאמר בשיחה" in html and TR_SHEL["url"] in html
    assert ("management", "מה ההנהלה אומרת", ("management",), ("call", "guidance")) in magazine.CHAPTERS


def test_no_transcript_no_dc26(db):
    client = CardClient()
    j = JG.Judge(AgentRunner(db, client), run_id="weekly-x").idea(
        _timing(1)[0] | {"sym": SYM}, _deep_dive([]), FIX["macro"], AS_OF)
    assert "DC-26" not in client.cards and "call" not in j


def test_documents_carry_the_call_until_the_agents_are_done():
    from qt.research import run

    class Sec:
        def documents(self, sym, as_of):
            return {"symbol": sym, "transcript": None, "errors": []}

    class Calls:
        def ok(self):
            return True

        def latest(self, sym, as_of):
            return copy.deepcopy(TR_SHEL)
    memo = {}
    docs = run.documents_source(Sec(), AS_OF, memo, Calls())
    assert len(docs(SYM)["transcript"]["turns"]) == 9
    run.strip_turns(memo)
    assert "turns" not in memo[SYM]["transcript"] and memo[SYM]["transcript"]["turns_n"] == 9
    assert memo[SYM]["transcript"]["url"] == TR_SHEL["url"]

    class Down(Calls):
        def latest(self, sym, as_of):
            raise TR.ProviderError("https://api.roic.ai/x?apikey=SECRET: HTTP 500")
    bad = run.documents_source(Sec(), AS_OF, {}, Down())(SYM)
    assert bad["transcript"] is None and "SECRET" not in bad["errors"][0] and "transcript:" in bad["errors"][0]


def test_a_ceo_who_only_answers_questions_is_management():
    t = parse_turns("Operator: Good afternoon.\nToshiya Hari: Welcome to the call.\nColette Kress: Record revenue.\n"
                    "Operator: [Operator Instructions] Your first question comes from Joseph Moore with Morgan Stanley.\n"
                    "Joseph Moore: Can you give us color on the 70%?\nJen-Hsun Huang: Yes. Thanks, Joe.\n"
                    "Joseph Moore: And a follow-up?\nColette Kress: Sure.\n"
                    "Operator: Your next question comes from Stacy Rasgon.\nStacy Rasgon: On fiscal 28.\n"
                    "Jen-Hsun Huang: The unconstrained would be higher.\n")
    assert [(x["speaker"], x["role"]) for x in t if x["role"] != "operator"] == [
        ("Toshiya Hari", "management"), ("Colette Kress", "management"), ("Joseph Moore", "analyst"),
        ("Jen-Hsun Huang", "management"), ("Joseph Moore", "analyst"), ("Colette Kress", "management"),
        ("Stacy Rasgon", "analyst"), ("Jen-Hsun Huang", "management")]
