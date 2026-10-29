#!/usr/bin/env python3
"""Order tickets, your yes, and the paper simulator for trader-broker. Standard library only.

This tool never talks to Zerodha and never places anything. It does its
work around the one moment that matters, your yes:

  show        renders each proposed order as a plain ticket plus the exact
              JSON Claude sends to the Kite MCP place_order tool: the entry
              and, for a day trade, its stop leg (an SL-M sell that goes to
              the exchange once the entry fills). An order is READY only if
              trader-risk passed THIS exact order today (matched by
              fingerprint) and nobody has decided on it yet.
  approve     records your explicit yes for ONE order, with your reply word
              for word. One yes covers the entry AND its stop leg (the stop
              only reduces risk). It refuses "yes to all", "sure", "ok", a yes
              that names a different stock or amount, and any order that is
              not READY.
  decline     records a no.
  paper-fill  paper mode ("paper": true, the default): fills the approved
              order at a fresh price from trader/prices.json (under 15
              minutes old), writes the fill to trader/portfolio.json and
              records the stop leg as a resting paper stop.
  paper-check paper mode: triggers paper stops (at the trigger, or worse if
              the price is already below it) and targets (price at or above
              the target) from fresh prices.
  result      live mode: records what happened after the Allow once / Deny
              card, with Kite's order_id, for the entry or its stop leg.
  fill        records a fill by hand in trader/portfolio.json (cash and shares).
  flatten     builds the MIS sell that closes a symbol's position by flat_by
              (a LIMIT a little under a fresh price, or MARKET with --market)
              and says how to cancel the resting stop leg. It still goes
              through trader-risk and your yes.
  log         prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py paper-fill --index 1    # paper: fill it at a fresh price
    python3 ticket.py paper-check             # paper: did a stop or a target hit?
    python3 ticket.py result --index 1 --status placed --order-id 250128000123456
    python3 ticket.py result --index 1 --leg stop --status placed --order-id 250128000123460
    python3 ticket.py fill --symbol RELIANCE --side sell --qty 46 --price 1160.00
    python3 ticket.py flatten --symbol RELIANCE
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
India dates; --now "2026-10-28 09:45" pins the clock (India time).
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

KITE_FIELDS = ("variety", "exchange", "tradingsymbol", "transaction_type", "quantity", "product",
               "order_type", "price", "trigger_price", "validity", "disclosed_quantity", "tag")
VALIDITY_WORDS = {"DAY": "good for today", "IOC": "immediate or cancel"}
PRODUCT_WORDS = {"MIS": "MIS intraday", "CNC": "CNC delivery"}
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}
FRESH_MINUTES = 15
FLATTEN_BUFFER_PCT = 0.1   # a flatten LIMIT sits this far under the last price (at least one tick)


class TicketError(Exception):
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


def parse_now(text, error=TicketError):
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


def ist_iso(now_utc):
    """naive UTC -> "2026-10-28T09:45:00+05:30" (India wall clock with its offset)."""
    return to_ist(now_utc).replace(microsecond=0).isoformat() + "+05:30"


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


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path):
    """The config, with a clear error for a dollar key left over from the US pack."""
    cfg = read_json(path, "config", required=False) or {}
    for k in sorted(cfg):
        m = re.match(r"^(\w+)_usd$", k)
        if m:
            raise TicketError("your config still has \"%s\": this pack trades rupees on NSE. Rename it to "
                              "\"%s_inr\" and give it a rupee amount" % (k, m.group(1)))
    return cfg


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def fresh_price(path, sym, now_utc):
    """(price, time_utc, why_not). why_not is None for a usable price: in rupees and under
    15 minutes old by the quote's own time (detail.time), else when it was fetched, else as_of."""
    doc = read_json(path, "prices", required=False)
    if not isinstance(doc, dict):
        return None, None, "no prices file at %s" % path
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, "no %s price in %s" % (sym, path)
    det = (doc.get("detail") or {}).get(sym) or {}
    if str(det.get("currency") or doc.get("currency") or "INR").upper() != "INR":
        return None, None, "the %s quote in %s is not in rupees" % (sym, path)
    t = parse_utc(det.get("time")) or parse_utc(det.get("fetched")) or parse_utc(doc.get("as_of"))
    if t is None:
        return p, None, "the %s price in %s has no time" % (sym, path)
    age = (now_utc - t).total_seconds()
    if not -60 <= age <= FRESH_MINUTES * 60:
        return p, t, ("the %s price %s is from %s IST, older than %d minutes"
                      % (sym, _p(p), to_ist(t).strftime("%Y-%m-%d %H:%M"), FRESH_MINUTES))
    return p, t, None


# ---------------------------------------------------------------- the order

def kite_of(order):
    """The place_order arguments of an order: its "kite" object, or the order itself
    when it is a bare Kite dict. Same function in trader-risk/check.py."""
    k = order.get("kite")
    if isinstance(k, dict):
        return k
    if "tradingsymbol" in order:
        return {f: order[f] for f in order if f in KITE_FIELDS or f not in
                ("symbol", "side", "qty", "stop_leg", "target", "why", "plan")}
    return {}


def fingerprint(order):
    """Same function as trader-risk/check.py: the exact entry and stop leg."""
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


def _num(v):
    f = float(v)
    return str(int(f)) if f == int(f) else ("%.4f" % f).rstrip("0")


def _p(v):
    return "%.2f" % float(v)


def tool_input(order):
    """The exact arguments for the entry's place_order call. "why", "plan" and
    "target" are ours and are never sent."""
    return {k: v for k, v in kite_of(order).items() if v is not None}


def stop_input(order):
    """The exact arguments for the stop leg's place_order call, or None."""
    leg = order.get("stop_leg")
    if not isinstance(leg, dict):
        return None
    return {k: v for k, v in leg.items() if v is not None}


def symbol_of(order):
    return str(kite_of(order).get("tradingsymbol") or order.get("symbol") or "").upper()


def side_of(order):
    tt = str(kite_of(order).get("transaction_type") or "").upper()
    return {"BUY": "buy", "SELL": "sell"}.get(tt, str(order.get("side", "")).lower())


def target_of(order):
    return _f(order.get("target")) or _f((order.get("plan") or {}).get("target"))


def max_loss(order):
    """(rupees lost if the stop fills exactly at the stop, charges included, stop), or (None, stop)."""
    k, plan = kite_of(order), order.get("plan") or {}
    stop = _f((order.get("stop_leg") or {}).get("trigger_price")) or _f(plan.get("stop"))
    q = _f(k.get("quantity"))
    if not stop or not q:
        return None, stop
    entry = _f(k.get("price")) if k.get("order_type") in ("LIMIT", "SL") else None
    if _f(plan.get("max_loss")) is not None and (entry is None or abs(entry - (_f(plan.get("entry")) or 0)) < 0.005):
        return round(_f(plan["max_loss"]), 2), stop
    entry = entry or _f(plan.get("entry"))
    if not entry:
        return None, stop
    rates = dict(CHARGES)
    rates.update(plan.get("charges_rates") or {})
    ch = charges(q, entry, stop, rates)["total"]
    return round(q * int(round(abs(entry - stop) * 100)) / 100.0 + ch, 2), stop


def describe(k):
    """BUY 46 RELIANCE, LIMIT 1169.40, MIS intraday, good for today"""
    q = k.get("quantity")
    ot = k.get("order_type")
    if ot == "LIMIT":
        how = "LIMIT %s" % _p(k.get("price"))
    elif ot == "SL-M":
        how = "SL-M trigger %s" % _p(k.get("trigger_price"))
    elif ot == "SL":
        how = "SL trigger %s, limit %s" % (_p(k.get("trigger_price")), _p(k.get("price")))
    else:
        how = str(ot)
    return "%s %s %s, %s, %s, %s" % (k.get("transaction_type"), q, k.get("tradingsymbol"), how,
                                     PRODUCT_WORDS.get(k.get("product"), k.get("product")),
                                     VALIDITY_WORDS.get(k.get("validity", "DAY"), k.get("validity")))


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    parts = [describe(kite_of(order))]
    leg = order.get("stop_leg")
    if isinstance(leg, dict):
        parts.append("stop leg: %s, placed once the entry fills" % describe(leg))
    t = target_of(order)
    if t and isinstance(leg, dict):
        parts.append("target %s: watched by trader-alerts, not an order at Kite" % _p(t))
    loss, stop = max_loss(order)
    if loss is not None and isinstance(leg, dict):
        parts.append("max loss about %s if the stop fills at %s, charges included (it can fill worse "
                     "in a fast market)" % (inr(loss), _p(stop)))
    return parts


def plain(order):
    """BUY 46 RELIANCE, LIMIT 1169.40, ... / stop leg: SELL 46 RELIANCE, SL-M trigger 1160.00, ... / ..."""
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    for src in (kite_of(order), order.get("stop_leg") or {}):
        for f in ("quantity", "price", "trigger_price"):
            v = _f(src.get(f))
            if v is not None:
                nums.append(v)
    if target_of(order):
        nums.append(target_of(order))
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order (and its stop leg)? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it, haan)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = symbol_of(order)
    tokens = set(re.findall(r"[A-Z][A-Z0-9&\-]{0,19}", raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_num(n) for n in nums), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today, paper=True):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for %s" % (
                rec.get("at"), "ticket.py paper-fill" if paper else "the place_order call"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if side_of(order) == "buy" else "exit"


def json_lines(obj, indent="    "):
    return [indent + l for l in json.dumps(obj, indent=2).splitlines()]


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today):
    paper = cfg.get("paper", True)
    ready, label, rv = status_of(order, risk, state, today, paper)
    acct = "PAPER (simulated here, nothing goes to Kite)" if paper else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s, Zerodha Kite, NSE" % (idx, total, acct)]
    for part in plain_parts(order):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    leg = stop_input(order)
    lines.append("  %s place_order with exactly:" % ("Kite would get" if paper else "Claude will call"))
    lines.extend(json_lines(tool_input(order)))
    if leg:
        lines.append("  then, once the entry fills, the stop leg (covered by the same yes), place_order with:")
        lines.extend(json_lines(leg))
    if ready:
        lines.append("  Reply \"yes\" to place this order%s, or \"no\" to skip it."
                     % (" and its stop leg" if leg else ""))
        if paper:
            lines.append("  Paper mode: after your yes, ticket.py paper-fill fills it at a fresh price.")
        else:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today, cfg.get("paper", True))
    loss, stop = max_loss(order)
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
            "broker": "zerodha", "tool": "place_order", "tool_input": tool_input(order),
            "stop_leg_input": stop_input(order), "target": target_of(order), "why": order.get("why", ""),
            "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell). Rupees;
    charges are not taken out of the cash (your contract note has them)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "currency": "INR", "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    if qty != int(qty):
        raise TicketError("NSE trades whole shares: a fill of %s shares cannot be right" % _num(qty))
    qty = int(qty)
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = int(round(float(pos.get("shares", 0)) + qty))
        pf["cash"] = round(float(pf.get("cash", 0)) - cost, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (qty, symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 0:
                break
            take = min(left, int(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = int(lot["shares"]) - take
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if int(l["shares"]) > 0]
        pos["shares"] = int(round(float(pos["shares"]) - qty))
        if pos["shares"] <= 0:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost, 2)
    write_json(portfolio_path, pf)
    return pf


def held(portfolio_path, sym):
    pf = read_json(portfolio_path, "portfolio", required=False) or {}
    return sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
               if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)


def log_fill(state, now, today, symbol, side, qty, price, paper=False, note=None):
    """A fill in the log (the caller writes the state)."""
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
           "qty": int(float(qty)), "price": float(price), "paper": bool(paper),
           "plain": "%s %s %s at %s" % (side.upper(), int(float(qty)), symbol.upper(), _p(price))}
    if note:
        rec["note"] = note
    state.setdefault("orders", []).append(rec)
    return rec


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-28")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = load_config(a.config)
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def _market_open(now):
    """(open, why) for paper fills: only inside today's NSE session."""
    local = to_ist(now)
    ok, why = trading_day(local.date())
    if not ok:
        return False, "the market is closed today (%s)" % why
    opens, closes = session(local.date())
    if not opens <= local.time() < closes:
        return False, "the market is closed (%s IST; the session is %s to %s)" % (
            local.strftime("%H:%M"), opens.strftime("%H:%M"), closes.strftime("%H:%M"))
    return True, ""


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket (its entry and its stop leg), never the batch. "
          "Education and tooling, not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    paper = cfg.get("paper", True)
    ready, label, rv = status_of(order, risk, state, today, paper)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    ok, why = parse_yes(a.reply, order, [symbol_of(o) for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order)
    leg = stop_input(order)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": symbol_of(order), "side": side_of(order), "qty": kite_of(order).get("quantity"),
           "kind": kind_of(order, rv), "max_loss": loss, "stop": stop, "target": target_of(order),
           "stop_leg": bool(leg), "plain": plain(order), "status": "approved", "reply": a.reply,
           "paper": bool(paper), "broker": "zerodha"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d%s: %s." % (a.index, " and its stop leg" if leg else "", plain(order)))
    if paper:
        print("Paper mode: nothing goes to Kite. Fill it at a fresh price: ticket.py paper-fill --index %d"
              % a.index)
        return 0
    print("Now call place_order with exactly:")
    print(json.dumps(tool_input(order), indent=2))
    print("Claude's card shows Allow once / Deny. Then: ticket.py result --index %d --status "
          "placed|denied|failed --order-id <Kite order_id>" % a.index)
    if leg:
        print("Once the entry fills (get_orders / get_trades), place the stop leg, covered by this same yes:")
        print(json.dumps(leg, indent=2))
        print("Then: ticket.py result --index %d --leg stop --status placed --order-id <its order_id>" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": symbol_of(order), "side": side_of(order),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def _placed_rec(state, fp, today):
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today and rec.get("status") == "placed":
            return rec
    return None


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    order_id = a.order_id or a.broker_id
    if a.leg == "stop":
        leg = stop_input(order)
        if not leg:
            raise TicketError("order %d has no stop leg" % a.index)
        entry = _placed_rec(state, fp, today)
        if not entry:
            raise TicketError("order %d's entry is not recorded as placed today; the stop leg follows the entry"
                              % a.index)
        new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": entry["symbol"],
               "side": "sell", "kind": "stop", "plain": "stop leg: " + describe(leg),
               "status": "stop_" + a.status, "trigger": leg.get("trigger_price"), "paper": False}
        if order_id:
            new["broker_order_id"] = order_id
        if a.note:
            new["note"] = a.note
        state["orders"].append(new)
        write_json(a.state, state)
        print("Recorded order %d's stop leg as %s." % (a.index, a.status))
        if a.status != "placed":
            print("WARNING: the %s position has NO stop at the exchange. Place the stop leg again (ticket.py show "
                  "--index %d prints it) or flatten now (ticket.py flatten --symbol %s)."
                  % (entry["symbol"], a.index, entry["symbol"]))
        return 0
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status, "paper": False}
    for k in ("qty", "kind", "max_loss", "stop", "target"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if order_id:
        new["broker_order_id"] = order_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    if a.status == "placed" and a.filled_qty and a.filled_price:
        apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today)
        log_fill(state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        print("Added the fill to %s." % a.portfolio)
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
    leg = stop_input(order)
    if a.status == "placed" and leg:
        print("Once the entry fills, place the stop leg (covered by the same yes): place_order with exactly:")
        print(json.dumps(leg, indent=2))
        print("Then: ticket.py result --index %d --leg stop --status placed --order-id <its order_id>" % a.index)
    return 0


def cmd_paper_fill(a):
    orders, cfg, risk, state, today, now = _load(a)
    if not cfg.get("paper", True):
        raise TicketError("this is a LIVE account (\"paper\": false): place it on Kite with place_order and "
                          "record it with ticket.py result")
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today (or it is already done): approve it first"
                          % a.index)
    ok, why = _market_open(now)
    if not ok:
        raise TicketError("no paper fill: %s. A paper order fills only in the NSE session" % why)
    k = kite_of(order)
    sym, side = symbol_of(order), side_of(order)
    price, t, stale = fresh_price(a.prices, sym, now)
    if stale:
        raise TicketError("no paper fill: %s. Refresh it first (trader-alerts: alerts.py quote %s --write)"
                          % (stale, sym))
    qty = int(k.get("quantity") or 0)
    ot, limit, trig = k.get("order_type"), _f(k.get("price")), _f(k.get("trigger_price"))
    fill = price
    if ot in ("SL", "SL-M") and trig and ((side == "buy" and price < trig) or (side == "sell" and price > trig)):
        print("Not filled: %s last %s has not reached the trigger %s. Your yes stands for today; run paper-fill "
              "again later." % (sym, _p(price), _p(trig)))
        return 1
    if ot in ("LIMIT", "SL") and limit:
        if (side == "buy" and price > limit + 0.005) or (side == "sell" and price < limit - 0.005):
            print("Not filled: %s last %s is %s your limit %s. Your yes stands for today; run paper-fill again "
                  "when the price comes back." % (sym, _p(price), "above" if side == "buy" else "below",
                                                   _p(limit)))
            return 1
    tag = k.get("tag") or fp[:8]
    placed = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": sym,
              "side": side, "plain": rec["plain"], "status": "placed", "paper": True,
              "broker_order_id": "PAPER-" + tag}
    for f in ("qty", "kind", "max_loss", "stop", "target"):
        if rec.get(f) is not None:
            placed[f] = rec[f]
    state.setdefault("orders", []).append(placed)
    apply_fill(a.portfolio, cfg, sym, side, qty, fill, today)
    log_fill(state, now, today, sym, side, qty, fill, paper=True)
    print("PAPER fill: %s %d %s at %s (last price %s at %s IST). Cash and shares are in %s."
          % (side.upper(), qty, sym, _p(fill), _p(price), to_ist(t).strftime("%H:%M"), a.portfolio))
    leg = stop_input(order)
    if leg and side == "buy":
        stop = {"id": "PAPER-" + str(leg.get("tag") or tag + "sl"), "fingerprint": fp, "date": today,
                "symbol": sym, "qty": qty, "order_type": leg.get("order_type"),
                "trigger": _f(leg.get("trigger_price")), "limit": _f(leg.get("price")),
                "target": target_of(order), "entry": fill, "product": leg.get("product"),
                "status": "open", "placed_at": ist_iso(now)}
        state.setdefault("paper_stops", []).append(stop)
        state["orders"].append({"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp,
                                "symbol": sym, "side": "sell", "kind": "stop", "status": "stop_placed",
                                "paper": True, "broker_order_id": stop["id"], "trigger": stop["trigger"],
                                "plain": "stop leg: " + describe(leg)})
        print("PAPER stop leg resting: %s. Target %s. ticket.py paper-check triggers them from fresh prices."
              % (describe(leg), _p(stop["target"]) if stop["target"] else "none"))
    if side == "sell":
        for s in state.get("paper_stops", []):
            if s.get("status") == "open" and s.get("symbol") == sym:
                s.update({"status": "canceled", "closed_at": ist_iso(now),
                          "note": "position closed by a sell at %s" % _p(fill)})
                print("PAPER stop %s canceled: the position is closed." % s["id"])
    write_json(a.state, state)
    return 0


def cmd_paper_check(a):
    cfg = load_config(a.config)
    if not cfg.get("paper", True):
        raise TicketError("this is a LIVE account (\"paper\": false): Kite holds your stop; check it with "
                          "get_orders and get_positions")
    state = load_state(a.state)
    now, today = _clock(a)
    stops = [s for s in state.get("paper_stops", []) if s.get("status") == "open"]
    if not stops:
        print("No open paper stops.")
        return 0
    ok, why = _market_open(now)
    if not ok:
        print("Nothing triggers now: %s. %d paper stop%s still open." % (why, len(stops),
                                                                         "" if len(stops) == 1 else "s"))
        return 0
    flat_by = cfg.get("flat_by") or "15:10"
    late = to_ist(now).strftime("%H:%M") >= flat_by
    for s in stops:
        sym, qty = s["symbol"], int(s["qty"])
        have = int(held(a.portfolio, sym))
        if have <= 0:
            s.update({"status": "canceled", "closed_at": ist_iso(now), "note": "no position left"})
            print("%s: no shares in %s any more; paper stop %s canceled." % (sym, a.portfolio, s["id"]))
            continue
        qty = min(qty, have)
        price, t, stale = fresh_price(a.prices, sym, now)
        if stale:
            print("%s: %s. Refresh it (trader-alerts: alerts.py quote %s --write); the paper stop at %s is "
                  "still open." % (sym, stale, sym, _p(s["trigger"])))
            continue
        fill, how = None, None
        if s.get("trigger") and price <= s["trigger"] + 0.005:
            limit = s.get("limit")
            if s.get("order_type") == "SL" and limit and price < limit - 0.005:
                print("%s: last %s is through the stop trigger %s AND below its limit %s: the SL did not fill. "
                      "The position is open and losing: flatten it (ticket.py flatten --symbol %s --market)."
                      % (sym, _p(price), _p(s["trigger"]), _p(limit), sym))
                continue
            fill, how = min(price, s["trigger"]), "stopped"
        elif s.get("target") and price >= s["target"] - 0.005:
            fill, how = s["target"], "target"
        if fill is None:
            line = "%s: last %s at %s IST, stop %s, target %s: still open." % (
                sym, _p(price), to_ist(t).strftime("%H:%M"), _p(s["trigger"]),
                _p(s["target"]) if s.get("target") else "none")
            if late:
                line += " It is past your flat-by %s: flatten now (ticket.py flatten --symbol %s)." % (flat_by, sym)
            print(line)
            continue
        apply_fill(a.portfolio, cfg, sym, "sell", qty, fill, today)
        log_fill(state, now, today, sym, "sell", qty, fill, paper=True,
                 note="paper %s" % ("stop" if how == "stopped" else "target"))
        state["orders"][-1]["kind"] = "exit"
        gross = round((fill - float(s["entry"])) * qty, 2)
        cost = charges(qty, float(s["entry"]), fill)["total"]
        s.update({"status": how, "closed_at": ist_iso(now), "fill": fill, "pnl_gross": gross,
                  "charges_est": cost})
        print("%s: PAPER %s, SELL %d at %s (last %s). P&L %s before about %s charges = %s."
              % (sym, "STOP HIT" if how == "stopped" else "TARGET HIT", qty, _p(fill), _p(price), inr(gross),
                 inr(cost), inr(round(gross - cost, 2))))
    write_json(a.state, state)
    return 0


def cmd_fill(a):
    cfg = load_config(a.config)
    now = parse_now(a.now)
    date = a.date or to_ist(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-28")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date)
    state = load_state(a.state)
    log_fill(state, now, date, a.symbol, a.side, a.qty, a.price, paper=bool(cfg.get("paper", True)))
    write_json(a.state, state)
    print("Recorded %s %s %s at %s on %s in %s. Cash now %s." % (a.side, a.qty, a.symbol.upper(), _p(a.price),
                                                                date, a.portfolio, inr(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = load_config(a.config)
    sym = a.symbol.strip().upper()
    now, today = _clock(a)
    paper = cfg.get("paper", True)
    source = "the broker, get_positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0 or qty != int(qty):
            raise TicketError("--qty wants the whole number of shares you hold, like --qty 46")
    else:
        qty = held(a.portfolio, sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_positions) and "
                              "pass --qty if you do hold some" % (sym, a.portfolio))
    qty = int(qty)
    if a.market and a.limit:
        raise TicketError("--market and --limit are two different orders: pick one")
    product = str(cfg.get("product") or "MIS").upper()
    k = {"variety": "regular", "exchange": "NSE", "tradingsymbol": sym, "transaction_type": "SELL",
         "quantity": qty, "product": product}
    if a.market:
        k["order_type"] = "MARKET"
        how = "MARKET"
    else:
        if a.limit:
            px = _f(a.limit)
            if not px or px <= 0:
                raise TicketError("--limit wants a price, like --limit 1171.50")
            px = round_tick(px, "nearest")
            how = "LIMIT %s (your price)" % _p(px)
        else:
            last, t, stale = fresh_price(a.prices, sym, now)
            if stale:
                raise TicketError("%s. Refresh it (trader-alerts: alerts.py quote %s --write), give a price with "
                                  "--limit, or use --market" % (stale, sym))
            tick = tick_size(last)
            px = round_tick(last - max(last * FLATTEN_BUFFER_PCT / 100.0, tick), "down")
            how = "LIMIT %s, a little under the last %s (%s IST) so it fills at once" % (
                _p(px), _p(last), to_ist(t).strftime("%H:%M"))
        k.update({"order_type": "LIMIT", "price": px})
    tag = "tdx" + hashlib.sha256(("%s|%d|%s|%s|%s" % (sym, qty, k["order_type"], k.get("price"), today))
                                 .encode("utf-8")).hexdigest()[:8]
    k.update({"validity": "DAY", "tag": tag})
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "15:10"
    order = {"symbol": sym, "side": "sell", "qty": qty, "kite": k,
             "why": "flat by %s IST: closes the %s day trade before Zerodha's MIS auto square-off (qty from %s)"
             % (flat_by, sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(paper),
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, describe(k), flat_by))
    print("Price: %s." % how)
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against get_positions before you say yes." % source)
    state = load_state(a.state)
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    if paper:
        open_stops = [s for s in state.get("paper_stops", []) if s.get("status") == "open" and s.get("symbol") == sym]
        for s in open_stops:
            print("  3. ticket.py paper-fill. The resting paper stop %s (trigger %s) is canceled when it fills."
                  % (s["id"], _p(s["trigger"])))
        if not open_stops:
            print("  3. ticket.py paper-fill.")
    else:
        legs = [r for r in state.get("orders", []) if r.get("status") == "stop_placed" and r.get("symbol") == sym
                and rec_day(r) == today and not r.get("paper")]
        print("  3. Cancel the resting stop leg FIRST, or it and this sell could both fill and leave you short:")
        if legs and legs[-1].get("broker_order_id"):
            print("     cancel_order with {\"variety\": \"regular\", \"order_id\": \"%s\"} (Needs approval)."
                  % legs[-1]["broker_order_id"])
        else:
            print("     get_orders, find the open SL-M sell for %s, then cancel_order with its variety and"
                  " order_id (Needs approval)." % sym)
        print("  4. place_order with the ticket's JSON, then ticket.py result.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % inr(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        tag = " (paper)" if r.get("paper") else ""
        print("%s  %s %-11s %s%s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra, tag))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets, your yes and the paper simulator (trader-broker). "
                                             "Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this India time, e.g. \"2026-10-28 09:45\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in India)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order and its stop leg")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("paper-fill", help="paper mode: fill the approved order at a fresh price")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("paper-check", help="paper mode: trigger paper stops and targets from fresh prices")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    clock(p)
    p = sub.add_parser("result", help="live mode: record what happened after the Allow once / Deny card")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--leg", choices=["entry", "stop"], default="entry")
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--order-id", help="Kite's order_id from place_order")
    p.add_argument("--broker-id", help=argparse.SUPPRESS)
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in India)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="an MIS sell that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from get_positions (default: trader/portfolio.json)")
    p.add_argument("--market", action="store_true", help="a MARKET sell instead of a LIMIT under the last price")
    p.add_argument("--limit", help="a LIMIT sell at this price")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "paper-fill": cmd_paper_fill, "paper-check": cmd_paper_check, "fill": cmd_fill,
                "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
