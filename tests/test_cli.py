from __future__ import annotations

from bot.accounting import Accounting
from bot.cli import _cmd_radar_stats
from bot.config import Config


def test_radar_stats_no_snapshots_yet(tmp_path, capsys):
    cfg = Config(db_path=str(tmp_path / "bot.db"))
    rc = _cmd_radar_stats(cfg)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No InsiderRadar snapshots recorded yet" in out


def test_radar_stats_prints_latest_snapshot(tmp_path, capsys):
    db_path = str(tmp_path / "bot.db")
    acc = Accounting(db_path)
    acc.record_radar_snapshot(
        {
            "wallets_indexed": 12,
            "tokens_tracked": 40,
            "total_buy_events": 88,
            "total_sell_events": 30,
            "sniper_count": 3,
            "conviction_count": 1,
            "unfollowed_count": 0,
            "watch_list_size": 12,
        }
    )
    acc.close()

    cfg = Config(db_path=db_path)
    rc = _cmd_radar_stats(cfg)
    out = capsys.readouterr().out

    assert rc == 0
    assert "Wallets indexed:   12" in out
    assert "Tokens tracked:    40" in out
    assert "Buy events seen:   88" in out
    assert "Sniper wallets:    3" in out
    assert "Conviction wallets:  1" in out


def test_radar_stats_warns_on_zero_wallets_indexed(tmp_path, capsys):
    db_path = str(tmp_path / "bot.db")
    acc = Accounting(db_path)
    acc.record_radar_snapshot(
        {"wallets_indexed": 0, "tokens_tracked": 0, "total_buy_events": 0, "total_sell_events": 0,
         "sniper_count": 0, "conviction_count": 0, "unfollowed_count": 0, "watch_list_size": 0}
    )
    acc.close()

    cfg = Config(db_path=db_path)
    _cmd_radar_stats(cfg)
    out = capsys.readouterr().out

    assert "HELIUS_WS_URL" in out
