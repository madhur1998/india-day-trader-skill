#!/usr/bin/env python3
"""Position sizing for trader-budget. Standard library only, no network.

Default mode: sizes ONE intraday trade on NSE from trader/plan.json (written
by trader-exit) to your budget and your max loss per trade, then writes one
order to trader/orders.json in the exact shape of the Kite MCP place_order
tool: the entry ("kite"), its protective stop ("stop_leg", an SL-M sell that
goes to the exchange once the entry fills) and the target (watched by
trader-alerts: Kite has no bracket orders for equity, and two live sells for
one position could both fill and leave you short). It never places
anything: trader-risk checks the order and trader-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit 1168.50 --write          # a LIMIT at 1168.50 (default: the plan's entry)
    python3 size.py --market                         # a MARKET order instead (opt-in)
    python3 size.py --now "2026-10-28 09:45" --json  # pin the clock (India time)

How many shares. Whole shares only: NSE has no fractions.

    shares = the most whole shares inside every one of
      max loss       qty x (entry - stop), plus the round-trip charges at the stop
                     ("count_charges": true, the default), within max_loss_per_trade_inr
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) / entry, times mis_leverage only
                     when "allow_margin" is true (and never more than the cash in
                     trader/portfolio.json, if you have one)

The entry it sizes at: the LIMIT price (the plan's entry unless you give
one). With --market: the plan's entry, or the price in trader/prices.json if
that is fresh (under 15 minutes old) and higher, because a price that ran up
since the plan means a bigger loss at the same stop. Zero shares (the stop is
too far away for your max loss) is a plain refusal: no order is written. So
is a reward:risk, net of charges, under min_reward_risk.

Prices go on NSE's tick grid (round_tick): the entry to the nearest tick,
the stop down, the target up. Charges are Zerodha's intraday rates (CHARGES
below), overridden by the plan's "charges_rates" and then by "charges" in
trader/config.json.

Weights mode (kept from v1, no longer the headline): split a budget across a
list of NSE stocks by weight, in whole shares.

    python3 size.py --weights RELIANCE=30,HDFCBANK=25,INFY=20,SBIN=15,M&M=10 --prices trader/prices.json
    python3 size.py --targets my-weights.json --budget 100000 --write

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   tickers are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_inr is dropped and its weight
                   shared out, so you never place a Rs 50 order
  5. rounding      whole rupees, then whole shares at the prices you give
                   (trader/prices.json); the rupee that rounding frees goes to
                   the biggest position that is not capped

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

# the defaults of config.example.json. Missing keys fall back to these.
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 80000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 500,
    "max_daily_loss_inr": 1500,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "15:10",
    "product": "MIS",
    "mis_leverage": 1,
    "count_charges": True,
    "charges": {},
    "broker": "zerodha",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
}
EPS = 1e-9
FRESH_MINUTES = 15
PRODUCTS = ("MIS", "CNC")
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- the NSE block (verbatim from tests/_nse.py)

IST = datetime.timedelta(hours=5, minutes=30)


def to_ist(utc):
    """Naive or aware UTC -> naive India time."""
    return utc.replace(tzinfo=None) + IST


def ist_to_utc(local):
    """Naive India time -> naive UTC."""
    return local.replace(tzinfo=None) - IST


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def parse_now(text, error=SizeError):
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
    """"2026-10-28T04:15:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or utc_now()
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def check_config(cfg):
    """A dollar key left over from the US pack is an error, never a silent
    fallback: a rupee amount under a dollar name is a bug waiting to happen."""
    for k in sorted(cfg or {}):
        m = re.match(r"^(\w+)_usd$", k)
        if m:
            raise SizeError("your config still has \"%s\": this pack trades rupees on NSE. Rename it to "
                            "\"%s_inr\" and give it a rupee amount" % (k, m.group(1)))
    if (cfg or {}).get("fractional"):
        raise SizeError("\"fractional\": true is not possible on NSE (whole shares only): remove it")
    prod = str((cfg or {}).get("product") or "MIS").upper()
    if prod not in PRODUCTS:
        raise SizeError("\"product\" must be MIS (intraday) or CNC (delivery), got %r" % prod)
    if not isinstance((cfg or {}).get("charges") or {}, dict):
        raise SizeError("\"charges\" must be an object like {\"brokerage_max\": 20}")
    return cfg


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return check_config(cfg)


def _f(x):
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": str(r["ticker"]).strip().upper(), "name": r["name"] or "", "weight": w})
    return out


def weights_from_arg(text):
    """RELIANCE=30,M&M=10 -> {ticker: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants SYMBOL=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[t.strip().upper()] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[str(t).upper()] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False).
    A quote in another currency than rupees is not a price for an NSE order."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    cur = (((doc.get("detail") or {}).get(sym) or {}).get("currency") or doc.get("currency") or "INR")
    if str(cur).upper() != "INR":
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def shares_for(rupees, per_share):
    """Whole shares that fit in rupees at per_share, in exact integer paise."""
    u, p = int(round(float(rupees) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _p(x):
    """1169.4 -> "1169.40"."""
    return "%.2f" % float(x)


def _num(x):
    """75 -> "75", 1.5 -> "1.5"."""
    x = round(float(x), 4)
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return ("%.4f" % x).rstrip("0")


def rates_for(plan, cfg):
    """Zerodha's intraday rates, then the plan's effective rates, then your config's."""
    r = dict(CHARGES)
    pr = (plan or {}).get("charges_rates")
    if isinstance(pr, dict):
        r.update(pr)
    r.update((cfg or {}).get("charges") or {})
    return r


def loss_at_stop(qty, entry, stop, risk_p, rates, count):
    """(rupees lost if the stop fills at the stop, the charges in it)."""
    if qty <= 0:
        return 0.0, 0.0
    ch = charges(qty, entry, stop, rates)["total"] if count else 0.0
    return round(qty * risk_p / 100.0 + ch, 2), ch


def shares_for_loss(max_loss, entry, stop, risk_p, rates, count):
    """The most whole shares whose loss at the stop, charges included, fits max_loss."""
    n = shares_for(max_loss, risk_p / 100.0)
    while n > 0 and loss_at_stop(n, entry, stop, risk_p, rates, count)[0] > max_loss + 0.005:
        n -= 1
    return n


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, budget=None, limit="plan", price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the LIMIT price: "plan" (the default) for the plan's entry, None for a MARKET order
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  India date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    check_config(c)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in India: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a day trade needs entry, "
                            "stop and target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    tick = _f(plan.get("tick_size")) or _f(c.get("tick_size")) or None
    rates = rates_for(plan, c)
    count = bool(c.get("count_charges", True))
    product = str(c.get("product") or "MIS").upper()

    entry_plan = round_tick(entry_plan, "nearest", tick)
    if limit == "plan":
        limit = entry_plan
    if limit is not None:
        ref, ref_from = round_tick(limit, "nearest", tick), "limit"
    elif price is not None and round_tick(price, "nearest", tick) > entry_plan:
        ref, ref_from = round_tick(price, "nearest", tick), "price"
    else:
        ref, ref_from = entry_plan, "plan"
    stop, target = round_tick(stop, "down", tick), round_tick(target, "up", tick)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_p(stop), _p(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_p(target), _p(ref)))
    risk_p = int(round((ref - stop) * 100))
    reward_p = int(round((target - ref) * 100))
    risk = risk_p / 100.0

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (inr(budget), inr(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (inr(cash), inr(cash_buffer))
    available = max(round(available, 2), 0.0)
    lev = 1.0
    if c.get("allow_margin") and product == "MIS":
        lev = max(_f(c.get("mis_leverage")) or 1.0, 1.0)
    buying_power = round(available * lev, 2)
    if lev > 1:
        cash_note += " x %s MIS leverage" % _num(lev)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        n = shares_for_loss(max_loss, ref, stop, risk_p, rates, count)
        how = "%s max loss / %s risk a share%s" % (inr(max_loss), _p(risk), " + charges" if count else "")
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": n, "how": how})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), inr(budget), inr(cap), _p(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (inr(max_order), _p(ref))})
    limits.append({"name": "cash", "inr": buying_power, "per": ref, "shares": shares_for(buying_power, ref),
                   "how": "%s = %s / %s" % (cash_note, inr(buying_power), _p(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss, ch_stop = loss_at_stop(qty, ref, stop, risk_p, rates, count)
    rr_gross = round(reward_p / float(risk_p), 2)
    rr = rr_gross
    if count and qty > 0:
        net_reward = qty * reward_p / 100.0 - charges(qty, ref, target, rates)["total"]
        rr = round(net_reward / loss, 2) if loss > 0 else 0.0
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            one, one_ch = loss_at_stop(1, ref, stop, risk_p, rates, count)
            refusal = ("one share risks %s (entry %s minus stop %s%s), more than your %s max loss per trade. "
                       "No order. Skip this trade, or plan a closer stop with trader-exit."
                       % (inr(one), _p(ref), _p(stop), (", plus %s charges" % inr(one_ch)) if count else "",
                          inr(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, inr(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and rr < _f(c["min_reward_risk"]) - EPS:
        refusal = ("reward:risk %.2f%s (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (rr, " net of charges" if count else "", _p(target), _p(ref), _p(stop),
                      _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, inr(cost), inr(c["min_order_inr"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": entry_plan,
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": rr, "reward_risk_gross": rr_gross,
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "leverage": lev, "buying_power": buying_power, "count_charges": count,
        "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "charges_at_stop": ch_stop, "max_loss": loss,
        "flat_by": plan.get("flat_by") or c.get("flat_by"), "product": product,
        "order_type": "LIMIT" if limit is not None else "MARKET", "refusal": refusal,
    }


def order_tag(*parts):
    """A Kite tag: alphanumeric, at most 20 characters, the same for the same order."""
    blob = "|".join(str(p) for p in parts)
    return "tds" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def kite_order(s):
    """The one proposed order: the place_order arguments for the entry ("kite") and its
    stop ("stop_leg"), the target, plus why and plan (notes, never sent to Kite)."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    qty = int(s["qty"])
    tag = order_tag(s["symbol"], qty, s["order_type"], _p(s["entry"]), _p(s["stop"]), _p(s["target"]),
                    s["date"] or "")
    kite = {"variety": "regular", "exchange": "NSE", "tradingsymbol": s["symbol"], "transaction_type": "BUY",
            "quantity": qty, "product": s["product"], "order_type": s["order_type"]}
    if s["order_type"] == "LIMIT":
        kite["price"] = s["entry"]
    kite.update({"validity": "DAY", "tag": tag})
    stop_leg = {"variety": "regular", "exchange": "NSE", "tradingsymbol": s["symbol"], "transaction_type": "SELL",
                "quantity": qty, "product": s["product"], "order_type": "SL-M", "trigger_price": s["stop"],
                "validity": "DAY", "tag": tag + "sl"}
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    bound = {"max loss": "%s max loss per trade" % inr(s["max_loss_per_trade_inr"] or 0),
             "position cap": "position cap", "max order": "max order",
             "cash": "cash after the buffer"}[s["bound_by"]]
    why = ("%s on %s: %d shares, bound by your %s (%d x %s%s = %s at the stop)"
           % (setup, s["symbol"], qty, bound, qty, _p(s["risk_per_share"]),
              (" + %s charges" % inr(s["charges_at_stop"])) if s["count_charges"] else "", inr(s["max_loss"])))
    return {
        "symbol": s["symbol"], "side": "buy", "qty": qty, "kite": kite, "stop_leg": stop_leg,
        "target": s["target"], "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "charges_at_stop": s["charges_at_stop"],
                 "max_loss": s["max_loss"], "setup": s["setup"], "entry_time": s["entry_time"],
                 "flat_by": s["flat_by"]},
    }


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _p(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_p(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _p(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "the limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f%s."
                 % (_p(s["entry"]), src, _p(s["risk_per_share"]), _p(s["entry"]), _p(s["stop"]),
                    s["reward_risk"], (" net of charges (%.2f before)" % s["reward_risk_gross"])
                    if s["count_charges"] and s["qty"] else ""))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-66s %4d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s. Max loss at the stop: %d x %s%s = %s."
                 % (s["qty"], s["symbol"], inr(s["cost"]), _p(s["entry"]), s["qty"], _p(s["risk_per_share"]),
                    (" + %s round-trip charges" % inr(s["charges_at_stop"])) if s["count_charges"] else "",
                    inr(s["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _p(s["stop"]))
    lines.append("")
    k, sl = order["kite"], order["stop_leg"]
    lines.append("Proposed order (Kite place_order arguments, nothing has been placed):")
    how = "LIMIT %s" % _p(k["price"]) if k["order_type"] == "LIMIT" else "MARKET"
    lines.append("  entry     BUY %d %s, %s, %s, DAY, tag %s" % (k["quantity"], k["tradingsymbol"], how,
                                                               k["product"], k["tag"]))
    lines.append("  stop leg  SELL %d %s, SL-M trigger %s, %s, DAY: placed once the entry fills"
                 % (sl["quantity"], sl["tradingsymbol"], _p(sl["trigger_price"]), sl["product"]))
    lines.append("  target    %s: watched by trader-alerts, not a second sell at Kite (two live sells for one "
                 "position could both fill and leave you short)" % _p(order["target"]))
    if s["order_type"] == "MARKET":
        lines.append("A MARKET order through the API may need market protection; the default LIMIT avoids that.")
    if s["flat_by"]:
        lines.append("Flat by %s India time, before Zerodha's MIS auto square-off (about 15:20, and it costs a "
                     "fee): trader-alerts rings, trader-broker flatten." % s["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you yes or no (one yes covers the entry "
                 "and its stop leg).")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {str(b).upper() for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) rupees per ticker with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: rupees}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_rupee(exact, capped, order, cap):
    """Round to whole rupees and keep the total.

    The total is the exact total rounded DOWN to the rupee, so it never
    spends money you do not have. Each slice is rounded to the nearest rupee,
    then the difference (usually one rupee) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    cap_u = int(math.floor(cap + EPS))
    total_u = int(math.floor(sum(exact.values()) + EPS))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: float(u) for t, u in out.items()}


def whole_shares(rupees, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(rupees[t] / prices[t] + EPS)) if rupees[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = rupees[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict).
    Whole shares only, so every stock needs a price."""
    check_config(cfg)
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order = float(cfg.get("min_order_inr", 500) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")
    missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
    if missing:
        raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                        "e.g. with trader-alerts: alerts.py quote %s --write)"
                        % (", ".join(missing), " ".join(missing)))

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    investable = round(budget - cash_buffer, 2)
    cap = round(budget * cap_pct / 100.0, 2)

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        rupees = round_to_rupee(exact, capped, order, cap)
        shares = whole_shares(rupees, prices, order, cap, sum(rupees.values()))
        rupees = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if rupees[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if shares[worst["ticker"]] == 0:
            reason = ("one share costs %s, more than its slice; it cannot be bought in whole shares"
                      % inr(prices[worst["ticker"]]))
        else:
            reason = "its slice (%s) is under your %s minimum order" % (inr(rupees[worst["ticker"]]),
                                                                        inr(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        positions.append({"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
                          "source_weight": r["source_weight"], "rupees": rupees[t],
                          "capped": t in capped, "price": prices[t], "shares": shares[t]})
    invested = round(sum(p["rupees"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "max_position_pct": cap_pct, "cap": cap,
        "min_order_inr": min_order, "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped,
    }


def orders_from(result, source=""):
    """Weights mode: one proposed buy per position, in Kite place_order shape. A split is
    not a day trade, so the product is CNC (delivery), a LIMIT at the price it was sized at."""
    out = []
    for p in result["positions"]:
        if p["shares"] <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), inr(result["budget"]))
        price = round_tick(p["price"], "nearest")
        tag = order_tag("w", p["ticker"], p["shares"], _p(price))
        out.append({"symbol": p["ticker"], "side": "buy", "qty": int(p["shares"]),
                    "kite": {"variety": "regular", "exchange": "NSE", "tradingsymbol": p["ticker"],
                             "transaction_type": "BUY", "quantity": int(p["shares"]), "product": "CNC",
                             "order_type": "LIMIT", "price": price, "validity": "DAY", "tag": tag},
                    "why": why})
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, investing %s, cap %s%% = %s per stock"
                 % (inr(r["budget"]), _num(r["cash_buffer_pct"]), inr(r["cash_buffer"]),
                    inr(r["investable"]), _num(r["max_position_pct"]), inr(r["cap"])))
    lines.append("")
    lines.append("%-11s %-22s %8s %16s %12s %7s  note" % ("symbol", "name", "weight", "rupees", "price", "shares"))
    for p in r["positions"]:
        row = "%-11s %-22s %7.2f%% %16s %12s %7d" % (p["ticker"], (p["name"] or "")[:22], p["weight"],
                                                     inr(p["rupees"]), _p(p["price"]), p["shares"])
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-11s %-22s %8s %16s" % ("", "invested", "", inr(r["invested"])))
    lines.append("%-11s %-22s %8s %16s" % ("", "cash buffer", "", inr(r["cash_buffer"])))
    lines.append("%-11s %-22s %8s %16s" % ("", "left idle", "", inr(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        idle_capped = all(p["capped"] for p in r["positions"])
        lines.append("Left idle %s: %s" % (inr(r["idle"]), (
            "every stock is at its cap; add stocks or raise max_position_pct" if idle_capped
            else "whole shares do not divide evenly")))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (Kite place_order arguments, CNC delivery, nothing has been placed):")
    for o in orders:
        k = o["kite"]
        lines.append("  BUY %d %s, LIMIT %s, %s, %s" % (k["quantity"], k["tradingsymbol"], _p(k["price"]),
                                                       k["product"], k["validity"]))
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until trader-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_ist(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    if a.market and a.limit is not None:
        raise SizeError("--market and --limit are two different orders: pick one")
    limit = None
    if not a.market:
        if a.limit is not None:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 1168.50")
        else:
            limit = "plan"
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            when = to_ist(t).strftime("%H:%M")
            if limit is None:
                price = p
                if p > (_f(plan.get("entry")) or 0) + 0.005:
                    notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk is "
                                 "bigger: sized at it." % (sym, _p(p), when, _p(plan.get("entry"))))
            elif p > (_f(plan.get("entry")) if limit == "plan" else limit) + 0.005:
                notes.append("%s last %s (as of %s IST) is above your limit %s: the order fills only if the "
                             "price comes back down to it." % (sym, _p(p), when,
                                                               _p(plan.get("entry") if limit == "plan" else limit)))
        elif p is not None and limit is None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_ist(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = kite_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed order (entry + stop leg) to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("trader/prices.json") and not a.prices:
        prices = load_prices("trader/prices.json")
    result = size(rows, cfg, a.budget, prices, a.top)
    orders = orders_from(result, source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size an NSE day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (rupees)")
    ap.add_argument("--limit", metavar="PRICE", help="the LIMIT price (default: the plan's entry)")
    ap.add_argument("--market", action="store_true", help="a MARKET order instead of a LIMIT")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this India time, e.g. \"2026-10-28 09:45\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list, whole shares)")
    g.add_argument("--weights", help="inline weights: RELIANCE=30,HDFCBANK=25")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    g.add_argument("--cents", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        if a.cents:
            raise SizeError("--cents is gone: NSE has no fractional shares, so weights mode buys whole shares")
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top:
            raise SizeError("--top works with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
