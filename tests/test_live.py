"""Живий агент на фейковій біржі: подвійний сигнал, стоп на біржі, вихід, STOP, відновлення стану."""

from decimal import Decimal

import pytest

from agent.data.exchange_info import ETHUSDT_DEFAULT
from agent.live import runner
from agent.live.binance import BinanceError, NetworkError
from agent.live.broker import ExchangeBroker, PaperBroker
from agent.live.journal import Journal, load_state
from agent.live.notify import Notifier
from agent.live.settings import LiveSettings, feature_params, risk_params, v3_params
from agent.money import D
from agent.strategy import v3
from agent.strategy.types import Intent, Side

ATR = D(10)


class FakeClient:
    def __init__(self):
        self.t = 1_700_000_000_000
        self.bid, self.ask = D("999.9"), D("1000.1")
        self.orders: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.fail_new = 0          # скільки наступних new_order «губляться» в мережі (ордер на біржі створено!)
        self.fail_get = 0
        self.eth, self.usdt = D(100), D(100000)

    def server_now_ms(self):
        return self.t

    def book_ticker(self, symbol):
        return self.bid, self.ask

    def klines(self, *a, **k):
        return []

    def new_order(self, **p):
        self.sent.append(p)
        coid = p["newClientOrderId"]
        px = self.ask if p["side"] == "BUY" else self.bid
        if p["type"] == "MARKET":
            qty = (D(p["quoteOrderQty"]) / px).quantize(D("0.0001")) if "quoteOrderQty" in p else D(p["quantity"])
            o = {"clientOrderId": coid, "status": "FILLED", "executedQty": str(qty),
                 "cummulativeQuoteQty": str(qty * px),
                 "fills": [{"price": str(px), "qty": str(qty), "commission": str(qty * D("0.001")),
                            "commissionAsset": "ETH" if p["side"] == "BUY" else "USDT"}]}
            if p["side"] == "SELL":
                o["fills"][0]["commission"] = str(qty * px * D("0.001"))
        else:
            o = {"clientOrderId": coid, "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0",
                 "type": p["type"], "stopPrice": p.get("stopPrice"), "quantity": p.get("quantity")}
        self.orders[coid] = o
        if self.fail_new:
            self.fail_new -= 1
            raise NetworkError("timeout")
        return o

    def get_order(self, symbol, coid):
        if self.fail_get:
            self.fail_get -= 1
            raise NetworkError("timeout")
        if coid not in self.orders:
            raise BinanceError(400, -2013, "Order does not exist.")
        return self.orders[coid]

    def cancel_order(self, symbol, coid):
        o = self.orders.get(coid)
        if not o or o["status"] != "NEW":
            raise BinanceError(400, -2011, "Unknown order sent.")
        o["status"] = "CANCELED"
        return o

    def trigger_stop(self, coid, px):
        o = self.orders[coid]
        q = D(o["quantity"])
        o.update(status="FILLED", executedQty=str(q), cummulativeQuoteQty=str(q * px),
                 fills=[{"price": str(px), "qty": str(q), "commission": str(q * px * D("0.001")),
                         "commissionAsset": "USDT"}])

    def account(self):
        return {"balances": [{"asset": "ETH", "free": str(self.eth), "locked": "0"},
                             {"asset": "USDT", "free": str(self.usdt), "locked": "0"}]}


def settings(mode="testnet", **v3kw):
    raw = {"bots": 1, "risk_per_trade_pct": "100", "filters": {"trend_block_pct": None, "atr_min_pct": None,
                                                               "atr_max_pct": None}}
    raw.update(v3kw)
    return LiveSettings(pair="ETHUSDT", mode=mode, v3=v3_params(raw), risk=risk_params({"max_open_risk_pct": 100,
                        "max_eth_share_pct": 100, "daily_loss_pct": 100, "max_dd_pct": 100}),
                        features=feature_params(None))


def make(tmp_path, mode="testnet", c=None):
    c = c or FakeClient()
    s = settings(mode)
    broker = ExchangeBroker(c, "ETHUSDT", ETHUSDT_DEFAULT, mode, sleep=lambda *_: None) if mode != "paper" \
        else PaperBroker(D("0.1"), ETHUSDT_DEFAULT)
    a = runner.Agent(s, c, broker, tmp_path, Journal(tmp_path / "j.sqlite"), Notifier(None, None), ETHUSDT_DEFAULT)
    a.next_features = 10**15                     # ознаки задаємо вручну
    a.next_reconcile = 10**15
    a.m.feat, a.m.feat_ts = v3.Features(atr=ATR, anchor_high=D(1000)), c.t
    return a, c


def step(a, c, price, dt=60_000):
    c.t += dt
    c.bid, c.ask = D(price) - D("0.1"), D(price) + D("0.1")
    a.m.feat_ts = c.t
    a.tick()


def buy_path(a, c):
    for px in (989, 980, 992):          # зведення ≤ 990, мін 980 → вікно [991; 994]
        step(a, c, px)


def test_buy_then_exchange_stop_placed(tmp_path):
    a, c = make(tmp_path)
    buy_path(a, c)
    b = a.state.bots[0]
    assert b.phase is v3.Phase.LONG
    assert [o["type"] for o in c.sent] == ["MARKET", "STOP_LOSS_LIMIT"]
    assert "quoteOrderQty" in c.sent[0]                               # купівля на суму, не на кількість
    assert D(c.sent[1]["stopPrice"]) == D("972.00")                   # 992 − 2×ATR
    # комісія в ETH: бот тримає нетто
    assert b.eth == D(c.orders[c.sent[0]["newClientOrderId"]]["executedQty"]) * D("0.999")


def test_double_signal_during_unknown_does_not_send_second_order(tmp_path):
    a, c = make(tmp_path)
    c.fail_new, c.fail_get = 1, 10          # відповідь загубилась, статус теж недоступний
    buy_path(a, c)
    assert a.state.bots[0].phase is v3.Phase.PENDING_BUY
    for px in (992, "992.5", 993):           # сигнал «купити» триває
        step(a, c, px)
    assert sum(1 for o in c.sent if o["type"] == "MARKET") == 1       # жодного другого ордера
    c.fail_get = 0
    step(a, c, 993)                                                   # статус з'ясовано → FILLED
    assert a.state.bots[0].phase is v3.Phase.LONG
    assert sum(1 for o in c.sent if o["type"] == "MARKET") == 1


def test_unknown_order_that_never_reached_exchange_is_rejected(tmp_path):
    a, c = make(tmp_path)
    orig = c.new_order

    def lost(**p):
        c.sent.append(p)
        raise NetworkError("timeout")          # запит не дійшов: ордера на біржі немає
    c.new_order = lost
    buy_path(a, c)
    c.new_order = orig
    assert a.state.bots[0].phase is v3.Phase.FLAT                      # -2013 → відхилено, можна пробувати знову


def test_exchange_stop_fill_closes_position(tmp_path):
    a, c = make(tmp_path)
    buy_path(a, c)
    stop_coid = c.sent[1]["newClientOrderId"]
    c.trigger_stop(stop_coid, D(972))
    step(a, c, 975, dt=31_000)
    assert a.state.bots[0].phase is v3.Phase.FLAT
    tr = a.j.recent_trades(1)[0]
    assert tr[7] == "stop_exchange" and D(tr[6]) < 0


def test_take_exit_cancels_stop_then_sells(tmp_path):
    a, c = make(tmp_path)
    buy_path(a, c)
    # E2: зведення ≥ вхід + 2 ATR; вихід при відкаті на 1,5 ATR від максимуму
    for px in (1013, 1030, 1014):
        step(a, c, px)
    stop_coid = c.sent[1]["newClientOrderId"]
    assert c.orders[stop_coid]["status"] == "CANCELED"
    assert c.sent[-1]["side"] == "SELL" and c.sent[-1]["type"] == "MARKET"
    assert a.state.bots[0].phase is v3.Phase.FLAT
    assert D(a.j.recent_trades(1)[0][6]) > 0


def test_stop_file_blocks_entries_and_survives_restart(tmp_path):
    a, c = make(tmp_path)
    (tmp_path / "STOP").write_text("x")
    buy_path(a, c)
    assert not any(o["type"] == "MARKET" for o in c.sent)
    a2, _ = make(tmp_path, c=c)
    assert a2.risk.killed
    a2.clear_kill()
    assert not a2.risk.killed and not (tmp_path / "STOP").exists()


def test_flatten_sells_everything(tmp_path):
    a, c = make(tmp_path)
    buy_path(a, c)
    (tmp_path / "STOP_FLATTEN").write_text("x")
    step(a, c, 995)
    assert a.state.bots[0].phase is v3.Phase.FLAT and a.risk.killed
    assert a.j.recent_trades(1)[0][7] == "flatten"


def test_state_persists_across_restart(tmp_path):
    a, c = make(tmp_path)
    buy_path(a, c)
    st, extra = load_state(tmp_path / "state_testnet.json")
    assert st.bots[0].phase is v3.Phase.LONG and st.bots[0].entry_px == a.state.bots[0].entry_px
    a2, _ = make(tmp_path, c=c)
    assert a2.state.bots[0].phase is v3.Phase.LONG and "0" in a2.extra["stops"]


def test_reconcile_detects_missing_eth_and_kills_entries(tmp_path):
    a, c = make(tmp_path)
    buy_path(a, c)
    c.eth = D(0)
    a.reconcile()
    assert a.balance_diff > D("0.5")
    it = Intent(c.t, 0, Side.BUY, D("0.01"), D(1000), "t", stop=D(980))
    assert a.risk.check(it, a.context(c.t, D(1000))) == "balance_diff" and a.risk.killed


def test_paper_mode_fills_at_bid_ask(tmp_path):
    a, c = make(tmp_path, mode="paper")
    buy_path(a, c)
    b = a.state.bots[0]
    assert b.phase is v3.Phase.LONG and b.entry_px == D("992.1")      # купівля за ask
    assert c.sent == []                                               # жодних запитів до біржі


def test_live_mode_is_disabled(tmp_path):
    with pytest.raises(runner.LiveDisabled):
        make(tmp_path, mode="live")


def test_status_page_renders(tmp_path):
    from agent.live.status_server import render
    a, c = make(tmp_path)
    buy_path(a, c)
    html = render(a)
    assert "ETH-Агент" in html and "LONG" in html
