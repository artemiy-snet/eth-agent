"""Рукав «ШІ-трейдер»: угоди лише за умовами стратегій, три перевірки, виконання й автовимкнення."""

import json

from agent.analyst import news, shadow
from agent.money import D
from agent.strategy import ai_trader as at
from agent.strategy import v3
from agent.strategy.types import Fill, Side, Tick

P = at.AiParams(budget_usdt=D(100), risk_per_trade_pct=D(100))
ATR = D(40)          # 2 % від 2000
DIP = v3.Features(atr=ATR, atr_pct=D(2), anchor_high=D(2100), ema_slope_pct=D(0), ema_dist_pct=D(-5))
TREND = v3.Features(atr=ATR, atr_pct=D(2), anchor_high=D(2000), ema_slope_pct=D(0), ema_dist_pct=D(8))


def sig(**kw):
    return {"action": "BUY", "setup": "dip_bounce", "size_pct": 50, "stop_pct": 5, "take_pct": 10,
            "price": "2000", "expires": 10**12, "id": "x", **kw}


def test_setup_must_hold_now():
    assert at.check_signal(sig(), D(2000), DIP, P, 0) is None
    assert "умова стратегії" in at.check_signal(sig(), D(2000), TREND, P, 0)          # немає падіння ≥ 1 ATR
    assert at.check_signal(sig(setup="regime_trend"), D(2000), TREND, P, 0) is None
    assert "режим" in at.check_signal(sig(setup="regime_trend"), D(2000), DIP, P, 0)
    assert "невідома" in at.check_signal(sig(setup="gut_feeling"), D(2000), DIP, P, 0)


def test_stop_in_atr_range_and_drift_and_expiry():
    assert "стоп" in at.check_signal(sig(stop_pct=1), D(2000), DIP, P, 0)          # < 1,5 ATR = 3 %
    assert "стоп" in at.check_signal(sig(stop_pct=8), D(2000), DIP, P, 0)          # > 3 ATR = 6 %
    assert "відійшла" in at.check_signal(sig(), D(2030), DIP, P, 0)
    assert "прострочено" in at.check_signal(sig(expires=5), D(2000), DIP, P, 10)
    assert "причина" in at.check_signal({"action": "SELL", "reason": "мені здається", "price": "2000",
                                         "expires": 10**12}, D(2000), DIP, P, 0)


def test_buy_take_and_sell_flow():
    s = at.init_state(P)
    s, it, bl = at.step(s, Tick(1, D(2000)), v3.Features(**{**DIP.__dict__, "ai_signal": sig()}), P)
    assert len(it) == 1 and it[0].side is Side.BUY and it[0].stop == D(1900) and bl == []
    assert it[0].qty * D(2000) <= D(50) * D("1.001")                                 # 50 % бюджету
    at.on_fill(s, Fill(1, 0, Side.BUY, it[0].qty, D(2000), D(0)))
    assert s.bots[0].sell_max == D(2200)                                              # тейк зберігся після on_fill
    s, it, _ = at.step(s, Tick(2, D(2100)), DIP, P)
    assert it == []
    s, it, _ = at.step(s, Tick(3, D(2201)), DIP, P)
    assert it[0].side is Side.SELL and it[0].reason == "ai_take"


def test_no_signal_no_trade_and_blocked_reason():
    s = at.init_state(P)
    s, it, bl = at.step(s, Tick(1, D(2000)), DIP, P)
    assert it == [] and bl == []
    s, it, bl = at.step(s, Tick(1, D(2000)), v3.Features(**{**TREND.__dict__, "ai_signal": sig()}), P)
    assert it == [] and "умова стратегії" in bl[0][1]


class J:
    def __init__(self):
        self.ev = []

    def event(self, level, kind, msg, **data):
        self.ev.append((level, kind, msg, data))


REP = {"news_regime": "NORMAL", "confidence": 0.7, "facts": ["ETH −4 % від максимуму"], "events_ahead": [],
       "positions_review": "D FLAT", "anomalies": [], "impact_on_strategy": "-", "valid_until": "2030-01-01T00:00Z",
       "trade_proposal": {"action": "BUY", "setup": "dip_bounce", "size_pct": 50, "stop_pct": 5, "take_pct": None,
                          "thesis": "падіння 1 ATR, новини нейтральні", "invalidation": "нижче 1900", "confidence": 0.7}}


def analyst(tmp_path, monkeypatch, precheck=lambda s: None):
    monkeypatch.setattr(news, "fetch", lambda feeds, since: ([], []))
    j = J()
    a = shadow.ShadowAnalyst(api_key="k", model="m", journal=j, home=tmp_path, mode="auto", trade=True,
                             precheck=precheck)
    return a, j


def calls(*answers):
    seq = list(answers)
    seen = []

    def complete(key, model, system, user):
        seen.append(system)
        return json.dumps(seq.pop(0))
    return complete, seen


BOOK = {"ai_sleeve": {"phase": "FLAT"}}


def test_approved_by_code_and_critic_creates_signal(tmp_path, monkeypatch):
    a, j = analyst(tmp_path, monkeypatch)
    c, seen = calls(REP, {"approve": True, "reasons": ["факти підтверджені"]})
    rec = a.assess(10_000, {"price": "2000"}, complete=c, book=BOOK)
    assert rec["trade"]["approved"] and "ШІ-трейдер" in seen[0] and "контролер ризику" in seen[1]
    s = a.pop_signal(10_000)
    assert s["setup"] == "dip_bounce" and s["price"] == "2000" and s["expires"] == 10_000 + 30 * 60_000
    assert a.pop_signal(10_000) is None                                    # одноразовий


def test_critic_rejects(tmp_path, monkeypatch):
    a, j = analyst(tmp_path, monkeypatch)
    c, seen = calls(REP, {"approve": False, "reasons": ["теза не підтверджена"]})
    rec = a.assess(0, {"price": "2000"}, complete=c, book=BOOK)
    assert not rec["trade"]["approved"] and rec["trade"]["stage"] == "критик" and a.signal is None


def test_code_rejects_before_critic(tmp_path, monkeypatch):
    a, j = analyst(tmp_path, monkeypatch, precheck=lambda s: "умова стратегії не виконується")
    c, seen = calls(REP)
    rec = a.assess(0, {"price": "2000"}, complete=c, book=BOOK)
    assert rec["trade"]["stage"] == "код" and len(seen) == 1                # критика навіть не питали


def test_buy_forbidden_when_regime_not_normal_and_low_confidence(tmp_path, monkeypatch):
    a, _ = analyst(tmp_path, monkeypatch)
    c, _ = calls({**REP, "news_regime": "CAUTION"})
    assert "заборонена" in a.assess(0, {"price": "2000"}, complete=c, book=BOOK)["trade"]["why"]
    low = {**REP, "trade_proposal": {**REP["trade_proposal"], "confidence": 0.4}}
    c, _ = calls(low)
    assert "впевненість" in a.assess(0, {"price": "2000"}, complete=c, book=BOOK)["trade"]["why"]


def test_no_trading_without_ai_sleeve_or_when_disabled(tmp_path, monkeypatch):
    a, _ = analyst(tmp_path, monkeypatch)
    c, seen = calls(REP)
    rec = a.assess(0, {"price": "2000"}, complete=c, book={})
    assert "ШІ-трейдер" not in seen[0] and "trade" not in rec
    a.trading_disabled = "PF < 1"
    c, seen = calls(REP)
    assert "trade" not in a.assess(0, {"price": "2000"}, complete=c, book=BOOK)


def test_auto_disable_after_bad_results():
    from agent.live import runner

    class A:
        extra = {"ai_pnls": ["-2"] * 6 + ["1"] * 4}
    an = type("An", (), {"trading_disabled": ""})()
    j, n = J(), type("N", (), {"send": lambda s, t: None})()
    runner.ai_gate(an, A(), j, n)
    assert "PF" in an.trading_disabled and A.extra["ai_disabled"]
