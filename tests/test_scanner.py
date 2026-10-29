import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

scan = load("trader-scanner", "scan")

SDIR = os.path.join(FIX, "trader-scanner")
NSE = os.path.join(FIX, "_nse")                                 # shared real NSE + Yahoo files of Oct 29 2026
DAILY = os.path.join(SDIR, "spark-3mo-1d-2026-10-29.json")      # real spark: 19 Nifty names + a made-up one
SPARK5 = os.path.join(SDIR, "spark-5d-5m-2026-10-29.json.gz")   # real spark, 5 days of 5-min closes, 1:41 PM IST
V8 = {s: os.path.join(NSE, "chart-%s-60d-5m.json.gz" % f)     # real v8 charts with highs and lows
      for s, f in (("RELIANCE", "RELIANCE"), ("HDFCBANK", "HDFCBANK"), ("INFY", "INFY"), ("SBIN", "SBIN"),
                   ("M&M", "MM"))}
DT = datetime.datetime
D = datetime.date
NIFTY19 = ["AXISBANK", "BAJAJ-AUTO", "BHARTIARTL", "ETERNAL", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "KOTAKBANK",
           "LT", "M&M", "MARUTI", "NTPC", "RELIANCE", "SBIN", "SUNPHARMA", "TATASTEEL", "TCS", "TITAN"]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def ts(ist_text):
    """India wall-clock text -> unix seconds."""
    utc = scan.ist_to_utc(DT.strptime(ist_text, "%Y-%m-%d %H:%M:%S" if ist_text.count(":") == 2
                                      else "%Y-%m-%d %H:%M"))
    return int((utc - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, lows=None, start="09:15"):
    """A v8-like series of 5-min bars from `start` on `day` (YYYY-MM-DD)."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    return {"t": t, "c": list(closes), "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
            "l": list(lows or [c - 0.1 if c is not None else None for c in closes]), "o": None,
            "v": [1000] * len(closes), "meta": {}}


def load_v8():
    data = {}
    for path in V8.values():
        d, _ = scan.load_offline(path)
        data.update(d)
    return data


class Universe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eq, cls.sec = text(os.path.join(NSE, "EQUITY_L.csv")), text(os.path.join(NSE, "sec_list.csv"))
        cls.rows = {r["SYMBOL"]: r for r in scan.parse_equity_list(cls.eq)}
        cls.bands = scan.parse_bands(cls.sec)

    def test_eq_only_and_the_5_percent_band_out(self):
        kept, counts, missing = scan.build_universe(self.eq, self.sec)
        self.assertEqual(len(kept), 2286)
        self.assertEqual(counts, {"rows": 2620, "keep": 2286, "band 5 %": 82,
                                  "series BE (trade-to-trade, no intraday)": 219,
                                  "series BZ (trade-to-trade, no intraday)": 33})
        self.assertEqual(missing, [])
        k = {s: (n, b) for s, n, b in kept}
        self.assertEqual(k["RELIANCE"], ("Reliance Industries Limited", "No Band"))
        self.assertEqual(k["M&M"], ("Mahindra & Mahindra Limited", "No Band"))
        self.assertEqual(k["BAJAJ-AUTO"][1], "No Band")
        self.assertEqual(k["20MICRONS"][1], "20")
        self.assertEqual(k["ABDL"][1], "10")
        self.assertEqual(k["ARHAM"][1], "?")                   # not in sec_list.csv: kept, band unknown

    def test_one_case_per_rule(self):
        cases = {
            "AARTISURF": "series BE (trade-to-trade, no intraday)",   # BE: delivery only
            "AGSTRA": "series BZ (trade-to-trade, no intraday)",      # BZ
            "21STCENMGM": "band 5 %", "A2ZINFRA": "band 5 %",          # EQ, 5 % circuit
            "SBIN": "keep", "360ONE": "keep",                          # No Band (F&O)
            "20MICRONS": "keep", "ABDL": "keep",                       # 20 % and 10 %
        }
        for sym, want in cases.items():
            self.assertEqual(scan.classify(self.rows[sym], self.bands), want, sym)
        two = {"SYMBOL": "XYZ", "NAME OF COMPANY": "Xyz Ltd", "SERIES": "EQ"}
        self.assertEqual(scan.classify(two, {"XYZ": "2"}), "band 2 %")
        self.assertEqual(scan.classify(two, {"XYZ": "2"}, ()), "keep")
        self.assertEqual(scan.classify(dict(two, SERIES="SM"), {}), "series SM")

    def test_band_flag(self):
        self.assertEqual(scan.parse_bands_flag("2,5"), ("2", "5"))
        self.assertEqual(scan.parse_bands_flag("5%, 10"), ("5", "10"))
        self.assertEqual(scan.parse_bands_flag("none"), ())
        with self.assertRaises(scan.ScanError):
            scan.parse_bands_flag("7")
        self.assertEqual(len(scan.build_universe(self.eq, self.sec, skip_bands=())[0]), 2368)
        self.assertEqual(len(scan.build_universe(self.eq, self.sec, skip_bands=("5", "10"))[0]), 2115)

    def test_index_presets(self):
        kept, counts, missing = scan.build_universe(self.eq, self.sec, text(os.path.join(NSE, "ind_nifty50list.csv")))
        self.assertEqual(len(kept), 50)
        self.assertEqual(set(b for _, _, b in kept), {"No Band"})
        self.assertIn("M&M", [s for s, _, _ in kept])
        kept, counts, missing = scan.build_universe(self.eq, self.sec,
                                                    text(os.path.join(NSE, "ind_nifty500list.csv")))
        self.assertEqual(len(kept), 493)
        self.assertEqual(counts["band 5 %"], 2)             # two Nifty 500 names sit in a 5 % band
        # in the index file, not EQ rows of EQUITY_L.csv: three REITs, two names moved to BE, a placeholder
        self.assertEqual(missing, ["BAGMANE", "BIRET", "DUMMYHEG", "EMBASSY", "HFCL", "STLTECH"])
        self.assertEqual(len(scan.parse_index(text(os.path.join(NSE, "ind_nifty500list.csv")))), 496)  # EQ rows

    def test_not_nse_files(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_equity_list("<html>Access Denied</html>")
        with self.assertRaises(scan.ScanError):
            scan.parse_bands(text(os.path.join(NSE, "EQUITY_L.csv")))

    def test_only_nsearchives_and_a_plain_user_agent(self):
        calls = []

        def fake(url, timeout=20, ua=scan.UA):
            calls.append((url, ua))
            return "﻿" + text(os.path.join(NSE, "ind_nifty50list.csv"))
        old = scan.http_get
        scan.http_get = fake
        try:
            syms = scan.parse_index(scan.nse_text("nifty50"))
        finally:
            scan.http_get = old
        self.assertEqual(len(syms), 50)
        self.assertEqual(calls, [("https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
                                  scan.UA)])
        self.assertNotIn("http", scan.UA)               # nsearchives stalls on a UA with a URL in it
        src = text(os.path.join(ROOT, "trader-scanner", "scan.py"))
        self.assertNotIn("www.nseindia.com/api", src)


class Time(unittest.TestCase):
    def test_ist_clock(self):
        self.assertEqual(scan.ts_to_ist(V8_FIRST), DT(2026, 8, 4, 9, 15))        # RELIANCE's first 5-min bar
        self.assertEqual(scan.parse_now("2026-10-28 10:15"), DT(2026, 10, 28, 4, 45))
        self.assertEqual(scan.parse_now("2026-10-28 10:15 IST"), DT(2026, 10, 28, 4, 45))
        self.assertEqual(scan.parse_now("2026-10-28T04:45:00Z"), DT(2026, 10, 28, 4, 45))
        self.assertEqual(scan.parse_now("2026-12-01 10:15"), DT(2026, 12, 1, 4, 45))   # no daylight saving
        self.assertEqual(scan.clock_label(DT(2026, 10, 28, 9, 45)), "9:45 AM")
        self.assertEqual(scan.clock_label(datetime.time(13, 5)), "1:05 PM")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")

    def test_75_bars_a_session(self):
        self.assertEqual(scan.slots_done(D(2026, 10, 28), DT(2026, 10, 28, 15, 30)), 75)
        self.assertEqual(scan.slots_done(D(2026, 10, 28), DT(2026, 10, 28, 9, 35)), 4)
        self.assertEqual(scan.slot(D(2026, 10, 28), 74), DT(2026, 10, 28, 15, 25))

    def test_rupees(self):
        self.assertEqual(scan.inr(1199.6), "Rs 1,199.60")
        self.assertEqual(scan.inr(10907662137), "Rs 10,90,76,62,137.00")
        self.assertEqual(scan.crore(1e8), "Rs 10 crore")
        self.assertEqual(scan.crore(0), "Rs 0.00")

    def test_calendar_warning_past_2026(self):
        self.assertIsNone(scan.calendar_warning(D(2026, 12, 31)))
        self.assertIn("2027", scan.calendar_warning(D(2027, 1, 4)))


V8_FIRST = ts("2026-08-04 09:15")


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        data = scan.parse_spark(text(DAILY))
        self.assertEqual(sorted(data), NIFTY19)        # 20 asked; Yahoo silently drops the made-up ZZZZQX
        self.assertIn("M&M", data)                     # M&M.NS comes back as ours
        r = data["RELIANCE"]
        self.assertIsNone(r["h"])                      # spark is closes only
        self.assertEqual(r["meta"]["regularMarketVolume"], 13361568)
        # read before the open on Oct 29: the last complete close is Wed Oct 28
        self.assertEqual(scan.daily_closes(r, scan.parse_now("2026-10-29 09:00"))[-1], (D(2026, 10, 28), 1158.0))
        # Thursday's bar is in progress until 3:30 PM IST
        self.assertEqual(scan.daily_closes(r, scan.parse_now("2026-10-29 15:29"))[-1][0], D(2026, 10, 28))
        self.assertEqual(scan.daily_closes(r, scan.parse_now("2026-10-29 15:30"))[-1], (D(2026, 10, 29), 1172.8))

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_urls(self):
        self.assertEqual(scan.spark_path(["RELIANCE", "m&m", "BAJAJ-AUTO"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=RELIANCE.NS,M%26M.NS,BAJAJ-AUTO.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("SBIN", "1d", "5m"), "/v8/finance/chart/SBIN.NS?range=1d&interval=5m")


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        self.body = text(DAILY)

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20, ua=scan.UA):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        self.assertEqual(scan.yahoo_get("/v7/finance/spark?symbols=SBIN.NS&range=3mo&interval=1d"), self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/SBIN.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        syms = NIFTY19 + ["ZZZZQX"] + ["X%02d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["ZZZZQX"], "no data from Yahoo")
        self.assertEqual(len(failed), 21)
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])  # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-10-29 09:00"), 50, 1e8)
        self.assertEqual((counts["requested"], counts["fetched"]), (40, 19))

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    # Hand-checked from the closes, through Wed Oct 28 2026 (read before Thursday's open).
    EXPECT = {
        "INFY": ["bull_flag"],                     # up 15.2 % over the 10 sessions, last 5 closes in a 3.8 % range
        "TCS": ["bull_flag"],                      # up 20.4 %, 3.0 % range
        "LT": ["breakout_20d", "reclaim_ma20"],    # 3801.30 > 3770.00; 3665.10 under 3682.53, then over 3684.28
        "M&M": ["reclaim_ma20"],                   # 2858.40 under 2864.97, then 2869.90 over 2858.71
    }

    def setUp(self):
        self.data, self.errors = scan.load_offline(DAILY)
        self.syms = sorted(self.data)

    def test_setups_before_the_open(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 09:00"), 50, 1e8)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 10, 28))
        self.assertEqual(counts, {"requested": 19, "fetched": 19, "illiquid": 0, "short": 0, "stale": 4,
                                  "scanned": 15, "hits": 4})
        self.assertEqual([h["symbol"] for h in hits], ["INFY", "TCS", "LT", "M&M"])   # most rupees traded first
        infy = hits[0]
        self.assertEqual((infy["price"], infy["rsi"], infy["close_date"], infy["volume"]),
                         (1123.15, 55.3, "2026-10-28", 9577366))

    def test_a_chart_with_no_bar_for_the_last_session_is_stale(self):
        # Yahoo's daily spark skipped Oct 28 for these four (MARUTI's last close is Oct 27)
        bars = scan.daily_closes(self.data["MARUTI"], scan.parse_now("2026-10-29 09:00"))
        self.assertEqual(bars[-1], (D(2026, 10, 27), 11450.0))
        hits, counts, _ = scan.scan_daily(self.data, ["MARUTI", "BAJAJ-AUTO", "KOTAKBANK", "TITAN", "INFY"],
                                          scan.parse_now("2026-10-29 09:00"), 50, 1e8)
        self.assertEqual((counts["stale"], counts["scanned"]), (4, 1))

    def test_the_in_progress_bar_is_dropped(self):
        before = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 09:00"), 50, 1e8)
        during = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 13:41"), 50, 1e8)
        self.assertEqual(before, during)

    def test_holiday_and_weekend_bars(self):
        s = {"t": [ts("2026-10-19 09:15"), ts("2026-10-21 09:15")], "c": [10.0, 11.0], "meta": {}}
        # Tue Oct 20 was Dussehra: no bar, and Wednesday's bar completes at 3:30 PM
        self.assertEqual(scan.daily_closes(s, scan.parse_now("2026-10-20 12:00")), [(D(2026, 10, 19), 10.0)])
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-10-21 15:29"))), 1)
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-10-21 15:30"))), 2)
        # on a Saturday the week's last bar is complete
        self.assertEqual(len(scan.daily_closes(s, scan.parse_now("2026-10-24 10:00"))), 2)

    def test_rupee_floors(self):
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 09:00"), 1000, 500 * 1e7)
        self.assertEqual([h["symbol"] for h in hits], ["INFY", "TCS", "LT"])
        self.assertEqual(counts["illiquid"], 13)        # under Rs 1,000 or under Rs 500 crore traded (M&M: 331)
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-10-29 09:00"), 50, 0,
                                          min_volume=10000000)
        self.assertEqual(counts["scanned"], 4)          # 5 over 1 crore shares; KOTAKBANK is stale

    def test_short_and_empty_charts(self):
        s = {"t": [ts("2026-10-%02d 09:15" % d) for d in range(5, 16)], "c": [100.0] * 11,
             "meta": {"regularMarketPrice": 100, "regularMarketVolume": 5000000}}
        hits, counts, last = scan.scan_daily({"X": s, "E": {"t": [], "c": [], "meta": {}}}, ["X", "E", "GONE"],
                                             scan.parse_now("2026-10-29 09:00"), 50, 1e8)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi_matches_the_reel_math(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_v8()
        cls.syms = sorted(cls.data)

    def scan_at(self, now, syms=None, min_price=50, or_minutes=15):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, or_minutes, datetime.time(14, 45))
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_wednesday_at_1015(self):
        hits, counts, failed = self.scan_at("2026-10-28 10:15")
        self.assertEqual(self.syms, ["HDFCBANK", "INFY", "M&M", "RELIANCE", "SBIN"])
        self.assertEqual((counts["scanned"], counts["unusable"], len(hits)), (5, 0, 2))
        # RELIANCE: range 9:15 to 9:30, high 1194.80 (the 9:15 bar), low 1187.00; the 9:40 bar closed 1199.60
        self.assertEqual(hits[1], {"symbol": "RELIANCE", "setups": ["orb"], "price": 1199.6, "at": "9:45 AM",
                                   "bar_start": "09:40", "bar_index": 5, "or_high": 1194.8, "or_low": 1187.0,
                                   "pct_over": 0.4})
        self.assertEqual((hits[0]["symbol"], hits[0]["at"], hits[0]["or_high"]), ("HDFCBANK", "9:45 AM", 676.8))

    def test_later_in_the_day(self):
        hits, _, _ = self.scan_at("2026-10-28 14:45")
        self.assertEqual([(h["symbol"], h["at"]) for h in hits],
                         [("HDFCBANK", "9:45 AM"), ("RELIANCE", "9:45 AM"), ("M&M", "10:50 AM"),
                          ("SBIN", "11:25 AM")])

    def test_the_9_15_grid_and_the_first_bar_after_the_range(self):
        day = D(2026, 10, 28)
        bars = scan.session_bars(self.data["RELIANCE"], day, DT(2026, 10, 28, 9, 35))
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars], ["09:15", "09:20", "09:25", "09:30"])
        hits, _, _ = self.scan_at("2026-10-28 09:44", syms=["RELIANCE"])
        self.assertEqual(hits, [])                         # the 9:40 bar is not done until 9:45
        hits, _, _ = self.scan_at("2026-10-28 09:45", syms=["RELIANCE"])
        self.assertEqual(hits[0]["at"], "9:45 AM")

    def test_a_full_session_has_75_bars_and_the_feed_drops_15_20(self):
        day = D(2026, 10, 28)
        bars = scan.session_bars(self.data["RELIANCE"], day, DT(2026, 10, 28, 15, 30))
        self.assertEqual(len(bars), 74)                    # 9:15 to 15:25, Yahoo left 15:20 empty
        self.assertNotIn("15:20", [b["start"].strftime("%H:%M") for b in bars])
        self.assertEqual(scan.gaps(bars, day, DT(2026, 10, 28, 15, 30)), 1)
        self.assertTrue(scan.usable(bars, day, DT(2026, 10, 28, 15, 30), 3))

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        s = self.data["RELIANCE"]
        self.assertEqual(scan.ts_to_ist(s["t"][-1]), DT(2026, 10, 29, 13, 35, 17))   # off the grid
        bars = scan.session_bars(s, D(2026, 10, 29), DT(2026, 10, 29, 13, 37))
        self.assertEqual(bars[-1]["start"], DT(2026, 10, 29, 13, 30))   # 13:35 in progress, the tick dropped
        hits, _, _ = self.scan_at("2026-10-29 13:37", syms=["RELIANCE"])
        self.assertEqual((hits[0]["at"], hits[0]["price"]), ("9:40 AM", 1174.3))

    def test_stage_one_is_a_necessary_condition(self):
        for day in (D(2026, 10, 26), D(2026, 10, 27), D(2026, 10, 28), D(2026, 10, 29)):
            cut = DT.combine(day, datetime.time(13, 30))
            for sym, s in self.data.items():
                bars = scan.session_bars(s, day, cut)
                if scan.usable(bars, day, cut, 3) and scan.orb_hit(bars, 3):
                    self.assertTrue(scan.orb_candidate(bars, 3, day, cut), (sym, day))

    def test_missing_bars(self):
        day, cut = D(2026, 10, 28), DT(2026, 10, 28, 10, 15)
        # a range bar missing: unusable
        s = bars_series("2026-10-28", [100, None, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7, 100.8, 100.9, 101])
        self.assertFalse(scan.usable(scan.session_bars(s, day, cut), day, cut, 3))
        # one bar after the range missing: usable, a stage-1 candidate, and the hit says so
        closes = [100, 100.2, 100.1, 100.0, None, 100.6, 100.4, 100.3, 100.2, 100.1, 100.0, 99.9]
        s = bars_series("2026-10-28", closes)
        bars = scan.session_bars(s, day, cut)
        self.assertTrue(scan.usable(bars, day, cut, 3))
        self.assertTrue(scan.orb_candidate(bars, 3, day, cut))
        hit = scan.orb_hit(bars, 3)
        self.assertEqual((hit["at"], hit["bar_index"], hit["missing_before"]), ("9:45 AM", 5, 1))
        # four missing after the range: a thin stock or a halt
        s = bars_series("2026-10-28", [100, 100.2, 100.1, None, None, None, None, 100.6, 100.4, 100.3, 100, 99])
        self.assertFalse(scan.usable(scan.session_bars(s, day, cut), day, cut, 3))

    def test_confirm_uses_the_v8_highs(self):
        spark, _ = scan.load_offline(SPARK5)
        self.assertEqual(sorted(spark), NIFTY19)
        asked = []

        def confirm(sym):
            asked.append(sym)
            if sym not in self.data:
                raise scan.ScanError("no data for %s" % sym)
            return self.data[sym]
        day, cut = D(2026, 10, 28), DT(2026, 10, 28, 10, 15)
        hits, counts, failed = scan.scan_orb(spark, NIFTY19, day, cut, 15, 50, 0, confirm)
        self.assertEqual((counts["scanned"], counts["candidates"], counts["confirmed"]), (19, 14, 5))
        self.assertEqual(len(asked), 14)                   # stage 1 skipped 5 charts
        self.assertIn("NTPC", asked)                       # no 10:05 bar in its spark: sent on to stage 2
        self.assertEqual([h["symbol"] for h in hits], ["HDFCBANK", "RELIANCE"])
        self.assertEqual(len(failed), 9)

    def test_stage_two_cap_takes_the_most_traded(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        day, cut = D(2026, 10, 28), DT(2026, 10, 28, 11, 0)
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, day, cut, 15, 0, 0, confirm, max_confirm=2)
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (2, counts["candidates"] - 2))
        hits_all, _, _ = scan.scan_orb(spark_like, self.syms, day, cut, 15, 0, 0, confirm, max_confirm=0)
        self.assertEqual(len(hits_all), 3)
        for h in hits:
            self.assertIn(h, hits_all)

    def test_price_and_turnover_floors(self):
        hits, counts, _ = self.scan_at("2026-10-28 10:15", min_price=1000)
        self.assertEqual(counts["illiquid"], 2)            # HDFCBANK (676) and SBIN (952) under Rs 1,000
        day, cut = D(2026, 10, 28), DT(2026, 10, 28, 10, 15)
        hits, counts, _ = scan.scan_orb(self.data, self.syms, day, cut, 15, 0, 1e13)
        self.assertEqual((counts["illiquid"], hits), (5, []))

    def test_muhurat_session_uses_its_own_grid(self):
        old = scan.NSE_MUHURAT["2026-11-08"]
        scan.NSE_MUHURAT["2026-11-08"] = ("18:00", "19:00")
        try:
            day = D(2026, 11, 8)
            self.assertEqual(scan.slots_done(day, DT(2026, 11, 8, 19, 30)), 12)
            s = bars_series("2026-11-08", [100, 100.5, 100.2, 100.4, 100.9, 101], start="18:00")
            d, cut = scan.orb_window(scan.parse_now("2026-11-08 18:30"), None, 15, None)
            hits, counts, _ = scan.scan_orb({"X": s}, ["X"], d, cut, 15, 0, 0)
            self.assertEqual((hits[0]["at"], hits[0]["or_high"]), ("6:25 PM", 100.6))
        finally:
            scan.NSE_MUHURAT["2026-11-08"] = old

    def test_window_messages(self):
        until = datetime.time(14, 45)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-10-31 10:15"))
        self.assertIn("Dussehra", msg("2026-10-20 10:15"))
        self.assertIn("Diwali", msg("2026-11-10 10:15"))
        self.assertIn("Muhurat", msg("2026-11-08 18:30"))
        self.assertIn("runs 9:15 AM to 9:30 AM", msg("2026-10-28 09:20"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-10-28 09:34"))
        self.assertIn("expired", msg("2026-10-28 15:00"))
        self.assertIn("expired", msg("2026-10-28 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 3:30 PM IST", msg("2026-10-28 15:45", until=None))
        self.assertIn("later than now", msg("2026-10-28 10:00", at=datetime.time(10, 15)))
        self.assertIn("after the 3:30 PM close", msg("2026-10-28 17:00", at=datetime.time(15, 45)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-28 16:30"), datetime.time(10, 15), 15, until),
                         (D(2026, 10, 28), DT(2026, 10, 28, 10, 15)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-28 09:35"), None, 15, until)[1],
                         DT(2026, 10, 28, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-10-28 10:15"), None, 7, until)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")
        self.charts = os.path.join(self.d, "charts")       # the five real v8 charts, as a folder
        os.makedirs(self.charts)
        for path in V8.values():
            shutil.copy(path, self.charts)

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, out, _ = run(["universe", "--offline", NSE, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("Kept 2,286 EQ-series stocks, skipping the 2 and 5 % price bands.", out)
        self.assertIn("219 series BE (trade-to-trade, no intraday)", out)
        lines = text(uni).splitlines()
        self.assertEqual(len(lines), 2286)
        self.assertIn("M&M|Mahindra & Mahindra Limited|No Band", lines)

        code, out, _ = run(["universe", "--offline", NSE, "--index", "nifty50", "--out", uni])
        self.assertIn("Kept 50 EQ-series stocks in the Nifty 50", out)
        self.assertEqual(len(text(uni).splitlines()), 50)

        # daily over the Nifty 50 with the 19-name fixture: partial, and said so
        code, out, _ = run(["daily", "--universe", uni, "--offline", DAILY, "--now", "2026-10-29 09:00",
                            "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("Daily scan, Thu 2026-10-29 09:00 IST, closes through 2026-10-28", out)
        self.assertIn("Fetched 19 of 50 charts (31 failed or had no data). 19 passed the liquidity filter "
                      "(Rs 50.00+ and Rs 10 crore+ traded), 4 had no bar for 2026-10-28 (stale); 15 scanned.", out)
        self.assertIn("  LT            Rs 3,801.30  RSI  56.4  20-day closing-high breakout, reclaimed", out)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"], doc["as_of"]),
                         ("daily", 50, 15, 31, "2026-10-28 close"))
        self.assertEqual(doc["filters"], {"min_price": 50.0, "min_turnover_inr": 1e8, "min_volume": 0})
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["INFY", "TCS", "LT", "M&M"])

        # the 10:15 scan on Wed Oct 28
        code, out, _ = run(["orb", "--offline", self.charts, "--now", "2026-10-28 10:15", "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("Opening-range breakout scan, Wed 2026-10-28 10:15 AM IST: 15-minute range "
                      "(9:15 AM to 9:30 AM)", out)
        self.assertIn("2 of 5 charts closed above their opening-range high by 10:15 AM", out)
        self.assertIn("   9:45 AM  RELIANCE      Rs 1,199.60  range 1187.00 to 1194.80  +0.40% over", out)
        self.assertIn("not a list of stocks to buy", out)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "source", "exchange")},
                         {"created_by": "trader-scanner", "created_at": "2026-10-28T04:45:00Z", "mode": "orb",
                          "as_of": "2026-10-28 10:15 IST", "universe": 5, "scanned": 5, "source": "yahoo",
                          "exchange": "NSE"})

        # the hits read back as a watchlist, replayed with --at on the last day in the files
        code, out, _ = run(["orb", "--offline", self.charts, "--at", "10:15", "--watchlist", self.wl,
                            "--json", "--no-write"])
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(res["as_of"], "2026-10-29 10:15 IST")
        self.assertEqual((res["universe"], [h["symbol"] for h in res["hits"]]), (2, ["HDFCBANK", "RELIANCE"]))

    def test_networked_orb_with_a_fake_yahoo(self):
        """The networked path, answered from the real spark and v8 files; stage 2 capped and said so."""
        spark = json.loads(scan._read_maybe_gz(SPARK5))
        by_sym = {r["symbol"]: r for r in spark["spark"]["result"]}
        v8 = {}
        for sym, path in V8.items():
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                v8[sym] = fh.read()
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                asked = urllib.parse.unquote(path.split("symbols=")[1].split("&")[0]).split(",")
                return json.dumps({"spark": {"result": [by_sym[y] for y in asked if y in by_sym], "error": None}})
            sym = scan.nse_symbol(urllib.parse.unquote(path.split("/chart/")[1].split("?")[0]))
            if sym in v8:
                return v8[sym]
            return '{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found"}}}'
        old = scan.yahoo_get, scan.SLEEP, scan.utc_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.utc_now = lambda: DT(2026, 10, 28, 4, 50)     # the machine clock, pinned to 10:20 AM IST
        try:
            code, out, _ = run(["orb", "--symbols", ",".join(NIFTY19 + ["ZZZZQX"]), "--now", "2026-10-28 10:15",
                                "--max-confirm", "6", "--no-write"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.utc_now = old
        self.assertEqual(code, 0)
        self.assertIn("Fetched 19 of 20 charts", out)
        self.assertIn("19 usable; 14 candidates", out)
        self.assertIn("PARTIAL: 8 more candidates were not checked", out)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 6)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 1)
        self.assertIn("M%26M.NS", calls[0])
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", self.charts, "--now", "2026-10-31 10:15", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-31 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", self.charts, "--now", "2026-10-20 10:15"])
        self.assertIn("NSE holiday: Dussehra", err)
        code, _, err = run(["orb", "--offline", self.charts, "--now", "2026-10-28 09:30", "--out", self.wl])
        self.assertIn("after 9:35 AM", err)
        cfg = os.path.join(self.t, "config.json")
        os.makedirs(self.t)
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "10:00"}, fh)
        code, _, err = run(["orb", "--offline", self.charts, "--now", "2026-10-28 10:15", "--config", cfg])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", self.charts])
        self.assertIn("--at HH:MM", err)
        code, out, _ = run(["orb", "--offline", self.charts, "--now", "2026-10-28 09:40", "--symbols", "RELIANCE",
                            "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 9:40 AM", out)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)
        code, _, err = run(["universe", "--offline", self.d])
        self.assertIn("no EQUITY_L.csv", err)

    def test_index_preset_on_daily(self):
        code, out, _ = run(["daily", "--index", "nifty50", "--lists", NSE, "--offline", DAILY,
                            "--now", "2026-10-29 09:00", "--json", "--no-write"])
        res = json.loads(out)
        self.assertEqual((res["index"], res["universe"], res["failed"]), ("nifty50", 50, 31))

    def test_offline_folder_names_charts_by_their_symbol(self):
        data, errors = scan.load_offline(self.charts)
        self.assertEqual(sorted(data), ["HDFCBANK", "INFY", "M&M", "RELIANCE", "SBIN"])
        with open(os.path.join(self.charts, "GONE.json"), "w") as fh:
            fh.write('{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found"}}}')
        code, out, _ = run(["orb", "--offline", self.charts, "--now", "2026-10-28 10:15", "--no-write", "--json"])
        res = json.loads(out)
        self.assertEqual((res["universe"], res["scanned"], res["failed"]), (6, 5, 1))
        self.assertEqual(code, 0)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nRELIANCE|Reliance Industries Limited|No Band\nm&m\n\nsbin.ns\nRELIANCE\n")
        self.assertEqual(scan.symbols_from_file(txt), ["RELIANCE", "M&M", "SBIN.NS", "RELIANCE"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "infy"}, {"symbol": "TCS"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["INFY", "TCS"])
        code, out, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-10-29 09:00",
                            "--json", "--no-write"])
        self.assertEqual(json.loads(out)["universe"], 3)   # RELIANCE, M&M, SBIN (.NS dropped, no repeats)
        code, out, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-10-29 09:00", "--json",
                            "--no-write"])
        self.assertEqual(json.loads(out)["universe"], 3)

    def test_calendar_warning_is_written(self):
        code, out, _ = run(["daily", "--offline", DAILY, "--now", "2027-01-04 09:00", "--json", "--no-write"])
        self.assertIn("2027", json.loads(out)["warning"])


if __name__ == "__main__":
    unittest.main()
