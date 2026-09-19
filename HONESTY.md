# HONESTY.md -- the candid version of the pitch above

README.md explains what this bot does and why it's built the way it is.
This file is the part of the conversation that doesn't make it into a
pitch deck. If you only read one file before running this live, make it
this one.

## The base rate is bad, and this bot doesn't repeal it

Most systems that look like this one -- a retail-accessible bot trading
low-cap memecoins against professional market makers, MEV searchers, and
the token deployers themselves -- lose the bankroll. Not "underperform."
Lose it. The mechanism is boring and doesn't require the bot to be badly
built: fees and slippage impose a real, mechanical drag (see the
breakeven math in README.md -- roughly +25% needed per round trip just
to be flat), and the counterparties on the other side of most memecoin
trades are structurally faster, better informed, or both. A well-written
bot reduces unforced errors. It does not, by itself, turn a
negative-expectancy game positive.

## The honeypot check is a proxy, not a guarantee

`TokenSafety.check_honeypot` cannot truly simulate selling a token before
this bot has bought it -- a real sell simulation needs a funded associated
token account for that mint, and we don't have one pre-buy. Solana's
`simulateTransaction` has no mechanism to override account state to fake
holding the token (unlike forking an EVM chain), so a first version of
this check that tried to simulate a real sell transaction failed with
`AccountNotFound` on every single candidate, regardless of whether it was
actually a honeypot. It could not pass on any token, ever -- a much worse
failure mode than a slightly weak check, since it silently meant "this
bot cannot buy anything" rather than "this bot is being appropriately
cautious."

What it checks now instead, cheaply, on a free RPC tier:

1. Jupiter quotes a sell-side route for our exact holdings, for a positive
   SOL amount out. A quote is a price lookup computed from the pool's
   on-chain reserves; it does not require holding the token, so it works
   before buying. It also does not prove sellability at execution time --
   a quote can exist for a pool whose actual transfer logic still reverts.
2. For DexScreener-sourced candidates, at least one real sell happened in
   the observed 5-minute window. pump.fun's coin feed doesn't expose this
   data at all, so pump.fun-sourced candidates skip this signal entirely
   rather than being unfairly rejected for a gap in the data, not the
   token.
3. No active Token-2022 `transferHook` extension on the mint -- the most
   common real honeypot mechanism on current Solana launches (arbitrary
   program logic runs on every transfer, including a sell, and can revert
   selectively). A quote never actually invokes the hook, so this is the
   one signal here that catches what signal 1 structurally cannot.

None of this is a guarantee. A sufficiently patient rug can pass all
three: route liquidity that's real but thin, sell volume from wash
trading or the deployer's own wallets, and a malicious mechanism that
isn't a Token-2022 transfer hook (a custom AMM program with its own
sell-blocking logic, for instance, which check 1's quote would also fail
to catch if the AMM itself refuses to route the sell -- but would then
correctly reject on signal 1). Treat a passing honeypot check as "the
cheapest available signals didn't fire," not as "confirmed sellable."

## Graduation lookups only work for pump.fun-origin tokens

`check_lp_or_graduation`'s LP-burn verification only has two paths that
actually resolve to a real answer: a pump.fun-origin mint (detected by its
vanity `...pump` address suffix), whose bonding-curve/graduation state is
asked directly from pump.fun's own API, and a candidate that already
carries a known `lp_mint` with a resolvable burn percentage on-chain. A
non-pump.fun DEX-native launch with no independently-known `lp_mint`
fails this check closed -- rejected, not passed on uncertainty -- because
this bot has no general way to resolve an arbitrary Raydium, Orca, or
Meteora pool's LP mint from just a pool/pair address without decoding
that DEX's own binary account layout (each is different, and none of them
are `getAccountInfo`-with-`jsonParsed`-friendly the way SPL Token and
System accounts are). If the daily report's rejection breakdown shows
`lp_burned_or_graduated` dominating for candidates whose mint doesn't end
in `pump`, that's this limitation, not a bug -- and not something safe to
work around by trusting an unresolved LP mint on faith.

This pump.fun-side lookup has its own on/off switch,
`enable_pumpfun_graduation_lookup` (default `true`), deliberately separate
from `enable_pumpfun_source` (which only controls SignalEngine's discovery
poll against a different, confirmed-530-blocked pump.fun endpoint).
Turning discovery off must not also turn this lookup off -- a production
run once did exactly that under a single shared flag, and every pump.fun
candidate discovered via DexScreener started failing this check with
"pump.fun lookups disabled via config," even though the graduation
endpoint itself was never confirmed dead. If the coin-info endpoint this
check hits (`PUMPFUN_COIN_INFO_URL` in `token_safety.py`) is ever itself
confirmed dead, set `ENABLE_PUMPFUN_GRADUATION_LOOKUP=false` deliberately
-- at which point every pump.fun-origin candidate also fails closed, same
as the non-pump.fun case above.

## The likely M3 outcome

If you run this bot honestly for the full 14-day paper period, the most
probable result is discovering that **the edge, if any exists, does not
cover the round-trip cost of fees and slippage.** That's not a bug in
the implementation; it's the modal outcome for this entire category of
strategy. `safety_pass_rate` and the rejection-reason breakdown in the
daily report exist specifically so you can tell the difference between
"the strategy doesn't work" and "the strategy never got a fair test
because the filters were miscalibrated" -- but don't let a healthy
rejection rate become a reason to assume the surviving trades are good.
Rejecting 80% of garbage and still losing money on the other 20% is a
completely normal, well-instrumented failure.

## Copying insiders can mean copying their exit liquidity, even though we never copy their sells

The HARD RULE in this codebase is real: `InsiderRadar` has no code path
that turns an observed sell into a trade, and `evaluate_copy_signal`
only ever fires on a buy. That protects you from the most direct version
of the problem -- mirroring an insider's dump in real time.

It does **not** fully protect you from a subtler version: if enough
copy-traders (us included) buy in response to seeing a conviction
wallet's entry, that collective buying pressure is itself what lets the
insider sell into size without moving the price against themselves. We
never copy their sell instruction, but we can still be part of the
liquidity their sell relies on. The `copy_max_price_move_pct` gate
(default 10%) and the bundled-launch rejection reduce this -- they
filter out the most obvious "we are the exit" setups -- but "reduce"
is not "eliminate." Any wallet good enough to make the conviction board
by our thresholds is also, definitionally, a wallet other copy-trading
bots have likely found too. Treat every conviction-board copy trade as
carrying this residual risk, not as a clean signal.

## What InsiderRadar's cold start actually means

The conviction leaderboard is empty for the first several days, by
design -- `conviction_min_trades=20`, `conviction_min_distinct_tokens=15`,
and `conviction_min_wallet_age_days=14` cannot be satisfied
instantaneously. During that window, every candidate this bot trades
comes from `SignalEngine` alone (DexScreener + pump.fun trend-following),
which is the weaker of the two signal sources by the strategy doctrine's
own logic in README.md. Don't read early paper-mode results as
representative of the bot's eventual behavior once copy-trading is live;
don't read them as irrelevant either, since a bot that can't survive its
own cold start on trend-following alone has no business advancing to
insider-copy trades in the first place.

## InsiderRadar's indexing has real, unrecoverable coverage gaps

`RpcWebSocket.subscribe()` reconnects on a dropped WebSocket and correctly
re-sends the subscribe request -- it does not silently die or get stuck.
What it cannot do is recover the gap itself: `logsSubscribe` is a live
stream with no replay or cursor on Solana's side, so any transaction the
chain confirmed between the drop and the new connection's ack is gone,
not delayed. `total_reconnects` and `last_drop_at` (surfaced in the
heartbeat log) tell you how often this happened, not what was missed
during it. In practice this means InsiderRadar's first-buyer/conviction
data is a *sample* of on-chain activity, not a complete record, even on a
long-running, well-connected instance -- treat wallet stats as directionally
useful, not as an exact trade count.

## Event-driven discovery's detection is a best-effort guess, verified but not proven live

`bot/pool_events.py` decides "this transaction is a pool creation / token
launch" by matching a byte-level instruction discriminator, and resolves
the traded mint via the SPL Token Program's own initializeMint
instructions rather than guessing either program's account layout. Both
pieces were checked against external sources during development (Raydium's
own GitHub source for the Initialize2 discriminant; pump.fun's own public
docs and an independent third-party analysis for the create discriminator
-- see pool_events.py's module docstring for the specifics and what's
still unverified, including a possible newer `create_v2` this doesn't
account for). None of that is the same as having run this against live
mainnet traffic and confirmed real detections end to end -- it hasn't been,
as of when this was written.

The failure mode if any of it is wrong is the honest kind, not the
dangerous kind: a discriminator mismatch means a real launch is silently
NOT detected (fails closed -- `detect_pool_creation` returns `None`, the
loop just moves to the next notification); an ambiguous mint resolution
does the same (`resolve_new_mint` returns `None` rather than guessing,
so a candidate is never fabricated from a wrong mint). Nothing here can
produce a WRONG candidate, only a MISSED one. If `ws_pool_events_matched`
stays near zero for one program while `ws_pool_events_seen` is clearly
nonzero, that program's detection logic is the first thing to
re-verify -- and DexScreener polling, still running unchanged as the
backup path, means a missed detection here is a delay, not a blind spot.

**A second, earlier gate now sits in front of the check above:**
`matches_creation_log_hint` decides whether a getTransaction fetch is
even worth making, by matching content in the `logsSubscribe`
notification itself (pump.fun's Anchor-generated "Instruction: Create"
log; Raydium's own `ray_log` LogType discriminant) -- see its docstring
in pool_events.py for the verified sources this is built on and the
production numbers that motivated it (a random rate-capped sample of ALL
traffic could statistically never catch a rare creation). Same failure
shape as above: if this pre-filter's assumption about either program's
logging format is ever wrong, it fails closed (the notification is never
even fetched, not fetched-and-misjudged) -- a missed detection, never a
fabricated one. The broad, throttled sample InsiderRadar's indexing still
separately runs continues to also check every transaction it fetches
through the same byte-level `detect_pool_creation`, as a fallback that
doesn't depend on the log pre-filter's assumptions holding.

**It has already been wrong once, in the loose direction.** The first
version matched a bare substring across the notification's whole log
list. Because `logsSubscribe` delivers every program's logs for the whole
transaction, and because the SPL Associated Token Account program emits a
`Program log: Instruction: Create` line byte-identical to pump.fun's, it
fired on a large share of ordinary pump.fun BUY traffic (~5 fetches/sec).
Detection itself stayed correct -- the byte-level discriminator rejected
every one of them -- but the wasted fetch rate was enough to stall the
WebSocket consumer and cause repeated server-side disconnects. It now
attributes each log line to the program that actually emitted it. The
lesson worth keeping: a pre-filter being cheap per event doesn't make it
free, and a matcher that is never *wrong* can still be badly *loose*.

None of these log-format assumptions have been confirmed against live
mainnet traffic from inside the test suite -- every fixture there is
synthetic, built from documented shapes. `tools/verify_pool_detection.py`
runs both stages against real transactions using a real RPC key, and
running it is the only thing that actually proves this works end to end.

## What would genuinely surprise us

A result that would be a genuine, non-obvious surprise: the conviction
leaderboard, after a full M3 cycle, identifying a small set of wallets
whose PnL edge **holds up out-of-sample** in the following month, net of
our fees and haircut -- not just during the window they were scored on.
Memecoin markets are adversarial and fast-adapting; a wallet's edge
during the indexing window is exactly the kind of thing that tends to
decay once it's identifiable (whether because the wallet's strategy
stopped working, or because enough copy-traders found it that its entries
stopped being early). If `per_leader_pnl` in the accounting output shows
a stable, positive, *persistent* edge for the same wallets across
multiple weekly digests, that's worth taking seriously precisely because
it would be unusual. A leaderboard that constantly turns over, or one
where auto-unfollow keeps firing on wallets that looked great a month
earlier, is the expected, unsurprising outcome -- and also a useful one,
because it's the daily/weekly report telling you the truth about what
you're actually looking at.

## The honest recommendation

Run M1 through M3 exactly as specified, including the parts of the
milestone gates that are boring (reading the rejection log, not just the
PnL number). If M3 doesn't clear net PnL >= 0 after the 2% paper
haircut, the correct response is not to loosen thresholds until it does
-- that's fitting the bot to noise. It's to conclude that, at this
bankroll and on this hardware, the edge doesn't exist yet, and to decide
honestly whether more indexing time, a different signal mix, or simply
not trading live is the right next step.
