"""Risk management — position limits, daily cap, deduplication."""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional, Set

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, max_positions: int, max_daily_spend: float,
                 dedupe_ticker: bool, confidence_min: int,
                 alloc_low_pct: float, alloc_med_pct: float, alloc_high_pct: float,
                 order_notional: float):
        self.max_positions   = max_positions
        self.max_daily_spend = max_daily_spend
        self.dedupe_ticker   = dedupe_ticker
        self.confidence_min  = confidence_min
        self._alloc = [(90, alloc_high_pct), (80, alloc_med_pct), (70, alloc_low_pct)]
        self._base_notional  = order_notional

        self._daily_spent: float = 0.0
        self._bought_today: Set[str] = set()
        self._today: str = datetime.now().strftime("%Y%m%d")

    def _check_date_rollover(self):
        today = datetime.now().strftime("%Y%m%d")
        if today != self._today:
            self._today       = today
            self._daily_spent = 0.0
            self._bought_today.clear()
            logger.info("New trading day — daily counters reset")

    def alloc_pct(self, conf: int) -> float:
        for threshold, pct in self._alloc:
            if conf >= threshold:
                return pct
        return self._alloc[-1][1]

    def notional_for(self, conf: int, buying_power: Optional[float] = None) -> float:
        pct = self.alloc_pct(conf)
        if buying_power and buying_power > 0:
            return round(buying_power * pct / 100, 2)
        return round(self._base_notional * pct / self._alloc[-1][1], 2)

    def check_buy(self, ticker: str, notional: float, open_count: int, conf: int) -> tuple[bool, str]:
        """Returns (allowed, reason). Call before placing any BUY."""
        self._check_date_rollover()

        if conf < self.confidence_min:
            return False, f"conf {conf}% < min {self.confidence_min}%"

        if self.dedupe_ticker and ticker in self._bought_today:
            return False, f"dedupe: already bought {ticker} today"

        if self._daily_spent + notional > self.max_daily_spend:
            return False, f"daily cap ${self._daily_spent:.0f}+${notional:.0f} > ${self.max_daily_spend:.0f}"

        if open_count >= self.max_positions:
            return False, f"max positions {open_count}/{self.max_positions}"

        return True, "ok"

    def record_buy(self, ticker: str, notional: float):
        self._daily_spent += notional
        self._bought_today.add(ticker)

    @property
    def daily_spent(self) -> float:
        return self._daily_spent
