"""Review the last N calendar days of real Alpaca fills against the new
two-window trading schedule (2026-09-03): entries 09:14-11:00 + 14:45-15:44 ET,
lunch-flat at 11:00, EOD exit at 15:44, no overnight holds.

Data source: Alpaca account activities (READ-ONLY GETs -- no orders placed).
Round trips are reconstructed with a POSITION LADDER keyed on Alpaca's signed
`position_qty` (net position after each fill), which correctly handles longs,
shorts, partial exits, scale-ins, options legs and overnight chains -- a plain
FIFO on (buy|sell) cannot tell a short-open from a long-close and corrupts
pairing (confirmed: a naive FIFO produced 377 phantom open positions).

Each reconstructed leg is then checked against the new limits:
  - entry before 09:14 ET -> BLOCKED by ENTRY_WINDOW_START_ET
  - entry in 11:00-14:45    -> BLOCKED (midday break, book flat)
  - entry after 15:44 ET    -> BLOCKED (EOD close limit)
  - leg still open at 11:00 -> lunch-flat sweep would close it
  - leg still open past 15:45 / overnight -> EOD close sweep would close it

Timestamps: Alpaca activity transaction_time is UTC; ET = UTC - 4h (EDT).

Usage: python scripts/_review_30d.py [days=30]
No writes, no order placement.
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz
import requests

ROOT = Path(__file__).resolve().parent.parent
ET = pytz.timezone("America/New_York")

# New two-window schedule (config.py 2026-09-01).
W_AM_OPEN, W_AM_CLOSE = 9 * 60 + 14, 11 * 60        # 09:14-11:00 ET (inclusive)
W_PM_OPEN, W_PM_CLOSE = 14 * 60 + 15, 15 * 60 + 44  # 14:15-15:44 ET (inclusive; reopen moved 14:45->14:15, 2026-09-04)


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


def to_et_minutes(utc_iso: str) -> int:
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    et = dt.astimezone(ET)
    return et.hour * 60 + et.minute


def et_hhmm(utc_iso: str) -> str:
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    return dt.astimezone(ET).strftime("%H:%M")


def in_entry_window(m: int) -> bool:
    return (W_AM_OPEN <= m <= W_AM_CLOSE) or (W_PM_OPEN <= m <= W_PM_CLOSE)


def in_lunch(m: int) -> bool:
    return W_AM_CLOSE <= m < W_PM_OPEN


def main() -> None:
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    env = load_env()
    key = env.get("LIVE_ALPACA_API_KEY", "")
    secret = env.get("LIVE_ALPACA_API_SECRET", "")
    if not key or not secret:
        print("LIVE_ALPACA creds missing from .env -- aborting (no live-account read)")
        sys.exit(1)

    base = "https://api.alpaca.markets/v2"
    acct = fetch(f"{base}/account", key, secret)
    print(f"account equity now: ${float(acct.get('equity', 0)):,.2f} | "
          f"buying_power={float(acct.get('buying_power', 0)):,.2f}")

    today = datetime.now(timezone.utc).date()
    dates = [today - timedelta(days=i) for i in range(days_back - 1, -1, -1)]

    # Side-inference position ladder (no position_qty in FILL activities).
    # buy/sell on equities, buy_to_open/sell_to_open/sell_to_close/buy_to_close
    # for options. pos=0 at window start is assumed (legacy positions that
    # predate the window produce at most a handful of mis-labeled shorts).
    pos: dict[str, float] = {}          # current signed position qty
    legs: dict[str, dict] = {}          # open leg state (qty, cost, entry)
    orphan_closes: list[dict] = []      # closes of positions that predate the window
    roundtrips = []
    for d in dates:
        # Alpaca activities cap at 100 per response with no cursor token -- page
        # by time-slicing the day (ET midnight..next ET midnight, UTC ISO) and
        # advancing `after` past the last returned fill.
        day_start_utc = ET.localize(datetime.combine(d, datetime.min.time())).astimezone(timezone.utc)
        day_end_utc = day_start_utc + timedelta(days=1)
        start = day_start_utc.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        end = day_end_utc.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        cursor = start
        seen_ids: set = set()
        while cursor < end:
            try:
                acts = fetch(f"{base}/account/activities?after={cursor}&until={end}&page_size=100&direction=asc", key, secret)
            except Exception as e:
                print(f"  fetch failed for {d}@{cursor}: {e}")
                break
            if not isinstance(acts, list) or not acts:
                break
            for a in sorted(acts, key=lambda x: x.get("transaction_time", "")):
                if a.get("id") in seen_ids:
                    continue
                seen_ids.add(a.get("id"))
                if str(a.get("activity_type", "")).upper() != "FILL":
                    continue
                side = str(a.get("side", "")).lower()
                qty = float(a.get("qty", 0))
                if not a.get("transaction_time") or not a.get("symbol") or qty <= 0:
                    continue
                sym = a["symbol"]
                # Equity + option side semantics (Alpaca): buy/sell for stocks;
                # buy_to_open/sell_to_open open long/short options; sell_to_close
                # closes a LONG option (-qty), buy_to_close closes a SHORT (+qty).
                if side in ("buy", "buy_to_open"):
                    delta = qty
                    open_side = True
                elif side == "sell":
                    delta = -qty
                    open_side = False
                elif side == "sell_to_open":
                    delta = -qty
                    open_side = True
                elif side == "sell_to_close":
                    delta = -qty
                    open_side = False
                elif side == "buy_to_close":
                    delta = qty
                    open_side = False
                else:
                    continue

                prev = pos.get(sym, 0.0)
                now = prev + delta
                pos[sym] = now
                leg = legs.get(sym)

                # Orphan close: a close-side fill with no tracked open leg closes
                # a position that predates the review window. Record the EXIT
                # event only (never fabricate a phantom leg) -- the account is
                # flat NOW, so this rule should end the ladder at ~zero residual.
                if leg is None and prev == 0 and not open_side:
                    orphan_closes.append({"sym": sym, "time": a["transaction_time"], "date": d.isoformat()})
                    continue

                # Sign flip: close the whole prior leg, then open a fresh one.
                if leg and leg["qty"] and ((prev > 0 and now < 0) or (prev < 0 and now > 0)):
                    px = float(a["price"])
                    rpnl = (px * leg["qty"] - leg["cost"]) if leg["dir"] > 0 else (leg["cost"] - px * leg["qty"])
                    roundtrips.append({
                        "sym": sym, "date": d.isoformat(), "dir": leg["dir"],
                        "entry_t": leg["entry"], "exit_t": a["transaction_time"],
                        "entry_p": round(leg["avg"], 2), "exit_p": px,
                        "qty": leg["qty"], "pnl": round(rpnl, 2),
                    })
                    legs.pop(sym, None)
                    leg = None

                if leg is None and now != 0:
                    legs[sym] = {"dir": 1 if now > 0 else -1, "qty": abs(now), "cost": abs(now) * float(a["price"]),
                                 "entry": a["transaction_time"], "avg": float(a["price"])}
                elif leg is not None:
                    if abs(now) >= abs(prev):  # scale-in / add
                        add_qty = abs(now) - abs(prev)
                        if add_qty > 0:
                            leg["cost"] += add_qty * float(a["price"])
                            leg["qty"] = abs(now)
                            leg["avg"] = leg["cost"] / leg["qty"]
                    elif now == 0:  # full close
                        rpnl = (float(a["price"]) * leg["qty"] - leg["cost"]) if leg["dir"] > 0 else (leg["cost"] - float(a["price"]) * leg["qty"])
                        roundtrips.append({
                            "sym": sym, "date": d.isoformat(), "dir": leg["dir"],
                            "entry_t": leg["entry"], "exit_t": a["transaction_time"],
                            "entry_p": round(leg["avg"], 2), "exit_p": float(a["price"]),
                            "qty": leg["qty"], "pnl": round(rpnl, 2),
                        })
                        legs.pop(sym, None)
                    else:  # partial close: realize the closed portion, keep the rest
                        closed_qty = abs(prev) - abs(now)
                        share = closed_qty / abs(prev)
                        rpnl = (float(a["price"]) - leg["avg"]) * closed_qty if leg["dir"] > 0 else (leg["avg"] - float(a["price"])) * closed_qty
                        roundtrips.append({
                            "sym": sym, "date": d.isoformat(), "dir": leg["dir"],
                            "entry_t": leg["entry"], "exit_t": a["transaction_time"],
                            "entry_p": round(leg["avg"], 2), "exit_p": float(a["price"]),
                            "qty": closed_qty, "pnl": round(rpnl, 2),
                        })
                        leg["cost"] *= (1 - share)
                        leg["qty"] = abs(now)

            # Page full (100) -> advance `after` past the last returned fill.
            if len(acts) < 100:
                break
            cursor = acts[-1]["transaction_time"]

    # Mark fresh vs re-entry (first leg per symbol in the window = fresh).
    seen: set = set()
    for rt in roundtrips:
        if rt["sym"] in seen:
            rt["kind"] = "re-entry"
        else:
            rt["kind"] = "fresh"
            seen.add(rt["sym"])

    total = sum(r["pnl"] for r in roundtrips)
    print(f"\n== {len(dates)} days ({dates[0]} .. {dates[-1]}) ==")
    print(f"round trips: {len(roundtrips)}  realized P&L: ${total:+,.2f}")

    fresh = [r for r in roundtrips if r["kind"] == "fresh"]
    reen = [r for r in roundtrips if r["kind"] == "re-entry"]
    print(f"fresh: {len(fresh)} trips ${sum(r['pnl'] for r in fresh):+,.2f} "
          f"| re-entry: {len(reen)} trips ${sum(r['pnl'] for r in reen):+,.2f}")

    # ---- new-schedule classification --------------------------------
    viol = {"before_am": [], "lunch": [], "after_pm": []}
    per_day = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for rt in roundtrips:
        em, xm = to_et_minutes(rt["entry_t"]), to_et_minutes(rt["exit_t"])
        rt["entry_et"], rt["exit_et"] = et_hhmm(rt["entry_t"]), et_hhmm(rt["exit_t"])
        per_day[rt["date"]]["n"] += 1
        per_day[rt["date"]]["pnl"] += rt["pnl"]

        if not in_entry_window(em):
            if em < W_AM_OPEN:
                viol["before_am"].append(rt)
            elif in_lunch(em):
                viol["lunch"].append(rt)
            else:
                viol["after_pm"].append(rt)

    print(f"\n=== per-day round trips ===")
    print(f"{'date':<12} {'n':>4} {'P&L':>10} {'1st entry ET':>12} {'last exit ET':>12}")
    for d in dates:
        if d.isoformat() not in per_day:
            continue
        day_rt = [r for r in roundtrips if r["date"] == d.isoformat()]
        print(f"{d.isoformat():<12} {per_day[d.isoformat()]['n']:>4} "
              f"${per_day[d.isoformat()]['pnl']:>9,.2f} {et_hhmm(min(r['entry_t'] for r in day_rt)):>12} "
              f"{et_hhmm(max(r['exit_t'] for r in day_rt)):>12}")

    print(f"\n=== new-schedule violations ===")
    print(f"entries BEFORE 09:14 ET (would be blocked): {len(viol['before_am'])}")
    for rt in viol["before_am"][:15]:
        print(f"  {rt['date']} {rt['sym']:<6} {rt['kind']:<9} {'short' if rt['dir']<0 else 'long':<5} "
              f"entry {rt['entry_et']} ET ${rt['pnl']:+.2f}")
    print(f"entries in lunch 11:00-14:45 ET (would be blocked): {len(viol['lunch'])}")
    for rt in viol["lunch"][:15]:
        print(f"  {rt['date']} {rt['sym']:<6} {rt['kind']:<9} {'short' if rt['dir']<0 else 'long':<5} "
              f"entry {rt['entry_et']} ET ${rt['pnl']:+.2f}")
    print(f"entries after 15:44 ET (would be blocked): {len(viol['after_pm'])}")
    for rt in viol["after_pm"][:15]:
        print(f"  {rt['date']} {rt['sym']:<6} {rt['kind']:<9} {'short' if rt['dir']<0 else 'long':<5} "
              f"entry {rt['entry_et']} ET ${rt['pnl']:+.2f}")

    # Exit-limit checks include pre-window positions' closes (orphan closes).
    exit_events = [{"time": r["exit_t"], "date": r["date"], "sym": r["sym"]} for r in roundtrips] + orphan_closes
    held_lunch = [e for e in exit_events if to_et_minutes(e["time"]) >= W_AM_CLOSE and e["date"] == e["time"][:10]]
    held_eod = [e for e in exit_events if to_et_minutes(e["time"]) > W_PM_CLOSE and e["date"] == e["time"][:10]]
    overnight_exits = [e for e in exit_events if e["date"] != e["time"][:10]]
    print(f"positions still open at/after 11:00 (lunch-flat would close): {len(held_lunch)}")
    for e in held_lunch[:10]:
        print(f"  {e['date']} {e['sym']:<6} exit {et_hhmm(e['time'])} ET")
    print(f"positions still open at/after 15:44 (EOD close would cut): {len(held_eod)}")
    for e in held_eod[:10]:
        print(f"  {e['date']} {e['sym']:<6} exit {et_hhmm(e['time'])} ET")
    print(f"positions exited on a different day than entered (overnight): {len(overnight_exits)}")
    for e in overnight_exits[:10]:
        print(f"  {e['date']} {e['sym']:<6} exit {e['time']}Z")
    print(f"pre-window position closes (orphan, no in-window entry): {len(orphan_closes)}")
    for e in orphan_closes[:10]:
        print(f"  {e['date']} {e['sym']:<6} closed at {et_hhmm(e['time'])} ET")

    open_legs = {s: lg for s, lg in legs.items() if lg["qty"]}
    print(f"\n=== residual open legs at end (self-check, account is flat now): {len(open_legs)} ===")
    for sym, lg in sorted(open_legs.items())[:12]:
        print(f"  {sym}: {lg['qty']} {'short' if lg['dir'] < 0 else 'long'} "
              f"@ {lg['avg']:.2f}, entered {lg['entry']}")

    if roundtrips:
        e_first = min(roundtrips, key=lambda r: r['entry_t'])
        x_last = max(roundtrips, key=lambda r: r['exit_t'])
        print(f"\n=== boundary check ===")
        print(f"earliest entry in {days_back}d: {et_hhmm(e_first['entry_t'])} ET "
              f"({e_first['sym']}, {e_first['date']})")
        print(f"latest  exit in {days_back}d:  {et_hhmm(x_last['exit_t'])} ET "
              f"({x_last['sym']}, {x_last['date']})")


if __name__ == "__main__":
    main()

