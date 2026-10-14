"""Завантажувач історії 1m-свічок у кеш Parquet (Біблія, розділ 11, етап 1).

Джерела:
  1. data.binance.vision — місячні архіви (швидко, без лімітів API) для завершених місяців;
  2. REST /api/v3/klines — для поточного місяця (по 1000 свічок за запит).

Кеш: <cache>/klines/<SYMBOL>/<interval>/<YYYY-MM>.parquet, один файл на місяць.
Колонки: open_time (UTC, ms), open, high, low, close, volume, close_time, quote_volume, trades.
Ціни зберігаються рядками, щоб без втрат перетворюватись у Decimal.
"""

from __future__ import annotations

import io
import time
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

VISION_URL = "https://data.binance.vision/data/spot/monthly/klines/{s}/{i}/{s}-{i}-{m}.zip"
REST_URL = "https://api.binance.com/api/v3/klines"
COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
]
KEEP = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades"]
PRICE_COLS = ["open", "high", "low", "close", "volume", "quote_volume"]


def month_range(start: str, end: str) -> list[str]:
    """'2025-10', '2026-01' -> ['2025-10', '2025-11', '2025-12', '2026-01']."""
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df[KEEP].copy()
    for c in ("open_time", "close_time"):
        df[c] = df[c].astype("int64")
        # З 2025 року spot-архіви Binance мають мікросекунди, а не мілісекунди.
        micro = df[c] > 10**14
        df.loc[micro, c] = df.loc[micro, c] // 1000
    for c in PRICE_COLS:
        df[c] = df[c].astype(str)
    df["trades"] = df["trades"].astype("int64")
    return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def parse_vision_zip(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            df = pd.read_csv(f, header=None, dtype=str)
    if not str(df.iloc[0, 0]).isdigit():  # деякі архіви мають заголовок
        df = df.iloc[1:]
    df.columns = COLUMNS[: df.shape[1]]
    return _normalize(df)


def _fetch_rest(client, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        r = client.get(REST_URL, params={
            "symbol": symbol, "interval": interval,
            "startTime": cur, "endTime": end_ms - 1, "limit": 1000,
        })
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + 60_000
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=COLUMNS[: len(rows[0])] if rows else COLUMNS)
    return _normalize(df) if rows else df[KEEP]


def _month_bounds_ms(month: str) -> tuple[int, int]:
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def download(symbol: str, start: str, end: str, cache: Path, interval: str = "1m",
             force: bool = False, log=print) -> list[Path]:
    """Завантажує місяці [start; end] у кеш. Повертає список файлів Parquet."""
    import httpx

    out_dir = cache / "klines" / symbol / interval
    out_dir.mkdir(parents=True, exist_ok=True)
    this_month = date.today().strftime("%Y-%m")
    paths = []
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for month in month_range(start, end):
            path = out_dir / f"{month}.parquet"
            complete = month < this_month
            if path.exists() and complete and not force:
                log(f"{month}: є в кеші")
                paths.append(path)
                continue
            df = None
            if complete:
                r = client.get(VISION_URL.format(s=symbol, i=interval, m=month))
                if r.status_code == 200:
                    df = parse_vision_zip(r.content)
                    log(f"{month}: архів data.binance.vision, {len(df)} свічок")
            if df is None:
                s, e = _month_bounds_ms(month)
                e = min(e, int(time.time() * 1000))
                df = _fetch_rest(client, symbol, interval, s, e)
                log(f"{month}: REST, {len(df)} свічок")
            df.to_parquet(path, index=False)
            paths.append(path)
    return paths


def load(symbol: str, start: str, end: str, cache: Path, interval: str = "1m") -> pd.DataFrame:
    """Читає кеш за діапазон місяців і перевіряє пропуски."""
    d = cache / "klines" / symbol / interval
    parts = []
    for month in month_range(start, end):
        p = d / f"{month}.parquet"
        if not p.exists():
            raise FileNotFoundError(f"Немає {p}. Спершу: eth-agent fetch --start {start} --end {end}")
        parts.append(pd.read_parquet(p))
    df = pd.concat(parts, ignore_index=True)
    return df


def gaps(df: pd.DataFrame, step_ms: int = 60_000) -> pd.DataFrame:
    """Пропуски в ряді свічок (біржові простої теж трапляються — їх треба бачити, а не вигадувати)."""
    diff = df["open_time"].diff()
    bad = df[diff > step_ms]
    return pd.DataFrame({
        "after": pd.to_datetime(df.loc[bad.index - 1, "open_time"].values, unit="ms", utc=True),
        "missing_candles": ((diff[bad.index] // step_ms) - 1).values,
    })
