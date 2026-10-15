"""Календар запланованих макроподій США (без залежностей — працює і на телефоні).

Лише ЗАПЛАНОВАНІ події, відомі заздалегідь: рішення FOMC (заява о 14:00 за Нью-Йорком) і
публікація CPI (08:30 за Нью-Йорком). Позачергові рішення ФРС (3 і 15 березня 2020) навмисно
не включено: їх не можна було знати наперед, і бектест з ними «бачив би майбутнє».
Березень 2020: стоїть запланована дата 18.03 — саме її знав ринок заздалегідь.

Джерела: federalreserve.gov/monetarypolicy/fomccalendars.htm (2021–2027; 2019–2020 — з архіву ФРС),
bls.gov/bls/news-release/cpi.htm (архів) і bls.gov/schedule/news_release/cpi.htm (розклад).
Осінь 2025: через шатдаун уряду CPI вийшов 24.10, листопадового випуску не було, наступний — 18.12.

Оновлювати раз на рік (наприкінці року з'являється розклад на наступний). Живий агент
попереджає в журналі, коли до кінця календаря лишається < 45 днів.
"""

from __future__ import annotations

import bisect
from datetime import date, datetime, timedelta, timezone

FOMC = """
2019-01-30 2019-03-20 2019-05-01 2019-06-19 2019-07-31 2019-09-18 2019-10-30 2019-12-11
2020-01-29 2020-03-18 2020-04-29 2020-06-10 2020-07-29 2020-09-16 2020-11-05 2020-12-16
2021-01-27 2021-03-17 2021-04-28 2021-06-16 2021-07-28 2021-09-22 2021-11-03 2021-12-15
2022-01-26 2022-03-16 2022-05-04 2022-06-15 2022-07-27 2022-09-21 2022-11-02 2022-12-14
2023-02-01 2023-03-22 2023-05-03 2023-06-14 2023-07-26 2023-09-20 2023-11-01 2023-12-13
2024-01-31 2024-03-20 2024-05-01 2024-06-12 2024-07-31 2024-09-18 2024-11-07 2024-12-18
2025-01-29 2025-03-19 2025-05-07 2025-06-18 2025-07-30 2025-09-17 2025-10-29 2025-12-10
2026-01-28 2026-03-18 2026-04-29 2026-06-17 2026-07-29 2026-09-16 2026-10-28 2026-12-09
2027-01-27 2027-03-17 2027-04-28 2027-06-09 2027-07-28 2027-09-15 2027-10-27 2027-12-08
"""

CPI = """
2019-01-11 2019-02-13 2019-03-12 2019-04-10 2019-05-10 2019-06-12 2019-07-11 2019-08-13 2019-09-12 2019-10-10 2019-11-13 2019-12-11
2020-01-14 2020-02-13 2020-03-11 2020-04-10 2020-05-12 2020-06-10 2020-07-14 2020-08-12 2020-09-11 2020-10-13 2020-11-12 2020-12-10
2021-01-13 2021-02-10 2021-03-10 2021-04-13 2021-05-12 2021-06-10 2021-07-13 2021-08-11 2021-09-14 2021-10-13 2021-11-10 2021-12-10
2022-01-12 2022-02-10 2022-03-10 2022-04-12 2022-05-11 2022-06-10 2022-07-13 2022-08-10 2022-09-13 2022-10-13 2022-11-10 2022-12-13
2023-01-12 2023-02-14 2023-03-14 2023-04-12 2023-05-10 2023-06-13 2023-07-12 2023-08-10 2023-09-13 2023-10-12 2023-11-14 2023-12-12
2024-01-11 2024-02-13 2024-03-12 2024-04-10 2024-05-15 2024-06-12 2024-07-11 2024-08-14 2024-09-11 2024-10-10 2024-11-13 2024-12-11
2025-01-15 2025-02-12 2025-03-12 2025-04-10 2025-05-13 2025-06-11 2025-07-15 2025-08-12 2025-09-11 2025-10-24 2025-12-18
2026-01-13 2026-02-13 2026-03-11 2026-04-10 2026-05-12 2026-06-10 2026-07-14 2026-08-12 2026-09-11 2026-11-10 2026-12-10
2027-01-13 2027-02-11 2027-03-10 2027-04-13 2027-05-12 2027-06-10 2027-07-14 2027-08-11 2027-09-14 2027-10-14 2027-11-10 2027-12-10
"""

TIMES = {"fomc": (14, 0), "cpi": (8, 30)}       # місцевий час Нью-Йорка
_RAW = {"fomc": FOMC, "cpi": CPI}


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def ny_offset_h(d: date) -> int:
    """UTC-зсув Нью-Йорка: літній час з 2-ї неділі березня до 1-ї неділі листопада (правило з 2007)."""
    return -4 if _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1) else -5


def _to_utc_ms(d: date, hh: int, mm: int) -> int:
    local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)
    return int((local - timedelta(hours=ny_offset_h(d))).timestamp() * 1000)


def events(kinds: str = "fomc,cpi") -> list[tuple[int, str]]:
    """[(utc_ms, kind)] за зростанням часу. kinds — 'fomc', 'cpi', 'fomc,cpi' або '' (вимкнено)."""
    out = []
    for k in [x.strip() for x in kinds.split(",") if x.strip()]:
        if k not in _RAW:
            raise ValueError(f"невідомий тип події {k!r}; є {sorted(_RAW)}")
        hh, mm = TIMES[k]
        out += [(_to_utc_ms(date.fromisoformat(s), hh, mm), k) for s in _RAW[k].split()]
    return sorted(out)


_CACHE: dict[str, list[int]] = {}


def event_times(kinds: str = "fomc,cpi") -> list[int]:
    if kinds not in _CACHE:
        _CACHE[kinds] = [t for t, _ in events(kinds)]
    return _CACHE[kinds]


def proximity(ts_ms: int, kinds: str = "fomc,cpi") -> tuple[float | None, float | None]:
    """(годин до наступної події, годин після останньої). None — поза календарем."""
    ts = event_times(kinds)
    i = bisect.bisect_left(ts, ts_ms)
    nxt = (ts[i] - ts_ms) / 3_600_000 if i < len(ts) else None
    prev = (ts_ms - ts[i - 1]) / 3_600_000 if i > 0 else None
    return nxt, prev


def next_event(ts_ms: int, kinds: str = "fomc,cpi") -> tuple[int, str] | None:
    for t, k in events(kinds):
        if t >= ts_ms:
            return t, k
    return None


def days_left(ts_ms: int, kinds: str = "fomc,cpi") -> float:
    ts = event_times(kinds)
    return (ts[-1] - ts_ms) / 86_400_000 if ts else 0.0


def in_window(event_in_h: float | None, event_ago_h: float | None,
              before_h: float | None, after_h: float | None) -> bool:
    """Чи момент у вікні «до події before_h год / після події after_h год»."""
    if before_h and event_in_h is not None and event_in_h <= before_h:
        return True
    if after_h and event_ago_h is not None and event_ago_h < after_h:
        return True
    return False
