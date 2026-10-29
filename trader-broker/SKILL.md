---
name: trader-broker
description: >-
  Order Executor for Zerodha Kite on NSE. Paper trading by default, simulated
  right here from fresh prices; never an order without your yes. Shows each
  order as a plain ticket (the entry, its SL-M stop leg, the target and the
  max loss at the stop in rupees with charges) plus the exact JSON for the
  Kite MCP place_order tool, records your yes word for word (one yes covers
  the entry and its stop leg), then in live mode Claude's own Allow once /
  Deny card asks again. Fills paper orders and triggers paper stops and
  targets, records Kite order ids, builds the MIS square-off that closes a
  day trade by your flat-by time and says how to cancel the resting stop.
  Covers setting up the official Kite MCP server (zerodha/kite-mcp-server) in
  Claude Desktop: the hosted read-only server, self-hosting with your own
  Kite Connect keys in env vars (never in chat) to place orders, and setting
  order tools to "Needs approval". Use when the user says "place the order
  on Kite", "buy it", "execute", "send it to Zerodha", "square off", "close
  my position", "flatten", "get me out", "connect Zerodha", "set up Kite
  MCP", "paper trade it", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the Kite order tools are set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order. In paper
mode (the default) nothing goes to Kite at all: gates 1 and 2 still apply,
and `ticket.py paper-fill` stands in for the broker.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper-fill --index 1     # paper: fill at a fresh price
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper-check              # paper: did the stop or the target hit?
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --order-id 251028000123456
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --leg stop --status placed --order-id 251028000123460
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol RELIANCE --side sell --qty 46 --price 1160.00
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to Zerodha. It renders tickets, checks that a reply
is a real yes for that order, simulates paper fills, builds the flatten
ticket, and keeps the log in `trader/state/broker.json`. `--now "2026-10-28
09:45"` pins the clock (India time).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders in Kite shape (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, stop leg, failed and filled, each entry's max loss, and the paper stops |
| `trader/portfolio.json` | cash (rupees) and shares, updated by fills |
| `trader/prices.json` | fresh prices (under 15 minutes old) for paper fills, paper stops and the flatten LIMIT |
| `trader/plan.json` | the flat-by time for `flatten` |

## The order: an entry and its stop leg

Kite has no bracket orders for equity. Each day-trade order has:

- `kite`: the entry, exactly the arguments for `place_order` (a LIMIT buy,
  MIS, DAY, with a tag).
- `stop_leg`: the protective stop, its own `place_order` arguments (an SL-M
  sell, same symbol, product and quantity). It goes to the exchange once the
  entry fills.
- `target`: watched by trader-alerts (`alerts.py add --from-plan`), not a
  second sell at Kite: two live sells for one position could both fill and
  leave the user short.

**One yes approves the entry and its stop leg.** The stop only reduces risk,
and the ticket shows both JSON objects before the user answers. Every other
rule of the yes holds: one order per question, never "yes to all", a reply
that names another stock or a different number is not a yes.

## Paper mode (`"paper": true`, the default)

Kite has no paper account, so paper trading is simulated here.

1. `ticket.py show`, the user's yes, `ticket.py approve` (as below).
2. `ticket.py paper-fill --index N` fills the approved order at the price in
   `trader/prices.json` if it is under 15 minutes old (refresh it with
   trader-alerts: `alerts.py quote RELIANCE --write`). A LIMIT buy fills only
   at or under its limit; otherwise it says "Not filled" and the yes stands
   for today. Only inside the NSE session (9:15 to 15:30 IST on a trading
   day). The fill goes to `trader/portfolio.json`; the stop leg becomes a
   resting paper stop.
3. `ticket.py paper-check` (run it whenever prices are refreshed, or on a
   `/loop`): a paper stop fills when the price is at or below its trigger, at
   the trigger or worse if the price is already below it (a gap). The target
   fills when the price is at or above it. It prints the P&L before and after
   estimated charges, and says when it is past `flat_by`.
4. A paper-filled sell (the flatten) cancels the resting paper stop.

Paper fills are optimistic about liquidity and slippage. They are for
practising the routine, not for proving a strategy.

## Setup: the Kite MCP server (live mode)

Zerodha's official server is github.com/zerodha/kite-mcp-server. Check its
README for current details before setting it up.

**Hosted, read-mostly.** Zerodha hosts it at `https://mcp.kite.trade/mcp`.
It reads holdings, positions, orders, margins and quotes, and leaves out
order placement. For Claude Desktop (Settings > Developer > Edit Config,
`claude_desktop_config.json`; needs Node.js for `npx`):

```json
{
  "mcpServers": {
    "kite": {"command": "npx", "args": ["mcp-remote", "https://mcp.kite.trade/mcp"]}
  }
}
```

Restart Claude Desktop. The first call asks the user to log in to Kite in
their browser through a link; the login lasts for the day. No keys go in
the file or the chat. This is enough for `get_positions`, `get_orders`,
`get_margins`, `get_ltp` and the paper flow.

**Self-hosted, to place orders.** The user creates their own Kite Connect
app at developers.kite.trade, runs kite-mcp-server on their own computer as
its README says, and gives the server the app's API key and secret as
environment variables in the config file (`claude_desktop_config.example.json`
here has the shape, under `_self_hosted_alternative`, with placeholder names
`KITE_API_KEY` / `KITE_API_SECRET`: check the real names against the README).

- **Keys never go in the chat.** Never ask for them, never read the config
  file back into the conversation, and if the user pastes a key or secret
  anyway, tell them to delete that message and regenerate the secret in the
  Kite Connect developer console.
- **Set the tool permissions.** Customize > Connectors (Settings > Connectors
  in older versions), open kite, and for each tool:
  - `place_order`, `modify_order`, `cancel_order`, `place_gtt_order` (and
    any GTT modify or delete tool): **Needs approval**. Never Always allow.
  - read-only tools (`get_orders`, `get_trades`, `get_positions`,
    `get_holdings`, `get_margins`, `get_ltp`): Always allow is fine.
- In Claude Code, keep the order tools on ask in `.claude/settings.json`:
  `"permissions": {"ask": ["mcp__kite__place_order", "mcp__kite__modify_order",
  "mcp__kite__cancel_order", "mcp__kite__place_gtt_order"]}`, and never put
  an order tool in `allow`.
- Check it: ask Claude for `get_margins` (read only) and confirm the account
  in Kite. Then set `"paper": false` in `trader/config.json` only when the
  user decides to.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket (the entry, its stop leg, the target, the max loss) and
   ask: "Place this order and its stop leg? Reply yes or no." One order per
   question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "haan", "yes to all", or a yes that names a
   different stock or number). Ask again. Never turn a reply into a yes yourself.
4. Paper: `ticket.py paper-fill --index N`. Live: only after "Recorded your
   yes", call `place_order` with exactly the entry JSON the ticket printed.
   Claude's card appears: Allow once or Deny.
5. Live: record it: `ticket.py result --index N --status placed --order-id
   <Kite order_id> [--filled-qty Q --filled-price P]` (from `get_orders` /
   `get_trades`), or `--status denied`, or `--status failed --note ...`. A
   failed order is not retried without a new check and a new yes.
6. Live: once the entry fills, call `place_order` with exactly the stop leg
   JSON (the same yes covers it; Claude's card still asks), then
   `ticket.py result --index N --leg stop --status placed --order-id <id>`.
   If the stop leg fails, say so loudly: the position has no stop at the
   exchange. Place it again or flatten.

## Steps: while it is open, and out by flat-by

- trader-alerts watches the stop, the target and the flat-by time. In paper
  mode run `ticket.py paper-check`; in live mode the SL-M stop sits at the
  exchange.
- When the target is reached, close with `flatten` (below). When the live
  stop fills, record it: `ticket.py fill --symbol RELIANCE --side sell --qty
  46 --price 1160.00`.
- By `flat_by` (15:10 IST), anything still open gets closed, before Zerodha's
  MIS auto square-off (about 15:20, which costs a fee):
  1. Live: ask Kite how many shares are held: `get_positions` (read only).
  2. `ticket.py flatten --symbol RELIANCE` writes an MIS SELL to
     `trader/orders.json`: a LIMIT a little (0.1%, at least one tick) under a
     fresh price, so it fills at once in a normal market and never sells
     far below it; `--market` for a MARKET sell, `--limit PRICE` for yours.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above.
  5. Live: cancel the resting SL-M stop leg FIRST, or the stop and the sell
     could both fill and leave the user short: `cancel_order` with the stop's
     variety and order_id (flatten prints it when the log has it; otherwise
     `get_orders`). Needs approval. Then `place_order` with the ticket, then
     `result`. Paper: `paper-fill` cancels the paper stop by itself.

## Example: the reel

```
ORDER 1 of 1, PAPER (simulated here, nothing goes to Kite), Zerodha Kite, NSE
  BUY 46 RELIANCE, LIMIT 1169.40, MIS intraday, good for today
  stop leg: SELL 46 RELIANCE, SL-M trigger 1160.00, MIS intraday, good for today, placed once the entry fills
  target 1188.20: watched by trader-alerts, not an order at Kite
  max loss about Rs 489.22 if the stop fills at 1160.00, charges included (it can fill worse in a fast market)
  READY: trader-risk PASS, waiting for your yes
  Kite would get place_order with exactly:
    {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY",
     "quantity": 46, "product": "MIS", "order_type": "LIMIT", "price": 1169.4, "validity": "DAY",
     "tag": "tds85567364"}
  then, once the entry fills, the stop leg (covered by the same yes), place_order with:
    {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "SELL",
     "quantity": 46, "product": "MIS", "order_type": "SL-M", "trigger_price": 1160.0, "validity": "DAY",
     "tag": "tds85567364sl"}
```

The user replies "yes". In paper mode, `paper-fill` with a 9:45 quote of
1169.00 fills 46 at 1169.00 and rests the paper stop at 1160.00; a later
quote of 1189.00 hits the target at 1188.20. In live mode Claude calls
`place_order` with the entry JSON, the card reads "Claude wants to use Place
order" with two buttons, Allow once and Deny, and after the fill the same
yes covers the stop leg's `place_order`. The `why`, the `plan` and the
`target` stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes (the stop leg is
  part of its entry's yes, nothing else is).
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, secrets, access tokens or passwords.
- Never switches `"paper"` to live. The user does that themselves.
- Never places F&O orders, GTTs or a second exit order while the stop leg is live.
- Never promises a fill price: a MARKET order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No F&O, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite Connect keys live in environment variables or the Claude Desktop config, never in the chat.
