---
name: trader-scanner
description: >-
  Chart Scanner for India's NSE. Scans thousands of NSE charts for setups:
  opening-range breakouts, flags, oversold bounces. Builds the list of
  EQ-series stocks from NSE's equity list (BE and BZ trade-to-trade stocks
  out, 2 % and 5 % circuit-band stocks out by default), with Nifty 50 and
  Nifty 500 presets, runs a pre-market scan of daily closes (20-day
  closing-high breakout, bull flag, reclaimed the 20-day average, oversold,
  oversold bounce) with rupee price and turnover floors, and an intraday
  opening-range breakout scan on 5-minute bars from 9:15 AM IST, then
  writes trader/watchlist.json for trader-news and trader-entry. Free Yahoo
  data (.NS symbols), bar by bar. Use when the user says "scan the Nifty",
  "scan the market", "intraday setups", "NSE breakouts", "find me setups",
  "what is breaking out", "opening range breakouts", "ORB scan", "scan for
  bull flags", "oversold stocks", "pre-market scan", "build my watchlist",
  "what should I look at today", or at the start of a trading day before
  /trader-news.
argument-hint: "[universe | daily | orb] [--index nifty50] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
free NSE charts and lists the ones that match a rule. That is all it does. A
scan is a list of charts that match a rule, not a list of stocks to buy, and
a setup on a chart is not a prediction. SEBI's own study found that about 7
in 10 individual intraday traders in equity cash lost money; the scanner does
not change that. It only saves you from flipping through 2,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                          # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe --index nifty500         # only the Nifty 500
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                             # before 9:15 AM -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --index nifty50             # or --symbols RELIANCE,SBIN,M&M, --watchlist FILE
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                               # after 9:35 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                    # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 50` (rupees), `--min-turnover` in rupees traded, price
times volume (daily 1e8 = Rs 10 crore, orb 0), `--min-volume` in shares (0),
`--index nifty50|nifty500`, `--skip-bands 2,5` (universe; `none` keeps every
band), `--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-28 10:15"` (India time) to pin the clock, `--offline PATH` to
read saved files, `--lists DIR` for a saved index list, `--no-write`,
`--show N`, `--json`. Exit code 0 when the scan found something, 1 when it
found nothing or could not run (the reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name\|Band` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of` (IST), `universe`, `scanned`, `failed`, `filters`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 14:45) |

Symbols are NSE's own (`RELIANCE`, `M&M`, `BAJAJ-AUTO`). The tool adds `.NS`
for Yahoo and encodes the `&` itself.

## The universe

`universe` downloads two public files from NSE's archive host,
`nsearchives.nseindia.com`, with plain GETs (never the `www.nseindia.com`
API, which sits behind a bot check):

- `content/equities/EQUITY_L.csv`: every listed equity with its series.
- `content/equities/sec_list.csv`: each security's price band (`No Band`
  for the F&O stocks, else 2, 5, 10 or 20 %).

It keeps a row when:

1. Its series is **EQ**. BE and BZ are trade-to-trade (every trade is for
   delivery, no intraday), and anything else is not a plain equity.
2. Its price band is not in `--skip-bands` (2 and 5 by default: a stock
   that hits a 2 % or 5 % circuit cannot be traded out of that day). A stock
   missing from `sec_list.csv` is kept with band `?`.
3. With `--index nifty50` or `--index nifty500`: it is in NSE's index list
   (`content/indices/ind_nifty50list.csv`, `ind_nifty500list.csv`).

On the Oct 29 2026 files: 2,620 rows, 2,368 EQ, and 2,286 after the 5 % band
(82) is left out (no EQ stock sat in the 2 % band that day). The Nifty 50
preset keeps all 50 (all `No Band`); the Nifty 500 keeps 493 (two 5 % band
names out; three REITs, two names moved to BE and a placeholder row are not EQ
rows of the equity list, and the tool names them).

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so 2,286
symbols take 115 requests. Liquidity comes from the spark meta: last price
at least `--min-price` (Rs 50) and price times volume at least
`--min-turnover` (Rs 10 crore). Run it before 9:15 AM or after 3:30 PM: during
the session the volume is the day's so far, and today's daily bar is left out
until the 3:30 PM close.

Yahoo's NSE daily series skips a session for a stock now and then. A chart
whose last close is older than the latest close in the scan is **stale**: it
is counted and not scanned, so a setup is never read off an old close.

The setups are computed from daily closes only (spark has no highs or lows):

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Hits are listed most traded first (rupees traded, price times volume).

## The opening-range breakout scan (during the session)

NSE trades 9:15 AM to 3:30 PM IST: 75 five-minute bars, 9:15 to 15:25. The
opening range is the first `--or-minutes` (15) of the session: the 9:15, 9:20
and 9:25 bars. A hit is the first completed 5-minute bar to CLOSE above the
range high, at that bar's close time (the 9:30 bar closes at 9:35 AM).

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on if a completed bar after the range closed at or
   above the highest close of the range (a necessary condition: the range
   high is at least every range close), or if a bar after the range is
   missing in the spark answer (it could be the breakout).
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. Stage 2 checks at most `--max-confirm` (200), the most
   traded first, and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

A chart is usable when every range bar is there and at most 3 bars after it
are missing. Yahoo's NSE feed leaves a few bars empty: the 15:20 bar nearly
every day, often 15:15 or 15:25, a midday bar now and then. A hit that came
after a missing bar says so (`missing_before`). Null closes are dropped, a
bar off the 5-minute grid (the live tick, like 13:35:17) is dropped, and a bar
counts only once it is complete (start + 5 minutes). Before 9:35 AM, on a
weekend or NSE holiday, after the close, or after `no_entries_after` (an
expired setup) it prints one plain line and writes nothing.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 19 of 50 charts (31 failed or
  had no data)". Never round a partial scan up to "the whole market".
- India time is UTC+5:30 all year; the tool never reads the machine's time
  zone. NSE holidays for 2026 are built in (NSE's circular). For a date in a
  year the tool does not carry yet it prints a warning instead of guessing.
  A Muhurat session (Diwali) uses NSE's announced times once they are in the
  calendar; until then the day counts as closed and the tool says why.

## Steps

1. Once a week or so: `scan.py universe` (or `--index nifty500` for a
   quicker, more liquid list).
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, stale, scanned, hits) and the top few by setup.
3. After 9:35 AM IST (10:15 is the classic): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news SYMBOL` (what is moving
   it), then `/trader-entry SYMBOL` (the entry bar by bar). Never pick for them.

## Example: Wed Oct 28 2026

Scan at 10:15 AM IST over five real v8 charts (`tests/fixtures/_nse/`):

```
python3 scan.py orb --offline charts/ --now "2026-10-28 10:15"
Opening-range breakout scan, Wed 2026-10-28 10:15 AM IST: 15-minute range (9:15 AM to 9:30 AM), 5-minute bars, Yahoo.
Fetched 5 of 5 charts; 5 usable; 5 candidates, 5 confirmed with real highs and lows.
2 of 5 charts closed above their opening-range high by 10:15 AM:
   9:45 AM  HDFCBANK        Rs 678.00  range 669.50 to 676.80  +0.18% over
   9:45 AM  RELIANCE      Rs 1,199.60  range 1187.00 to 1194.80  +0.40% over
```

RELIANCE's range is the 9:15 to 9:25 bars, high 1194.80, low 1187.00; the
9:40 bar closed at 1199.60, 0.40 % over the high, at 9:45 AM. By 2:45 PM
M&M (10:50 AM) and SBIN (11:25 AM) had broken theirs too. The pre-market scan
the next morning over 19 Nifty names (closes through Oct 28) tags LT a 20-day
breakout and a reclaim of the 20-day average, INFY and TCS bull flags, and
M&M a reclaim; four charts had no Oct 28 bar on Yahoo and were left out as
stale.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data (IST).
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, never calls NSE's website API, and never promises
that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite Connect keys live in environment variables or the Claude Desktop config, never in the chat.
