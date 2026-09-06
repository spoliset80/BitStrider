"""Simulate 2026-09-01's trades under the proposed rule changes.

Data source: Alpaca account activities (READ-ONLY GETs -- no orders placed).
Reconstructs every round trip (entry fill -> exit fill) from real fills, then
re-runs the day under:
  R1  daily loss-limit gate on ALL new entries (fresh + re-entry) once the
      running realized P&L crosses -(limit_pct)% of start equity
  R2  staged re-entry sizing: each re-entry round trip sized at 25% (one
      tranche) instead of 100%
R3 (ATR trailing stop / order-churn hysteresis) cannot be simulated without
   intraday price paths and is reported qualitatively.

Usage: python scripts/_simulate_day.py
"""
import sys
from collections import defaultdict
from datetime import timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATE = "2026-09-01"
START_EQUITY = 2035.10          # engine/.daily_state.json daily_start_equity
KNOWN_REALIZED = -27.49         # stated in the post-mortem
# .env loss limits
ENV_LIMITS = {"bull": 5.0, "bear": 8.0}   # .env overrides
DEFAULT_LIMITS = {"bull": 1.0, "bear": 2.0}  # config.py defaults

REENTRY_TRANCHE_PCT = 25.0      # staged allocation for re-entries (one 25% tranche)

_ET = timezone(timedelta(hours=-4))   # ET (EDT) in September


def load_env() -> dict:
    env = {}
    for ln in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        env[k.strip()] = v.strip()
    return env


def fetch(url: str, key: str, secret: str) -> list | dict:
    r = requests.get(url, auth=(key, secret), timeout=30)
    r.raise_for_status()
    return r.json()


def main():
    env = load_env()
    key = env.get("LIVE_ALPACA_API_KEY", "")
    secret = env.get("LIVE_ALPACA_API_SECRET", "")
    if not key or not secret:
        print("LIVE_ALPACA creds missing from .env -- aborting (no live-account read)")
        sys.exit(1)

    base = "https://api.alpaca.markets/v2"
    acct = fetch(f"{base}/account", key, secret)
    start_eq = float(acct.get("equity", START_EQUITY))
    print(f"account equity now: ${start_eq:,.2f} | buying_power={acct.get('buying_power')}")

    acts = fetch(f"{base}/account/activities?date={DATE}", key, secret)
    fills = []
    for a in acts:
        if a.get("activity_type", "").upper() != "FILL":
            continue
        fills.append({
            "time": a.get("transaction_time"),
            "sym": a.get("symbol"),
            "side": a.get("side"),
            "qty": float(a.get("qty", 0)),
            "price": float(a.get("price", 0)),
            "order": a.get("order_id"),
            "coid": a.get("client_order_id"),
        })
    fills.sort(key=lambda f: f["time"])
    print(f"fills on {DATE}: {len(fills)}")
    if not fills:
        print("no fills -- aborting")
        sys.exit(1)
    print(f"first fill {fills[0]['time']} | last fill {fills[-1]['time']}")

    # ---- reconstruct round trips (FIFO per symbol) -------------------------
    open_q = defaultdict(list)
    roundtrips = []  # {sym, entry_t, exit_t, side, entry_p, exit_p, qty, pnl, kind}
    for f in fills:
        sym, side = f["sym"], f["side"]
        if side in ("buy", "buy_to_open"):
            open_q[sym].append(f)
        elif side in ("sell", "sell_to_close", "buy_to_close"):
            ent = None
            for i, e in enumerate(open_q.get(sym, [])):
                if e["order"] == f["order"]:
                    ent = open_q[sym].pop(i)
                    break
            if ent is None and open_q.get(sym):
                ent = open_q[sym].pop(0)
            if ent is None:
                print(f"  warn: orphan exit {sym} {side} {f['qty']} @ {f['price']} {f['time']}")
                continue
            dirn = -1.0 if "short" in ent["side"] or ent["side"] == "sell" else 1.0
            if f["side"] == "buy_to_close":
                dirn = -1.0
            pnl = (f["price"] - ent["price"]) * dirn * f["qty"]
            roundtrips.append({
                "sym": sym, "entry_t": ent["time"], "exit_t": f["time"],
                "side": ent["side"], "entry_p": ent["price"], "exit_p": f["price"],
                "qty": f["qty"], "pnl": round(pnl, 2), "kind": None,
            })

    # mark fresh vs re-entry: first round trip per symbol = fresh
    seen = set()
    for rt in roundtrips:
        if rt["sym"] in seen:
            rt["kind"] = "re-entry"
        else:
            rt["kind"] = "fresh"
            seen.add(rt["sym"])
    roundtrips.sort(key=lambda r: r["exit_t"])

    total = sum(r["pnl"] for r in roundtrips)
    print(f"\nround trips: {len(roundtrips)}  realized P&L: ${total:,.2f}  "
          f"(stated on 9/1: ${KNOWN_REALIZED})")
    fresh = [r for r in roundtrips if r["kind"] == "fresh"]
    reen = [r for r in roundtrips if r["kind"] == "re-entry"]
    print(f"fresh: {len(fresh)} trips ${sum(r['pnl'] for r in fresh):+.2f} | "
          f"re-entry: {len(reen)} trips ${sum(r['pnl'] for r in reen):+.2f}")

    # ---- simulation ---------------------------------------------------------
    def sim(loss_pct: float, stage_reentry: bool, r1_on: bool,
            only_open_hour: bool = False, fresh_only: bool = False):
        run = 0.0
        blocked = 0
        n_in = 0
        halted = False
        limit = -(START_EQUITY * loss_pct / 100.0)
        for rt in roundtrips:
            if only_open_hour and rt["entry_t"][11:13] != "13":  # 13Z = 09 ET
                continue
            if fresh_only and rt["kind"] != "fresh":
                continue
            n_in += 1
            if halted and r1_on:
                blocked += 1
                continue
            contrib = rt["pnl"]
            if stage_reentry and rt["kind"] == "re-entry":
                contrib *= REENTRY_TRANCHE_PCT / 100.0
            run += contrib
            if r1_on and run <= limit:
                halted = True
        return run, blocked, n_in

    def fmt(run, blk, n):
        return f"${run:>8,.2f}  ({n} trips" + (f", {blk} gated)" if blk else ")")

    print("\n=== scenario table (realized P&L for 2026-09-01) ===")
    print(f"{'scenario':<56} {'$ P&L':<24}")
    print(f"{'ACTUAL day (baseline)':<56} {fmt(total, 0, len(roundtrips))}")
    print(f"{'OPEN-HOUR ONLY (entry 09:xx ET, nothing after)':<56} "
          f"{fmt(*sim(999.0, False, False, only_open_hour=True))}")
    print(f"{'NO RE-ENTRIES (fresh only)':<56} "
          f"{fmt(*sim(999.0, False, False, fresh_only=True))}")
    run, blk, n = sim(1.0, False, True)
    print(f"{'R1 only, loss-limit 1% (default)':<56} {fmt(run, blk, n)}")
    run, blk, n = sim(5.0, False, True)
    print(f"{'R1 only, loss-limit 5% (.env bull)':<56} {fmt(run, blk, n)}")
    print(f"{'R2 only, staged re-entries (25%)':<56} {fmt(*sim(999.0, True, False))}")
    run, blk, n = sim(1.0, True, True)
    print(f"{'R1+R2, loss-limit 1%':<56} {fmt(run, blk, n)}")
    run, blk, n = sim(5.0, True, True)
    print(f"{'R1+R2, loss-limit 5%':<56} {fmt(run, blk, n)}")
    run, blk, n = sim(1.0, True, True, only_open_hour=True)
    print(f"{'R1+R2, loss-limit 1%, open-hour only':<56} {fmt(run, blk, n)}")



    print("\n=== big round trips (|pnl| >= $1) ===")
    for rt in sorted(roundtrips, key=lambda r: -abs(r["pnl"])):
        if abs(rt["pnl"]) >= 1:
            print(f"  {rt['sym']:<6} {rt['kind']:<9} {rt['side']:<11} "
                  f"qty={rt['qty']:>5} @ {rt['entry_p']:>8.2f} -> {rt['exit_p']:>8.2f} "
                  f"${rt['pnl']:+.2f}  (exit {rt['exit_t'][11:19]}Z)")


if __name__ == "__main__":
    main()

