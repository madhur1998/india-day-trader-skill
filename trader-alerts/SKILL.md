---
name: trader-alerts
description: >-
  Price Alerts. Rings when an NSE price, a stop or the opening bell hits.
  Turns the trade plan into three alerts in one go (the stop, the target and
  "flat by close" at your square-off time, 15:10 by default, before Zerodha's
  MIS auto square-off), plus above / below / stop / target levels, percent
  moves from the previous close, any time of day, the 9:00 pre-open and the
  9:15 opening bell, each ringing once so you are not spammed. Knows NSE
  hours in India time (IST) with the 2026 trading holidays and Muhurat
  trading, fetches free delayed quotes from Yahoo (.NS) one symbol at a
  time, and writes trader/prices.json in rupees for sizing and the risk
  check. Use when the user says "alert me when RELIANCE hits 1200", "tell me
  if SBIN falls below 950", "watch my stop", "watch my trade", "remind me to
  square off by 3:10", "is the NSE open", "is the market open today", "is
  it a trading holiday", "when does the market open", "get me a price", or
  right after /trader-exit writes a plan.
argument-hint: "[NSE symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. On Zerodha the protective SL-M stop sits at
the exchange once the entry fills and does the work; the target is NOT an
order (Kite has no bracket orders for equity), so the target alert is the
user's cue to sell. The alerts also say when it is time to be flat.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by close from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell             # 9:15
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --pre-open                 # 9:00 to 9:15
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --stop 1163.3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --above 1200
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:10 --label "square off" --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol SBIN --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote RELIANCE "M&M" --write   # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset reliance-plan-stop
```

Symbols are NSE symbols: `RELIANCE`, `M&M` (quote it in the shell),
`BAJAJ-AUTO`; a `.NS` suffix is accepted and dropped. `--now "2026-10-29
15:10"` pins the clock (India time); `--offline DIR` reads saved quote files
(`DIR/yahoo-<SYMBOL>.json`, `&` dropped: `yahoo-MM.json`) instead of the
network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` (symbol, date, stop, target, flat_by) |
| `trader/config.json` | `flat_by`, when the plan has none |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes in rupees (`"currency": "INR"`), merged in, for trader-budget and trader-risk |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

`trader/prices.json` keeps machine times in UTC (`as_of`, `detail.time`,
`detail.fetched`, ending in `Z`, which trader-budget and trader-risk read)
and the same times in India time for people (`as_of_ist`, `detail.time_ist`).

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `time` | the India clock passed `at` ("15:10") on a trading day | once per trading day |
| `pre_open` | inside the pre-open, 9:00 to 9:15 (order entry 9:00 to 9:08) | once per trading day, only in that window |
| `opening_bell` | the NSE is open (9:15, or a Muhurat session's start) | once per trading day |

A rule with `"date"` is watched on that India date only; `--from-plan` rules
carry the plan's date, so yesterday's stop never rings today. The flat-by
alert defaults to 15:10: before Zerodha squares off MIS positions itself
(about 15:20, and it charges for it).

**No early closes.** NSE has none, so the US-era `"before_close"` field is
accepted in an old rules file and ignored, and `add --before-close` is
refused with a plain message. A time alert rings at its `at` every trading
day. On a Muhurat day a day-session time alert (say 15:10) stays quiet; add
one at the Muhurat session's own times instead.

## Quotes

- Yahoo only: `query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS`, an
  unofficial free JSON endpoint, one request per symbol, half a second
  apart. On a 429 it waits and tries once more; on a 429 or a 5xx after that
  it tries `query1`. It identifies itself as `india-day-trader-skill/3.0`.
  Stooq is gone: it has no NSE coverage.
- Free quotes are unofficial, delayed and can lag. They are fine for alerts
  and for checking a size, not for timing an order. The broker's price is the
  one that fills. Each price in `trader/prices.json` keeps its quote time, and
  trader-risk only uses one under 15 minutes old.
- If Yahoo is down, read the price from the Kite connector (`get_ltp`) and
  write it into `trader/prices.json` as
  `{"as_of": "...Z", "currency": "INR", "prices": {"RELIANCE": 1174.1}}`.

## The NSE clock

Regular session 9:15 to 15:30 India time (IST, UTC+5:30 all year, no
daylight saving), with the pre-open from 9:00 (order entry 9:00 to 9:08,
matching until 9:15). Closed on weekends and on the 2026 NSE trading
holidays: Jan 26 (Republic Day), Mar 3 (Holi), Mar 26 (Shri Ram Navami),
Mar 31 (Shri Mahavir Jayanti), Apr 3 (Good Friday), Apr 14 (Dr. Baba Saheb
Ambedkar Jayanti), May 1 (Maharashtra Day), May 28 (Bakri Id), Jun 26
(Muharram), Sep 14 (Ganesh Chaturthi), Oct 2 (Mahatma Gandhi Jayanti), Oct
20 (Dussehra), Nov 10 (Diwali Balipratipada), Nov 24 (Prakash Gurpurb Sri
Guru Nanak Dev), Dec 25 (Christmas). Confirm on NSE's holiday page.

- **Muhurat trading** on Diwali (Sunday 2026-11-08): a one-hour evening
  session whose times NSE announces by circular. Until they are in the tool
  the clock says the day is closed and why; once added, the clock, the
  opening bell and the pre-open follow the Muhurat session.
- **Other years.** The tool knows 2026 only. For 2027 and later it uses the
  weekend rule and says "holiday list unknown" every time, until NSE
  publishes the list (in December) and it is added.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by
   time, in rupees and IST.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop alert means the SL-M
   stop has probably triggered: check `get_positions` and `get_orders` and
   record the fill with trader-broker. A target alert means the price got
   there, and nothing has been sold: if the user wants out, trader-broker
   `flatten`, then trader-risk, then the user's yes. "Flat by close" means
   close anything still open the same way. Never act for them.

## Example: RELIANCE

RELIANCE on Thursday 2026-10-29: entry 1,174.30 at 9:40 AM, stop 1,163.30,
target 1,185.30, flat by 15:10. `add --from-plan` writes
`reliance-plan-stop`, `reliance-plan-target` and `reliance-flat-by` for that
day. A `check` at 13:36 reads the real 13:35 quote, Rs 1,174.10, between the
stop and the target:

```
NSE OPEN (2026-10-29 13:36 IST, regular session).
Nothing hit. 3 rule(s) watched today, RELIANCE Rs 1,174.10.
```

At 15:10:

```
ALERT Flat by close: it is 15:10 IST (set for 15:10), the market closes at 15:30. If RELIANCE is
still open, close it: trader-broker flatten --symbol RELIANCE builds the sell ticket, then
trader-risk and your yes. Zerodha squares off what is left of an MIS position itself at about
15:20, for a fee.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --pre-open` and `add --opening-bell`, the pre-open rings
from 9:00 and the bell at 9:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live. Free quotes are delayed and unofficial.
- Confirm every new rule back to the user: symbol, type, level in rupees,
  day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite Connect keys live in environment variables or the Claude Desktop config, never in the chat.
