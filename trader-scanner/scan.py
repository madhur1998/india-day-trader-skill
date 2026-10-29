#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan thousands of NSE charts for setups. Standard library only.

    python3 scan.py universe                          # NSE lists -> trader/universe.txt
    python3 scan.py universe --index nifty500         # only the Nifty 500 (or nifty50)
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --index nifty50             # or --symbols RELIANCE,SBIN,M&M, --watchlist FILE, --limit 200
    python3 scan.py orb                               # after 9:35 AM IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 AM IST
    python3 scan.py orb --offline charts/ --now "2026-10-28 10:15"

universe  Downloads NSE's equity list (EQUITY_L.csv) and the price-band list
          (sec_list.csv) from nsearchives.nseindia.com and keeps a row when:
            1. its series is EQ (BE and BZ are trade-to-trade: delivery only,
               no intraday; anything else is not a plain equity)
            2. its price band is not in --skip-bands (2 and 5 by default: a
               2 % or 5 % circuit stops a day trade cold). "No Band" (the F&O
               stocks), 10 and 20 stay. A stock missing from sec_list.csv is
               kept with band "?".
            3. with --index nifty50 / nifty500: it is in NSE's index list
          Writes one SYMBOL|Name|Band a line.

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (Rs 50) and price x
          regularMarketVolume >= --min-turnover (Rs 10 crore = 1e8), plus an
          optional --min-volume in shares. Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the 3:30 PM close.

orb       Opening-range breakouts, 5-minute bars on the 9:15 grid (75 bars a
          session, 9:15 to 15:25). The range is the first --or-minutes (15) of
          the session. Stage 1: spark closes for every symbol (5-minute bars,
          20 a request); a chart goes on if a completed bar after the range
          closed at or above the highest close of the range, or if a bar after
          the range is missing (it could be the breakout). Stage 2: each
          candidate's v8 chart (real highs and lows): range high = the highest
          high of the range bars, a hit = the first completed bar to CLOSE
          above it, at that bar's close time. Stage 2 fetches at most
          --max-confirm (200) charts, the most traded candidates first (rupees
          traded so far), and says so when it stops short. A chart is usable
          when every range bar is there and at most MAX_GAPS (3) bars after it
          are missing: Yahoo's NSE feed drops a few bars (15:15 to 15:25 most
          days). Floors: --min-price 50 (last close), --min-turnover 0.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 13:35:17) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are India time (IST, UTC+5:30 all
year). --now pins the clock ("2026-10-28 10:15", IST; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. NSE's lists come from
nsearchives.nseindia.com only (plain GETs of public files; never the
www.nseindia.com API). A polite pause between requests (--pause). Partial
results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with EQUITY_L.csv and sec_list.csv (and ind_nifty50list.csv /
ind_nifty500list.csv for --index); daily and orb take a folder of v8 chart
files or spark responses, or one file that is a spark response, a v8 chart,
or a bundle {"RELIANCE": <v8 json>, ...}. .json.gz is fine. With daily or orb,
--lists DIR reads the --index list from a folder instead of NSE.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; india-day-trader-skill/3.0)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NSE_ARCHIVES = "https://nsearchives.nseindia.com/content/"
NSE_FILES = {  # key -> path under NSE_ARCHIVES; offline, the basename in the --offline / --lists folder
    "equity": "equities/EQUITY_L.csv",
    "bands": "equities/sec_list.csv",
    "nifty50": "indices/ind_nifty50list.csv",
    "nifty500": "indices/ind_nifty500list.csv",
}
INDEX_NAME = {"nifty50": "Nifty 50", "nifty500": "Nifty 500"}
SKIP_BANDS = ("2", "5")  # circuit bands (in %) the universe leaves out by default
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_GAPS = 3        # missing 5-minute bars after the range a chart may have and stay usable
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
CRORE = 1e7
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_UNTIL = "14:45"  # no_entries_after when the config has none
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

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

OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
IST = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


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


def parse_now(text, error=ScanError):
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


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an India time like 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


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


def calendar_warning(d):
    """A plain line when the day is past the holiday list this tool carries."""
    if d.year not in CALENDAR_YEARS:
        return ("The NSE holiday list for %d is not in this tool yet: a weekday may still be a holiday. "
                "Check NSE's list." % d.year)
    return None


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- money

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


def crore(amount):
    """1e8 -> 'Rs 10 crore'; under a crore -> inr()."""
    if amount < CRORE:
        return inr(amount)
    return "Rs %s crore" % _n(round(amount / CRORE, 2))


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


# ---------------------------------------------------------------- network

def http_get(url, timeout=20, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def nse_text(key, folder=None):
    """One of NSE_FILES: from `folder` (by basename) or from nsearchives.nseindia.com."""
    path = NSE_FILES[key]
    if folder:
        p = os.path.join(folder, os.path.basename(path))
        if not os.path.exists(p):
            raise ScanError("no %s in %s" % (os.path.basename(path), folder))
        with open(p, encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    try:
        text = http_get(NSE_ARCHIVES + path, timeout=60, ua=UA)
    except urllib.error.HTTPError as e:
        raise ScanError("nsearchives.nseindia.com answered HTTP %d for %s" % (e.code, os.path.basename(path)))
    except (urllib.error.URLError, OSError) as e:
        raise ScanError("could not reach nsearchives.nseindia.com: %s" % getattr(e, "reason", e))
    return text.lstrip("﻿")


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


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(yahoo_symbol(s) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_symbol(sym), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc):
    """Spark JSON -> {NSE SYMBOL: series}. Symbols Yahoo does not know are simply
    missing from the answer. Yahoo's M&M.NS comes back as ours, M&M."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        out[nse_symbol(ysym)] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors. A v8 chart is filed under
    the symbol in its meta (RELIANCE.NS -> RELIANCE), else under the file or key name."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        try:
            sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
        except (AttributeError, KeyError, TypeError, IndexError):
            sym = name_hint
        if not sym:
            raise ScanError("an offline v8 chart without a symbol: name the file <SYMBOL>.json")
        sym = nse_symbol(sym)
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of .json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

def read_csv(text, need, what):
    """CSV text -> rows with stripped keys and values (EQUITY_L.csv's header
    has leading spaces, " SERIES"). `need` = the columns that must be there."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    hdr = set((k or "").strip() for k in (rows[0].keys() if rows else []))
    if not rows or not set(need) <= hdr:
        raise ScanError("not NSE's %s (want columns %s)" % (what, ", ".join(need)))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def parse_equity_list(text):
    """EQUITY_L.csv -> rows (SYMBOL, NAME OF COMPANY, SERIES, ...)."""
    return read_csv(text, ("SYMBOL", "NAME OF COMPANY", "SERIES"), "EQUITY_L.csv")


def parse_bands(text):
    """sec_list.csv -> {SYMBOL: band} for the EQ series. Band is "No Band"
    (F&O stocks: no daily band), "2", "5", "10" or "20" (percent)."""
    return {r["Symbol"].upper(): r["Band"] for r in read_csv(text, ("Symbol", "Series", "Band"), "sec_list.csv")
            if r["Series"] == "EQ"}


def parse_index(text, eq_only=True):
    """ind_nifty50list.csv / ind_nifty500list.csv -> [SYMBOL] (series EQ, unless eq_only is off)."""
    return [r["Symbol"].upper() for r in read_csv(text, ("Symbol",), "index list")
            if not eq_only or r.get("Series", "EQ") == "EQ"]


def classify(row, bands, skip_bands=SKIP_BANDS, index=None):
    """One EQUITY_L.csv row -> 'keep' or the rule that drops it."""
    series = row.get("SERIES", "")
    if series != "EQ":
        if series in ("BE", "BZ"):
            return "series %s (trade-to-trade, no intraday)" % series
        return "series %s" % (series or "blank")
    sym = row.get("SYMBOL", "").upper()
    if index is not None and sym not in index:
        return "not in the index"
    band = bands.get(sym) if bands is not None else None
    if band in skip_bands:
        return "band %s %%" % band
    return "keep"


def build_universe(equity_text, bands_text=None, index_text=None, skip_bands=SKIP_BANDS):
    """-> (kept [(SYM, name, band)], counts, index_missing [SYM]). index_missing
    are index names that are not EQ rows of EQUITY_L.csv (a REIT, a rename)."""
    rows = parse_equity_list(equity_text)
    bands = parse_bands(bands_text) if bands_text is not None else None
    index = set(parse_index(index_text, False)) if index_text is not None else None
    kept, counts = {}, {"rows": len(rows)}
    for r in rows:
        why = classify(r, bands, skip_bands, index)
        counts[why] = counts.get(why, 0) + 1
        if why == "keep":
            sym = r["SYMBOL"].upper()
            band = "?" if bands is None else bands.get(sym, "?")
            kept[sym] = (" ".join(r["NAME OF COMPANY"].split()), band)
    eq = set(r["SYMBOL"].upper() for r in rows if r.get("SERIES") == "EQ")
    missing = sorted(index - eq) if index is not None else []
    return sorted((s, n, b) for s, (n, b) in kept.items()), counts, missing


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (the same math as the reel's refs/scan.py)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_turnover, min_volume=0):
    """Pure. data = {SYM: series}. Turnover = price x volume, in rupees.
    A chart whose last close is older than the latest close of any chart is
    stale (Yahoo's NSE daily series skips a day now and then) and is not scanned.
    Returns (hits, counts, last_date)."""
    hits, dates, ready = [], {}, []
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "stale": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        floors = min_turnover > 0 or min_volume > 0
        if (price is None or price < min_price or (floors and vol is None)
                or (vol is not None and (vol < min_volume or price * vol < min_turnover))):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        dates[bars[-1][0]] = dates.get(bars[-1][0], 0) + 1
        ready.append((sym, bars, price, vol))
    last_date = max(dates) if dates else None
    for sym, bars, price, vol in ready:
        if bars[-1][0] < last_date:
            counts["stale"] += 1
            continue
        counts["scanned"] += 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": bars[-1][0].isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(vol) if vol is not None else None,
                         "turnover": int(round(price * vol)) if vol is not None else None})
    hits.sort(key=lambda h: (-(h["turnover"] or 0), h["symbol"]))  # most rupees traded first
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    """Start of the k-th 5-minute bar of the session (k = 0 is 9:15)."""
    s = session(day)
    return datetime.datetime.combine(day, s[0] if s else OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def slots_done(day, cutoff):
    """How many 5-minute bars of the session are complete at `cutoff` (75 a full day)."""
    s = session(day)
    end = min(cutoff, datetime.datetime.combine(day, s[1] if s else CLOSE))
    return max(0, int((end - slot(day, 0)).total_seconds() // (BAR_MIN * 60)))


def session_bars(series, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive India time),
    on the grid, with a close. Sorted, one per slot; "k" is the slot (0 = 9:15)."""
    s = session(day)
    open_t, close_t = s if s else (OPEN, CLOSE)
    out = {}
    hs, ls = series.get("h"), series.get("l")
    v = series.get("v")
    first = slot(day, 0)
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        t = ts_to_ist(ts)
        if t.date() != day or t.second or t.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if t.time() < open_t or t.time() >= close_t:
            continue  # the pre-open, or the 3:30 PM closing print
        if t + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        out[t] = {"start": t, "k": int((t - first).total_seconds() // (BAR_MIN * 60)), "c": float(c),
                  "h": None if not hs or hs[i] is None else float(hs[i]),
                  "l": None if not ls or ls[i] is None else float(ls[i]),
                  "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def gaps(bars, day, cutoff):
    """Complete slots up to the cutoff that have no bar."""
    return slots_done(day, cutoff) - len(bars)


def usable(bars, day, cutoff, n_range, max_gaps=MAX_GAPS):
    """Every range bar is there and at most max_gaps bars after it are missing.
    Yahoo's NSE feed leaves a few bars empty (15:15 to 15:25 most days, a
    midday bar now and then); more than that is a halt or a thin stock."""
    if slots_done(day, cutoff) < n_range or len(bars) < n_range:
        return False
    if [b["k"] for b in bars[:n_range]] != list(range(n_range)):
        return False
    return gaps(bars, day, cutoff) <= max_gaps


def orb_candidate(bars, n_range, day=None, cutoff=None):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range
    close). A chart with a missing bar after the range goes on too: the missing bar
    may be the breakout, and stage 2 reads the v8 chart."""
    top = max(b["c"] for b in bars[:n_range])
    if any(b["c"] >= top for b in bars[n_range:]):
        return True
    return day is not None and gaps(bars, day, cutoff) > 0


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for i in range(n_range, len(bars)):
        b = bars[i]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            hit = {"price": round(b["c"], 2), "at": clock_label(closes_at),
                   "bar_start": b["start"].strftime("%H:%M"), "bar_index": b["k"],
                   "or_high": round(oh, 2), "or_low": round(ol, 2),
                   "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
            if b["k"] != i:
                hit["missing_before"] = b["k"] - i  # Yahoo had no bar for that many slots before it
            return hit
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): NSE is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day)
    open_dt = slot(day, 0)
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("NSE closed at %s IST on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs %s to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(open_dt), clock_label(range_end),
                           clock_label(first_close), clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb on the next session after %s, "
                        "or replay today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                               clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_turnover=0.0, confirm=None,
             max_confirm=None, min_volume=0):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (rupees traded so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        turnover = bars[-1]["c"] * vol
        if bars[-1]["c"] < min_price or turnover < min_turnover or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range, day, cutoff):
            cands.append((sym, bars, turnover))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff)
            if not usable(full, day, cutoff, n_range):
                failed[sym] = "the v8 chart is missing bars"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)))
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif a.index:
        syms = parse_index(nse_text(a.index, a.lists))
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols or --index nifty50" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        s = nse_symbol(s)
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def parse_bands_flag(text):
    """--skip-bands "2,5" -> ("2", "5"); "none" -> ()."""
    t = (text or "").strip().lower()
    if t in ("", "none", "no"):
        return ()
    out = []
    for b in re.split(r"[,\s]+", t):
        b = b.rstrip("%")
        if b not in ("2", "5", "10", "20"):
            raise ScanError("--skip-bands wants NSE bands from 2, 5, 10, 20 (or none), got %s" % b)
        out.append(b)
    return tuple(out)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    skip = parse_bands_flag(a.skip_bands)
    texts = {}
    for k, key in enumerate(["equity", "bands"] + ([a.index] if a.index else [])):
        if k and a.pause and not a.offline:
            SLEEP(a.pause)
        texts[key] = nse_text(key, a.offline)
    kept, counts, missing = build_universe(texts["equity"], texts["bands"], texts.get(a.index), skip)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name, band in kept:
                fh.write("%s|%s|%s\n" % (sym, name, band))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "index": a.index,
                          "skip_bands": list(skip), "dropped": dropped, "index_not_eq": missing,
                          "out": None if a.no_write else a.out, "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("NSE equity list (EQUITY_L.csv): %s rows%s." % (
            "{:,}".format(counts.get("rows", 0)), (", %s preset" % INDEX_NAME[a.index]) if a.index else ""))
        print("Kept %s EQ-series stocks%s%s." % (
            "{:,}".format(len(kept)), (" in the %s" % INDEX_NAME[a.index]) if a.index else "",
            (", skipping the %s %% price bands" % " and ".join(skip)) if skip else ", every price band"))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                          for k, v in sorted(dropped.items(), key=lambda kv: (-kv[1], kv[0]))) + ".")
        if missing:
            print("In the %s list but not an EQ row of EQUITY_L.csv: %s." % (INDEX_NAME[a.index], ", ".join(missing)))
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name|Band a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_turnover, a.min_volume)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    local = to_ist(now)
    warn = calendar_warning(local.date())
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "exchange": "NSE",
           "filters": {"min_price": a.min_price, "min_turnover_inr": a.min_turnover, "min_volume": a.min_volume},
           "note": NOTE, "hits": hits}
    if a.index:
        doc["index"] = a.index
    if warn:
        doc["warning"] = warn
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Daily scan, %s IST, closes through %s (Yahoo spark, NSE, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    if warn:
        print(warn)
    floor = "%s+ and %s+ traded" % (inr(a.min_price), crore(a.min_turnover))
    if a.min_volume:
        floor += ", %s+ shares" % "{:,}".format(a.min_volume)
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (%s)%s; %s scanned." % (
        "{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
        (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
        "{:,}".format(counts["fetched"] - counts["illiquid"]), floor,
        ((", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "")
        + ((", %d had no bar for %s (stale)" % (counts["stale"], last_date.isoformat())) if counts["stale"] else ""),
        "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (rupees traded, price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-10s %14s  RSI %5.1f  %s" % (h["symbol"], inr(h["price"]), h["rsi"],
                                               ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL.")
    return 0 if hits else 1


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or DEFAULT_UNTIL, "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    symbols = read_symbols(a, offline_syms)
    confirm = None
    if not a.offline:
        today = to_ist(utc_now()).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            return parse_chart(yahoo_get(chart_path(sym, rng, "%dm" % BAR_MIN)), sym)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_turnover,
                                     confirm, a.max_confirm, a.min_volume)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    warn = calendar_warning(day)
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "exchange": "NSE", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_turnover_inr": a.min_turnover, "min_volume": a.min_volume},
           "note": NOTE, "hits": hits}
    if a.index:
        doc["index"] = a.index
    if warn:
        doc["warning"] = warn
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    n_range = a.or_minutes // BAR_MIN
    print("Opening-range breakout scan, %s %s IST: %d-minute range (%s to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, 0)),
             clock_label(slot(day, n_range))))
    if warn:
        print(warn)
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / turnover floor)" % "{:,}".format(counts["illiquid"]))
             if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-10s %14s  range %.2f to %.2f  %+.2f%% over%s" % (
                h["at"], h["symbol"], inr(h["price"]), h["or_low"], h["or_high"], h["pct_over"],
                ("  (%d bar%s missing before it)" % (h["missing_before"], "" if h["missing_before"] == 1 else "s"))
                if h.get("missing_before") else ""))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL and /trader-entry SYMBOL.")
    return 0 if hits else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): NSE daily setups and "
                                             "opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download NSE's equity and band lists -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--index", choices=sorted(INDEX_NAME), help="only this index (nifty50 or nifty500)")
    p.add_argument("--skip-bands", default=",".join(SKIP_BANDS),
                   help="price bands (%%) to leave out, e.g. 2,5 (default) or none")
    p.add_argument("--offline", metavar="DIR", help="a folder with EQUITY_L.csv, sec_list.csv (and the index list)")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_turnover):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe (RELIANCE,M&M)")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        g.add_argument("--index", choices=sorted(INDEX_NAME), help="scan this index's list from NSE")
        p.add_argument("--lists", metavar="DIR", help="read the --index list from this folder instead of NSE")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price, help="rupees (%s)" % _n(min_price))
        p.add_argument("--min-turnover", type=float, default=min_turnover,
                       help="rupees traded, price x volume (%s; 1e8 = Rs 10 crore)" % _n(min_turnover))
        p.add_argument("--min-volume", type=int, default=0, help="shares (0)")
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-28 10:15" IST, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 50.0, 10 * CRORE)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 50.0, 0.0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this India time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, %s)"
                   % DEFAULT_UNTIL)
    p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
