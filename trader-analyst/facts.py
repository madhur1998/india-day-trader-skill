#!/usr/bin/env python3
"""Results reader for trader-analyst. Standard library only.

Pulls a company's quarterly and annual numbers from Yahoo Finance's
fundamentals timeseries and lays out what changed, quarter by quarter:
revenue, gross, operating and net margins, net income, diluted EPS, and
growth against the same quarter a year earlier, with the trailing twelve
months. It also reads NSE's board-meetings feed for the next results date and
NSE's announcements feed for the latest results filing (the PDF).

    python3 facts.py RELIANCE
    python3 facts.py RELIANCE INFY --quarters 8
    python3 facts.py SBIN --json
    python3 facts.py RELIANCE TCS M&M --metrics trader/metrics.json

Sources (plain GETs, no keys):
  https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/<SYM>.NS
      ?type=quarterlyTotalRevenue,...&period1=..&period2=..
      Consolidated figures, in the currency Yahoo has (rupees for nearly every
      NSE company). Unofficial: the source of truth is the results the company
      filed on NSE.
  https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml
      "Financial Results |Meeting Date: 05-Nov-2026": the next results date.
  https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
      the latest results filing (subject "Financial Result..." or "Outcome of
      Board Meeting") and its PDF.
  https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
      symbol -> company name, to find the company in the two NSE feeds.

How the quarters are built:
  - Indian fiscal year: Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec, Q4 = Jan-Mar;
    FY27 runs from April 2026 to March 2027. A quarter is labelled
    "Q2 FY27 (Jul-Sep 2026)".
  - Year on year is against the quarter ending in the same month a year
    earlier. The trailing twelve months need four quarters in a row; when
    Yahoo skips one, the tool says which and gives no TTM. Nothing is
    interpolated or estimated.
  - Amounts print in crore (1 crore = 1,00,00,000 rupees) with Indian digit
    grouping. A company Yahoo reports in another currency (Infosys, from its
    US filings) prints in that currency in millions, with a note.

--metrics FILE merges one row per symbol into a JSON file (TTM revenue and
EPS, margins, growth). Offline: --offline DIR (repeatable) reads
timeseries-<SYM>.json, rss-Board_Meetings.xml, rss-Online_announcements.xml
and EQUITY_L.csv, with <SYM> stripped of anything but letters and digits.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import calendar
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
import xml.etree.ElementTree as ET

TIMESERIES = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/"
              "timeseries/%s?type=%s&period1=%d&period2=%d")
BOARD_MEETINGS = "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml"
ANNOUNCEMENTS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
UA = "Mozilla/5.0 (compatible; india-day-trader-skill/3.0)"
YEARS_BACK = 4

QUARTERLY = {"revenue": "quarterlyTotalRevenue", "gross_profit": "quarterlyGrossProfit",
             "operating_income": "quarterlyOperatingIncome", "net_income": "quarterlyNetIncome",
             "eps_diluted": "quarterlyDilutedEPS"}
ANNUAL = {"revenue": "annualTotalRevenue", "net_income": "annualNetIncome",
          "eps_diluted": "annualDilutedEPS"}
MONTHS = calendar.month_abbr
RESULT_SUBJECTS = r"financial result|outcome of board meeting"


class FactsError(Exception):
    pass


# copied verbatim from tests/_nse.py
IST = datetime.timedelta(hours=5, minutes=30)


def to_ist(utc):
    """Naive or aware UTC -> naive India time."""
    return utc.replace(tzinfo=None) + IST


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


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


def file_key(sym):
    """M&M -> MM, BAJAJ-AUTO -> BAJAJAUTO: the offline file name part."""
    return re.sub(r"[^A-Z0-9]", "", sym.upper())


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.5):
        self.offline = [offline] if isinstance(offline, str) else (offline or [])
        self.pause, self._last = pause, 0.0

    def get(self, url, name):
        """GET url, or offline the first DIR/name found (.gz is unpacked)."""
        if self.offline:
            for folder in self.offline:
                for n in (name, name + ".gz"):
                    path = os.path.join(folder, n)
                    if os.path.isfile(path):
                        with open(path, "rb") as fh:
                            data = fh.read()
                        return gzip.decompress(data) if n.endswith(".gz") else data
            raise FactsError("offline: no file %s" % name)
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise FactsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise FactsError("could not reach %s: %s" % (url, exc.reason))


def timeseries_url(sym, today):
    types = ",".join(list(QUARTERLY.values()) + list(ANNUAL.values()))
    end = datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time())
    start = end.replace(year=end.year - YEARS_BACK)
    epoch = datetime.datetime(1970, 1, 1)
    return TIMESERIES % (yahoo_symbol(sym), types, int((start - epoch).total_seconds()),
                         int((end - epoch).total_seconds()))


# ---------------------------------------------------------------- numbers

def parse_timeseries(doc):
    """Yahoo timeseries JSON -> {yahoo type: {end date: (value, currency)}}."""
    if isinstance(doc, bytes):
        doc = json.loads(doc.decode("utf-8"))
    ts = (doc or {}).get("timeseries") or {}
    if ts.get("error"):
        raise FactsError("Yahoo: %s" % (ts["error"].get("description") or ts["error"]))
    out = {}
    for block in ts.get("result") or []:
        kind = ((block.get("meta") or {}).get("type") or [None])[0]
        if not kind:
            continue
        vals = out.setdefault(kind, {})
        for v in block.get(kind) or []:
            if not v or not v.get("asOfDate") or not v.get("reportedValue"):
                continue
            vals[v["asOfDate"]] = (v["reportedValue"].get("raw"), v.get("currencyCode") or "INR")
    return out


def _ym(end):
    d = datetime.date.fromisoformat(end)
    return d.year, d.month


def month_end(year, month):
    return datetime.date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def shift(end, months):
    """A month-end date moved by whole months: shift("2026-06-30", -12) -> "2025-06-30"."""
    y, m = _ym(end)
    i = y * 12 + (m - 1) + months
    return month_end(i // 12, i % 12 + 1)


def fiscal_label(end, annual=False):
    """Quarter ending 2026-09-30 -> "Q2 FY27 (Jul-Sep 2026)"; a year ending
    2026-03-31 -> "FY26 (Apr 2025-Mar 2026)". Q1 = Apr-Jun."""
    y, m = _ym(end)
    fy = y if m <= 3 else y + 1
    if annual:
        start = _ym(shift(end, -11))
        return "FY%02d (%s %d-%s %d)" % (fy % 100, MONTHS[start[1]], start[0], MONTHS[m], y)
    if m not in (3, 6, 9, 12):
        return "quarter to %s" % end
    q = {6: 1, 9: 2, 12: 3, 3: 4}[m]
    first = _ym(shift(end, -2))
    return "Q%d FY%02d (%s-%s %d)" % (q, fy % 100, MONTHS[first[1]], MONTHS[m], y)


def short_label(end):
    """"Q2 FY27"."""
    return fiscal_label(end).split(" (")[0]


def pct(a, b):
    """Growth in percent; None when either side is missing or the base is
    zero or a loss (growth from a loss means nothing)."""
    if a is None or b is None or b <= 0:
        return None
    return round(100.0 * (a - b) / b, 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(series, quarters=6):
    """Every number in the report, as a dict."""
    q = {k: {e: v for e, (v, _) in series.get(t, {}).items() if v is not None}
         for k, t in QUARTERLY.items()}
    a = {k: {e: v for e, (v, _) in series.get(t, {}).items() if v is not None}
         for k, t in ANNUAL.items()}
    currencies = sorted({c for t in list(QUARTERLY.values()) + list(ANNUAL.values())
                         for _, c in series.get(t, {}).values()})
    have = sorted(set(q["revenue"]) | set(q["net_income"]))
    if not have:
        raise FactsError("Yahoo has no quarterly revenue or net income for it")
    # every quarter-end from the oldest to the newest Yahoo has, newest first
    ends = [have[-1]]
    while ends[-1] > have[0]:
        ends.append(shift(ends[-1], -3))
    missing = [e for e in ends if e not in have]
    rows = []
    for end in ends[:quarters]:
        r, ni = q["revenue"].get(end), q["net_income"].get(end)
        gp, oi = q["gross_profit"].get(end), q["operating_income"].get(end)
        eps = q["eps_diluted"].get(end)
        ago = shift(end, -12)
        rows.append({
            "end": end, "fiscal": fiscal_label(end), "missing": end in missing,
            "revenue": r, "gross_profit": gp, "operating_income": oi, "net_income": ni,
            "eps_diluted": round(eps, 2) if eps is not None else None,
            "gross_margin": margin(gp, r), "operating_margin": margin(oi, r),
            "net_margin": margin(ni, r),
            "revenue_yoy": pct(r, q["revenue"].get(ago)),
            "net_income_yoy": pct(ni, q["net_income"].get(ago)),
            "eps_yoy": pct(eps, q["eps_diluted"].get(ago)),
            "net_income_year_ago": q["net_income"].get(ago),
        })
    last4 = [ends[0], shift(ends[0], -3), shift(ends[0], -6), shift(ends[0], -9)]
    ttm, ttm_gap = {}, []
    for key in ("revenue", "net_income", "eps_diluted"):
        gap = [e for e in last4 if e not in q[key]]
        if not gap:
            ttm[key] = sum(q[key][e] for e in last4)
        elif key != "eps_diluted":  # an EPS gap alone just leaves the EPS out
            ttm_gap = sorted(set(ttm_gap) | set(gap), reverse=True)
    if "eps_diluted" in ttm:
        ttm["eps_diluted"] = round(ttm["eps_diluted"], 2)
    years = []
    for end in sorted(a["revenue"] or a["net_income"], reverse=True)[:2]:
        prior = shift(end, -12)
        years.append({"end": end, "fiscal": fiscal_label(end, annual=True),
                      "revenue": a["revenue"].get(end), "net_income": a["net_income"].get(end),
                      "eps_diluted": a["eps_diluted"].get(end),
                      "revenue_yoy": pct(a["revenue"].get(end), a["revenue"].get(prior)),
                      "net_income_yoy": pct(a["net_income"].get(end), a["net_income"].get(prior))})
    return {"rows": rows, "missing": missing, "ttm": ttm, "ttm_missing": ttm_gap,
            "years": years, "currency": currencies[0] if len(currencies) == 1 else ",".join(currencies),
            "no_gross_or_operating": not q["gross_profit"] and not q["operating_income"]}


# ---------------------------------------------------------------- formatting

def crore(v):
    """3094680000000 -> "Rs 3,09,468 cr"; small amounts keep two decimals."""
    if v is None:
        return "-"
    c = v / 1e7
    if abs(c) >= 100:
        return inr(round(c))[:-3] + " cr"
    return inr(c) + " cr"


def money(v, currency="INR"):
    if v is None:
        return "-"
    if currency == "INR":
        return crore(v)
    return "%s %sM" % (currency, "{:,.0f}".format(v / 1e6))


def eps_text(v, currency="INR"):
    if v is None:
        return "-"
    return inr(v) if currency == "INR" else "%s %.2f" % (currency, v)


def _p(v):
    return "-" if v is None else "%+.1f%%" % v if v < 0 or v > 0 else "0.0%"


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def changes(result):
    """Plain sentences: what changed in the latest quarter."""
    rows = [r for r in result["rows"] if not r.get("missing")]
    if not rows:
        return []
    cur = rows[0]
    cy = result.get("currency", "INR")
    by_end = {r["end"]: r for r in result["rows"]}
    prev = by_end.get(shift(cur["end"], -3))
    if prev is not None and prev.get("missing"):
        prev = None
    out = []
    line = "%s: revenue %s" % (cur["fiscal"], money(cur["revenue"], cy))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net income %s" % money(cur["net_income"], cy)
    if cur["net_income_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_income_yoy"]
    elif cur["net_income"] is not None and (cur.get("net_income_year_ago") or 0) < 0:
        line += " (a loss of %s a year earlier)" % money(-cur["net_income_year_ago"], cy)
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS %s" % eps_text(cur["eps_diluted"], cy)
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    for key, name in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["net_income"] is not None and cur["operating_income"] is not None \
            and cur["net_income"] > cur["operating_income"]:
        out.append("Net income is above operating income: it includes other income or "
                   "one-offs (investment gains, tax items, exceptional items). Check the "
                   "results filing before reading it as growth.")
    if prev and cur["revenue"] is not None and prev["revenue"]:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: "
                   "compare with a year earlier first)." % pct(cur["revenue"], prev["revenue"]))
    return out


# ---------------------------------------------------------------- NSE feeds

def norm_name(name):
    """Company name -> a comparable key: case, "&", "Limited"/"Ltd" and
    punctuation do not count. "Mahindra & Mahindra Ltd." -> "mahindra and mahindra"."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = [w for w in s.split() if w not in ("limited", "ltd", "the")]
    return " ".join(words)


def parse_equity_list(data):
    """EQUITY_L.csv -> {symbol: company name}. The header has leading spaces."""
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
        if row.get("SYMBOL") and row.get("NAME OF COMPANY"):
            out[row["SYMBOL"].upper()] = row["NAME OF COMPANY"]
    return out


def same_company(title, name):
    """NSE feed title vs a company name. NSE's board-meetings feed cuts
    names at 40 characters, so a cut title may match the start of the name."""
    a, b = norm_name(title), norm_name(name)
    if not a or not b:
        return False
    return a == b or (len(title.strip()) >= 40 and b.startswith(a))


def _items(data, what):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FactsError("NSE %s feed is not valid XML: %s" % (what, exc))
    for it in root.iter("item"):
        yield {c.tag: (c.text or "").strip() for c in it}


def _nse_time(text):
    """NSE's "29-Oct-2026 13:14:24" (India time) -> naive India datetime, or None."""
    try:
        return datetime.datetime.strptime(text.strip(), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        return None


def board_meetings(data, name):
    """NSE Board_Meetings.xml -> this company's meetings, soonest first:
    [{"date", "purpose", "results", "intimated", "link"}]."""
    out, seen = [], set()
    for it in _items(data, "board meetings"):
        if not same_company(it.get("title", ""), name):
            continue
        desc = it.get("description", "")
        m = re.search(r"Meeting Date:\s*(\d{1,2}-[A-Za-z]{3}-\d{4})", desc)
        if not m:
            continue
        day = datetime.datetime.strptime(m.group(1), "%d-%b-%Y").date()
        purpose = re.sub(r"\s+", " ", desc[:m.start()].rstrip(" |")).strip()
        results = bool(re.search(r"financial result", purpose, re.I))
        key = (day, results)
        if key in seen:
            continue
        seen.add(key)
        when = _nse_time(it.get("pubDate", ""))
        out.append({"date": day.isoformat(), "purpose": purpose, "results": results,
                    "intimated": when.strftime("%Y-%m-%d %H:%M IST") if when else None,
                    "link": it.get("link", "")})
    out.sort(key=lambda m: (m["date"], not m["results"]))
    return out


def results_filings(data, name):
    """NSE Online_announcements.xml -> this company's results filings, newest first."""
    out = []
    for it in _items(data, "announcements"):
        if not same_company(it.get("title", ""), name):
            continue
        desc = it.get("description", "")
        subject = desc.rpartition("|SUBJECT:")[2].strip() if "|SUBJECT:" in desc else ""
        if not re.search(RESULT_SUBJECTS, subject, re.I):
            continue
        when = _nse_time(it.get("pubDate", ""))
        out.append({"subject": subject, "filed": when.strftime("%Y-%m-%d %H:%M IST") if when else None,
                    "link": it.get("link", "")})
    out.sort(key=lambda f: f["filed"] or "", reverse=True)
    return out


def next_results(meetings, today):
    """The first board meeting on or after today (results first on a tie),
    with how far away it is and whether it is in this Monday-to-Sunday week."""
    ahead = [m for m in meetings if m["date"] >= today.isoformat()]
    if not ahead:
        return None
    m = dict(ahead[0])
    day = datetime.date.fromisoformat(m["date"])
    m["days_away"] = (day - today).days
    m["this_week"] = day.isocalendar()[:2] == today.isocalendar()[:2]
    m["weekday"] = day.strftime("%a")
    return m


def expected_quarter(today):
    """The newest quarter whose results should be out by today: listed
    companies file within 45 days of a quarter's end, 60 for the March quarter
    (SEBI LODR Regulation 33)."""
    end = month_end(today.year, today.month)
    while True:
        y, m = _ym(end)
        if m in (3, 6, 9, 12):
            due = datetime.date.fromisoformat(end) + datetime.timedelta(days=60 if m == 3 else 45)
            if due < today:
                return end
        end = shift(end, -1)


def lag_note(result, today):
    """A note when Yahoo's newest quarter is older than the results due by now."""
    rows = [r for r in result["rows"] if not r["missing"]]
    if not rows:
        return None
    due = expected_quarter(today)
    if rows[0]["end"] < due:
        return ("Yahoo's newest quarter is %s, but results up to %s were due by now (45 days "
                "after a quarter, 60 for Jan-Mar). Yahoo's data lags here: read the latest "
                "results the company filed on NSE." % (fiscal_label(rows[0]["end"]),
                                                       fiscal_label(due)))
    return None


def stale_note(result, filings):
    """A note when a results filing on NSE is newer than Yahoo's latest quarter."""
    if not filings or not result["rows"]:
        return None
    f = next((f for f in filings if re.search(r"financial result", f["subject"], re.I)), filings[0])
    latest = result["rows"][0]["end"]
    if f["filed"] and f["filed"][:10] > shift(latest, 1):
        return ("Yahoo's numbers stop at %s, but the company filed \"%s\" on NSE at %s, "
                "probably a newer quarter. Read the filing before summarising: %s" % (
                    fiscal_label(latest), f["subject"], f["filed"], f["link"]))
    return None


# ---------------------------------------------------------------- output

def render(sym, name, result, nse):
    cy = result["currency"]
    unit = "Rs crore except EPS" if cy == "INR" else "%s millions except EPS" % cy
    out = ["%s (%s). %s. Source: Yahoo Finance fundamentals (consolidated, unofficial); "
           "the company's results filed on NSE are the source of truth." % (
               name or sym, sym, unit), ""]
    out.append("%-23s %14s %8s %7s %7s %7s %13s %10s %8s" % (
        "quarter", "revenue", "rev yoy", "gross", "oper", "net", "net income", "EPS dil",
        "EPS yoy"))
    for r in result["rows"]:
        if r["missing"]:
            out.append("%-23s %s" % (r["fiscal"], "missing from Yahoo's data"))
            continue
        out.append("%-23s %14s %8s %7s %7s %7s %13s %10s %8s" % (
            r["fiscal"], _num(r["revenue"], cy), _p(r["revenue_yoy"]),
            _pm(r["gross_margin"]), _pm(r["operating_margin"]), _pm(r["net_margin"]),
            _num(r["net_income"], cy),
            "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"], _p(r["eps_yoy"])))
    t = result["ttm"]
    out.append("")
    if result["ttm_missing"]:
        out.append("No trailing twelve months: %s not in Yahoo's data (four quarters "
                   "in a row are needed; nothing is estimated)." % ", ".join(
                       fiscal_label(e) for e in result["ttm_missing"]))
    if t:
        out.append("Last 4 quarters: revenue %s, net income %s, diluted EPS %s." % (
            money(t.get("revenue"), cy), money(t.get("net_income"), cy),
            eps_text(t.get("eps_diluted"), cy)))
    for y in result["years"]:
        line = "%s: revenue %s" % (y["fiscal"], money(y["revenue"], cy))
        if y["revenue_yoy"] is not None:
            line += " (%+.1f%%)" % y["revenue_yoy"]
        line += ", net income %s" % money(y["net_income"], cy)
        if y["net_income_yoy"] is not None:
            line += " (%+.1f%%)" % y["net_income_yoy"]
        if y["eps_diluted"] is not None:
            line += ", diluted EPS %s" % eps_text(y["eps_diluted"], cy)
        out.append(line + ".")
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    notes = []
    if cy != "INR":
        notes.append("Yahoo has this company's numbers in %s (from its overseas filings), not "
                     "the rupee results filed on NSE. Read the NSE filing for rupee figures." % cy)
    if result.get("lag"):
        notes.append(result["lag"])
    if result["no_gross_or_operating"]:
        notes.append("No gross profit or operating income in Yahoo's data (usual for banks, "
                     "NBFCs and insurers): those margins are blank, not zero.")
    if nse:
        out.append("")
        nxt = nse.get("next")
        if nxt:
            what = "financial results" if nxt["results"] else "board meeting (%s)" % nxt["purpose"]
            when = ("THIS WEEK" if nxt["this_week"] else "in %d days" % nxt["days_away"])
            if nxt["days_away"] == 0:
                when = "TODAY"
            out.append("Next results date: %s %s, %s (%s), per NSE board-meeting intimation "
                       "of %s  %s" % (nxt["weekday"], nxt["date"], what, when,
                                      nxt["intimated"], nxt["link"]))
        elif nse.get("checked"):
            out.append("Next results date: not in NSE's board-meetings feed right now (it only "
                       "holds the latest intimations). Check the company's announcements.")
        for f in (nse.get("filings") or [])[:3]:
            out.append("NSE filing %s: %s  %s" % (f["filed"], f["subject"], f["link"]))
        if nse.get("stale"):
            notes.append(nse["stale"])
        for e in nse.get("errors", []):
            notes.append(e)
    for n in notes:
        out.append("")
        out.append("NOTE: " + n)
    return "\n".join(out) + "\n"


def _num(v, currency):
    """Table cell: crore with Indian grouping, or millions for another currency."""
    if v is None:
        return "-"
    if currency == "INR":
        return crore(v).replace("Rs ", "").replace(" cr", "")
    return "{:,.0f}".format(v / 1e6)


def metrics_row(sym, name, result):
    rows = [r for r in result["rows"] if not r["missing"]]
    cur = rows[0] if rows else {}
    t = result["ttm"]
    return {"symbol": sym, "name": name, "period_end": cur.get("end"),
            "fiscal": short_label(cur["end"]) if cur else None, "currency": result["currency"],
            "revenue_ttm": t.get("revenue"), "net_income_ttm": t.get("net_income"),
            "eps_ttm": t.get("eps_diluted"),
            "revenue_growth_yoy": cur.get("revenue_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "net_income_growth_yoy": cur.get("net_income_yoy"),
            "gross_margin": cur.get("gross_margin"), "operating_margin": cur.get("operating_margin"),
            "net_margin": cur.get("net_margin"),
            "missing_quarters": result["missing"],
            "source": "Yahoo Finance fundamentals (unofficial)"}


def nse_context(fetcher, name, today, want_filings=True):
    """Next results date and the latest results filings for one company."""
    ctx = {"next": None, "filings": [], "errors": [], "checked": False}
    if not name:
        ctx["errors"].append("no company name from NSE's equity list: results date not looked up")
        return ctx
    try:
        meetings = board_meetings(fetcher.get(BOARD_MEETINGS, "rss-Board_Meetings.xml"), name)
        ctx["next"] = next_results(meetings, today)
        ctx["checked"] = True
    except FactsError as exc:
        ctx["errors"].append("NSE board meetings: %s" % exc)
    if want_filings:
        try:
            ctx["filings"] = results_filings(
                fetcher.get(ANNOUNCEMENTS, "rss-Online_announcements.xml"), name)
        except FactsError as exc:
            ctx["errors"].append("NSE announcements: %s" % exc)
    return ctx


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tickers", nargs="*", help="NSE symbols, e.g. RELIANCE INFY M&M")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--no-nse", action="store_true",
                    help="skip the results date and the NSE filing links")
    ap.add_argument("--today", help="India date YYYY-MM-DD for 'this week' (default: today)")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per symbol into this JSON")
    ap.add_argument("--offline", metavar="DIR", action="append")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.tickers:
        ap.print_help()
        return 2
    try:
        today = (datetime.date.fromisoformat(args.today) if args.today
                 else to_ist(utc_now()).date())
    except ValueError:
        print("facts: --today wants YYYY-MM-DD", file=sys.stderr)
        return 2
    fetcher = Fetcher(offline=args.offline)
    names = {}
    if not args.no_nse:
        try:
            names = parse_equity_list(fetcher.get(EQUITY_LIST, "EQUITY_L.csv"))
        except FactsError as exc:
            print("facts: NSE equity list: %s (results dates skipped)" % exc, file=sys.stderr)
    results, rows, failed = [], [], 0
    for t in args.tickers:
        sym = nse_symbol(t)
        name = names.get(sym)
        if names and not name:
            print("facts: %s is not in NSE's equity list (EQUITY_L.csv); check the symbol" % sym,
                  file=sys.stderr)
        nse = None if args.no_nse else nse_context(fetcher, name, today)
        try:
            series = parse_timeseries(fetcher.get(timeseries_url(sym, today),
                                                  "timeseries-%s.json" % file_key(sym)))
            result = analyse(series, quarters=args.quarters)
        except (FactsError, ValueError) as exc:
            print("facts: %s: %s" % (sym, exc), file=sys.stderr)
            failed += 1
            if nse and nse["next"] and not args.json:
                n = nse["next"]
                print("%s: next board meeting %s %s (%s), per NSE  %s" % (
                    sym, n["weekday"], n["date"], "results" if n["results"] else n["purpose"],
                    n["link"]))
            continue
        result["lag"] = lag_note(result, today)
        if nse:
            nse["stale"] = stale_note(result, nse["filings"])
        results.append({"symbol": sym, "name": name, "result": result, "nse": nse,
                        "changes": changes(result)})
        rows.append(metrics_row(sym, name, result))
        if not args.json:
            sys.stdout.write(render(sym, name, result, nse))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = {r.get("symbol") or r.get("ticker"): r
                            for r in json.load(fh).get("companies", [])}
        for r in rows:
            existing[r["symbol"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": today.isoformat(),
                       "companies": sorted(existing.values(), key=lambda r: r.get("symbol") or r.get("ticker", ""))},
                      fh, indent=2)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())
