---
name: trader-insider
description: >-
  The Insider Tracker. Watches what India's company insiders and big traders
  disclose: SEBI insider trading (PIT Regulation 7(2)) filings on NSE, where
  promoters, directors, KMPs and designated persons report their buys,
  sells, pledges, ESOPs and gifts, plus NSE's daily bulk and block deals.
  Alerts once per new filing or deal and turns each XBRL filing into clean
  rows (who, role, buy or sell, shares, rupee value, mode, dates, holding
  before and after, how many trading days it took to disclose), rates each
  row's signal (an open-market promoter or director buy is the one that
  matters), pairs block-deal buyers with sellers and flags stocks you hold.
  Use when the user says "what are promoters buying", "insider trades in
  RELIANCE", "did any director buy", "bulk deals today", "block deals",
  "who bought in the block deal", "track insider trading", "promoter
  pledge", "SAST" or "run the insider skill".
argument-hint: "[symbol] [--signal strong]"
---

# trader-insider

Indian company insiders have to report their own trades. Under SEBI's
Prohibition of Insider Trading (PIT) Regulations, **Regulation 7(2)**, a
promoter, member of the promoter group, director, key managerial person
(KMP) or designated employee who trades more than Rs 10 lakh of the company's
shares in a calendar quarter must tell the company **within 2 trading days**,
and the company must tell the exchange **within 2 trading days** of hearing
about it. NSE publishes each filing as an XBRL file and lists them in an RSS
feed. Separately, NSE posts the day's **bulk deals** (one client trading
more than 0.5 % of a company's listed shares in a day, on the normal market)
and **block deals** (large negotiated trades in the block-deal window, where
a buyer and a seller agree a big lot at one price) as CSV files every
evening.

This skill reads those files, notices what is new and reads it for you.

It is fast. It is not early. A filing reaches NSE up to four trading days
after the trade, and deals are published after the close. There is no
inside information here: only public filings, read quickly. Say that every
time.

Tool in this folder (standard library Python 3.8, no installs):

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" --help
```

## What it reads and writes

Only plain GETs to NSE's archive host, one file at a time with a pause
between requests. Never the `www.nseindia.com/api/...` JSON endpoints: they
need a browser-cookie handshake and this pack does not work around bot checks.

- `https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml`: the
  feed. Each item links to one disclosure such as
  `https://nsearchives.nseindia.com/corporate/xbrl/IT_1197873_1662973_02052026044642_WEB.xml`.
- `https://nsearchives.nseindia.com/content/equities/bulk.csv` and `block.csv`:
  the latest trading day's deals (Date, Symbol, Security Name, Client Name,
  Buy/Sell, Quantity Traded, Trade Price / Wght. Avg. Price).
- Writes `trader/state/insider.json` (the filings and deals already shown,
  so each one alerts once).
- Reads `trader/portfolio.json`, `trader/watchlist.json` and
  `trader/plan.json` (whichever exist) to flag your symbols.

## The rules

- **Quote the filing.** Every insider row comes from the company's XBRL
  filing, with its file id and link. If a news site says something else,
  the filing wins.
- **Say the signal, and why.** Each insider row is rated:
  - `strong`: an open-market buy (mode "Market Purchase") by a promoter,
    promoter group, director or KMP. Their own money, at the market price.
  - `medium`: an open-market buy by anyone else who has to file, or an
    open-market sale by a promoter, director or KMP (people sell for school
    fees, taxes and houses; a sale is weaker evidence than a buy).
  - `low`: ESOP exercises, gifts, off-market and inter-se transfers,
    pledges and their release or invocation, allotments, transmission.
    Paperwork, not a view on the price. A promoter **pledge** is still worth
    a sentence: it means borrowing against the shares.
- **Say the delay.** Each row shows the trade dates, when the company was
  told (with the count of trading days, skipping weekends and NSE holidays;
  over 2 is flagged LATE) and how many days after the trade it became public.
- **Bulk deals are not stake-building by default.** Many bulk deals are
  trading desks buying and selling the same stock the same day; the tool
  marks a client on both sides as "trading around it" and lists those last.
- **Block deals come in pairs.** A block buy and a block sell with the same
  stock and quantity are one trade: the tool shows "seller sold N to buyer
  at price = value" (a cross).
- **Rupees, Indian style.** Quantities as 2,41,53,106; values in crore
  (Rs 87.34 cr), under a crore in rupees.

## Step 1: what is new

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" check
python3 "${CLAUDE_SKILL_DIR}/insider.py" check --only insider --signal medium
```

Shows only filings and deals not seen at the last check, then records them.
Exit code 1 means nothing new. A filing that could not be read is shown
with its link and tried again next time. NSE adds filings through the
trading day and replaces the deal files each evening, so once an hour in
market hours plus once after 18:00 IST is plenty: run it with `/loop 1h` or
a scheduled task.

## Step 2: look something up

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" list --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/insider.py" list --only insider --signal strong    # did any director buy?
python3 "${CLAUDE_SKILL_DIR}/insider.py" list --only bulk,block --min-value 100000000   # Rs 10 crore+
python3 "${CLAUDE_SKILL_DIR}/insider.py" list --person "graviton"
python3 "${CLAUDE_SKILL_DIR}/insider.py" list --holdings                         # only your symbols
python3 "${CLAUDE_SKILL_DIR}/insider.py" filing IT_1197873_1662973_02052026044642_WEB.xml
```

`--symbol` repeats; `--person` matches the insider's name or the deal
client; `--min-value` is in rupees; `--signal` keeps insider rows at that
level or stronger (and drops the deals); `--watch SYM` flags an extra
symbol; `--json` for the raw rows; `--offline DIR` reads saved copies.

## The feed can be stale or short

NSE's insider RSS holds only the last 20 or so items and is not always
fresh: the copy read on 2026-10-29 was rebuilt that afternoon but its newest
item was from 2026-05-02. The tool says so ("The insider feed looks stale")
whenever the newest item is over 7 days old, and says when the feed is
empty. Then tell the user plainly, and point them to NSE's insider trading
page (Companies > Corporate filings > Insider trading) in their own browser
for the full list. Do not try to call NSE's JSON API instead.

## SAST and other disclosures

SAST (Substantial Acquisition of Shares and Takeovers) Regulation 29
disclosures, for crossing 5 % and each 2 % change after that, and promoter
pledge (encumbrance) disclosures under Regulation 31 are separate filings
that this tool does not read. They show up as corporate announcements:
`/trader-news` tags SAST and pledge announcements from NSE's announcements
feed. Say which source each fact came from.

## Example

```
NEW SAMPLEIND Sample Industries Limited, filed 2026-10-28 18:05 (IT_9000001_...)
  - Promoter Group Asha Sample Family Trust: buy 2,50,000 Equity Shares (Rs 3.12 cr
    at about Rs 125.00), Market Purchase on NSE, traded 2026-10-22 to 2026-10-23,
    told the company 2026-10-27 (2 trading days later), public 5 days after the
    trade; holding 10,00,000 (1.2 %) -> 12,50,000 (1.5 %)
      [strong: open-market buy by Promoter Group]
Block deals (large negotiated trades in the block-deal window), 2026-10-28:
  - STLTECH Sterlite Technologies Ltd: SERUM INSTITUTE OF INDIA PRIVATE LIMITED sold
    6,01,900 to POONAWALLA AVIATION PRIVATE LIMITED at Rs 887.00 = Rs 53.39 cr
```

The insider part is the test fixture (a made-up company in the real filing
shape); the block deal is real, from 2026-10-28. Then: "A promoter-group
trust bought on the open market last week and it became public today. It
says what they did, not what the price will do."

If the user wants to act on it, it goes the long way: a filing is days old,
so it is a reason to look at a chart, not a setup. `/trader-scanner` and
`/trader-entry` decide if there is a trade today; then sized by
`/trader-budget`, checked by `/trader-risk`, placed by `/trader-broker` only
after their yes.

## What it never does

- Never says or implies an insider or a deal counterparty knows something,
  or that copying them beats the market. No return claims.
- Never calls an ESOP, gift, pledge or transfer a "buy signal".
- Never calls `www.nseindia.com/api`, never works around a bot check, never
  logs in anywhere.
- Never places an order.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a
  recommendation to buy or sell anything.
- Paper trading by default (`"paper": true` in `trader/config.json`).
- Nothing places an order except `/trader-broker`, and only after
  `/trader-risk` passes AND the user types an explicit yes for that order in
  the chat, AND the broker's order tool is set to "Needs approval" in Claude
  (Customize > Connectors > tool permissions) so Claude's own "Allow once /
  Deny" card appears too. Never "Always allow" an order tool.
- No options, margin, shorting or leverage unless the config allows it and
  the user confirms.
- Insider filings and deals are PUBLIC data, published days or hours after
  the trade. This skill reads them fast; it has no inside information, and
  it says so.
