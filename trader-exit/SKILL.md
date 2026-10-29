---
name: trader-exit
description: >-
  Exit Planner for NSE intraday trades. Sets your stop-loss (SL) and your
  target before you enter, so you know when to get out. Reads the entry from
  trader-entry (or a price you give), puts the stop at the opening-range
  midpoint by default (or the range low, your price, a percent, or a multiple
  of the ATR), rounds it down onto the NSE tick, and sets the target at 2R
  (or 1R, or your price), rounded up onto the tick. Counts the round-trip
  charges (brokerage, STT, exchange, SEBI, stamp duty, GST) in a net
  reward:risk. Refuses a stop at or above the entry, a target at or below it,
  or a stop too tight to survive the spread or the charges, and says why. Flat
  by 15:10 IST, before the MIS square-off. Writes trader/plan.json for the
  Time Machine, the Budget Manager and Price Alerts. Long side only. Use when
  the user says "where's my stop", "set my SL", "set my stop and target",
  "plan the exit on RELIANCE", "when do I get out", "stop loss", "what's my
  R", "risk reward after charges", or right after /trader-entry triggers,
  before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap, a fast market or a stock locked at its circuit can fill past it, or not
fill at all. Day trading loses most people money, and a neat plan does not
change that. In India the charges alone take a real bite out of every
intraday round trip; this plan shows how much.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --write                  # also save trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-low --target 1.5R
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol RELIANCE --entry 1199.6 --or-high 1194.8 --or-low 1187
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 15:00`, `--json`, `--now "2026-10-28 09:46"` (India
time), `--entry PRICE` with a waiting entry.json to plan ahead, `--setup
level`, `--offline FILE` (a saved Yahoo chart JSON for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, entry price, time, bar, opening range, any circuit warning |
| `trader/config.json` | `flat_by` (15:10), `min_reward_risk` (1.0: a note here, trader-risk enforces it), `count_charges` (true), `charges` (overrides of the rates), `tick_size` (pins the tick). A leftover `*_usd` key is an error naming its `*_inr` name |
| Yahoo chart API | only for an `atr:K` stop: the session's bars up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, `risk_per_share`, `reward_risk`, `reward_risk_net`, `tick_size`, `charges_rates`, flat by. trader-timemachine, trader-budget and trader-alerts read it |

`plan.json` holds the charge RATES, not rupees: the quantity is not known
yet. trader-budget and the others work out the rupees for their own quantity
from `charges_rates` (the defaults merged with your `charges` in the
config). `risk_per_share` stays gross: entry minus stop.

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **The tick.** NSE prices move in steps: Rs 0.01 under Rs 250, Rs 0.05 to
  Rs 1,000, Rs 0.10 to Rs 5,000, Rs 0.50 to Rs 10,000, then Rs 1 and Rs 5.
  The tick comes from the entry price, or `tick_size` in the config if NSE
  set this stock's tick from last month's close and it differs.
- **Rounding.** The stop rounds DOWN onto the tick (away from the entry, for
  a long). Risk = entry minus stop. The target is entry + R x risk, rounded UP
  onto the tick. `2R` by default; `1R`, `1.5R` or `price:X` also work.
- **Charges.** Intraday (MIS) rates as Zerodha publishes them: brokerage
  0.03% an order (capped at Rs 20), STT 0.025% on the sell, NSE transaction
  charge 0.00297%, SEBI Rs 10 a crore, stamp duty 0.003% on the buy, GST 18%
  on brokerage and fees. Per share it uses brokerage at the full 0.03% and
  leaves STT and stamp unrounded: the most a share can cost, exact for orders
  up to about Rs 66,667 a side. Your contract note is the truth; put your
  broker's rates in `charges` in the config.
- **Net reward:risk.** (reward minus the charges at the target) / (risk plus
  the charges at the stop), a share. `min_reward_risk` is checked against it
  (set `count_charges` to false for the gross figure).
- **Refused, with the reason.**
  - A stop at or above the entry. A target at or below it.
  - A risk under 0.05% of the price: too tight to survive the spread, normal
    wiggle would stop you out.
  - Round-trip charges a share at the stop above 25% of the risk a share:
    too tight to survive the charges. At 25% a 2R target is already only
    about 1.4R net, and every stop-out costs a quarter of an R more than it
    says. It tells you the smallest stop distance that passes.
  - An entry.json that has not triggered (unless you give `--entry`).
  - A short: this pack plans longs only.
- **Flat by.** 15:10 IST by default (or `flat_by` in the config). Out by
  then, win or lose, no overnight hold, and before brokers square off MIS
  positions themselves (about 15:20, for a fee). A flat-by after 15:20 gets
  a note; one outside 9:15 to 15:30 is refused. On a Muhurat evening session
  it moves with that session's close (15:10 becomes 18:40 for an 18:00 to
  19:00 session). An entry at or after the flat-by time is refused.
- **Holidays.** A plan for a weekend, an NSE holiday or a Muhurat day whose
  times are not out yet gets a note that the market is closed.

## Steps

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk gross and net of
   charges, and flat by. Pass on any note (a circuit warning, a closed day).
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade. Offer `/trader-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example: the reel

RELIANCE, Wednesday 2026-10-28. The entry is 1,199.60 at 9:45 AM (the 9:40
bar, bar 5) and the opening range is 1,187.00 to 1,194.80. `exit.py plan
--write`:

```
Exit plan: RELIANCE long, Wed 2026-10-28, opening-range breakout on 5-minute bars
  Entry         Rs 1,199.60 at 9:45 AM (bar 5)
  Stop          Rs 1,190.90  opening-range midpoint: (Rs 1,194.80 + Rs 1,187.00) / 2 = 1190.9
  Target        Rs 1,217.00  2R (two times the risk)
  Risk a share  Rs 8.70 (0.73% of the entry)
  Reward:risk   2.0
  Charges       about Rs 1.27 a share round trip at the stop, Rs 1.28 at the target
  Net of them   reward:risk 1.62
  Flat by       15:10 IST, win or lose: no overnight hold
```

With `--target 1.5R` the target is 1,199.60 + 13.05 = 1,212.65, rounded up to
Rs 1,212.70. With `--stop price:1195` the risk is Rs 4.60 a share and the
charges are about 28% of it: refused, too tight to survive the charges (it
asks for at least Rs 5.08). trader-budget then sizes the reel: a Rs 500 max
loss per trade with charges counted buys about 50 shares, not
floor(500 / 8.70) = 57.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk, charges
  a share. It never prints a P&L or a return.
- Nothing here promises winning trades or money. A 2R target is not a
  prediction that the price gets there.
- Long side only: this pack plans longs only.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite Connect keys live in environment variables or the Claude Desktop config, never in the chat.
