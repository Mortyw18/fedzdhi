"""InsiderRadar against synthetic wallet histories.

Three required outcomes from the spec:
  - a sniper wallet (same-slot bundled entries) lands on the sniper board
    and is NEVER copyable, no matter how profitable.
  - a genuinely diversified, patient, profitable wallet qualifies for the
    conviction board and is copyable.
  - a wallet with monster wins concentrated on a single token does NOT
    qualify, because the distinct-token requirement exists specifically
    to filter out "got lucky on one launch."
"""
from __future__ import annotations

from bot.insider_radar import InsiderRadar
from bot.models import WalletBuyRecord, WalletClass, WalletSellRecord, now_ts

DAY = 86400.0


def _round_trip(radar: InsiderRadar, wallet: str, mint: str, slot: int, buy_sol: float, sell_sol: float, hold_s: float, t0: float, tokens: float = 1000.0) -> None:
    """A full round trip: buys `tokens` and later sells all of them.
    pnl = sell_sol - buy_sol exactly, since 100% of the lot is matched."""
    radar.record_buy(WalletBuyRecord(wallet=wallet, mint=mint, slot=slot, amount_sol=buy_sol, price_usd=0.001, tokens=tokens, timestamp=t0))
    radar.record_sell(
        WalletSellRecord(wallet=wallet, mint=mint, slot=slot + 1, amount_sol=sell_sol, price_usd=0.002, tokens=tokens, timestamp=t0 + hold_s)
    )


def test_sniper_wallet_never_copyable():
    radar = InsiderRadar(bundle_cluster_min_wallets=3)
    mint = "SnipedMint"
    t0 = 0.0
    # Three wallets buy in the exact same slot as pool creation -> bundled launch.
    for i, wallet in enumerate(["sniper_1", "sniper_2", "sniper_3"]):
        radar.record_buy(WalletBuyRecord(wallet=wallet, mint=mint, slot=500, amount_sol=1.0, price_usd=0.001, timestamp=t0))

    # Give the sniper a great, prolific, diversified track record on paper --
    # none of it should matter once it's flagged as a same-block sniper.
    wallet = "sniper_1"
    radar._wallet_stats[wallet].first_seen = t0 - 30 * DAY
    for i in range(25):
        _round_trip(radar, wallet, f"OtherMint{i}", slot=1000 + i, buy_sol=0.1, sell_sol=0.3, hold_s=20 * 60, t0=t0)

    assert radar.classify(wallet) == WalletClass.SNIPER
    assert radar.should_copy(wallet) is False
    sniper_wallets = {s.wallet for s in radar.sniper_leaderboard()}
    assert wallet in sniper_wallets
    conviction_wallets = {s.wallet for s in radar.conviction_leaderboard()}
    assert wallet not in conviction_wallets


def test_conviction_wallet_qualifies_and_is_copyable():
    radar = InsiderRadar(
        conviction_min_trades=20,
        conviction_min_distinct_tokens=15,
        conviction_min_hold_s=15 * 60,
        conviction_min_wallet_age_days=14,
    )
    wallet = "conviction_wallet"
    t0 = 1_000_000.0

    for i in range(20):
        _round_trip(radar, wallet, f"Mint{i}", slot=1000 + i, buy_sol=0.1, sell_sol=0.15, hold_s=30 * 60, t0=t0)

    radar._wallet_stats[wallet].first_seen = t0 - 30 * DAY

    stats = radar.stats_for(wallet)
    assert stats.trades_closed == 20
    assert stats.distinct_tokens == 20
    assert stats.realized_pnl_sol > 0
    assert stats.median_hold_s == 30 * 60

    assert radar.classify(wallet) == WalletClass.CONVICTION
    assert radar.should_copy(wallet) is True

    buy = WalletBuyRecord(wallet=wallet, mint="NewLaunch", slot=99999, amount_sol=0.2, price_usd=0.01)
    signal = radar.evaluate_copy_signal(buy)
    assert signal is not None
    assert signal.leader_wallet == wallet
    assert signal.mint == "NewLaunch"


def test_single_token_concentration_does_not_qualify():
    """3 monster wins on ONE token, repeated to hit the trade-count floor,
    must NOT qualify -- distinct-token diversity is the point of the rule."""
    radar = InsiderRadar(conviction_min_trades=20, conviction_min_distinct_tokens=15)
    wallet = "one_trick_pony"
    t0 = 1_000_000.0

    # 20 profitable round trips, but all on the same 2 tokens.
    for i in range(20):
        mint = "MintA" if i % 2 == 0 else "MintB"
        _round_trip(radar, wallet, mint, slot=1000 + i, buy_sol=0.1, sell_sol=0.5, hold_s=30 * 60, t0=t0)

    radar._wallet_stats[wallet].first_seen = t0 - 30 * DAY

    stats = radar.stats_for(wallet)
    assert stats.trades_closed == 20
    assert stats.distinct_tokens == 2  # far below the 15 required
    assert stats.realized_pnl_sol > 0

    assert radar.classify(wallet) != WalletClass.CONVICTION
    assert radar.should_copy(wallet) is False


def test_wallet_below_age_threshold_not_conviction():
    radar = InsiderRadar(conviction_min_trades=5, conviction_min_distinct_tokens=3, conviction_min_wallet_age_days=14)
    wallet = "too_new"
    t0 = now_ts()  # wallet's first trade is "now" -- nowhere near the 14-day age floor
    for i in range(5):
        _round_trip(radar, wallet, f"Mint{i}", slot=1000 + i, buy_sol=0.1, sell_sol=0.2, hold_s=30 * 60, t0=t0)
    # first_seen left at default (now), so wallet_age_days ~ 0
    assert radar.classify(wallet) == WalletClass.UNRANKED


def test_never_copies_a_sell():
    """There is no method on InsiderRadar that turns a sell into a trade signal."""
    radar = InsiderRadar()
    assert not hasattr(radar, "evaluate_sell_signal")
    # record_sell must not raise and must not create any actionable signal
    radar.record_buy(WalletBuyRecord(wallet="w", mint="m", slot=1, amount_sol=0.1, price_usd=0.001))
    result = radar.record_sell(WalletSellRecord(wallet="w", mint="m", slot=2, amount_sol=0.2, price_usd=0.002))
    assert result is None


def test_auto_unfollow_on_negative_trailing_pnl():
    radar = InsiderRadar(
        conviction_min_trades=20,
        conviction_min_distinct_tokens=15,
        auto_unfollow_trailing_n=20,
    )
    wallet = "fading_leader"
    t0 = 1_000_000.0

    for i in range(20):
        _round_trip(radar, wallet, f"Mint{i}", slot=1000 + i, buy_sol=0.1, sell_sol=0.15, hold_s=20 * 60, t0=t0)
    radar._wallet_stats[wallet].first_seen = t0 - 30 * DAY
    assert radar.classify(wallet) == WalletClass.CONVICTION

    # Now a losing streak: 20 more trades, all losers, pushing the trailing
    # window (last 20) net negative.
    for i in range(20, 40):
        _round_trip(radar, wallet, f"Mint{i}", slot=1000 + i, buy_sol=0.2, sell_sol=0.05, hold_s=20 * 60, t0=t0 + i)

    assert radar.classify(wallet) == WalletClass.UNRANKED
    assert radar.should_copy(wallet) is False
    assert radar.stats_for(wallet).unfollowed is True


def test_distinct_token_count_used_for_bundled_launch_exemption():
    radar = InsiderRadar()
    wallet = "diversified_wallet"
    for i in range(16):
        radar.record_buy(WalletBuyRecord(wallet=wallet, mint=f"Mint{i}", slot=1000 + i, amount_sol=0.1, price_usd=0.001))
    assert radar.distinct_token_count(wallet) == 16
    assert radar.distinct_token_count("unknown_wallet") == 0
