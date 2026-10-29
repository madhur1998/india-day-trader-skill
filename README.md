# The India Day Trader skill

11 Claude skills for intraday trading on India's NSE: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a rupee.

Free, MIT, no signup, no API key for the data. The tools are plain Python
that read free public data: Yahoo Finance charts for NSE stocks and NSE's
public archive files (the equity list, circuit bands, corporate
announcements, insider-trading disclosures, bulk and block deals).

One scans thousands of NSE charts for setups. Another tracks breaking news
and exchange announcements on every stock you watch. One watches a setup bar
by bar and tells you the moment to get in, and another sets your stop and
your target before you do. The Time Machine replays the rest of the session
10,000 times from the stock's own recent bars, plus every past day the same
setup fired, and tells you the honest odds, after charges.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default.

> Ported from [Jake Schincariol's day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
> (US markets). This version is India only: NSE, IST, rupees, STT and
> charges, Zerodha Kite.

## Install

Paste this into Claude:

```
https://github.com/madhur1998/india-day-trader-skill

install skill
```

Or as a plugin, in Claude Code:

```
/plugin marketplace add madhur1998/india-day-trader-skill
/plugin install india-day-trader-skill@india-day-trader-skill
```

Claude Code namespaces plugin skills, so installed as a plugin they show up as
`/india-day-trader-skill:trader-scanner` and so on. Copy the folders instead
if you want plain `/trader-scanner`:

```bash
git clone https://github.com/madhur1998/india-day-trader-skill.git
cp -r india-day-trader-skill/trader-* ~/.claude/skills/
```

Project-local instead of global: copy the same folders into your repo's
`.claude/skills/`. No Claude Code at all? Paste any single `SKILL.md` at the
top of a chat and it runs as a mode. You lose the Python tools, but the method
works.

The tools need Python 3.8 or newer. Nothing to pip install.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans thousands of NSE charts for setups: opening-range breakouts, flags, oversold bounces. |
| `/trader-news` | News Monitor | Tracks headlines and NSE corporate announcements on every stock you watch and flags what moves it. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar and tells you the moment to get in. |
| `/trader-exit` | Exit Planner | Sets your stop and your target on the NSE tick grid before you enter, so you know when to get out. |
| `/trader-timemachine` | Time Machine | Replays every possible outcome of a trade before you risk a rupee: 10,000 simulated sessions plus every past time the setup fired, net of charges. |
| `/trader-risk` | Risk Manager | Checks every trade against your limits and stops anything over them. |
| `/trader-budget` | Budget Manager | Sizes every position to your budget and your max loss per trade, charges included. |
| `/trader-broker` | Order Executor | Sends approved orders to Zerodha Kite, paper trading by default, never without your yes. |
| `/trader-alerts` | Price Alerts | Rings when a price, a stop, the square-off time or the opening bell hits. |
| `/trader-analyst` | Earnings Analyst | Reads quarterly results and sums up what changed, in Indian fiscal quarters and crore. |
| `/trader-insider` | Insider Tracker | Reads promoter and director trades (SEBI insider-trading disclosures) and bulk and block deals as soon as NSE posts them. |

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, square off by 15:10                                      (while it is open)
news / analyst / insider                                                       (on a schedule)
```

The market day in India time (IST): pre-open 9:00 to 9:15, the session 9:15
to 15:30. The defaults: no new entries before 9:30 (after a 15-minute opening
range) or after 14:45, and flat by 15:10, ahead of the broker's automatic
square-off of intraday (MIS) positions.

An example: RELIANCE, Wednesday October 28 2026, real data (the test
fixtures reproduce every number).

1. **`/trader-scanner`** at 10:15 AM: RELIANCE and HDFCBANK both closed
   above their 15-minute opening range at 9:45.
2. **`/trader-entry`** watched RELIANCE bar by bar. Range 9:15 to 9:30:
   Rs 1,187.00 to Rs 1,194.80. At 9:45 it said triggered: the 9:40 candle
   closed at Rs 1,199.60, the first 5-minute close above the range.
3. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, Rs 1,190.90 (Rs 8.70 a share at risk), target 2R, Rs 1,217.00,
   both on NSE's tick grid; flat by 15:10 either way. Reward to risk 2.0, or
   1.62 once charges are counted.
4. **`/trader-timemachine`** replayed the rest of the day 10,000 times from
   RELIANCE's own 5-minute candles over its last 51 full sessions (seed 19):
   target first 7.5 %, stop first 46.4 %, still open at 3:20 PM 46.1 %.
   The same rule on those 51 real sessions fired 25 times: 7 target, 11 stop,
   7 neither, an average of -0.04R after charges. Its verdict: strongly
   toward the stop. That is the point of looking before you risk a rupee.
5. **`/trader-budget`** with a Rs 1,00,000 account and a Rs 500 max loss per
   trade: 50 shares (Rs 59,980). If the stop fills at Rs 1,190.90 the loss is
   50 x 8.70 = Rs 435.00 plus Rs 63.64 of charges, Rs 498.64. It writes one
   Kite order: BUY 50 RELIANCE, LIMIT 1199.60, MIS, with an SL-M stop leg at
   1190.90 to place once the entry fills.
6. **`/trader-risk`** checks it against your limits: the max loss (charges
   included), the daily loss limit, trades per day, reward to risk, the
   9:30 to 14:45 entry window, the EQ series, position size and cash. PASS or
   BLOCK, with the reason.
7. **`/trader-broker`** shows you the ticket in plain words and the exact
   `place_order` JSON. You type yes for that one order and its stop leg. In
   paper mode it fills locally; live, Claude calls the Kite MCP server and
   Claude's own "Allow once / Deny" card asks you one more time.
8. **`/trader-alerts`** rings on the stop, the target and at 15:10 to square
   off whatever is still open, before Zerodha's own MIS square-off.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader-scanner/scan.py universe                                # NSE equity list, EQ series
python3 trader-scanner/scan.py daily --index nifty500                  # pre-market setups
python3 trader-scanner/scan.py orb --at 09:45                          # opening-range breakouts so far
python3 trader-news/news.py --tickers RELIANCE --hours 12              # headlines, NSE announcements, the move
python3 trader-entry/entry.py check RELIANCE --setup orb               # waiting / triggered / expired
python3 trader-entry/entry.py watch RELIANCE --every 30                # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, square off
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/facts.py INFY                                   # quarters, growth, EPS
python3 trader-insider/insider.py check                                # new insider filings and deals
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on real data recorded on October 29 2026: 60 days of
5-minute bars for RELIANCE, HDFCBANK, INFY, SBIN and M&M, NSE's equity list
and circuit bands, its announcement and insider-trading feeds, and the day's
bulk and block deals.

## Fine print

**Not financial advice.** This is software that reads public data and does
arithmetic. It does not know your situation and it cannot see the future.
SEBI's own study found that 7 in 10 individual intraday traders in the
equity cash segment lost money in FY23. You can lose money. Decide for
yourself, and talk to a SEBI-registered investment adviser if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Set your Kite order tools to "Needs approval" in
Claude, never "Always allow", so Claude's own "Allow once / Deny" card is a
second check. No options, futures, margin (MIS leverage) or short selling
unless you turn them on.

**Paper first.** `"paper": true` is the default in `config.example.json`.
Zerodha has no paper account, so paper mode is simulated locally by
`/trader-broker` and never calls Kite. Your Kite Connect keys live in
environment variables, never in the chat.

**Charges are real money.** Every intraday round trip pays brokerage, STT,
exchange and SEBI fees, stamp duty and GST. On a small position they can be a
real slice of your risk, so the max loss, reward to risk and the Time
Machine's R are all counted after charges. The rates are Zerodha's published
ones; your contract note is the truth, and `"charges"` in your config
overrides them.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 60 days of 5-minute bars. The future can do things those 60
days never did: results, a circuit, a headline. Treat its odds as a reality
check, never as a promise.

**A stop is a plan, not a guarantee.** In a fast market, across a gap or into
a circuit limit, a stop-loss order can fill well past its trigger or not at
all. The max loss shown is the loss at the stop price.

**The data is free and unofficial.** Yahoo Finance's chart endpoint is not an
official API: it can lag, change or rate-limit you. The scanner and the entry
timer read 1-minute or 5-minute bars, not every tick. Check anything that
matters against your broker before you act on it.

**Holidays.** The NSE holiday list for 2026 is built in. NSE publishes the
next year's list in December; until it is added, the tools say the calendar
is unknown rather than guess. Muhurat trading times are added when NSE
announces them.

**Insider trades are public, not inside information.** Promoters, directors
and key staff must disclose their trades within two trading days, and the
company must tell the exchange within two more. `/trader-insider` reads those
public filings as soon as they post.

**Taxes.** Intraday equity profit is speculative business income in India,
taxed at your slab rate, not as capital gains. Nothing here is tax advice.

## Files

```
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py, claude_desktop_config.example.json
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, facts.py, metrics.example.json
trader-insider/     SKILL.md, insider.py
tests/              unittest suite + offline fixtures (tests/_nse.py is the shared NSE block)
```

MIT. Original US pack by [Jake Schincariol](https://opusjake.ai); India port by Madhur Gupta.
