"""Telegram-команди, комісія в BNB, відсотки на вільні USDT (Earn)."""

from decimal import Decimal
from pathlib import Path

from agent.data.exchange_info import ETHUSDT_DEFAULT
from agent.live.binance import BinanceError
from agent.live.broker import ExchangeBroker
from agent.live.earn import EarnManager
from agent.live.tg_bot import TelegramControl, status_text
from agent.money import D
from agent.strategy.types import Side


class J:
    def __init__(self):
        self.ev = []

    def event(self, lvl, kind, msg, **k):
        self.ev.append((lvl, kind, msg))


class N:
    def __init__(self):
        self.sent = []

    def send(self, t):
        self.sent.append(t)


# --- Telegram -----------------------------------------------------------------------------
OV = {"mode": "live", "price": "2500", "equity": "21", "start_equity": "20", "killed": False,
      "sleeves": [{"sleeve": "Cm", "bots": [{"id": 0, "phase": "LONG", "eth": "0.004", "entry": "2400", "stop": ""}]},
                  {"sleeve": "Bm", "bots": [{"id": 0, "phase": "FLAT", "eth": "0", "entry": ""}]}],
      "analyst": {"active_regime": "CAUTION", "size_mult": "0.5"}, "earn": {"parked": "8.00", "apr_pct": "3.10"},
      "bnb_fees": True}


def tc(tmp_path):
    return TelegramControl("t", "42", tmp_path, overview=lambda: OV, trades=lambda: [])


def test_status_text_shows_position_and_pnl():
    t = status_text(OV)
    assert "Cm #0" in t and "+0.40 $" in t and "+4.2 %" in t
    assert "CAUTION" in t and "Earn" in t and "BNB" in t
    assert "Bm" not in t                       # бот у USDT не показується як позиція


def test_stop_and_resume_write_control_files(tmp_path):
    c = tc(tmp_path)
    c.handle("/stop")
    assert (tmp_path / "STOP").exists()
    c.handle("/resume")
    assert (tmp_path / "CLEAR_KILL").exists()


def test_flatten_needs_confirmation_within_60s(tmp_path):
    c = tc(tmp_path)
    assert "Немає активного запиту" in c.handle("/flatten_yes", now=100)
    c.handle("/flatten", now=100)
    assert not (tmp_path / "STOP_FLATTEN").exists()
    assert "Немає активного запиту" in c.handle("/flatten_yes", now=200)      # прострочено
    c.handle("/flatten", now=300)
    c.handle("/flatten_yes@my_bot", now=320)
    assert (tmp_path / "STOP_FLATTEN").exists()


def test_foreign_chat_is_ignored(tmp_path):
    c = tc(tmp_path)
    sent = []
    c._call = lambda m, p=None, timeout=0: (
        [{"update_id": 5, "message": {"chat": {"id": 999}, "text": "/stop"}}] if m == "getUpdates" else sent.append(p))
    c.send = lambda text: sent.append(text)
    c.poll_once()
    assert not (tmp_path / "STOP").exists() and not sent and c.offset == 6


# --- BNB ----------------------------------------------------------------------------------
class BnbClient:
    def book_ticker(self, symbol):
        assert symbol == "BNBUSDT"
        return D("599"), D("601")


def test_bnb_fee_counts_as_cost_and_eth_arrives_in_full():
    b = ExchangeBroker(BnbClient(), "ETHUSDT", ETHUSDT_DEFAULT, "live", sleep=lambda *_: None)
    o = {"executedQty": "0.004", "cummulativeQuoteQty": "10",
         "fills": [{"commission": "0.0000125", "commissionAsset": "BNB"}]}
    f = b._to_fill(o, 0, Side.BUY, 1)
    assert f.qty == D("0.004")                     # ETH не урізано комісією
    assert f.fee_quote == D("0.0000125") * 600     # ≈ 0,0075 $ — 0,075 % від 10 $


# --- Earn ---------------------------------------------------------------------------------
class EarnClient:
    def __init__(self, free="100"):
        self.free, self.parked_ex = D(free), D(0)
        self.calls = []
        self.deny = False

    def account(self):
        return {"balances": [{"asset": "USDT", "free": str(self.free), "locked": "0"}]}

    def earn_products(self, asset):
        if self.deny:
            raise BinanceError(400, -1002, "not authorized")
        return [{"productId": "USDT001", "latestAnnualPercentageRate": "0.031", "canPurchase": True}]

    def earn_subscribe(self, pid, amount):
        self.calls.append(("sub", amount))
        self.free -= D(amount)
        self.parked_ex += D(amount)

    def earn_redeem(self, pid, amount):
        self.calls.append(("red", amount))
        self.free += D(amount)
        self.parked_ex -= D(amount)


def em(tmp_path, c, **k):
    return EarnManager(c, tmp_path, J(), N(), sleep=lambda *_: None, **k)


def test_sweep_parks_agent_usdt_minus_reserve(tmp_path):
    c = EarnClient(free="100")                     # на рахунку 100, з них агента 20
    m = em(tmp_path, c, reserve_pct=D(20))
    m.sweep(D(20), 0)
    assert c.calls == [("sub", "16")] and m.parked == D(16) and m.apr_pct == D("3.1")
    m.sweep(D(20), 1)                              # раніше ніж за 30 хв — нічого
    assert len(c.calls) == 1
    assert EarnManager(c, tmp_path, J(), N()).parked == D(16)       # облік переживає рестарт


def test_buy_redeems_only_what_is_missing_and_never_more_than_parked(tmp_path):
    c = EarnClient(free="4")
    m = em(tmp_path, c)
    m.parked = D(16)
    m.ensure_spot(D(10))                           # бракує 6 → повертає 6 + 1 запасу
    assert c.calls == [("red", "7")] and m.parked == D(9) and c.free >= 10
    c.free = D(0)
    m.ensure_spot(D(50))                           # просить більше, ніж поклав агент
    assert c.calls[-1] == ("red", "9") and m.parked == 0


def test_disabling_returns_everything_agent_parked(tmp_path):
    c = EarnClient(free="0")
    m = em(tmp_path, c, enabled=False)
    m.parked = D(16)
    m.sweep(D(20), 0)
    assert c.calls == [("red", "16")] and m.parked == 0


def test_permission_error_disables_once_and_trading_continues(tmp_path):
    c = EarnClient()
    c.deny = True
    m = em(tmp_path, c)
    m.sweep(D(20), 0)
    m.sweep(D(20), 10**10)
    assert m.disabled and len(m.n.sent) == 1 and not c.calls


def test_broker_balances_count_parked_usdt(tmp_path):
    class C(EarnClient):
        def account(self):
            return {"balances": [{"asset": "USDT", "free": "2", "locked": "0"},
                                 {"asset": "ETH", "free": "0.004", "locked": "0"}]}
    b = ExchangeBroker(C(), "ETHUSDT", ETHUSDT_DEFAULT, "live", sleep=lambda *_: None)
    b.earn = em(tmp_path, C())
    b.earn.parked = D(8)
    assert b.balances()["USDT"] == D(10)
    assert isinstance(b.balances()["ETH"], Decimal)
    assert Path(tmp_path).exists()


def test_outage_note_reports_gap(tmp_path):
    import time as _t
    from types import SimpleNamespace
    from agent.live.runner import outage_note
    now = int(_t.time() * 1000)
    (tmp_path / "equity_live.csv").write_text(f"{now - 7 * 3_600_000},20.0,2450\n")
    o = SimpleNamespace(home=tmp_path, s=SimpleNamespace(mode="live"), j=J(), n=N())
    outage_note(o, now)
    assert "7 год 0 хв" in o.n.sent[0]
    o2 = SimpleNamespace(home=tmp_path, s=SimpleNamespace(mode="live"), j=J(), n=N())
    outage_note(o2, now - 7 * 3_600_000 + 60_000)            # перерва 1 хв — мовчимо
    assert not o2.n.sent


def test_notifier_dedupes_same_message():
    from agent.live.notify import Notifier
    n = Notifier("t", "1")
    sent = []
    import urllib.request as ur
    orig = ur.urlopen
    ur.urlopen = lambda *a, **k: sent.append(1) or type("R", (), {"read": lambda s: b""})()
    try:
        for _ in range(3):
            n.send("⛔ KILL SWITCH: STOP_FLATTEN")
        n.send("інше")
    finally:
        ur.urlopen = orig
    assert len(sent) == 2


def test_budget_change_rebases_flat_bots_and_clears_balance_kill(tmp_path):
    from agent.live.journal import save_state
    from agent.strategy import v3
    st = v3.V3State(bots=[v3.BotV3(bot_id=0, usdt=D(10))])
    save_state(tmp_path / "state_live_Bm.json", st, {"killed": True, "kill_reason": "balance_diff 9.97%"})

    class A:
        pass
    from agent.live.runner import Agent
    a = A()
    a.p = type("P", (), {"budget_usdt": D("8.5")})()
    a.state, a.extra, a.j, a.name = st, {"killed": True, "kill_reason": "balance_diff 9.97%"}, J(), "Bm"
    a.tag = lambda: "[Bm] "
    Agent.rebase_budget(a)
    assert st.bots[0].usdt == D("8.5") and a.extra["killed"] is False and a.extra["budget"] == "8.5"
    Agent.rebase_budget(a)                         # повторно — нічого не міняє
    assert len(a.j.ev) == 1
