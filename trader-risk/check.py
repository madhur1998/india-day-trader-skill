#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-28 09:45"        # pin the clock (India time)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --series EQUITY_L.csv           # only EQ series can be traded intraday
    python3 check.py --no-write                      # check without recording the verdicts

An order is the Kite place_order arguments ("kite"), plus for a day-trade
entry its protective stop ("stop_leg": an SL-M or SL sell that goes to the
exchange once the entry fills) and notes for us ("target", "why", "plan").

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        the Kite fields: variety regular or amo, exchange NSE,
                transaction_type BUY or SELL, product MIS or CNC, order_type
                MARKET / LIMIT / SL / SL-M with the right price and
                trigger_price, validity DAY or IOC, quantity a positive whole
                number, prices as numbers on the tick grid, a tag of at most
                20 letters and digits; a stop leg that is not the opposite side,
                SL-M or SL, the same product, symbol and quantity, beyond the entry
  asset         futures and options (NFO symbols) unless "allow_options": true;
                anything that does not look like an NSE equity symbol
  series        with --series FILE (EQUITY_L.csv or sec_list.csv): a symbol
                that is not EQ series (BE / BZ trade to trade: no intraday)
  short         an MIS sell without the shares is a short entry: blocked
                unless "allow_short": true

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
                (with "allow_margin": true, an MIS buy needs value / mis_leverage)
  circuit       with a band from sec_list.csv and a previous close: the entry
                outside the day's price band (the exchange rejects it)
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (the stop leg, or the
                order's plan stop); a stop on the wrong side of the entry
  loss          qty x (entry - stop) + the round-trip charges at the stop
                ("count_charges": true) over max_loss_per_trade_inr. The entry
                is the limit price for a LIMIT order, else the HIGHER of the
                plan's entry and a fresh price (under 15 minutes old)
  reward:risk   net reward / net risk (after charges) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (India
                time, 09:30 and 14:45 by default), or the market is closed
                (weekend, NSE holiday, outside 9:15 to 15:30)

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order
(its "kite" and "stop_leg"), so trader-broker only shows an order as ready
if THIS exact order passed today.
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import sys

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 80000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:10",
    "product": "MIS", "mis_leverage": 1, "count_charges": True, "charges": {},
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
}
# the place_order arguments trader-broker sends; anything else is flagged
KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "validity", "disclosed_quantity", "tag")
VARIETIES = ("regular", "amo")
PRODUCTS = ("MIS", "CNC")
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
VALIDITIES = ("DAY", "IOC")
DERIV_EXCHANGES = ("NFO", "BFO", "MCX", "CDS", "BCD")
# NIFTY26NOV24000CE, RELIANCE26NOVFUT, BANKNIFTY2611352000PE
DERIV = re.compile(r"^[A-Z&\-]+\d{2}[A-Z0-9]{3}\d*(CE|PE|FUT)$")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
TAG = re.compile(r"^[A-Za-z0-9]{1,20}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PAISA = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger": "the trigger price", "stale price": "an old price"}


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- the NSE block (verbatim from tests/_nse.py)

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

CALENDAR_YEARS = (2026,)

NSE_MUHURAT = {
    "2026-11-08": None,  # Diwali Laxmi Pujan (Sunday); e.g. ("18:00", "19:00") once announced
}

PRE_OPEN = datetime.time(9, 0)   # pre-open order entry 9:00 to 9:08, matching to 9:15

OPEN = datetime.time(9, 15)

CLOSE = datetime.time(15, 30)

IST = datetime.timedelta(hours=5, minutes=30)


def to_ist(utc):
    """Naive or aware UTC -> naive India time."""
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    """Naive India time -> naive UTC."""
    return local.replace(tzinfo=None) - IST


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def parse_now(text, error=RiskError):
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


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an India time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def window_check(now_ist, c):
    """Reasons an entry cannot be placed now (India time). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    if d.year not in CALENDAR_YEARS:
        warns.append("the NSE holiday list covers %s only: check nseindia.com for %s"
                     % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat()))
    ok, why = trading_day(d)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s IST)" % (why, stamp)], warns
    opens, closes = session(d)
    if t < opens or now_ist.time() >= closes:
        return ["window: the market is closed (%s IST; the session is %s to %s)"
                % (stamp, opens.strftime("%H:%M"), closes.strftime("%H:%M"))], warns
    if (opens, closes) != (OPEN, CLOSE):
        warns.append("special session today (%s, %s to %s): your entry window is not shifted"
                     % (why, opens.strftime("%H:%M"), closes.strftime("%H:%M")))
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s; MIS positions are squared off "
                "near the close)" % (after.strftime("%H:%M"), t.strftime("%H:%M"))], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def check_config(cfg):
    """A dollar key from the US pack is an error, never a silent fallback."""
    for k in sorted(cfg or {}):
        m = re.match(r"^(\w+)_usd$", k)
        if m:
            raise RiskError("your config still has \"%s\": this pack trades rupees on NSE. Rename it to "
                            "\"%s_inr\" and give it a rupee amount" % (k, m.group(1)))
    return cfg


def kite_of(order):
    """The place_order arguments of an order: its "kite" object, or the order itself
    when it is a bare Kite dict."""
    k = order.get("kite")
    if isinstance(k, dict):
        return k
    if "tradingsymbol" in order:
        return {f: order[f] for f in order if f in KITE_FIELDS or f not in
                ("symbol", "side", "qty", "stop_leg", "target", "why", "plan")}
    return {}


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact entry and stop leg that were checked."""
    core = {"kite": kite_of(order), "stop_leg": order.get("stop_leg")}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _p(x):
    return "%.2f" % float(x)


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def on_tick(price, tick=None):
    return abs(round_tick(price, "nearest", tick) - price) < 1e-6


def rates_for(plan, cfg):
    """Zerodha's intraday rates, then the plan's effective rates, then your config's."""
    r = dict(CHARGES)
    pr = (plan or {}).get("charges_rates")
    if isinstance(pr, dict):
        r.update(pr)
    r.update((cfg or {}).get("charges") or {})
    return r


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number (rupees)")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or p.get("tradingsymbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty", p.get("quantity"))))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, inr(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker (paper fills count as placed)."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


def load_series(path):
    """EQUITY_L.csv (SYMBOL, NAME OF COMPANY, SERIES, ...) or sec_list.csv (Symbol, Series,
    Security Name, Band, ...) -> {SYMBOL: {"series": "EQ", "band": 20.0 or None}}.
    A symbol can have rows in more than one series; EQ wins."""
    out = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
    except (IOError, OSError) as e:
        raise RiskError("cannot read the series file %s: %s" % (path, e))
    if not rows:
        raise RiskError("the series file %s is empty" % path)
    head = [h.strip().upper() for h in rows[0]]
    if "SYMBOL" not in head or "SERIES" not in head:
        raise RiskError("%s is not EQUITY_L.csv or sec_list.csv (no SYMBOL and SERIES columns)" % path)
    i_sym, i_ser = head.index("SYMBOL"), head.index("SERIES")
    i_band = head.index("BAND") if "BAND" in head else None
    for r in rows[1:]:
        if len(r) <= max(i_sym, i_ser):
            continue
        sym, ser = r[i_sym].strip().upper(), r[i_ser].strip().upper()
        band = None
        if i_band is not None and len(r) > i_band:
            band = _f(r[i_band].strip())
        if sym not in out or ser == "EQ":
            out[sym] = {"series": ser, "band": band}
    return out


# ---------------------------------------------------------------- order format

def kite_format(k, label, c):
    """Reasons a place_order argument object is malformed, and warnings. label is "" for
    the entry, "stop leg: " for the stop."""
    reasons, warns = [], []
    if not isinstance(k, dict) or not k:
        return ["format: %sno Kite order (\"kite\": the place_order arguments)" % label], warns
    tick = _f(c.get("tick_size")) or None
    var = k.get("variety")
    if var not in VARIETIES:
        reasons.append("format: %svariety must be regular or amo, got %r" % (label, var))
    elif var == "amo":
        warns.append("%san after-market order (amo) waits for the next session" % label)
    exch = k.get("exchange")
    if exch != "NSE" and exch not in DERIV_EXCHANGES:
        reasons.append("format: %sexchange must be NSE (this pack trades NSE only), got %r" % (label, exch))
    if not str(k.get("tradingsymbol") or "").strip():
        reasons.append("format: %sno tradingsymbol" % label)
    if k.get("transaction_type") not in ("BUY", "SELL"):
        reasons.append("format: %stransaction_type must be BUY or SELL, got %r" % (label, k.get("transaction_type")))
    prod = k.get("product")
    if prod not in PRODUCTS and not (exch in DERIV_EXCHANGES and prod == "NRML"):
        reasons.append("format: %sproduct must be MIS (intraday) or CNC (delivery), got %r" % (label, prod))
    q = k.get("quantity")
    if isinstance(q, bool) or not isinstance(q, int) or q <= 0:
        reasons.append("format: %squantity must be a positive whole number (an int), got %r" % (label, q))
    ot = k.get("order_type")
    if ot not in ORDER_TYPES:
        reasons.append("format: %sorder_type must be MARKET, LIMIT, SL or SL-M, got %r" % (label, ot))
    for f in ("price", "trigger_price"):
        v = k.get(f)
        if v is None:
            continue
        if isinstance(v, str) or _f(v) is None:
            reasons.append("format: %s%s must be a number (Kite takes numbers, not strings), got %r"
                           % (label, f, v))
        elif _f(v) < 0 or (_f(v) == 0 and not (f == "price" and ot in ("MARKET", "SL-M"))):
            reasons.append("format: %s%s must be positive, got %r" % (label, f, v))
        elif _f(v) > 0 and not on_tick(_f(v), tick):
            reasons.append("format: %s%s %s is off the tick grid (tick %s): Kite rejects it"
                           % (label, f, _num(v), _num(tick or tick_size(_f(v)))))
    price, trig = _f(k.get("price")) or 0, _f(k.get("trigger_price")) or 0
    if ot == "LIMIT":
        if not price:
            reasons.append("format: %sa LIMIT order needs a price" % label)
        if trig:
            reasons.append("format: %sa LIMIT order takes no trigger_price" % label)
    elif ot == "MARKET":
        if price or trig:
            reasons.append("format: %sa MARKET order takes no price or trigger_price" % label)
    elif ot == "SL":
        if not price or not trig:
            reasons.append("format: %san SL order needs both a price and a trigger_price" % label)
        elif k.get("transaction_type") == "SELL" and price > trig + PAISA:
            reasons.append("format: %sa sell SL needs its price at or below the trigger_price" % label)
        elif k.get("transaction_type") == "BUY" and price < trig - PAISA:
            reasons.append("format: %sa buy SL needs its price at or above the trigger_price" % label)
    elif ot == "SL-M":
        if not trig:
            reasons.append("format: %san SL-M order needs a trigger_price" % label)
        if price:
            reasons.append("format: %san SL-M order takes no price" % label)
    val = k.get("validity", "DAY")
    if val not in VALIDITIES:
        reasons.append("format: %svalidity must be DAY or IOC, got %r" % (label, val))
    tag = k.get("tag")
    if tag is not None and not (isinstance(tag, str) and TAG.match(tag)):
        reasons.append("format: %stag must be 1 to 20 letters and digits, got %r" % (label, tag))
    extra = sorted(f for f in k if f not in KITE_FIELDS)
    if extra:
        warns.append("%snot a place_order field this pack knows: %s; check it before you say yes"
                     % (label, ", ".join(extra)))
    return reasons, warns


def stop_leg_format(entry, leg, c):
    """The stop leg: the opposite side, SL-M or SL, same symbol, product and quantity."""
    reasons, warns = kite_format(leg, "stop leg: ", c)
    if reasons:
        return reasons, warns
    want = "SELL" if entry.get("transaction_type") == "BUY" else "BUY"
    if leg.get("transaction_type") != want:
        reasons.append("format: stop leg: must be a %s to close a %s, got %s"
                       % (want, entry.get("transaction_type"), leg.get("transaction_type")))
    if leg.get("order_type") not in ("SL-M", "SL"):
        reasons.append("format: stop leg: order_type must be SL-M or SL, got %s" % leg.get("order_type"))
    for f in ("tradingsymbol", "exchange", "product", "quantity"):
        if leg.get(f) != entry.get(f):
            reasons.append("format: stop leg: %s %r is not the entry's %r" % (f, leg.get(f), entry.get(f)))
    if leg.get("product") != "MIS":
        warns.append("stop leg: product %s; a day trade's stop is MIS" % leg.get("product"))
    if entry.get("tag") and leg.get("tag") and not str(leg["tag"]).startswith(str(entry["tag"])):
        warns.append("stop leg: its tag %s does not start with the entry's tag %s" % (leg["tag"], entry["tag"]))
    return reasons, warns


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          series=None, prev_close=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its India date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    series       {SYM: {"series", "band"}} from EQUITY_L.csv / sec_list.csv; None = not checked
    prev_close   {SYM: previous close} for the circuit band check"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    check_config(c)
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    prev_close = {str(k).upper(): float(v) for k, v in (prev_close or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_ist = to_ist(now_utc)
    today = today or now_ist.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), str(r.get("side", "")).lower()) for r in done_today}
    win_reasons, win_warns = window_check(now_ist, c)
    count_charges = bool(c.get("count_charges", True))
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders go to Zerodha with real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        if not isinstance(o, dict):
            o = {}
        k = kite_of(o)
        leg = o.get("stop_leg")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        sym = str(k.get("tradingsymbol") or o.get("symbol") or "").strip().upper()
        tt = str(k.get("transaction_type") or "").upper()
        side = {"BUY": "buy", "SELL": "sell"}.get(tt, str(o.get("side", "")).strip().lower())
        typ = str(k.get("order_type") or "")
        product = str(k.get("product") or "")
        q = k.get("quantity")
        q = float(q) if isinstance(q, int) and not isinstance(q, bool) and q > 0 else None
        value = None

        # format
        f_r, f_w = kite_format(k, "", c)
        reasons.extend(f_r)
        warns.extend(f_w)
        if o.get("notional") is not None:
            reasons.append("format: Kite takes a whole-share quantity, not a rupee amount (notional)")
        if o.get("symbol") and k.get("tradingsymbol") and str(o["symbol"]).upper() != str(k["tradingsymbol"]).upper():
            reasons.append("format: the order says %s but kite.tradingsymbol is %s" % (o["symbol"], k["tradingsymbol"]))
        if o.get("side") and tt and str(o["side"]).lower() != side:
            reasons.append("format: the order says %s but kite.transaction_type is %s" % (o["side"], tt))
        if o.get("qty") is not None and q is not None and _f(o.get("qty")) != q:
            reasons.append("format: the order says qty %s but kite.quantity is %s" % (o["qty"], _num(q)))
        if leg is not None:
            if side == "sell" and not c.get("allow_short"):
                reasons.append("format: a sell has no stop leg (only a day-trade entry carries one)")
            else:
                l_r, l_w = stop_leg_format(k, leg, c)
                reasons.extend(l_r)
                warns.extend(l_w)

        # asset
        exch = k.get("exchange")
        if sym and (exch in DERIV_EXCHANGES or DERIV.match(sym)):
            if not c.get("allow_options"):
                reasons.append("asset: %s is a futures or options contract (%s); F&O is off "
                               "(\"allow_options\": false)" % (sym, exch or "NFO"))
            else:
                warns.append("F&O order: the checks here are built for shares; options can lose all you pay")
        elif sym and not SYMBOL.match(sym):
            reasons.append("asset: %s does not look like an NSE equity symbol; this pack trades NSE shares only"
                           % sym)
        elif sym and series is not None:
            row = series.get(sym)
            if row is None:
                reasons.append("series: %s is not in the NSE securities list you gave (--series)" % sym)
            elif row["series"] != "EQ":
                reasons.append("series: %s trades in the %s series (trade to trade: delivery only, no intraday "
                               "and no MIS); this pack trades EQ only" % (sym, row["series"]))

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("LIMIT", "SL") and _f(k.get("price")):
            ref, ref_from = _f(k["price"]), "limit"
        elif typ == "SL-M" and _f(k.get("trigger_price")):
            ref, ref_from = _f(k["trigger_price"]), "trigger"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = round(ref, 2)
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISA:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "MARKET":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q and ref:
            value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to value a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, _num(q), sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - PAISA:
                later.append("size: %s is under your %s minimum order"
                             % (inr(value), inr(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA:
                later.append("size: %s is over your %s max order"
                             % (inr(value), inr(c["max_order_inr"])))

        # circuit band: the exchange rejects orders outside the day's band
        band = ((series or {}).get(sym) or {}).get("band")
        pc = prev_close.get(sym)
        if band and pc and ref and kind == "entry":
            lo, hi = pc * (1 - band / 100.0), pc * (1 + band / 100.0)
            if ref > hi + PAISA or ref < lo - PAISA:
                later.append("circuit: the entry %s is outside %s's %s%% band today (%s to %s from the previous "
                             "close %s)" % (_p(ref), sym, _num(band), _p(lo), _p(hi), _p(pc)))
            elif side == "buy" and ref > hi - (hi - pc) * 0.2:
                warns.append("%s is near its upper circuit %s (%s%% band): it can freeze there"
                             % (sym, _p(hi), _num(band)))
            if band <= 5:
                warns.append("%s has a %s%% price band: a stock locked at the lower circuit cannot be sold, "
                             "so the stop may not fill" % (sym, _num(band)))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        lev = 1.0
        if c.get("allow_margin") and product == "MIS":
            lev = max(_f(c.get("mis_leverage")) or 1.0, 1.0)
        need = value / lev if value is not None else None
        if need is not None and side == "buy" and kind == "entry":
            after_cash = cash - need
            if after_cash < -PAISA and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (borrowed money)"
                             % (inr(value), inr(cash)))
            elif after_cash < -PAISA:
                later.append("cash: needs %s at %sx MIS leverage but only %s cash"
                             % (inr(need), _num(lev), inr(cash)))
            elif after_cash < buffer_inr - PAISA:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (inr(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), inr(buffer_inr)))
            if lev > 1:
                info.append("margin: %s at %sx MIS leverage needs %s of your cash"
                            % (inr(value), _num(lev), inr(need)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + PAISA:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, inr(pos_after), 100.0 * pos_after / total, inr(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            over = (q > held + 1e-9) if q is not None else held <= 0
            if over and not c.get("allow_short"):
                what = "an intraday (MIS) short" if product == "MIS" else "a short sale"
                reasons.append("short: selling more %s than you hold (%s shares) is %s; short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held), what))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window
        max_loss = risk_ps = rr = stop = target = ch_stop = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop = _f((leg or {}).get("trigger_price")) if isinstance(leg, dict) else None
            plan_stop = _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("target")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > PAISA:
                warns.append("the stop leg's trigger %s differs from the plan's stop %s: the stop leg is the one "
                             "the exchange holds" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not at the exchange: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a stop_leg "
                                 "from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    rates = rates_for(plan, c)
                    risk_p = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_p / 100.0
                    shares = q or 0
                    ch_stop = 0.0
                    if count_charges and shares:
                        buy_p, sell_p = (ref, stop) if long else (stop, ref)
                        ch_stop = charges(shares, buy_p, sell_p, rates)["total"]
                    max_loss = round(shares * risk_p / 100.0 + ch_stop, 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + PAISA:
                        later.append("loss: %s shares x %s risk a share%s = %s at the stop, over your %s max loss "
                                     "per trade" % (_num(shares), _p(risk_ps),
                                                    (" + %s charges" % inr(ch_stop)) if count_charges else "",
                                                    inr(max_loss), inr(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            reward_p = int(round(abs(target - ref) * 100))
                            rr = round(reward_p / float(risk_p), 2)
                            if count_charges and shares and max_loss > 0:
                                buy_p, sell_p = (ref, target) if long else (target, ref)
                                net = shares * reward_p / 100.0 - charges(shares, buy_p, sell_p, rates)["total"]
                                rr = round(net / max_loss, 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f%s (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, " net of charges" if count_charges else "",
                                                             _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (inr(used), inr(max_loss), inr(used + max_loss),
                                                       inr(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s%s%s, reward:risk %s%s"
                            % (inr(max_loss), (" incl. %s charges" % inr(ch_stop)) if count_charges else "",
                               (" (limit %s)" % inr(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               inr(loss_today + batch_loss), inr(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if need is not None:
                    acct["cash"] -= need
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": ("%s shares" % _num(q)) if q is not None else "? shares",
            "product": product, "order_type": typ, "stop_leg": isinstance(leg, dict),
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "charges_at_stop": ch_stop,
            "max_loss": max_loss, "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                      r["product"] or "?", r["order_type"] or "?", r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], inr(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}, {SYM: prev close}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of. A quote in another currency is skipped."""
    if not isinstance(pdoc, dict):
        return {}, {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}, {}
    out, times, prev = {}, {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        d = detail.get(s) or {}
        if str(d.get("currency") or pdoc.get("currency") or "INR").upper() != "INR":
            continue
        out[s] = float(p)
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
        if _f(d.get("prev_close")):
            prev[s] = float(d["prev_close"])
    return out, times, prev


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--series", help="EQUITY_L.csv or sec_list.csv from NSE: only EQ series pass "
                                     "(sec_list.csv also gives the circuit band)")
    ap.add_argument("--now", help="pretend it is this India time, e.g. \"2026-10-28 09:45\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in India)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-28")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times, prev = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        series = load_series(a.series) if a.series else None
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, series, prev)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
