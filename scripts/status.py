"""ApexTrader -- one-glance status dashboard (2026-09-02). READ-ONLY.

Prints local coordination state (guardian, flags, heartbeat, daily baseline,
processes) and --account optionally polls Alpaca for the live equity/P&L.

Usage:
  python scripts/status.py            # local state only (no network)
  python scripts/status.py --account  # + live account equity vs today baseline
"""
import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import pytz

ROOT = Path(__file__).resolve().parent.parent
ET = pytz.timezone("America/New_York")
LOCAL_STATE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ApexTrader" / "state"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", action="store_true", help="also poll Alpaca for live equity/day P&L")
    args = ap.parse_args()

    now_et = datetime.datetime.now(ET)
    print(f"ApexTrader status  |  {now_et.isoformat()} ET")
    print("-" * 70)

    # heartbeat
    hb = ROOT / "heartbeat.txt"
    if hb.exists():
        age_s = None
        try:
            last = datetime.datetime.fromisoformat(hb.read_text(encoding="utf-8").strip())
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            age_s = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
        except Exception:
            pass
        if age_s is None:
            print(f"heartbeat:   unreadable ({hb})")
        else:
            print(f"heartbeat:   {age_s / 60:.1f} min ago {'-- STALE' if age_s > 300 else ''}")
    else:
        print("heartbeat:   MISSING (bot not running / never wrote)")

    # daily baseline
    ds = ROOT / "engine" / ".daily_state.json"
    if ds.exists():
        try:
            st = json.loads(ds.read_text(encoding="utf-8"))
            print(f"day start:   ${float(st.get('daily_start_equity', 0)):,.2f} since {st.get('daily_reset')}")
        except Exception:
            print("day start:   unreadable .daily_state.json")
    else:
        print("day start:   no .daily_state.json")

    # guardian state
    gs = LOCAL_STATE / "guardian_state.json"
    if gs.exists():
        try:
            g = json.loads(gs.read_text(encoding="utf-8"))
            print(f"guardian:    last={g.get('last_run', '?')} pnl=${g.get('pnl', '?')} "
                  f"({g.get('pct', '?')}%) halted={g.get('halted_date', 'no')} alerted={g.get('alerted_date', 'no')}")
        except Exception:
            print("guardian:    unreadable guardian_state.json")
    else:
        print("guardian:    no guardian_state.json yet (guardian not run in band yet)")

    # flags
    flat = LOCAL_STATE / "flat_request.flag"
    dep = LOCAL_STATE / "deploy_requested.flag"
    if flat.exists():
        print(f"FLAG:        flat_request.flag PRESENT ({flat.read_text(encoding='utf-8')[:120]})")
    if dep.exists():
        print(f"FLAG:        deploy_requested.flag PRESENT (watchdog will consume at next flat window)")

    # processes (best-effort; elevated ones show as '?')
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["name", "cmdline"]):
            cl = " ".join(p.info.get("cmdline") or [])
            if "main.py" in cl or "autobot.py" in cl or "guardian.py" in cl:
                try:
                    started = datetime.datetime.fromtimestamp(p.create_time()).strftime("%H:%M:%S")
                except Exception:
                    started = "?"
                procs.append(f"{p.info.get('name')} pid={p.pid} started={started} {cl[:80]}")
        if procs:
            print("processes:")
            for line in procs:
                print(f"   {line}")
        else:
            print("processes:   none matching main/autobot/guardian visible from this shell")
    except Exception as exc:
        print(f"processes:   psutil unavailable ({exc})")

    # optional live account
    if args.account:
        print("-" * 70)
        env = {}
        try:
            for ln in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, _, v = ln.partition("=")
                env[k.strip()] = v.strip()
        except Exception as exc:
            print(f"account:     .env unreadable ({exc})")
            return 1
        mode = env.get("TRADE_MODE", "paper").strip().lower()
        key = env.get("LIVE_ALPACA_API_KEY" if mode == "live" else "PAPER_ALPACA_API_KEY", "")
        secret = env.get("LIVE_ALPACA_API_SECRET" if mode == "live" else "PAPER_ALPACA_API_SECRET", "")
        if not key or not secret:
            print(f"account:     missing {mode.upper()} creds in .env")
            return 1
        try:
            import requests
            base = "https://api.alpaca.markets/v2" if mode == "live" else "https://paper-api.alpaca.markets/v2"
            acct = requests.get(f"{base}/account", auth=(key, secret), timeout=30).json()
            equity = float(acct.get("equity", 0))
            baseline = 0.0
            try:
                st = json.loads(ds.read_text(encoding="utf-8"))
                if st.get("daily_reset") == now_et.date().isoformat():
                    baseline = float(st.get("daily_start_equity", 0))
            except Exception:
                pass
            pnl = equity - baseline if baseline else None
            print(f"account ({mode}): equity=${equity:,.2f} buying_power=${float(acct.get('buying_power', 0)):,.2f}")
            if pnl is not None:
                print(f"   day P&L vs baseline: ${pnl:+,.2f} ({(pnl / baseline * 100) if baseline else 0:+.2f}%)")
            else:
                print("   day P&L: no today baseline yet (bot hasn't reset)")
        except Exception as exc:
            print(f"account:     poll failed ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
