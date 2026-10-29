import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reliance.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-reliance.json")
NOW = "2026-10-28 09:45"
TODAY = "2026-10-28"
BATCH = ["RELIANCE", "HDFCBANK", "INFY", "SBIN", "M&M"]
ENTRY_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
              "quantity": 46, "product": "MIS", "order_type": "LIMIT", "price": 1169.4, "validity": "DAY",
              "tag": "tds85567364"}
STOP_CALL = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
             "quantity": 46, "product": "MIS", "order_type": "SL-M", "trigger_price": 1160.0, "validity": "DAY",
             "tag": "tds85567364sl"}
REL_PLAIN = ("BUY 46 RELIANCE, LIMIT 1169.40, MIS intraday, good for today / stop leg: SELL 46 RELIANCE, "
             "SL-M trigger 1160.00, MIS intraday, good for today, placed once the entry fills / target 1188.20: "
             "watched by trader-alerts, not an order at Kite / max loss about Rs 489.22 if the stop fills at "
             "1160.00, charges included (it can fill worse in a fast market)")
SBIN = {"symbol": "SBIN", "side": "sell", "qty": 14,
        "kite": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "SBIN", "transaction_type": "SELL",
                 "quantity": 14, "product": "CNC", "order_type": "LIMIT", "price": 960.5, "validity": "DAY"}}


def rel(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


def json_after(text, marker):
    """The first JSON object printed after marker."""
    i = text.index("{", text.index(marker))
    depth = 0
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return json.loads(text[i:j + 1])
    raise AssertionError("no JSON after " + marker)


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=BATCH):
        return ticket.parse_yes(reply, order or rel(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 46 RELIANCE",
                  "yes 1169.40", "yes max loss 489.22", "yes target 1188.20", "yes trigger 1160"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "haan", "go", "do it", "yeah", "yep", "absolutely", "send it",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both", "yes HDFCBANK", "yes M&M",
                  "yes 50", "yes but make it 40", "yes?", "no", "not yes", "yes, wait", "yes if it is under 1170",
                  "yes, stop 1160"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        self.assertTrue(self.yes("yes sell 14 SBIN", SBIN))
        self.assertFalse(self.yes("yes sell 15", SBIN))
        self.assertTrue(self.yes("yes 960.50", SBIN))


class TicketShape(unittest.TestCase):
    def test_tool_input_is_the_kite_call(self):
        self.assertEqual(ticket.tool_input(rel()), ENTRY_CALL)
        self.assertEqual(ticket.stop_input(rel()), STOP_CALL)
        self.assertIsNone(ticket.stop_input(SBIN))
        self.assertEqual(ticket.tool_input(dict(SBIN, why="a split")), SBIN["kite"])
        self.assertEqual(ticket.tool_input(ENTRY_CALL), ENTRY_CALL)     # a bare Kite dict

    def test_why_plan_and_target_are_never_sent_or_fingerprinted(self):
        bare = {"kite": ENTRY_CALL, "stop_leg": STOP_CALL}
        self.assertEqual(ticket.fingerprint(rel()), ticket.fingerprint(bare))
        self.assertEqual(ticket.fingerprint(rel()), check.fingerprint(rel()))
        self.assertNotIn("why", json.dumps(ticket.tool_input(rel())))

    def test_plain_english(self):
        self.assertEqual(ticket.plain(rel()), REL_PLAIN)
        self.assertEqual(ticket.plain(SBIN), "SELL 14 SBIN, LIMIT 960.50, CNC delivery, good for today")
        self.assertEqual(ticket.max_loss(rel()), (489.22, 1160.0))
        other = rel()
        other["kite"] = dict(ENTRY_CALL, price=1168.0)
        want = round(46 * 8.0 + ticket.charges(46, 1168.0, 1160.0)["total"], 2)
        self.assertEqual(ticket.max_loss(other), (want, 1160.0))
        self.assertEqual(ticket.max_loss({"kite": ENTRY_CALL, "stop_leg": STOP_CALL})[1], 1160.0)


class DesktopConfig(unittest.TestCase):
    def test_hosted_read_mostly_and_a_self_hosted_alternative(self):
        with open(os.path.join(ROOT, "trader-broker", "claude_desktop_config.example.json")) as fh:
            doc = json.load(fh)
        hosted = doc["mcpServers"]["kite"]
        self.assertEqual((hosted["command"], hosted["args"]), ("npx", ["mcp-remote", "https://mcp.kite.trade/mcp"]))
        self.assertNotIn("env", hosted)
        own = doc["_self_hosted_alternative"]["kite"]
        for k in ("KITE_API_KEY", "KITE_API_SECRET"):
            self.assertIn("NEVER_IN_CHAT", own["env"][k])
        self.assertIn("README", " ".join(doc["_comment"]))


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.prices = os.path.join(self.d, "prices.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def paper(self, *extra, **kw):
        return self.args(*extra, **kw) + ["--prices", self.prices, "--portfolio", self.pf]

    def quote(self, price, utc):
        with open(self.prices, "w") as fh:
            json.dump({"as_of": utc, "currency": "INR", "prices": {"RELIANCE": price},
                       "detail": {"RELIANCE": {"time": utc}}}, fh)

    def set_cfg(self, **over):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def portfolio(self):
        with open(self.pf) as fh:
            return json.load(fh)

    def fill_entry(self, price=1169.0):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.quote(price, "2026-10-28T04:15:00Z")
        return run(self.paper("paper-fill", "--index", "1", now="2026-10-28 09:46"))

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_one_yes_covers_the_entry_and_its_stop_leg(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER (simulated here, nothing goes to Kite), Zerodha Kite, NSE", text)
        for part in REL_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertEqual(json_after(text, "place_order with exactly"), ENTRY_CALL)
        self.assertEqual(json_after(text, "the stop leg (covered by the same yes)"), STOP_CALL)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertIn('Reply "yes" to place this order and its stop leg', text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes both"))
        self.assertEqual(code, 1)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("Recorded your yes for order 1 and its stop leg", text)
        self.assertIn("ticket.py paper-fill --index 1", text)
        appr = self.state_doc()["orders"][0]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["stop_leg"], appr["date"]),
                         ("yes", "entry", 489.22, True, TODAY))
        self.assertEqual(appr["at"], "2026-10-28T09:45:00+05:30")
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

    def test_paper_fill_needs_a_fresh_price_and_an_open_market(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, _, err = run(self.paper("paper-fill", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("no prices file", err)
        self.quote(1169.0, "2026-10-28T03:50:00Z")                       # 09:20 IST, 25 minutes old
        code, _, err = run(self.paper("paper-fill", "--index", "1"))
        self.assertIn("older than 15 minutes", err)
        self.assertIn("alerts.py quote RELIANCE --write", err)
        self.quote(1169.0, "2026-10-28T10:05:00Z")
        code, _, err = run(self.paper("paper-fill", "--index", "1", now="2026-10-28 15:40"))
        self.assertIn("the market is closed", err)
        self.quote(1175.0, "2026-10-28T04:14:00Z")                       # above the limit: no fill yet
        code, text, _ = run(self.paper("paper-fill", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("Not filled: RELIANCE last 1175.00 is above your limit 1169.40", text)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved"])
        self.assertFalse(os.path.exists(self.pf))

    def test_paper_fill_then_the_target(self):
        code, text, _ = self.fill_entry(1169.0)
        self.assertEqual(code, 0, text)
        self.assertIn("PAPER fill: BUY 46 RELIANCE at 1169.00", text)
        self.assertIn("PAPER stop leg resting: SELL 46 RELIANCE, SL-M trigger 1160.00", text)
        pf = self.portfolio()
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"]), (46226.0, 46))
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled", "stop_placed"])
        self.assertEqual((st["orders"][1]["broker_order_id"], st["orders"][1]["max_loss"]),
                         ("PAPER-tds85567364", 489.22))
        self.assertEqual(st["paper_stops"][0]["trigger"], 1160.0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: stop_placed", text)

        # trader-risk now counts it: a second RELIANCE buy today is a duplicate, Rs 489.22 is used
        res = check.check([rel()], {"budget": 100000}, state=st, now=check.parse_now("2026-10-28 10:00"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 489.22))

        check_args = ["paper-check", "--config", self.cfg, "--state", self.state, "--prices", self.prices,
                      "--portfolio", self.pf]
        self.quote(1175.0, "2026-10-28T05:00:00Z")
        code, text, _ = run(check_args + ["--now", "2026-10-28 10:31"])
        self.assertIn("RELIANCE: last 1175.00 at 10:30 IST, stop 1160.00, target 1188.20: still open.", text)
        self.quote(1189.0, "2026-10-28T06:00:00Z")
        code, text, _ = run(check_args + ["--now", "2026-10-28 11:31"])
        self.assertEqual(code, 0)
        self.assertIn("PAPER TARGET HIT, SELL 46 at 1188.20 (last 1189.00)", text)
        self.assertEqual(self.portfolio(), {"cash": 100883.2, "currency": "INR", "positions": []})
        stop = self.state_doc()["paper_stops"][0]
        self.assertEqual((stop["status"], stop["fill"], stop["pnl_gross"]), ("target", 1188.2, 883.2))
        code, text, _ = run(check_args + ["--now", "2026-10-28 11:40"])
        self.assertIn("No open paper stops", text)

    def test_paper_stop_fills_at_the_trigger_or_worse(self):
        self.fill_entry(1169.0)
        check_args = ["paper-check", "--config", self.cfg, "--state", self.state, "--prices", self.prices,
                      "--portfolio", self.pf]
        self.quote(1155.5, "2026-10-28T05:00:00Z")                       # gapped through 1160
        code, text, _ = run(check_args + ["--now", "2026-10-28 10:31"])
        self.assertIn("PAPER STOP HIT, SELL 46 at 1155.50", text)
        self.assertEqual(self.portfolio()["cash"], round(46226.0 + 46 * 1155.5, 2))
        self.assertEqual(self.state_doc()["orders"][-1]["kind"], "exit")

    def test_paper_check_says_when_the_price_is_stale_or_it_is_past_flat_by(self):
        self.fill_entry(1169.0)
        check_args = ["paper-check", "--config", self.cfg, "--state", self.state, "--prices", self.prices,
                      "--portfolio", self.pf]
        code, text, _ = run(check_args + ["--now", "2026-10-28 10:31"])
        self.assertIn("older than 15 minutes", text)
        self.assertIn("still open", text)
        self.quote(1170.0, "2026-10-28T09:41:00Z")
        code, text, _ = run(check_args + ["--now", "2026-10-28 15:12"])
        self.assertIn("past your flat-by 15:10: flatten now", text)
        code, text, _ = run(check_args + ["--now", "2026-10-28 16:00"])
        self.assertIn("Nothing triggers now: the market is closed", text)

    def test_flatten_by_the_close_on_paper(self):
        self.fill_entry(1169.0)
        late = "2026-10-28 15:05"
        self.quote(1175.0, "2026-10-28T09:34:00Z")
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--prices", self.prices, "--plan", PLAN_PATH, "--config", self.cfg,
                             "--state", self.state, "--now", late])
        self.assertEqual(code, 0, text)
        self.assertIn("Flatten RELIANCE: SELL 46 RELIANCE, LIMIT 1173.80, MIS intraday, good for today "
                      "(flat by 15:10 IST)", text)
        self.assertIn("a little under the last 1175.00", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("paper stop PAPER-tds85567364sl (trigger 1160.00) is canceled when it fills", text)
        with open(self.orders) as fh:
            o = json.load(fh)["orders"][0]
        k = o["kite"]
        self.assertEqual({f: k[f] for f in k if f != "tag"},
                         {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                          "transaction_type": "SELL", "quantity": 46, "product": "MIS", "order_type": "LIMIT",
                          "price": 1173.8, "validity": "DAY"})
        self.assertRegex(k["tag"], r"^tdx[0-9a-f]{8}$")
        self.assertNotIn("stop_leg", o)
        self.assertEqual(self.run_risk(now=late), 0)        # after the 14:45 entry cutoff: an exit still passes
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 46 RELIANCE", now=late))[0], 0)
        code, text, _ = run(self.paper("paper-fill", "--index", "1", now=late))
        self.assertEqual(code, 0, text)
        self.assertIn("PAPER fill: SELL 46 RELIANCE at 1175.00", text)
        self.assertIn("canceled: the position is closed", text)
        self.assertEqual(self.portfolio()["positions"], [])
        self.assertEqual(self.state_doc()["paper_stops"][0]["status"], "canceled")
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty_market_or_no_fresh_price(self):
        base = ["flatten", "--symbol", "reliance", "--qty", "46", "--orders", self.orders, "--portfolio", self.none,
                "--plan", self.none, "--config", self.cfg, "--state", self.state, "--prices", self.prices,
                "--now", "2026-10-28 15:05"]
        code, _, err = run(base)
        self.assertEqual(code, 1)
        self.assertIn("--market", err)
        code, text, _ = run(base + ["--market"])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against get_positions", text)
        with open(self.orders) as fh:
            k = json.load(fh)["orders"][0]["kite"]
        self.assertEqual((k["order_type"], "price" in k), ("MARKET", False))
        code, text, _ = run(base + ["--limit", "1171.53"])
        with open(self.orders) as fh:
            self.assertEqual(json.load(fh)["orders"][0]["kite"]["price"], 1171.5)
        self.assertEqual(run(base[:4] + ["0", "--orders", self.orders])[0], 1)
        self.assertEqual(run(base + ["--market", "--limit", "1171.5"])[0], 1)

    def test_live_mode_shows_both_calls_and_records_kite_order_ids(self):
        self.set_cfg(paper=False)
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("LIVE ACCOUNT, REAL MONEY", text)
        self.assertIn("Claude will call place_order with exactly:", text)
        self.assertIn("Allow once", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertEqual(json_after(text, "Now call place_order with exactly:"), ENTRY_CALL)
        self.assertEqual(json_after(text, "covered by this same yes"), STOP_CALL)
        code, _, err = run(self.paper("paper-fill", "--index", "1"))
        self.assertIn("LIVE account", err)
        code, _, err = run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed"))
        self.assertIn("the stop leg follows the entry", err)
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--order-id",
                                      "251028000123456", "--filled-qty", "46", "--filled-price", "1169.40",
                                      "--portfolio", self.pf, now="2026-10-28 09:46"))
        self.assertEqual(code, 0)
        self.assertEqual(json_after(text, "place the stop leg"), STOP_CALL)
        code, text, _ = run(self.args("result", "--index", "1", "--leg", "stop", "--status", "placed",
                                      "--order-id", "251028000123460", now="2026-10-28 09:47"))
        self.assertEqual(code, 0)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled", "stop_placed"])
        self.assertEqual((st["orders"][1]["broker_order_id"], st["orders"][3]["broker_order_id"]),
                         ("251028000123456", "251028000123460"))
        self.assertEqual(self.portfolio()["cash"], 46207.6)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: stop_placed", text)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss Rs 489.22", text)
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--config", self.cfg, "--state", self.state, "--market", "--now", "2026-10-28 15:05"])
        self.assertIn('cancel_order with {"variety": "regular", "order_id": "251028000123460"}', text)
        self.assertIn("Cancel the resting stop leg FIRST", text)

    def test_a_failed_stop_leg_is_loud(self):
        self.set_cfg(paper=False)
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "placed", "--order-id", "1"))
        code, text, _ = run(self.args("result", "--index", "1", "--leg", "stop", "--status", "failed",
                                      "--note", "trigger outside the circuit"))
        self.assertEqual(code, 0)
        self.assertIn("has NO stop at the exchange", text)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "stop_failed")

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["stop_leg"]["trigger_price"] = 1159.0
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-27 09:45")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-28 09:20"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-28 09:20"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:30", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-28 09:20"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_a_dollar_config_is_an_error(self):
        self.set_cfg(max_loss_per_trade_usd=50)
        code, _, err = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("\"max_loss_per_trade_inr\"", err)

    def test_fill_sell_relieves_the_oldest_lot_in_whole_shares(self):
        pf = {"cash": 0, "positions": [{"ticker": "SBIN", "shares": 3, "lots": [
            {"id": "SBIN-1", "date": "2026-01-02", "shares": 2, "cost": 1800.0},
            {"id": "SBIN-2", "date": "2026-06-02", "shares": 1, "cost": 950.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        base = ["fill", "--symbol", "SBIN", "--side", "sell", "--price", "960", "--date", TODAY,
                "--portfolio", self.pf, "--config", self.cfg, "--state", self.state]
        code, _, err = run(base + ["--qty", "2.5"])
        self.assertEqual(code, 1)
        self.assertIn("whole shares", err)
        code, text, _ = run(base + ["--qty", "2"])
        self.assertEqual(code, 0)
        self.assertIn("Cash now Rs 1,920.00", text)
        lots = self.portfolio()["positions"][0]["lots"]
        self.assertEqual([(l["id"], l["shares"], l["cost"]) for l in lots], [("SBIN-2", 1, 950.0)])
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        code, _, err = run(base + ["--qty", "5"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
