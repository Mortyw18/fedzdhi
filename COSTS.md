# COSTS.md

Numbers below are estimates for planning purposes. RPC provider pricing
changes; verify current tiers before committing to a paid plan. Solana
network fees are approximately stable but priority fees fluctuate with
network congestion.

## RPC tiers

| Provider | Tier | Approx. cost | Notes |
|---|---|---|---|
| Helius | Free | $0/mo | Generous for a single bot at this trade frequency; shared rate limits. This is the default (`HELIUS_API_KEY` with no paid plan). `RpcGateway`'s budget tracker (`rpc_rate_limit_per_10s`, default 100 req/10s) is set conservatively below the free-tier ceiling and alerts at 70% usage. |
| Helius | Developer | ~$49/mo | Higher rate limits, dedicated enhanced APIs (webhooks, better indexing). Worth it once InsiderRadar is subscribed to enough wallets/programs that the free tier's WebSocket connection limits start to bind. |
| Helius | Business+ | ~$499+/mo | Not justified at 0.2 SOL bankroll. Listed for completeness. |
| QuickNode | Build/Free | $0/mo (limited) | Alternative if Helius rate limits become the bottleneck; the config supports `FAILOVER_RPC_URL` for exactly this kind of provider diversification. |

**Recommendation for this bankroll: Helius free tier.** Nothing about a
0.2 SOL bankroll trading a handful of times a day justifies a paid RPC
plan. Revisit only if `RpcGateway`'s 70%-budget alerts start firing
routinely.

## The free tier is a design constraint, not an afterthought

Three things in the code exist specifically to keep this bot inside a
free-tier RPC budget, not just to survive an occasional spike:

- **TokenSafety batches, never loops, per-holder RPC calls.** The
  holder-concentration and LP-burn checks used to make up to ~2 individual
  `getAccountInfo` calls per top holder (owner lookup, then
  pool-authority lookup) -- worst case, ~40 calls for ONE candidate's
  holder check alone. They now resolve all holders in a single batched
  `getMultipleAccounts` call each (owners, then pool authorities), so the
  cost is ~4 RPC calls regardless of how many holders there are. The mint
  account is also fetched once per candidate and shared between the
  mint/freeze and transfer-fee checks instead of being fetched twice.
- **InsiderRadar's on-chain indexing is the lowest-priority RPC
  consumer, and it knows it.** A `logsSubscribe` on an AMM program's logs
  can be a genuinely enormous stream -- most swap activity on that
  program network-wide, not just candidates this bot cares about. Before
  spending an RPC call on any indexing notification, the bot checks
  whether the kill switch is halted or the gateway's own rolling budget
  usage is already above `indexing_max_rpc_budget_pct` (default 50%) and,
  if so, drops the notification without ever calling `getTransaction`.
  TokenSafety and ExitMonitor -- the checks a live trade is actually
  waiting on -- always get priority over background learning.
- **A 429 never triggers an immediate retry.** `RpcGateway` treats a 429
  as a signal to stop entirely for a while: it sets a shared,
  gateway-wide cooldown (exponential, capped at 60s, reset only after a
  call actually succeeds) that every subsequent call -- from any code
  path -- waits out before trying again. This is what actually stops a
  rate-limit episode from turning into the death spiral of "every caller
  retries a few times, which trips the limit further, which makes every
  caller retry again."
- **A per-mint verdict cache stops repeat evaluation, not just repeat
  RPC calls.** DexScreener rediscovers the same actively-trending mints
  every poll cycle by design. Without a cache, `evaluate_candidate` reran
  the ENTIRE safety pipeline -- RPC, Jupiter, rugcheck, and pump.fun's
  graduation API -- on the same mint every single cycle, forever.
  `verdict_cache_ttl_s` (default 20 min) means a rediscovered mint short-
  circuits before any of that spends a single call, with a cache-bypass
  on a large liquidity swing or an RPC-error verdict (see HONESTY.md and
  README.md's "Verdict cache" section for why those two cases skip the
  cache). This is the single biggest lever on RPC budget when
  `--daily-report` shows a high `safety_checks` count relative to
  `signals` -- if they're close to equal, the cache isn't doing its job
  (check `verdict_cache_ttl_s` and `verdict_cache_liquidity_change_pct`).
- **Indexing has its own local rate cap and exponential backoff**,
  separate from and in addition to the RPC-budget-ceiling throttle above
  -- see README.md's "Indexing's own backoff" section. Neither one costs
  a single `RpcGateway.call()` when it triggers; both exist specifically
  to stop a sustained RPC outage from turning into a tight retry loop the
  same way the 429 cooldown above does for rate limiting specifically.

The RPC budget itself is audited, not just capped: `RpcGateway` tracks
per-method call counts and calls-per-minute live, and the running bot
snapshots that into SQLite every `rpc_budget_snapshot_interval_s`
(default 60s) so `python run.py --daily-report` shows average/peak
calls-per-minute, peak budget usage, and the top methods by call count
for the day -- even though that command itself never starts a live
gateway. If the report shows budget usage regularly peaking near 100%,
that's the signal to either raise `dexscreener_poll_interval_s`, lower
`indexing_max_rpc_budget_pct` further, or move to a paid RPC tier --
not to loosen the budget tracker's own limit.

### A day of "RPC budget: 0.0" is not automatically a bug

`RpcGateway`'s budget tracker only counts calls that actually go through
`RpcGateway.call()` -- and only two things in this codebase ever do
that: `TokenSafety`'s RPC-backed checks (mint/holder/LP-burn) and
InsiderRadar's indexing loop's `getTransaction` lookups. Several other
load-bearing parts of the pipeline never touch it at all, by design:

- **DexScreener polling** uses `SignalEngine`'s own `requests.Session`,
  talking directly to `api.dexscreener.com` -- not Helius, not
  `RpcGateway`, at any point.
- **WebSocket indexing** (`RpcWebSocket`) opens its own raw
  `websockets.connect()` to Helius's WS endpoint and does its own
  JSON-RPC framing over that socket. It never calls `RpcGateway.call()`
  either, so `logsSubscribe` traffic doesn't touch the HTTP budget
  tracker regardless of how many notifications arrive.
- **`check_lp_or_graduation`'s pump.fun graduation lookup, `check_honeypot`'s
  Jupiter sell-route quote, and `check_rugcheck_secondary`** all go through
  their own `requests.Session`s (pump.fun's coin API, Jupiter's quote API,
  RugCheck) -- none of them touch `RpcGateway` either, same as DexScreener
  and pump.fun's coin-discovery polling above.

So if `TokenSafety.evaluate()` was never invoked (no candidate ever
passed SignalEngine's filters) and the indexing loop never got as far as
a successful `getTransaction` (e.g. it was skipped by the kill-switch
guard, or budget-priority backoff, the whole run), the RPC budget
snapshot legitimately reads all zeroes for the entire period -- not
because tracking broke, but because nothing that counts against it ever
ran. Check `--radar-stats` and the `dexscreener_poll_summary` /
`heartbeat` log lines before assuming a flat RPC budget line means the
tracker itself is broken; it usually means the funnel upstream of it is
empty.

## Per-trade fee estimate

| Component | Estimate | Notes |
|---|---|---|
| Base network fee (signature) | 0.000005 SOL | Fixed protocol fee per transaction |
| Priority fee | 0.00001 - 0.0001 SOL | Set dynamically from `getRecentPrioritizationFees`; varies with congestion. Memecoin launch windows see spikes. |
| Jupiter aggregator fee | ~0 SOL | Jupiter's public swap API does not charge a platform fee for this integration |
| Slippage cost (doctrine-derived) | position-dependent | Not a fixed fee -- `slippage_bps_for_impact` caps it at 10% (15% emergency), but typical entries at 1-3% price impact cost roughly 3-7% after the 2x multiplier + 1% addon |
| **Total per fill (network + priority)** | **~0.0005 - 0.0015 SOL** | |
| **Total per round trip (buy + sell)** | **~0.001 - 0.003 SOL** | Matches the ~4-6% of a 0.05 SOL position cited in README.md's breakeven math, once slippage is included |

A position that uses the take-profit ladder generates a **third** fill
(partial sell at +100%, then a final sell on the trailing stop or time
stop), so a "successful" trade can cost more in fees than a simple
stop-out, not less.

## Projected burn at 3 trades/day

`max_buys_per_day` defaults to 3. Assume roughly 1.4 sell fills per buy
on average (some positions hard-stop in a single fill, some ladder into
two):

| Period | Buys | Est. sell fills | Total fills | Fee cost (network + priority only) |
|---|---|---|---|---|
| 1 day | 3 | 4 | 7 | ~0.0035 - 0.0105 SOL |
| 1 week | 21 | 29 | 50 | ~0.025 - 0.075 SOL |
| 1 month (30d) | 90 | 126 | 216 | ~0.108 - 0.324 SOL |

**This is the number that matters most in this file: at 3 trades/day,
fees alone over a month range from roughly half the entire 0.2 SOL
bankroll to more than it**, before accounting for slippage on top of
network/priority fees, and completely independent of whether the
underlying trades are winners or losers. This is not a hypothetical --
it's the same arithmetic behind the "+25% to breakeven" figure in
README.md, extended across a month of trading frequency. It's also
exactly why `max_buys_per_day=3` and `daily_loss_cap_sol=0.04` exist as
hard caps rather than suggestions, and why M3's 14-day paper gate
reports PnL **after** fees and the paper haircut, not before.

(`max_buys_per_day` was lowered from an earlier default of 5 specifically
to cut this monthly fee burn -- at 5 trades/day the same arithmetic put
the high end of the range at ~0.54 SOL/month, more than 2.5x the entire
bankroll in fees alone.)

RPC costs do not scale with trade count in any way that matters here --
even at 3 trades/day, request volume for quoting, safety checks, and
exit polling stays comfortably inside Helius's free tier. The dominant
cost, by a wide margin, is Solana network + priority fees, not
infrastructure.
