"""Відсотки на вільні USDT агента: Binance Simple Earn, гнучкий депозит (лише live, вмикається в панелі).

Правила:
- у депозит іде лише USDT агента (сума ботів у USDT) за вирахуванням резерву reserve_pct на споті;
- перед кожною купівлею, якщо на споті бракує, потрібна сума повертається з депозиту (redeem → SPOT);
- агент веде власний облік `parked` і ніколи не забирає більше, ніж сам поклав (чужі вклади й нараховані
  відсотки не чіпає — відсотки лишаються тобі);
- вимкнули в панелі → на наступному кроці все, що поклав агент, повертається на спот;
- будь-яка відмова API (права ключа тощо) → депозит вимикається з одним сповіщенням, торгівля триває.
"""

from __future__ import annotations

import json
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

from agent.live.binance import BinanceError, NetworkError, fmt
from agent.money import HUNDRED, ZERO, D

CENT = D("0.01")


class EarnManager:
    def __init__(self, client, home: Path, journal, notifier, enabled: bool = True, reserve_pct=D(20),
                 min_move=D(1), every_s: int = 1800, sleep=time.sleep):
        self.c, self.j, self.n = client, journal, notifier
        self.enabled, self.reserve = enabled, D(reserve_pct) / HUNDRED
        self.min_move, self.every_ms, self.sleep = D(min_move), every_s * 1000, sleep
        self.path = home / "earn_live.json"
        self.parked = ZERO
        self.product_id: str | None = None
        self.apr_pct: Decimal | None = None
        self.disabled = ""
        self.next_sweep = 0
        try:
            self.parked = D(json.loads(self.path.read_text()).get("parked", "0"))
        except (OSError, ValueError):
            pass

    # --- облік ----------------------------------------------------------------------------
    def _save(self) -> None:
        self.path.write_text(json.dumps({"parked": str(self.parked), "ts": int(time.time())}))

    def status(self) -> dict:
        return {"enabled": self.enabled, "parked": f"{self.parked:.2f}",
                "apr_pct": None if self.apr_pct is None else f"{self.apr_pct:.2f}", "disabled": self.disabled}

    def _fail(self, what: str, e: Exception) -> None:
        if isinstance(e, BinanceError):
            first = not self.disabled
            self.disabled = f"{what}: {e}"[:300]
            if first:
                self.j.event("WARN", "earn", f"Earn вимкнено — {self.disabled}")
                self.n.send(f"⚠️ Earn (відсотки на USDT) вимкнено: {str(e)[:200]}. Торгівля працює як звичайно.")
        else:
            self.j.event("WARN", "earn", f"Earn: {what} — немає зв'язку ({e}), повтор пізніше")

    def _product(self) -> str | None:
        if self.product_id is None:
            rows = self.c.earn_products("USDT")
            row = next((r for r in rows if r.get("canPurchase", True)), None)
            if row is None:
                raise BinanceError(200, None, "немає гнучкого продукту USDT")
            self.product_id = str(row["productId"])
            apr = row.get("latestAnnualPercentageRate")
            self.apr_pct = D(str(apr)) * HUNDRED if apr not in (None, "") else None
        return self.product_id

    def free_usdt(self) -> Decimal:
        acc = self.c.account()
        return next((D(b["free"]) for b in acc.get("balances", []) if b["asset"] == "USDT"), ZERO)

    # --- рухи -----------------------------------------------------------------------------
    def redeem(self, amount: Decimal) -> Decimal:
        amt = min(self.parked, amount.quantize(CENT, rounding=ROUND_UP))
        if amt <= 0:
            return ZERO
        self.c.earn_redeem(self._product(), fmt(amt))
        self.parked -= amt
        self._save()
        self.j.event("INFO", "earn", f"Earn → спот: {amt} USDT (у депозиті лишилось {self.parked:.2f})")
        return amt

    def ensure_spot(self, need: Decimal) -> None:
        """Перед купівлею: якщо вільних USDT на споті бракує — повернути з депозиту й дочекатися зарахування."""
        if self.parked <= 0:
            return
        try:
            free = self.free_usdt()
            if free >= need:
                return
            self.redeem(need - free + D(1))
            for _ in range(5):
                self.sleep(1)
                if self.free_usdt() >= need:
                    return
            self.j.event("WARN", "earn", "Earn: кошти ще не надійшли на спот — купівля може бути відхилена й повториться")
        except (BinanceError, NetworkError) as e:
            self._fail("повернення перед купівлею", e)

    def sweep(self, agent_usdt: Decimal, now_ms: int) -> None:
        """Раз на 30 хв: тримати в депозиті (USDT агента − резерв); вимкнено → повернути все своє."""
        if now_ms < self.next_sweep:
            return
        self.next_sweep = now_ms + self.every_ms
        target = ZERO if (not self.enabled or self.disabled) else \
            (agent_usdt * (1 - self.reserve)).quantize(CENT, rounding=ROUND_DOWN)
        try:
            if self.enabled and not self.disabled:
                self._product()                      # оновити APR для панелі
            move = target - self.parked
            if move <= -self.min_move or (target == 0 and self.parked > 0):
                self.redeem(-move if target else self.parked)
            elif move >= self.min_move:
                amt = min(move, self.free_usdt() - D(1)).quantize(CENT, rounding=ROUND_DOWN)
                if amt >= self.min_move:
                    self.c.earn_subscribe(self._product(), fmt(amt))
                    self.parked += amt
                    self._save()
                    apr = f" під {self.apr_pct:.2f} % річних" if self.apr_pct is not None else ""
                    self.j.event("INFO", "earn", f"Спот → Earn: {amt} USDT{apr} (усього {self.parked:.2f})")
        except (BinanceError, NetworkError) as e:
            self._fail("переміщення", e)
