#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and NSE corporate announcements for the stocks you watch or
hold and prints only what is new since the last check, newest first, with how
far each stock has moved today and a flag on anything that touches your
symbols.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE INFY M&M
    python3 news.py --tickers TCS --all --hours 72   # everything in the last 3 days, seen or not
    python3 news.py --no-nse                         # headlines only
    python3 news.py --routine                        # also newspaper copies, trading window...
    python3 news.py --no-moves                       # skip the price line per symbol

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json. NSE symbols (RELIANCE, M&M, BAJAJ-AUTO);
a trailing .NS is dropped.

Sources (plain GETs, no keys, no login):
  - Yahoo Finance headline RSS, one request per symbol:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYM>.NS&region=IN&lang=en-IN
  - NSE corporate announcements, one request for every company:
    https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
    Each item is a company name, the filing's PDF and "...|SUBJECT: <subject>".
    The name is matched to your symbols through NSE's equity list
    https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv (fetched
    when a symbol's name is not cached yet, then cached in the state file for
    7 days). The subject drives the tag: results, board meeting, deal, SAST,
    pledge, credit rating, corporate action, analyst meet, people... Routine
    filings (newspaper copies, trading window, lost share certificates) are
    counted but hidden unless --routine.
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against the previous close:
    https://query2.finance.yahoo.com/v8/finance/chart/<SYM>.NS?range=1d&interval=5m
  Never the www.nseindia.com/api JSON endpoints: they need a browser-cookie
  handshake, and this tool does not work around bot checks.

Times print in India time (IST, UTC+5:30). State: trader/state/news.json
remembers what you have seen (30 days), so an item shows once. Offline:
--offline DIR reads rss-<SYM>.xml, chart-<SYM>.json (or chart-<SYM>-5d-1m.json.gz),
rss-Online_announcements.xml and EQUITY_L.csv from DIR, with <SYM> stripped
of anything but letters and digits (M&M -> MM). The tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import csv
import datetime
import email.utils
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BROWSER_UA = "Mozilla/5.0 (compatible; india-day-trader-skill/3.0)"
KEEP_DAYS = 30
NAMES_DAYS = 7  # refetch EQUITY_L.csv after a week (new listings, renames)
MOVE_PCT = 3.0  # a stock this far from the previous close is flagged as moving

# The SUBJECT of an NSE announcement -> (tag, important). First match wins, so
# the specific subjects sit above the broad ones.
SUBJECTS = [
    (r"newspaper|trading window|loss of share|duplicate|share certificate|structural digital"
     r"|integrated filing|74\s*\(5\)|regulation 50\(1\)|security cover|monitoring agency"
     r"|redemption|payment of interest|declaration of nav|movement in units|no dues|\bNOC\b"
     r"|compliance certificate|statement of (?:deviation|investor complaints)", "routine", False),
    (r"financial result", "results", True),
    (r"outcome of board meeting", "board outcome", True),
    (r"board meeting", "board meeting", False),
    (r"\bSAST\b|regulation 29|regulation 31|substantial acqui", "SAST", True),
    (r"pledge|encumbrance", "pledge", True),
    (r"buy ?back", "buyback", True),
    (r"acqui|amalgamation|merger|demerger|scheme of arrangement|restructuring|takeover"
     r"|open offer|disinvestment|sale of (?:stake|assets|undertaking)", "deal", True),
    (r"credit rating", "credit rating", True),
    (r"action\(s\) taken|orders? passed|penalt|litigation|dispute|fraud|default|insolvency"
     r"|\bNCLT\b|\bsearch\b|\braid\b|show cause|tax demand", "legal", True),
    (r"dividend|record date|bonus|split|sub-division|corporate action|rights issue", "corporate action", False),
    (r"bagging|order|contract|agreement|MOU|tie up|tie-up|capacity addition", "orders", False),
    (r"fund raising|alteration of capital|allotment|preferential|\bQIP\b|ESOP|ESOS|ESPS", "capital", False),
    (r"resignation", "people", True),
    (r"appointment|director|\bKMP\b|\bSMP\b|cessation|auditor|change in management", "people", False),
    (r"analyst|investor meet|con\.? ?call|investor presentation|earnings call|transcript", "analyst meet", False),
    (r"insider|regulation 7|\bPIT\b", "insider", False),
    (r"press release|media release", "press release", False),
    (r"shareholders? meeting|\bAGM\b|\bEGM\b|postal ballot|voting results", "shareholder meeting", False),
]
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bprofit\b|\bEPS\b|\bguidance\b|\boutlook\b"
                 r"|\bQ[1-4]\b|\bquarterly\b|\bFY ?\d\d\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|target price|\boutperform\b"
                r"|\bunderperform\b|\bbuy rating\b|\bsell rating\b|\bbrokerages?\b"),
    ("legal", r"\blawsuit\b|\bprobe\b|\binvestigation\b|\bcourt\b|\bNCLT\b|\bpenalty\b|\braid\b"
              r"|\bshow[- ]cause\b|\bfraud\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bdemerger\b|\bbuyout\b|\btakeover\b"
             r"|\bdeal\b|\bstake\b|\bbags?\b.*\border\b|\border win\b"),
    ("capital", r"\bdividend\b|\bbuy ?back\b|\bbonus\b|\bstock split\b|\bQIP\b|\brights issue\b"
                r"|\bOFS\b|\bblock deal\b|\bbulk deal\b|\bpledge\b|\bIPO\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bchairman\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\brecall\b|\bdefault\b|\binsolvency\b|\bdelist|\bhack|\bbreach\b|\boutage\b"
             r"|\bdark web\b|\bcyber"),
    ("halt", r"\bsuspen(?:d|ded|sion)\b|\bupper circuit\b|\blower circuit\b|\bhalt(?:s|ed)?\b"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bCCI\b|\bUSFDA\b|\bFDA\b|\bapproval\b|\bapproves\b"
                   r"|\blicen[cs]e\b|\bGST\b"),
    ("macro", r"\brepo rate\b|\bMPC\b|\binflation\b|\bCPI\b|\bGDP\b|\bNifty\b|\bSensex\b"
              r"|\bFIIs?\b|\bFPIs?\b|\brupee\b|\bcrude\b|\btariffs?\b|\bFed\b|\bmonsoon\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

# copied verbatim from tests/_nse.py
IST = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)


def to_ist(utc):
    """Naive or aware UTC -> naive India time."""
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    """Naive India time -> naive UTC."""
    return local.replace(tzinfo=None) - IST


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def parse_now(text, error=NewsError):
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


def _utc(naive):
    return naive.replace(tzinfo=datetime.timezone.utc)


def ist_text(utc, fmt="%Y-%m-%d %H:%M IST"):
    return to_ist(utc).strftime(fmt)


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[nse_symbol(sym)] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(nse_symbol(pl["symbol"]), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[nse_symbol(sym)] = p.get("name") or out.get(nse_symbol(sym))
    return out


def file_key(sym):
    """M&M -> MM, BAJAJ-AUTO -> BAJAJAUTO: the offline file name part."""
    return re.sub(r"[^A-Z0-9]", "", sym.upper())


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.5):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, names):
        """GET url, or offline the first of names found in the folder (.gz is unpacked)."""
        if self.offline:
            for name in ([names] if isinstance(names, str) else names):
                path = os.path.join(self.offline, name)
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        data = fh.read()
                    return gzip.decompress(data) if name.endswith(".gz") else data
            raise NewsError("offline: no file %s" % os.path.join(
                self.offline, names if isinstance(names, str) else names[0]))
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA,
                                                   "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


# ---------------------------------------------------------------- names

def norm_name(name):
    """Company name -> a comparable key: case, "&", "Limited"/"Ltd" and
    punctuation do not count. "Mahindra & Mahindra Ltd." -> "mahindra and mahindra"."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = [w for w in s.split() if w not in ("limited", "ltd", "the")]
    return " ".join(words)


def short_name(name):
    """"Reliance Industries Limited" -> "Reliance Industries" (for headline matching)."""
    return re.sub(r"[\s.,]+(?:limited|ltd\.?)\s*$", "", (name or "").strip(), flags=re.I).strip()


def parse_equity_list(data):
    """EQUITY_L.csv -> {symbol: company name}. The header has leading spaces."""
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
        if row.get("SYMBOL") and row.get("NAME OF COMPANY"):
            out[row["SYMBOL"].upper()] = row["NAME OF COMPANY"]
    return out


def company_names(fetcher, watch, state, now, errors=None):
    """{symbol: NSE company name} for the watched symbols, cached in the state."""
    errors = [] if errors is None else errors
    cache = state.setdefault("names", {})
    fresh = state.get("names_checked", "") >= (now - datetime.timedelta(days=NAMES_DAYS)).isoformat()
    unknown = set(state.get("names_unknown", [])) if fresh else set()
    missing = sorted(s for s in watch if s not in cache)
    if missing and not set(missing) <= unknown:  # fetch the list only for a new symbol
        try:
            listing = parse_equity_list(fetcher.get(EQUITY_LIST, "EQUITY_L.csv"))
        except NewsError as exc:
            errors.append("NSE equity list: %s" % exc)
            listing = {}
        if listing:
            cache.update({s: listing[s] for s in missing if s in listing})
            state["names_checked"] = now.isoformat()
            state["names_unknown"] = sorted(unknown | {s for s in missing if s not in listing})
    for s in sorted(s for s in watch if s not in cache):
        errors.append("%s has no company name from NSE's equity list (EQUITY_L.csv), so no "
                      "NSE announcements are matched to it" % s)
    return {s: cache[s] for s in watch if s in cache}


def match_company(title, names):
    """NSE announcement title -> symbol, or None. Exact on the normalised
    name; a title cut at 40 characters (NSE truncates some feeds) may match
    the start of exactly one name."""
    key = norm_name(title)
    if not key:
        return None
    for sym, name in sorted(names.items()):
        if norm_name(name) == key:
            return sym
    if len(title.strip()) >= 40:
        hits = [s for s, n in names.items() if norm_name(n).startswith(key)]
        if len(hits) == 1:
            return hits[0]
    return None


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (Yahoo RSS), ISO 8601, or NSE's "29-Oct-2026 13:26:05" (India
    time) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        local = datetime.datetime.strptime(value, "%d-%b-%Y %H:%M:%S")
        return _utc(ist_to_utc(local))
    except ValueError:
        pass
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def subject_tag(subject):
    """(tag, important) for an announcement's SUBJECT."""
    for rx, label, important in SUBJECTS:
        if re.search(rx, subject or "", re.I):
            return label, important
    return "update", False


def parse_announcements(data, names):
    """NSE Online_announcements.xml -> items for the companies in names
    ({symbol: company name}). Everyone else's filings are dropped."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("NSE announcements feed is not valid XML: %s" % exc)
    out = []
    for it in root.iter("item"):
        company = _text(it, "title")
        sym = match_company(company, names)
        if not sym:
            continue
        desc = _text(it, "description")
        body, _, subject = desc.rpartition("|SUBJECT:")
        if not _:
            body, subject = desc, ""
        subject = subject.strip()
        body = re.sub(r"\s+", " ", body).strip()
        link = _text(it, "link")
        when = _text(it, "pubDate")
        label, important = subject_tag(subject)
        ident = link or hashlib.sha1(("%s|%s|%s" % (company, subject, when)).encode("utf-8")).hexdigest()
        out.append({"id": "nse:" + ident, "source": "NSE", "kind": "filing", "ticker": sym,
                    "company": company, "subject": subject, "filing_tag": label,
                    "title": "%s: %s" % (company, subject or "announcement"),
                    "summary": body, "important": important, "routine": label == "routine",
                    "link": link, "time": parse_time(when)})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise.

    The previous close is meta.previousClose (the last session's close); on a
    one-day chart meta.chartPreviousClose is the same thing. On a longer range
    chartPreviousClose is the close before the range, so it is not used."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose")
    if not prev and meta.get("range", "1d") == "1d":
        prev = meta.get("chartPreviousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _utc(EPOCH + datetime.timedelta(seconds=int(when))) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        k = file_key(t)
        names = ["chart-%s.json" % k, "chart-%s.json.gz" % k, "chart-%s-5d-1m.json.gz" % k]
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), names)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), names)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        e = re.escape(t)
        alts = [r"\(%s\)" % e, r"\b(?:NSE|BSE)\s*:\s*%s(?![\w&-])" % e, r"\b%s\.NS\b" % e]
        name = short_name(name)
        if name and len(name) >= 4:
            alts.append(r"(?i:\b%s\b)" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your symbols each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        if it["kind"] != "filing":  # a filing is the company's own: it touches that company
            for t, rx in pats.items():
                if rx.search(hay):
                    touches.add(t)
        it["touches"] = sorted(touches)
        if it["kind"] == "filing":
            it["tags"] = [it["filing_tag"]]
        else:
            it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, now, nse=True, errors=None):
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        try:
            got = parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "rss-%s.xml" % file_key(t)), t)
            if not got:
                errors.append("Yahoo's feed for %s.NS is empty right now (it often is for "
                              "some symbols); NSE filings still count" % t)
            items += got
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
    if nse:
        names = company_names(fetcher, watch, state, now, errors)
        if names:
            try:
                data = fetcher.get(ANNOUNCEMENTS, "rss-Online_announcements.xml")
                items += parse_announcements(data, names)
            except NewsError as exc:
                errors.append("NSE announcements: %s" % exc)
    return items


def select(items, state, now, hours=48, show_all=False, routine=False):
    """Items within the window; only unseen ones unless show_all. Routine
    filings are left out unless routine; returns (items, routine_hidden)."""
    seen = state.setdefault("seen", {})
    cutoff = now - datetime.timedelta(hours=hours)
    ahead = now + datetime.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out, hidden = [], 0
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            if it.get("routine") and not routine:
                hidden += 1
                continue
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out, hidden


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s" % ist_text(m["time"], "%H:%M IST")) if m.get("time") else ""
        out.append("%-10s %s %+.2f%% vs previous close %s%s  %s" % (
            m["ticker"], inr(m["price"]), m["pct"], inr(m["prev_close"]), when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT, hidden=0):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = ist_text(it["time"]) if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        if it["kind"] == "filing" and it.get("summary"):
            extra.append(it["summary"][:160])
        extra.append(it["link"] or "(no link in the feed)")
        out.append("      " + "  ".join(extra))
    if hidden:
        out.append("%d routine NSE filing%s hidden (newspaper copies, trading window...): "
                   "--routine shows them." % (hidden, "" if hidden == 1 else "s"))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story or the filing "
               "before acting. Not financial advice." % ist_text(now))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        d["time_ist"] = ist_text(it["time"], "%Y-%m-%d %H:%M") if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols; default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", action="store_true", help="headlines only, no NSE announcements")
    ap.add_argument("--routine", action="store_true",
                    help="also show routine filings (newspaper copies, trading window...)")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help='India time "2026-10-29 13:40" (or ...Z), for reproducible runs')
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.tickers:
        watch = {nse_symbol(t): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    try:
        now = _utc(parse_now(args.now))
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, state, now, nse=not args.no_nse, errors=errors)
    names = state.get("names", {})
    watch = {t: n or names.get(t) for t, n in watch.items()}
    items = dedupe(tag(items, watch))
    shown, hidden = select(items, state, now, hours=args.hours, show_all=args.all,
                           routine=args.routine)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "checked_ist": ist_text(now, "%Y-%m-%d %H:%M"),
                          "moves": mv, "items": _json_ready(shown), "routine_hidden": hidden,
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct, hidden))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
