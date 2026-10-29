---
name: trader-news
description: >-
  News Monitor. Tracks breaking news on every NSE stock you watch and flags
  what moves it: Yahoo Finance headlines per symbol, each company's NSE
  corporate announcements (results, board meetings, acquisitions, SAST,
  pledges, credit ratings, dividends, analyst meets, director changes) and how
  far each stock is from the previous close. Shows only what is new since the
  last check, newest first, in India time (IST), tags each item and never
  repeats one. Use when the user says "any news on RELIANCE", "why is TCS
  moving", "what's the news on my watchlist", "check NSE announcements", "did
  Infosys file anything", "morning briefing", "pre-open news", "monitor the
  news", "did anything break", or after /trader-scanner finds setups.
argument-hint: "[NSE symbols]"
---

# trader-news

A chart can look perfect and still be about to gap on a results day, a
pledge invocation or a SEBI order. Before a setup becomes a trade, this skill
reads the two feeds that carry those first, for every stock on the
watchlist, and tells you only what is new. It also prints how far each stock
has moved today, so a headline and a move sit side by side.

Tool in this folder (standard library Python, no keys, no login):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                          # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS M&M --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-nse --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner); NSE symbols like `RELIANCE`, `M&M`, `BAJAJ-AUTO` |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| Yahoo Finance headline RSS | `https://feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN` |
| NSE corporate announcements | `https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml`, one request for all companies; each item links the filing's PDF |
| NSE equity list | `https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv`, to match the company name in each announcement to your symbol (fetched only for a symbol not cached yet) |
| Yahoo Finance chart endpoint | the last price against the previous close (unofficial, can lag) |
| `trader/state/news.json` | what has been shown (kept 30 days) and each symbol's company name |

Only plain downloads of public files. It never calls the
`www.nseindia.com/api` JSON endpoints: those need a browser-cookie handshake
and the tool does not work around bot checks.

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an NSE announcement and a headline
  disagree, the announcement is what the company told the exchange. Open the
  PDF.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags. Headlines: earnings, analyst, legal, deal, capital, people, risk,
  halt (suspension, upper or lower circuit), regulatory (SEBI, RBI, CCI,
  USFDA), macro (RBI policy, Nifty, FII flows, rupee, crude). NSE filings
  carry their subject's tag: results, board outcome, board meeting, deal,
  SAST, pledge, buyback, credit rating, legal, corporate action, orders,
  capital, people, analyst meet, insider, press release, shareholder meeting,
  update. Filings marked `!` matter most: results, the outcome of a board
  meeting, an acquisition or restructuring, SAST (substantial acquisition),
  a pledge, a buyback, a credit rating, a director or KMP resignation, and
  action taken by a regulator or court.
- **Routine filings stay out of the way.** Newspaper copies, trading window
  closures, lost share certificates, NAVs and debt-servicing confirmations
  are counted ("3 routine NSE filings hidden") and shown with `--routine`.
- **MOVING** marks a stock 3 % or more from the previous close
  (`--move-pct`). A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is
  from the previous close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing (open the PDF), anything
   tagged results, earnings, halt, legal, regulatory or risk, and every
   MOVING symbol first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "the board
   meets on results this week: the stock can gap at 9:15, past any stop").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: a results date inside the trade, a suspension, a pledge,
   a fund raise. Never act on a headline alone. For "when does it report",
   `/trader-analyst` reads NSE's board-meetings feed.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before the 9:00 pre-open.

## Example

Thursday 2026-10-29, checked at 13:40 IST for RELIANCE and INFY:

```
RELIANCE   Rs 1,174.30 +1.41% vs previous close Rs 1,158.00 at 13:35 IST
! 2026-10-29 08:55 IST  RELIANCE    NSE           Reliance Industries Limited: Other Restructuring
      tags: deal  Disclosure under Regulation 30 ... Voluntary strike off of Hamleys (Franchising) Limited
```

The brief: "Reliance filed a small restructuring before the open (a
subsidiary's voluntary strike off); nothing on results. Yahoo had no
Reliance headlines (its India feed is often empty for some symbols, the tool
says so). Infosys: the last stories are about the Q2 results on Oct 23 and
the guidance cut." When a symbol has no news, say so rather than padding the
brief.

Yahoo's feed for a symbol with `&` (M&M) can mix in stories about other
companies; the NSE announcements are matched by company name and are not
affected.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per symbol, half a second
  between calls, and the NSE announcements feed once per check.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
