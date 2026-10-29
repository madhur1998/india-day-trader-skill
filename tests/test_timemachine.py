import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")

NSE_DIR = os.path.join(FIX, "_nse")   # shared fixtures, read-only: recorded 2026-10-29 about 13:35 IST
RELIANCE = os.path.join(NSE_DIR, "chart-RELIANCE-60d-5m.json.gz")   # Yahoo v8 chart, range=60d interval=5m
with gzip.open(RELIANCE, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
with gzip.open(os.path.join(NSE_DIR, "chart-NSEI-60d-5m.json.gz"), "rt", encoding="utf-8") as _fh:
    NIFTY = _fh.read()
NOW = "2026-10-29 13:36"
NOW_UTC = tm.parse_now(NOW)

# the plan, as trader-exit writes it: RELIANCE on Thursday 2026-10-29, 15-minute opening range
# 1156.20 to 1170.50, the 9:35 bar closes above it at 1174.30, stop at the range midpoint 1163.35
# rounded down onto the 0.10 tick, 1R target
PLAN = {"created_by": "trader-exit", "symbol": "RELIANCE", "side": "buy", "date": "2026-10-29", "setup": "orb",
        "entry": 1174.3, "entry_time": "9:40 AM", "entry_bar": 4, "interval": "5m",
        "opening_range": {"high": 1170.5, "low": 1156.2, "minutes": 15},
        "stop": 1163.3, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1185.3, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 11.0, "reward_risk": 1.0, "flat_by": "15:10", "tick_size": 0.1}

# fingerprints of the seed-19 run on this data: any change to the draws shows up here
SIM_BANDS = "5fbaaa41bf792975"
SIM_PATHS = "2d34045793bcb7eb"
SIM_TRADES = "dc358220be6c4660"
SIM_HIST = {"lo": 1143.94, "hi": 1198.03,
            "bins": [92, 87, 110, 173, 222, 288, 373, 492, 604, 677, 710, 844, 838, 816, 773, 686, 522, 442,
                     353, 274, 198, 109, 105, 56]}
GAPS = ["2026-08-14", "2026-08-25", "2026-09-01", "2026-09-08", "2026-09-25", "2026-10-15", "2026-10-22"]
VERDICT = ("Leans toward the stop: at 1R (0.91R after charges) the target has to come first in 54 % of the "
           "replays that hit one side; it did in 40 % (target first 24.2 % vs stop first 36.2 %).")


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture's chart JSON, changed by fn(result), back as text."""
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.ts_to_ist(ts)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Seed19(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine(index_text=NIFTY)

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:20"))
        self.assertEqual(m["pool_sessions"], 52)
        self.assertEqual(m["bars_left"], 68)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-04", "2026-10-28"))
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.2417, 0.3622, 0.3961, 0.4245))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (1171.72, 1152.79, 1189.65))
        self.assertEqual((m["avg_R"], m["r_at_target"]), (-0.138, 1.0))

    def test_r_net_of_charges(self):
        m = self.doc["mc"]
        # 85 shares is about Rs 1 lakh at 1174.30; Rs 40 brokerage (Rs 20 a side), STT, exchange, SEBI, stamp, GST
        self.assertEqual(m["charges"], {"qty": 85, "at_target": 82.47, "at_stop": 82.4, "qty_from": "nominal",
                                        "rates": "default"})
        self.assertEqual(m["charges"]["at_target"], tm.charges(85, 1174.3, 1185.3)["total"])
        self.assertEqual((m["r_at_target_net"], m["r_at_stop_net"]), (round(1 - 82.47 / 935, 4), round(-1 - 82.4 / 935, 4)))
        self.assertEqual(m["avg_R_net"], -0.227)
        self.assertLess(m["avg_R_net"], m["avg_R"])

    def test_the_draws_are_pinned(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), SIM_BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), SIM_PATHS)
        self.assertEqual(m["hist"], SIM_HIST)
        self.assertEqual(m["bands"][-1], [1152.79, 1164.68, 1172.38, 1179.25, 1189.72])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (52, 26))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (12, 10, 4, 13))
        self.assertEqual((h["avg_R"], h["avg_R_net"]), (0.078, -0.135))
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), SIM_TRADES)
        self.assertEqual(h["trades"][0], {"date": "2026-08-06", "time": "9:45 AM", "entry": 1300.9, "stop": 1292.0,
                                          "target": 1309.8, "outcome": "target", "R": 1.0, "R_net": 0.88})
        for t in h["trades"]:   # every stop and target sits on the 0.10 tick
            self.assertEqual(round(t["stop"] * 10, 6) % 1, 0, t)
            self.assertEqual(round(t["target"] * 10, 6) % 1, 0, t)

    def test_the_verdict_weighs_the_charges(self):
        self.assertEqual(self.doc["verdict"], VERDICT)
        # gross, 1R breaks even at 50 %; after Rs 82 of charges on a 935 rupee risk it needs 54 %
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), round(even, 3)), (0.4, 0.544))
        self.assertEqual(self.doc["verdict_notes"], [])

    def test_the_nifty_context(self):
        self.assertEqual(self.doc["context"], {"index": "^NSEI", "at_entry": 22758.4, "prev_close": 22698.85,
                                               "change_pct": 0.26, "symbol_change_pct": 1.41})
        self.assertIsNone(machine(replays=50)["context"])
        cx = machine(replays=50, index_text="<html>")["context"]
        self.assertIn("not JSON", cx["note"])
        self.assertIn("Nifty 50 context: not available", "\n".join(tm.report(machine(replays=50, index_text="x"))))

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1174.3] * 5)
        self.assertEqual(len(m["bands"]), 69)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:40", "15:20", 69))
        self.assertTrue(all(p[0] == 1174.3 and len(p) == 69 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("RELIANCE", 1174.3, 1163.3, 1185.3, 4, "9:40 AM"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"], p["tick_size"]), (11.0, 1.0, "15:10", 0.1))
        self.assertNotIn("qty", p)
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["data"]["last_bar"], "2026-10-29 13:30 IST")

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy, Thursday 2026-10-29: entry 1174.30 at 9:40 AM (bar 4), stop 1163.30, "
                     "target 1185.30 (1R, risk 11.00 a share).",
                     "10,000 replays", "9:40 AM to 3:20 PM (68 five-minute bars)", "seed 19", "52 full sessions",
                     "target first   24.2 %", "stop first     36.2 %", "neither        39.6 %   still open at 3:20 PM",
                     "green          42.4 %", "median 1171.72, 90 % of replays between 1152.79 and 1189.65",
                     "Average R: -0.138 gross, -0.227 after charges",
                     "on 85 shares (a nominal position of about Rs 1 lakh", "Rs 82.47 at the target, Rs 82.40 at the "
                     "stop. After charges the target is +0.91R and the stop -1.09R.",
                     "Context at the 9:40 AM entry: Nifty 50 22758.40, +0.26 % on the day; RELIANCE +1.41 %.",
                     "fired 26 times: 12 target, 10 stop, 4 neither. Average R +0.078 gross, -0.135 after charges.",
                     VERDICT, "Left out 7 session(s) with a gap before 3:20 PM", "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "$"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.2417, 0.3622, 1171.72))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), SIM_BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2; the pinned numbers above
        # are the real proof, this pins the first draws so a failure points at the RNG
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE")

    def test_the_live_tick_and_the_missing_closing_bars(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        # Yahoo has no NSE 15:20 bar on any day and no 15:25 bar on most: those come as nulls
        self.assertEqual(dropped, {"no_price": 115, "off_grid": 1, "not_complete": 0, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-29", "13:30", 51))
        self.assertFalse(any(b["start"] == "15:20" for b in bars))
        entry = [b for b in bars if b["date"] == "2026-10-29" and b["slot"] == 4][0]
        self.assertEqual((entry["start"], round(entry["c"], 2)), ("09:35", 1174.3))

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-10-29 09:37"))[1]
        self.assertEqual(len([b for b in bars if b["date"] == "2026-10-29"]), 4)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-29 09:37"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:30 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-10-29 09:40"), replays=200)["mc"]["pool_sessions"], 52)

    def test_india_time_has_no_daylight_saving(self):
        stamps = [1793245500,   # 2026-10-29 03:45Z = 09:15 IST
                  1785728700,   # 2026-08-03 03:45Z = 09:15 IST, months away: the same offset
                  1793244600,   # 03:30Z = 09:00 IST, the pre-open: not a bar
                  1793268000,   # 10:00Z = 15:30 IST, the close: not a bar
                  1793284200]   # 14:30Z = 20:00 IST, an evening (Muhurat-style) bar: outside hours
        res = {"meta": {"symbol": "X.NS"}, "timestamp": stamps,
               "indicators": {"quote": [{"open": [1] * 5, "high": [1] * 5, "low": [1] * 5, "close": [1] * 5,
                                         "volume": [0] * 5}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-10-30T00:00Z"))
        self.assertEqual([(b["date"], b["start"], b["slot"]) for b in bars],
                         [("2026-08-03", "09:15", 0), ("2026-10-29", "09:15", 0)])
        self.assertEqual(dropped["outside_hours"], 3)

    def test_sessions_with_a_gap_are_left_out(self):
        doc = machine(replays=300)
        self.assertEqual([x["date"] for x in doc["data"]["short_sessions_left_out"]], GAPS)
        self.assertEqual(doc["history"]["sessions"], 52)
        self.assertFalse(set(GAPS) & {t["date"] for t in doc["history"]["trades"]})
        # most of those only miss the thin 15:15 bar: an earlier --until keeps them
        early = machine(until="15:10", replays=300)
        self.assertEqual((early["mc"]["pool_sessions"], early["mc"]["bars_left"]), (58, 66))
        self.assertEqual(early["data"]["short_sessions_left_out"], [{"date": "2026-10-15", "bars": 72, "reason": "gap"}])

    def test_a_muhurat_session_is_left_out(self):
        with mock.patch.dict(tm.NSE_MUHURAT, {"2026-10-28": ("09:15", "15:30")}):
            doc = machine(replays=300)
            self.assertEqual(doc["mc"]["pool_sessions"], 51)
            self.assertIn({"date": "2026-10-28", "bars": 74, "reason": "Muhurat session"},
                          doc["data"]["short_sessions_left_out"])
            with self.assertRaises(tm.TMError) as cm:
                plan(date="2026-10-28")
            self.assertIn("Muhurat", str(cm.exception))

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-28")
        self.assertNotIn("2026-10-29", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 116)
        self.assertEqual(doc["mc"]["pool_sessions"], 51)   # 2026-08-04 now has a gap

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        def trim(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-09-28")
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda ts: ist(ts).date().isoformat() >= "2026-09-24")
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="INFY"))
        self.assertIn("data is for RELIANCE", str(cm.exception))

    def test_an_ampersand_symbol(self):
        with gzip.open(os.path.join(NSE_DIR, "chart-MM-60d-5m.json.gz"), "rt", encoding="utf-8") as fh:
            text = fh.read()
        p = plan(symbol="m&m.ns", entry=2873.2, stop=2860.0, target=2886.4, stop_rule="pct", tick_size=None)
        doc = machine(p, text=text, replays=300)
        self.assertEqual((doc["plan"]["symbol"], doc["data"]["symbol"]), ("M&M", "M&M"))
        self.assertEqual(doc["mc"]["charges"]["qty"], 34)      # Rs 1 lakh / 2873.20
        self.assertEqual(tm.file_key("M&M"), "MM")
        self.assertEqual(tm.file_key("^NSEI"), "NSEI")


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-30"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 13:40 has not happened in this data yet
            machine(plan(entry_time="1:45 PM", entry_bar=None, entry=1173.4))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("the data stops at the 13:30 bar", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="9:42 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="3:20 PM", entry_bar=None), "between 9:20 AM and 3:15 PM"),
            (dict(entry_bar=9), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1175.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Oct 29"), "date like"),
            (dict(date="2026-10-20"), "not an NSE trading day (NSE holiday: Dussehra)"),
            (dict(date="2026-10-31"), "not an NSE trading day (weekend)"),
            (dict(date="2026-11-08"), "Muhurat trading day"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(qty=2.5), "whole number of shares"),
            (dict(qty=0), "whole number of shares"),
            (dict(charges_rates={"brokerage": 20}), "unknown key(s): brokerage"),
            (dict(charges_rates={"stt_sell_pct": "x"}), "charges_rates.stt_sell_pct"),
            (dict(tick_size=0), "tick_size"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "9:40 AM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="9:40", entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="9:40am", entry_bar=None)["entry_bar"], 4)
        self.assertEqual(plan(entry_time="2:15", entry_bar=None)["entry_bar"], 59)   # a bare 2:15 is p.m.

    def test_until_cuts_the_replay(self):
        doc = machine(until="15:10", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:10", 66, "15:10"))
        self.assertIn("still open at 3:10 PM", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="3:10 PM", replays=500)["mc"], m)
        for bad, words in (("9:40 AM", "nothing to replay"), ("15:25", "no later than 3:20 PM"),
                           ("15:30", "MIS"), ("15:12", "5-minute grid"), ("noon", "India time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1180.0, stop=1170.0, target=1190.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_stops_round_onto_the_tick_away_from_the_entry(self):
        self.assertEqual(tm.tick_away(1163.35, True), 1163.3)
        self.assertEqual(tm.tick_away(1163.35, False), 1163.4)
        self.assertEqual(tm.tick_away(682.33, True), 682.3)          # 0.05 tick under Rs 1,000
        self.assertEqual(tm.tick_away(682.33, False), 682.35)
        self.assertEqual(tm.tick_away(1156.2000732421875, True), 1156.2)
        self.assertEqual(tm.tick_away(1163.37, True, 0.05), 1163.35)  # the plan's tick_size wins

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1156.2, target=1192.4), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1165.0, target=1192.9), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.792, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 52)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1185.3, target=1163.3, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertLess(m["r_at_target_net"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
            self.assertLess(t["R_net"], t["R"])
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_plans_qty_and_rates(self):
        m = machine(plan(qty=10), replays=200)["mc"]
        self.assertEqual(m["charges"], {"qty": 10, "at_target": 12.2, "at_stop": 12.11, "qty_from": "plan",
                                        "rates": "default"})
        self.assertEqual(m["r_at_target_net"], 0.8891)                # 12.20 on a 110 rupee risk
        free = machine(plan(charges_rates={"brokerage_pct": 0, "brokerage_max": 0}), replays=200)["mc"]
        self.assertEqual((free["charges"]["at_target"], free["charges"]["rates"]), (35.27, "plan"))
        self.assertIn("(the plan's qty)", "\n".join(tm.report(machine(plan(qty=10), replays=200))))

    def test_the_verdict_thresholds_at_1r(self):
        # 1R without charges: the target's share of the replays that hit one side against 50 %
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:10"}),
                         "No replay reached the target or the stop by 3:10 PM: the price at 3:10 PM decides every one.")

    def test_charges_move_the_break_even(self):
        # 52 % of the sided replays is a coin flip gross; after charges a 1R target nets 0.85R and the
        # stop costs 1.15R, so break-even is 57.5 % and the same odds lean toward the stop
        mc = {"p_target": 0.30, "p_stop": 0.28, "r_at_target": 1.0}
        self.assertTrue(tm.verdict(mc).startswith("Close to a coin flip"))
        net = dict(mc, r_at_target_net=0.85, r_at_stop_net=-1.15)
        self.assertEqual(tm.verdict(net), "Leans toward the stop: at 1R (0.85R after charges) the target has to come "
                                          "first in 57 % of the replays that hit one side; it did in 52 % (target "
                                          "first 30.0 % vs stop first 28.0 %).")
        self.assertEqual(round(tm.lean(net)[2], 3), 0.575)
        self.assertEqual(tm.verdict(dict(mc, r_at_target=0.1, r_at_target_net=-0.05, r_at_stop_net=-1.15)),
                         "The charges eat the target: at 0.1R the target nets -0.05R after round-trip charges, so "
                         "even the target loses money.")
        notes = tm.verdict_notes(dict(net, p_target=0.20, p_stop=0.40, p_neither=0.4, avg_R_net=0.02, avg_R=0.1))
        self.assertEqual(notes, ["Average R after charges is +0.020 all the same: the replays that hit neither "
                                 "pull it the other way."])

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_2r_on_the_fixture(self):
        doc = machine(plan(target=1196.3))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"], m["avg_R_net"]),
                         (2.0, 0.0263, 0.3679, 0.6058, -0.176, -0.264))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: at 2R (1.91R after charges) the target has to come "
                                         "first in 36 % of the replays that hit one side; it did in 7 % (target first "
                                         "2.6 % vs stop first 36.8 %).")
        self.assertEqual(doc["verdict_notes"],
                         ["60.6 % of the replays hit neither by 3:20 PM; the price at 3:20 PM decides those."])
        text = "\n".join(tm.report(doc))
        self.assertIn(doc["verdict"] + "\n" + doc["verdict_notes"][0], text)


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes, bodies=None):
        codes = list(codes)
        bodies = list(bodies or [])

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return bodies.pop(0) if bodies else TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("M&M", "query1"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", tm.chart_url("bajaj-auto"))
        self.assertIn("/chart/%5ENSEI?", tm.chart_url("^NSEI"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; india-day-trader-skill/3.0)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("check the NSE symbol", str(cm.exception))

    def test_run_over_the_network_path(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            self.answer([200, 200], [TEXT, NIFTY])        # the stock, then the Nifty 50, half a second apart
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0)
            self.assertEqual(self.calls, ["query2", "query2"])
            self.assertEqual(self.sleeps, [tm.PAUSE])
            self.assertIn("Context at the 9:40 AM entry: Nifty 50", text)
            self.calls = []
            self.answer([200, 404])                       # no Nifty: the run still works, without the line
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0)
            self.assertNotIn("Nifty", text)
            self.calls = []
            self.answer([200])
            code, text, _ = run(["run", "--plan", p, "--now", NOW, "--replays", "300", "--no-index"])
            self.assertEqual((code, self.calls), (0, ["query2"]))
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", NSE_DIR, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   24.2 %", text)
        self.assertIn(VERDICT, text)
        self.assertIn("Nifty 50 22758.40", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "trader-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"], doc["mc"]["avg_R_net"]), (0.2417, -0.138, -0.227))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), SIM_PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (26, 0.078))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.3622)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertIsNone(doc["context"])                  # a single file: no Nifty bars to read
        self.assertEqual(doc["verdict"], VERDICT)
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance", "--date", "2026-10-29", "--entry", "1174.3",
                               "--stop", "1163.3", "--target", "1185.3", "--entry-time", "9:40 AM",
                               "--offline", RELIANCE, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"]), (0.2417, 0.4245))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (12, 10, 0.078))
        code, text, err = run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--json", "--qty", "10"])
        self.assertEqual(json.loads(text)["mc"]["charges"]["qty"], 10)
        code, _, err = run(["run", "--symbol", "RELIANCE", "--entry", "1174.3", "--offline", RELIANCE])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(RELIANCE, os.path.join(self.d, "RELIANCE-60d5m.json.gz"))
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertNotIn("Nifty", text)                # no NSEI file in this folder
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json")) as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", self.d],
                     ["run", "--plan", self.plan, "--offline", RELIANCE, "--until", "9:00"],
                     ["run", "--plan", self.plan, "--offline", RELIANCE, "--qty", "0"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 9:40 AM entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("  1185.30 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1163.30 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1174.30 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("9:40", text)
        self.assertIn("3:20", text)
        self.assertEqual(lines[-1], VERDICT)
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", RELIANCE, "--now", NOW])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
