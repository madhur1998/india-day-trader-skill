#!/usr/bin/env python3
"""Insider and bulk/block deal tracker for trader-insider. Standard library only.

Reads three public NSE files: the insider trading disclosures companies file
under SEBI's insider trading rules (PIT), and the day's bulk and block deals.

    python3 insider.py check                          # only what is new since last check
    python3 insider.py list --symbol RELIANCE         # everything in the feeds for a stock
    python3 insider.py list --only insider --signal strong   # open-market promoter/director buys
    python3 insider.py list --only bulk,block --min-value 100000000   # deals of Rs 10 crore+
    python3 insider.py filing IT_1197873_1662973_02052026044642_WEB.xml   # read one disclosure

Where the data comes from (plain GETs to NSE's archive host, nothing else):
  - Insider feed: https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml
    Each <item> links to one XBRL disclosure, e.g.
    https://nsearchives.nseindia.com/corporate/xbrl/IT_1197873_1662973_02052026044642_WEB.xml
  - Bulk deals:  https://nsearchives.nseindia.com/content/equities/bulk.csv
  - Block deals: https://nsearchives.nseindia.com/content/equities/block.csv
    (the latest trading day's deals; NSE replaces the files every evening)
  - Never the www.nseindia.com/api JSON endpoints: they need a browser-cookie
    handshake, and this pack does not work around bot checks.

What a disclosure is, and is not:
  - SEBI PIT Regulation 7(2): a promoter, director, KMP or designated person
    who trades over Rs 10 lakh in a quarter tells the company within 2
    trading days, and the company tells the exchange within 2 trading days of
    that. So a filing is days old when it appears. Public, not early.
  - Reading them fast is not inside information. There is none here.

State: `check` remembers what it has shown in trader/state/insider.json, so
each filing and each deal alerts once. Offline: --offline DIR reads saved
copies (rss-Insider_Trading.xml or Insider_Trading.xml, IT_*.xml or
insider-IT_*.xml, bulk.csv, block.csv) instead of the network. The tests use it.
"""

import argparse
import csv
import datetime
import email.utils
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

RSS_URL = "https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml"
DEAL_URL = "https://nsearchives.nseindia.com/content/equities/%s.csv"
UA = "Mozilla/5.0 (compatible; india-day-trader-skill/3.0)"
PAUSE = 0.5          # seconds between requests to NSE: one file at a time
STALE_DAYS = 7       # newest insider item older than this: say the feed looks stale
DEALS_STALE_DAYS = 5
MAX_FILINGS = 25     # XBRL files read per run
SEEN_CAP = 5000
SOURCES = ("insider", "bulk", "block")
LEVELS = {"strong": 3, "medium": 2, "low": 1}
CRORE = 1e7


class InsiderError(Exception):
    pass


# ---------------------------------------------------------------- shared block (tests/_nse.py)

NSE_HOLIDAYS = {
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
NSE_MUHURAT = {
    "2026-11-08": None,  # Diwali Laxmi Pujan (Sunday); e.g. ("18:00", "19:00") once announced
}
IST = datetime.timedelta(hours=5, minutes=30)


def to_ist(utc):
    """Naive or aware UTC -> naive India time."""
    return utc.replace(tzinfo=None) + IST


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def trading_day(d):
    """(is_trading_day, reason). Weekends, the NSE holiday list and Muhurat."""
    key = d.isoformat()
    if key in NSE_MUHURAT:
        if NSE_MUHURAT[key]:
            return True, "Muhurat trading"
        return False, "Muhurat trading day: session times not published yet"
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(key)
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def inr(amount):
    """1234567.5 -> 'Rs 12,34,567.50' (Indian digit grouping)."""
    neg = amount < 0
    whole, frac = ("%.2f" % abs(amount)).split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return "%sRs %s.%s" % ("-" if neg else "", whole, frac)


def nse_symbol(sym):
    """RELIANCE.NS / reliance -> RELIANCE."""
    s = sym.strip().upper()
    return s[:-3] if s.endswith(".NS") else s


# ---------------------------------------------------------------- small helpers

MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def iso(text):
    """'2026-04-29', '29-04-2026', '28-OCT-2026' or '02-May-2026' -> '2026-04-29'."""
    t = (text or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{1,2})[-/ ]([A-Za-z]{3}|\d{1,2})[-/ ](\d{4})", t)
        if not m:
            return None
        d, y = int(m.group(1)), int(m.group(3))
        mo = MONTHS.get(m.group(2).upper()) if m.group(2).isalpha() else int(m.group(2))
    try:
        return datetime.date(y, mo or 0, d).isoformat()
    except ValueError:
        return None


def trading_days_after(start, end):
    """Trading days in (start, end]: how many sessions passed between a trade
    and its intimation. Weekends and NSE holidays do not count."""
    if not start or not end:
        return None
    d0, d1 = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    n, d = 0, d0
    while d < d1:
        d += datetime.timedelta(days=1)
        if trading_day(d)[0]:
            n += 1
    return n


def days_between(start, end):
    if not start or not end:
        return None
    return (datetime.date.fromisoformat(end[:10]) - datetime.date.fromisoformat(start[:10])).days


def grp(n):
    """1234567 -> '12,34,567'."""
    return inr(n)[3:-3] if n is not None else "?"


def crore(v):
    """87341231.0 -> 'Rs 8.73 cr'; under a crore in plain rupees."""
    if v is None:
        return "value not stated"
    return inr(v / CRORE) + " cr" if v >= CRORE else inr(v)


def squash(text):
    return " ".join((text or "").split())


def today_ist():
    return to_ist(utc_now()).date()


# ---------------------------------------------------------------- network

class Fetcher:
    """One request at a time with a pause between them, or files from a folder."""

    def __init__(self, offline=None, pause=PAUSE):
        self.offline = offline
        self.pause = pause
        self.last = 0.0

    def get(self, url, names=()):
        if self.offline:
            base = url.rsplit("/", 1)[-1]
            for name in tuple(names) + (base, "insider-" + base, "rss-" + base):
                path = os.path.join(self.offline, name)
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        return fh.read()
            raise InsiderError("%s is not in %s" % (base, self.offline))
        gap = time.time() - self.last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*",
                                                   "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise InsiderError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise InsiderError("could not reach %s: %s" % (url, exc.reason))
        finally:
            self.last = time.time()


# ---------------------------------------------------------------- the insider feed

def parse_rss(data):
    """The Insider_Trading.xml feed -> {"built", "items": [...]}, newest first.
    NSE lists some filings twice: one item per XBRL link."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise InsiderError("insider feed is not valid XML: %s" % exc)
    built = None
    lb = root.findtext("channel/lastBuildDate")
    if lb:
        try:
            dt = email.utils.parsedate_to_datetime(lb)
            built = to_ist(dt - dt.utcoffset()).isoformat(" ", "minutes")
        except (TypeError, ValueError):
            built = None
    items, seen = [], set()
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        if not link or link in seen:
            continue
        seen.add(link)
        pub = (it.findtext("pubDate") or "").strip()
        m = re.match(r"^(\S+)\s+(\d{1,2}:\d{2})", pub)
        day = iso(m.group(1) if m else pub)
        published = "%s %s" % (day, m.group(2).zfill(5)) if (day and m) else day
        items.append({"id": filing_id(link), "company": squash(it.findtext("title")),
                      "url": link, "about": squash(it.findtext("description")),
                      "published": published})
    items.sort(key=lambda r: (r["published"] or "", r["id"]), reverse=True)
    return {"built": built, "items": items}


def filing_id(link):
    base = link.rsplit("/", 1)[-1]
    if base.startswith("insider-"):
        base = base[len("insider-"):]
    return base[:-4] if base.lower().endswith(".xml") else base


def _num(text, kind=float):
    t = (text or "").replace(",", "").strip()
    if not t or t.upper() in ("NA", "NIL", "-"):
        return None
    try:
        return kind(float(t)) if kind is int else float(t)
    except ValueError:
        return None


# XBRL element (local name) -> row key. "Acquistion" is NSE's own spelling.
FIELDS = {
    "CategoryOfPerson": "category",
    "NameOfThePerson": "person",
    "TypeOfInstrument": "instrument",
    "SecuritiesAcquiredOrDisposedNumberOfSecurity": "qty",
    "SecuritiesAcquiredOrDisposedValueOfSecurity": "value",
    "SecuritiesAcquiredOrDisposedTransactionType": "txn",
    "SecuritiesHeldPriorToAcquisitionOrDisposalNumberOfSecurity": "pre_qty",
    "SecuritiesHeldPriorToAcquisitionOrDisposalPercentageOfShareholding": "pre_pct",
    "SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity": "post_qty",
    "SecuritiesHeldPostAcquistionOrDisposalPercentageOfShareholding": "post_pct",
    "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate": "from_date",
    "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate": "to_date",
    "DateOfIntimationToCompany": "intimated",
    "ModeOfAcquisitionOrDisposal": "mode",
    "ExchangeOnWhichTheTradeWasExecuted": "exchange",
}
INTS = ("qty", "pre_qty", "post_qty")
FLOATS = ("value", "pre_pct", "post_pct")
DATES = ("from_date", "to_date", "intimated")
COMPANY = {"NSESymbol": "symbol", "NameOfTheCompany": "company", "ISIN": "isin",
           "RegulationOfInsiderTrading": "regulation"}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xbrl(data, published=None, fid=None):
    """One IT_*.xml disclosure -> {"id", "symbol", "company", "isin",
    "regulation", "published", "rows": [...]}. One row per person (one per
    ChangeInHoldingOfSecurities context): category, person, instrument, qty,
    value, price, txn, pre/post qty and %, from_date, to_date, intimated,
    mode, exchange, trading_days_to_intimate, days_to_intimate,
    days_to_public, late, signal, why."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise InsiderError("disclosure is not valid XML: %s" % exc)
    head = {"id": fid, "symbol": None, "company": None, "isin": None, "regulation": None,
            "published": published}
    by_ctx = {}
    for el in root.iter():
        ctx = el.get("contextRef")
        if not ctx:
            continue
        name, text = _local(el.tag), squash(el.text)
        if name in COMPANY:
            head[COMPANY[name]] = text or None
        elif name in FIELDS:
            by_ctx.setdefault(ctx, {})[FIELDS[name]] = text
    if not head["symbol"] and not by_ctx:
        raise InsiderError("no insider trading facts in this file (not an IT_*.xml disclosure?)")
    rows = []
    for ctx in sorted(by_ctx):
        f = by_ctx[ctx]
        row = {k: None for k in FIELDS.values()}
        row.update({k: (v or None) for k, v in f.items()})
        for k in INTS:
            row[k] = _num(f.get(k), int)
        for k in FLOATS:
            row[k] = _num(f.get(k))
        for k in DATES:
            row[k] = iso(f.get(k))
        if row["exchange"] in ("NA", "N.A.", "-"):
            row["exchange"] = None
        row["context"] = ctx
        rows.append(_finish(row, head))
    head["rows"] = rows
    return head


def _finish(row, head):
    row["symbol"] = head["symbol"]
    row["company"] = head["company"]
    row["filing"] = head["id"]
    row["price"] = round(row["value"] / row["qty"], 2) if row["value"] and row["qty"] else None
    traded = row["to_date"] or row["from_date"]
    row["days_to_intimate"] = days_between(traded, row["intimated"])
    row["trading_days_to_intimate"] = trading_days_after(traded, row["intimated"])
    row["late"] = (row["trading_days_to_intimate"] or 0) > 2
    row["days_to_public"] = days_between(traded, head.get("published"))
    row["signal"], row["why"] = classify(row)
    return row


# ---------------------------------------------------------------- what is a signal

CORE = ("promoter", "director", "kmp", "key managerial", "managing", "chairman", "ceo", "cfo")
LOW_MODES = ("esop", "gift", "pledge", "invocation", "invoke", "revoc", "revoke", "off market",
             "off-market", "inter-se", "inter se", "transmission", "bonus", "rights",
             "allotment", "preferential", "buy back", "buyback", "conversion", "scheme",
             "inherit", "encumbrance", "creation", "release", "warrant")
MARKET_MODES = ("market purchase", "market sale", "open market", "on market", "market")


def classify(row):
    """(signal, why). strong: an open-market buy by a promoter, director or
    KMP, people putting their own money in at the market price. low: ESOPs,
    gifts, pledges, transfers: paperwork, not a view on the price."""
    txn = (row.get("txn") or "").strip().lower()
    mode = (row.get("mode") or "").strip().lower()
    cat = (row.get("category") or "").strip().lower()
    who = row.get("category") or "insider"
    if txn not in ("buy", "sell"):
        return "low", "%s, not a trade" % (txn or "transaction type not stated")
    if any(k in mode for k in LOW_MODES):
        return "low", "%s, not an open-market %s" % (row["mode"], "buy" if txn == "buy" else "sale")
    if not any(k in mode for k in MARKET_MODES):
        return "low", "mode %s: read the filing" % (row.get("mode") or "not stated")
    core = any(k in cat for k in CORE)
    if txn == "buy":
        if core:
            return "strong", "open-market buy by %s" % who
        return "medium", "open-market buy by %s" % who
    if core:
        return "medium", "open-market sale by %s (people sell for many reasons)" % who
    return "low", "open-market sale by %s" % who


# ---------------------------------------------------------------- bulk and block deals

def parse_deals(data, kind):
    """bulk.csv / block.csv -> {"kind", "date", "deals": [...]}. Columns:
    Date, Symbol, Security Name, Client Name, Buy/Sell, Quantity Traded,
    Trade Price / Wght. Avg. Price[, Remarks]."""
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
    reader = csv.reader(io.StringIO(text))
    header = None
    deals = []
    for rec in reader:
        if not any(c.strip() for c in rec):
            continue
        if header is None:
            header = [squash(c).lower() for c in rec]
            if "symbol" not in header:
                raise InsiderError("%s.csv has no Symbol column (got: %s)" % (kind, ", ".join(rec)))
            continue
        r = dict(zip(header, [c.strip() for c in rec]))
        qty = _num(r.get("quantity traded"), int)
        price = _num(r.get("trade price / wght. avg. price"))
        side = (r.get("buy/sell") or "").strip().lower()
        if not r.get("symbol") or qty is None or side not in ("buy", "sell"):
            continue  # "NO RECORDS" lines and the like
        remarks = r.get("remarks")
        d = {"kind": kind, "date": iso(r.get("date")), "symbol": r["symbol"].upper(),
             "security": squash(r.get("security name")), "client": squash(r.get("client name")),
             "side": side, "qty": qty, "price": price,
             "value": round(qty * price, 2) if price is not None else None,
             "remarks": remarks if remarks and remarks != "-" else None}
        d["key"] = "%s|%s|%s|%s|%s|%s|%s" % (kind, d["date"], d["symbol"], d["client"], side,
                                             qty, price)
        deals.append(d)
    _round_trips(deals)
    dates = sorted({d["date"] for d in deals if d["date"]})
    return {"kind": kind, "date": dates[-1] if dates else None, "deals": deals}


def _round_trips(deals):
    """A client on both sides of the same stock the same day is trading
    around it (often a market maker or an arbitrage desk), not building a stake."""
    sides = {}
    for d in deals:
        sides.setdefault((d["date"], d["symbol"], d["client"]), set()).add(d["side"])
    for d in deals:
        d["round_trip"] = len(sides[(d["date"], d["symbol"], d["client"])]) == 2


def pair_crosses(deals):
    """Block deals where a buy and a sell match on stock and quantity: one
    seller handing a block to one buyer (a cross). Returns (crosses, rest)."""
    buys = [d for d in deals if d["side"] == "buy"]
    sells = [d for d in deals if d["side"] == "sell"]
    crosses, used = [], set()
    for b in buys:
        for s in sells:
            if id(s) in used or (s["date"], s["symbol"], s["qty"]) != (b["date"], b["symbol"], b["qty"]):
                continue
            used.update((id(b), id(s)))
            crosses.append({"date": b["date"], "symbol": b["symbol"], "security": b["security"],
                            "qty": b["qty"], "price": b["price"], "sell_price": s["price"],
                            "value": b["value"], "buyer": b["client"], "seller": s["client"],
                            "keys": [b["key"], s["key"]]})
            break
    rest = [d for d in deals if id(d) not in used]
    return crosses, rest


# ---------------------------------------------------------------- your symbols

SYMBOL_KEYS = ("symbol", "ticker", "tradingsymbol")
LIST_KEYS = ("symbols", "tickers", "watchlist")


def _walk_symbols(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SYMBOL_KEYS and isinstance(v, str) and v.strip():
                out.add(nse_symbol(v))
            elif k in LIST_KEYS and isinstance(v, list):
                out.update(nse_symbol(x) for x in v if isinstance(x, str) and x.strip())
            _walk_symbols(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_symbols(v, out)


def my_symbols(trader_dir, extra=()):
    """Symbols in trader/portfolio.json, watchlist.json and plan.json, plus --watch."""
    out = {nse_symbol(s) for s in extra or ()}
    for name in ("portfolio.json", "watchlist.json", "plan.json"):
        path = os.path.join(trader_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                _walk_symbols(json.load(fh), out)
        except (OSError, ValueError):
            continue
    return {s for s in out if s and not s.startswith("^")}


# ---------------------------------------------------------------- state

def load_state(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"seen": [], "last_check": None}


def save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    state["seen"] = state["seen"][-SEEN_CAP:]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- gathering

def gather(fetch, only, seen=frozenset(), max_filings=MAX_FILINGS, today=None):
    """Read the feeds. Filings in `seen` are not downloaded again.
    Returns {"insider": {...}, "bulk": {...}, "block": {...}, "warnings", "errors"}."""
    today = today or today_ist()
    out = {"warnings": [], "errors": []}
    if "insider" in only:
        ins = {"built": None, "items": 0, "newest": None, "filings": [], "unread": []}
        try:
            feed = parse_rss(fetch.get(RSS_URL, ("rss-Insider_Trading.xml", "Insider_Trading.xml")))
        except InsiderError as exc:
            out["errors"].append("insider feed: %s" % exc)
            feed = None
        if feed is not None:
            ins["built"], ins["items"] = feed["built"], len(feed["items"])
            if not feed["items"]:
                out["warnings"].append("The insider feed is empty right now (NSE sometimes "
                                       "serves it with no items). Try again later.")
            else:
                newest = feed["items"][0]["published"]
                ins["newest"] = newest
                age = days_between(newest, today.isoformat()) if newest else None
                if age is not None and age > STALE_DAYS:
                    out["warnings"].append(
                        "The insider feed looks stale: its newest item is from %s (%d days ago) "
                        "although NSE rebuilt the file %s. Open NSE's insider trading page "
                        "(Companies > Corporate filings > Insider trading) in your browser for "
                        "the latest." % (newest[:10], age, ins["built"] or "recently"))
            todo = [it for it in feed["items"] if it["id"] not in seen]
            for it in todo[:max_filings]:
                try:
                    f = parse_xbrl(fetch.get(it["url"]), it["published"], it["id"])
                except InsiderError as exc:
                    ins["unread"].append(dict(it, error=str(exc)))
                    continue
                f["company"] = f["company"] or it["company"]
                f["url"] = it["url"]
                ins["filings"].append(f)
            for it in todo[max_filings:]:
                ins["unread"].append(dict(it, error="over --max-filings %d" % max_filings))
        out["insider"] = ins
    for kind in ("bulk", "block"):
        if kind not in only:
            continue
        try:
            got = parse_deals(fetch.get(DEAL_URL % kind), kind)
        except InsiderError as exc:
            out["errors"].append("%s deals: %s" % (kind, exc))
            continue
        age = days_between(got["date"], today.isoformat()) if got["date"] else None
        if age is not None and age > DEALS_STALE_DAYS:
            out["warnings"].append("%s.csv holds deals of %s, %d days ago." % (kind, got["date"], age))
        out[kind] = got
    return out


# ---------------------------------------------------------------- filters

def row_ok(row, args, mine, who_key):
    if args.symbol and row["symbol"] not in {nse_symbol(s) for s in args.symbol}:
        return False
    if args.holdings and row["symbol"] not in mine:
        return False
    if args.person and squash(args.person).lower() not in (row.get(who_key) or "").lower():
        return False
    if args.min_value and (row.get("value") is None or row["value"] < args.min_value):
        return False
    return True


def apply_filters(data, args, mine, seen=frozenset()):
    """Keep what matches the filters and is not in `seen`. Flags your symbols."""
    lowest = LEVELS[args.signal] if getattr(args, "signal", None) else 0
    out = {"warnings": data["warnings"], "errors": data["errors"]}
    if "insider" in data:
        ins = dict(data["insider"])
        filings = []
        for f in ins["filings"]:
            rows = [dict(r, mine=r["symbol"] in mine) for r in f["rows"]
                    if row_ok(r, args, mine, "person") and LEVELS[r["signal"]] >= lowest]
            if rows:
                filings.append(dict(f, rows=rows))
        ins["filings"] = filings
        ins["unread"] = [u for u in ins["unread"] if not args.symbol and not args.holdings]
        out["insider"] = ins
    for kind in ("bulk", "block"):
        if kind not in data:
            continue
        got = dict(data[kind])
        deals = [dict(d, mine=d["symbol"] in mine) for d in got["deals"]
                 if d["key"] not in seen and row_ok(d, args, mine, "client") and not lowest]
        if kind == "block":
            got["crosses"], deals = pair_crosses(deals)
            for c in got["crosses"]:
                c["mine"] = c["symbol"] in mine
        # stake-building first, then the desks trading around a stock
        got["deals"] = sorted(deals, key=lambda d: (d["round_trip"], d["symbol"], d["side"]))
        out[kind] = got
    return out


def count(view):
    n = sum(len(f["rows"]) for f in view.get("insider", {}).get("filings", []))
    for kind in ("bulk", "block"):
        n += len(view.get(kind, {}).get("deals", [])) + len(view.get(kind, {}).get("crosses", []))
    return n


# ---------------------------------------------------------------- output

FOOTER = ("Public filings and exchange data, read quickly, not early: insiders tell the company "
          "within 2 trading days of a trade and the company tells NSE within 2 more. Not inside "
          "information. Not financial advice.")


def describe(r):
    held = ""
    if r.get("pre_qty") is not None and r.get("post_qty") is not None:
        held = "; holding %s (%s %%) -> %s (%s %%)" % (
            grp(r["pre_qty"]), "?" if r["pre_pct"] is None else "%g" % r["pre_pct"],
            grp(r["post_qty"]), "?" if r["post_pct"] is None else "%g" % r["post_pct"])
    when = r["from_date"] or "?"
    if r.get("to_date") and r["to_date"] != r["from_date"]:
        when += " to %s" % r["to_date"]
    told = ""
    if r.get("intimated"):
        n = r["trading_days_to_intimate"]
        told = ", told the company %s (%s trading day%s later%s)" % (
            r["intimated"], n, "" if n == 1 else "s", ", LATE: over 2" if r.get("late") else "")
    public = ""
    if r.get("days_to_public") is not None:
        n = r["days_to_public"]
        public = ", public %d day%s after the trade" % (n, "" if n == 1 else "s")
    price = " at about %s" % inr(r["price"]) if r.get("price") else ""
    return "%s %s: %s %s %s (%s%s), %s%s, traded %s%s%s%s" % (
        r.get("category") or "?", r.get("person") or "?", (r.get("txn") or "?").lower(),
        grp(r["qty"]), r.get("instrument") or "securities", crore(r.get("value")), price,
        r.get("mode") or "mode not stated", " on %s" % r["exchange"] if r.get("exchange") else "",
        when, told, public, held)


def flag(sym, mine):
    return "  <- %s is on your list" % sym if mine else ""


def render(view, title):
    out = []
    ins = view.get("insider")
    if ins is not None:
        out.append("Insider trades (SEBI PIT, Reg 7(2)). Feed rebuilt %s IST, %d filings listed, "
                   "newest %s." % (ins["built"] or "?", ins["items"], ins["newest"] or "none"))
        for f in ins["filings"]:
            out.append("%s %s %s, filed %s (%s)" % (title, f["symbol"] or "?", f["company"] or "",
                                                    f["published"] or "?", f["id"]))
            for r in f["rows"]:
                out.append("  - %s%s" % (describe(r), flag(r["symbol"], r["mine"])))
                out.append("      [%s: %s]" % (r["signal"], r["why"]))
        for u in ins["unread"]:
            out.append("%s %s, filed %s: not read (%s)\n      %s" % (
                title, u["company"], u["published"] or "?", u["error"], u["url"]))
        if not ins["filings"] and not ins["unread"]:
            out.append("  no insider filings to show")
    names = {"bulk": "Bulk deals (one client trading over 0.5 % of a company's shares in a day)",
             "block": "Block deals (large negotiated trades in the block-deal window)"}
    for kind in ("bulk", "block"):
        got = view.get(kind)
        if got is None:
            continue
        out.append("%s, %s:" % (names[kind], got["date"] or "no date"))
        for c in got.get("crosses", []):
            out.append("  - %s %s: %s sold %s to %s at %s = %s (a cross: both sides of one block)%s"
                       % (c["symbol"], c["security"], c["seller"], grp(c["qty"]), c["buyer"],
                          inr(c["price"]), crore(c["value"]), flag(c["symbol"], c["mine"])))
        for d in got["deals"]:
            note = "  [same client on both sides today: trading around it]" if d["round_trip"] else ""
            out.append("  - %s %s: %s %s %s at %s = %s%s%s" % (
                d["symbol"], d["security"], d["client"], d["side"].upper(), grp(d["qty"]),
                "?" if d["price"] is None else inr(d["price"]), crore(d["value"]), note,
                flag(d["symbol"], d["mine"])))
        if not got["deals"] and not got.get("crosses"):
            out.append("  none to show")
    for w in view["warnings"]:
        out.append("NOTE: %s" % w)
    for e in view["errors"]:
        out.append("ERROR: %s" % e)
    out.append(FOOTER)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--only", default=",".join(SOURCES),
                       help="sources, comma separated: insider,bulk,block (default all)")
        p.add_argument("--symbol", action="append", help="only this NSE symbol; repeat for more")
        p.add_argument("--person", help="name contains this (insider or deal client)")
        p.add_argument("--min-value", type=float, default=0, help="rupees, e.g. 10000000 = 1 crore")
        p.add_argument("--signal", choices=sorted(LEVELS),
                       help="insider rows at this signal or stronger (drops the deals)")
        p.add_argument("--holdings", action="store_true",
                       help="only symbols in your portfolio, watchlist or plan")
        p.add_argument("--watch", action="append", default=[], help="flag this symbol too")
        p.add_argument("--trader-dir", default="trader")
        p.add_argument("--max-filings", type=int, default=MAX_FILINGS)
        p.add_argument("--offline", metavar="DIR", help="read saved files from DIR")
        p.add_argument("--today", help="YYYY-MM-DD (India), for reproducible runs")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("list", help="everything in the feeds now, filtered")
    common(p)

    p = sub.add_parser("check", help="only what was not shown at the last check")
    common(p)
    p.add_argument("--state", default="trader/state/insider.json")
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")

    p = sub.add_parser("filing", help="read one IT_*.xml disclosure (file or URL)")
    p.add_argument("source")
    p.add_argument("--published", help="when NSE published it, YYYY-MM-DD [HH:MM]")
    p.add_argument("--watch", action="append", default=[])
    p.add_argument("--trader-dir", default="trader")
    p.add_argument("--json", action="store_true")
    return ap


def main(argv=None):
    args = _parser().parse_args(argv)
    if not args.cmd:
        _parser().print_help()
        return 2
    try:
        return _run(args)
    except (InsiderError, OSError, ValueError) as exc:
        print("insider: %s" % exc, file=sys.stderr)
        return 2


def _run(args):
    mine = my_symbols(args.trader_dir, args.watch)
    if args.cmd == "filing":
        if re.match(r"^https?://", args.source):
            if not args.source.startswith("https://nsearchives.nseindia.com/"):
                raise InsiderError("only nsearchives.nseindia.com links are read")
            data = Fetcher().get(args.source)
        else:
            with open(args.source, "rb") as fh:
                data = fh.read()
        f = parse_xbrl(data, args.published, filing_id(args.source))
        for r in f["rows"]:
            r["mine"] = r["symbol"] in mine
        if args.json:
            print(json.dumps(f, indent=2))
        else:
            view = {"insider": {"built": None, "items": 1, "newest": args.published,
                                "filings": [f], "unread": []}, "warnings": [], "errors": []}
            sys.stdout.write(render(view, "FILING"))
        return 0 if f["rows"] else 1

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    bad = [s for s in only if s not in SOURCES]
    if bad or not only:
        raise InsiderError("--only takes insider, bulk, block (got %s)" % args.only)
    today = datetime.date.fromisoformat(args.today) if args.today else today_ist()
    fetch = Fetcher(args.offline)

    if args.cmd == "list":
        data = gather(fetch, only, max_filings=args.max_filings, today=today)
        view = apply_filters(data, args, mine)
        if args.json:
            print(json.dumps(view, indent=2))
        else:
            sys.stdout.write(render(view, "FILING"))
        if len(data["errors"]) == len(only):
            return 2
        return 0 if count(view) else 1

    # check
    state = load_state(args.state)
    seen = set(state.get("seen", []))
    data = gather(fetch, only, seen, args.max_filings, today)
    view = apply_filters(data, args, mine, seen)
    fresh = count(view)
    if args.json:
        print(json.dumps(dict(view, new=fresh), indent=2))
    else:
        if not fresh:
            print("Nothing new since %s." % (state.get("last_check") or "the last check"))
        sys.stdout.write(render(view, "NEW"))
    if not args.dry_run:
        keys = [f["id"] for f in view.get("insider", {}).get("filings", [])]
        for kind in ("bulk", "block"):
            got = view.get(kind, {})
            keys += [d["key"] for d in got.get("deals", [])]
            for c in got.get("crosses", []):
                keys += c["keys"]
        state["seen"] = list(state.get("seen", [])) + [k for k in keys if k not in seen]
        state["last_check"] = to_ist(utc_now()).isoformat(" ", "minutes") + " IST"
        save_state(args.state, state)
    if len(data["errors"]) == len(only):
        return 2
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
