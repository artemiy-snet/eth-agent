"""Мінімальний REST-клієнт Binance Spot на стандартній бібліотеці (urllib + hmac).

Виправлення вад v2.02 (Біблія, розділ 6): Decimal без залежності від локалі, синхронізація часу
з /api/v3/time і recvWindow 5000, newClientOrderId для ідемпотентності, помилки не ковтаються.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

BASES = {
    "paper": "https://data-api.binance.vision",      # лише публічні ринкові дані
    "testnet": "https://testnet.binance.vision",
    "live": "https://api.binance.com",
}


class BinanceError(Exception):
    def __init__(self, http_status: int, code: int | None, msg: str):
        super().__init__(f"HTTP {http_status} code={code}: {msg}")
        self.http_status, self.code, self.msg = http_status, code, msg


class NetworkError(Exception):
    """Немає відповіді (таймаут, обрив). Для ордера означає стан UNKNOWN — перевіряти статусом."""


def fmt(x: Decimal) -> str:
    s = format(x.normalize(), "f")
    return s


class Client:
    def __init__(self, base: str, api_key: str = "", api_secret: str = "", timeout: float = 10.0):
        self.base, self.key, self.secret, self.timeout = base.rstrip("/"), api_key, api_secret, timeout
        self.time_offset_ms = 0

    # --- транспорт -----------------------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"User-Agent": "eth-agent/0.1"}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            params["recvWindow"] = 5000
            q = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            q += "&signature=" + sig
            headers["X-MBX-APIKEY"] = self.key
        else:
            q = urllib.parse.urlencode(params)
        url = f"{self.base}{path}"
        data = None
        if method == "GET" or method == "DELETE":
            url += ("?" + q) if q else ""
        else:
            data = q.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                j = json.loads(body)
                raise BinanceError(e.code, j.get("code"), j.get("msg", body)) from None
            except ValueError:
                raise BinanceError(e.code, None, body[:200]) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise NetworkError(str(e)) from None

    # --- публічне ------------------------------------------------------------------------
    def sync_time(self) -> int:
        t0 = time.time() * 1000
        srv = self._request("GET", "/api/v3/time")["serverTime"]
        t1 = time.time() * 1000
        self.time_offset_ms = int(srv - (t0 + t1) / 2)
        return self.time_offset_ms

    def server_now_ms(self) -> int:
        return int(time.time() * 1000) + self.time_offset_ms

    def book_ticker(self, symbol: str) -> tuple[Decimal, Decimal]:
        j = self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})
        return Decimal(j["bidPrice"]), Decimal(j["askPrice"])

    def klines(self, symbol: str, interval: str, limit: int = 1000, end_time: int | None = None):
        rows = self._request("GET", "/api/v3/klines",
                             {"symbol": symbol, "interval": interval, "limit": limit, "endTime": end_time})
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]

    def exchange_info(self, symbol: str) -> dict:
        return self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})

    # --- підписане -----------------------------------------------------------------------
    def account(self) -> dict:
        return self._request("GET", "/api/v3/account", signed=True)

    def new_order(self, **params) -> dict:
        params.setdefault("newOrderRespType", "FULL")
        return self._request("POST", "/api/v3/order", params, signed=True)

    def get_order(self, symbol: str, client_order_id: str) -> dict:
        return self._request("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id},
                             signed=True)

    def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        return self._request("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id},
                             signed=True)


def fill_from_response(o: dict) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(виконана кількість брутто, середня ціна з УСІХ fills, комісія в USDT, комісія в ETH)."""
    qty = Decimal(o.get("executedQty", "0"))
    quote = Decimal(o.get("cummulativeQuoteQty", "0"))
    avg = quote / qty if qty > 0 else Decimal(0)
    fee_usdt, fee_eth = Decimal(0), Decimal(0)
    for f in o.get("fills", []) or []:
        c, asset = Decimal(f.get("commission", "0")), f.get("commissionAsset", "")
        if asset == "USDT":
            fee_usdt += c
        elif asset == "ETH":
            fee_eth += c
        # BNB тощо — комісія поза парою; розбіжність покаже звірка балансів
    return qty, avg, fee_usdt, fee_eth
