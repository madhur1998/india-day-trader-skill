"""trader-exit tests. The reel is RELIANCE on 2026-10-28 (see test_entry.py): the
9:40 bar closes 1,199.60 above the 1,187.00 to 1,194.80 range, trigger 9:45 AM.
Bars for the ATR stop come from tests/fixtures/_nse/chart-RELIANCE-60d-5m.json.gz."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

import _nse as nse
from _load import load, FIX, ROOT

exitp = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

RELIANCE = os.path.join(FIX, "_nse", "chart-RELIANCE-60d-5m.json.gz")

# trader/plan.json for the reel, exactly as the contract writes it
REEL_PLAN = {"created_by": "trader-exit", "symbol": "RELIANCE", "side": "buy", "date": "2026-10-28", "setup": "orb",
             "entry": 1199.6, "entry_time": "9:45 AM", "entry_bar": 5, "interval": "5m",
             "opening_range": {"high": 1194.8, "low": 1187.0, "minutes": 15},
             "stop": 1190.9, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
             "target": 1217.0, "target_r": 2.0, "target_label": "2R (two times the risk)",
             "risk_per_share": 8.7, "reward_risk": 2.0, "reward_risk_net": 1.62, "tick_size": 0.1,
             "charges_rates": nse.CHARGES, "flat_by": "15:10"}
REEL_ENTRY = {"created_by": "trader-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy", "date": "2026-10-28",
              "status": "triggered", "time": "9:45 AM", "bar_start": "09:40", "bar_index": 5, "price": 1199.6,
              "level": 1194.8, "interval": "5m", "opening_range": {"high": 1194.8, "low": 1187.0, "minutes": 15},
              "source": "yahoo", "checked_at": "2026-10-28T04:15:00Z"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1194.8, ol=1187.0, atr=None, n=None, tick=None, rates=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, tick=tick, rates=rates)


class Math(unittest.TestCase):
    def test_the_reel_or_mid_2r(self):
        p = c(1199.6, "or-mid")
        self.assertEqual(p["stop"], Decimal("1190.9"))            # (1194.80 + 1187.00) / 2, already on the tick
        self.assertEqual(p["risk"], Decimal("8.7"))
        self.assertEqual(p["target"], Decimal("1217.0"))
        self.assertEqual(p["reward_risk"], Decimal("2.00"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")
        self.assertNotIn("rounded", p["stop_detail"])
        self.assertEqual(p["tick"], Decimal("0.1"))
        # charges a share: about Rs 1.27 selling at the stop, Rs 1.28 at the target; net R:R (17.40 - 1.28) / (8.70 + 1.27)
        self.assertEqual((p["cost_stop"], p["cost_target"]), (Decimal("1.27"), Decimal("1.28")))
        self.assertEqual(p["reward_risk_net"], Decimal("1.62"))
        self.assertEqual(c(1199.6, "or-mid", "1R")["target"], Decimal("1208.3"))

    def test_stop_rounds_down_and_target_rounds_up_to_the_tick(self):
        p = c(1199.6, "or-mid", "1.5R", 1194.8, 1187.1)
        self.assertEqual(p["stop"], Decimal("1190.9"))            # 1190.95 down to the 0.10 tick
        self.assertIn("rounded down to the Rs 0.10 tick", p["stop_detail"])
        self.assertEqual(p["target"], Decimal("1212.7"))          # 1199.60 + 13.05 = 1212.65, up
        self.assertEqual(p["reward_risk"], Decimal("1.51"))
        self.assertEqual(c(1199.6, "price:1190.99")["stop"], Decimal("1190.9"))
        self.assertEqual(c(1199.6, "or-mid", "price:1215.01")["target"], Decimal("1215.1"))
        # tick 0.05 under Rs 1,000 (SBIN), 0.01 under Rs 250
        p = c(954.6, "or-mid", "2R", 954.0, 944.65)
        self.assertEqual((p["stop"], p["risk"], p["target"], p["tick"]),
                         (Decimal("949.3"), Decimal("5.3"), Decimal("965.2"), Decimal("0.05")))
        p = c(100.62, "pct:1", "1.5R", None, None)
        self.assertEqual((p["stop"], p["target"]), (Decimal("99.61"), Decimal("102.14")))   # 99.6138 down; 102.135 up
        # a tick pinned in the config wins
        self.assertEqual(c(1199.6, "or-mid", oh=1194.8, ol=1187.1, tick=0.05)["stop"], Decimal("1190.95"))

    def test_raw_yahoo_floats_land_on_the_grid(self):
        p = c(1199.5999755859375, "or-mid", "2R", 1194.800048828125, 1187.0)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("1199.6"), Decimal("1190.9"), Decimal("1217.0")))

    def test_other_stops(self):
        p = c(1199.6, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1187.0"), Decimal("12.6"), Decimal("1224.8")))
        p = c(1199.6, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1187.6"), Decimal("12.0"), Decimal("1223.6")))
        self.assertIn("1187.604, rounded down", p["stop_detail"])
        p = c(1199.6, "atr:1.5", atr=5.1, n=6)
        self.assertEqual(p["stop"], Decimal("1191.9"))              # 1199.60 - 7.65 = 1191.95, down
        self.assertEqual(p["stop_label"], "1.5 x ATR(6) under the entry")
        self.assertEqual(c(1199.6, "price:rs 1,190.90")["stop"], Decimal("1190.9"))

    def test_refusals_say_why(self):
        cases = [("price:1199.6", "2R", "at or above the entry"),
                 ("price:1200", "2R", "at or above the entry"),
                 ("or-mid", "price:1199", "at or below the entry"),
                 ("price:1199.1", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread"),
                 ("price:1195", "2R", "too tight to survive the charges")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1199.6, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        with self.assertRaises(exitp.PlanError):
            c(1180.0, "or-mid")                                     # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1199.6, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1199.6, "or-mid", oh=1187.0, ol=1194.8)

    def test_charges_over_a_quarter_of_the_risk_are_refused(self):
        # about Rs 1.27 a share round trip: Rs 5.10 of risk passes (24.9%), Rs 5.00 does not (25.4%)
        self.assertEqual(c(1199.6, "price:1194.5")["risk"], Decimal("5.1"))
        with self.assertRaises(exitp.PlanError) as cm:
            c(1199.6, "price:1194.6")
        msg = str(cm.exception)
        self.assertIn("round-trip charges of about Rs 1.27 a share", msg)
        self.assertIn("25.38% of the Rs 5.00 risk a share, more than 25%", msg)
        self.assertIn("Widen the stop to at least Rs 5.08 under the entry", msg)
        # a zero-brokerage broker in the config: the same stop now fits
        p = c(1199.6, "price:1195", rates={"brokerage_pct": 0})
        self.assertLess(p["cost_stop"], Decimal("0.6"))
        self.assertGreater(p["reward_risk_net"], Decimal("1.7"))

    def test_per_share_charges_match_charges_on_a_big_round_order(self):
        # 50 shares of Rs 1,199.60 stays under the Rs 20 brokerage cap; only the rupee rounding of STT and stamp differs
        rates = dict(nse.CHARGES)
        per = exitp.per_share_charges(Decimal("1199.6"), Decimal("1190.9"), rates)
        whole = nse.charges(50, 1199.6, 1190.9)
        self.assertLess(abs(float(per) * 50 - whole["total"]), 1.0)

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("RELIANCE", "5m", None, RELIANCE)
        day = exitp.datetime.date(2026, 10, 28)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=5)
        self.assertEqual(len(bars), 6)                              # 9:15 ... the 9:40 entry bar
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1194.8 - 1187.0, places=3)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 6)
        self.assertAlmostEqual(atr, sum(trs) / 6, places=9)
        self.assertAlmostEqual(atr, 5.1, places=3)
        # a whole session: the last 14 of its 75 five-minute bars (Yahoo sent 15:20 as nulls)
        full = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(full), 74)
        self.assertEqual(exitp.atr_of(full)[1], 14)
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.load_chart("SBIN", "5m", None, RELIANCE)
        self.assertIn("holds RELIANCE, not SBIN", str(cm.exception))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-28 09:46"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "RELIANCE", "--offline", RELIANCE, "--now", "2026-10-28 09:45", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Entry         Rs 1,199.60 at 9:45 AM (bar 5)", text)
        self.assertIn("Stop          Rs 1,190.90  opening-range midpoint: (Rs 1,194.80 + Rs 1,187.00) / 2 = 1190.9", text)
        self.assertIn("Target        Rs 1,217.00  2R (two times the risk)", text)
        self.assertIn("Risk a share  Rs 8.70 (0.73% of the entry)", text)
        self.assertIn("Reward:risk   2.0", text)
        self.assertIn("Charges       about Rs 1.27 a share round trip at the stop, Rs 1.28 at the target", text)
        self.assertIn("Net of them   reward:risk 1.62", text)
        self.assertIn("Flat by       15:10 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertNotIn("$", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        # downstream skills work out the rupees for their own quantity from the rates
        self.assertEqual(nse.charges(40, p["entry"], p["stop"], p["charges_rates"]),
                         nse.charges(40, 1199.6, 1190.9))
        code, text, _ = self.plan("--target", "1R", "--write")
        self.assertEqual(self.read_plan()["target"], 1208.3)
        self.assertEqual(self.read_plan()["target_r"], 1.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-28 10:00"])
        self.assertEqual(code, 0)
        self.assertIn("Rs 1,208.30", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-29 10:00"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "reliance", "--entry", "1199.6", "--or-high", "1194.8", "--or-low", "1187",
                                  "--time", "9:45", "--entry-bar", "5", "--date", "2026-10-28", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in REEL_PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", RELIANCE, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1191.9, 7.7, 1215.0))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 6))
        self.assertAlmostEqual(p["atr"], 5.1, places=3)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", RELIANCE, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1196", "--json")      # plan ahead at a price over the range high
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1196.0, 1190.9, None))
        code, _, err = self.plan("--entry", "1194.9")              # Rs 4.00 of risk: the charges eat it
        self.assertIn("too tight to survive the charges", err)

        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, setup="level", level=1199.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--stop", "price:1195")
        self.assertEqual(code, 1)
        self.assertIn("too tight to survive the charges", err)
        code, _, err = self.plan("--side", "sell")
        self.assertIn("longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--entry", "1199.6", "--or-high", "1194.8")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_config_flat_by_reward_risk_and_charges(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        cfg = os.path.join("trader", "config.json")
        self.write(cfg, {"flat_by": "15:00", "min_reward_risk": 1.8})
        code, text, _ = self.plan("--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("Reward:risk 1.62 net of charges is under your min_reward_risk 1.8", p["warnings"][0])
        self.write(cfg, {"min_reward_risk": 1.8, "count_charges": False})
        self.assertEqual(json.loads(self.plan("--json")[1])["warnings"], [])     # gross 2.0 is fine
        self.write(cfg, {"flat_by": "15:25"})
        p = json.loads(self.plan("--json")[1])
        self.assertIn("square off MIS positions", p["warnings"][0])
        self.write(cfg, {"charges": {"brokerage_pct": 0, "brokerage_max": 0}, "tick_size": 0.05})
        p = json.loads(self.plan("--json")[1])
        self.assertEqual(p["charges_rates"]["brokerage_pct"], 0)
        self.assertEqual(p["charges_rates"]["stt_sell_pct"], 0.025)    # the rest stay at the defaults
        self.assertEqual(p["tick_size"], 0.05)
        self.assertGreater(p["reward_risk_net"], 1.62)
        self.write(cfg, {"charges": {"brokerage": 0}})
        self.assertIn("unknown charge 'brokerage'", self.plan()[2])
        self.write(cfg, {"tick_size": 0})
        self.assertIn("tick_size", self.plan()[2])
        self.write(cfg, {"max_loss_per_trade_" + "usd": 50})
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("rename it max_loss_per_trade_inr", err)

    def test_flat_by_session_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), REEL_ENTRY)
        code, _, err = self.plan("--flat-by", "16:00")
        self.assertEqual(code, 1)
        self.assertIn("outside the 09:15 to 15:30 session", err)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:45"})
        self.assertIn("flat_by in the config 15:45 is outside", self.plan()[2])
        os.remove(os.path.join("trader", "config.json"))
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, time="3:12 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_holidays_and_muhurat(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, date="2026-10-20"))
        p = json.loads(self.plan("--json")[1])
        self.assertIn("The NSE is closed on 2026-10-20 (NSE holiday: Dussehra).", p["warnings"])
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, date="2026-11-08", time="6:30 PM"))
        p = json.loads(run(exitp, ["plan", "--now", "2026-11-08 18:31", "--json"])[1])
        self.assertIn("session times not published yet", p["warnings"][0])
        saved = dict(exitp.NSE_MUHURAT)
        exitp.NSE_MUHURAT["2026-11-08"] = ("18:00", "19:00")
        try:
            code, text, err = run(exitp, ["plan", "--now", "2026-11-08 18:31"])
            self.assertEqual(code, 0, err)
            self.assertIn("Flat by       18:40 IST", text)          # 15:10 is 20 minutes before 15:30
            self.assertIn("moved with the close of the Muhurat trading session (18:00 to 19:00)", text)
            code, _, err = run(exitp, ["plan", "--now", "2026-11-08 18:31", "--flat-by", "15:10"])
            self.assertIn("outside the 18:00 to 19:00 session", err)
        finally:
            exitp.NSE_MUHURAT.clear()
            exitp.NSE_MUHURAT.update(saved)

    def test_band_warning_passes_through(self):
        self.write(os.path.join("trader", "entry.json"), dict(REEL_ENTRY, band_warning="CIRCUIT: near the upper band."))
        code, text, _ = self.plan()
        self.assertEqual(code, 0)
        self.assertIn("Note: CIRCUIT: near the upper band.", text)

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/trader-timemachine", "/trader-budget", "longs only", "rs 1,190.90", "rs 1,217.00", "rs 8.70",
                     "1,212.70", "too tight to survive the spread", "too tight to survive the charges", "25%",
                     "charges_rates", "tick_size", "reward_risk_net", "15:10", "where's my stop", "set my sl",
                     "not financial advice"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", "nyse"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()
