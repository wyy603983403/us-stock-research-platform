"""Lowest days-to-cover (study sp500_short_interest).

Input ``short_interest/YYYYMMDD`` (FINRA, ``collectors/finra_short.py``). On a signal day the
latest settlement date with settlement + 14 days <= the signal day is used; FINRA symbols with
"." or "/" are written with "-" to match member tickers; several rows of one symbol keep the one
with the largest short position. Days to cover = short shares / average daily volume (volume > 0).
Score = -days to cover (the engine buys the highest scores), candidates without a value -inf.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

PUBLICATION_LAG = timedelta(days=14)
UNRANKED = float("-inf")


def norm(symbol: str) -> str:
    return symbol.strip().replace(".", "-").replace("/", "-")


def days_to_cover(rows: list[tuple[str, float | None, float | None]]) -> dict[str, float]:
    best: dict[str, tuple[float, float | None]] = {}
    for sym, qty, adv in rows:
        s = norm(sym)
        q = qty if qty is not None else float("-inf")
        if s not in best or q > best[s][0]:
            best[s] = (q, adv)
    return {
        s: q / adv
        for s, (q, adv) in best.items()
        if adv is not None and adv > 0 and q != float("-inf")
    }


@dataclass
class ShortInterest:
    contract: dict[str, Any]
    days: list[date]
    tables: Any
    cache: dict[date, dict[str, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.settlements = sorted(date.fromisoformat(f"{k[:4]}-{k[4:6]}-{k[6:]}")
                                  for k in self.tables.keys("short_interest"))  # fmt: skip

    def snapshot(self, day: date) -> tuple[date | None, dict[str, float]]:
        k = bisect.bisect_right(self.settlements, day - PUBLICATION_LAG) - 1
        if k < 0:
            return None, {}
        s = self.settlements[k]
        if s not in self.cache:
            rows = self.tables.read("short_interest", f"{s:%Y%m%d}", "symbol, short_qty, adv")
            self.cache[s] = days_to_cover([(str(a), b, c) for a, b, c in rows])
        return s, self.cache[s]

    def __call__(self, candidates: list[str], t: int) -> tuple[dict[str, float], dict[str, int]]:
        settle, dtc = self.snapshot(self.days[t])
        scores = {s: (-dtc[s] if s in dtc else UNRANKED) for s in candidates}
        with_value = sum(1 for s in candidates if s in dtc)
        lag = (self.days[t] - settle).days if settle else -1
        return scores, {"candidates": len(candidates), "with_value": with_value, "lag_days": lag}


def fingerprint(tables: Any) -> str:
    digest = hashlib.sha256()
    for key in tables.keys("short_interest"):
        digest.update(key.encode())
        digest.update(hashlib.sha256(tables.path("short_interest", key).read_bytes()).digest())
    return "finra-short:sha256:" + digest.hexdigest()
