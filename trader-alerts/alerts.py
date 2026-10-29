#!/usr/bin/env python3
"""Price alerts and the NSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is the NSE open? next open / close
    python3 alerts.py quote RELIANCE --write         # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader/plan.json
    python3 alerts.py add --symbol RELIANCE --stop 1163.3
    python3 alerts.py add --symbol RELIANCE --target 1185.3
    python3 alerts.py add --at 15:10 --label "square off"
    python3 alerts.py add --symbol SBIN --pct 3
    python3 alerts.py add --opening-bell
    python3 alerts.py add --pre-open
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-10-29 15:10" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset reliance-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  opening_bell    the NSE opened today, 9:15 India time (rings once per trading day)
  pre_open        the NSE pre-open is on, 9:00 to 9:15 (rings once per trading day,
                  only inside that window)
  time            the India clock passed "at" (HH:MM) on a trading day
                  (rings once per trading day). NSE has no early closes, so the
                  US-era "before_close" field is accepted and ignored.

A rule with "date" is only watched on that India date (rules from a plan are
for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day.

Quotes (free, delayed, unofficial; for alerts only, never for timing an order):
  https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS (falls back to
  query1 on a 429 or a 5xx; one retry with a pause on a 429; one request per
  symbol, half a second apart). Yahoo is the only source: Stooq has no NSE
  coverage. --offline DIR reads DIR/yahoo-<SYMBOL>.json (or .json.gz) instead,
  "&" dropped from the file name (M&M -> yahoo-MM.json).

NSE calendar: the 2026 trading holidays from NSE's circular, plus the Muhurat
session (times None until NSE publishes them). Outside CALENDAR_YEARS the clock
says the holiday list is unknown. All times are India time (IST, UTC+5:30).
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
FLAT_BY = "15:10"   # default square-off alert: before Zerodha's MIS auto square-off (about 15:20)
PRE_OPEN_MINUTES = 15


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- the NSE block
# Copied verbatim from tests/_nse.py (tests/test_nse.py checks every copy).

# ---------------------------------------------------------------- calendar

# NSE capital-market trading holidays that fall on weekdays, from NSE's 2026
# holiday circular (dates cross-checked against broker copies 2026-10-29;
# confirm at https://www.nseindia.com/resources/exchange-communication-holidays).
# Holidays on a Saturday or Sunday are left out: the weekend rule covers them.
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
# NSE publishes next year's list in December. Until 2027 is added here the
# clock says the holiday list is unknown for 2027 instead of guessing.
CALENDAR_YEARS = (2026,)

# Muhurat trading: a one-hour evening session on Diwali, often on a weekend.
# NSE announces the times by circular a few weeks ahead. None means "announced
# as a session, times not published yet": the clock treats the day as closed
# and says why.
NSE_MUHURAT = {
    "2026-11-08": None,  # Diwali Laxmi Pujan (Sunday); e.g. ("18:00", "19:00") once announced
}

PRE_OPEN = datetime.time(9, 0)   # pre-open order entry 9:00 to 9:08, matching to 9:15
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
IST = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Naive or aware UTC -> naive India time."""
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    """Naive India time -> naive UTC."""
    return local.replace(tzinfo=None) - IST


def ts_to_ist(ts):
    """Unix seconds -> naive India time. Never uses the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts)) + IST


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def parse_now(text, error=AlertError):
    """--now -> naive UTC. "2026-10-19 10:15" (or ...T10:15, or just 10:15 for
    today) is India time; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise error('--now wants an India time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def _hm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


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


def session(d):
    """(open_time, close_time) in India time, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    special = NSE_MUHURAT.get(d.isoformat())
    if special:
        return _hm(special[0]), _hm(special[1])
    return OPEN, CLOSE


# ---------------------------------------------------------------- tick size

# NSE cash-market tick size by price, from the tiered table in force since
# 2025-04-15 (the under-250 band at 0.01 since 2024-06-10). NSE sets each
# stock's tick monthly from the previous month's last close; the tools use the
# price at hand, so pass "tick_size" in the config to pin one if they differ.
TICK_BANDS = (  # (price below, tick)
    (250, 0.01),
    (1000, 0.05),
    (5000, 0.10),
    (10000, 0.50),
    (20000, 1.00),
    (float("inf"), 5.00),
)


def tick_size(price):
    for below, tick in TICK_BANDS:
        if price < below:
            return tick
    return TICK_BANDS[-1][1]


def round_tick(price, direction="nearest", tick=None):
    """Round a price onto the tick grid: "down" for a long stop, "up" for a
    long target, "nearest" otherwise. Works in integer paise to dodge float
    error: the price is snapped to the paisa first, so Yahoo's 1156.199951...
    is 1156.20 (on the grid already), not a hair under it."""
    tick = tick or tick_size(price)
    paise = int(round(tick * 100))
    p = int(round(price * 100))
    if direction == "down":
        n = p // paise
    elif direction == "up":
        n = -(-p // paise)
    else:
        n = (2 * p + paise) // (2 * paise)
    return round(n * paise / 100.0, 2)


# ---------------------------------------------------------------- charges

# Intraday (MIS) equity on NSE through Zerodha, as published on
# https://zerodha.com/charges (read 2026-10-29). Override any of these with
# "charges": {...} in trader/config.json; your contract note is the truth.
CHARGES = {
    "brokerage_pct": 0.03,          # of turnover per executed order ...
    "brokerage_max": 20.0,          # ... capped at Rs 20 an order
    "stt_sell_pct": 0.025,          # securities transaction tax, sell side, intraday
    "exchange_pct": 0.00297,        # NSE transaction charge, both sides
    "sebi_pct": 0.0001,             # Rs 10 a crore, both sides
    "stamp_buy_pct": 0.003,         # stamp duty, buy side, intraday
    "gst_pct": 18.0,                # on brokerage + exchange + SEBI fees
}


def charges(qty, buy_price, sell_price, rates=None):
    """Round-trip cost in rupees of buying and selling qty shares intraday.
    Returns a dict of each part plus "total"."""
    r = dict(CHARGES)
    r.update(rates or {})
    buy, sell = qty * buy_price, qty * sell_price
    brokerage = (min(buy * r["brokerage_pct"] / 100, r["brokerage_max"])
                 + min(sell * r["brokerage_pct"] / 100, r["brokerage_max"]))
    stt = round(sell * r["stt_sell_pct"] / 100)          # STT is rounded to the rupee
    exchange = (buy + sell) * r["exchange_pct"] / 100
    sebi = (buy + sell) * r["sebi_pct"] / 100
    stamp = round(buy * r["stamp_buy_pct"] / 100)
    gst = (brokerage + exchange + sebi) * r["gst_pct"] / 100
    out = {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi,
           "stamp": stamp, "gst": gst}
    out = {k: round(v, 2) for k, v in out.items()}
    out["total"] = round(sum(out.values()), 2)
    return out


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


# ---------------------------------------------------------------- network

# One User-Agent for every request. No URL in it: nsearchives.nseindia.com
# drops the connection when the User-Agent carries one (checked 2026-10-29).
UA = "Mozilla/5.0 (compatible; india-day-trader-skill/3.0)"


# ---------------------------------------------------------------- symbols

def yahoo_symbol(sym):
    """NSE symbol -> Yahoo's, for the URL path: RELIANCE -> RELIANCE.NS,
    M&M -> M%26M.NS. Indices (^NSEI) pass through."""
    s = sym.strip().upper()
    if s.startswith("^") or s.endswith(".NS"):
        base = s
    else:
        base = s + ".NS"
    return base.replace("&", "%26").replace("^", "%5E")


def nse_symbol(sym):
    """RELIANCE.NS / reliance -> RELIANCE."""
    s = sym.strip().upper()
    return s[:-3] if s.endswith(".NS") else s


# ---------------------------------------------------------------- the clock

def _minus(t, minutes):
    return (datetime.datetime.combine(datetime.date(2000, 1, 1), t) - datetime.timedelta(minutes=minutes)).time()


def market_status(now_utc):
    local = to_ist(now_utc)
    d = local.date()
    ok, reason = trading_day(d)
    key = d.isoformat()
    st = {"now_ist": local.strftime("%Y-%m-%d %H:%M"), "date": key,
          "weekday": local.strftime("%A"), "calendar_known": d.year in CALENDAR_YEARS,
          "open": False, "pre_open": False, "reason": reason, "holiday": NSE_HOLIDAYS.get(key),
          "muhurat": None}
    if key in NSE_MUHURAT:
        times = NSE_MUHURAT[key]
        st["muhurat"] = ("%s to %s" % times) if times else "times not published yet"
    s = session(d)
    if s:
        o, c = s
        pre = PRE_OPEN if o == OPEN else _minus(o, PRE_OPEN_MINUTES)
        st["pre_opens"], st["opens"], st["closes"] = pre.strftime("%H:%M"), o.strftime("%H:%M"), c.strftime("%H:%M")
        if o <= local.time() < c:
            st["open"] = True
            st["reason"] = "Muhurat trading session" if st["muhurat"] else "regular session"
        elif pre <= local.time() < o:
            st["pre_open"] = True
            st["reason"] = "pre-open (order entry %s to %s, matching until the open)" % (
                pre.strftime("%H:%M"), _minus(o, 7).strftime("%H:%M"))
        elif local.time() < pre:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    if not st["calendar_known"] and st["reason"] in ("", "regular session", "before the open", "after the close"):
        st["reason"] = (st["reason"] + "; " if st["reason"] else "") + "holiday list unknown for %d" % d.year
    # next open
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        ns = session(nd)
        if ns:
            st["next_open"] = "%s %s" % (nd.isoformat(), ns[0].strftime("%H:%M"))
            if nd.isoformat() in NSE_MUHURAT:
                st["next_open_note"] = "Muhurat trading"
            elif nd.year not in CALENDAR_YEARS:
                st["next_open_note"] = "holiday list unknown for %d" % nd.year
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, yahoo_symbol(sym))


def _utc_stamp(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_yahoo(text, sym):
    sym = nse_symbol(sym)
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown symbol")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = _utc_stamp(EPOCH + datetime.timedelta(seconds=int(t))) if t else ""
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym, "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "time_ist": ts_to_ist(t).strftime("%Y-%m-%d %H:%M:%S") if t else "",
            "currency": m.get("currency") or "INR", "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, sym):
    """DIR/yahoo-<SYMBOL>.json(.gz), trying the symbol as is, then without "&" (M&M -> MM)."""
    tried = []
    for key in _dedupe([sym, sym.replace("&", "").replace("^", "")]):
        base = os.path.join(folder, "yahoo-%s.json" % key)
        tried.append(base)
        if os.path.exists(base):
            with open(base, encoding="utf-8") as fh:
                return fh.read()
        if os.path.exists(base + ".gz"):
            with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
                return fh.read()
    raise AlertError("no offline quote file %s" % tried[-1])


def get_quotes(symbols, offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per symbol, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        sym = nse_symbol(sym)
        try:
            if offline:
                text = _offline_text(offline, sym)
            else:
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(sym)
            quotes[sym] = parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc):
    """Merge quotes into trader/prices.json. Machine times stay UTC (...Z, what
    trader-budget and trader-risk read); the *_ist fields are the same times in
    India time for people."""
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = _utc_stamp(now_utc)
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "time_ist": q.get("time_ist"),
                     "source": q.get("source"), "fetched": stamp}
    doc = {"as_of": stamp, "as_of_ist": to_ist(now_utc).strftime("%Y-%m-%d %H:%M:%S"), "source": "yahoo",
           "currency": "INR",
           "note": ("Delayed quotes in rupees for alerts and sizing checks. Your broker's price is the one that "
                    "fills. detail.fetched is when each price was read (UTC); *_ist is India time."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "opening_bell", "pre_open", "time"}
NO_QUOTE = {"opening_bell", "pre_open", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an India time like \"15:10\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that India date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this India date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_ist(utc).date().isoformat() == today


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_ist(now_utc)
    stamp = _utc_stamp(now_utc)
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                if status.get("muhurat"):
                    msg = "Opening bell: Muhurat trading is open (%s to %s IST)." % (status["opens"], status["closes"])
                else:
                    msg = "Opening bell: the NSE is open (%s IST), the market closes at %s." % (
                        status["now_ist"], status["closes"])
                rings.append({"id": rid, "symbol": "", "message": msg})
                rung[rid] = stamp
            continue
        if typ == "pre_open":
            if status.get("pre_open") and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Pre-open: the NSE pre-open is on (%s IST, %s); "
                              "the market opens at %s." % (status["now_ist"], status["reason"], status["opens"])})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r.get("at"))     # "before_close" is a no-op: NSE has no early closes
            opens = status.get("opens")
            if status.get("muhurat") and opens and at.strftime("%H:%M") < opens:
                continue                # a day-session alert means nothing on a Muhurat evening
            if opens and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:],
                                                         local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", the market closes at %s." % status["closes"]) if status["open"] else \
                    ", the market has closed." if local.strftime("%H:%M") >= status["closes"] else \
                    ", the market opens at %s." % opens
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: trader-broker flatten --symbol %s builds the sell "
                            "ticket, then trader-risk and your yes. Zerodha squares off what is left of an MIS "
                            "position itself at about 15:20, for a fee." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = nse_symbol(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %s: %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", inr(p), word, label, inr(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %s (from %s)."
                              % (sym, move, inr(p), inr(float(ref)))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = nse_symbol(str(plan["symbol"]))
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or FLAT_BY
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not an India time like 15:10" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by close"})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(nse_symbol(str(p.get("ticker") or p.get("symbol") or "")))
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(nse_symbol(str(plan["symbol"])))
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    st = market_status(parse_now(a.now))
    if a.json:
        print(json.dumps(st, indent=2))
        return 0 if st["open"] else 1
    print("India %s IST (%s): NSE %s, %s." % (st["now_ist"], st["weekday"],
                                             "OPEN" if st["open"] else "closed", st["reason"]))
    if st.get("muhurat"):
        print("Muhurat trading day: %s." % (("session %s IST" % st["muhurat"]) if st.get("opens") else
                                            "session times not published yet, watch NSE's circular"))
    elif st.get("opens"):
        print("Today's session: %s to %s IST (pre-open from %s)." % (st["opens"], st["closes"], st["pre_opens"]))
    if not st["open"] and st.get("next_open"):
        print("Next open: %s IST%s." % (st["next_open"], (" (%s)" % st["next_open_note"])
                                        if st.get("next_open_note") else ""))
    if not st["calendar_known"]:
        print("Holiday list unknown for %s: the tool knows %s only. Check NSE's holiday list "
              "(nseindia.com, Exchange communication > Holidays) for this date."
              % (st["date"][:4], " and ".join(map(str, CALENDAR_YEARS))))
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([nse_symbol(s) for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote RELIANCE SBIN, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close %s)" % (100.0 * (q["price"] / q["prev_close"] - 1),
                                                          inr(q["prev_close"]))
                print("%-12s %s%s  %s IST  [%s]" % (s, inr(q["price"]), chg, q.get("time_ist", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, delayed and unofficial: for alerts only. Your broker's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    status = market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([nse_symbol(str(r.get("symbol", ""))) for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"],
                                        status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %s" % (s, inr(quotes[s]["price"])) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. The SL-M stop leg sits at the broker once the entry "
                  "fills; the target is not an order (Kite has no bracket orders for equity), so a target alert "
                  "is your cue to sell. Check get_positions and get_orders, record any fill (ticket.py fill), and "
                  "to close a position yourself: ticket.py flatten, then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    if a.before_close:
        raise AlertError("--before-close is gone: NSE has no early closes, so a time alert rings at --at on "
                         "every trading day")
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free quotes are delayed; the SL-M stop at the broker is what actually stops you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.pre_open:
        rule = {"id": a.id or "pre-open", "type": "pre_open"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an India time like 15:10")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = nse_symbol(a.symbol)
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell, --pre-open)")
        sym = nse_symbol(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        else:
            raise AlertError("give one of --above, --below, --stop, --target or --pct")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read DIR/yahoo-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = India time)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--pre-open", action="store_true", help="rings in the 9:00 to 9:15 pre-open")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, India time")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"square off\"")
    p.add_argument("--before-close", action="store_true", help=argparse.SUPPRESS)   # US early closes: refused
    p.add_argument("--date", help="watch this rule on this India date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
