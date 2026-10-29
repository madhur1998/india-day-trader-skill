---
name: trader-risk
description: >-
  Risk Manager. Checks every NSE trade against your limits and stops anything
  over them, before it can reach Zerodha. For an intraday trade: a stop leg is
  required, the loss at the stop including round-trip charges must fit your
  max loss per trade in rupees, reward:risk net of charges must meet your
  minimum, the day's total risk must fit your daily loss limit, no more than
  your trades per day, and no new entries outside 09:30 to 14:45 India time,
  on weekends or NSE holidays. It validates the Kite place_order format
  (variety, exchange, product MIS or CNC, order type, price and trigger on the
  tick grid, quantity, tag, the SL-M stop leg), blocks F&O, BE and BZ series
  (no intraday), MIS shorts, margin you did not allow, prices outside the
  circuit band, blocked symbols, order size, position cap, cash buffer and
  duplicates. Getting out is never blocked by a day limit. Prints PASS or
  BLOCK with the reason and records the verdicts so trader-broker only shows
  an order that passed. Use when the user says "check my order", "is this
  safe", "risk check", "can I take this trade", "does this break my rules",
  "how much have I risked today", "is it too late for MIS", or before
  /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget or trader-broker's flatten passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to Kite. Most people who trade intraday lose money (SEBI's own
studies say so). These limits keep each loss and each day's losses small.
They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --series sec_list.csv     # EQ series only, plus the circuit band
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-10-28 09:45"  # pin the clock (India time)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders in Kite shape (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits in rupees (trader-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures a MARKET order's risk from; `prev_close` feeds the circuit check |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |
| `--series FILE` | optional: NSE's `EQUITY_L.csv` or `sec_list.csv` (from nsearchives.nseindia.com) |

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, reward:risk, blocked
list, size, duplicates) turns into a warning for an exit, never a BLOCK. A
square-off after 14:45 passes.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | the `kite` object is not valid `place_order` input: variety not regular or amo, exchange not NSE, transaction_type not BUY or SELL, product not MIS or CNC, order_type not MARKET / LIMIT / SL / SL-M or missing its `price` / `trigger_price` (or carrying one it should not), a price as a string or off the tick grid, validity not DAY or IOC, quantity not a positive whole number, a tag over 20 characters or with anything but letters and digits; a rupee notional |
| stop leg | not the opposite side, not SL-M or SL, a different symbol, product or quantity than the entry, or a trigger on the wrong side of the entry |
| asset | futures or options (an NFO symbol like NIFTY26NOV24000CE) while `allow_options` is false; anything that is not an NSE equity symbol |
| series | (with `--series`) the symbol is not EQ series: BE and BZ trade to trade, delivery only, no intraday |
| short | an MIS sell without the shares (an intraday short) while `allow_short` is false |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need margin; with `allow_margin` an MIS buy needs value / `mis_leverage` |
| circuit | (entries, with a band from `sec_list.csv` and a previous close) the entry is outside the day's price band |
| stop | (entries) no stop while `require_stop` is true (the stop leg, or the order's plan stop) |
| loss | (entries) qty x (entry minus stop) + round-trip charges at the stop over `max_loss_per_trade_inr` |
| reward:risk | (entries) net reward / net risk (both after charges) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` (09:30) or after `no_entries_after` (14:45), or the market is closed |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a LIMIT or SL order,
  the trigger for an SL-M. For a MARKET order, the HIGHER of the plan's
  entry and a fresh price from `trader/prices.json` (under 15 minutes old,
  in rupees). An older price is not used, and it says so.
- **Charges** are Zerodha's published intraday rates (brokerage 0.03% capped
  at Rs 20 an order, STT on the sell side, NSE and SEBI fees, stamp duty,
  GST), overridden by the plan's `charges_rates` and then the config's
  `charges`. `"count_charges": false` measures the bare price risk.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as trader-broker logged it. Three full-size trades use up a Rs 1,500 day.
- **The window** is India time (IST, UTC+5:30, no daylight saving), worked
  out in the tool, never from the computer's clock zone. Weekends and NSE
  holidays (the 2026 list) are closed; the session is 9:15 to 15:30. A
  Muhurat session counts only once its times are published. Set both
  `no_entries_*` to null to switch the window off.
- **The circuit band** comes from `sec_list.csv` (2, 5, 10 or 20 percent, or
  no band) and the previous close in `trader/prices.json`. A stock with a 2%
  or 5% band gets a warning: locked at the lower circuit, it cannot be sold,
  so the stop may not fill.

Warnings do not block, but say them to the user: a LIVE account, a field
`place_order` may not know, a stop that lives only in the plan and not at
the exchange, a price too old to use, an AMO, a narrow circuit band.

## Steps

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote RELIANCE --write`).
2. Run `check.py` (with `--series` if the user keeps NSE's `sec_list.csv`).
   Exit code 0 means every order passed, 1 means at least one was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with charges, reward:risk, and how much of today's limits it
   uses. For a BLOCK, give the reason and the fix (fewer shares, a closer
   stop from trader-exit, wait for the window, or skip the trade). Never
   change a limit yourself.
4. If the order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint (the `kite` entry and its
   `stop_leg`), so an edited order is not ready until it passes again, and a
   verdict from yesterday does not count today.

## Example: the reel

The RELIANCE order from trader-budget at 9:45 AM on Wednesday 2026-10-28, on
a fresh Rs 1,00,000 paper account with the example config:

```
PASS  #1 buy RELIANCE 46 shares MIS LIMIT, entry
          entry 1169.40 (the limit price), stop 1160.00, target 1188.20: risk 9.40 a share
          max loss Rs 489.22 incl. Rs 56.82 charges (limit Rs 500.00), reward:risk 1.65 (min 1)
          today before this: 0 of 3 trades, Rs 0.00 of Rs 1,500.00 daily loss

1 passed, 0 blocked at 2026-10-28 09:45 IST. Cash after the passed orders: Rs 46,207.60 (paper account).
```

One more share and the charges tip it over:

```
BLOCK #1 buy RELIANCE 47 shares MIS LIMIT, entry
        - loss: 47 shares x 9.40 risk a share + Rs 58.72 charges = Rs 500.52 at the stop, over your Rs 500.00 max loss per trade
```

At 9:20 the same order is blocked by the window (no new entries before
09:30 IST, while the opening range forms). The 15:05 sell that closes the
position passes, even though it is after the 14:45 entry cutoff: it is an
exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- Free Yahoo prices are unofficial and can lag; the exchange's price is the one that fills, and a
  stop can fill worse than its trigger in a fast market.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite Connect keys live in environment variables or the Claude Desktop config, never in the chat.
