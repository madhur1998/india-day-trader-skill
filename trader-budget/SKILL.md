---
name: trader-budget
description: >-
  Budget Manager. Sizes every NSE intraday position to your budget and your
  max loss per trade, in rupees and whole shares. Reads the trade plan from
  trader-exit (entry, stop, target), finds the shares that fit inside four
  limits (max loss per trade including Zerodha's round-trip charges at the
  stop, position cap, max order, cash after your buffer, with MIS leverage
  only if you allow margin), says which limit decided it, and writes ONE
  order to trader/orders.json in the exact shape of the Kite MCP place_order
  tool: a LIMIT entry, its SL-M stop leg and the target. A stop too far away
  for your max loss is a plain refusal, not a smaller order. Nothing is
  placed. Use when the user says "how many shares of RELIANCE", "size this
  trade", "how much should I buy", "I have 1 lakh", "I only want to risk
  Rs 500 a trade", "what are the charges", "set my rules", "keep 10% in
  cash", or after /trader-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy RELIANCE at 1169.40, stop 1160.00")
into an exact order ("buy 46 shares"), inside rules the user sets once. The
stop decides the size: the further away the stop, the fewer shares. Charges
count too: on a small intraday move, brokerage, STT, exchange fees, stamp
duty and GST are a real part of the loss. It writes a proposal only.
trader-risk checks it and trader-broker asks the user yes or no. Intraday
trading loses most people money (SEBI's own studies say so). Sizing to a
max loss keeps each loss small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                        # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1168.50 --json # a LIMIT at 1168.50 instead of the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --market --write       # a MARKET order (opt-in)
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights RELIANCE=60,SBIN=40 --config split.json   # weights mode, below
```

`--now "2026-10-28 09:45"` pins the clock (India time, IST). A plan for
another day is refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time, tick size and charge rates (trader-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk for a MARKET order (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its cash (rupees) caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for, in rupees (the example: 1,00,000). Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop, charges included (default 500).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (Rs 80,000) and the smallest order (Rs 500).
- The day limits trader-risk enforces: **max_daily_loss_inr** (1,500), **max_trades_per_day** (3),
  **min_reward_risk** (1.0, net of charges), **require_stop** (true), **no_entries_before** /
  **no_entries_after** ("09:30" / "14:45") and **flat_by** ("15:10"), all India time. 15:10 is before
  Zerodha's MIS auto square-off (about 15:20, which costs a fee).
- **product**: "MIS" (intraday, the default) or "CNC" (delivery).
- **mis_leverage** (1) counts only when **allow_margin** is true. Leave both alone unless the user
  understands that leverage multiplies losses too.
- **count_charges** (true) and **charges** ({}): Zerodha's published intraday rates are built in;
  put any rate that differs from the user's contract note in `charges`, e.g. `{"brokerage_max": 20}`.
- **blocked**: NSE symbols they never want to trade.
- Keep **paper** true until they have watched it work in paper mode.

An old config with dollar keys (`max_loss_per_trade_usd` and the like) is an
error that names the rupee key to use instead. Rename the key and put a
rupee amount in it; never convert silently.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only: NSE has no fractions. The size is the most shares inside
all four limits:

| limit | shares |
| --- | --- |
| max loss | qty x (entry minus stop) + round-trip charges at the stop, within `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, never more than the cash in `trader/portfolio.json`; times `mis_leverage` only with `allow_margin` |

The entry it sizes at: a LIMIT at the plan's entry by default (a market
order through the API needs market protection, so MARKET is opt-in with
`--market`). For a MARKET order: the plan's entry, or the price in
`trader/prices.json` if it is fresh (under 15 minutes old) and higher. A
price that ran up since the plan means a bigger loss at the same stop, so
fewer shares. A lower price never shrinks the risk. Prices go on NSE's tick
grid: the entry to the nearest tick, the stop down, the target up.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk net of charges is under `min_reward_risk`,
the stop is not below the entry, the target is not above it (the move
already happened), the plan is for another day, the symbol is blocked, or
the plan is a short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees,
the charges and the max loss at the stop. Say plainly that a stop is an
order, not a promise: in a fast market it can fill below the stop. Then
hand over to `/trader-risk`.

## The order it writes (Kite shape)

Kite has no bracket orders for equity. A day trade is an entry, then a
protective SL-M stop on the exchange once the entry fills, and a target that
trader-alerts watches. Two live sell orders for one position could both
fill and leave the user short, so the target is not an order at Kite.

- `kite` is exactly the argument object for the Kite MCP `place_order` tool:
  numbers are numbers, `quantity` is a whole number, `tag` is at most 20
  letters and digits ("tds" + 8 hex characters from the order, the same every
  time for the same order).
- `stop_leg` is the stop's own `place_order` arguments: SELL, SL-M, the same
  symbol, product and quantity, `trigger_price` the stop, tag + "sl".
- `target`, `why` and `plan` are notes for trader-risk, trader-broker and
  trader-alerts. They are never sent to Kite.

## Example: the reel

RELIANCE on Wednesday 2026-10-28. Opening-range breakout: entry 1169.40 at
9:45 AM, stop 1160.00 (the opening-range midpoint, on the 0.10 tick), target
1188.20 (2R). Risk 9.40 a share. The example config: Rs 1,00,000 budget, 10%
cash buffer, Rs 500 max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | 46 x 9.40 = 432.40, + Rs 56.82 charges = Rs 489.22 (47 would be Rs 500.52) | **46 (decides it)** |
| position cap | 75% of Rs 1,00,000 = Rs 75,000 / 1169.40 | 64 |
| max order | Rs 80,000 / 1169.40 | 68 |
| cash | Rs 1,00,000 minus Rs 10,000 = Rs 90,000 / 1169.40 | 76 |

46 shares = Rs 53,792.40. Max loss at the stop: Rs 489.22 with charges.
Reward:risk is 2.00 before charges, 1.65 after. It writes:

```json
{"symbol": "RELIANCE", "side": "buy", "qty": 46,
 "kite": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
          "quantity": 46, "product": "MIS", "order_type": "LIMIT", "price": 1169.4, "validity": "DAY",
          "tag": "tds85567364"},
 "stop_leg": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
              "quantity": 46, "product": "MIS", "order_type": "SL-M", "trigger_price": 1160.0,
              "validity": "DAY", "tag": "tds85567364sl"},
 "target": 1188.2,
 "why": "opening-range breakout on RELIANCE: 46 shares, bound by your Rs 500.00 max loss per trade (46 x 9.40 + Rs 56.82 charges = Rs 489.22 at the stop)",
 "plan": {"entry": 1169.4, "stop": 1160.0, "target": 1188.2, "risk_per_share": 9.4, "charges_at_stop": 56.82,
          "max_loss": 489.22, "setup": "orb", "entry_time": "9:45 AM", "flat_by": "15:10"}}
```

With a Rs 60 max loss and the stop at 1100.00, one share would risk Rs 70.29
with charges: no order. As a MARKET order with a fresh 1180.00 quote, the
risk is 20.00 a share and reward:risk falls to 0.33: refused, because the
move it was waiting for has mostly happened.

## Weights mode (kept from v1)

`--weights RELIANCE=30,HDFCBANK=25` or `--targets FILE` (`targets.example.json`
here) splits a budget across a list by weight: the cash buffer comes off the
top, weights are renormalised to 100, any stock over `max_position_pct` is
pinned at the cap and its excess shared out, slices under the minimum order
are dropped, and the rest is bought in whole shares at the prices you give
(`--prices trader/prices.json`). With `"max_position_pct": 25` and the
2026-10-28 closes, the example gives 21 RELIANCE (capped), 33 HDFCBANK, 16
INFY, 14 SBIN and 4 M&M. The orders are CNC (delivery) LIMIT buys with no
stop: trader-risk blocks them while `require_stop` is true and counts each
one against `max_trades_per_day`. Use a separate config (`--config`) for a
long-term split. There is no `--cents`: NSE has no fractional shares.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices are unofficial and can lag. The exchange's price is the one that fills.
- The charges are an estimate from Zerodha's published rates; the contract note is the truth.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes F&O, margin
(unless allowed) or short positions.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite Connect keys live in environment variables or the Claude Desktop config, never in the chat.
