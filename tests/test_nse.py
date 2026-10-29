import datetime
import inspect
import os
import re
import unittest

import _nse as nse
from _load import ROOT, load

# script per skill; trader-analyst and trader-insider keep no market clock
TOOLS = {
    "trader-scanner": "scan", "trader-news": "news", "trader-entry": "entry",
    "trader-exit": "exit", "trader-timemachine": "timemachine", "trader-risk": "check",
    "trader-budget": "size", "trader-broker": "ticket", "trader-alerts": "alerts",
}
# copied verbatim from tests/_nse.py wherever a tool defines them
SHARED_DATA = ("UA", "NSE_HOLIDAYS", "CALENDAR_YEARS", "NSE_MUHURAT", "PRE_OPEN", "OPEN", "CLOSE",
               "IST", "TICK_BANDS", "CHARGES")
SHARED_FUNCS = ("to_ist", "ist_to_utc", "ts_to_ist", "_hm", "trading_day", "session",
                "tick_size", "round_tick", "charges", "inr", "yahoo_symbol", "nse_symbol")
US_LEFTOVERS = ("NYSE", "eastern_offset", "to_eastern", "New York", "Alpaca", "ALPACA",
                "America/New_York", "pdt_guard", "_usd\"")


class Reference(unittest.TestCase):
    def test_session_is_9_15_to_15_30_india_time(self):
        self.assertEqual(nse.session(datetime.date(2026, 10, 29)),
                         (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertEqual(nse.ts_to_ist(0), datetime.datetime(1970, 1, 1, 5, 30))
        self.assertEqual(nse.parse_now("2026-10-29 09:15"), datetime.datetime(2026, 10, 29, 3, 45))
        self.assertEqual(nse.parse_now("2026-10-29T03:45Z"), datetime.datetime(2026, 10, 29, 3, 45))

    def test_weekends_holidays_and_muhurat(self):
        self.assertEqual(nse.trading_day(datetime.date(2026, 10, 31))[1], "weekend")
        self.assertIn("Diwali", nse.trading_day(datetime.date(2026, 11, 10))[1])
        ok, why = nse.trading_day(datetime.date(2026, 11, 8))
        self.assertFalse(ok)
        self.assertIn("Muhurat", why)
        for day in nse.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(day).weekday(), 5, day)

    def test_tick_size_bands_and_rounding(self):
        self.assertEqual([nse.tick_size(p) for p in (99, 250, 999.95, 1000, 7500, 15000, 30000)],
                         [0.01, 0.05, 0.05, 0.10, 0.50, 1.00, 5.00])
        self.assertEqual(nse.round_tick(1234.567, "down"), 1234.5)
        self.assertEqual(nse.round_tick(1234.567, "up"), 1234.6)
        self.assertEqual(nse.round_tick(512.33), 512.35)
        self.assertEqual(nse.round_tick(0.15, "down", 0.05), 0.15)
        # raw Yahoo floats sit a hair off the paisa: they must not lose or gain a tick
        self.assertEqual(nse.round_tick(1156.199951171875, "down"), 1156.2)
        self.assertEqual(nse.round_tick(1199.6000366, "up"), 1199.6)
        self.assertEqual(nse.round_tick(1187.0000001, "up"), 1187.0)
        self.assertEqual(nse.round_tick(1190.93, "down"), 1190.9)
        self.assertEqual(nse.round_tick(1216.96, "up"), 1217.0)

    def test_charges_match_the_zerodha_calculator(self):
        c = nse.charges(100, 1000, 1010)
        self.assertEqual(c["brokerage"], 40.0)
        self.assertEqual(c["stt"], 25)
        self.assertAlmostEqual(c["total"], 82.48, places=2)
        self.assertLess(nse.charges(1, 100, 100)["brokerage"], 0.1)  # 0.03 % under the Rs 20 cap

    def test_indian_digit_grouping_and_yahoo_symbols(self):
        self.assertEqual(nse.inr(1234567.5), "Rs 12,34,567.50")
        self.assertEqual(nse.inr(-950), "-Rs 950.00")
        self.assertEqual(nse.yahoo_symbol("m&m"), "M%26M.NS")
        self.assertEqual(nse.yahoo_symbol("RELIANCE.NS"), "RELIANCE.NS")
        self.assertEqual(nse.yahoo_symbol("^NSEI"), "%5ENSEI")


class Copies(unittest.TestCase):
    """Each tool's copy of the shared block matches tests/_nse.py."""

    def test_shared_data_matches_the_reference(self):
        for folder, name in TOOLS.items():
            mod = load(folder, name)
            for attr in SHARED_DATA:
                if hasattr(mod, attr):
                    self.assertEqual(getattr(mod, attr), getattr(nse, attr), "%s %s" % (folder, attr))

    def test_shared_functions_are_verbatim_copies(self):
        for folder, name in TOOLS.items():
            mod = load(folder, name)
            for fn in SHARED_FUNCS:
                if hasattr(mod, fn):
                    self.assertEqual(inspect.getsource(getattr(mod, fn)),
                                     inspect.getsource(getattr(nse, fn)), "%s %s()" % (folder, fn))

    def test_one_user_agent_without_a_url(self):
        self.assertNotIn("http", nse.UA)
        for folder, name in list(TOOLS.items()) + [("trader-analyst", "facts"), ("trader-insider", "insider")]:
            mod = load(folder, name)
            self.assertEqual(getattr(mod, "UA", nse.UA), nse.UA, folder)
            self.assertFalse(hasattr(mod, "NSE_UA"), "%s: use the one UA" % folder)

    def test_every_clocked_tool_carries_the_calendar(self):
        for folder, name in TOOLS.items():
            mod = load(folder, name)
            self.assertTrue(hasattr(mod, "to_ist"), folder)
            self.assertFalse(hasattr(mod, "eastern_offset"), folder)

    def test_no_us_market_leftovers_in_the_tools(self):
        for folder in list(TOOLS) + ["trader-analyst", "trader-insider"]:
            for f in os.listdir(os.path.join(ROOT, folder)):
                if not f.endswith((".py", ".md", ".json")):
                    continue
                with open(os.path.join(ROOT, folder, f), encoding="utf-8") as fh:
                    text = fh.read()
                for word in US_LEFTOVERS:
                    self.assertNotIn(word, text, "%s/%s" % (folder, f))
                self.assertNotRegex(text, r"\$\d", "%s/%s: prices are in rupees" % (folder, f))


if __name__ == "__main__":
    unittest.main()
