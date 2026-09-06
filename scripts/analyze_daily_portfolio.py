"""Daily portfolio observation for the automation loop (2026-09-08).

READ-ONLY: reconstructs round trips from real Alpaca fills (position ladder,
same method proven in scripts/_review_30d.py -- a naive FIFO corrupts pairing
across longs/shorts/partial exits) and produces the daily observation the
automation controller plans against:

  - account snapshot (equity, day P&L vs last close, buying power)
  - round-trip summary (n, win rate, avg win/loss, expectancy, profit factor)
  - per-day P&L series + max drawdown on the daily cumulative
  - per-symbol attribution + churn chains (>=3 round trips/day, the 9/4
    GPRO+AXTX pattern the per-symbol chain budget targets)
  - entry-band outcomes + window violations
  - runtime health: heartbeat freshness, guardian state, pending flags

No orders are placed, nothing in the repo is written, and no credentials are
ever copied into artifacts. Artifacts go to machine-local
%LOCALAPPDATA%\\ApexTrader\\automation\\<YYYY-MM-DD>\\ (outside OneDrive,
like all coordination state per the 2026-09-02 incidents).

Usage:
  python scripts/analyze_daily_portfolio.py [--days 30] [--out DIR] [--offline]
  --offline: skip all network calls (weekday fallback for market day, empty
  fills) -- used when Alpaca is unreachable and by tests.
"""
import argparse
import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytz
import requests

ROOT = Path(__file__).resolve().parent.parent
ET = pytz.timezone("America/New_York")
LOCAL_BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
AUTOMATION_DIR = LOCAL_BASE / "ApexTrader" / "automation"

HEARTBEAT_STALE_SECONDS = 300  # same staleness the guardian treats as "bot down"


def load_env(root: Path = ROOT) -> dict:
    env = {}
    for ln in (root / ".env").read_text(encoding="utf-8-sig").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        env[k.strip()] = v.strip()
    return env


def fetch_json(url: str, key: str, secret: str, params: dict | None = None):
    r = requests.get(url, auth=(key, secret), params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def market_day_open(env: dict, day) -> bool | None:
    """True/False from the Alpaca calendar; None when unavailable (offline /
    network failure) -- callers must treat None as 'unknown'."""
    try:
        key = env.get("LIVE_ALPACA_API_KEY", "")
        secret = env.get("LIVE_ALPACA_API_SECRET", "")
        if not key or not secret:
            return None
        data = fetch_json(
            "https://api.alpaca.markets/v2/calendar", key, secret,
            {"start": day.isoformat(), "end": day.isoformat()},
        )
        if isinstance(data, list) and data:
            return bool(data[0].get("date")) and data[0].get("open", 1) is not False
        return False
    except Exception:
        return None


def fetch_fills(env: dict, days: int) -> list:
    """FILL activities for the last `days` calendar days (paginated)."""
    key = env.get("LIVE_ALPACA_API_KEY", "")
    secret = env.get("LIVE_ALPACA_API_SECRET", "")
    if not key or not secret:
        return []
    start = (datetime.datetime.now(ET) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    out: list = []
    token = None
    for _ in range(40):  # 100/page -> 4000 fills is far beyond one month
        params = {"activity_types": "FILL", "page_size": 100, "after": start}
        if token:
            params["page_token"] = token
        data = fetch_json("https://api.alpaca.markets/v2/account/activities", key, secret, params)
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        token = data[-1].get("id")
        if len(data) < 100:
            break
    return out


def _parse_utc(ts: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def classify_entry_kind(client_order_id: str) -> str:
    cid = str(client_order_id or "")
    if cid.startswith("apex-staged-"):
        return "staged"
    if cid.startswith("apex-reentry-trail-"):
        return "reentry"
    if cid.startswith("apex-entry-"):
        return "fresh"
    if cid.startswith("apex-close-") or cid.startswith("apex-rechase-"):
        return "close_side"
    return "other"


def reconstruct_roundtrips(fills: list) -> list:
    """Position-ladder reconstruction (mirrors scripts/_review_30d.py's method;
    fills are re-sorted defensively by transaction time)."""
    legs: dict[str, dict] = {}
    rts: list = []
    for a in sorted(
        fills,
        key=lambda x: (_parse_utc(x.get("transaction_time") or x.get("activity_time") or "")
                       or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)),
    ):
        try:
            sym = str(a["symbol"]).upper()
            price = float(a["price"])
            qty = abs(float(a["qty"]))
            side = str(a.get("side", "")).lower()
        except Exception:
            continue
        ts = _parse_utc(a.get("transaction_time") or a.get("activity_time") or "")
        if ts is None:
            ts = datetime.datetime.now(datetime.timezone.utc)
        et_ts = ts.astimezone(ET)
        if qty <= 0 or side not in ("buy", "sell"):
            continue
        now_iso = et_ts.isoformat()
        leg = legs.get(sym)
        closing = (side == "buy" and leg and leg["dir"] < 0) or \
                  (side == "sell" and leg and leg["dir"] > 0)
        if closing:
            close_qty = min(qty, leg["qty"])
            pnl = (leg["avg"] - price) * close_qty if leg["dir"] < 0 else (price - leg["avg"]) * close_qty
            rts.append({
                "symbol": sym,
                "kind": "short" if leg["dir"] < 0 else "long",
                "entry_kind": leg.get("entry_kind", "other"),
                "qty": close_qty,
                "entry_px": leg["avg"],
                "exit_px": price,
                "entry_et": leg["entry_et"],
                "exit_et": now_iso,
                "date": et_ts.date().isoformat(),
                "pnl": round(pnl, 4),
            })
            leg["qty"] -= close_qty
            remaining = qty - close_qty
            if remaining > 0:
                legs[sym] = {"dir": 1 if side == "buy" else -1, "qty": remaining,
                             "avg": price, "entry_et": now_iso,
                             "entry_kind": classify_entry_kind(a.get("client_order_id"))}
            elif leg["qty"] <= 1e-9:
                legs.pop(sym, None)
        elif leg and ((side == "buy" and leg["dir"] > 0) or (side == "sell" and leg["dir"] < 0)):
            total = leg["qty"] + qty
            leg["avg"] = (leg["avg"] * leg["qty"] + price * qty) / total
            leg["qty"] = total
        else:
            legs[sym] = {"dir": 1 if side == "buy" else -1, "qty": qty, "avg": price,
                         "entry_et": now_iso,
                         "entry_kind": classify_entry_kind(a.get("client_order_id"))}
    return rts


def summarize_roundtrips(rts: list) -> dict:
    pnls = [r["pnl"] for r in rts]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    n = len(pnls)
    return {
        "count": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss": round(abs(sum(losses) / len(losses)), 4) if losses else 0.0,
        "expectancy": round(sum(pnls) / n, 4) if n else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0
                         else (1.0 if gross_win > 0 else 0.0),
        "total_pnl": round(sum(pnls), 2),
    }


def daily_pnl_series(rts: list) -> list:
    by_day: dict = defaultdict(float)
    for r in rts:
        by_day[r["date"]] += r["pnl"]
    return [{"date": d, "pnl": round(by_day[d], 2)} for d in sorted(by_day)]


def max_drawdown(series: list) -> float:
    """Max peak-to-trough drawdown of the CUMULATIVE daily P&L (<= 0)."""
    cum, peak, worst = 0.0, 0.0, 0.0
    for point in series:
        cum += point["pnl"]
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return round(worst, 2)


def band_for_minutes(m: int) -> str:
    if m < 9 * 60 + 14:
        return "pre_window"
    if m < 10 * 60 + 30:
        return "am_0914_1030"
    if m < 11 * 60:
        return "am_1030_1100"
    if m < 14 * 60 + 15:
        return "lunch_violation"
    if m <= 15 * 60 + 44:
        return "pm_1415_1544"
    return "after_eod_violation"


def entry_band_stats(rts: list) -> dict:
    bands: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0, "losses": 0})
    for r in rts:
        ts = _parse_utc(r["entry_et"])
        if ts is None:
            continue
        et_ts = ts.astimezone(ET)
        b = band_for_minutes(et_ts.hour * 60 + et_ts.minute)
        bands[b]["n"] += 1
        bands[b]["pnl"] = round(bands[b]["pnl"] + r["pnl"], 2)
        if r["pnl"] <= 0:
            bands[b]["losses"] += 1
    return {k: dict(v) for k, v in sorted(bands.items())}


def per_symbol_stats(rts: list) -> list:
    by_sym: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for r in rts:
        by_sym[r["symbol"]]["n"] += 1
        by_sym[r["symbol"]]["pnl"] = round(by_sym[r["symbol"]]["pnl"] + r["pnl"], 2)
    rows = [{"symbol": s, **v} for s, v in by_sym.items()]
    rows.sort(key=lambda x: x["pnl"])  # worst first
    return rows


def churn_chains(rts: list, min_count: int = 3) -> list:
    """Same-symbol same-day chains with >= min_count round trips and net
    negative P&L -- the repeated-churn pattern (9/4 GPRO+AXTX = 24% of gross
    losses) the per-symbol daily chain budget targets."""
    chains: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for r in rts:
        key = (r["date"], r["symbol"])
        chains[key]["n"] += 1
        chains[key]["pnl"] = round(chains[key]["pnl"] + r["pnl"], 2)
    out = [
        {"date": d, "symbol": s, "count": v["n"], "pnl": v["pnl"]}
        for (d, s), v in chains.items()
        if v["n"] >= min_count and v["pnl"] < 0
    ]
    out.sort(key=lambda x: x["pnl"])
    return out


def runtime_health(root: Path = ROOT, state_dir: Path | None = None) -> dict:
    if state_dir is None:
        state_dir = LOCAL_BASE / "ApexTrader" / "state"
    now = datetime.datetime.now(datetime.timezone.utc)
    hb_age = None
    hb = root / "heartbeat.txt"
    if hb.exists():
        try:
            last = datetime.datetime.fromisoformat(hb.read_text(encoding="utf-8").strip())
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            hb_age = round((now - last).total_seconds(), 1)
        except Exception:
            hb_age = None
    guardian = {}
    gs = state_dir / "guardian_state.json"
    if gs.exists():
        try:
            guardian = json.loads(gs.read_text(encoding="utf-8"))
        except Exception:
            guardian = {"unreadable": True}
    halted_today = str(guardian.get("halted_date", "")) == datetime.datetime.now(ET).date().isoformat()
    return {
        "heartbeat_age_s": hb_age,
        "heartbeat_stale": hb_age is None or hb_age > HEARTBEAT_STALE_SECONDS,
        "guardian_halted_today": halted_today,
        "guardian_last_run": guardian.get("last_run"),
        "flat_request_flag": (state_dir / "flat_request.flag").exists(),
        "deploy_request_flag": (state_dir / "deploy_requested.flag").exists(),
    }


def build_observation(days: int = 30, offline: bool = False, root: Path = ROOT,
                      now: datetime.datetime | None = None) -> dict:
    now = now or datetime.datetime.now(ET)
    obs: dict = {
        "schema_version": 1,
        "as_of_et": now.isoformat(),
        "days_requested": days,
        "notes": [],
    }
    if offline:
        obs["notes"].append("offline mode: no network data")
        obs["market_day_open_today"] = None
        obs["account"] = None
        obs["fills_count"] = 0
    else:
        try:
            env = load_env(root)
            today = now.date()
            obs["market_day_open_today"] = market_day_open(env, today)
            acct = fetch_json("https://api.alpaca.markets/v2/account",
                              env.get("LIVE_ALPACA_API_KEY", ""), env.get("LIVE_ALPACA_API_SECRET", ""))
            obs["account"] = {
                "equity": round(float(acct.get("equity", 0)), 2),
                "last_equity": round(float(acct.get("last_equity", 0)), 2),
                "day_pnl": round(float(acct.get("equity", 0)) - float(acct.get("last_equity", 0)), 2),
                "buying_power": round(float(acct.get("buying_power", 0)), 2),
            }
            fills = fetch_fills(env, days)
            obs["fills_count"] = len(fills)
        except Exception as exc:
            obs["notes"].append(f"network data unavailable: {type(exc).__name__}")
            obs["market_day_open_today"] = None
            obs["account"] = None
            obs["fills_count"] = 0
            fills = []
        rts = reconstruct_roundtrips(fills) if fills else []
    if offline:
        rts = []
    obs["roundtrips"] = summarize_roundtrips(rts)
    series = daily_pnl_series(rts)
    obs["daily"] = series[-15:]
    obs["max_drawdown_daily"] = max_drawdown(series)
    syms = per_symbol_stats(rts)
    obs["per_symbol_worst"] = syms[:10]
    obs["per_symbol_best"] = sorted(syms, key=lambda x: -x["pnl"])[:5]
    obs["churn_chains"] = churn_chains(rts)[:15]
    obs["entry_bands"] = entry_band_stats(rts)
    obs["window_violations"] = (obs["entry_bands"].get("lunch_violation", {}).get("n", 0)
                                + obs["entry_bands"].get("pre_window", {}).get("n", 0)
                                + obs["entry_bands"].get("after_eod_violation", {}).get("n", 0))
    obs["runtime"] = runtime_health(root)
    return obs


def write_artifacts(obs: dict, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or (AUTOMATION_DIR / datetime.datetime.now(ET).date().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "observation.json").write_text(
        json.dumps(obs, indent=2, default=str), encoding="utf-8")
    rt = obs.get("roundtrips", {})
    lines = [
        f"# Daily observation — {obs.get('as_of_et', '?')}",
        f"- fills analyzed: {obs.get('fills_count')}",
        f"- round trips: {rt.get('count')} | win rate: {rt.get('win_rate')} | "
        f"expectancy: ${rt.get('expectancy')} | profit factor: {rt.get('profit_factor')}",
        f"- total P&L: ${rt.get('total_pnl')} | max daily-cumulative drawdown: ${obs.get('max_drawdown_daily')}",
        f"- market day open today: {obs.get('market_day_open_today')}",
        f"- account: {obs.get('account')}",
        f"- window violations: {obs.get('window_violations')}",
        f"- churn chains (>=3 RT/day, net neg): {obs.get('churn_chains')}",
        f"- worst symbols: {obs.get('per_symbol_worst')}",
        f"- runtime: {obs.get('runtime')}",
        f"- notes: {obs.get('notes')}",
    ]
    (out_dir / "observation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="", help="override artifact directory")
    ap.add_argument("--offline", action="store_true", help="no network (tests / Alpaca unreachable)")
    args = ap.parse_args()
    obs = build_observation(days=args.days, offline=args.offline)
    out_dir = write_artifacts(obs, Path(args.out) if args.out else None)
    print(f"[OBSERVE] artifacts -> {out_dir}")
    print(f"[OBSERVE] round trips: {obs['roundtrips'].get('count')} "
          f"total P&L: ${obs['roundtrips'].get('total_pnl')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())



