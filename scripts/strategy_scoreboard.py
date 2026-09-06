"""Strategy scoreboard -- recurring Kelly/win-rate health check across every
strategy (2026-08-15, user request: idea #4 of six suggested improvements,
"a recurring strategy scoreboard instead of one-off manual reviews").

Same methodology as the manual backtests run earlier this session (matched
entry/exit pairs from Alpaca order history, confidence pulled from
autobot.log), packaged so it can run unattended on a schedule instead of
only when someone happens to ask. Flags any CURRENTLY ENABLED strategy
whose Kelly % has gone negative with enough trades (n >= MIN_TRADES_TO_JUDGE)
to trust the number -- same "don't judge on fewer than 10 trades" rule the
user set for the one-off disables.

Run manually with:
  python scripts/strategy_scoreboard.py
Or scheduled weekly via windows_schedule_strategy_scoreboard.ps1 (mirrors
windows_schedule_ti_capture.ps1's pattern). Writes strategy_scoreboard.log
next to autobot.log and prints the same report to stdout.
"""
import sys
import re
import datetime
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIN_TRADES_TO_JUDGE = 10  # same floor as the 2026-08-15 manual disables

# Every strategy name get_strategy_instances() can currently produce.
# Anything in the trade data NOT in this set is either retired code (e.g.
# "Sweepea", removed in a pre-2026-08 refactor but still in older order
# history) or an execution-mechanism artifact, not an actual strategy:
# _sweep_pending_entries' re-chase orders are tagged client_order_id=
# "apex-rechase-{sym}-{ts}" (enhanced.py), losing the original strategy
# name -- "rechase" showed up as a fake 3-trade "strategy" the first time
# this ran live. Both cases get reported (useful to know they're in the
# data) but never flagged, since there's no config flag to act on either.
KNOWN_STRATEGIES = {
    "TrendBreaker", "Technical", "Momentum", "GapBreakout", "ORB", "VWAPReclaim",
    "VWAPFade", "LiquiditySweep", "FloatRotation", "PreMarketMomentum",
    "OpeningBellSurge", "PMHighBreakout", "EarlySqueeze", "BearBreakdown",
    "PowerOf3", "Sentiment",
}

# All strategies get_strategy_instances() can produce, mapped to the
# config flag that controls them (None = no toggle, always active).
_STRATEGY_TOGGLE_NAMES = {
    "VWAPFade":          "VWAP_FADE_ENABLED",
    "Momentum":          "MOMENTUM_ENABLED",
    "PreMarketMomentum": "PRE_MARKET_MOMENTUM_ENABLED",
    "FloatRotation":     "FLOAT_ROTATION_ENABLED",
    "Sentiment":         "SENTIMENT_ENABLED",
    "LiquiditySweep":    "LIQUIDITY_SWEEP_ENABLED",
    "PMHighBreakout":    "PM_HIGH_BREAKOUT_ENABLED",
    "Technical":         "TECHNICAL_ENABLED",
}


def kelly_pct(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Kelly fraction: W - (1-W)/R, R = avg_win/avg_loss (both positive $
    amounts). avg_loss <= 0 (no losses observed yet) is treated as a strong
    positive edge (1.0) rather than dividing by zero -- rare, only possible
    with a tiny/all-winning sample."""
    if avg_loss <= 0:
        return 1.0
    r = avg_win / avg_loss
    if r <= 0:
        return -1.0
    return win_rate - (1 - win_rate) / r


def should_flag(enabled: bool, n: int, kelly: float, min_n: int = MIN_TRADES_TO_JUDGE, known: bool = True) -> bool:
    """True if a strategy is worth surfacing: currently enabled, enough
    trades to trust the number, Kelly has gone negative (no edge), and it's
    a real, currently-controllable strategy (known=False -> retired code or
    an execution artifact like a re-chase order that lost its original
    strategy tag -- nothing to action, never flag)."""
    return known and enabled and n >= min_n and kelly < 0


def _strategy_enabled_map() -> dict:
    from engine import config as cfg
    enabled = {}
    for strat, flag_name in _STRATEGY_TOGGLE_NAMES.items():
        enabled[strat] = bool(getattr(cfg, flag_name))
    return enabled  # strategies not in this dict (ORB, GapBreakout, TrendBreaker,
    # VWAPReclaim, OpeningBellSurge, EarlySqueeze, PowerOf3, BearBreakdown) have
    # no toggle -- caller treats "not in dict" as always-enabled.


def _pull_matched_trades():
    """Network call: pull every apex-tagged entry, match to its exit,
    pull confidence from autobot.log. Returns a list of trade dicts."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.common.enums import Sort
    from engine.config import API_KEY, API_SECRET, PAPER

    tc = TradingClient(API_KEY, API_SECRET, paper=PAPER)
    coid_re = re.compile(r"^apex-([A-Za-z0-9]+)-([A-Z]+)-\d+$")

    all_entries = []
    until = None
    for _ in range(30):
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, direction=Sort.DESC, until=until)
        orders = tc.get_orders(req)
        if not orders:
            break
        for o in orders:
            if o.status.value != "filled" or not o.client_order_id:
                continue
            m = coid_re.match(o.client_order_id)
            if m:
                all_entries.append((o, m.group(1)))
        until = orders[-1].submitted_at
        if len(orders) < 500:
            break

    by_symbol_cache = {}

    def orders_for_symbol(sym):
        if sym not in by_symbol_cache:
            req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[sym], limit=500, direction=Sort.ASC)
            by_symbol_cache[sym] = [o for o in tc.get_orders(req) if o.status.value == "filled"]
        return by_symbol_cache[sym]

    trades = []
    for o, strat in all_entries:
        sym_orders = orders_for_symbol(o.symbol)
        entry_side = o.side.value
        exit_side = "sell" if entry_side == "buy" else "buy"
        exit_order = None
        for x in sym_orders:
            if x.filled_at and o.filled_at and x.filled_at > o.filled_at and x.side.value == exit_side:
                exit_order = x
                break
        if not exit_order:
            continue
        entry_px = float(o.filled_avg_price)
        exit_px = float(exit_order.filled_avg_price)
        qty = float(o.filled_qty)
        is_long = entry_side == "buy"
        pnl_usd = (exit_px - entry_px) * qty if is_long else (entry_px - exit_px) * qty
        trades.append({"strategy": strat, "pnl_usd": pnl_usd})

    return trades


def _summarize(trades: list) -> dict:
    """{strategy: (n, win_rate, avg_win, avg_loss, kelly)} from a trade list."""
    by_strat = {}
    for t in trades:
        by_strat.setdefault(t["strategy"], []).append(t["pnl_usd"])

    out = {}
    for strat, pnls in by_strat.items():
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / n if n else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
        out[strat] = (n, win_rate, avg_win, avg_loss, kelly_pct(win_rate, avg_win, avg_loss))
    return out


def run(log_to_file: bool = True) -> list:
    """Pull data, compute the scoreboard, log + print it, return flagged strategies."""
    log = logging.getLogger("StrategyScoreboard")
    log.setLevel(logging.INFO)
    if log_to_file and not log.handlers:
        fh = logging.FileHandler(ROOT / "strategy_scoreboard.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        log.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)

    log.info("=" * 70)
    log.info(f"STRATEGY SCOREBOARD -- {datetime.datetime.now():%Y-%m-%d %H:%M}")
    log.info("=" * 70)

    trades = _pull_matched_trades()
    summary = _summarize(trades)
    enabled_map = _strategy_enabled_map()

    flagged = []
    for strat in sorted(summary, key=lambda s: summary[s][4]):
        n, win_rate, avg_win, avg_loss, kelly = summary[strat]
        known = strat in KNOWN_STRATEGIES
        enabled = enabled_map.get(strat, True)  # not in map -> no toggle, always on
        status = ("ENABLED" if enabled else "disabled") if known else "RETIRED/OTHER"
        # Kelly can swing to implausible-looking extremes when avg_loss is
        # tiny (e.g. -707%) -- the sign/magnitude direction is still right,
        # but clip the DISPLAYED number to a readable +/-100% (Kelly fractions
        # are conventionally bounded there anyway); should_flag() below still
        # sees the real, unclipped value.
        kelly_display = max(-1.0, min(1.0, kelly))
        log.info(
            f"  {strat:18} n={n:>3}  win={win_rate:>5.0%}  "
            f"avg_win=${avg_win:>6.2f}  avg_loss=${avg_loss:>6.2f}  "
            f"kelly={kelly_display:>+6.1%}  [{status}]"
        )
        if should_flag(enabled, n, kelly, known=known):
            flagged.append(strat)

    log.info("-" * 70)
    if flagged:
        log.info(f"FLAGGED (enabled, n>={MIN_TRADES_TO_JUDGE}, negative Kelly): {', '.join(flagged)}")
        log.info("Consider disabling via the strategy's *_ENABLED flag in config.py.")
    else:
        log.info("Nothing flagged -- every enabled strategy with enough trades has a non-negative Kelly.")
    log.info("=" * 70)
    return flagged


if __name__ == "__main__":
    run()
