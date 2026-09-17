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
| CLI | `bot/cli.py`, `run.py` | Entry point, confirmations, `--sweep`, `--daily-report`, `--reset-kill-switch` |

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

## 4. Running

Paper mode is the default. It runs the identical pipeline and risk
rules as live mode; only `ExecutionEngine` is swapped for a simulator
that fills against real Jupiter quotes with a 2% haircut baked in
(quotes are always a little optimistic about what you'd actually get).

```bash
python run.py                                  # paper mode, 0.05 SOL positions
caffeinate python run.py                       # macOS: prevents sleep from silently disabling stops
python run.py --daily-report                   # print today's report and exit
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
| **M2** | 48h OBSERVE-ONLY on live data: candidates + safety verdicts logged, InsiderRadar begins indexing | Manually review the rejection log -- if almost nothing is rejected, thresholds are wrong (the daily report says so explicitly above a 70% pass rate) |
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
  rate limit and alerts (via `Alerter`) at 70% utilization, before
  Helius starts returning 429s.

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
