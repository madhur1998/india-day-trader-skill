import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

facts = load("trader-analyst", "facts")
NSE = os.path.join(FIX, "_nse")              # shared recordings, 2026-10-29 IST
DIR = os.path.join(FIX, "trader-analyst")    # two more Yahoo timeseries, same day
OFF = ["--offline", NSE, "--offline", DIR, "--today", "2026-10-29"]


def series(sym):
    for folder in (NSE, DIR):
        path = os.path.join(folder, "timeseries-%s.json" % sym)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return facts.parse_timeseries(fh.read())
    raise AssertionError(sym)


def feed(name):
    with open(os.path.join(NSE, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


class Reliance(unittest.TestCase):
    def setUp(self):
        self.r = facts.analyse(series("RELIANCE"), quarters=8)

    def test_latest_quarter(self):
        q = self.r["rows"][0]
        self.assertEqual(q["end"], "2026-06-30")
        self.assertEqual(q["fiscal"], "Q1 FY27 (Apr-Jun 2026)")
        self.assertEqual(q["revenue"], 3094680000000)
        self.assertEqual(q["net_income"], 209460000000)
        self.assertEqual(q["eps_diluted"], 15.48)
        self.assertEqual(q["revenue_yoy"], 27.0)    # vs 2,43,632 crore in Apr-Jun 2025
        self.assertEqual(q["eps_yoy"], -22.4)       # vs 19.95
        self.assertEqual(q["gross_margin"], 26.8)
        self.assertEqual(q["operating_margin"], 10.5)
        self.assertEqual(q["net_margin"], 6.8)
        self.assertEqual(self.r["currency"], "INR")

    def test_a_missing_quarter_is_shown_not_filled(self):
        self.assertEqual(self.r["missing"], ["2025-09-30"])
        labels = [(r["fiscal"].split(" (")[0], r["missing"]) for r in self.r["rows"]]
        self.assertEqual(labels, [("Q1 FY27", False), ("Q4 FY26", False), ("Q3 FY26", False),
                                  ("Q2 FY26", True), ("Q1 FY26", False), ("Q4 FY25", False)])
        gap = self.r["rows"][3]
        self.assertIsNone(gap["revenue"])
        # no TTM: Jul-Sep 2025 is one of the last four quarters
        self.assertEqual(self.r["ttm"], {})
        self.assertEqual(self.r["ttm_missing"], ["2025-09-30"])
        # and the year-ago quarter of Q3 FY26 (Oct-Dec 2024) is outside Yahoo's data: no YoY
        self.assertIsNone(self.r["rows"][2]["revenue_yoy"])

    def test_fiscal_years(self):
        fy = self.r["years"][0]
        self.assertEqual(fy["fiscal"], "FY26 (Apr 2025-Mar 2026)")
        self.assertEqual(fy["revenue_yoy"], 9.6)
        self.assertEqual(fy["eps_diluted"], 59.69)

    def test_what_changed_in_words(self):
        lines = facts.changes(self.r)
        self.assertIn("Q1 FY27 (Apr-Jun 2026): revenue Rs 3,09,468 cr (+27.0% vs a year earlier)",
                      lines[0])
        self.assertIn("diluted EPS Rs 15.48 (-22.4%)", lines[0])
        self.assertTrue(any(l.startswith("Gross margin 26.8% (+0.2 points") for l in lines))


class Quarters(unittest.TestCase):
    def test_fiscal_labels(self):
        self.assertEqual(facts.fiscal_label("2026-06-30"), "Q1 FY27 (Apr-Jun 2026)")
        self.assertEqual(facts.fiscal_label("2026-09-30"), "Q2 FY27 (Jul-Sep 2026)")
        self.assertEqual(facts.fiscal_label("2026-12-31"), "Q3 FY27 (Oct-Dec 2026)")
        self.assertEqual(facts.fiscal_label("2027-03-31"), "Q4 FY27 (Jan-Mar 2027)")
        self.assertEqual(facts.fiscal_label("2026-03-31", annual=True), "FY26 (Apr 2025-Mar 2026)")
        self.assertEqual(facts.fiscal_label("2026-08-31"), "quarter to 2026-08-31")
        self.assertEqual(facts.short_label("2026-09-30"), "Q2 FY27")

    def test_month_end_shifts(self):
        self.assertEqual(facts.shift("2026-06-30", -12), "2025-06-30")
        self.assertEqual(facts.shift("2026-03-31", -3), "2025-12-31")
        self.assertEqual(facts.shift("2024-05-31", -3), "2024-02-29")

    def test_trailing_twelve_months_with_four_in_a_row(self):
        r = facts.analyse(series("MATRIMONY"))
        self.assertEqual(r["missing"], [])
        # Jul-Sep 2025 + Oct-Dec 2025 + Jan-Mar 2026 + Apr-Jun 2026
        self.assertEqual(r["ttm"]["revenue"], 1145900000 + 1132400000 + 1168300000 + 1305100000)
        self.assertEqual(r["ttm"]["eps_diluted"], 21.27)
        self.assertEqual(r["rows"][0]["revenue_yoy"], 13.2)

    def test_growth_from_a_loss_is_not_a_percentage(self):
        r = facts.analyse(series("ESAFSFB"))
        q = r["rows"][0]
        self.assertEqual(q["net_income"], 800800000)
        self.assertIsNone(q["net_income_yoy"])      # a loss of 81.22 crore a year earlier
        self.assertIn("(a loss of Rs 81.22 cr a year earlier)", facts.changes(r)[0])
        self.assertIsNone(facts.pct(5, 0))
        self.assertEqual(facts.pct(-5, 10), -150.0)

    def test_banks_have_no_gross_or_operating_margin(self):
        r = facts.analyse(series("SBIN"))
        self.assertTrue(r["no_gross_or_operating"])
        self.assertIsNone(r["rows"][0]["gross_margin"])
        self.assertEqual(r["rows"][0]["net_margin"], 24.7)

    def test_a_company_yahoo_reports_in_dollars(self):
        r = facts.analyse(series("INFY"))
        self.assertEqual(r["currency"], "USD")
        self.assertEqual(r["rows"][0]["fiscal"], "Q2 FY27 (Jul-Sep 2026)")
        self.assertEqual(r["ttm"]["revenue"], 20366000000)
        self.assertIn("revenue USD 5,145M", facts.changes(r)[0])

    def test_yahoo_lagging_the_results_calendar(self):
        today = datetime.date(2026, 10, 29)
        self.assertEqual(facts.expected_quarter(today), "2026-06-30")   # Sep results due Nov 14
        self.assertEqual(facts.expected_quarter(datetime.date(2026, 5, 30)), "2025-12-31")  # Mar results due May 30
        self.assertIsNone(facts.lag_note(facts.analyse(series("RELIANCE")), today))
        note = facts.lag_note(facts.analyse(series("HDFCBANK")), today)
        self.assertIn("newest quarter is Q1 FY26 (Apr-Jun 2025)", note)

    def test_quarters_need_revenue_or_net_income(self):
        with self.assertRaises(facts.FactsError):
            facts.analyse({})
        with self.assertRaises(facts.FactsError):
            facts.parse_timeseries(b'{"timeseries": {"result": null, "error": {"description": "bad"}}}')


class Crore(unittest.TestCase):
    def test_crore_formatting(self):
        self.assertEqual(facts.crore(3094680000000), "Rs 3,09,468 cr")
        self.assertEqual(facts.crore(1057219000000), "Rs 1,05,722 cr")
        self.assertEqual(facts.crore(190800000), "Rs 19.08 cr")
        self.assertEqual(facts.crore(-1158100000), "-Rs 116 cr")
        self.assertEqual(facts.crore(None), "-")
        self.assertEqual(facts.money(5145000000, "USD"), "USD 5,145M")
        self.assertEqual(facts.inr(1234567.5), "Rs 12,34,567.50")


class ResultsDates(unittest.TestCase):
    def test_board_meetings_feed(self):
        m = facts.board_meetings(feed("rss-Board_Meetings.xml"), "Matrimony.Com Limited")
        self.assertEqual([(x["date"], x["results"]) for x in m], [("2026-11-10", True)])
        self.assertEqual(m[0]["intimated"], "2026-10-29 13:03 IST")
        self.assertTrue(m[0]["link"].endswith(".pdf"))

    def test_a_name_cut_at_40_characters(self):
        m = facts.board_meetings(feed("rss-Board_Meetings.xml"),
                                 "Southern Petrochemicals Industries Corporation  Limited")
        self.assertEqual(m[0]["date"], "2026-11-04")
        self.assertTrue(m[0]["results"])
        self.assertFalse(facts.same_company("Reliance", "Reliance Industries Limited"))
        self.assertEqual(facts.board_meetings(feed("rss-Board_Meetings.xml"),
                                              "Reliance Industries Limited"), [])  # Reliance Chemotex is not it

    def test_this_week(self):
        m = facts.board_meetings(feed("rss-Board_Meetings.xml"), "Sundaram Finance Limited")
        nxt = facts.next_results(m, datetime.date(2026, 11, 2))   # Monday
        self.assertEqual((nxt["date"], nxt["days_away"], nxt["this_week"], nxt["weekday"]),
                         ("2026-11-04", 2, True, "Wed"))
        nxt = facts.next_results(m, datetime.date(2026, 10, 29))  # Thursday the week before
        self.assertFalse(nxt["this_week"])
        self.assertIsNone(facts.next_results(m, datetime.date(2026, 11, 5)))

    def test_results_filing_on_nse(self):
        f = facts.results_filings(feed("rss-Online_announcements.xml"), "ESAF Small Finance Bank Limited")
        self.assertEqual([x["subject"] for x in f],
                         ["Financial Results Updates", "Outcome of Board Meeting", "Outcome of Board Meeting"])
        self.assertEqual(f[0]["filed"], "2026-10-29 13:18 IST")
        note = facts.stale_note(facts.analyse(series("ESAFSFB")), f)
        self.assertIn("Yahoo's numbers stop at Q1 FY27 (Apr-Jun 2026)", note)
        self.assertIn(f[0]["link"], note)


class Cli(unittest.TestCase):
    def test_report(self):
        code, out, err = run(["MATRIMONY", "ESAFSFB"] + OFF)
        self.assertEqual(code, 0, err)
        self.assertIn("Matrimony.Com Limited (MATRIMONY). Rs crore except EPS.", out)
        self.assertIn("source of truth", out)
        self.assertIn("Next results date: Tue 2026-11-10, financial results (in 12 days)", out)
        self.assertIn("Last 4 quarters: revenue Rs 475 cr, net income Rs 44.85 cr, diluted EPS Rs 21.27.", out)
        self.assertIn("NSE filing 2026-10-29 13:18 IST: Financial Results Updates", out)
        self.assertIn("NOTE: Yahoo's numbers stop at Q1 FY27", out)
        self.assertNotIn(chr(0x2014), out)

    def test_missing_quarter_and_no_meeting(self):
        code, out, err = run(["RELIANCE"] + OFF)
        self.assertEqual(code, 0, err)
        self.assertIn("Q2 FY26 (Jul-Sep 2025)  missing from Yahoo's data", out)
        self.assertIn("No trailing twelve months: Q2 FY26 (Jul-Sep 2025) not in Yahoo's data", out)
        self.assertIn("not in NSE's board-meetings feed", out)

    def test_metrics_file_for_the_screener(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader", "metrics.json")
            code, out, err = run(["MATRIMONY", "M&M", "--metrics", path] + OFF)
            self.assertEqual(code, 0, err)
            with open(path) as fh:
                data = json.load(fh)
        rows = {r["symbol"]: r for r in data["companies"]}
        self.assertEqual(sorted(rows), ["M&M", "MATRIMONY"])
        self.assertEqual(rows["MATRIMONY"]["eps_ttm"], 21.27)
        self.assertEqual(rows["MATRIMONY"]["fiscal"], "Q1 FY27")
        self.assertIsNone(rows["M&M"]["revenue_ttm"])
        self.assertEqual(rows["M&M"]["missing_quarters"], ["2025-09-30"])
        self.assertEqual(data["as_of"], "2026-10-29")
        self.assertIn("wrote", out)

    def test_unknown_symbol(self):
        code, _, err = run(["ZZZZ"] + OFF)
        self.assertEqual(code, 2)
        self.assertIn("ZZZZ is not in NSE's equity list", err)
        self.assertIn("no file timeseries-ZZZZ.json", err)

    def test_no_numbers_still_gives_the_date(self):
        code, out, err = run(["SPIC"] + OFF)
        self.assertEqual(code, 2)
        self.assertIn("SPIC: next board meeting Wed 2026-11-04 (results)", out)

    def test_json_output(self):
        code, out, err = run(["SBIN", "--json", "--quarters", "4", "--no-nse"] + OFF)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data[0]["result"]["rows"]), 4)
        self.assertIsNone(data[0]["nse"])
        self.assertEqual(data[0]["result"]["rows"][0]["fiscal"], "Q1 FY27 (Apr-Jun 2026)")


if __name__ == "__main__":
    unittest.main()
