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
                 order_notional: float, min_notional: float = 0.0,
                 max_bp_pct: float = 100.0):
        self.max_positions   = max_positions
        self.max_daily_spend = max_daily_spend
        self.dedupe_ticker   = dedupe_ticker
        self.confidence_min  = confidence_min
        self._alloc = [(90, alloc_high_pct), (80, alloc_med_pct), (70, alloc_low_pct)]
        self._base_notional  = order_notional
        self._min_notional   = min_notional
        self._max_bp_pct     = max_bp_pct

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
        if not buying_power or buying_power <= 0:
            return round(self._base_notional * pct / self._alloc[-1][1], 2)

        notional = buying_power * pct / 100
        # Small accounts: a raw % of BP can be too small to afford a single
        # contract, so lift it to the floor (never above the BP ceiling).
        ceiling = buying_power * self._max_bp_pct / 100
        if notional < self._min_notional:
            notional = self._min_notional
            logger.info(f"  [SIZE] conf={conf} {pct}% of ${buying_power:,.0f} below "
                        f"${self._min_notional:,.0f} floor — raising to floor")
        if notional > ceiling:
            notional = ceiling
        return round(notional, 2)

    def check_buy(self, ticker: str, notional: float, open_count: int, conf: int) -> tuple[bool, str]:
        """Returns (allowed, reason). Call before placing any BUY.
        Confidence filtering is handled upstream in the router."""
        self._check_date_rollover()
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
