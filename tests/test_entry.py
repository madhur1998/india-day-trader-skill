"""trader-entry tests. Real bars from tests/fixtures/_nse/ (read-only, recorded
2026-10-29 IST afternoon; the last full session is 2026-10-28):

chart-RELIANCE-60d-5m.json.gz  RELIANCE's five-minute bars, 60 days. The reel is
                               Wed 2026-10-28: range 1,187.00 to 1,194.80, the 9:40
                               bar closes 1,199.60 above it, so the trigger is 9:45 AM.
                               The file ends on 2026-10-29 with a null 13:35 bar and
                               Yahoo's live tick at 13:35:17.
chart-INFY-60d-5m.json.gz      INFY on 2026-10-28: no close above its range high
                               1,130.00 all day (expires at the 14:45 cutoff).
chart-SBIN-60d-5m.json.gz      SBIN on 2026-10-28: waits until the 11:20 bar.
chart-*-5d-1m.json.gz          one-minute bars, 2026-10-23 to 2026-10-29.
sec_list.csv                   NSE's band file (big F&O stocks say "No Band").

tests/fixtures/trader-entry/DEMO-1m-synthetic.json is SYNTHETIC one-minute bars
(not market data) on the IST grid: a wick above the range high, a close AT the
high, a null bar, a live tick.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("trader-entry", "entry")

NSE = os.path.join(FIX, "_nse")
RELIANCE = os.path.join(NSE, "chart-RELIANCE-60d-5m.json.gz")
INFY = os.path.join(NSE, "chart-INFY-60d-5m.json.gz")
SBIN = os.path.join(NSE, "chart-SBIN-60d-5m.json.gz")
MM = os.path.join(NSE, "chart-MM-60d-5m.json.gz")
SBIN_1M = os.path.join(NSE, "chart-SBIN-5d-1m.json.gz")
RELIANCE_1M = os.path.join(NSE, "chart-RELIANCE-5d-1m.json.gz")
SEC_LIST = os.path.join(NSE, "sec_list.csv")
DEMO = os.path.join(FIX, "trader-entry", "DEMO-1m-synthetic.json")
OPEN_28 = 1793159100       # 2026-10-28 09:15 IST
DT = datetime.datetime
_DOCS = {}


def ist(text):
    return entry.parse_now(text)


def doc(path=RELIANCE):
    if path not in _DOCS:
        _DOCS[path] = json.loads(entry.read_text(path))
    return copy.deepcopy(_DOCS[path])


def chart(d=None, sym="RELIANCE"):
    return entry.parse_chart(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift(d, from_open, to_open):
    """Every bar moved so that from_open (unix seconds) lands on to_open."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    delta = to_open - from_open
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def unix(day, hhmm):
    """India time -> unix seconds."""
    local = DT.combine(day, entry._hm(hhmm))
    return int((entry.ist_to_utc(local) - DT(1970, 1, 1)).total_seconds())


def cut(d, last_ts, data_time):
    """A live-looking file: bars up to last_ts only, Yahoo's last trade at data_time."""
    d = copy.deepcopy(d)
    r0 = d["chart"]["result"][0]
    keep = [i for i, t in enumerate(r0["timestamp"]) if t <= last_ts]
    r0["timestamp"] = [r0["timestamp"][i] for i in keep]
    q = r0["indicators"]["quote"][0]
    for k in q:
        q[k] = [q[k][i] for i in keep]
    r0["meta"]["regularMarketTime"] = data_time
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheReel(unittest.TestCase):
    def test_triggers_at_945_on_the_940_bar(self):
        r = at("2026-10-28 09:45")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["symbol"], "RELIANCE")                 # the NSE symbol, not RELIANCE.NS
        self.assertEqual(r["opening_range"]["high"], 1194.8)
        self.assertEqual(r["opening_range"]["low"], 1187.0)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual(t["time"], "9:45 AM")
        self.assertEqual(t["bar_start"], "09:40")
        self.assertEqual(t["bar_index"], 5)
        self.assertEqual(t["price"], 1199.6)                     # on the 0.10 tick grid
        self.assertEqual(t["close"], 1199.5999755859375)
        self.assertEqual(t["pct_over"], 0.4)
        self.assertEqual(r["level"], 1194.8)
        self.assertEqual((r["opens"], r["closes"]), ("09:15", "15:30"))
        self.assertEqual((r["no_entries_before"], r["no_entries_after"]), ("09:30", "14:45"))

    def test_at_944_the_940_bar_is_still_forming(self):
        r = at("2026-10-28 09:44")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:35")
        self.assertEqual(r["last"]["close"], 1191.8)
        self.assertEqual(r["forming"]["bar_start"], "09:40")
        self.assertIn("Rs 3.00 under the range high Rs 1,194.80", r["reason"])
        self.assertEqual(at("2026-10-28 09:44:59")["status"], "waiting")

    def test_not_open_yet_and_while_the_range_forms(self):
        for t in ("2026-10-28 08:30", "2026-10-28 09:05", "2026-10-28 09:14:59"):
            r = at(t)
            self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"), t)
            self.assertIn("opens at 9:15", r["reason"])
            self.assertIn("NOT OPEN YET", "\n".join(entry.describe(r)))
        r = at("2026-10-28 09:25")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-28 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_null_bar_are_dropped(self):
        c = chart()
        self.assertEqual(c["bars"][-1]["t"], 1793261117)          # 13:35:17 on 2026-10-29 is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))     # read as of the file's own time
        self.assertEqual(r["now"], "2026-10-29 13:35:17")
        self.assertEqual(r["_bars"][-1]["slot"], 51)               # the 13:30 bar
        self.assertEqual(r["bars_closed"], 52)
        self.assertIsNone(r["forming"])                            # Yahoo sent the 13:35 bar as nulls
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_a_closed_bar_waits_for_yahoo_to_catch_up(self):
        d = cut(doc(), unix(datetime.date(2026, 10, 29), "13:25"), unix(datetime.date(2026, 10, 29), "13:29") + 40)
        r = at("2026-10-29 13:30:10", d)
        self.assertEqual(r["_bars"][-1]["slot"], 49)               # 13:25 closed by the clock only
        self.assertIn("Yahoo's last trade is 13:29:40", r["lag_note"])
        r = at("2026-10-29 13:31:10", d)                           # a minute later it counts
        self.assertEqual(r["_bars"][-1]["slot"], 50)

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-28 16:00")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "9:45 AM")
        self.assertEqual(r["last"]["close"], 1158.0)               # Yahoo's 15:25 bar carries the close
        text = "\n".join(entry.describe(r))
        self.assertIn("375 minutes ago", text)
        self.assertNotIn("This is the moment", text)

    def test_prices_print_in_rupees(self):
        text = "\n".join(entry.describe(at("2026-10-28 09:45")))
        self.assertIn("RELIANCE  Wed 2026-10-28 09:45 IST", text)
        self.assertIn("Opening range 9:15-9:30: high Rs 1,194.80, low Rs 1,187.00 (3 of 3 bars).", text)
        self.assertIn("TRIGGERED 9:45 AM at Rs 1,199.60: the 9:40 bar (bar 5) closed above the range high "
                      "Rs 1,194.80 (+0.40%).", text)
        self.assertNotIn("$", text)


class Rules(unittest.TestCase):
    def test_volume_average_skips_the_empty_915_bar(self):
        r = at("2026-10-28 09:45", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 228242)   # (292856 + 163628) / 2: the 9:15 bar says 0
        self.assertEqual(r["trigger"]["volume_x"], 2.45)
        r = at("2026-10-28 09:50", min_volume_x=2.5)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("the 9:45 bar closed above at Rs 1,203.30 but on 2.14x the range's volume, under your 2.50x",
                      r["reason"])
        self.assertEqual(at("2026-10-28 14:45", min_volume_x=2.5)["status"], "expired")

    def test_level_setup(self):
        r = at("2026-10-28 11:00", setup="level", level=1200)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("9:50 AM", 6, 1203.3))
        # a level under the open: the first eligible close is the 9:25 bar's, at 9:30 (no_entries_before)
        r = at("2026-10-28 11:00", setup="level", level=1150)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-28 11:00", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = doc(INFY)
        r = at("2026-10-28 11:00", d)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["opening_range"]["high"], 1130.0)
        r = at("2026-10-28 14:44", d)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Rs 16.95 under the range high Rs 1,130.00", r["reason"])
        r = at("2026-10-28 14:45", d)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertIn("by 14:45", r["reason"])
        self.assertIn("No entry on this setup today", "\n".join(entry.describe(r)))
        self.assertEqual(at("2026-10-28 16:30", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        day = datetime.date(2026, 10, 28)
        d = cut(doc(INFY), unix(day, "12:55"), unix(day, "12:59") + 40)   # live: up to the 12:55 bar
        cfg = {"no_entries_before": "09:30", "no_entries_after": "13:00"}
        r = at("2026-10-28 13:00:10", d, cfg=cfg)                 # the 12:55 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 12:59:40", r["lag_note"])
        self.assertEqual(at("2026-10-28 13:01:10", d, cfg=cfg)["status"], "expired")

    def test_config_window(self):
        r = at("2026-10-28 09:45", cfg={"no_entries_before": "09:30", "no_entries_after": "09:40"})
        self.assertEqual(r["status"], "expired")
        r = at("2026-10-28 11:00", cfg={"no_entries_before": "10:00", "no_entries_after": "14:45"})
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("10:00 AM", 1195.7))  # the 9:55 bar
        r = at("2026-10-28 09:47", cfg={"no_entries_before": "10:00", "no_entries_after": "14:45"})
        self.assertIn("but no entries before 10:00", r["reason"])

    def test_bad_inputs_and_the_india_clock(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-28 09:45", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-28 09:45", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-10-28 09:45"), DT(2026, 10, 28, 4, 15))   # UTC+5:30, all year
        self.assertEqual(entry.parse_now("2026-10-28 09:45 IST"), DT(2026, 10, 28, 4, 15))
        self.assertEqual(entry.parse_now("2026-10-28T04:15:00Z"), DT(2026, 10, 28, 4, 15))
        self.assertEqual(entry.parse_now("2026-01-05 09:45"), DT(2026, 1, 5, 4, 15))     # no daylight saving

    def test_or_minutes_30(self):
        r = at("2026-10-28 11:00", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1199.6)       # the 9:40 bar is inside the range now
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("9:50 AM", 1203.3))

    def test_a_long_wait(self):
        d = doc(SBIN)
        r = at("2026-10-28 11:00", d)
        self.assertEqual(r["status"], "waiting")
        r = at("2026-10-28 11:25", d)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("11:25 AM", 25, 954.6))                  # 0.05 tick under Rs 1,000


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(doc(DEMO)), "DEMO")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-19 09:51:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:44 AM", "09:43", 28, 100.62))
        r = entry.evaluate(c, ist("2026-10-19 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")
        self.assertEqual(r["last"]["close"], 100.5)             # the 9:42 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:47 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 9:34 came as nulls
        self.assertEqual(slots[-1], 35)                           # 9:51 still forming, 9:51:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:51")

    def test_real_one_minute_bars(self):
        r = entry.evaluate(chart(doc(SBIN_1M)), ist("2026-10-28 12:00"), interval="1m")
        self.assertEqual(r["opening_range"]["bars"], 15)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("11:02 AM", 106, 955.5))
        r = entry.evaluate(chart(doc(RELIANCE_1M)), ist("2026-10-28 12:00"), interval="1m")
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("9:43 AM", 1196.3))

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-10-24 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertIn("weekend", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-26 09:15")
        r = at("2026-10-20 10:30")                                # Yahoo has no bars that day either
        self.assertIn("NSE holiday: Dussehra", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-21 09:15")
        empty = doc()
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-28 09:45", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-10-30 10:15")                                # Yahoo sent earlier days only
        self.assertEqual(r["status"], "no-data")
        r = at("2027-03-01 10:30")
        self.assertFalse(r["calendar_known"])
        self.assertIn("covers 2026 only", "\n".join(entry.describe(r)))

    def test_muhurat_without_times_is_closed_and_says_why(self):
        r = at("2026-11-08 18:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertIn("Muhurat trading day: session times not published yet", r["reason"])

    def test_muhurat_session_once_announced(self):
        saved = dict(entry.NSE_MUHURAT)
        entry.NSE_MUHURAT["2026-11-08"] = ("18:00", "19:00")
        try:
            day = datetime.date(2026, 11, 8)
            d = shift(doc(), OPEN_28, unix(day, "18:00"))           # the 2026-10-28 session, moved to 18:00
            r = at("2026-11-08 17:30", d)
            self.assertEqual(r["phase"], "pre-open")
            self.assertIn("opens at 18:00 (Muhurat trading)", r["reason"])
            r = at("2026-11-08 18:30", d)
            self.assertEqual((r["opens"], r["closes"]), ("18:00", "19:00"))
            self.assertEqual((r["no_entries_before"], r["no_entries_after"]), ("18:15", "18:45"))
            self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("6:30 PM", 5))
            self.assertIn("Muhurat trading session today, 18:00 to 19:00", "\n".join(entry.describe(r)))
            d = shift(doc(INFY), OPEN_28, unix(day, "18:00"))
            r = at("2026-11-08 18:45", d)
            self.assertEqual(r["status"], "expired")
            self.assertIn("by 18:45", r["reason"])
            self.assertIn("Muhurat", r["reason"])
        finally:
            entry.NSE_MUHURAT.clear()
            entry.NSE_MUHURAT.update(saved)

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Bands(unittest.TestCase):
    """The circuit check. Big stocks have no fixed band, so the tests use a small band file."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "sec_list.csv")
        with open(self.path, "w") as fh:
            fh.write('Symbol,Series,Security Name,Band,Remarks\n'
                     'RELIANCE,EQ,RELIANCE INDUSTRIES LIMITED,2,"-"\n'
                     'INFY,EQ,INFOSYS LIMITED,5,"-"\n'
                     'SBIN,BE,STATE BANK OF INDIA,20,"-"\n')
        self.bands = entry.read_bands(self.path)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_previous_close_from_the_file(self):
        c = chart()
        self.assertEqual(entry.prev_close_of(c, datetime.date(2026, 10, 28))[0], 1196.2)
        self.assertEqual(entry.prev_close_of(c, datetime.date(2026, 10, 29))[0], 1158.0)   # = Yahoo's previousClose
        self.assertEqual(c["prev_close"], 1158.0)

    def test_not_near_the_band(self):
        r = at("2026-10-28 09:45", bands=self.bands)
        self.assertEqual((r["band"]["upper"], r["band"]["lower"]), (1220.1, 1172.3))   # 1,196.20 +/- 2%, inside the band
        self.assertIsNone(r["band"]["warning"])
        self.assertIn("Circuit limits today Rs 1,172.30 to Rs 1,220.10 (2% band), not near.", r["band_note"])

    def test_near_the_upper_circuit(self):
        r = at("2026-10-29 09:45", bands=self.bands)               # trigger 1,174.30 vs upper 1,181.10
        self.assertEqual(r["band"]["upper"], 1181.1)
        self.assertIn("CIRCUIT: the trigger price Rs 1,174.30 is within 1% of the upper circuit Rs 1,181.10",
                      r["band"]["warning"])
        self.assertIn("CIRCUIT", "\n".join(entry.describe(r)))
        self.assertEqual(entry.entry_doc(r, ist("2026-10-29 09:45"))["band_warning"], r["band"]["warning"])

    def test_near_the_lower_circuit(self):
        r = at("2026-10-28 14:30", doc(INFY), bands=self.bands)    # INFY down from 1,163.00, 5% band
        self.assertEqual(r["band"]["lower"], 1104.9)               # 1,104.85 rounded up onto the 0.10 tick
        self.assertIn("lower circuit Rs 1,104.90", r["band"]["warning"])

    def test_series_no_band_and_missing(self):
        r = at("2026-10-28 11:25", doc(SBIN), bands=self.bands)
        self.assertIn("BE series: trade-for-trade, no intraday", r["band"]["warning"])
        real = entry.read_bands(SEC_LIST)
        self.assertIsNone(real["M&M"]["band"])
        r = entry.evaluate(chart(doc(MM), "M&M"), ist("2026-10-28 11:00"), bands=real)
        self.assertEqual(r["symbol"], "M&M")
        self.assertIn("no fixed price band", r["band_note"])
        self.assertIsNone(r["band"]["warning"])
        r = entry.evaluate(entry.parse_chart(json.dumps(doc(DEMO)), "DEMO"), ist("2026-10-19 09:50"),
                           interval="1m", bands=real)
        self.assertIn("not in the band file", r["band_note"])
        with self.assertRaises(entry.EntryError):
            entry.read_bands(DEMO)


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        self.body = entry.read_text(RELIANCE)

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 28), today=datetime.date(2026, 10, 28))
        self.assertEqual(c["symbol"], "RELIANCE")
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 28), today=datetime.date(2026, 10, 28))
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 28), today=datetime.date(2026, 10, 28))
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "RELIANCE", "--now", "2026-10-28 09:45", "--config", "none.json"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", datetime.date(2026, 10, 28), today=datetime.date(2026, 10, 28))
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_and_symbols(self):
        today = datetime.date(2026, 10, 29)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 28), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 9), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 10), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 9), today, "1m")
        self.assertIn("/chart/M%26M.NS?", entry.chart_path("m&m", "1d", "5m"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", entry.chart_path("BAJAJ-AUTO", "1d", "5m"))


def live_provider(d, sym="RELIANCE", spike=1196.0, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to `spike` (above the high, then it closes under), plus a live tick.
    lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d, sym)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = spike, max(b["h"], spike)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": spike, "h": spike, "l": spike, "c": spike, "v": 0})
        return {"symbol": base["symbol"], "meta": {}, "bars": bars, "prev_close": 1196.2, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-10-28 09:37", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:45 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1199.6)
        # it looked at 9:37 (9:35 bar spiking to 1,196 in progress) and at 9:40:02 (that bar closed 1,191.80)
        self.assertEqual([entry.to_ist(c).strftime("%H:%M:%S") for c in calls], ["09:37:00", "09:40:02", "09:45:02"])
        text = "\n".join(lines)
        self.assertIn(" 9:35 bar  closed Rs 1,191.80  Rs 3.00 under Rs 1,194.80", text)
        self.assertIn(" 9:40 bar  closed Rs 1,199.60  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        lag = ist("2026-10-28 09:45:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=lag, calls=calls),
                                                   "2026-10-28 09:42", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["09:42:00", "09:45:02", "09:45:32"])   # 9:45:02: Yahoo still at 9:44:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:45 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-10-28 08:50", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn("first bar's close at 9:20", lines[0])
        self.assertIn(" 9:25 bar  closed Rs 1,189.30  opening range set: high Rs 1,194.80, low Rs 1,187.00", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 6)    # 9:15 ... 9:40, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-28 10:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(SBIN), "SBIN", spike=954.5, calls=calls), clock.now,
                                     clock.sleep, lines.append, dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_ist(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["10:31:00", "10:49:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in ("10:30", "10:35", "10:40"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "11:25 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc(INFY), "INFY", spike=1131.0),
                                                   "2026-10-28 14:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_ist(clock.now()).strftime("%H:%M"), "14:45")
        until = DT(2026, 10, 28, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(doc(INFY), "INFY", spike=1131.0),
                                                   "2026-10-28 10:30", every=300, until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-24 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-20 10:00")
        self.assertIn("Dussehra", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-28 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", RELIANCE, "--now", "2026-10-28 09:45", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 9:45 AM at Rs 1,199.60", text)
        self.assertIn("/trader-exit", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy",
                "date": "2026-10-28", "status": "triggered", "time": "9:45 AM", "bar_start": "09:40", "bar_index": 5,
                "price": 1199.6, "level": 1194.8, "interval": "5m",
                "opening_range": {"high": 1194.8, "low": 1187.0, "minutes": 15},
                "source": "yahoo", "checked_at": "2026-10-28T04:15:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)
        self.assertNotIn("band_warning", e)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", RELIANCE, "--now", "2026-10-28 09:44", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "RELIANCE", "--offline", RELIANCE, "--now", "2026-10-28 09:44",
                             "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_gz_folder_ampersand_and_mismatch(self):
        os.mkdir("charts")
        shutil.copy(RELIANCE, os.path.join("charts", "RELIANCE.json.gz"))
        shutil.copy(MM, os.path.join("charts", "M&M.json.gz"))
        code, text, _ = run(["check", "reliance", "--offline", "charts", "--now", "2026-10-28 09:45"])
        self.assertEqual(code, 0)
        code, text, _ = run(["check", "M&M", "--offline", "charts", "--now", "2026-10-28 11:00"])
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("M&M  Wed 2026-10-28 11:00 IST"), text)
        self.assertIn("TRIGGERED 10:50 AM at Rs 2,852.10", text)   # tick 0.10 between Rs 1,000 and Rs 5,000
        code, _, err = run(["check", "SBIN", "--offline", RELIANCE])
        self.assertEqual(code, 1)
        self.assertIn("holds RELIANCE, not SBIN", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "RELIANCE", "--offline", RELIANCE, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("longs only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", RELIANCE, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", RELIANCE, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:40"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", RELIANCE, "--now", "2026-10-28 09:45"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"max_loss_per_trade_" + "usd": 50}, fh)
        code, _, err = run(["check", "RELIANCE", "--offline", RELIANCE])
        self.assertEqual(code, 1)
        self.assertIn("rename it max_loss_per_trade_inr", err)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", RELIANCE])
        self.assertIn("not valid JSON", err)

    def test_bands_flag(self):
        code, text, _ = run(["check", "M&M", "--offline", MM, "--now", "2026-10-28 11:00", "--bands", SEC_LIST])
        self.assertEqual(code, 0)
        self.assertIn("no fixed price band", text)
        code, _, err = run(["check", "M&M", "--offline", MM, "--bands", "missing.csv"])
        self.assertIn("could not read missing.csv", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", RELIANCE, "--now", "2026-10-28 09:31", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn(" 9:40 bar  closed Rs 1,199.60  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 5)
        code, text, _ = run(["watch", "RELIANCE", "--offline", RELIANCE, "--now", "2026-10-28 09:31",
                             "--until", "09:40", "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(RELIANCE, os.path.join("charts", "RELIANCE.json.gz"))
        shutil.copy(INFY, os.path.join("charts", "INFY.json.gz"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [
                {"symbol": "RELIANCE", "setups": ["orb"], "or_high": 1194.8, "or_low": 1187.0},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]},
                {"symbol": "INFY", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-28 09:45"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("RELIANCE    triggered  9:45 AM   Rs 1,199.60   Rs 1,194.80"), rows[1])
        self.assertIn("ZZZZ        error: no offline file", rows[2])
        self.assertIn("DEMO        error: DEMO has 1m bars", rows[3])
        self.assertTrue(rows[4].startswith("INFY        waiting    -         -             Rs 1,130.00"), rows[4])
        self.assertIn("1 of 4 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-28 09:44", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /trader-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/trader-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "9:45 am", "rs 1,199.60", "bar 5",
                     "9:15", "muhurat", "circuit", "sec_list.csv", "time my entry on reliance", "not financial advice"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", "nyse"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
