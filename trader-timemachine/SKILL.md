---
name: trader-timemachine
description: >-
  Time Machine. Replays every possible outcome of an NSE intraday trade
  before you risk a rupee: 10,000 simulated sessions plus every past time the
  setup fired. Takes the plan from trader-exit (entry, stop, target), rebuilds
  the rest of the day 10,000 times from the stock's own 5-minute bars over
  the last 60 days (9:15 to 15:30 IST, Yahoo .NS data), and re-runs the same
  rule on every past session. Prints the odds of target first, stop first, or
  neither by your square-off time, where the price sits then, the average R
  gross and after Zerodha intraday charges (brokerage, STT, stamp duty, GST),
  the history, a Nifty 50 context line and one honest verdict, then hands off
  to /trader-budget. Use when the user says "run the time machine", "replay
  this trade", "what are the odds", "simulate this trade", "monte carlo",
  "backtest this setup", "how often does RELIANCE hit the target", "is this a
  coin flip", "is it worth it after charges", or after /trader-exit writes a
  plan, before /trader-budget.
argument-hint: "[path to plan.json, or NSE symbol]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session, both before and after the charges a round trip
costs. It is a simulation built from the stock's own recent bars, not a
prediction. Intraday trading loses most people money (SEBI's own study found
that most individual intraday traders lose), and a good-looking replay does
not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:10      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol RELIANCE --date 2026-10-29 \
    --entry 1174.3 --stop 1163.3 --target 1185.3 --entry-time "9:40 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--qty N` (shares, for the charges), `--no-index` (skip the Nifty 50
request), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`,
or a folder holding `<SYMBOL>-60d5m.json` or `chart-<SYMBOL>-60d-5m.json.gz`,
with `&` and `^` dropped from the name: M&M is `MM`, ^NSEI is `NSEI`),
`--now "2026-10-29 10:15"` (India time) to pin the clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, and when present `qty`, `charges_rates` and `tick_size` |
| Yahoo chart API | one request for `<SYMBOL>.NS` (`range=60d&interval=5m`), one more for the Nifty 50 (`^NSEI`); free and unofficial, can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history, context}` plus the verdict. `mc` holds the odds, percentiles, gross and net R, the charges, the fan-chart bands, 160 sample paths and a 24-bin histogram of the price at the end |

## How it works

1. **The session.** NSE trades 9:15 to 15:30 India time (IST, no daylight
   saving): 75 five-minute bars, no early closes. Yahoo's free NSE bars stop
   a little early, though: the 15:20 bar is never there, and a 15:25 bar,
   when it shows up, is the closing-price print, not trading. So every replay
   ends at `--until`, 15:20 at the latest. That is also about when Zerodha
   squares off an MIS position itself (for a fee). Pass your flat-by time,
   usually `--until 15:10`.
2. **The pool.** It keeps only past sessions before the plan's date that
   have every bar from 9:15 to `--until`. Sessions with a gap, a Muhurat
   evening session, and today are left out, and named in the output. Every
   bar becomes four ratios to the bar before it: open, high, low and close
   over the previous close.
3. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
4. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until`. Green, which means
   target first or above the entry at `--until`. R is +1 at a 1R target, -1
   at the stop, and otherwise the distance from the entry at `--until`
   divided by the risk. A gap through the stop still counts as -1R here. A
   real fill can be worse, and a stock can hit its circuit limit and stop
   trading.
5. **Charges.** Every outcome also pays the round-trip intraday charges:
   brokerage (0.03 % or Rs 20 an order, whichever is lower), STT on the sell
   side, NSE transaction charges, SEBI fees, stamp duty on the buy side and
   GST, as Zerodha publishes them. They are worked out for the plan's `qty`,
   or for a nominal position of about Rs 1 lakh (Rs 1,00,000 divided by the
   entry, whole shares) when the plan has none, and the plan's
   `charges_rates` replace the default rates. R after charges = R minus the
   charges divided by (shares x risk). The output says which quantity it used.
   On a Rs 1 lakh position with a tight stop the charges are easily a tenth
   of R, so read the net line.
6. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions, with stops
   and targets rounded onto the NSE tick. That is what really happened, not
   a simulation, also gross and after charges.
7. **The context.** One line: where the Nifty 50 and the stock stood on the
   day at the entry bar, against the previous session's last bar. If the
   index request fails the line is left out; the run does not fail.
8. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even. Without charges that
   is 1 in (1 + R): 50 % at 1R, 33 % at 2R. With charges every win is a
   little smaller and every loss a little bigger, so the tool compares
   against the break-even share AFTER charges: loss / (win + loss), with the
   win and the loss in R net of charges. The thresholds are the same as
   before. Less than 5 points from break-even: "Close to a coin flip" when
   break-even is 50 %, "Close to break-even" otherwise. 5 to 15 points: "Leans
   toward the target" or "Leans toward the stop". 15 or more: "Strongly
   toward" one of them. Why the change: judged gross, a 1R plan that wins 52 %
   of the sided replays reads as a coin flip, but with charges taking 0.15R
   off each side it needs 57.5 %, and it really leans toward the stop. When
   the charges are bigger than the whole target, the verdict says so.
9. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R after charges when its sign goes against the lean.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the plan's day
is not an NSE trading day (a weekend, an NSE holiday, or a Muhurat day), when
the entry bar is not in the data (wrong date, not closed yet, a future day),
or when the plan does not make sense (a stop above a buy's entry, an entry
off the 5-minute grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --until 15:10 --write` (or the plan's flat-by
   time), so "neither" means still open at the time you would square off.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the end, the average R gross and after charges, the
   charges line (and whether it used their quantity or the Rs 1 lakh
   nominal), the history line, then the verdict line and any line under it
   exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Quarterly results, news, circuit limits and halts can do things those 60
   days never did. Check `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: RELIANCE

RELIANCE, Thursday 2026-10-29. The 15-minute opening range is 1,156.20 to
1,170.50. The 9:35 bar closes above it: entry 1,174.30 at 9:40 AM (bar 4),
stop 1,163.30 (the range midpoint 1,163.35, rounded down onto the 0.10 tick),
target 1,185.30 (1R, risk 11.00 a share). Run at 13:36 IST with the default
end of 15:20:

```
10,000 replays of the rest of the session, 9:40 AM to 3:20 PM (68 five-minute bars), seed 19.
  target first   24.2 %
  stop first     36.2 %
  neither        39.6 %   still open at 3:20 PM
  green          42.4 %   target first, or above the entry at 3:20 PM
  Price at 3:20 PM: median 1171.72, 90 % of replays between 1152.79 and 1189.65.
  Average R: -0.138 gross, -0.227 after charges (R = the 11.00 risk: +1R at the target, -1R at the stop).
  Charges (intraday round trip) on 85 shares (a nominal position of about Rs 1 lakh; give the plan
  a qty for your own size): Rs 82.47 at the target, Rs 82.40 at the stop. After charges the target
  is +0.91R and the stop -1.09R.
  Context at the 9:40 AM entry: Nifty 50 22758.40, +0.26 % on the day; RELIANCE +1.41 %.
History: the same rule on each of those 52 real sessions fired 26 times: 12 target, 10 stop,
4 neither. Average R +0.078 gross, -0.135 after charges.
Leans toward the stop: at 1R (0.91R after charges) the target has to come first in 54 % of the
replays that hit one side; it did in 40 % (target first 24.2 % vs stop first 36.2 %).
```

The real history was a little better than even before charges and below
zero after them. The replays lean toward the stop. That is the honest answer
for this setup on this day, and it is what the user should hear before
`/trader-budget` sizes anything.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R and the
  charges.
- Always give the R after charges next to the gross R. Gross alone flatters
  every intraday plan.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can lag. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite Connect keys live in environment variables or the Claude Desktop config, never in the chat.
