import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("trader-budget", "size")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-reliance.json")
ORDERS = os.path.join(FIX, "trader-risk", "orders-reliance.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
CLOSES = os.path.join(FIX, "trader-budget", "prices-close-2026-10-28.json")
NOW = "2026-10-28 09:45"
TODAY = "2026-10-28"
# PLAN.md's config, key for key
CONTRACT_CFG = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 80000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10", "product": "MIS",
    "mis_leverage": 1, "count_charges": True, "charges": {}, "broker": "zerodha", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False,
}
# a long-term split, for the weights mode tests
SPLIT = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "min_order_inr": 500,
         "paper": True, "blocked": []}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def split(**over):
    c = dict(SPLIT)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def closes():
    with open(CLOSES) as fh:
        return json.load(fh)["prices"]


def rows(path):
    with open(path) as fh:
        return size.parse_weights(json.load(fh))


def held(result):
    return {p["ticker"]: p["shares"] for p in result["positions"]}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-10-28T04:22:13Z", currency="INR"):
    return {"as_of": quote_time, "source": "yahoo", "prices": {"RELIANCE": price},
            "detail": {"RELIANCE": {"prev_close": 1158.0, "time": quote_time, "currency": currency}}}


class TheReel(unittest.TestCase):
    """Rs 1,00,000, 10% buffer, Rs 500 max loss, RELIANCE risk 9.40 + charges -> 46 shares."""

    def test_config_example_is_the_contract(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)

    def test_reliance_is_46_shares_bound_by_the_max_loss_with_charges(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["order_type"]), (46, "max loss", "LIMIT"))
        self.assertEqual((s["entry"], s["stop"], s["target"], s["risk_per_share"]), (1169.4, 1160.0, 1188.2, 9.4))
        self.assertEqual(s["cost"], 53792.4)
        self.assertEqual(s["charges_at_stop"], size.charges(46, 1169.4, 1160.0)["total"])
        self.assertEqual((s["charges_at_stop"], s["max_loss"]), (56.82, 489.22))
        self.assertEqual((s["reward_risk"], s["reward_risk_gross"]), (1.65, 2.0))
        self.assertEqual(s["cash_buffer"], 10000)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 46), ("position cap", 64), ("max order", 68), ("cash", 76)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["max loss"])
        self.assertIsNone(s["refusal"])
        # one more share would cross the line once charges count
        self.assertGreater(47 * 9.4 + size.charges(47, 1169.4, 1160.0)["total"], 500)

    def test_the_kite_order(self):
        o = size.kite_order(size.size_trade(plan(), cfg(), today=TODAY))
        why = o.pop("why")
        tag = o["kite"]["tag"]
        self.assertEqual(o, {
            "symbol": "RELIANCE", "side": "buy", "qty": 46,
            "kite": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                     "transaction_type": "BUY", "quantity": 46, "product": "MIS", "order_type": "LIMIT",
                     "price": 1169.4, "validity": "DAY", "tag": tag},
            "stop_leg": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                         "transaction_type": "SELL", "quantity": 46, "product": "MIS", "order_type": "SL-M",
                         "trigger_price": 1160.0, "validity": "DAY", "tag": tag + "sl"},
            "target": 1188.2,
            "plan": {"entry": 1169.4, "stop": 1160.0, "target": 1188.2, "risk_per_share": 9.4,
                     "charges_at_stop": 56.82, "max_loss": 489.22, "setup": "orb", "entry_time": "9:45 AM",
                     "flat_by": "15:10"}})
        self.assertIsInstance(o["kite"]["quantity"], int)
        self.assertIsInstance(o["kite"]["price"], float)
        self.assertIn("bound by your Rs 500.00 max loss", why)
        self.assertIn("46 x 9.40 + Rs 56.82 charges = Rs 489.22", why)

    def test_the_tag_is_short_alphanumeric_and_deterministic(self):
        a = size.kite_order(size.size_trade(plan(), cfg(), today=TODAY))
        b = size.kite_order(size.size_trade(plan(), cfg(), today=TODAY))
        tag = a["kite"]["tag"]
        self.assertRegex(tag, r"^tds[0-9a-f]{8}$")
        self.assertEqual(tag, b["kite"]["tag"])
        self.assertLessEqual(len(a["stop_leg"]["tag"]), 20)
        self.assertTrue(a["stop_leg"]["tag"].isalnum())
        c = size.kite_order(size.size_trade(plan(), cfg(max_loss_per_trade_inr=400), today=TODAY))
        self.assertNotEqual(c["kite"]["tag"], tag)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(ORDERS) as fh:
            fixture = json.load(fh)["orders"][0]
        self.assertEqual(size.kite_order(size.size_trade(plan(), cfg(), today=TODAY)), fixture)

    def test_another_limit_and_a_market_order(self):
        s = size.size_trade(plan(), cfg(), limit=1167.0, today=TODAY)
        self.assertEqual((s["risk_per_share"], s["qty"]), (7.0, 61))
        self.assertEqual(size.kite_order(s)["kite"]["price"], 1167.0)
        s = size.size_trade(plan(), cfg(), limit=None, today=TODAY)
        k = size.kite_order(s)["kite"]
        self.assertEqual((k["order_type"], "price" in k), ("MARKET", False))
        self.assertEqual(s["qty"], 46)

    def test_charges_can_be_switched_off_or_changed(self):
        s = size.size_trade(plan(), cfg(count_charges=False), today=TODAY)
        self.assertEqual((s["qty"], s["max_loss"], s["charges_at_stop"], s["reward_risk"]), (53, 498.2, 0.0, 2.0))
        self.assertNotIn("charges", size.kite_order(s)["why"])
        free = size.size_trade(plan(charges_rates={"brokerage_max": 0}), cfg(), today=TODAY)
        self.assertEqual(free["charges_at_stop"], size.charges(free["qty"], 1169.4, 1160.0, {"brokerage_max": 0})["total"])
        self.assertGreater(free["qty"], 46)
        # the config overrides the plan's rates
        s = size.size_trade(plan(charges_rates={"brokerage_max": 0}), cfg(charges={"brokerage_max": 20}), today=TODAY)
        self.assertEqual(s["qty"], 46)

    def test_prices_go_on_the_tick_grid(self):
        s = size.size_trade(plan(entry=1169.43, stop=1160.07, target=1188.11), cfg(), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (1169.4, 1160.0, 1188.2))
        s = size.size_trade(plan(entry=245.0, stop=243.123, target=249.013, tick_size=None), cfg(), today=TODAY)
        self.assertEqual((s["stop"], s["target"]), (243.12, 249.01))

    def test_cli_writes_one_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 46 shares of RELIANCE = Rs 53,792.40 at 1169.40", text)
            self.assertIn("46 x 9.40 + Rs 56.82 round-trip charges = Rs 489.22", text)
            self.assertIn("stop leg  SELL 46 RELIANCE, SL-M trigger 1160.00, MIS", text)
            self.assertIn("<- binds", text)
            self.assertIn("India time", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["status"], "proposed")
            self.assertIs(doc["paper"], True)
            self.assertEqual(doc["created_at"], "2026-10-28T04:15:00Z")
            self.assertEqual(doc["source"], "trader/plan.json (RELIANCE orb 2026-10-28)")
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["plan"]["max_loss"], 489.22)

    def test_cli_json_limit_and_market(self):
        base = ["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW, "--json"]
        out = json.loads(run(base + ["--limit", "1168.5"])[1])
        self.assertEqual((out["sizing"]["entry"], out["orders"][0]["kite"]["price"]), (1168.5, 1168.5))
        out = json.loads(run(base + ["--market"])[1])
        self.assertEqual(out["orders"][0]["kite"]["order_type"], "MARKET")
        code, _, err = run(base + ["--market", "--limit", "1168.5"])
        self.assertEqual(code, 1)
        self.assertIn("pick one", err)


class Leverage(unittest.TestCase):
    WIDE = dict(max_loss_per_trade_inr=100000, max_position_pct=1000, max_order_inr=10 ** 7, min_reward_risk=0)

    def test_mis_leverage_counts_only_with_allow_margin(self):
        s = size.size_trade(plan(), cfg(**self.WIDE), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["leverage"]), (76, "cash", 1.0))
        s = size.size_trade(plan(), cfg(mis_leverage=5, **self.WIDE), today=TODAY)
        self.assertEqual(s["qty"], 76)
        s = size.size_trade(plan(), cfg(mis_leverage=5, allow_margin=True, **self.WIDE), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["buying_power"]), (384, "cash", 450000))
        self.assertIn("x 5 MIS leverage", s["limits"][-1]["how"])

    def test_no_leverage_for_delivery(self):
        s = size.size_trade(plan(), cfg(mis_leverage=5, allow_margin=True, product="CNC", **self.WIDE), today=TODAY)
        self.assertEqual(s["qty"], 76)
        self.assertEqual(size.kite_order(s)["kite"]["product"], "CNC")


class PlanRules(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(stop=1100.0), cfg(max_loss_per_trade_inr=60), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("one share risks Rs 70.29 (entry 1169.40 minus stop 1100.00, plus Rs 0.89 charges)", s["refusal"])
        self.assertIn("more than your Rs 60.00 max loss per trade", s["refusal"])
        self.assertIsNone(size.kite_order(s))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(plan(stop=1100.0), fh)
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(cfg(max_loss_per_trade_inr=60), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", c, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_each_limit_can_bind(self):
        s = size.size_trade(plan(), cfg(max_order_inr=20000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (17, "max order"))
        s = size.size_trade(plan(), cfg(max_position_pct=20), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (17, "position cap"))
        s = size.size_trade(plan(), cfg(), cash=30000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (17, "cash", 20000))

    def test_a_fresh_higher_price_means_a_bigger_risk_for_a_market_order(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=0), limit=None, price=1172.05, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1172.1, "price", 12.1))
        self.assertEqual(s["qty"], 37)
        s = size.size_trade(plan(), cfg(), limit=None, price=1180.0, today=TODAY)
        self.assertIn("reward:risk 0.33 net of charges (target 1188.20, entry 1180.00", s["refusal"])
        self.assertIn("net of charges", s["refusal"])
        self.assertIsNone(size.kite_order(s))
        lower = size.size_trade(plan(), cfg(), limit=None, price=1165.0, today=TODAY)
        self.assertEqual((lower["entry"], lower["qty"]), (1169.4, 46))

    def test_prices_file_is_used_only_when_fresh_and_in_rupees(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp,
                    "--json", "--market"]
            with open(pp, "w") as fh:
                json.dump(prices_doc(1172.05), fh)
            out = json.loads(run(base + ["--now", "2026-10-28 10:00"])[1])
            self.assertEqual((out["sizing"]["entry"], out["sizing"]["qty"]), (1172.1, 37))
            self.assertIn("above the plan's entry", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-10-28 10:30"])[1])
            self.assertEqual(out["sizing"]["qty"], 46)
            self.assertIn("not used", out["notes"][0])
            with open(pp, "w") as fh:
                json.dump(prices_doc(1172.05, currency="USD"), fh)
            out = json.loads(run(base + ["--now", "2026-10-28 10:00"])[1])
            self.assertEqual(out["sizing"]["entry"], 1169.4)
            # a LIMIT is sized at its price; a higher last price is only a note
            with open(pp, "w") as fh:
                json.dump(prices_doc(1172.05), fh)
            out = json.loads(run(base[:-1] + ["--now", "2026-10-28 10:00"])[1])
            self.assertEqual((out["sizing"]["entry"], out["sizing"]["qty"]), (1169.4, 46))
            self.assertIn("fills only if the price comes back", out["notes"][0])

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 30000, "positions": []}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (17, "cash"))

    def test_bad_plans(self):
        for p, word in ((plan(stop=1170.0), "not below the entry"),
                        (plan(target=1160.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) day trades only"),
                        (plan(symbol=""), "no symbol")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), limit=1169.4, today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), limit=None, price=1190.0, today=TODAY)
        self.assertIn("already happened", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["reliance"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-10-29")
        self.assertIn("make a new plan", str(e.exception))

    def test_dollar_keys_and_fractions_are_errors(self):
        for key, want in (("max_loss_per_trade_usd", "max_loss_per_trade_inr"), ("max_order_usd", "max_order_inr")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(plan(), cfg(**{key: 50}), today=TODAY)
            self.assertIn('"%s"' % want, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(fractional=True), today=TODAY)
        self.assertIn("whole shares", str(e.exception))
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump({"budget": 5000, "max_loss_per_trade_usd": 50}, fh)
            code, _, err = run(["--plan", PLAN, "--config", c, "--now", NOW])
            self.assertEqual(code, 1)
            self.assertIn("Rename it to \"max_loss_per_trade_inr\"", err)
            self.assertNotIn("Traceback", err)

    def test_under_the_minimum_order(self):
        cheap = plan(symbol="IDEA", entry=10.0, stop=9.5, target=11.0, tick_size=None, charges_rates=None)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_inr=5), today=TODAY)
        self.assertGreater(s["qty"], 0)
        self.assertIn("under your Rs 500.00 minimum order", s["refusal"])
        self.assertIsNone(size.kite_order(s))

    def test_missing_plan_and_weights_only_flags(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--top", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--weights", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertIn("--now wants an India time", err)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["--targets", TARGETS, "--config", CFG, "--cents"])
        self.assertEqual(code, 1)
        self.assertIn("no fractional shares", err)


class WeightsMode(unittest.TestCase):
    """v1's split, kept behind --weights / --targets: Rs 1,00,000, 10% cash, 25% cap, whole shares."""

    def test_the_example_split(self):
        r = size.size(rows(TARGETS), split(), prices=closes())
        self.assertEqual(held(r), {"RELIANCE": 21, "HDFCBANK": 33, "INFY": 16, "SBIN": 14, "M&M": 4})
        self.assertEqual((r["cash_buffer"], r["cap"]), (10000, 25000))
        self.assertEqual([p["ticker"] for p in r["positions"] if p["capped"]], ["RELIANCE"])
        for p in r["positions"]:
            self.assertAlmostEqual(p["rupees"], p["shares"] * closes()[p["ticker"]], places=2)
            self.assertLessEqual(p["rupees"], 25000)
        self.assertAlmostEqual(r["invested"] + r["idle"], 90000, places=2)
        self.assertLess(r["idle"], 2 * max(closes().values()))

    def test_from_inline_weights(self):
        w = size.parse_weights(size.weights_from_arg("RELIANCE=30,HDFCBANK=25,INFY=20,SBIN=15,M&M=10"))
        self.assertEqual(held(size.size(w, split(), prices=closes())), held(size.size(rows(TARGETS), split(),
                                                                                       prices=closes())))

    def test_orders_are_kite_shaped_delivery_limits(self):
        r = size.size(rows(TARGETS), split(), prices=closes())
        orders = size.orders_from(r, "an example")
        k = orders[0]["kite"]
        self.assertEqual({f: k[f] for f in k if f != "tag"},
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                          "transaction_type": "BUY", "quantity": 21, "product": "CNC", "order_type": "LIMIT",
                          "price": 1158.0, "validity": "DAY"})
        self.assertRegex(k["tag"], r"^[A-Za-z0-9]{1,20}$")
        self.assertIn("capped at 25%", orders[0]["why"])
        for o in orders:
            self.assertIsInstance(o["kite"]["quantity"], int)
            self.assertNotIn("stop_leg", o)
        self.assertEqual([o["symbol"] for o in orders], ["RELIANCE", "HDFCBANK", "INFY", "SBIN", "M&M"])

    def test_whole_shares_need_prices(self):
        with self.assertRaises(size.SizeError) as e:
            size.size(rows(TARGETS), split())
        self.assertIn("need a price", str(e.exception))

    def test_cap_redistributes_until_nobody_is_over(self):
        w = size.parse_weights({"A": 60, "B": 30, "C": 10})
        r = size.size(w, {"budget": 1000, "cash_buffer_pct": 0, "max_position_pct": 40, "min_order_inr": 1},
                      prices={"A": 1, "B": 1, "C": 1})
        self.assertEqual(held(r), {"A": 400, "B": 400, "C": 200})
        self.assertEqual(sorted(p["ticker"] for p in r["positions"] if p["capped"]), ["A", "B"])

    def test_everything_capped_leaves_money_idle(self):
        w = size.parse_weights({"RELIANCE": 50, "SBIN": 50})
        r = size.size(w, split(), prices={"RELIANCE": 1000.0, "SBIN": 1000.0})
        self.assertEqual(held(r), {"RELIANCE": 25, "SBIN": 25})
        self.assertEqual(r["idle"], 40000)
        self.assertIn("every stock is at its cap", size.report(r, size.orders_from(r)))

    def test_slices_under_the_minimum_are_dropped_and_shared_out(self):
        w = size.parse_weights({"A": 90, "B": 7, "C": 3})
        r = size.size(w, {"budget": 10000, "cash_buffer_pct": 10, "max_position_pct": 100, "min_order_inr": 500},
                      prices={"A": 1, "B": 1, "C": 1})
        self.assertNotIn("C", held(r))
        self.assertEqual(r["invested"], 9000)
        self.assertEqual(r["dropped"][0]["ticker"], "C")
        self.assertIn("under your Rs 500.00 minimum order", r["dropped"][0]["reason"])

    def test_a_share_dearer_than_its_slice_is_dropped(self):
        w = size.parse_weights({"RELIANCE": 95, "MRF": 5})
        r = size.size(w, split(max_position_pct=100), prices={"RELIANCE": 1158.0, "MRF": 140000.0})
        self.assertNotIn("MRF", held(r))
        self.assertIn("cannot be bought in whole shares", r["dropped"][0]["reason"])

    def test_blocked_tickers_are_dropped_first(self):
        r = size.size(rows(TARGETS), split(blocked=["sbin"]), prices=closes())
        self.assertNotIn("SBIN", held(r))
        self.assertIn("blocked", r["dropped"][0]["reason"])

    def test_top_n(self):
        r = size.size(rows(TARGETS), split(max_position_pct=100), prices=closes(), top=2)
        self.assertEqual(sorted(held(r)), ["HDFCBANK", "RELIANCE"])

    def test_no_budget_duplicates_and_dollar_keys_are_errors(self):
        with self.assertRaises(size.SizeError):
            size.size(rows(TARGETS), {"cash_buffer_pct": 10}, prices=closes())
        with self.assertRaises(size.SizeError):
            size.size([{"ticker": "SBIN", "name": "", "weight": 1}, {"ticker": "SBIN", "name": "", "weight": 2}],
                      split(), prices=closes())
        with self.assertRaises(size.SizeError) as e:
            size.size(rows(TARGETS), split(min_order_usd=5), prices=closes())
        self.assertIn("min_order_inr", str(e.exception))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(split(), fh)
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--targets", TARGETS, "--config", c, "--prices", CLOSES, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("left idle", text)
            self.assertIn("require_stop", text)
            self.assertIn("BUY 21 RELIANCE, LIMIT 1158.00, CNC, DAY", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual([o["symbol"] for o in doc["orders"]], ["RELIANCE", "HDFCBANK", "INFY", "SBIN", "M&M"])
            code, text, _ = run(["--targets", TARGETS, "--config", c, "--prices", CLOSES, "--json"])
            self.assertEqual(json.loads(text)["result"]["positions"][0]["shares"], 21)
            code, _, err = run(["--targets", "/nonexistent/targets.json", "--config", c])
            self.assertEqual(code, 1)
            self.assertIn("no weights file", err)


if __name__ == "__main__":
    unittest.main()
