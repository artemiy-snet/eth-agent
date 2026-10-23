"""Друга пара (SOL): автоматичне додавання рукава Cs, окремий брокер, звірка по монетах."""

import yaml

from agent.data.exchange_info import default_filters
from agent.live import runner
from agent.live.broker import ExchangeBroker
from agent.live.journal import Journal
from agent.live.notify import Notifier
from agent.live.settings import LiveSettings, build_sleeves, sol_auto
from agent.money import D
from agent.strategy import trend, v3

from tests.test_live import FakeClient, settings


def test_sol_auto_splits_regime_sleeve_only_from_threshold():
    assert sol_auto({"Bm": 0.5, "Cm": 0.5}, None, D(17)) == {"Bm": 0.5, "Cm": 0.5}       # live-міні 17 $ — без SOL
    assert sol_auto({"Bm": 0.5, "Cm": 0.5}, None, D(100)) == {"Bm": 0.5, "Cm": 0.25, "Cs": 0.25}
    assert sol_auto({"B1": 0.6, "C": 0.3, "D": 0.1}, None, D(250)) == {"B1": 0.6, "C": 0.15, "D": 0.1, "Cs": 0.15}
    assert sol_auto({"Bm": 0.5, "Cm": 0.5}, {"auto": False}, D(500)) == {"Bm": 0.5, "Cm": 0.5}
    assert sol_auto({"B": 1.0}, None, D(500)) == {"B": 1.0}                                 # немає C — нічого


def test_live_cap_17_keeps_live_mini():
    raw = yaml.safe_load(open("config.portfolio.yaml", encoding="utf-8"))
    raw["portfolio"] = {"capital_usdt": 17, "preset": "live_міні"}
    sl, _ = build_sleeves(raw)
    assert [s.name for s in sl] == ["Bm", "Cm"]
    raw["portfolio"]["capital_usdt"] = 120
    sl, _ = build_sleeves(raw)
    cs = next(s for s in sl if s.name == "Cs")
    assert [s.name for s in sl] == ["Bm", "Cm", "Cs"] and cs.pair == "SOLUSDT"
    assert sum(s.params.budget_usdt * s.params.bots for s in sl) == D(120)


class TwoPairs(FakeClient):
    def __init__(self):
        super().__init__()
        self.px = {"ETHUSDT": (D("999.9"), D("1000.1")), "SOLUSDT": (D("149.99"), D("150.01"))}

    def book_ticker(self, symbol):
        return self.px[symbol]

    def new_order(self, **p):
        self.bid, self.ask = self.px[p["symbol"]]
        o = super().new_order(**p)
        if p["type"] == "MARKET" and p["side"] == "BUY":
            o["fills"][0]["commissionAsset"] = p["symbol"][:-4]
        return o


def test_portfolio_trades_sol_with_own_symbol_and_reconciles_per_coin(tmp_path):
    raw = yaml.safe_load(open("config.portfolio.yaml", encoding="utf-8"))
    raw["portfolio"] = {"capital_usdt": 100, "preset": "live_міні"}
    sleeves, cap = build_sleeves(raw)
    sleeves = tuple(s for s in sleeves if s.strategy == "trend")            # Cm + Cs
    c = TwoPairs()
    s0 = settings("testnet")
    s = LiveSettings(pair="ETHUSDT", mode="testnet", v3=s0.v3, risk=s0.risk, features=s0.features, sleeves=sleeves,
                     capital_usdt=cap)
    j = Journal(tmp_path / "j.sqlite")
    br = {p: ExchangeBroker(c, p, default_filters(p), "testnet", sleep=lambda *_: None) for p in ("ETHUSDT", "SOLUSDT")}
    agents = [runner.Agent(s, c, br[sl.pair or "ETHUSDT"], tmp_path, j, Notifier(None, None),
                           default_filters(sl.pair or "ETHUSDT"), strategy=trend, params=sl.params, risk=sl.risk,
                           features=sl.features, name=sl.name, pair=sl.pair) for sl in sleeves]
    pf = runner.Portfolio(agents)
    for a, px in zip(agents, (1000, 150)):
        a.next_features = 10**15
        a.m.feat = v3.Features(atr=D(px) * D("0.03"), atr_pct=D(3), anchor_high=D(px), ema_dist_pct=D(5))
    c.t += 60_000
    for a in agents:
        a.m.feat_ts = c.t
    pf.tick()
    cm, cs = agents
    assert cm.state.bots[0].phase is v3.Phase.LONG and cs.state.bots[0].phase is v3.Phase.LONG
    sol_orders = [o for o in c.sent if o["symbol"] == "SOLUSDT"]
    assert sol_orders and sol_orders[0]["newClientOrderId"].startswith("eats")
    assert abs(cs.state.bots[0].eth * D(150) - D(25)) < D(1)               # ≈ 25 % від 100 $
    assert "SOL" in cs.last_status["base"] and cs.last_status["pair"] == "SOLUSDT"
    assert (tmp_path / "state_testnet_Cs.json").exists()
    need = runner.demand(agents)
    assert need["SOL"] == cs.state.bots[0].eth and need["ETH"] == cm.state.bots[0].eth
    assert runner.shortage(need, {"USDT": need["USDT"], "ETH": need["ETH"], "SOL": need["SOL"]}) == 0
    assert runner.shortage(need, {"USDT": need["USDT"], "ETH": need["ETH"]}) == 100     # SOL зник з рахунку → тривога


def test_telegram_status_shows_sol_price_and_coin():
    from agent.live.tg_bot import status_text
    ov = {"mode": "live", "pair": "ETHUSDT", "price": "2500", "equity": "100", "start_equity": "100",
          "sleeves": [{"sleeve": "Cm", "pair": "ETHUSDT", "base": "ETH", "price": "2500",
                       "bots": [{"id": 0, "phase": "LONG", "eth": "0.01", "entry": "2400"}]},
                      {"sleeve": "Cs", "pair": "SOLUSDT", "base": "SOL", "price": "160",
                       "bots": [{"id": 0, "phase": "LONG", "eth": "0.15", "entry": "150"}]}]}
    t = status_text(ov)
    assert "SOL 160" in t and "0.1500 SOL по 150" in t and "+1.50 $" in t and "0.0100 ETH" in t
