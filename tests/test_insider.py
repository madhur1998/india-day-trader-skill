import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

ins = load("trader-insider", "insider")
DIR = os.path.join(FIX, "trader-insider")      # fresh synthetic feed + real files
NSE = os.path.join(FIX, "_nse")                # real files recorded 2026-10-29
SAMPLE = os.path.join(DIR, "IT_9000001_SAMPLE_28102026060500_WEB.xml")


def raw(folder, name):
    with open(os.path.join(folder, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(args)
    return code, out.getvalue(), err.getvalue()


class Xbrl(unittest.TestCase):
    def test_every_field_of_a_real_disclosure(self):
        f = ins.parse_xbrl(raw(NSE, "insider-IT_1197873_1662973_02052026044642_WEB.xml"),
                           "2026-05-02 16:46", "IT_1197873_1662973_02052026044642_WEB")
        self.assertEqual((f["symbol"], f["company"], f["isin"], f["regulation"]),
                         ("RELTD", "Ravindra Energy Limited", "INE206N01018", "7(2)"))
        self.assertEqual(len(f["rows"]), 1)
        r = f["rows"][0]
        self.assertEqual((r["category"], r["person"], r["instrument"]),
                         ("Director", "Shantanu Lath", "Equity Shares"))
        self.assertEqual((r["qty"], r["value"], r["price"], r["txn"]), (70000, 7000000.0, 100.0, "Buy"))
        self.assertEqual((r["pre_qty"], r["pre_pct"], r["post_qty"], r["post_pct"]),
                         (50000, 0.03, 120000, 0.07))
        self.assertEqual((r["from_date"], r["to_date"], r["intimated"]),
                         ("2026-04-29", "2026-04-29", "2026-05-02"))
        self.assertEqual((r["mode"], r["exchange"]), ("ESOP", None))  # "NA" in the file

    def test_days_to_disclose_skip_weekends_and_holidays(self):
        # traded Wed 29 Apr, told the company Sat 2 May: 1 May is Maharashtra Day
        r = ins.parse_xbrl(raw(NSE, "insider-IT_1197873_1662973_02052026044642_WEB.xml"),
                           "2026-05-02 16:46")["rows"][0]
        self.assertEqual((r["days_to_intimate"], r["trading_days_to_intimate"], r["days_to_public"]),
                         (3, 1, 3))
        self.assertFalse(r["late"])
        # a KMP sale on Fri 16 Oct told on Fri 23 Oct: four sessions (20 Oct is Dussehra)
        kmp = ins.parse_xbrl(raw(DIR, os.path.basename(SAMPLE)))["rows"][1]
        self.assertEqual(kmp["trading_days_to_intimate"], 4)
        self.assertTrue(kmp["late"])
        self.assertEqual(ins.trading_days_after("2026-04-30", "2026-05-01"), 0)

    def test_several_people_in_one_filing(self):
        f = ins.parse_xbrl(raw(DIR, os.path.basename(SAMPLE)))
        self.assertEqual([(r["category"], r["txn"], r["mode"]) for r in f["rows"]],
                         [("Promoter Group", "Buy", "Market Purchase"), ("KMP", "Sell", "Market Sale"),
                          ("Immediate relative", "Buy", "Gift")])
        self.assertIsNone(f["rows"][2]["value"])
        self.assertIsNone(f["rows"][2]["price"])
        self.assertEqual(f["rows"][1]["exchange"], "BSE")

    def test_pledge_and_a_trust_are_low_signal(self):
        mwl = ins.parse_xbrl(raw(NSE, "insider-IT_1197871_1662943_02052026022032_WEB.xml"))["rows"][0]
        self.assertEqual((mwl["category"], mwl["txn"], mwl["mode"], mwl["exchange"]),
                         ("Promoters", "Pledge", "Pledge Creation", "NSE"))
        self.assertEqual((mwl["from_date"], mwl["to_date"]), ("2026-04-28", "2026-04-29"))
        self.assertEqual(mwl["signal"], "low")
        self.assertIn("not a trade", mwl["why"])
        lal = ins.parse_xbrl(raw(NSE, "insider-IT_1197868_1662824_01052026041558_WEB.xml"))
        self.assertEqual(lal["symbol"], "LALPATHLAB")
        self.assertEqual(lal["rows"][0]["signal"], "low")  # an ESOP trust buying for employees

    def test_not_a_disclosure(self):
        with self.assertRaises(ins.InsiderError):
            ins.parse_xbrl(b"not xml")
        with self.assertRaises(ins.InsiderError):
            ins.parse_xbrl(b"<rss><channel/></rss>")


class Signal(unittest.TestCase):
    def row(self, cat, txn, mode):
        return ins.classify({"category": cat, "txn": txn, "mode": mode})

    def test_open_market_buys_by_promoters_and_directors_are_strong(self):
        for cat in ("Promoter", "Promoter Group", "Promoters", "Director", "KMP",
                    "Key Managerial Personnel"):
            self.assertEqual(self.row(cat, "Buy", "Market Purchase")[0], "strong", cat)
        self.assertEqual(self.row("Designated Person", "Buy", "Market Purchase")[0], "medium")
        self.assertEqual(self.row("Director", "Sell", "Market Sale")[0], "medium")
        self.assertEqual(self.row("Immediate relative", "Sell", "Market Sale")[0], "low")

    def test_paperwork_is_low(self):
        for mode in ("ESOP", "Gift", "Off Market", "Inter-se Transfer", "Pledge Creation",
                     "Invocation of pledge", "Preferential Offer", "Transmission"):
            self.assertEqual(self.row("Promoter", "Buy", mode)[0], "low", mode)
        for txn in ("Pledge", "Revoke", "Invoke"):
            self.assertEqual(self.row("Promoter", txn, "Market Purchase")[0], "low", txn)
        self.assertEqual(self.row("Promoter", "Buy", None)[0], "low")


class Deals(unittest.TestCase):
    def setUp(self):
        self.bulk = ins.parse_deals(raw(NSE, "bulk.csv"), "bulk")
        self.block = ins.parse_deals(raw(NSE, "block.csv"), "block")

    def test_bulk_rows_and_value(self):
        self.assertEqual(self.bulk["date"], "2026-10-28")
        self.assertEqual(len(self.bulk["deals"]), 57)
        ola = next(d for d in self.bulk["deals"] if d["symbol"] == "OLAELEC" and d["side"] == "buy")
        self.assertEqual((ola["client"], ola["qty"], ola["price"]),
                         ("JUMP TRADING FINANCIAL INDIA PRIVATE LIMITED", 24153106, 36.16))
        self.assertAlmostEqual(ola["value"], 873376312.96, places=2)
        self.assertEqual(ins.crore(ola["value"]), "Rs 87.34 cr")
        self.assertIsNone(ola["remarks"])  # "-"
        cps = [d["client"] for d in self.bulk["deals"] if d["symbol"] == "CPS"]
        self.assertEqual(cps, ["MANSI CHANDAK", "SHRUTI CHANDAK"])  # double spaces squashed

    def test_a_client_on_both_sides_is_a_round_trip(self):
        kaynes = [d for d in self.bulk["deals"] if d["symbol"] == "KAYNES"]
        self.assertTrue(all(d["round_trip"] for d in kaynes))
        asian = [d for d in self.bulk["deals"] if d["symbol"] == "ASIANTILES"]
        self.assertFalse(any(d["round_trip"] for d in asian))  # two different companies

    def test_block_buy_and_sell_pair_into_a_cross(self):
        self.assertEqual(len(self.block["deals"]), 2)
        crosses, rest = ins.pair_crosses(self.block["deals"])
        self.assertEqual(rest, [])
        c = crosses[0]
        self.assertEqual((c["symbol"], c["qty"], c["price"]), ("STLTECH", 601900, 887.0))
        self.assertEqual(c["seller"], "SERUM INSTITUTE OF INDIA PRIVATE LIMITED")
        self.assertEqual(c["buyer"], "POONAWALLA AVIATION PRIVATE LIMITED")
        self.assertEqual(ins.crore(c["value"]), "Rs 53.39 cr")

    def test_no_records_and_bad_files(self):
        empty = ins.parse_deals(b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,"
                                b"Trade Price / Wght. Avg. Price\nNO RECORDS,,,,,,\n", "block")
        self.assertEqual((empty["date"], empty["deals"]), (None, []))
        with self.assertRaises(ins.InsiderError):
            ins.parse_deals(b"<html>Access denied</html>\n", "bulk")


class Feed(unittest.TestCase):
    def test_real_feed_dedupes_and_is_called_stale(self):
        feed = ins.parse_rss(raw(NSE, "rss-Insider_Trading.xml"))
        self.assertEqual(feed["built"], "2026-10-29 13:30")
        self.assertEqual(len(feed["items"]), 12)  # 20 items, several listed twice
        self.assertEqual(feed["items"][0]["id"], "IT_1197873_1662973_02052026044642_WEB")
        self.assertEqual(feed["items"][0]["published"], "2026-05-02 16:46")
        code, out, _ = run(["list", "--offline", NSE, "--only", "insider", "--today", "2026-10-29",
                            "--trader-dir", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("looks stale", out)
        self.assertIn("180 days ago", out)
        self.assertIn("not read (IT_1197866", out)  # listed, but not recorded offline

    def test_empty_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "Insider_Trading.xml"), "w") as fh:
                fh.write("<rss version='2.0'><channel><title>x</title></channel></rss>")
            code, out, _ = run(["list", "--offline", tmp, "--today", "2026-10-29",
                                "--trader-dir", tmp])
            self.assertEqual(code, 1)
            self.assertIn("feed is empty", out)
            self.assertIn("bulk deals: bulk.csv is not in", out)  # the other sources say why
            self.assertIn("not early", out)


class CheckAndFilters(unittest.TestCase):
    def test_alerts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state", "insider.json")
            base = ["check", "--offline", DIR, "--state", state, "--today", "2026-10-29",
                    "--trader-dir", tmp]
            code, out, _ = run(base)
            self.assertEqual(code, 0)
            self.assertEqual(out.count("NEW SAMPLEIND"), 1)  # listed twice in the feed
            self.assertIn("STLTECH", out)
            with open(state) as fh:
                seen = json.load(fh)["seen"]
            self.assertIn("IT_9000001_SAMPLE_28102026060500_WEB", seen)
            self.assertNotIn("IT_9000002_MISSING_28102026050000_WEB", seen)  # unread: retried
            self.assertEqual(sum(k.startswith("block|") for k in seen), 2)
            code, out, _ = run(base)
            self.assertEqual(code, 1)
            self.assertIn("Nothing new", out)
            self.assertNotIn("NEW SAMPLEIND", out)
            self.assertNotIn("KAYNES", out)

    def test_dry_run_keeps_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "insider.json")
            run(["check", "--offline", DIR, "--state", state, "--dry-run", "--trader-dir", tmp])
            self.assertFalse(os.path.exists(state))

    def test_flags_your_holdings(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "portfolio.json"), "w") as fh:
                json.dump({"positions": [{"symbol": "KAYNES", "qty": 3}]}, fh)
            with open(os.path.join(tmp, "watchlist.json"), "w") as fh:
                json.dump({"setups": [{"symbol": "stltech.ns"}], "symbols": ["^NSEI"]}, fh)
            with open(os.path.join(tmp, "plan.json"), "w") as fh:
                json.dump({"symbol": "SAMPLEIND", "entry": 125}, fh)
            self.assertEqual(ins.my_symbols(tmp), {"KAYNES", "STLTECH", "SAMPLEIND"})
            code, out, _ = run(["list", "--offline", DIR, "--holdings", "--today", "2026-10-29",
                                "--trader-dir", tmp])
            self.assertEqual(code, 0)
            self.assertIn("KAYNES is on your list", out)
            self.assertIn("STLTECH is on your list", out)
            self.assertIn("SAMPLEIND is on your list", out)
            self.assertNotIn("OLAELEC", out)
            self.assertNotIn("RELTD", out)

    def test_filters(self):
        base = ["list", "--offline", DIR, "--today", "2026-10-29", "--trader-dir", "/nonexistent",
                "--json"]
        code, out, _ = run(base + ["--only", "insider", "--signal", "strong"])
        view = json.loads(out)
        rows = [r for f in view["insider"]["filings"] for r in f["rows"]]
        self.assertEqual([r["person"] for r in rows], ["Asha Sample Family Trust"])
        code, out, _ = run(base + ["--only", "bulk", "--min-value", "800000000"])
        self.assertEqual({d["symbol"] for d in json.loads(out)["bulk"]["deals"]},
                         {"KAYNES", "OLAELEC", "PGEL"})
        code, out, _ = run(base + ["--person", "graviton", "--only", "bulk"])
        self.assertEqual({d["symbol"] for d in json.loads(out)["bulk"]["deals"]}, {"KAYNES", "PGEL"})
        code, out, _ = run(base + ["--symbol", "smt", "--only", "bulk,block"])
        view = json.loads(out)
        self.assertEqual(len(view["bulk"]["deals"]), 6)
        self.assertEqual(view["block"]["crosses"], [])
        code, _, err = run(base + ["--only", "nasdaq"])
        self.assertEqual(code, 2)
        self.assertIn("--only", err)

    def test_filing_command_reads_one_file(self):
        code, out, _ = run(["filing", SAMPLE, "--published", "2026-10-28 18:05",
                            "--trader-dir", "/nonexistent", "--watch", "SAMPLEIND"])
        self.assertEqual(code, 0)
        self.assertIn("Promoter Group Asha Sample Family Trust: buy 2,50,000 Equity Shares "
                      "(Rs 3.12 cr at about Rs 125.00), Market Purchase on NSE", out)
        self.assertIn("[strong: open-market buy by Promoter Group]", out)
        self.assertIn("LATE: over 2", out)
        self.assertIn("SAMPLEIND is on your list", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)
        code, _, err = run(["filing", "https://www.nseindia.com/api/x"])
        self.assertEqual(code, 2)
        self.assertIn("nsearchives", err)


if __name__ == "__main__":
    unittest.main()
