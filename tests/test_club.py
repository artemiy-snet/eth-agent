"""Клуб ETH-Агента: сервер (site/club.py) і клієнт агента (agent/live/club.py), без мережі."""

import importlib.util
import io
import json
from datetime import date, timedelta
from pathlib import Path

from agent.live import club as cl

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("site_club", ROOT / "site/club.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


def metric(day, idx, ch=0.5, **kw):
    return {"date": day, "equity_change_pct": ch, "equity_index": idx, "trades": 1, "win_rate": 0.5,
            "max_drawdown_pct": -1.0, "strategy_version": "0.9.1", "settings_hash": "abcd1234", "pair": "ETHUSDT",
            "mode": "paper"} | kw


def test_register_submit_rank_leave(tmp_path):
    c = sc.Club(tmp_path, {})
    assert c.register({"nick": "Admin"}, "ip")[0] == 400
    code, r = c.register({"nick": "alpha"}, "ip")
    assert code == 200 and r["token"].startswith("club_")
    assert c.register({"nick": "alpha"}, "ip2")[0] == 409
    assert r["token"] not in (tmp_path / "club/bots.json").read_text()       # лише хеш
    auth = "Bearer " + r["token"]
    assert c.submit("Bearer club_wrong", metric("2026-10-01", 100))[0] == 401
    assert c.submit(auth, metric(date.today().isoformat(), 100, balance_usd=5))[0] == 400   # суми в $ не приймаються
    today = date.today()
    for i in range(20):
        assert c.submit(auth, metric((today - timedelta(days=19 - i)).isoformat(), 100 + i))[0] == 200
    m = c.members(today)[0]
    assert m["days"] == 20 and m["ret_7"] > 0 and m["dd_30"] == 0
    s = c.summary()
    assert s["bots"] == 1 and s["top"][0]["nick"] == "alpha" and s["versions"][0]["median_return_30d_pct"] is None
    assert c.leave(auth, {})[0] == 200
    assert c.members(today) == [] and c.submit(auth, metric(today.isoformat(), 120))[0] == 401


def test_register_limit_and_closed(tmp_path):
    c = sc.Club(tmp_path, {})
    assert [c.register({"nick": f"bot{i}"}, "ip", now=1000)[0] for i in range(6)] == [200] * 5 + [429]
    assert sc.Club(tmp_path, {"CLUB_OPEN": "0"}).register({"nick": "zzz"}, "x")[0] == 403
    assert sc.Club(tmp_path, {"CLUB_INVITE": "k"}).register({"nick": "zzz"}, "x")[0] == 403


def test_day_metrics_and_hash():
    t0 = 1_760_000_000_000 - 1_760_000_000_000 % 86_400_000
    rows = [[t0 + i * 3_600_000, 100 + i - (5 if i == 30 else 0), 2000 + i, 100] for i in range(48)]
    day = cl.day_of(t0 + 86_400_000)
    m = cl.day_metrics(rows, [{"exit_ts": t0 + 86_400_000 + 5, "pnl": 1.0}, {"exit_ts": t0, "pnl": -1.0}], day)
    assert m["trades"] == 1 and m["win_rate"] == 1 and m["equity_change_pct"] > 0 and m["max_drawdown_pct"] < 0
    assert sc.validate(m | {"strategy_version": "1", "settings_hash": "abcd1234", "pair": "ETHUSDT", "mode": "live",
                            "date": date.today().isoformat()}) is None
    assert cl.day_metrics(rows, [], "2000-01-01") is None
    assert len(cl.settings_hash(())) == 8


def test_register_writes_token_only_to_secrets(tmp_path, capsys):
    (tmp_path / "secrets.env").write_text("SITE_URL=x\nCLUB_TOKEN=old\n")

    def opener(req, timeout):
        assert json.loads(req.data) == {"nick": "alpha"}
        return io.BytesIO(json.dumps({"ok": True, "token": "club_new", "page": "/u-alpha.html"}).encode())
    page = cl.register(tmp_path, "alpha", "https://x.test", opener=opener)
    assert page == "https://x.test/u-alpha.html"
    assert (tmp_path / "secrets.env").read_text() == "SITE_URL=x\nCLUB_TOKEN=club_new\nCLUB_NICK=alpha\n"
    assert "club_new" not in capsys.readouterr().out


class FakeJ:
    def __init__(self):
        self.ev = []

    def event(self, *a):
        self.ev.append(a)


def test_join_leave_without_restart(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agent.live import tg_bot
    (tmp_path / "secrets.env").write_text("BINANCE_API_KEY=k\n")
    owner = SimpleNamespace(home=tmp_path, j=FakeJ(), club_url="https://x.test", club_token="", club_nick="")

    def fake_register(home, nick, url):
        cl._write_secrets(home, {"CLUB_TOKEN": "club_t", "CLUB_NICK": nick})
        return url + f"/u-{nick}.html"
    monkeypatch.setattr(cl, "register", fake_register)
    assert "Приєднатися" in tg_bot.club_text(owner, "")
    assert "kyiv_bot" in tg_bot.club_text(owner, "Kyiv_Bot")
    assert owner.club_token == "club_t" and cl.status(owner)["joined"]
    sent = []
    cl.leave(owner, opener=lambda req, timeout: sent.append(req.full_url) or __import__("io").BytesIO(b"{}"))
    assert sent == ["https://x.test/api/club/leave"] and not cl.status(owner)["joined"]
    assert (tmp_path / "secrets.env").read_text() == "BINANCE_API_KEY=k\n"
