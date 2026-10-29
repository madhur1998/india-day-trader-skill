---
name: trader-analyst
description: >-
  The Earnings Analyst. Reads an NSE company's quarterly results (Yahoo
  Finance fundamentals, consolidated, in rupees crore) and sums up what
  changed: revenue, gross, operating and net margins, net income and diluted
  EPS, quarter by quarter on India's April-March fiscal year (Q2 FY27 =
  Jul-Sep 2026), each against the same quarter a year earlier, with the
  trailing twelve months. Finds the next results date in NSE's board-meeting
  intimations and links the results filed on NSE. Reads a results press
  release or con-call transcript when you paste one. Use when the user says
  "how were Infosys results", "Q2 FY27 numbers", "analyze RELIANCE's
  results", "what changed this quarter", "is revenue growing", "compare
  margins", "sum up the con call", "when does HDFC Bank report", "does it
  report this week", or before trading a stock around its results.
argument-hint: "[NSE symbols]"
---

# trader-analyst

Results move Indian stocks more than anything else on the calendar, and the
press release is written to put the best number first. This skill lines up a
company's numbers quarter by quarter, says plainly what changed, and finds
when the next results are due. For a day trader it answers two questions
before a trade: what did the last results say, and is the next board meeting
inside this trade?

Tool in this folder (standard library Python, no keys, no login):

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" --help
```

## What it reads and writes

- Reads Yahoo Finance's fundamentals timeseries for `<SYMBOL>.NS`
  (`https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/RELIANCE.NS?type=quarterlyTotalRevenue,...`):
  quarterly revenue, gross profit, operating income, net income and diluted
  EPS, and the same for full fiscal years. Consolidated, in rupees.
- Reads NSE's board-meeting intimations
  (`https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml`, items
  like "Financial Results |Meeting Date: 05-Nov-2026") for the next results
  date, and NSE's announcements
  (`https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml`)
  for the latest results filing and its PDF. The company is found by its name
  from NSE's equity list (`EQUITY_L.csv`). Plain downloads only, never the
  `www.nseindia.com/api` endpoints.
- Writes `trader/metrics.json` with `--metrics`: one row per company (TTM
  revenue, net income and diluted EPS, the latest margins and growth, any
  missing quarters), for your notes or a spreadsheet. `metrics.example.json`
  in this folder is a real run for RELIANCE, INFY, SBIN, M&M and MATRIMONY on
  2026-10-29.
- Results press releases, investor presentations and con-call transcripts:
  the tool does not fetch them. Open the PDF linked from the NSE filing, or
  read what the user pastes, and sum up what management said changed:
  guidance, margins, demand, order book, anything they declined to answer.

## The rules

- **Yahoo's numbers are unofficial.** The source of truth is the results the
  company filed on NSE (the outcome of the board meeting, with the results
  PDF). The table is a fast first read; when a number matters, or the tool
  prints a NOTE, open the NSE filing and quote it. Say which numbers came from
  where.
- **Compare like with like.** Growth is against the same quarter a year
  earlier (Q2 FY27 against Q2 FY26). Quarter-on-quarter changes are shown,
  but many businesses are seasonal (festive demand lands in Oct-Dec).
- **Indian fiscal year.** Q1 = Apr-Jun, Q2 = Jul-Sep, Q3 = Oct-Dec,
  Q4 = Jan-Mar; FY27 runs April 2026 to March 2027.
- **Say when a quarter is missing.** Yahoo sometimes skips a quarter. The
  table shows it as "missing from Yahoo's data", there is no trailing twelve
  months unless four quarters in a row exist, and nothing is interpolated or
  estimated. Read that quarter from the NSE filing if it matters.
- **Say when the data lags.** Listed companies file results within 45 days of
  a quarter's end (60 for Jan-Mar). When Yahoo's newest quarter is older than
  that, or a results filing on NSE is newer than Yahoo's last quarter, the
  tool prints a NOTE with the link: read the filing before summarising.
- **Amounts in crore.** 1 crore = 1,00,00,000 rupees; the tool prints
  "Rs 3,09,468 cr" with Indian digit grouping (₹ in prose is fine). Growth
  from a loss is not a percentage: the tool says "a loss of Rs 81.22 cr a year
  earlier" instead.
- **Flag one-offs.** When net income is above operating income, other income
  or exceptional items are inside it. The tool says so. Banks, NBFCs and
  insurers have no gross or operating margin in Yahoo's data: blank, not zero.
- **Another currency.** A few companies appear in Yahoo in US dollars from
  their overseas filings (Infosys). The tool prints those in USD millions
  with a note; the rupee results are on NSE.

## Steps

1. Run it for the companies in question (or the symbols on
   `trader/watchlist.json`):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE
   python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE TCS M&M --metrics trader/metrics.json
   ```

   `--quarters 8` shows two years, `--json` everything machine-readable,
   `--no-nse` skips the results date and filing links, `--today 2026-11-02`
   pins "this week".
2. Open the latest results filing on NSE (the PDF link) when the numbers
   need context or the tool printed a NOTE, and read it.
3. Write the summary: one line on the quarter (revenue, net profit, EPS,
   growth against the same quarter a year earlier), one on margins, one on
   anything unusual (a one-off, a missing quarter, data that lags), the next
   results date, and the links. Plain words, real numbers, no adjectives the
   numbers do not support.
4. If the next board meeting on results falls inside the planned trade (an
   overnight hold, or results announced before 9:15), say it before
   `/trader-exit` sets the stop: a stop does not protect against a gap, the
   stock can open past it. Many companies announce during market hours; the
   stock can move sharply within minutes.
5. When the next results date is not in NSE's feed (it only holds the
   latest intimations, hours to a few days), say so and point to the
   company's announcements page. Never guess a date.

## Example

`facts.py RELIANCE` (2026-10-29): Q1 FY27 (Apr-Jun 2026) revenue
Rs 3,09,468 cr, +27.0% against a year earlier, net income Rs 20,946 cr
(-22.4%), diluted EPS Rs 15.48 (-22.4%); gross margin 26.8%, operating margin
10.5%. Q2 FY26 (Jul-Sep 2025) is missing from Yahoo's data, so there is no
trailing twelve months; FY26 revenue Rs 10,57,219 cr (+9.6%). No upcoming
board meeting in NSE's feed that day.

`facts.py MATRIMONY` the same day: Q1 FY27 revenue Rs 131 cr (+13.2%), last
four quarters Rs 475 cr and EPS Rs 21.27, and "Next results date: Tue
2026-11-10, financial results (in 12 days), per NSE board-meeting
intimation". For ESAF Small Finance Bank the tool noted that Yahoo stopped at
Q1 FY27 while the Q2 results were filed on NSE at 13:18 that afternoon, so
the summary read that filing directly.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company published.
- Never invents a number, fills a missing quarter or guesses a results date.
- Never hammers a server: one request per company to Yahoo, the NSE feeds
  once per run, half a second between requests.

## Safety

Education and tooling, not financial advice. This skill only reads published
numbers and filings. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
