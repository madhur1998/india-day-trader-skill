#!/usr/bin/env python3
"""Time Machine for trader-timemachine. Standard library only.

Replays a planned NSE day trade before you risk a rupee: 10,000 simulated runs
of the rest of the session from your entry, plus the same rule on every past
session in the data.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:10            # stop each replay at your flat-by time
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol RELIANCE --date 2026-10-29 --entry 1174.3 \\
        --stop 1163.3 --target 1185.3 --entry-time "9:40 AM"
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the reel's sim.py, same algorithm, same random draws):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, <SYMBOL>.NS, range=60d
     interval=5m; one request). The NSE session is 9:15 to 15:30 India time
     (IST), 75 five-minute bars, with no early closes. Yahoo's NSE bars stop
     early, though: the 15:20 bar is never there and the 15:25 bar, when it is,
     is the closing-price print, not trading. So a replay ends at --until, 15:20
     at the latest (also about when Zerodha squares off an MIS position), and
     a past session counts as FULL when it has every bar from 9:15 to --until.
     Sessions with a gap, Muhurat sessions and today are left out.
  2. The pool: for every bar slot 1..last (the --until bar), each past
     session's bar as ratios to the previous bar's close:
     (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..last, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default 15:20).
     "green" = target first, or above the entry at --until (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
     R after charges: each outcome also pays the round-trip intraday charges
     (brokerage, STT, exchange, SEBI, stamp duty, GST; charges() below) for the
     plan's "qty", or for a nominal position of about Rs 1 lakh when the plan
     has none, divided by qty x risk. The plan's "charges_rates" override the
     default rates.
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     full sessions, first close beyond the range = entry, same stop-first rule.
     Stops and targets are rounded onto the NSE tick (stop away from the entry,
     target up for a buy).

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even AFTER CHARGES: loss / (win + loss), where win is the target's
R net of charges and loss is 1R plus the charges at the stop. Without charges
that is 1 / (1 + R): 50 % at 1R, 33 % at 2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (break-even 50 %) / "Close to break-even"
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), and the average R after charges when its sign goes
against the lean. When the charges eat the whole target the verdict says so.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 full sessions, the plan's day is not a
regular NSE session, or the entry bar is not in the data. Same plan, same data
and same --seed give the same numbers on any machine. A simulation built from
the stock's own recent bars is not a prediction: results, news, circuit limits
and halts can do what the last 60 days never did.

Context: one more request for the Nifty 50 (^NSEI) 5-minute bars, to say where
the index and the stock stood on the day at the entry bar (skip with --no-index).

Data: https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=60d&interval=5m
(unofficial and free, can lag; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead; a folder
is searched for <SYM>-60d5m.json(.gz) or chart-<SYM>-60d-5m.json(.gz) ("&" and
"^" dropped from the name: M&M -> MM, ^NSEI -> NSEI).
Times are India time (IST, UTC+5:30); --now "2026-10-29 10:15" pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.request

PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 75    # 9:15 to 15:30 in 5-minute bars
OPEN_MIN = 9 * 60 + 15
LAST_SLOT = 72       # the 15:15 bar: Yahoo has no NSE bars after it (see the docstring)
UNTIL_MAX = OPEN_MIN + 5 * (LAST_SLOT + 1)   # 15:20, the latest --until
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
NOMINAL_INR = 100000  # position size for the charges when the plan has no qty: about Rs 1 lakh
INDEX = "^NSEI"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


class TMError(Exception):
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


def parse_now(text, error=TMError):
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


# ---------------------------------------------------------------- clock helpers

def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'9:45 AM', '9:45am', '15:10' or '3:10' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants an India time like 9:45 AM or 15:10, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


# ---------------------------------------------------------------- data

def chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (host, yahoo_symbol(sym))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym):
    """One request for 60 days of 5-min bars. query2 first, one retry after a 429,
    then query1 on 429 or 5xx."""
    last = None
    name = nse_symbol(sym)
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = chart_url(sym, host)
            try:
                return _get(url)
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise TMError("Yahoo has no chart for %s (HTTP 404): check the NSE symbol" % name)
                raise TMError("Yahoo answered HTTP %d for %s" % (e.code, name))
            except urllib.error.URLError as e:
                raise TMError("could not reach Yahoo: %s" % e.reason)
            except OSError as e:
                raise TMError("could not reach Yahoo: %s" % e)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, name))


def file_key(sym):
    """The name a symbol has in an offline folder: M&M -> MM, ^NSEI -> NSEI."""
    return nse_symbol(sym).replace("&", "").replace("^", "")


def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding
    <SYM>-60d5m.json(.gz) or chart-<SYM>-60d-5m.json(.gz)."""
    if os.path.isdir(path):
        key = file_key(sym)
        names = ["%s-60d5m.json" % key, "chart-%s-60d-5m.json" % key]
        for name in [n + ext for n in names for ext in ("", ".gz")]:
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) or chart-%s-60d-5m.json(.gz) in %s" % (key, key, path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def chart_result(text, sym):
    name = nse_symbol(sym)
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % name)
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % name)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (name, err.get("description") or "unknown symbol"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price, bars off the 5-minute grid
    (the live tick, e.g. 13:35:17), bars not complete yet (now < start + 5 min,
    where now is the earlier of --now and the data's own regularMarketTime), and
    bars outside 9:15 to 15:30 India time (a Muhurat evening session among them)."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = ts_to_ist(ts)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before, last=LAST_SLOT):
    """Full sessions (every bar from 9:15 to bar `last`) strictly before the plan's
    date, oldest first, as lists of bars 0..last, and the ones that were left out
    (a gap, or a Muhurat session)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if d in NSE_MUHURAT:
            short.append({"date": d, "bars": len(s), "reason": "Muhurat session"})
        elif all(i in s for i in range(last + 1)):
            full.append((d, [s[i] for i in range(last + 1)]))
        else:
            short.append({"date": d, "bars": len(s), "reason": "gap"})
    return full, short


# ---------------------------------------------------------------- charges

def trade_charges(qty, entry, exit_price, long, rates):
    """Round-trip charges in rupees: a buy enters at the entry, a sell at the exit."""
    buy, sell = (entry, exit_price) if long else (exit_price, entry)
    return charges(qty, buy, sell, rates)["total"]


def nominal_qty(price):
    """Whole shares worth about Rs 1 lakh at this price (at least 1)."""
    return max(1, int(NOMINAL_INR // price))


# ---------------------------------------------------------------- the simulation (sim.py)

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    n = len(sessions[0][1])
    pool = [[] for _ in range(n)]
    for _, D in sessions:
        for i in range(1, n):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy",
             qty=None, rates=None):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the reel's sim.py.
    With qty, every R is also given net of the round-trip charges for qty shares."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)
    top = len(pool) - 1

    def slot(rng, i):
        return i if window == 0 else min(top, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    out = {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }
    if qty:
        per_r = qty * risk
        at = {"target": trade_charges(qty, entry, target, long, rates),
              "stop": trade_charges(qty, entry, stop, long, rates)}
        net = [R - (at[o] if o in at else trade_charges(qty, entry, p, long, rates)) / per_r
               for R, o, p in zip(Rs, outcomes, closes)]
        out.update({
            "avg_R_net": round(statistics.mean(net), 3),
            "r_at_target_net": round(r_target - at["target"] / per_r, 4),
            "r_at_stop_net": round(-1.0 - at["stop"] / per_r, 4),
            "charges": {"qty": qty, "at_target": at["target"], "at_stop": at["stop"]},
        })
    return out


# ---------------------------------------------------------------- stop rules and the history replay

def tick_away(x, long, tick=None):
    """Round a stop onto the NSE tick, away from the entry: down for a buy, up for a sell."""
    return round_tick(x, "down" if long else "up", tick)


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list,
    to the paisa: Yahoo's 1156.199951171875 is 1156.20, or the tick rounding below
    would take it down a whole tick."""
    rows = [D[i] for i in range(n)]
    return round(max(b["h"] for b in rows), 2), round(min(b["l"] for b in rows), 2)


def rule_stop(rule, oh, ol, entry, long, tick=None):
    r = rule["stop_rule"]
    if r == "or_mid":
        return tick_away((oh + ol) / 2, long, tick)
    if r in ("or_low", "or_high"):
        return tick_away(ol if long else oh, long, tick)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), long, tick)


def history_replay(sessions, rule, last, side="buy", qty=None, rates=None, tick=None):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close beyond it (up to the --until bar), then stop first, then
    target, bar by bar, else the close at --until. R_net pays the round-trip charges
    for qty shares (the plan's, else about Rs 1 lakh at that day's entry)."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = round(D[kk]["c"], 2)
        s = rule_stop(rule, oh, ol, e, long, tick)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = round_tick(e + rule["target_r"] * risk if long else e - rule["target_r"] * risk,
                       "up" if long else "down", tick)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        exit_price = t if oc == "target" else s if oc == "stop" else cl
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        q = qty or nominal_qty(e)
        R_net = R - trade_charges(q, e, exit_price, long, rates) / (q * risk)
        trades.append({"date": d, "time": clock12(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2), "R_net": round(R_net, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "avg_R_net": round(statistics.mean(x["R_net"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now_utc):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or to_ist(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m", "qty": a.qty}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        if a.qty:
            raw = dict(raw, qty=a.qty)
        src = a.plan
    return normalize_plan(raw, src)


def _plan_extras(raw):
    """The optional qty, charges_rates and tick_size, checked."""
    qty = raw.get("qty")
    if qty is not None:
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty != int(qty) or qty < 1:
            raise TMError("the plan's qty must be a whole number of shares, got %r" % qty)
        qty = int(qty)
    rates = raw.get("charges_rates")
    if rates is not None:
        if not isinstance(rates, dict):
            raise TMError("the plan's charges_rates must be an object like {\"brokerage_max\": 20}")
        unknown = sorted(set(rates) - set(CHARGES))
        if unknown:
            raise TMError("the plan's charges_rates has unknown key(s): %s (known: %s)"
                          % (", ".join(unknown), ", ".join(sorted(CHARGES))))
        for k, v in rates.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise TMError("the plan's charges_rates.%s must be a number of 0 or more, got %r" % (k, v))
        rates = dict(rates)
    tick = raw.get("tick_size")
    if tick is not None:
        if isinstance(tick, bool) or not isinstance(tick, (int, float)) or not tick > 0:
            raise TMError("the plan's tick_size must be a positive number, got %r" % tick)
        tick = float(tick)
    return qty, rates, tick


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = nse_symbol(str(raw.get("symbol") or ""))
    if not re.match(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$", sym) or not re.search(r"[A-Z]", sym):
        raise TMError("the plan needs an NSE symbol like RELIANCE or M&M, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        day = datetime.date.fromisoformat(str(raw.get("date") or ""))
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-29, got %r" % raw.get("date"))
    date = day.isoformat()
    ok, why = trading_day(day)
    if date in NSE_MUHURAT:
        raise TMError("%s is a Muhurat trading day: the time machine replays the regular 9:15 to 15:30 session "
                      "only, and a one-hour evening session has no past sessions to draw from" % date)
    if not ok:
        raise TMError("%s is not an NSE trading day (%s)" % (date, why))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 9:40 AM)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 9:40 AM) or entry_bar")
    if not 0 <= k <= LAST_SLOT - 1:
        raise TMError("the entry must close between 9:20 AM and 3:15 PM to leave bars to replay "
                      "(got %s)" % clock12(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= SESSION_BARS * 5:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    qty, rates, tick = _plan_extras(raw)
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock12(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "qty": qty, "charges_rates": rates, "tick_size": tick, "from": src,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    tick = plan.get("tick_size")
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(tick_away((oh + ol) / 2, long, tick) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(tick_away(ol if long else oh, long, tick) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the Nifty 50 context

def day_change(days, date, k):
    """(close of bar k on date, the previous session's last close) from {date: {slot: bar}}, or None."""
    bar = days.get(date, {}).get(k)
    prev = [d for d in days if d < date and days[d]]
    if not bar or not prev:
        return None
    p = days[max(prev)]
    return bar["c"], p[max(p)]["c"]


def context(index_text, plan, days, now_utc):
    """Where the Nifty 50 and the stock stood on the day at the entry bar. Never fails the run."""
    out = {"index": INDEX}
    if index_text is None:
        return None
    try:
        _, ibars, _ = parse_bars(chart_result(index_text, INDEX), now_utc)
    except TMError as e:
        out["note"] = str(e)
        return out
    idx = day_change(split_sessions(ibars), plan["date"], plan["entry_bar"])
    if not idx:
        out["note"] = "no Nifty 50 bar for the entry in the data"
        return out
    out.update({"at_entry": round(idx[0], 2), "prev_close": round(idx[1], 2),
                "change_pct": round(100.0 * (idx[0] / idx[1] - 1), 2)})
    own = day_change(days, plan["date"], plan["entry_bar"])
    if own:
        out["symbol_change_pct"] = round(100.0 * (own[0] / own[1] - 1), 2)
    return out


# ---------------------------------------------------------------- the run

def _until12(mc):
    t = mc.get("until") or clock24(UNTIL_MAX)
    return clock12(int(t[:2]) * 60 + int(t[3:]))


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R after charges (1 / (1 + R)
    when the result has no charges). None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    share = pt / (pt + ps)
    win, loss = mc.get("r_at_target_net"), mc.get("r_at_stop_net")
    if win is None or loss is None:
        r = mc.get("r_at_target") or 1.0
        return share - 1.0 / (1.0 + r), share, 1.0 / (1.0 + r)
    even = 1.0 if win <= 0 else -loss / (win - loss)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until12(mc), _until12(mc))
    r = mc.get("r_at_target") or 1.0
    net = mc.get("r_at_target_net")
    if net is not None and net <= 0:
        return ("The charges eat the target: at %gR the target nets %+.2fR after round-trip charges, so even "
                "the target loses money." % (round(r, 2), net))
    d, share, even = lv
    one_r = abs(r - 1.0) < 0.005 and abs(even - 0.5) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    after = " (%.2fR after charges)" % net if net is not None else ""
    return ("%s: at %gR%s the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), after, 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until12(mc), _until12(mc)))
    lv = lean(mc)
    net = mc.get("avg_R_net") is not None
    avg = mc.get("avg_R_net") if net else mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R%s is %+.3f all the same: the replays that hit neither pull it the other way."
                     % (" after charges" if net else "", avg))
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo",
                 index_text=None):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    result = chart_result(text, sym)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = nse_symbol(str(meta.get("symbol") or ""))
    if msym and msym != sym:
        raise TMError("the data is for %s but the plan is for %s" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    until_min = UNTIL_MAX if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > UNTIL_MAX:
        raise TMError("--until must be on the 5-minute grid and no later than %s: Yahoo has no NSE bars after "
                      "that, and Zerodha squares off MIS positions at about 15:20" % clock12(UNTIL_MAX))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock12(until_min),
                                                                             plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    if not eb["l"] - 0.01 <= plan["entry"] <= eb["h"] + 0.01:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    sessions, short = full_sessions(days, plan["date"], last)
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    qty = plan.get("qty") or nominal_qty(plan["entry"])
    rates = plan.get("charges_rates")
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"],
                  qty, rates)
    mc["charges"].update(qty_from="plan" if plan.get("qty") else "nominal",
                         rates="plan" if rates else "default")
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0],
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], plan.get("qty"), rates, plan.get("tick_size"))
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or sym, "interval": "5m", "range": "60d",
                 "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "context": context(index_text, plan, days, now_utc),
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: results, "
                 "news, circuit limits and halts can do things those sessions never did. Same seed, same numbers."
                 % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = clock12(int(m["until"][:2]) * 60 + int(m["until"][3:]))
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    ch = m.get("charges") or {}
    lines = [
        "%s %s, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (p["symbol"], p["side"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
    ]
    if ch:
        lines += [
            "  Average R: %+.3f gross, %+.3f after charges (R = the %.2f risk: %sR at the target, -1R at the stop)."
            % (m["avg_R"], m["avg_R_net"], p["risk_per_share"], ("+%g" % m["r_at_target"])),
            "  Charges (intraday round trip) on %d shares%s: %s at the target, %s at the stop. After charges "
            "the target is %+.2fR and the stop %+.2fR."
            % (ch["qty"], " (the plan's qty)" if ch.get("qty_from") == "plan" else
               " (a nominal position of about Rs 1 lakh; give the plan a qty for your own size)",
               inr(ch["at_target"]), inr(ch["at_stop"]), m["r_at_target_net"], m["r_at_stop_net"]),
        ]
    else:
        lines.append("  Average R: %+.3f (R = the %.2f risk: %sR at the target, -1R at the stop)."
                     % (m["avg_R"], p["risk_per_share"], ("+%g" % m["r_at_target"])))
    cx = doc.get("context")
    if cx and cx.get("at_entry") is not None:
        own = cx.get("symbol_change_pct")
        lines.append("  Context at the %s entry: Nifty 50 %.2f, %+.2f %% on the day%s."
                     % (p["entry_time"], cx["at_entry"], cx["change_pct"],
                        ("; %s %+.2f %%" % (p["symbol"], own)) if own is not None else ""))
    elif cx:
        lines.append("  Nifty 50 context: not available (%s)." % cx.get("note", "no data"))
    lines.append("")
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f "
                         "gross, %+.3f after charges."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"], h["avg_R_net"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d session(s) with a gap before %s (or a Muhurat session): %s."
                     % (len(doc["data"]["short_sessions_left_out"]), until,
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        t = times[j]
        hh, mm = int(t[:2]), t[3:]
        lab = "%d:%s" % (hh % 12 or 12, mm)
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def index_text_for(a):
    """The Nifty 50 bars for the context line, or None. Never fails the run."""
    if a.no_index:
        return None
    try:
        if a.offline:
            return read_offline(a.offline, INDEX) if os.path.isdir(a.offline) else None
        _sleep(PAUSE)
        return fetch_chart(INDEX)
    except TMError:
        return None


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["symbol"]), "yahoo"
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source, index_text_for(a))


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned NSE day trade (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: NSE symbol, e.g. RELIANCE")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, India time)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, e.g. "9:40 AM"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--qty", type=int, help="shares, for the charges (default: the plan's qty, else about Rs 1 lakh)")
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this time (default and latest 15:20)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), or a "
                                                         "folder of them, no network")
        p.add_argument("--no-index", action="store_true", help="skip the Nifty 50 context line (one request)")
        p.add_argument("--now", help='pretend it is this time ("2026-10-29 10:15" India time, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    if a.qty is not None and a.qty < 1:
        print("timemachine.py: --qty must be 1 or more", file=sys.stderr)
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
