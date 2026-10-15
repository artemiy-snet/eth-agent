"""Живий агент на фейковій біржі: подвійний сигнал, стоп на біржі, вихід, STOP, відновлення стану."""


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


def test_portfolio_two_sleeves_on_fake_exchange(tmp_path):
    import yaml

    from agent.live.settings import build_sleeves
    from agent.live.status_server import render

    raw = yaml.safe_load(open("config.example.yaml", encoding="utf-8"))
    raw["portfolio"] = {"capital_usdt": 500, "preset": "збалансований"}
    raw["v3"]["filters"] = {"trend_block_pct": None, "atr_min_pct": None, "atr_max_pct": None}
    sleeves, cap = build_sleeves(raw)
    c = FakeClient()
    s = settings("testnet")
    s = LiveSettings(pair=s.pair, mode="testnet", v3=s.v3, risk=s.risk, features=s.features, sleeves=sleeves,
                     capital_usdt=cap)
    broker = ExchangeBroker(c, "ETHUSDT", ETHUSDT_DEFAULT, "testnet", sleep=lambda *_: None)
    j = Journal(tmp_path / "j.sqlite")
    from agent.strategy import trend
    agents = [runner.Agent(s, c, broker, tmp_path, j, Notifier(None, None), ETHUSDT_DEFAULT,
                           strategy=trend if sl.strategy == "trend" else v3, params=sl.params, risk=sl.risk,
                           features=sl.features, name=sl.name) for sl in sleeves]
    pf = runner.Portfolio(agents)
    for a in agents:
        a.next_features = 10**15
        a.m.feat = v3.Features(atr=ATR, anchor_high=D(1000), ema_dist_pct=D(5))
    for px in (989, 980, 992):
        c.t += 60_000
        c.bid, c.ask = D(px) - D("0.1"), D(px) + D("0.1")
        for a in agents:
            a.m.feat_ts = c.t
        pf.tick()
    b1, cc = agents
    assert cc.state.bots[0].phase is v3.Phase.LONG                      # режим вгору → C купив одразу
    assert abs(cc.state.bots[0].eth * D(989) - D(150)) < D(2)           # ≈ 30 % від 500
    assert b1.state.bots[0].phase is v3.Phase.LONG                      # B1 купив на відскоку
    stops = [o for o in c.sent if o["type"] == "STOP_LOSS_LIMIT"]
    longs_b1 = sum(1 for b in b1.state.bots if b.phase is v3.Phase.LONG)
    assert len(stops) == longs_b1 and not cc.extra["stops"]             # стопи лише в B1; C — вихід за режимом
    assert (tmp_path / "state_testnet_B1.json").exists() and (tmp_path / "state_testnet_C.json").exists()
    html = render(pf)
    assert "B1 · Відскок" in html and "C · Режимне" in html


def test_ai_risk_off_blocks_only_new_entries(tmp_path):
    a, c = make(tmp_path)
    a.ai_mult = D(0)
    buy_path(a, c)
    assert c.sent == [] and a.state.bots[0].phase is v3.Phase.FLAT


def test_ai_caution_halves_entry_and_exits_untouched(tmp_path):
    a, c = make(tmp_path)
    a.ai_mult = D("0.5")
    buy_path(a, c)
    q = D(c.sent[0]["quoteOrderQty"])
    a2, c2 = make(tmp_path / "full")
    buy_path(a2, c2)
    assert abs(q * 2 - D(c2.sent[0]["quoteOrderQty"])) <= D(1)
    a.ai_mult = D(0)                                   # RISK_OFF не заважає стопу/продажу
    open(tmp_path / "STOP_FLATTEN", "w").write("x")
    step(a, c, 992)
    assert c.sent[-1]["side"] == "SELL"


def test_ai_sleeve_executes_vetted_signal_with_exchange_stop(tmp_path):
    import yaml

    from agent.live.settings import build_sleeves
    from agent.live.status_server import render
    from agent.strategy import ai_trader

    raw = yaml.safe_load(open("config.portfolio.yaml", encoding="utf-8"))
    raw["portfolio"] = {"capital_usdt": 500, "sleeves": {"D": 1.0}}
    sleeves, cap = build_sleeves(raw)
    c = FakeClient()
    s0 = settings("testnet")
    s = LiveSettings(pair=s0.pair, mode="testnet", v3=s0.v3, risk=s0.risk, features=s0.features, sleeves=sleeves,
                     capital_usdt=cap)
    broker = ExchangeBroker(c, "ETHUSDT", ETHUSDT_DEFAULT, "testnet", sleep=lambda *_: None)
    j = Journal(tmp_path / "j.sqlite")
    a = runner.Agent(s, c, broker, tmp_path, j, Notifier(None, None), ETHUSDT_DEFAULT, strategy=ai_trader,
                     params=sleeves[0].params, risk=sleeves[0].risk, features=sleeves[0].features, name="D")
    a.next_features = 10**15
    a.m.feat = v3.Features(atr=D(20), atr_pct=D(2), anchor_high=D(1030), ema_slope_pct=D(0), ema_dist_pct=D(-3))
    step(a, c, 1000)
    assert c.sent == []                                                   # без ідеї ШІ — нічого
    a.ai_signal = {"action": "BUY", "setup": "dip_bounce", "size_pct": 50, "stop_pct": 5, "take_pct": None,
                   "price": "1000.00", "expires": c.t + 10**7, "id": "ai1"}
    step(a, c, 1000)
    assert [o["type"] for o in c.sent] == ["MARKET", "STOP_LOSS_LIMIT"]
    assert D(c.sent[1]["stopPrice"]) == D("950.00")
    assert D(c.sent[0]["quoteOrderQty"]) <= D(250)                         # ≤ 50 % рукава
    step(a, c, 1000)
    assert len(c.sent) == 2 and a.ai_signal is None                        # сигнал одноразовий
    assert "сигнал ai1" in " ".join(m for _, _, _, m in j.recent_events(10))
    assert "D" in render(a)


def test_panel_api_settings_and_csrf(tmp_path):
    import json as _json
    import urllib.error
    import urllib.request
    from pathlib import Path

    from agent.live import panel_api
    from agent.live.settings import load
    from agent.live.status_server import serve

    a, c = make(tmp_path)
    step(a, c, 1000)
    srv = serve(a, 0, config_path=Path("config.portfolio.yaml"))
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        ov = _json.loads(urllib.request.urlopen(base + "/api/overview").read())
        assert ov["price"] and ov["last_tick"] and "calendar" in ov
        assert b"ETH" in urllib.request.urlopen(base + "/").read()
        st = _json.loads(urllib.request.urlopen(base + "/api/settings").read())
        assert st["values"]["portfolio.preset"] and "ANTHROPIC_API_KEY" in st["secrets"]

        def post(path, body, hdr=True):
            req = urllib.request.Request(base + path, data=_json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **({"X-ETH-Agent": "1"} if hdr else {})})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, _json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, _json.loads(e.read())
        code, _ = post("/api/stop", {}, hdr=False)                      # без заголовка — сторонній сайт
        assert code == 403 and not (tmp_path / "STOP").exists()
        code, r = post("/api/settings", {"values": {"agent.mode": "live"}})
        assert code == 400 and r["errors"]                                # live з панелі неможливий
        code, r = post("/api/settings", {"values": {"risk.max_dd_pct": 500}})
        assert code == 400
        code, r = post("/api/settings", {"values": {"portfolio.preset": "агресивний+ші", "analyst.every_h": 8},
                                         "secrets": {"ANTHROPIC_API_KEY": "dummy-value-abcd"}})
        assert code == 200 and r["ok"]
        sec = (tmp_path / "secrets.env").read_text()
        assert "ANTHROPIC_API_KEY=dummy-value-abcd" in sec and oct((tmp_path / "secrets.env").stat().st_mode)[-3:] == "600"
        st = _json.loads(urllib.request.urlopen(base + "/api/settings").read())
        assert st["secrets"]["ANTHROPIC_API_KEY"] == "…abcd" and "dummy-value" not in _json.dumps(st)
        s = load(Path("config.portfolio.yaml"), home=tmp_path)
        assert [x.name for x in s.sleeves] == ["B1", "C", "D"] and s.analyst["every_h"] == 8
        code, r = post("/api/settings", {"values": {"agent.mode": "testnet"}})
        assert code == 400                                                 # немає ключів Binance
        code, _ = post("/api/restart", {})
        assert code == 200 and runner.restart_requested(tmp_path) and not runner.restart_requested(tmp_path)
    finally:
        srv.shutdown()
        srv.server_close()
    assert panel_api.mask("abcdef") == "…cdef"


def test_live_mode_resolution(tmp_path):
    from pathlib import Path

    from agent.live.settings import live_mode, load
    assert live_mode(None, "backtest") == "paper" and live_mode(None, None) == "paper"
    assert live_mode(None, "testnet") == "testnet" and live_mode("paper", "testnet") == "paper"
    assert load(Path("config.portfolio.yaml"), home=tmp_path).mode == "paper"       # config має mode: backtest
    (tmp_path / "overrides.yaml").write_text("agent:\n  mode: testnet\n")
    assert load(Path("config.portfolio.yaml"), home=tmp_path).mode == "testnet"


def test_check_uses_unsaved_field_value(tmp_path, monkeypatch):
    from agent.live import panel_api
    seen = {}

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_open(req, timeout=0):
        seen["key"] = req.headers.get("X-api-key")
        return R()
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    assert panel_api.check("anthropic", tmp_path, "paper")["ok"] is False          # нічого не збережено
    r = panel_api.check("anthropic", tmp_path, "paper", {"ANTHROPIC_API_KEY": "field-value-1234"})
    assert r["ok"] and seen["key"] == "field-value-1234"


def test_live_requires_confirmation_keys_and_cap(tmp_path):
    from pathlib import Path

    import pytest as _pt

    from agent.live import panel_api
    from agent.live.settings import load

    # без підтвердження — заблоковано
    (tmp_path / "overrides.yaml").write_text("agent: {mode: live}\n")
    s = load(Path("config.portfolio.yaml"), home=tmp_path)
    with _pt.raises(runner.LiveDisabled):
        runner.build(s, tmp_path)
    # панель: live без слова LIVE і без ключів — відмова
    r = panel_api.save_settings(tmp_path, {"values": {"agent.mode": "live"}})
    assert not r["ok"] and any("LIVE" in e for e in r["errors"]) and any("LIVE API" in e for e in r["errors"])
    r = panel_api.save_settings(tmp_path, {"values": {"agent.mode": "live", "live.max_capital_usdt": 20},
                                           "secrets": {"BINANCE_LIVE_API_KEY": "kkkk1234", "BINANCE_LIVE_API_SECRET": "ssss5678"},
                                           "confirm_live": "live"})
    assert r["ok"]
    s = load(Path("config.portfolio.yaml"), home=tmp_path)
    assert s.mode == "live" and s.live_confirmed
    total = sum(x.params.budget_usdt * x.params.bots for x in s.sleeves)
    assert total == D(20) and [x.name for x in s.sleeves] == ["B1", "C", "D"]          # повна версія, межа 20 $
    warn = runner.sleeve_feasibility(s)
    assert any("B1" in w for w in warn) and any("D" in w for w in warn) and not any("C:" in w for w in warn)
    # повернення в paper знімає підтвердження
    panel_api.save_settings(tmp_path, {"values": {"agent.mode": "paper"}})
    assert not load(Path("config.portfolio.yaml"), home=tmp_path).live_confirmed


def test_live_mini_preset_can_trade_with_20(tmp_path):
    from pathlib import Path

    from agent.live.settings import load
    (tmp_path / "overrides.yaml").write_text("agent: {mode: live}\nlive: {confirmed: true, max_capital_usdt: 20, preset: live_міні}\n")
    s = load(Path("config.portfolio.yaml"), home=tmp_path)
    assert [x.name for x in s.sleeves] == ["Bm", "Cm"] and runner.sleeve_feasibility(s) == []


def test_binance_resolves_ipv4_only():
    import socket

    from agent.live import binance  # noqa: F401 — встановлює фільтр
    orig = binance._orig_getaddrinfo
    fake = [(socket.AF_INET6, 1, 6, "", ("::1", 443, 0, 0)), (socket.AF_INET, 1, 6, "", ("1.2.3.4", 443))]
    binance._orig_getaddrinfo = lambda *a, **k: fake
    try:
        assert [r[0] for r in socket.getaddrinfo("api.binance.com", 443)] == [socket.AF_INET]
        assert len(socket.getaddrinfo("example.org", 443)) == 2
    finally:
        binance._orig_getaddrinfo = orig
    e = binance.BinanceError(401, -2015, "Invalid API-key, IP, or permissions for action.")
    assert "IP" in str(e) and "curl -4" in str(e)


def test_client_backs_off_on_rate_limit_and_resyncs_clock(monkeypatch):
    import pytest as _pt

    from agent.live import binance as bn
    c = bn.Client("https://x", "k", "s")
    calls = []

    def once(method, path, params=None, signed=False):
        calls.append(path)
        if path == "/limited":
            e = bn.BinanceError(429, -1003, "Too many requests")
            e.retry_after = "120"
            raise e
        if path == "/clock" and len([p for p in calls if p == "/clock"]) == 1:
            raise bn.BinanceError(400, -1021, "Timestamp outside recvWindow")
        if path == "/api/v3/time":
            return {"serverTime": 0}
        return {"ok": True}
    monkeypatch.setattr(c, "_request_once", once)
    with _pt.raises(bn.NetworkError):
        c._request("GET", "/limited")
    with _pt.raises(bn.NetworkError):                      # під час паузи — навіть не стукаємо
        c._request("GET", "/anything")
    assert calls.count("/anything") == 0
    c.blocked_until = 0
    assert c._request("GET", "/clock", signed=True) == {"ok": True} and "/api/v3/time" in calls


def test_trend_without_breakout_needs_only_24h_anchor():
    import yaml

    from agent.live.settings import build_sleeves
    raw = yaml.safe_load(open("config.portfolio.yaml", encoding="utf-8"))
    sl = {s.name: s for s in build_sleeves(raw)[0]}
    assert sl["C"].features.anchor_h == 24


def test_exchange_rejection_backs_off_instead_of_spamming(tmp_path, monkeypatch):
    from agent.live.binance import BinanceError
    a, c = make(tmp_path)
    calls = []

    def reject(**p):
        calls.append(p)
        raise BinanceError(401, -2015, "Invalid API-key, IP, or permissions for action.")
    monkeypatch.setattr(c, "new_order", reject)
    buy_path(a, c)                                   # купівля → відмова
    n = len(calls)
    assert n == 1 and a.state.bots[0].phase is v3.Phase.FLAT
    for px in (989, 980, 992, 989, 980, 992):        # ще 6 хвилин сигналів — без нових ордерів
        step(a, c, px, dt=30_000)
    assert len(calls) == 1
    warns = [m for _, lvl, _, m in a.j.recent_events(50) if "ордер відхилено" in m]
    assert len(warns) == 1 and "5 хв" in warns[0]
    for px in (989, 980, 992):                       # після паузи — нова спроба
        step(a, c, px, dt=120_000)
    assert len(calls) >= 2


def test_claude_truncated_reply_is_error(monkeypatch):
    import io
    import json as _json

    import pytest as _pt

    from agent.analyst import claude as cl

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: R(_json.dumps(
        {"content": [{"type": "text", "text": '{"a": '}], "stop_reason": "max_tokens"}).encode()))
    with _pt.raises(cl.ClaudeError, match="обрізана"):
        cl.complete("k", "m", "s", "u")


def test_fresh_warnings_drops_resolved_order_errors():
    from agent.live.runner import fresh_warnings
    h = 3_600_000
    now = 10 * h
    ev = [(now - 1 * h, "INFO", "fill", "куплено"),
          (now - 2 * h, "WARN", "order", "code=-2015"),       # до fill → розв'язано
          (now - 0.5 * h, "WARN", "market", "дані застаріли"),  # свіже
          (now - 8 * h, "ERROR", "crash", "старе")]             # поза вікном
    r = fresh_warnings(ev, now)
    assert [w["kind"] for w in r["recent_warnings"]] == ["market"]
    assert r["last_fill_min_ago"] == 60
