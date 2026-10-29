import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-reliance.json")
SEC_LIST = os.path.join(FIX, "_nse", "sec_list.csv")
EQUITY_L = os.path.join(FIX, "_nse", "EQUITY_L.csv")
NOW = "2026-10-28 09:45"
TODAY = "2026-10-28"
QUOTE_TIME = datetime.datetime(2026, 10, 28, 4, 22, 13)   # a 09:52:13 IST quote
# the day-trade checks switched off: the account checks still hold
PLAIN = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_inr": 20000,
         "min_order_inr": 500, "max_orders_per_day": 10, "require_stop": False, "no_entries_before": None,
         "no_entries_after": None, "max_trades_per_day": 0, "max_daily_loss_inr": 0, "max_loss_per_trade_inr": 0,
         "min_reward_risk": 0, "product": "CNC"}
PRICES = {"RELIANCE": 1158.0, "HDFCBANK": 685.3, "INFY": 1123.15, "SBIN": 956.8, "M&M": 2869.9}


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def plain(**over):
    c = dict(PLAIN)
    c.update(over)
    return c


def rel(qty=None, kite=None, leg=None, **over):
    """The RELIANCE order from trader-budget, with changes."""
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    if qty is not None:
        o["qty"] = qty
        o["kite"]["quantity"] = qty
        o["stop_leg"]["quantity"] = qty
    o["kite"].update(kite or {})
    if leg is not None:
        o["stop_leg"].update(leg)
    for k, v in over.items():
        if v is None:
            o.pop(k, None)
        else:
            o[k] = v
    for d in (o["kite"], o.get("stop_leg") or {}):
        for k in [k for k, v in d.items() if v is None]:
            d.pop(k)
    return o


def entry(sym, qty, price, stop, target):
    """A minimal day-trade entry: LIMIT buy with an SL-M stop leg."""
    k = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "BUY",
         "quantity": qty, "product": "MIS", "order_type": "LIMIT", "price": price, "validity": "DAY"}
    leg = dict(k, transaction_type="SELL", order_type="SL-M", trigger_price=stop)
    leg.pop("price")
    return {"symbol": sym, "side": "buy", "qty": qty, "kite": k, "stop_leg": leg, "target": target,
            "plan": {"entry": price, "stop": stop, "target": target}}


def buy(sym, qty, price=None, product="CNC"):
    k = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "BUY",
         "quantity": qty, "product": product, "order_type": "LIMIT" if price else "MARKET", "validity": "DAY"}
    if price:
        k["price"] = price
    return {"symbol": sym, "side": "buy", "qty": qty, "kite": k}


def sell(sym, qty, price=None, product="MIS"):
    o = buy(sym, qty, price, product)
    o["side"] = "sell"
    o["kite"]["transaction_type"] = "SELL"
    return o


def placed(sym, side="buy", day=TODAY, max_loss=489.22, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T09:50:00+05:30"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TheReel(unittest.TestCase):
    def test_the_reliance_order_passes_every_check(self):
        res = check.check([rel()], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual((r["reasons"], r["warnings"]), ([], []))
        self.assertEqual((r["kind"], r["entry_ref"], r["entry_ref_from"], r["stop"], r["target"]),
                         ("entry", 1169.4, "limit", 1160.0, 1188.2))
        self.assertEqual((r["risk_per_share"], r["charges_at_stop"], r["max_loss"], r["reward_risk"]),
                         (9.4, 56.82, 489.22, 1.65))
        self.assertEqual(r["value_inr"], 53792.4)
        self.assertEqual(res["cash_after"], 46207.6)
        self.assertEqual((res["date"], res["now_ist"]), (TODAY, "2026-10-28 09:45"))
        self.assertIs(res["paper"], True)

    def test_47_shares_is_blocked_by_the_max_loss_with_charges(self):
        res = check.check([rel(qty=47)], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 47 shares x 9.40 risk a share + Rs 58.72 charges = Rs 500.52 at the stop, over "
                          "your Rs 500.00 max loss per trade"])
        self.assertEqual(verdicts(check.check([rel(qty=47)], cfg(count_charges=False), now=at(NOW))), ["PASS"])

    def test_reward_risk_is_net_of_charges(self):
        res = check.check([rel(target=1180.0)], cfg(), now=at(NOW))
        self.assertIn("reward:risk: 0.88 net of charges (target 1180.00, entry 1169.40, stop 1160.00)", reasons(res))
        res = check.check([rel(target=1180.0)], cfg(count_charges=False), now=at(NOW))
        self.assertEqual((verdicts(res), res["results"][0]["reward_risk"]), (["PASS"], 1.13))

    def test_fingerprint_covers_kite_and_stop_leg_only(self):
        o = rel()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={}, target=1200.0)), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(rel(qty=45)), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(rel(leg={"trigger_price": 1159.0})), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(rel(stop_leg=None)), check.fingerprint(o))
        self.assertEqual(check.KITE_FIELDS, ticket.KITE_FIELDS)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 buy RELIANCE 46 shares MIS LIMIT, entry", text)
            self.assertIn("entry 1169.40 (the limit price), stop 1160.00, target 1188.20: risk 9.40 a share", text)
            self.assertIn("max loss Rs 489.22 incl. Rs 56.82 charges (limit Rs 500.00), reward:risk 1.65 (min 1)",
                          text)
            self.assertIn("1 passed, 0 blocked at 2026-10-28 09:45 IST", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(rel()))


class KiteFormat(unittest.TestCase):
    def why(self, order, **over):
        return reasons(check.check([order], cfg(**over), now=at(NOW)))

    def test_entry_fields(self):
        cases = [({"variety": "bo"}, "variety must be regular or amo"),
                 ({"exchange": "BSE"}, "exchange must be NSE"),
                 ({"transaction_type": "buy"}, "transaction_type must be BUY or SELL"),
                 ({"product": "NRML"}, "product must be MIS (intraday) or CNC"),
                 ({"order_type": "SLM"}, "order_type must be MARKET, LIMIT, SL or SL-M"),
                 ({"price": "1169.40"}, "price must be a number (Kite takes numbers, not strings)"),
                 ({"price": 1169.43}, "price 1169.43 is off the tick grid (tick 0.1)"),
                 ({"price": None}, "a LIMIT order needs a price"),
                 ({"trigger_price": 1169.0}, "a LIMIT order takes no trigger_price"),
                 ({"order_type": "MARKET"}, "a MARKET order takes no price"),
                 ({"order_type": "SL"}, "an SL order needs both a price and a trigger_price"),
                 ({"order_type": "SL-M", "trigger_price": 1169.0}, "an SL-M order takes no price"),
                 ({"validity": "TTL"}, "validity must be DAY or IOC"),
                 ({"tag": "tds-8556"}, "tag must be 1 to 20 letters and digits"),
                 ({"tag": "t" * 21}, "tag must be 1 to 20 letters and digits")]
        for change, want in cases:
            self.assertIn("format: " + want, self.why(rel(kite=change)), change)
        for q in ("46", 46.0, 0, -5, True):
            o = rel()
            o["kite"]["quantity"] = q
            self.assertIn("quantity must be a positive whole number", self.why(o), q)

    def test_good_variants_pass(self):
        for change in ({"validity": "IOC"}, {"order_type": "SL", "trigger_price": 1169.0},
                       {"tag": None}, {"variety": "amo"}):
            res = check.check([rel(kite=change)], cfg(), now=at(NOW))
            self.assertEqual(verdicts(res), ["PASS"], (change, reasons(res)))
        res = check.check([rel(kite={"variety": "amo"})], cfg(), now=at(NOW))
        self.assertIn("after-market order", warnings(res))

    def test_unknown_fields_are_flagged(self):
        res = check.check([rel(kite={"squareoff": 10})], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("not a place_order field this pack knows: squareoff", warnings(res))

    def test_the_stop_leg(self):
        cases = [({"transaction_type": "BUY"}, "stop leg: must be a SELL to close a BUY"),
                 ({"order_type": "LIMIT", "trigger_price": None, "price": 1160.0},
                  "stop leg: order_type must be SL-M or SL"),
                 ({"quantity": 40}, "stop leg: quantity 40 is not the entry's 46"),
                 ({"product": "CNC"}, "stop leg: product 'CNC' is not the entry's 'MIS'"),
                 ({"tradingsymbol": "TCS"}, "stop leg: tradingsymbol 'TCS' is not the entry's 'RELIANCE'"),
                 ({"trigger_price": None}, "stop leg: an SL-M order needs a trigger_price"),
                 ({"order_type": "SL", "price": 1161.0}, "stop leg: a sell SL needs its price at or below")]
        for change, want in cases:
            self.assertIn("format: " + want, self.why(rel(leg=change)), change)
        self.assertIn("stop: the stop 1170.00 must be below the entry 1169.40",
                      self.why(rel(leg={"trigger_price": 1170.0})))
        res = check.check([rel(leg={"order_type": "SL", "price": 1159.0})], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))

    def test_wrapper_must_agree_with_kite(self):
        self.assertIn("the order says qty 40 but kite.quantity is 46", self.why(dict(rel(), qty=40)))
        self.assertIn("the order says TCS but kite.tradingsymbol is RELIANCE", self.why(dict(rel(), symbol="TCS")))
        self.assertIn("not a rupee amount", self.why(dict(rel(), notional=50000)))

    def test_a_bare_kite_dict_is_an_order_too(self):
        bare = rel()["kite"]
        res = check.check([bare], cfg(require_stop=False, min_reward_risk=0), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertEqual(res["results"][0]["fingerprint"], ticket.fingerprint(bare))


class Assets(unittest.TestCase):
    def test_futures_and_options_are_off_unless_allowed(self):
        for sym, exch in (("NIFTY26NOV24000CE", "NFO"), ("RELIANCE26NOVFUT", "NFO"),
                          ("BANKNIFTY2611352000PE", "NFO"), ("RELIANCE26NOVFUT", "NSE")):
            o = buy(sym, 75, 120.5, "NRML" if exch == "NFO" else "MIS")
            o["kite"]["exchange"] = exch
            r = check.check([o], plain(), prices={sym: 120.5}, today=TODAY)["results"][0]
            self.assertEqual(r["verdict"], "BLOCK", sym)
            self.assertIn("asset: %s is a futures or options contract" % sym, " ".join(r["reasons"]))
        o = buy("NIFTY26NOV24000CE", 75, 120.5, "NRML")
        o["kite"]["exchange"] = "NFO"
        r = check.check([o], plain(allow_options=True, max_position_pct=100), today=TODAY)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertIn("F&O order", " ".join(r["warnings"]))

    def test_junk_symbols(self):
        r = check.check([buy("BTC/INR", 1, 100.0)], plain(), today=TODAY)["results"][0]
        self.assertIn("does not look like an NSE equity symbol", " ".join(r["reasons"]))
        for sym in ("M&M", "BAJAJ-AUTO", "3MINDIA"):
            r = check.check([buy(sym, 1, 900.0)], plain(), today=TODAY)["results"][0]
            self.assertNotIn("asset:", " ".join(r["reasons"]), sym)

    def test_series_must_be_eq(self):
        series = check.load_series(SEC_LIST)
        self.assertEqual(series["RELIANCE"], {"series": "EQ", "band": None})
        self.assertEqual(series["ABAN"]["series"], "BE")
        self.assertEqual(verdicts(check.check([rel()], cfg(), now=at(NOW), series=series)), ["PASS"])
        r = check.check([entry("ABAN", 100, 30.5, 30.0, 31.5)], cfg(), now=at(NOW), series=series)
        self.assertIn("series: ABAN trades in the BE series (trade to trade", reasons(r))
        r = check.check([entry("ZZZZNOTREAL", 100, 30.5, 30.0, 31.5)], cfg(), now=at(NOW), series=series)
        self.assertIn("series: ZZZZNOTREAL is not in the NSE securities list", reasons(r))
        eq_l = check.load_series(EQUITY_L)
        self.assertEqual(eq_l["M&M"]["series"], "EQ")
        self.assertEqual(eq_l["3IINFOLTD"]["series"], "BE")
        with self.assertRaises(check.RiskError):
            check.load_series(ORDERS_PATH)

    def test_the_circuit_band(self):
        band5 = {"RELIANCE": {"series": "EQ", "band": 5.0}}
        res = check.check([rel()], cfg(), now=at(NOW), series=band5, prev_close={"RELIANCE": 1100.0})
        self.assertIn("circuit: the entry 1169.40 is outside RELIANCE's 5% band today (1045.00 to 1155.00",
                      reasons(res))
        res = check.check([rel()], cfg(), now=at(NOW), series=band5, prev_close={"RELIANCE": 1158.0})
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("a 5% price band", warnings(res))
        band20 = {"RELIANCE": {"series": "EQ", "band": 20.0}}
        res = check.check([rel()], cfg(), now=at(NOW), series=band20, prev_close={"RELIANCE": 1158.0})
        self.assertEqual((verdicts(res), res["results"][0]["warnings"]), (["PASS"], []))


class DayTradeChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_a_stop_is_required(self):
        o = rel(stop_leg=None, plan={"entry": 1169.4})
        self.assertIn("stop: no stop", reasons(self.one(o)))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        res = self.one(rel(stop_leg=None))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only, not at the exchange", warnings(res))
        self.assertEqual(res["results"][0]["max_loss"], 489.22)

    def test_target_on_the_right_side(self):
        res = self.one(rel(target=1169.0))
        self.assertIn("target: the target 1169.00 must be above the entry 1169.40", reasons(res))

    def test_a_fresh_higher_price_is_the_entry_of_a_market_order(self):
        mkt = rel(kite={"order_type": "MARKET", "price": None})
        later = at("2026-10-28 10:00")
        res = check.check([mkt], cfg(), prices={"RELIANCE": 1172.05}, now=later,
                          price_times={"RELIANCE": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1172.05, "price", 12.05))
        self.assertIn("loss: 46 shares x 12.05 risk a share", reasons(res))
        for now in ("2026-10-28 10:30", "2026-10-28 09:45"):     # stale, or from the future
            res = check.check([mkt], cfg(), prices={"RELIANCE": 1172.05}, now=at(now),
                              price_times={"RELIANCE": QUOTE_TIME})
            self.assertEqual((verdicts(res), res["results"][0]["entry_ref"]), (["PASS"], 1169.4), now)
            self.assertIn("older than 15 minutes", warnings(res))
        res = check.check([mkt], cfg(), prices={"RELIANCE": 1165.0}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 1169.4)

    def test_a_limit_order_uses_its_limit_price(self):
        res = check.check([rel(kite={"price": 1167.0})], cfg(), prices={"RELIANCE": 1172.05}, now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["risk_per_share"], r["verdict"]), (1167.0, 7.0, "PASS"))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("SBIN"), placed("INFY")]}
        res = check.check([rel()], cfg(), state=two, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, Rs 978.44 of Rs 1,500.00 daily loss",
                      res["results"][0]["info"][-1])
        three = {"orders": [placed("SBIN"), placed("INFY"), placed("HDFCBANK")]}
        why = reasons(check.check([rel()], cfg(), state=three, now=at(NOW)))
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: Rs 1,467.66 already at risk today plus Rs 489.22 for this one = Rs 1,956.88, "
                      "over your Rs 1,500.00", why)
        yesterday = {"orders": [placed(s, day="2026-10-27") for s in ("SBIN", "INFY", "HDFCBANK")]}
        self.assertEqual(verdicts(check.check([rel()], cfg(), state=yesterday, now=at(NOW))), ["PASS"])

    def test_exits_stops_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("SBIN"), placed("SBIN", side="sell", kind="exit", max_loss=None),
                         placed("SBIN", side="sell", kind="stop", status="stop_placed", max_loss=None),
                         placed("INFY", status="declined"), placed("HDFCBANK", status="failed")]}
        res = check.check([rel()], cfg(), state=st, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual((res["today"]["entries_placed"], res["today"]["orders_placed"]), (1, 2))

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [entry(s, 10, 500.0, 480.0, 560.0) for s in ("SBIN", "INFY", "ITC", "TCS")]
        res = check.check(orders, cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", why)

    def test_entry_window_india_time(self):
        cases = [("2026-10-28 09:29", "no new entries before 09:30 IST (it is 09:29)"),
                 ("2026-10-28 14:46", "no new entries after 14:45 IST (it is 14:46"),
                 ("2026-10-28 09:10", "the market is closed (Wed 2026-10-28 09:10 IST; the session is 09:15 to 15:30)"),
                 ("2026-10-28 15:31", "the market is closed"),
                 ("2026-10-31 10:15", "the market is closed today (weekend"),
                 ("2026-11-10 10:15", "the market is closed today (NSE holiday: Diwali Balipratipada"),
                 ("2026-11-08 18:15", "the market is closed today (Muhurat trading day: session times not published"),
                 ("2026-10-28T03:55:00Z", "no new entries before 09:30 IST (it is 09:25)")]
        for now, want in cases:
            self.assertIn("window: " + want, reasons(self.one(rel(), now=now)), now)
        for now in ("2026-10-28 09:30", "2026-10-28 14:45", "2026-10-28T04:00:00Z", "2026-10-28 12:00 IST"):
            self.assertEqual(verdicts(self.one(rel(), now=now)), ["PASS"], now)
        res = self.one(rel(), now="2026-10-31 10:15", no_entries_before=None, no_entries_after=None)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("holiday list covers 2026 only", " ".join(self.one(rel(), now="2027-01-04 10:00")["warnings"]))
        with self.assertRaises(check.RiskError):
            self.one(rel(), no_entries_before="9.30am")

    def test_the_window_uses_india_time_not_the_machine(self):
        self.assertEqual(check.to_ist(at("2026-10-28 09:45")), datetime.datetime(2026, 10, 28, 9, 45))
        self.assertEqual(at("2026-10-28 09:45"), datetime.datetime(2026, 10, 28, 4, 15))
        self.assertEqual(at("2026-12-01 09:45"), datetime.datetime(2026, 12, 1, 4, 15))   # no daylight saving

    def test_an_mis_short_is_blocked_unless_allowed(self):
        short = sell("RELIANCE", 46, 1169.4)
        short["stop_leg"] = dict(short["kite"], transaction_type="BUY", order_type="SL-M", trigger_price=1178.8)
        short["stop_leg"].pop("price")
        short["target"] = 1150.6
        res = self.one(short)
        self.assertIn("short: selling more RELIANCE than you hold (0 shares) is an intraday (MIS) short", reasons(res))
        res = self.one(short, allow_short=True)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["risk_per_share"]), ("PASS", "entry", 9.4), r["reasons"])
        res = self.one(dict(short, stop_leg=dict(short["stop_leg"], trigger_price=1160.0)), allow_short=True)
        self.assertIn("must be above the entry 1169.40 for a sell", reasons(res))

    def test_mis_leverage_only_with_allow_margin(self):
        pf = {"cash": 20000, "positions": []}
        wide = dict(max_position_pct=400, max_order_inr=100000)
        res = check.check([rel()], cfg(**wide), portfolio=pf, now=at(NOW))
        self.assertIn("cash: needs Rs 53,792.40 but only Rs 20,000.00 cash; that would be margin", reasons(res))
        res = check.check([rel()], cfg(mis_leverage=5, **wide), portfolio=pf, now=at(NOW))
        self.assertIn("that would be margin", reasons(res))
        res = check.check([rel()], cfg(allow_margin=True, mis_leverage=5, **wide), portfolio=pf, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))
        self.assertIn("margin: Rs 53,792.40 at 5x MIS leverage needs Rs 10,758.48", " ".join(res["results"][0]["info"]))
        self.assertEqual(res["cash_after"], 9241.52)
        res = check.check([rel()], cfg(allow_margin=True, mis_leverage=2, **wide), portfolio=pf, now=at(NOW))
        self.assertIn("cash: needs Rs 26,896.20 at 2x MIS leverage but only Rs 20,000.00", reasons(res))


class Exits(unittest.TestCase):
    PF = {"cash": 46207.6, "positions": [{"ticker": "RELIANCE", "shares": 46,
                                          "lots": [{"date": TODAY, "shares": 46, "cost": 53792.4}]}]}
    SELL = sell("RELIANCE", 46, 1174.8)

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("RELIANCE"), placed("SBIN"), placed("INFY")] +
                          [placed("T%d" % i, max_loss=1) for i in range(6)] +
                          [placed("RELIANCE", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["RELIANCE"], max_order_inr=1000, max_daily_loss_inr=50)
        res = check.check([self.SELL], c, portfolio=self.PF, state=full, now=at("2026-10-28 15:05"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:", "size:"):
            self.assertIn("exit, not blocked: " + word, why)
        res = check.check([self.SELL], cfg(), portfolio=self.PF, now=at("2026-10-31 12:00"))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_market_exit_needs_no_price(self):
        res = check.check([sell("RELIANCE", 46)], cfg(), portfolio=self.PF, now=at("2026-10-28 15:05"))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertNotIn("price:", warnings(res))

    def test_a_partial_exit_and_the_rest(self):
        half = sell("RELIANCE", 23, 1174.8)
        res = check.check([half, half], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate", warnings(res, 1))
        res = check.check([half, half, half], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 2))

    def test_format_asset_and_short_still_apply_to_a_sell(self):
        bad = sell("RELIANCE", 46, 1174.8)
        bad["kite"]["price"] = None
        bad["kite"].pop("price")
        res = check.check([bad], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("a LIMIT order needs a price", reasons(res))
        res = check.check([sell("RELIANCE", 47, 1174.8)], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling more RELIANCE than you hold (46 shares)", reasons(res))
        res = check.check([self.SELL], cfg(), now=at(NOW))
        self.assertIn("record the fill first", reasons(res))
        with_leg = dict(self.SELL, stop_leg=rel()["stop_leg"])
        res = check.check([with_leg], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("a sell has no stop leg", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([rel(), self.SELL], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class AccountChecks(unittest.TestCase):
    """The account checks still hold with the day-trade checks off."""

    def one(self, order, **over):
        return check.check([order], plain(**over), prices=PRICES, today=TODAY)["results"][0]

    def test_a_split_passes_on_a_fresh_account(self):
        orders = [buy("RELIANCE", 17, 1158.0), buy("HDFCBANK", 29, 685.3), buy("SBIN", 14, 956.8)]
        res = check.check(orders, plain(), today=TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 3, [r["reasons"] for r in res["results"]])
        self.assertEqual(res["results"][0]["value_inr"], 19686.0)

    def test_the_day_trade_config_blocks_buys_without_a_stop(self):
        res = check.check([buy("SBIN", 14, 956.8)], cfg(), now=at(NOW))
        self.assertIn("stop: no stop", reasons(res))

    def test_max_and_min_order(self):
        self.assertIn("over your Rs 20,000.00 max order", " ".join(self.one(buy("SBIN", 30, 956.8))["reasons"]))
        self.assertIn("under your Rs 500.00 minimum", " ".join(self.one(buy("IDEA", 10, 9.5))["reasons"]))

    def test_blocked_list(self):
        r = self.one(buy("SBIN", 10, 956.8), blocked=["sbin"])
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("blocked list", r["reasons"][0])

    def test_share_orders_need_a_price(self):
        r = self.one(buy("ZZZZ", 2))
        self.assertIn("no price for ZZZZ", " ".join(r["reasons"]))
        r = self.one(buy("SBIN", 2))
        self.assertEqual(r["verdict"], "PASS")
        self.assertAlmostEqual(r["value_inr"], 1913.6)

    def test_position_and_cash_buffer(self):
        r = check.check([buy("SBIN", 20, 956.8)], plain(max_order_inr=100000),
                        portfolio={"cash": 50000, "positions": []}, today=TODAY)["results"][0]
        self.assertIn("position: SBIN would be Rs 19,136.00", " ".join(r["reasons"]))
        r = check.check([buy("SBIN", 20, 956.8)], plain(max_order_inr=100000, max_position_pct=100),
                        portfolio={"cash": 20000, "positions": []}, today=TODAY)["results"][0]
        self.assertIn("under your 10% buffer", " ".join(r["reasons"]))

    PF = {"cash": 5000.0, "positions": [{"ticker": "SBIN", "shares": 14,
                                         "lots": [{"date": "2026-10-27", "shares": 14, "cost": 13230.0}]}]}

    def test_a_sell_frees_cash_for_a_later_buy(self):
        orders = [sell("SBIN", 10, 956.8, "CNC"), buy("HDFCBANK", 10, 685.3)]
        res = check.check(orders, plain(max_position_pct=100), portfolio=self.PF, prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        res = check.check([buy("HDFCBANK", 10, 685.3)], plain(max_position_pct=100), portfolio=self.PF,
                          prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["BLOCK"])

    def test_daily_limit_and_already_placed_today(self):
        st = {"orders": [{"symbol": "T%d" % i, "side": "buy", "status": "placed", "at": TODAY + "T10:00:00"}
                         for i in range(10)]}
        res = check.check([buy("SBIN", 1, 956.8)], plain(), state=st, today=TODAY)
        self.assertIn("daily limit", reasons(res))
        res = check.check([buy("SBIN", 1, 956.8)], plain(), state=st, today="2026-10-29")
        self.assertEqual(verdicts(res), ["PASS"])
        sb = {"orders": [{"symbol": "SBIN", "side": "buy", "status": "placed", "at": TODAY + "T10:00:00"}]}
        res = check.check([buy("SBIN", 1, 956.8)], plain(), state=sb, today=TODAY)
        self.assertIn("already placed today", reasons(res))
        declined = {"orders": [{"symbol": "SBIN", "side": "buy", "status": "declined", "at": TODAY + "T10:00:00"}]}
        self.assertEqual(verdicts(check.check([buy("SBIN", 1, 956.8)], plain(), state=declined, today=TODAY)),
                         ["PASS"])

    def test_live_account_warns_and_dollar_keys_are_errors(self):
        res = check.check([buy("SBIN", 1, 956.8)], plain(paper=False), today=TODAY)
        self.assertIn("LIVE", res["warnings"][0])
        with self.assertRaises(check.RiskError) as e:
            check.check([buy("SBIN", 1, 956.8)], plain(max_daily_loss_usd=150), today=TODAY)
        self.assertIn('"max_daily_loss_inr"', str(e.exception))


class Cli(unittest.TestCase):
    def test_prices_file_freshness_and_currency(self):
        with tempfile.TemporaryDirectory() as d:
            orders = os.path.join(d, "orders.json")
            with open(orders, "w") as fh:
                json.dump({"orders": [rel(kite={"order_type": "MARKET", "price": None})]}, fh)
            pp = os.path.join(d, "prices.json")
            none = os.path.join(d, "none")
            base = ["--orders", orders, "--config", CFG_PATH, "--portfolio", none, "--prices", pp,
                    "--state", none, "--no-write"]
            for cur, code_10_00 in (("INR", 1), ("USD", 0)):
                with open(pp, "w") as fh:
                    json.dump({"as_of": "2026-10-28T04:25:00Z", "currency": cur, "prices": {"RELIANCE": 1172.05},
                               "detail": {"RELIANCE": {"time": "2026-10-28T04:22:13Z",
                                                       "fetched": "2026-10-28T04:25:00Z"}}}, fh)
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(check.main(base + ["--now", "2026-10-28 10:00"]), code_10_00, cur)
                if cur == "INR":
                    self.assertIn("46 shares x 12.05 risk a share", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-10-28 10:30"]), 0)

    def test_series_flag(self):
        with tempfile.TemporaryDirectory() as d:
            orders = os.path.join(d, "orders.json")
            with open(orders, "w") as fh:
                json.dump({"orders": [rel(), entry("ABAN", 100, 30.5, 30.0, 31.5)]}, fh)
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", orders, "--config", CFG_PATH, "--portfolio", none, "--prices", none,
                                   "--state", none, "--now", NOW, "--series", SEC_LIST, "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("PASS  #1 buy RELIANCE", buf.getvalue())
            self.assertIn("series: ABAN trades in the BE series", buf.getvalue())

    def test_errors_are_one_line(self):
        for argv, want in ((["--orders", "/nonexistent.json"], "no orders file"),
                           (["--orders", ORDERS_PATH, "--now", "noon"], "--now wants"),
                           (["--orders", ORDERS_PATH, "--today", "monday"], "--today wants"),
                           (["--orders", ORDERS_PATH, "--series", "/nonexistent.csv"], "series file")):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(argv + ["--no-write"]), 1)
            self.assertIn(want, err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump({"budget": 5000, "max_loss_per_trade_usd": 50}, fh)
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(check.main(["--orders", ORDERS_PATH, "--config", c, "--now", NOW, "--no-write"]), 1)
            self.assertIn("Rename it to \"max_loss_per_trade_inr\"", err.getvalue())


if __name__ == "__main__":
    unittest.main()
