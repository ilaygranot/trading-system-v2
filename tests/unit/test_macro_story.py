"""The market story of the week (DC-27, qt.research.macro_story): the headlines and moves it stands on, the code's
check of each sentence and event, and that a run without it is not finished (Ilay 24.10)."""
from datetime import date

from qt.agents import AgentRunner
from qt.facts import FactSheet
from qt.research import judge as JG
from qt.research import macro_story as MS
from qt.research import report
from qt.research.state import State
from test_judge import AS_OF, FIX, CardClient, _answer, _deep_dive, _timing

RSS = """<rss><channel>
<item><title>Fed raises rates by a quarter point - Reuters</title><link>https://news.example/fed</link>
<pubDate>Wed, 16 Sep 2026 18:00:00 GMT</pubDate><source url="https://reuters.com">Reuters</source></item>
<item><title><![CDATA[Oil jumps 13% as Hormuz closes &amp; LNG halts]]></title><link>https://news.example/oil</link>
<pubDate>Mon, 02 Mar 2026 09:00:00 GMT</pubDate></item>
<item><title>Too new - Reuters</title><link>https://news.example/new</link><pubDate>Fri, 30 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>No date</title><link>https://news.example/x</link></item>
</channel></rss>"""


def S(text, *ids):
    return {"text": text, "fact_ids": list(ids), "quotes": []}


def test_rss_items_carry_date_publisher_and_link():
    got = MS.parse_rss(RSS, "Google News")
    assert got[0] == {"date": "2026-09-16", "title": "Fed raises rates by a quarter point", "publisher": "Reuters",
                      "url": "https://news.example/fed"}
    assert got[1]["title"] == "Oil jumps 13% as Hormuz closes & LNG halts" and got[1]["publisher"] == "Google News"
    assert len(got) == 3                                                  # the item without a date is dropped


def test_fetch_news_keeps_the_window_once_per_title_and_lists_failures():
    calls = []

    def get(url):
        calls.append(url)
        if "Treasury" in url:
            raise RuntimeError("HTTP 503")
        return RSS
    n = MS.fetch_news(date(2026, 10, 23), get)
    titles = [x["title"] for x in n["items"]]
    assert "Too new" not in titles and titles.count("Fed raises rates by a quarter point") == 1
    assert "Oil jumps 13% as Hormuz closes & LNG halts" not in titles     # before the window (120 days)
    assert n["errors"] == {"Treasury yields": "RuntimeError: HTTP 503"}
    assert len(calls) == len(MS.QUERIES) + 1 and "before%3A2026-10-24" in calls[0]


def test_moves_are_the_largest_steps_one_per_month():
    ch = {"cpi_yoy": {"dates": ["2026-01", "2026-02", "2026-03", "2026-04"], "values": [2.4, 2.5, 3.6, 3.3]},
          "spx": {"dates": [f"2026-03-{d:02d}" for d in range(1, 31)], "values": [100.0] * 10 + [90.0] * 20}}
    mv = MS.moves(ch)
    cpi = [m for m in mv if m["chart"] == "cpi_yoy"]
    assert cpi[0]["date"] == "2026-03" and cpi[0]["change"] == 1.1 and cpi[1]["date"] == "2026-04"
    spx = [m for m in mv if m["chart"] == "spx"]
    assert len(spx) == 1 and spx[0]["pct"] == -10.0 and spx[0]["window"] == "20d"   # one per month


def _sheet():
    news = {"items": [{"date": "2026-09-16", "title": "Fed raises rates by a quarter point", "publisher": "Reuters",
                       "url": "https://news.example/fed"}], "errors": {}}
    mv = [{"chart": "ffr", "date": "2026-09", "from": 3.5, "to": 3.75, "change": 0.25, "pct": 7.1, "window": "1m"}]
    fs = FactSheet()
    fs.add("macro.stance.zone", "hot", "code:macro", AS_OF)
    return MS.story_sheet(fs, AS_OF, news, mv, {"DC-01": {"choice": "hot", "why": "56%"}})


def test_verify_keeps_what_stands_on_its_facts():
    fs, by_id = _sheet()                         # F-001 zone, F-002 the move, F-003 the headline, F-004 A1's choice
    out = {"story": [{"sentences": [S("המקרו חם.", "F-001"), S("הריבית עלתה ל-3.75%.", "F-002"),
                                    S("הריבית עלתה ל-4.5%.", "F-002"), S("בלי מקור.")]},
                     {"sentences": [S("A1 בחר חם בגלל 56%.", "F-004")]}],
           "fed": {"headline": S("הפד העלה ריבית.", "F-003"), "paragraphs": [{"sentences": [S("ההעלאה ב-16.9.", "F-003")]}],
                   "next": S("עוד העלאה ב-2027.", "F-003")},
           "events": [{"date": "2026-09", "title": "הפד מעלה", "charts": ["ffr", "bogus"], "sentences": [S("הפד העלה.", "F-003")]},
                      {"date": "2026-05", "title": "תאריך זר", "charts": [], "sentences": [S("הפד העלה.", "F-003")]},
                      {"date": "2026-09", "title": "עלייה של 25", "charts": [], "sentences": [S("הפד העלה.", "F-003")]}]}
    n = MS.verify(out, fs, by_id)
    assert n["story"] == ["המקרו חם. הריבית עלתה ל-3.75%."]             # an agent fact holds no number
    assert n["fed"]["headline"] == "הפד העלה ריבית." and n["fed"]["text"] == ["ההעלאה ב-16.9."] and n["fed"]["next"] == ""
    assert n["fed"]["sources"] == [{"title": "Reuters: Fed raises rates by a quarter point", "url": "https://news.example/fed"}]
    assert [(e["date"], e["charts"]) for e in n["events"]] == [("2026-09", ["ffr"])]
    why = {d["why"] for d in n["dropped"]}
    assert {"the number 4.5% is not in the cited facts", "no fact cited", "the date is not a date of the cited facts",
            "a number in the title"} <= why
    assert not MS.complete(n) and MS.complete({**n, "story": ["a", "b"]})


def test_a_run_without_the_story_is_not_finished_and_the_resume_asks_again(db):
    st = State(db, AS_OF)
    bad = CardClient({"DC-27": _answer("DC-27", "story", story=[], events=[],
                                       fed={"headline": {"text": "", "fact_ids": [], "quotes": []}, "paragraphs": [],
                                            "next": {"text": "", "fact_ids": [], "quotes": []}})})
    J = JG.Judge(AgentRunner(db, bad), state=st)({"as_of": AS_OF, "macro": FIX["macro"], "timing": _timing(1)}, _deep_dive([]))
    assert J["incomplete"][0] == "A1:narrative" and "story" not in J["A1"]
    good = CardClient()
    J = JG.Judge(AgentRunner(db, good), state=st)({"as_of": AS_OF, "macro": FIX["macro"], "timing": _timing(1)}, _deep_dive([]))
    assert good.cards == ["DC-27"] and "incomplete" not in J                # A1's cards and the idea came from the state
    assert J["A1"]["story"] and J["A1"]["fed"]["headline"] and J["A1"]["events"][0]["date"] == "2026-10"
    d = {**FIX, "judgments": {**FIX["judgments"], "A1": J["A1"], "carried": {}}}
    html = report._macro(d)
    assert "מה קורה, במילים פשוטות" in html and "הפד מקל." in html and "השוק בשיא" in html and "מהמחקר של" not in html
