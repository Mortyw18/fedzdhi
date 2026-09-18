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
