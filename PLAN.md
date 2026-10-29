# Converting the pack to India (NSE)

Working spec for turning the US day-trading pack into **india-day-trader-skill**.
Delete this file when the conversion is done (and drop it from
`tests/test_repo.py`'s root list).

## Decisions

- **India only.** No US mode, no `market` switch. NSE only (no BSE).
- **Broker: Zerodha Kite**, through the official Kite MCP server
  (`zerodha/kite-mcp-server`). Paper trading stays the default; Kite has no
  paper account, so paper mode is a local simulator in `trader-broker`.
- **trader-pelosi becomes trader-insider**: SEBI PIT insider disclosures +
  NSE bulk and block deals.
- **Name:** `india-day-trader-skill` .
- **Data access rule:** only plain GETs to public files: Yahoo chart /
  spark / RSS / fundamentals-timeseries, and NSE's archive host
  `nsearchives.nseindia.com`. **Never** the `www.nseindia.com/api/...` JSON
  API (it needs a browser-cookie handshake; we do not work around bot checks).
- Long side only, as before. Intraday shorts are legal in India but stay out
  of scope (`allow_short: false`).

## The shared block

`tests/_nse.py` is the one source for the clock, the calendar, tick size,
charges, rupee formatting and Yahoo symbols. Each tool pastes **verbatim**
the parts it uses; `tests/test_nse.py` fails if a copy drifts, if a tool
still has `eastern_offset`/`NYSE`/`New York`/`Alpaca`/`$<digit>`, or if a
clocked tool lacks `to_ist`. `parse_now` may differ per tool only in the
error class it raises.

| | US (old) | India (new) |
|---|---|---|
| Session | 9:30 to 16:00 ET, DST | **9:15 to 15:30 IST**, UTC+5:30 fixed. Pre-open 9:00 to 9:15. |
| Calendar | NYSE 2026 + 2027, early closes | `NSE_HOLIDAYS` 2026; `CALENDAR_YEARS = (2026,)`. No early closes. `NSE_MUHURAT` special session (times may be None = closed, say why). |
| 5-minute bars a session | 78 | **75** (9:15 ... 15:25) |
| Names | `to_eastern`, `eastern_to_utc`, `ts_to_eastern`, "New York", "ET" | `to_ist`, `ist_to_utc`, `ts_to_ist`, "India time", "IST" |
| Output keys | `now_new_york` etc. | `now_ist` etc. |
| Prices | `$`, cents | rupees; print with `inr()` ("Rs 1,23,456.00", ASCII so Windows consoles cope). SKILL.md prose may use the rupee sign. |
| Rounding | cents | `round_tick(price, "down"/"up"/"nearest")`: stop down, target up |
| Costs | none | `charges(qty, buy, sell, rates)` round trip; config `"charges": {...}` overrides `CHARGES` |
| Symbols | `TSLA`, `BRK.B` | NSE symbols `RELIANCE`, `M&M`, `BAJAJ-AUTO`; `yahoo_symbol()` for URLs (`.NS`, `&` encoded) |
| Benchmark | SPY | `^NSEI` (Nifty 50) |

## Config (`trader/config.json`)

Every `*_usd` key becomes `*_inr`. A config that still has a `*_usd` key is a
plain error naming the new key (no silent fallback: a rupee value under a
dollar name is a bug waiting to happen).

```json
{
  "budget": 100000,
  "cash_buffer_pct": 10,
  "max_position_pct": 75,
  "max_order_inr": 80000,
  "min_order_inr": 500,
  "max_loss_per_trade_inr": 500,
  "max_daily_loss_inr": 1500,
  "max_trades_per_day": 3,
  "max_orders_per_day": 10,
  "min_reward_risk": 1.0,
  "require_stop": true,
  "no_entries_before": "09:30",
  "no_entries_after": "14:45",
  "flat_by": "15:10",
  "product": "MIS",
  "mis_leverage": 1,
  "count_charges": true,
  "charges": {},
  "broker": "zerodha",
  "paper": true,
  "blocked": [],
  "allow_options": false,
  "allow_margin": false,
  "allow_short": false
}
```

- Removed: `fractional`, `pdt_guard` (no pattern-day-trader rule in India),
  `sec_user_agent`.
- `flat_by` 15:10 sits before Zerodha's MIS auto square-off (about 15:20,
  and it costs a fee). `no_entries_before` 09:30 = after a 15-minute range.
- `mis_leverage` only counts when `allow_margin` is true (then the cash limit
  is `cash x mis_leverage`). Default 1: no leverage.
- `count_charges`: max loss at the stop = qty x (entry - stop) **+ round-trip
  charges at the stop**; reward:risk uses net reward / net risk.

## Orders (`trader/orders.json`), Kite shape

Kite has no bracket orders for equity. A day trade is an entry, then a
protective SL-M stop on the exchange once the entry fills, and a target that
trader-alerts watches (two live sell orders for one position could both fill
and leave you short).

```json
{
  "created_by": "trader-budget",
  "created_at": "2026-10-28T04:45:00Z",
  "status": "proposed",
  "paper": true,
  "note": "Proposals only. ...",
  "source": "trader/plan.json (RELIANCE orb 2026-10-28)",
  "orders": [
    {
      "symbol": "RELIANCE",
      "side": "buy",
      "qty": 40,
      "kite": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
               "transaction_type": "BUY", "quantity": 40, "product": "MIS",
               "order_type": "LIMIT", "price": 1169.4, "validity": "DAY", "tag": "tds3f9a1c2b"},
      "stop_leg": {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE",
                   "transaction_type": "SELL", "quantity": 40, "product": "MIS",
                   "order_type": "SL-M", "trigger_price": 1160.0, "validity": "DAY", "tag": "tds3f9a1c2bsl"},
      "target": 1188.2,
      "why": "...",
      "plan": {"entry": 1169.4, "stop": 1160.0, "target": 1188.2, "risk_per_share": 9.4,
               "charges_at_stop": 61.2, "max_loss": 437.2, "setup": "orb",
               "entry_time": "9:45 AM", "flat_by": "15:10"}
    }
  ]
}
```

- `kite` is exactly the argument object for the Kite MCP `place_order` tool:
  numbers are numbers (Alpaca wanted strings), `tag` at most 20 chars,
  alphanumeric. Default `order_type` **LIMIT** at the plan's entry (a market
  order through the API needs market protection; keep it opt-in with
  `--market`).
- Exits (`side: sell`) have only `kite` (no `stop_leg`).
- The fingerprint trader-risk records covers `kite` + `stop_leg`.
- One "yes" approves the entry **and its stop leg** (the stop only reduces
  risk); the ticket shows both. Every other rule of the yes stays as is.

## Paper mode (`"paper": true`, the default)

trader-broker never calls Kite. `ticket.py paper-fill` fills the approved
order at a fresh price from `trader/prices.json` (under 15 minutes old;
refuse otherwise), writes the fill to `trader/portfolio.json`, records the
stop leg as a resting paper stop, and `ticket.py paper-check` triggers paper
stops/targets from fresh prices. Live mode (`"paper": false`) shows the exact
`place_order` JSON for Claude's Allow once / Deny card, as Alpaca did.

## Per skill

| Skill | What changes |
|---|---|
| scanner | Universe = `EQUITY_L.csv`, series `EQ` only (BE/BZ are trade-to-trade: no intraday). Presets `--index nifty50/nifty500` from `ind_nifty*list.csv`. Drop ADR/SPAC/warrant rules. Circuit band from `sec_list.csv` (skip stocks with a 2 % or 5 % band by default, `--bands`). Floors in rupees: `--min-price 50`, `--min-turnover` (price x volume) Rs 10 crore. ORB from 9:15. Yahoo symbols `.NS`. |
| news | Yahoo RSS `region=IN&lang=en-IN`. SEC 8-K -> NSE `RSS/Online_announcements.xml` (filter by company name -> symbol via `EQUITY_L.csv`; the "SUBJECT:" part drives the tag: results, board meeting, acquisition, SAST, pledge, credit rating, dividend, investor meet...). Drop `sec_user_agent`. |
| entry | IST clock, 9:15 range, `.NS`. Warn when the stock's band from `sec_list.csv` (if given) is near. |
| exit | Tick rounding (`round_tick`), `charges` in R and in the spread check, flat by 15:10. |
| timemachine | 75 bars, 9:15 to 15:30, R net of charges, `^NSEI` context. |
| risk | Drop PDT. Add: series must be EQ, circuit band check (optional file), MIS cutoff (no entry after `no_entries_after`, exits always allowed), leverage via `mis_leverage`, max loss includes charges. Kite order format validation. |
| budget | Rupees, `*_inr`, whole shares, charges in the max-loss limit, `mis_leverage`, Kite order shape above. |
| broker | Kite MCP tool names (`place_order`, `modify_order`, `cancel_order`, `get_orders`, `get_positions`, `get_margins`, `get_ltp`), the paper simulator, `flatten` builds a MIS SELL MARKET/LIMIT. `claude_desktop_config.example.json` for Kite MCP (self-hosted with your own Kite Connect keys for trading; the hosted `mcp.kite.trade` leaves out order placement). |
| alerts | IST clock, opening bell 9:15, optional pre-open 9:00, flat-by 15:10, `.NS` quotes; Stooq dropped (no NSE coverage). |
| analyst | Yahoo `fundamentals-timeseries` (consolidated, INR): revenue, gross profit, operating income, net income, diluted EPS, quarter vs the same quarter a year earlier, TTM. Indian fiscal year (Q1 = Apr-Jun; FY27 = Apr 2026 to Mar 2027). Say when a quarter is missing. Results dates from `RSS/Board_Meetings.xml` ("Financial Results \| Meeting Date: ..."). Crore formatting (1 crore = 1e7). Pasted results press release / con-call transcript as before. |
| insider | `RSS/Insider_Trading.xml` -> each `IT_*.xml` XBRL (SEBI PIT Reg 7(2)): person, category (promoter, director, KMP...), buy/sell, shares, value, mode (market, ESOP, gift...), dates, days to disclose. Plus `equities/bulk.csv` and `equities/block.csv` (daily). Alert once per new item, flag your tickers, state in `trader/state/insider.json`. Say every time: public filings, read quickly, not early. |

## Fixtures

`tests/fixtures/_nse/` holds real data recorded 2026-10-29 (IST afternoon,
so the last full session is **2026-10-28**; 2026-10-29 is partial):
5-minute bars (60 days), 1-minute (5 days), daily (6 months) for RELIANCE,
HDFCBANK, INFY, SBIN, M&M and ^NSEI; Yahoo RSS and fundamentals per stock;
`EQUITY_L.csv`, `sec_list.csv`, Nifty 50 / 500 lists, bulk and block deals,
NSE announcement / insider / board-meeting / results RSS, three insider XBRL
files. Tests pin `--now` to IST times. US fixtures are deleted when the skill
that used them is converted. A skill that needs more (the scanner's spark
batches) records it into its own fixture folder, politely (half a second
between requests).

## Status (2026-10-29)

All four steps are done on branch `india-conversion` (not committed): 382
tests pass offline, and the whole chain (entry, exit, timemachine, budget,
risk, broker, alerts) was run end to end on the RELIANCE 2026-10-28 fixtures.

Learned along the way (now in the code):
- nsearchives.nseindia.com drops requests whose User-Agent carries a URL:
  one shared `UA` without one (`tests/_nse.py`, enforced by test_nse).
- Yahoo's NSE 5-minute data leaves the 15:20 bar empty and usually the 15:25
  bar too: the Time Machine replays to 15:20; the scanner tolerates up to 3
  missing bars after the range. Yahoo's 9:15 bar reports zero volume.
- Raw Yahoo floats sit a hair off the paisa: `round_tick` snaps to the paisa
  first (it used to lose or gain a tick).
- Yahoo reports a few companies (e.g. INFY) in USD; the analyst says so.

Check by hand before trading live:
- Kite MCP `place_order` parameter names, whether the hosted server can place
  orders, the self-hosted env var names, and whether API MARKET orders need
  a market-protection field (orders default to LIMIT meanwhile).
- The tick-size table and the charge rates against the current NSE circular
  and Zerodha's charges page.
- NSE's 2027 holiday list (due in December) and the 2026 Muhurat times.
- The circuit-band warning in trader-entry uses Yahoo's last bar as the
  previous close; NSE's official close is a 30-minute weighted average.
- Paper fills do not deduct charges from cash yet.

Possible follow-ups: move the company-name matcher (news, analyst) and the
Kite fingerprint (risk, broker) into the shared block; a shared
`charges_per_share()` so exit, budget and timemachine compute net R alike.
