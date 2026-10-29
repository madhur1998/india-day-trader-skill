"""The NSE reference block: clock, calendar, tick size and charges.

Every skill folder stays self-contained, so each tool carries its own copy of
the parts of this block it needs (the US version did the same with the NYSE
clock). This file is the one source those copies are taken from, and
tests/test_nse.py checks that every copy still matches it. Change it here
first, then paste.

Standard library only, Python 3.8. No zoneinfo: India has one offset all year
(UTC+5:30, no daylight saving), so the clock is a fixed shift.
"""

import datetime
import re

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


def parse_now(text, error=ValueError):
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
