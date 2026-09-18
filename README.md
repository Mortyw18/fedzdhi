# memebot -- Solana memecoin trend-following + insider copy-trading bot

Paper-first. Bankroll: 0.2 SOL. Every default in this codebase is chosen
around one goal: **survive long enough for the InsiderRadar module to
build a real track record**, because that track record is the only edge
this bot actually has.

Read this file before running anything. Read `HONESTY.md` before you
decide whether to run it live. Read `COSTS.md` before you decide which
RPC tier to pay for.

---

## 1. Strategy doctrine (read this first)

There are exactly three real edges in memecoin trading: **launch speed**,
**information**, and **discipline**. A bot running on a laptop has none
of the first two at the moment a token launches -- by the time our
signal fires, same-block snipers and insiders already have their bags.

What a laptop bot *can* have:

- **Safety filtering** that catches the rug patterns a human under time
  pressure misses (frozen mints, fake-locked LPs, honeypots, bundled
  insider launches).
- **Exit discipline** that a human staring at a pumping chart doesn't
  have -- a stop-loss and a take-profit ladder that fire mechanically,
  every time, with no exceptions for "just one more candle."
- **A self-built record of which insider wallets are actually good.**
  This is the only moat. It doesn't exist on day one. It takes about a
  week of indexing before the conviction leaderboard has enough data to
  mean anything, and that's why paper mode's default run length doubles
  as InsiderRadar's cold-start period.

### Copy-trading reality: two classes of wallet, one of which we copy

Every wallet that's early into a new token falls into one of two
buckets, and they require opposite strategies:

- **Snipers.** Same-block or bundled entries at pool creation. This is
  infrastructure, not insight -- MEV bots and co-located validators, not
  something a laptop with a Helius free-tier WebSocket connection will
  ever compete with on latency. **We do not try.** `InsiderRadar` puts
  these wallets on a sniper leaderboard for visibility and permanently
  excludes them from copying, no matter how good their numbers look --
  once a wallet is seen doing a same-block bundled entry, it's
  structurally uncopyable, full stop.
- **Conviction smart money.** Wallets that take real, sized positions,
  hold them for actual time, across many different tokens, with a
  positive track record over enough trades to not be luck. This is
  copyable within our latency, and it's the *only* kind of wallet this
  bot ever copies.

**HARD RULE, enforced in code, not just in this paragraph:** we copy
entries, never exits. `InsiderRadar` has no method that turns a sell
into a trading signal -- see the module docstring in
`bot/insider_radar.py`. Insiders routinely dump into the liquidity that
their own copy-traders create; if we copied their exits too, we'd be
volunteering to be their bag on the way out. Our exit engine
(`ExitMonitor` + `RiskManager`) manages every position we open,
regardless of how we found it, using our own stop/ladder/time-stop
rules and nothing else.

### Slippage doctrine

Slippage tolerance is not a cost you sometimes pay -- **it's an order
you're placing with MEV bots.** A wide slippage tolerance is an
invitation to get sandwiched. So tolerance is never a fixed number we
pick; it's derived from the trade's own price impact:

```
tolerance = price_impact * 2 + 1%, hard-capped at 10% (15% for emergency exits only)
```

And if the price impact for *our* position size exceeds 5%, we don't
adjust the tolerance -- we reject the trade outright. A pool where a
0.05 SOL buy moves the price more than 5% is not a pool with a real
chart, it's a mirage with a chart-shaped shadow. See
`bot/jupiter_client.py::slippage_bps_for_impact` and
`TokenSafety.check_price_impact`.

### Breakeven math

At roughly 0.003 SOL in network + priority fees per round trip, plus
realistic slippage on entry and exit, a round trip costs **4-6% of
position size** before any price movement at all. That means a trade
has to clear **roughly +25%** before it was ever "profitable" in any
sense that survives fees. This is the single biggest reason most manual
memecoin trades lose money despite feeling like a coin flip: the coin
flip isn't 50/50 against a house edge of 25%.

### Position sizing: the ruin table

Run `python run.py` (or add `--allow-heavy-sizing`) and the bot prints
this table before it will let you trade. The numbers, for a 0.2 SOL
bankroll:

```
RUIN TABLE (bankroll=0.200 SOL)
------------------------------------------------------------------------
 position size |  % of bankroll |  loss @ -35% stop |  % bankroll/stop
        0.050 |         25.0% |           0.0175 |            8.7%
        0.100 |         50.0% |           0.0350 |           17.5%
------------------------------------------------------------------------
  at 0.050 SOL/position, ~17.6 consecutive stop-outs reach an 80% bankroll drawdown
  at 0.100 SOL/position, ~8.4 consecutive stop-outs reach an 80% bankroll drawdown
------------------------------------------------------------------------
```

0.1 SOL positions with a -35% stop lose **19%** of a 0.2 SOL bankroll
per stop-out (once you net out that 0.1 SOL is 50% of bankroll, and 35%
of that is the loss) -- five losing trades in a row is close to ruin.
0.05 SOL survives that same streak with room to spare. Even a genuinely
good edge doesn't justify sizing above ~25% of bankroll per position;
50%+ is negative-growth under any sizing math (Kelly included). This is
why 0.05 SOL is the hard default and 0.1 SOL requires both
`--allow-heavy-sizing` and a typed confirmation of this exact table
(`bot/config.py::confirm_startup`).

---

## 2. Architecture

| Module | File | Responsibility |
|---|---|---|
| RpcGateway | `bot/rpc_gateway.py` | Helius HTTP + WebSocket, retry/backoff, rate-limit budget tracker, failover RPC |
| Solana wallet | `bot/solana_wallet.py` | Keypair loading, raw Ed25519 signing of Jupiter transactions, no external Solana SDK dependency |
| JupiterClient | `bot/jupiter_client.py` | Jupiter v6 quote/swap, the slippage doctrine formula, sell-simulation for honeypot checks |
| TokenSafety | `bot/token_safety.py` | The core gate. Every candidate, from either signal source, passes here before any buy |
| SignalEngine | `bot/signal_engine.py` | DexScreener polling + pump.fun launches, budget-aware, configurable thresholds |
| InsiderRadar | `bot/insider_radar.py` | First-buyer indexing, wallet scoring, sniper/conviction leaderboards, copy-signal emission, auto-unfollow |
| RiskManager | `bot/risk_manager.py` | The constitution: position caps, stop/ladder/time-stop math, no averaging down, ever |
| ExecutionEngine | `bot/execution_engine.py` | Live (real signing/sending) and Paper (haircut-simulated) implementations of the same interface |
| ExitMonitor | `bot/exit_monitor.py` | Isolated polling loop, per-position error boundary, executable (not chart) prices |
| Accounting | `bot/accounting.py` | SQLite ledger + the daily report generator |
| Alerter | `bot/alerter.py` | Telegram notifications, `/status` and `/stop` |
| KillSwitch | `bot/kill_switch.py` | Daily loss cap, consecutive-failure cap, RPC-outage halt, manual reset only |
| Orchestrator | `bot/orchestrator.py` | Wires everything into one asyncio process |
| CLI | `bot/cli.py`, `run.py` | Entry point, confirmations, `--observe-only`, `--sweep`, `--daily-report`, `--radar-stats`, `--reset-kill-switch` |

There is exactly one path from "candidate token" to "open position":
`Orchestrator.evaluate_candidate` -> `TokenSafety.evaluate` ->
`RiskManager.can_open_position` -> `ExecutionEngine.buy`. Insider
copy-trades (`Orchestrator.handle_copy_signal`) build a `Candidate` and
feed it into the exact same function. There is no shortcut for a
conviction leader's buy.

---

## 3. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in HELIUS_API_KEY at minimum; SOLANA_PRIVATE_KEY only for --live
```

Run the test suite (no network required):

```bash
pytest
```

### 3.1 Setting up your `.env`, step by step

#### Helius API key (RPC access)

1. Go to https://dev.helius.xyz and sign up (free tier).
2. Create a project in the dashboard; copy its API key.
3. Paste it into `.env`:
   ```
   HELIUS_API_KEY=<your key>
   ```
   `load_config_from_env` (`bot/config.py`) derives both
   `HELIUS_RPC_URL` and `HELIUS_WS_URL` from this key automatically. You
   only need to set `HELIUS_RPC_URL`/`HELIUS_WS_URL` directly if you're
   using a different provider (QuickNode, a paid Helius plan with a
   custom endpoint, etc.) instead of `HELIUS_API_KEY`.

This is the one variable both `--observe-only` and normal paper-mode
runs actually need -- without it, `TokenSafety` has no RPC to check
mint authorities, holder concentration, or run honeypot simulations
against, and `Config.validate()` will refuse to start `--observe-only`
without it (paper/live trading runs will also fail at the first
on-chain call).

#### Generating a dedicated bot wallet (only needed for `--live` / `--sweep`)

**Never use your main wallet's private key.** Generate a fresh,
dedicated keypair and fund it with only the bankroll you intend to
trade with.

This codebase deliberately avoids depending on the full
solana-py/solders SDK (see `bot/solana_wallet.py`), so you can generate
a compatible keypair with the same primitives the bot itself uses --
no extra tooling required:

```bash
python3 -c "
from nacl.signing import SigningKey
import base58
sk = SigningKey.generate()
secret = bytes(sk) + bytes(sk.verify_key)
print('SOLANA_PRIVATE_KEY=' + base58.b58encode(secret).decode())
print('Wallet address (fund this one):', base58.b58encode(bytes(sk.verify_key)).decode())
"
```

- Paste the `SOLANA_PRIVATE_KEY=...` line into `.env` exactly as printed.
- Fund the printed **wallet address** (not the private key!) with your
  intended bankroll -- 0.2 SOL is the default this entire bot is tuned
  around (see the ruin table above) -- plus roughly another 0.02 SOL to
  cover network and priority fees, since SOL itself pays for its own
  transactions.
- If you'd rather use the official Solana CLI:
  `solana-keygen new --outfile bot-wallet.json --no-bip39-passphrase`,
  then paste the resulting JSON array (e.g. `[12,45,...]`) directly into
  `SOLANA_PRIVATE_KEY` -- `bot/solana_wallet.py` accepts either a
  base58 string or a JSON array of 64 bytes.
- `SOLANA_PRIVATE_KEY` is read once at process start, is never logged,
  and `repr(Wallet)` deliberately omits it -- see
  `bot/solana_wallet.py::Wallet.__repr__` and its test in
  `tests/test_solana_wallet.py::test_wallet_repr_never_leaks_key_material`.
- Paper mode and `--observe-only` never read this variable at all --
  they generate their own disposable, unfunded keypair internally
  purely to satisfy Jupiter's transaction-building API (see
  `Orchestrator.__init__`), so you can leave it blank until you're
  ready for M4.

#### Telegram alerts (optional but recommended)

1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`,
   follow the prompts. It gives you a token.
2. Set `TELEGRAM_BOT_TOKEN=<that token>` in `.env`.
3. Send your new bot any message, then visit
   `https://api.telegram.org/bot<token>/getUpdates` in a browser and
   read `message.chat.id` from the JSON response.
4. Set `TELEGRAM_CHAT_ID=<that id>` in `.env`.

Without these two set, `Alerter` is a safe no-op -- every notification
is still written to the structured logs, it just doesn't reach
Telegram (`bot/alerter.py::Alerter.enabled`).

#### Every variable in `.env.example`, explained

| Variable | Required? | What it does |
|---|---|---|
| `HELIUS_API_KEY` | Recommended | Free-tier Helius key; `HELIUS_RPC_URL`/`HELIUS_WS_URL` are derived from it automatically. |
| `HELIUS_RPC_URL` | Optional | Overrides the derived HTTP RPC endpoint. Set this instead of `HELIUS_API_KEY` for a different provider or a custom paid endpoint. |
| `HELIUS_WS_URL` | Optional | Overrides the derived WebSocket endpoint, used by `InsiderRadar`'s on-chain indexing (`logsSubscribe`). |
| `FAILOVER_RPC_URL` | Optional | A second RPC endpoint `RpcGateway` falls back to if the primary fails. Leave blank to run without failover. |
| `SOLANA_PRIVATE_KEY` | Only for `--live` / `--sweep` | The dedicated bot wallet's secret key (base58 or JSON array). Never your main wallet, never committed, never logged. |
| `TELEGRAM_BOT_TOKEN` | Optional | Enables Telegram alerts and `/status`, `/stop`. Blank = alerts go to logs only. |
| `TELEGRAM_CHAT_ID` | Required if `TELEGRAM_BOT_TOKEN` is set | The chat Telegram alerts are sent to. |
| `BANKROLL_SOL` | Recommended | Feeds the ruin table and the position-size-vs-bankroll checks in `Config.validate()`. Set it to what you actually funded. |
| `DB_PATH` | Optional | SQLite ledger path. Defaults to `data/bot.db`. |

Once `.env` is filled in, sanity-check that it loads correctly without
starting any run that touches RPC or a wallet:

```bash
python run.py --daily-report
```

## 4. Running

**M2 (observe-only) comes first.** `--observe-only` runs SignalEngine,
TokenSafety, and InsiderRadar's indexing against live data and logs
every candidate and safety verdict, but never calls
`ExecutionEngine.buy` -- not even a paper fill. No wallet is required.
This is the mode to run for the 48h M2 gate, before any capital
(paper or real) is ever put at risk:

```bash
python run.py --observe-only                   # logs candidates + safety verdicts, indexes InsiderRadar, never trades
python run.py --observe-only --daily-report     # (in a second terminal) check the rejection-reason breakdown so far
```

Cannot be combined with `--live` -- `Config.validate()` refuses that
combination with an explanation, since observe-only never trades either
way.

Paper mode is the default once you move past M2. It runs the identical
pipeline and risk rules as live mode; only `ExecutionEngine` is swapped
for a simulator that fills against real Jupiter quotes with a 2%
haircut baked in (quotes are always a little optimistic about what
you'd actually get).

```bash
python run.py                                  # paper mode, 0.05 SOL positions
caffeinate python run.py                       # macOS: prevents sleep from silently disabling stops
python run.py --daily-report                   # print today's report and exit
python run.py --radar-stats                    # print InsiderRadar's latest indexing snapshot and exit
python run.py --reset-kill-switch              # manually clear a tripped kill switch
python run.py --sweep --sweep-to <address>     # drain the live wallet (requires typed confirmation)
```

Live mode and heavy sizing both require a flag *and* a typed
confirmation printed alongside the ruin table -- there is no
`--yes`-by-default path to real money:

```bash
python run.py --live                           # requires SOLANA_PRIVATE_KEY, typed confirmation
python run.py --allow-heavy-sizing             # 0.10 SOL positions, typed confirmation of the ruin table
```

**On macOS, closing the laptop lid stops the process.** `ExitMonitor` is
what enforces every stop-loss; if it isn't running, your stops don't
exist. Always run under `caffeinate`, and ideally under `pm2` so a crash
restarts the process instead of silently leaving positions unmanaged:

```bash
pm2 start "caffeinate python run.py" --name memebot
pm2 logs memebot
```

---

## 5. Milestones

| Milestone | What | Gate to proceed |
|---|---|---|
| **M1** | All 10 modules implemented, full test suite passing offline (`pytest`, zero network) | Green suite, no placeholders |
| **M2** | 48h `python run.py --observe-only`: candidates + safety verdicts logged against live data, InsiderRadar begins indexing, zero trades placed | Manually review the rejection log (`--daily-report`) -- if almost nothing is rejected, thresholds are wrong (the daily report says so explicitly above a 70% pass rate) |
| **M3** | 14 days PAPER, unattended, laptop caffeinated, including copy-trade paper results once the leaderboard is live | Zero crashes, complete daily reports every day, honest PnL after fees + haircut (even if negative -- see `HONESTY.md`) |
| **M4** | LIVE at HALF size (0.025 SOL/position via `--position-size 0.025`), kill switch armed | 2 weeks live, net PnL >= 0 before raising to 0.05. Raising to 0.10 (only if ever) requires another 2 weeks positive AND the operator accepting the ruin table in writing |

Each gate is a decision for the human operator, not something the code
auto-advances through. Nothing in this codebase raises position size or
flips to live mode on its own.

---

## 6. Ops & security

- The private key is read from `SOLANA_PRIVATE_KEY` only. It is never
  logged, never printed, and `repr(Wallet)` deliberately omits it (see
  `bot/solana_wallet.py`).
- `python run.py --sweep --sweep-to <address>` drains the wallet
  (balance minus a fee reserve) to a destination you specify, behind a
  typed confirmation.
- Logs are structured JSON with rotation (`bot/logging_setup.py`), one
  object per line, written to `logs/memebot.jsonl`. Every pipeline stage
  can be wrapped in `stage_timer` for per-stage latency instrumentation.
- `RpcGateway` tracks its own request budget against the configured
  rate limit and alerts (via `Alerter`) once at 70% utilization -- the
  alert latches (won't re-fire on jitter around the threshold) and only
  re-arms once usage drops back below 40%. On an actual 429, the gateway
  never retries immediately: it sets a shared, exponentially growing
  cooldown (capped at 60s) that every subsequent call waits out first,
  which is what stops a rate-limit episode from spiraling. See
  "The free tier is a design constraint" in `COSTS.md` for the full
  picture, including how `TokenSafety` and `InsiderRadar`'s indexing
  keep their own RPC usage bounded, and how `--daily-report` surfaces
  calls-per-minute and peak budget usage after the fact.

### Diagnosing a quiet run

An overnight run once logged 0 signals, 0 safety checks, and 0 RPC calls
for 10 hours straight -- and nothing in the logs distinguished that from
"a genuinely quiet, healthy night." The root cause was `SignalEngine`'s
default DexScreener query (`"solana"`): DexScreener's `/search` endpoint
is a keyword text search over token name/symbol/address, not a chain
filter, and the literal word "solana" almost never appears in a pair's
name or symbol -- so the `chainId=="solana"` filter had nothing to keep,
every single poll. The default is now `"SOL"` (the actual quote-token
symbol on nearly every Solana memecoin pair), but the real fix is that
this class of failure is no longer silent:

- Every DexScreener poll ends with one INFO-level `dexscreener_poll_summary`
  log: raw pairs returned, how many were even on-chain (`solana_pairs`),
  and a rejection-reason breakdown for the rest. If `solana_pairs` is
  consistently 0 or 1, the query itself is the thing to change, not the
  liquidity/volume thresholds.
- Every `heartbeat_interval_s` (default 10min) there's one INFO-level
  `heartbeat` log: poll counts per source, RPC calls/minute and budget
  usage, WebSocket active-connection and reconnect counts, and whether
  the kill switch is halted. Silence between heartbeats now means "still
  running," not "might have died three hours ago."
- `python run.py --radar-stats` shows InsiderRadar's last snapshot
  (wallets indexed, tokens tracked, buy/sell events seen) -- previously
  this state existed only in the running process's memory and the daily
  report had no visibility into it at all.

---

## 7. Project tree

```
bot/
  models.py            shared dataclasses (Candidate, SafetyVerdict, Position, Fill, LeaderStats, ...)
  config.py             Config, defaults, reckless-config refusal, the ruin table, CLI confirmations
  logging_setup.py       structured JSON logging + stage_timer
  rpc_gateway.py          RpcGateway (HTTP) + RpcWebSocket (logsSubscribe/accountSubscribe)
  solana_wallet.py        keypair loading, raw Ed25519 signing, shortvec helpers, SOL transfer builder
  jupiter_client.py       Jupiter v6 client, slippage doctrine, honeypot sell-simulation
  token_safety.py         every safety check, run on every candidate
  signal_engine.py        DexScreener + pump.fun polling and filtering
  insider_radar.py        indexing, wallet scoring, leaderboards, copy-signal emission
  risk_manager.py         position gating, stop/ladder/time-stop math, no-averaging-down
  execution_engine.py     LiveExecutionEngine + PaperExecutionEngine
  exit_monitor.py         isolated per-position exit polling loop
  accounting.py           SQLite ledger + daily report
  alerter.py              Telegram notifications + /status /stop
  kill_switch.py          daily cap / failure cap / RPC-outage halt, manual reset
  orchestrator.py         wires every module together
  cli.py                  argument parsing, confirmations, one-shot commands
run.py                    entry point
tests/                    unit tests + replay harness, zero network
requirements.txt
.env.example
README.md HONESTY.md COSTS.md
```

---

## 8. Next steps

See the end of `HONESTY.md` and the final message of the delivery for
three proposed next improvements once M1 is reviewed.
