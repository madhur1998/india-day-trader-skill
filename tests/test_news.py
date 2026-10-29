import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "_nse")  # real feeds recorded 2026-10-29 around 13:30 IST
NOW = "2026-10-29 13:40"         # India time
UTC = datetime.timezone.utc


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


NAMES = news.parse_equity_list(raw("EQUITY_L.csv"))


def names(*syms):
    return {s: NAMES[s] for s in syms}


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("rss-INFY.xml"), "INFY")
        self.assertEqual(len(items), 15)
        first = items[0]
        self.assertTrue(first["title"].startswith("Canada's UNI Financial Cooperation Advances"))
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 28, 12, 14, tzinfo=UTC))
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertTrue(first["id"].startswith("rss:"))
        self.assertEqual(news.parse_rss(raw("rss-RELIANCE.xml"), "RELIANCE"), [])  # Yahoo sent none

    def test_nse_announcements(self):
        items = news.parse_announcements(raw("rss-Online_announcements.xml"),
                                         names("RELIANCE", "KPIL", "BIKAJI", "FDC"))
        by = {i["ticker"]: i for i in items}
        self.assertEqual(sorted(by), ["BIKAJI", "FDC", "KPIL", "RELIANCE"])
        ril = by["RELIANCE"]
        self.assertEqual(ril["title"], "Reliance Industries Limited: Other Restructuring")
        self.assertIn("Voluntary strike off of Hamleys", ril["summary"])
        self.assertTrue(ril["link"].endswith(".pdf"))
        self.assertEqual(ril["source"], "NSE")
        # pubDate "29-Oct-2026 08:55:40" is India time: 03:25:40 UTC
        self.assertEqual(ril["time"], datetime.datetime(2026, 10, 29, 3, 25, 40, tzinfo=UTC))
        self.assertEqual((by["KPIL"]["filing_tag"], by["KPIL"]["important"]), ("legal", True))
        self.assertEqual(by["BIKAJI"]["filing_tag"], "credit rating")
        self.assertEqual(by["FDC"]["filing_tag"], "buyback")

    def test_every_other_company_is_dropped(self):
        items = news.parse_announcements(raw("rss-Online_announcements.xml"), names("HDFCBANK", "M&M"))
        self.assertEqual(items, [])  # HDFC and Kotak Mahindra mutual fund NAVs are not the bank or M&M

    def test_subject_tags(self):
        cases = {
            "Financial Results Updates": ("results", True),
            "Outcome of Board Meeting-XBRL": ("board outcome", True),
            "Board Meeting Intimation": ("board meeting", False),
            "Acquisition": ("deal", True),
            "Disclosure under Regulation 29(1) of SAST": ("SAST", True),
            "Creation of pledge": ("pledge", True),
            "Credit Rating": ("credit rating", True),
            "Dividend": ("corporate action", False),
            "Record Date": ("corporate action", False),
            "Analysts/Institutional Investor Meet/Con. Call Updates": ("analyst meet", False),
            "Resignation of Director/KMP/SMP": ("people", True),
            "Change in Directors/KMP/SMP/Auditor/RTA": ("people", False),
            "Press Release": ("press release", False),
            "Copy of Newspaper Publication": ("routine", False),
            "Trading Window-XBRL": ("routine", False),
            "Bagging/Receiving of orders/contracts": ("orders", False),
            "Action(s) taken or orders passed": ("legal", True),
            "General Updates": ("update", False),
        }
        for subject, want in cases.items():
            self.assertEqual(news.subject_tag(subject), want, subject)

    def test_times(self):
        self.assertEqual(news.parse_time("Tue, 27 Oct 2026 11:28:00 +0000").hour, 11)
        self.assertEqual(news.parse_time("29-Oct-2026 13:26:05"),
                         datetime.datetime(2026, 10, 29, 7, 56, 5, tzinfo=UTC))
        self.assertEqual(news.parse_time("2026-10-29T13:26:05+05:30").hour, 7)
        self.assertIsNone(news.parse_time("soon"))

    def test_india_time(self):
        utc = datetime.datetime(2026, 10, 29, 3, 45, tzinfo=UTC)
        self.assertEqual(news.ist_text(utc), "2026-10-29 09:15 IST")
        self.assertEqual(news.parse_now("2026-10-29 09:15"), datetime.datetime(2026, 10, 29, 3, 45))
        with self.assertRaises(news.NewsError):
            news.parse_now("tomorrow")

    def test_symbols_and_offline_names(self):
        self.assertEqual(news.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertEqual(news.nse_symbol("infy.ns"), "INFY")
        self.assertEqual(news.file_key("M&M"), "MM")
        self.assertEqual(news.file_key("BAJAJ-AUTO"), "BAJAJAUTO")


class Names(unittest.TestCase):
    def test_equity_list_with_spaced_header(self):
        self.assertGreater(len(NAMES), 2500)
        self.assertEqual(NAMES["M&M"], "Mahindra & Mahindra Limited")
        self.assertEqual(NAMES["BAJAJ-AUTO"], "Bajaj Auto Limited")

    def test_tricky_names(self):
        m = news.match_company
        self.assertEqual(m("Mahindra & Mahindra Ltd.", NAMES), "M&M")  # not M&MFIN
        self.assertEqual(m("Star Health And Allied Insurance Company Limited", NAMES), "STARHEALTH")
        self.assertEqual(m("Matrimony.Com Limited", NAMES), "MATRIMONY")
        self.assertEqual(m("Colgate Palmolive (India) Limited", NAMES), "COLPAL")
        self.assertEqual(m("State Bank of India", NAMES), "SBIN")
        self.assertEqual(m("Reliance Chemotex Industries Limited", NAMES), "RELCHEMQ")
        # NSE's board-meetings feed cuts names at 40 characters
        self.assertEqual(m("Southern Petrochemicals Industries Corpo", NAMES), "SPIC")
        # a short name is never matched by its start
        self.assertIsNone(m("Reliance", NAMES))
        self.assertIsNone(m("HDFC Mutual Fund - HDFC NIFTY 100 ETF", NAMES))
        self.assertIsNone(m("", NAMES))

    def test_names_are_cached_in_the_state(self):
        now = datetime.datetime(2026, 10, 29, 8, 10, tzinfo=UTC)
        state, errors = {}, []
        got = news.company_names(news.Fetcher(offline=DIR), {"INFY": None, "ZZZZ": None},
                                 state, now, errors)
        self.assertEqual(got, {"INFY": "Infosys Limited"})
        self.assertEqual(state["names_unknown"], ["ZZZZ"])
        self.assertIn("ZZZZ has no company name", errors[0])
        # a day later nothing new to look up: no fetch (an empty folder would fail)
        with tempfile.TemporaryDirectory() as empty:
            errors = []
            got = news.company_names(news.Fetcher(offline=empty), {"INFY": None, "ZZZZ": None},
                                     state, now + datetime.timedelta(days=1), errors)
        self.assertEqual(got, {"INFY": "Infosys Limited"})
        self.assertFalse(any("equity list:" in e for e in errors))


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_rss(raw("rss-SBIN.xml"), "SBIN")
        watch = {"SBIN": "State Bank of India Limited", "INFY": "Infosys Limited"}
        news.tag(items, watch)
        by = {i["title"][:30]: i for i in items}
        leak = by["State Bank of India reports da"]
        self.assertEqual(leak["touches"], ["SBIN"])
        self.assertIn("risk", leak["tags"])  # dark web exposure
        profit = by["India's largest lender SBI bea"]
        self.assertIn("earnings", profit["tags"])

    def test_a_headline_naming_another_watched_company_touches_it(self):
        items = news.parse_rss(raw("rss-INFY.xml"), "INFY")
        news.tag(items, {"INFY": None, "SBIN": "State Bank of India"})
        self.assertTrue(all(i["touches"] == ["INFY"] for i in items))
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "INFY",
                  "title": "Infosys wins deal from State Bank of India; NSE: M&M up 2%",
                  "summary": "", "link": "", "time": None}]
        news.tag(items, {"INFY": None, "SBIN": "State Bank of India", "M&M": None})
        self.assertEqual(items[0]["touches"], ["INFY", "M&M", "SBIN"])

    def test_filings_use_the_subject_tag(self):
        items = news.parse_announcements(raw("rss-Online_announcements.xml"), names("EMBDL"))
        news.tag(items, {"EMBDL": None})
        self.assertEqual([i["tags"] for i in items], [["deal"], ["update"]])

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("rss-INFY.xml"), "INFY")
        b = news.parse_rss(raw("rss-INFY.xml"), "TCS")
        items = news.dedupe(news.tag(a + b, {"INFY": None, "TCS": None}))
        self.assertEqual(len(items), 15)
        self.assertEqual(items[0]["touches"], ["INFY", "TCS"])

    def test_indian_market_tags(self):
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "SBIN",
                  "title": "SBIN hits upper circuit after SEBI nod; Nifty, rupee firm ahead of RBI repo rate call",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"SBIN": None})[0]["tags"]
        for t in ("halt", "regulatory", "macro"):
            self.assertIn(t, tags)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--watchlist", os.path.join(self.tmp.name, "none.json"),
                     "--plan", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "RELIANCE", "INFY", "--hours", "96", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 6)  # 1 NSE filing + 5 Infosys headlines since Oct 25 13:40
        self.assertEqual(data["items"][0]["source"], "NSE")
        self.assertEqual(data["items"][0]["time_ist"], "2026-10-29 08:55")
        self.assertEqual(data["checked_ist"], "2026-10-29 13:40")
        self.assertTrue(any("RELIANCE.NS is empty" in n for n in data["notes"]))
        with open(self.state) as fh:
            state = json.load(fh)
        self.assertEqual(state["names"]["RELIANCE"], "Reliance Industries Limited")
        code, out, _ = run(["--tickers", "RELIANCE", "INFY", "--hours", "96"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        code, out, _ = run(["--tickers", "RELIANCE", "INFY", "--hours", "96", "--all",
                            "--dry-run"] + self.base)
        self.assertEqual(code, 1)  # shown again, but nothing new
        self.assertIn("Other Restructuring", out)

    def test_the_text_report(self):
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "24", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-10-29 08:55 IST  RELIANCE    NSE           "
                      "Reliance Industries Limited: Other Restructuring", out)
        self.assertIn("tags: deal  Disclosure under Regulation 30", out)
        self.assertIn("RELIANCE   Rs 1,174.30 +1.41% vs previous close Rs 1,158.00 at 13:35 IST", out)
        self.assertIn("Checked 2026-10-29 13:40 IST", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_routine_filings_are_counted_not_shown(self):
        code, out, err = run(["--tickers", "ANANTRAJ", "STARHEALTH", "--hours", "24", "--dry-run",
                              "--no-moves"] + self.base)
        self.assertEqual(code, 1, err)
        self.assertIn("Nothing new.", out)
        self.assertIn("2 routine NSE filings hidden", out)
        code, out, err = run(["--tickers", "ANANTRAJ", "STARHEALTH", "--hours", "24", "--dry-run",
                              "--no-moves", "--routine"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("Anant Raj Limited: Copy of Newspaper Publication", out)
        self.assertIn("STARHEALTH", out)

    def test_pinned_clock_hides_later_items(self):
        code, out, err = run(["--tickers", "INFY", "--hours", "200", "--json", "--dry-run",
                              "--no-moves", "--now", "2026-10-23 18:20"] + self.base[:4])
        self.assertEqual(code, 0, err)
        items = json.loads(out)["items"]
        self.assertEqual(items[0]["time_ist"], "2026-10-23 18:00")  # 18:37 is after 18:20 + 10 min
        self.assertEqual(len(items), 4)

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "SBIN", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "M&M.NS"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["M&M", "SBIN"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "200"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"SBIN"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)

    def test_headlines_only(self):
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "24", "--dry-run", "--no-nse",
                              "--no-moves"] + self.base)
        self.assertEqual(code, 1, err)
        self.assertNotIn("Other Restructuring", out)

    def test_moves(self):
        data = news.Fetcher(offline=DIR).get("", "chart-RELIANCE-5d-1m.json.gz")  # gunzipped
        m = news.parse_chart(data, "RELIANCE")
        # a 5-day chart: previousClose (Oct 28), not chartPreviousClose (Oct 23)
        self.assertEqual(m["prev_close"], 1158.0)
        self.assertEqual(m["pct"], 1.41)
        self.assertEqual(news.ist_text(m["time"], "%H:%M"), "13:35")
        one_day = json.dumps({"chart": {"result": [{"meta": {
            "regularMarketPrice": 2900.0, "chartPreviousClose": 2800.0, "range": "1d"}}]}})
        self.assertEqual(news.parse_chart(one_day, "M&M")["pct"], 3.57)
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # no chart file offline: no move line, no error note
        code, out, _ = run(["--tickers", "STARHEALTH", "--hours", "24", "--dry-run"] + self.base)
        self.assertNotIn("vs previous close", out)
        self.assertNotIn("price for", out)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = datetime.datetime(2026, 10, 29, 8, 10, tzinfo=UTC)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
