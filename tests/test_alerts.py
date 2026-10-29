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
from unittest import mock

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")

# yahoo-<SYMBOL>.json: Yahoo chart answers in the range=1d interval=1d shape, cut from the shared
# recordings in tests/fixtures/_nse (2026-10-29 about 13:35 IST): meta from the 60d 5m file, the
# day's bar from the 6mo 1d file. yahoo-ZZZZ.json is Yahoo's answer for an unknown symbol.
QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(QDIR, "plan-reliance.json")   # trader/plan.json as trader-exit writes it
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; india-day-trader-skill/3.0)"

# NSE 2026 trading holidays on weekdays, from NSE's circular (tests/_nse.py)
NSE_2026 = ["2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
            "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
            "2026-12-25"]


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class Calendar(unittest.TestCase):
    def test_holidays_match_the_published_list(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        self.assertEqual(alerts.CALENDAR_YEARS, (2026,))
        self.assertFalse(hasattr(alerts, "NYSE_EARLY_CLOSE"))
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)

    def test_parse_now_is_india_time_all_year(self):
        self.assertEqual(ist("2026-10-29 09:15"), DT(2026, 10, 29, 3, 45))
        self.assertEqual(ist("2026-01-05T09:15"), DT(2026, 1, 5, 3, 45))      # no daylight saving
        self.assertEqual(ist("2026-10-29 09:15 IST"), DT(2026, 10, 29, 3, 45))
        self.assertEqual(ist("2026-10-29T08:05:17Z"), DT(2026, 10, 29, 8, 5, 17))
        with self.assertRaises(alerts.AlertError) as cm:
            ist("tomorrow")
        self.assertIn("India time", str(cm.exception))

    def test_a_trading_day(self):
        st = alerts.market_status(ist("2026-10-29 13:36"))
        self.assertEqual((st["open"], st["reason"], st["now_ist"]), (True, "regular session", "2026-10-29 13:36"))
        self.assertEqual((st["pre_opens"], st["opens"], st["closes"]), ("09:00", "09:15", "15:30"))
        self.assertNotIn("now_new_york", st)
        pre = alerts.market_status(ist("2026-10-29 09:05"))
        self.assertEqual((pre["open"], pre["pre_open"]), (False, True))
        self.assertIn("order entry 09:00 to 09:08", pre["reason"])
        self.assertEqual(pre["next_open"], "2026-10-29 09:15")
        self.assertEqual(alerts.market_status(ist("2026-10-29 08:59"))["reason"], "before the open")
        self.assertTrue(alerts.market_status(ist("2026-10-29 09:15"))["open"])
        self.assertTrue(alerts.market_status(ist("2026-10-29 15:29"))["open"])
        after = alerts.market_status(ist("2026-10-29 15:30"))
        self.assertEqual((after["open"], after["reason"], after["next_open"]), (False, "after the close", "2026-10-30 09:15"))

    def test_weekends_and_holidays(self):
        sat = alerts.market_status(ist("2026-10-31 10:00"))
        self.assertEqual((sat["open"], sat["reason"], sat["next_open"]), (False, "weekend", "2026-11-02 09:15"))
        dussehra = alerts.market_status(ist("2026-10-20 10:00"))
        self.assertEqual((dussehra["open"], dussehra["holiday"]), (False, "Dussehra"))
        self.assertEqual(dussehra["reason"], "NSE holiday: Dussehra")
        self.assertEqual(dussehra["next_open"], "2026-10-21 09:15")
        self.assertEqual(alerts.market_status(ist("2026-10-19 15:31"))["next_open"], "2026-10-21 09:15")
        xmas = alerts.market_status(ist("2026-12-24 16:00"))
        self.assertEqual(xmas["next_open"], "2026-12-28 09:15")      # Christmas Friday, then the weekend

    def test_muhurat_without_times_is_closed_and_says_why(self):
        st = alerts.market_status(ist("2026-11-08 18:30"))
        self.assertFalse(st["open"])
        self.assertEqual(st["reason"], "Muhurat trading day: session times not published yet")
        self.assertEqual(st["muhurat"], "times not published yet")
        self.assertEqual(alerts.market_status(ist("2026-11-06 16:00"))["next_open"], "2026-11-09 09:15")

    def test_muhurat_with_published_times(self):
        with mock.patch.dict(alerts.NSE_MUHURAT, {"2026-11-08": ("18:00", "19:00")}):
            st = alerts.market_status(ist("2026-11-08 18:30"))
            self.assertEqual((st["open"], st["reason"], st["opens"], st["closes"]),
                             (True, "Muhurat trading session", "18:00", "19:00"))
            pre = alerts.market_status(ist("2026-11-08 17:50"))
            self.assertEqual((pre["pre_open"], pre["pre_opens"]), (True, "17:45"))
            nxt = alerts.market_status(ist("2026-11-06 16:00"))
            self.assertEqual((nxt["next_open"], nxt["next_open_note"]), ("2026-11-08 18:00", "Muhurat trading"))

    def test_a_year_outside_the_holiday_list(self):
        st = alerts.market_status(ist("2027-01-04 10:00"))
        self.assertFalse(st["calendar_known"])
        self.assertTrue(st["open"])                                   # weekday rule only
        self.assertIn("holiday list unknown for 2027", st["reason"])
        dec = alerts.market_status(ist("2026-12-31 16:00"))
        self.assertTrue(dec["calendar_known"])
        self.assertEqual((dec["next_open"], dec["next_open_note"]), ("2027-01-01 09:15", "holiday list unknown for 2027"))


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "RELIANCE")
        self.assertEqual((q["symbol"], q["price"], q["prev_close"], q["open"]), ("RELIANCE", 1174.1, 1158.0, 1159.0))
        self.assertEqual((q["time"], q["time_ist"], q["currency"]),
                         ("2026-10-29T08:05:17Z", "2026-10-29 13:35:17", "INR"))
        with open(os.path.join(QDIR, "yahoo-MM.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "M&M.NS")
        self.assertEqual((q["symbol"], q["price"]), ("M&M", 2901.1))

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d")
        self.assertIn("/chart/M%26M.NS?", alerts.yahoo_url("M&M"))
        self.assertIn("/chart/BAJAJ-AUTO.NS?", alerts.yahoo_url("bajaj-auto"))
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("SBIN", alerts.YAHOO_HOSTS[1]))
        self.assertFalse(hasattr(alerts, "stooq_url"))

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["RELIANCE", "sbin", "M&M", "ZZZZ", "NOPE"], QDIR)
        self.assertEqual(sorted(q), ["M&M", "RELIANCE", "SBIN"])
        self.assertEqual(q["SBIN"]["price"], 964.25)
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])
        self.assertIn("yahoo-NOPE.json", errors[1])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-INFY.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-INFY.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["INFY"], d)
            self.assertEqual((q["INFY"]["price"], errors), (1139.2, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent"), req.full_url.split("/")[-1]))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _, _ in f.calls]

    def test_query2_first_with_the_contract_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual((q["RELIANCE"]["price"], errors), (1174.1, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA, "RELIANCE.NS?range=1d&interval=1d")])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1174.1)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("RELIANCE", alerts.get_quotes(["RELIANCE"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["RELIANCE"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_symbols(self):
        f = self.net([200, 200])
        alerts.get_quotes(["RELIANCE", "M&M"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])
        self.assertEqual(f.calls[1][2], "M%26M.NS?range=1d&interval=1d")


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["RELIANCE", "HDFCBANK", "SBIN", "INFY"], QDIR)
        self.now = ist("2026-10-29 13:36")
        self.open = alerts.market_status(self.now)

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "reliance-1170", "symbol": "RELIANCE", "type": "above", "price": 1170}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0]["message"], "RELIANCE is Rs 1,174.10: at or above your alert level Rs 1,170.00.")
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        closed = self.at("2026-10-29 16:00", rules)[0]
        self.assertIn("RELIANCE last Rs 1,174.10", closed[0]["message"])

    def test_stop_and_rearm(self):
        rules = [{"id": "hdfc-stop", "symbol": "HDFCBANK", "type": "stop", "price": 685, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("HDFCBANK is Rs 682.00: at or below your stop Rs 685.00", rings[0]["message"])
        self.q["HDFCBANK"]["price"] = 686.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("hdfc-stop", st["rung"])
        self.q["HDFCBANK"]["price"] = 684.5
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "rel-2", "symbol": "RELIANCE", "type": "pct_move", "pct": 2}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "rel-1", "symbol": "RELIANCE", "type": "pct_move", "pct": 1}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "RELIANCE moved +1.39% to Rs 1,174.10 (from Rs 1,158.00).")
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])      # once a day
        self.assertEqual(len(self.at("2026-10-30 10:00", small, st)[0]), 1)                    # and again tomorrow
        down = [{"id": "rel-down", "symbol": "RELIANCE", "type": "pct_move", "pct": 1, "direction": "down"}]
        self.assertEqual(alerts.evaluate(down, self.q, self.open, {}, self.now)[0], [])
        hdfc = [{"id": "hdfc-down", "symbol": "HDFCBANK", "type": "pct_move", "pct": 0.4, "direction": "down"}]
        rings, _ = alerts.evaluate(hdfc, self.q, self.open, {}, self.now)
        self.assertIn("HDFCBANK moved -0.48% to Rs 682.00 (from Rs 685.30)", rings[0]["message"])
        ref = [{"id": "infy-ref", "symbol": "INFY", "type": "pct_move", "pct": 3, "ref": 1100}]
        self.assertIn("+3.56%", alerts.evaluate(ref, self.q, self.open, {}, self.now)[0][0]["message"])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        self.assertEqual(self.at("2026-10-29 09:10", rules)[0], [])          # pre-open is not the bell
        rings, st = self.at("2026-10-29 09:15", rules)
        self.assertEqual(rings[0]["message"], "Opening bell: the NSE is open (2026-10-29 09:15 IST), the market "
                                              "closes at 15:30.")
        self.assertEqual(self.at("2026-10-29 11:00", rules, st)[0], [])
        self.assertEqual(self.at("2026-10-31 10:00", rules, st)[0], [])      # Saturday
        self.assertEqual(self.at("2026-11-10 10:00", rules, st)[0], [])      # Diwali Balipratipada
        self.assertEqual(len(self.at("2026-10-30 09:16", rules, st)[0]), 1)
        with mock.patch.dict(alerts.NSE_MUHURAT, {"2026-11-08": ("18:00", "19:00")}):
            rings, _ = self.at("2026-11-08 18:01", rules, st)
            self.assertEqual(rings[0]["message"], "Opening bell: Muhurat trading is open (18:00 to 19:00 IST).")

    def test_pre_open_rings_inside_its_window_only(self):
        rules = [{"id": "pre", "type": "pre_open"}]
        self.assertEqual(self.at("2026-10-29 08:59", rules)[0], [])
        rings, st = self.at("2026-10-29 09:00", rules)
        self.assertEqual(rings[0]["message"], "Pre-open: the NSE pre-open is on (2026-10-29 09:00 IST, pre-open "
                                              "(order entry 09:00 to 09:08, matching until the open)); the market "
                                              "opens at 09:15.")
        self.assertEqual(self.at("2026-10-29 09:10", rules, st)[0], [])
        self.assertEqual(self.at("2026-10-29 09:20", rules)[0], [])           # the window has passed
        self.assertEqual(self.at("2026-10-20 09:05", rules)[0], [])           # Dussehra

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("reliance-plan-stop", "stop"), ("reliance-plan-target", "target"),
                          ("reliance-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1163.3, 1185.3))
        self.assertEqual((rules[2]["at"], rules[2]["label"]), ("15:10", "flat by close"))
        self.assertNotIn("before_close", rules[2])
        self.assertTrue(all(r["date"] == "2026-10-29" for r in rules))
        rings, st = self.at("2026-10-29 13:36", rules)          # the real 13:35 quote, 1174.10
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1163.2
        rings, st = self.at("2026-10-29 14:00", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-stop"])
        self.assertEqual(rings[0]["message"], "RELIANCE is Rs 1,163.20: at or below your stop Rs 1,163.30 "
                                              "(plan stop: opening-range midpoint).")
        q["RELIANCE"]["price"] = 1185.3
        self.assertEqual([r["id"] for r in self.at("2026-10-29 14:30", rules, st, q)[0]], ["reliance-plan-target"])

    def test_the_flat_by_default_and_the_config(self):
        bare = {"symbol": "SBIN", "stop": 950, "target": 980}
        self.assertEqual(alerts.rules_from_plan(bare)[2]["at"], "15:10")
        self.assertEqual(alerts.rules_from_plan(bare, "15:00")[2]["at"], "15:00")
        self.assertEqual(alerts.rules_from_plan(dict(bare, symbol="m&m.ns"))[0]["id"], "m&m-plan-stop")

    def test_flat_by_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-29 15:09", rules)[0], [])
        rings, st = self.at("2026-10-29 15:10", rules)
        self.assertEqual(len(rings), 1)
        msg = rings[0]["message"]
        self.assertIn("Flat by close: it is 15:10 IST (set for 15:10), the market closes at 15:30.", msg)
        self.assertIn("trader-broker flatten --symbol RELIANCE", msg)
        self.assertIn("MIS position itself at about 15:20", msg)
        self.assertEqual(self.at("2026-10-29 15:20", rules, st)[0], [])
        rings, _ = self.at("2026-10-29 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-10-30 15:11", rules, st)[0], [])     # plan rules: their day only

    def test_a_plain_time_alert_and_the_old_before_close_field(self):
        rule = [{"id": "flat", "type": "time", "at": "15:10", "label": "square off", "before_close": True}]
        self.assertEqual(self.at("2026-10-29 12:10", rule)[0], [])          # no early-close shift any more
        rings, st = self.at("2026-10-29 15:10", rule)
        self.assertIn("Square off: it is 15:10 IST", rings[0]["message"])
        self.assertEqual(st["rung"]["flat"], "2026-10-29T09:40:00Z")
        self.assertEqual(self.at("2026-10-31 15:11", rule)[0], [])          # Saturday: no session
        self.assertEqual(self.at("2026-10-20 15:11", rule)[0], [])          # Dussehra
        self.assertEqual(self.at("2026-11-08 18:30", rule)[0], [])          # Muhurat, times unknown
        with mock.patch.dict(alerts.NSE_MUHURAT, {"2026-11-08": ("18:00", "19:00")}):
            self.assertEqual(self.at("2026-11-08 18:30", rule)[0], [])      # a day alert means nothing then
            evening = [{"id": "m", "type": "time", "at": "18:50", "label": "muhurat square off"}]
            self.assertIn("the market closes at 19:00", self.at("2026-11-08 18:50", evening)[0][0]["message"])

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("reliance-")], plan_rules())

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "SBIN"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "3pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1163.3})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "RELIANCE", "stop": 1163.3, "target": 1185.3, "flat_by": "3:10pm"})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_plan_trade_through_the_day(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)
        self.assertEqual(run(["add", "--rules", self.rules, "--pre-open"])[0], 0)

        code, text, _ = self.check("2026-10-29 09:02")
        self.assertEqual(code, 0)
        self.assertIn("NSE closed (2026-10-29 09:02 IST, pre-open", text)
        self.assertIn("ALERT Pre-open", text)
        code, text, _ = self.check("2026-10-29 09:16")
        self.assertIn("ALERT Opening bell: the NSE is open", text)
        self.assertNotIn("Pre-open", text)
        code, text, _ = self.check("2026-10-29 13:36")
        self.assertEqual(code, 1)
        self.assertIn("NSE OPEN (2026-10-29 13:36 IST, regular session).", text)
        self.assertIn("Nothing hit. 5 rule(s) watched today, RELIANCE Rs 1,174.10.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["prices"], doc["currency"], doc["source"]), ({"RELIANCE": 1174.1}, "INR", "yahoo"))
        self.assertEqual((doc["as_of"], doc["as_of_ist"]), ("2026-10-29T08:06:00Z", "2026-10-29 13:36:00"))
        self.assertEqual(doc["detail"]["RELIANCE"], {"prev_close": 1158.0, "time": "2026-10-29T08:05:17Z",
                                                     "time_ist": "2026-10-29 13:35:17", "source": "yahoo",
                                                     "fetched": "2026-10-29T08:06:00Z"})

        code, text, _ = self.check("2026-10-29 15:10")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close", text)
        self.assertIn("not an order", text)
        self.assertIn("get_positions", text)
        self.assertEqual(self.check("2026-10-29 15:12")[0], 1)
        code, text, _ = self.check("2026-10-30 09:15")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-30 10:05")
        self.assertIn("Nothing hit. 2 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "pre-open", "reliance-flat-by", "reliance-plan-stop",
                                       "reliance-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("reliance-flat-by", json.load(fh)["rung"])

    def test_a_watchlist_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "SBIN", "--above", "1000"], ["--symbol", "M&M", "--stop", "2800"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "sbin", "--above", "1000"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-10-29 18:00")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"SBIN": 964.25, "M&M": 2901.1})

        code, text, _ = self.check("2026-10-30 09:15")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-30 09:45")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertIn("m&m-stop-2800", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:10", "--label", "square off",
                             "--symbol", "reliance"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--target", "1185.3",
                             "--date", "2026-10-29"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1510", "type": "time", "at": "15:10", "label": "square off",
                                    "symbol": "RELIANCE"})
        self.assertEqual(rules[1], {"id": "reliance-target-1185.30", "symbol": "RELIANCE", "type": "target",
                                    "price": 1185.3, "date": "2026-10-29"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "3pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--at", "15:10", "--before-close"])
        self.assertEqual(code, 1)
        self.assertIn("NSE has no early closes", err)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "RELIANCE", "SBIN", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-29 13:36"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE     Rs 1,174.10 (+1.39% vs prev close Rs 1,158.00)  2026-10-29 13:35:17 IST", text)
        self.assertIn("delayed", text)
        self.assertNotIn("$", text)
        run(["quote", "INFY", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["INFY", "RELIANCE", "SBIN"])
        self.assertEqual(doc["detail"]["SBIN"]["prev_close"], 956.8)
        self.assertEqual(doc["detail"]["SBIN"]["fetched"], "2026-10-29T08:06:00Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"symbol": "HDFCBANK", "qty": 1}, {"ticker": "M&M.NS"}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["HDFCBANK", "M&M", "RELIANCE"])

    def test_clock(self):
        code, text, _ = run(["clock", "--now", "2026-10-29 10:15"])
        self.assertEqual(code, 0)
        self.assertIn("India 2026-10-29 10:15 IST (Thursday): NSE OPEN, regular session.", text)
        self.assertIn("Today's session: 09:15 to 15:30 IST (pre-open from 09:00).", text)
        code, text, _ = run(["clock", "--now", "2026-10-20 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("NSE holiday: Dussehra", text)
        self.assertIn("Next open: 2026-10-21 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2026-11-08 18:00"])
        self.assertIn("Muhurat trading day: session times not published yet", text)
        code, text, _ = run(["clock", "--now", "2027-03-01 10:00"])
        self.assertIn("Holiday list unknown for 2027", text)
        code, text, _ = run(["clock", "--now", "2026-10-29 13:36", "--json"])
        self.assertEqual(json.loads(text)["now_ist"], "2026-10-29 13:36")

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


if __name__ == "__main__":
    unittest.main()
