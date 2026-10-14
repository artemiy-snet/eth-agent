"""Сторінка статусу для браузера на тому ж телефоні: http://127.0.0.1:8765
Лише localhost — ззовні недоступна. Кнопки STOP і STOP FLATTEN створюють відповідні файли."""

from __future__ import annotations

import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15">
<title>ETH-Агент</title><style>
:root{{--bg:#0f1115;--fg:#e8e8e8;--mut:#9aa0a6;--card:#181b21;--ok:#3fb950;--bad:#f85149;--warn:#d29922}}
@media (prefers-color-scheme: light){{:root{{--bg:#f6f7f9;--fg:#15171a;--mut:#5f6368;--card:#fff}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}}
h1{{font-size:20px;margin:0 0 4px}} .mut{{color:var(--mut)}} .card{{background:var(--card);border-radius:12px;padding:12px;margin:12px 0;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}} td,th{{padding:4px 6px;text-align:left;border-bottom:1px solid #8883}}
.big{{font-size:26px;font-weight:600}} .ok{{color:var(--ok)}} .bad{{color:var(--bad)}} .warn{{color:var(--warn)}}
form{{display:inline}} button{{font-size:15px;padding:10px 14px;border-radius:10px;border:0;margin:4px 4px 0 0}}
.stop{{background:var(--bad);color:#fff}} .flat{{background:#6e1b17;color:#fff}}
</style></head><body>
<h1>ETH-Агент · {mode}</h1><div class="mut">{when} · оновлення кожні 15 с</div>
<div class="card"><div class="big">{price} USDT</div>
<div>Капітал агента: <b>{equity}</b> із {start_equity} USDT · зв'язок: {conn}</div>
<div>Стан: {killed}</div><div>Входи: {gate}</div></div>
<div class="card"><b>Боти</b><table><tr><th>#</th><th>Фаза</th><th>USDT</th><th>ETH</th><th>Вхід</th><th>Стоп</th><th>Рівень</th></tr>{bots}</table></div>
<div class="card"><b>Ознаки</b><div class="mut">ATR {atr} ({atr_pct} %) · нахил EMA200 {ema_slope_pct} % · максимум {anchor_high}</div></div>
<div class="card"><b>Останні події</b><table>{events}</table></div>
<div class="card"><b>Останні угоди</b><table>{trades}</table></div>
<div class="card"><form method="post" action="/stop"><button class="stop">STOP — заборонити входи</button></form>
<form method="post" action="/flatten" onsubmit="return confirm('Продати всі позиції за ринком?')"><button class="flat">STOP FLATTEN — продати все</button></form>
<div class="mut">Зняти STOP: у Termux <code>eth-agent live --clear-kill</code></div></div>
</body></html>"""


def render(agent) -> str:
    s = agent.last_status or {}
    e = html.escape
    bots = "".join(
        f"<tr><td>{b['id']}</td><td>{e(b['phase'])}{' ⚡' if b['armed'] else ''}</td><td>{b['usdt']}</td>"
        f"<td>{b['eth']}</td><td>{b['entry']}</td><td>{b['stop']}</td><td>{b['level']}</td></tr>"
        for b in s.get("bots", []))
    evs = "".join(f"<tr><td class='mut'>{time.strftime('%d.%m %H:%M', time.localtime(ts / 1000))}</td>"
                  f"<td class='{'bad' if lvl == 'ERROR' else 'warn' if lvl == 'WARN' else ''}'>{e(msg)}</td></tr>"
                  for ts, lvl, _, msg in agent.j.recent_events(15))
    trs = "".join(f"<tr><td>бот {t[0]}</td><td>{e(t[7])}</td><td>{float(t[3]):.2f} → {float(t[4]):.2f}</td>"
                  f"<td class='{'ok' if float(t[6]) >= 0 else 'bad'}'>{float(t[6]):+.2f}</td></tr>"
                  for t in agent.j.recent_trades(10)) or "<tr><td class='mut'>ще немає</td></tr>"
    killed = (f"<span class='bad'>KILL SWITCH: {e(s.get('kill_reason', ''))}</span>" if s.get("killed")
              else "<span class='ok'>працює</span>")
    gate = s.get("entry_gate", "—")
    return PAGE.format(
        mode=e(s.get("mode", "—")), when=time.strftime("%d.%m.%Y %H:%M:%S"), price=s.get("price", "—"),
        equity=s.get("equity", "—"), start_equity=s.get("start_equity", "—"),
        conn="<span class='ok'>є</span>" if s.get("connected") else "<span class='bad'>немає</span>",
        killed=killed, gate=f"<span class='{'ok' if gate == 'відкрито' else 'warn'}'>{e(gate)}</span>",
        bots=bots, atr=s.get("atr"), atr_pct=s.get("atr_pct"), ema_slope_pct=s.get("ema_slope_pct"),
        anchor_high=s.get("anchor_high"), events=evs, trades=trs)


def serve(agent, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            if self.path.startswith("/api/status"):
                self._send(200, json.dumps(agent.last_status, ensure_ascii=False), "application/json")
            else:
                self._send(200, render(agent))

        def do_POST(self):
            name = {"/stop": "STOP", "/flatten": "STOP_FLATTEN"}.get(self.path)
            if name:
                (agent.home / name).write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
