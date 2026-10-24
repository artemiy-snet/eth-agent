"""Гіпотези з «25 правил трейдера»: правило 21 (просадка → розмір удвічі) і 10 (2 збитки за день → пауза)."""

from agent.money import D
from agent.risk.guard import DAY, RiskGuard, RiskParams
from agent.strategy.sleeves import GROUPS, SLEEVES


def test_rule21_halves_after_drawdown_and_restores_after_recovery():
    g = RiskGuard(RiskParams(dd_half_pct=D(10), max_dd_pct=D(25)))
    g.on_equity(0, D(100))
    g.on_equity(1, D(91))
    assert g.size_mult == 1
    g.on_equity(2, D(89))                      # −11 % від піку
    assert g.size_mult == D("0.5") and not g.killed
    g.on_equity(3, D(94))                      # −6 %: ще не відновився до половини межі
    assert g.size_mult == D("0.5")
    g.on_equity(4, D(96))                      # −4 % < 5 % → повний розмір
    assert g.size_mult == 1
    g.on_equity(5, D(70))                      # −30 % → жорсткий kill switch лишається
    assert g.killed


def test_rule21_off_by_default():
    g = RiskGuard()
    g.on_equity(0, D(100))
    g.on_equity(1, D(85))
    assert g.size_mult == 1 and g.killed       # стара поведінка: kill на −10 %


def test_rule10_two_losses_pause_until_next_day():
    g = RiskGuard(RiskParams(daily_max_losses=2))
    g.on_trade_closed(DAY + 100, D(-1))
    assert g.pause_until == 0
    g.on_trade_closed(DAY + 200, D(-1))
    assert g.pause_until == 2 * DAY and "losses_today" in g.pause_reason
    g2 = RiskGuard(RiskParams(daily_max_losses=2))
    g2.on_trade_closed(DAY - 10, D(-1))
    g2.on_trade_closed(DAY + 10, D(-1))       # різні дні — паузи немає
    assert g2.pause_until == 0


def test_rules25_group_and_c_keeps_no_kill():
    assert "збалансований_п21" in GROUPS["rules25"] and "live_міні_п21_усі" in GROUPS["rules25"]
    assert "risk.max_dd_pct=25" not in SLEEVES["C_п21"]["set"] and "risk.dd_half_pct=10" in SLEEVES["C_п21"]["set"]
    assert all("_п" not in k for k in GROUPS["all"])
