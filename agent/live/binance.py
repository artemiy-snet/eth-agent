"""Мінімальний REST-клієнт Binance Spot на стандартній бібліотеці (urllib + hmac).

Виправлення вад v2.02 (Біблія, розділ 6): Decimal без залежності від локалі, синхронізація часу
з /api/v3/time і recvWindow 5000, newClientOrderId для ідемпотентності, помилки не ковтаються.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_for_binance(host, *args, **kwargs):
    """Ключ Binance прив'язаний до IPv4-адреси — запити до Binance завжди через IPv4,
    інакше на мережі з IPv6 біржа побачить «чужу» адресу (помилка -2015)."""
    res = _orig_getaddrinfo(host, *args, **kwargs)
    if isinstance(host, str) and host.endswith(("binance.com", "binance.vision")):
        v4 = [r for r in res if r[0] == socket.AF_INET]
        return v4 or res
    return res


socket.getaddrinfo = _ipv4_for_binance

BASES = {
    "paper": "https://data-api.binance.vision",      # лише публічні ринкові дані
    "testnet": "https://testnet.binance.vision",
    "live": "https://api.binance.com",
}


class BinanceError(Exception):
    def __init__(self, http_status: int, code: int | None, msg: str):
        hint = (" → ключ не прийнято: перевір IP у ключі (адреса цього пристрою: curl -4 ifconfig.me), "
                "права спотової торгівлі і чи ключ від того середовища (live/testnet)") if code == -2015 else ""
        super().__init__(f"HTTP {http_status} code={code}: {msg}{hint}")
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
        """Запит з захистом від бану: на 429/418 пауза за Retry-After; на -1021 (годинник) — ресинхронізація й повтор."""
        if time.time() < getattr(self, "blocked_until", 0):
            raise NetworkError(f"пауза після ліміту запитів Binance ще {int(self.blocked_until - time.time())} с")
        try:
            return self._request_once(method, path, params, signed)
        except BinanceError as e:
            if e.http_status in (418, 429):
                self.blocked_until = time.time() + max(60, int(getattr(e, "retry_after", 0) or 0))
                raise NetworkError(f"ліміт запитів Binance (HTTP {e.http_status}) — пауза") from None
            if e.code == -1021 and signed and not getattr(self, "_resyncing", False):
                self._resyncing = True
                try:
                    self.sync_time()
                    return self._request_once(method, path, params, signed)
                finally:
                    self._resyncing = False
            raise

    def _request_once(self, method: str, path: str, params: dict | None = None, signed: bool = False):
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
            except ValueError:
                j = None
            err = BinanceError(e.code, j.get("code") if isinstance(j, dict) else None,
                               j.get("msg", body) if isinstance(j, dict) else body[:200])
            err.retry_after = (e.headers or {}).get("Retry-After") if hasattr(e, "headers") else None
            raise err from None
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
