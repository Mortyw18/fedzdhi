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
