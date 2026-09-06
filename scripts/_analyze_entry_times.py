"""Analyze apextrader.log: entry time -> outcome, to test the hypothesis
that entries right at the open (09:30 ET) are the winners.
Log timestamps are US Central Time; ET = CT + 1h (both observe DST).
Read-only analysis - no network, no writes.
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "apextrader.log"

# Trailing-stop submission lines fire ONCE per actual entry order (fresh or
# re-entry) -- far closer to a real position than the per-cycle EXECUTE logs
# (which also include retries and blocked-attempt spam).
ENTRY_KIND_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) .*?"
    r"(?P<sym>[A-Z]+): (?P<kind>re-entry|entry) -- 0.25% trailing-stop (BUY|SELL) entry"
)
# exit records + the _maybe_rearm_reentry outcome that follows tells win/loss
REARM_OUTCOME_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[(?:WARNING|INFO)\] "
    r"(?P<tag>STOPPED OUT|EMA9 EXIT|NO-GAIN EXIT|SOFTWARE SL HIT|SWING STALE|SWING DRIFT|PRICE DRIFT|EOD CLOSE) "
    r"(?P<sym>[A-Z]+): (?P<body>.*)"
)


def to_minutes(ts: str) -> int:
    _, hhmm = ts.split(" ")
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


def et_hhmm(ts: str) -> str:
    mins = to_minutes(ts) + 60  # CT -> ET
    return f"{mins // 60:02d}:{mins % 60:02d}"


def main():
    text = LOG.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    entries = []  # (ts, sym, kind)
    exits = []    # (ts, tag, sym, is_loss)

    for ln in lines:
        m = ENTRY_KIND_RE.search(ln)
        if m:
            entries.append((m["ts"], m["sym"], m["kind"]))
            continue
        m = REARM_OUTCOME_RE.search(ln)
        if m:
            body = m["body"]
            is_loss = ("loss/reversal" in body) or ("30m gate" in body) or ("not aligned" in body)
            exits.append((m["ts"], m["tag"], m["sym"], is_loss))

    print(f"entry orders submitted={len(entries)}  exits={len(exits)}")
    if not entries:
        print("no entries found")
        return
    print(f"first entry order: {entries[0][0]} CT  = {et_hhmm(entries[0][0])} ET")
    print(f"last  entry order: {entries[-1][0]} CT  = {et_hhmm(entries[-1][0])} ET")

    # FIFO pair each exit with the earliest unclosed entry order for the symbol.
    open_q = defaultdict(list)
    for e in entries:
        open_q[e[1]].append(e)
    results = []
    for (xts, tag, sym, is_loss) in exits:
        q = open_q.get(sym)
        if not q:
            results.append((None, sym, None, tag, is_loss))
            continue
        entry = q.pop(0)
        results.append((entry[0], sym, entry[2], tag, is_loss))

    by_hour = defaultdict(lambda: {"n": 0, "loss": 0})
    by_hour_kind = defaultdict(lambda: defaultdict(lambda: {"n": 0, "loss": 0}))
    for (ets, sym, kind, tag, is_loss) in results:
        if ets is None:
            continue
        hour = et_hhmm(ets)[:2] + ":xx ET"
        by_hour[hour]["n"] += 1
        if is_loss:
            by_hour[hour]["loss"] += 1
        by_hour_kind[hour][kind]["n"] += 1
        if is_loss:
            by_hour_kind[hour][kind]["loss"] += 1

    print("\n=== outcome by ENTRY hour (ET) -- entry-order submissions ===")
    print(f"{'hour':<10} {'matched':>8} {'loss':>6} {'loss%':>7}")
    for hour in sorted(by_hour):
        d = by_hour[hour]
        pct = d["loss"] / d["n"] * 100 if d["n"] else 0
        print(f"{hour:<10} {d['n']:>8} {d['loss']:>6} {pct:>6.1f}%")

    print("\n=== outcome by ENTRY hour x fresh/re-entry ===")
    print(f"{'hour':<10} {'kind':<10} {'n':>4} {'loss':>5} {'loss%':>7}")
    for hour in sorted(by_hour_kind):
        for kind in sorted(by_hour_kind[hour]):
            d = by_hour_kind[hour][kind]
            pct = d["loss"] / d["n"] * 100 if d["n"] else 0
            print(f"{hour:<10} {kind:<10} {d['n']:>4} {d['loss']:>5} {pct:>6.1f}%")

    print("\n=== fresh vs re-entry totals ===")
    ft = {"n": 0, "loss": 0}
    rt = {"n": 0, "loss": 0}
    for (ets, sym, kind, tag, is_loss) in results:
        if ets is None:
            continue
        t = ft if kind == "entry" else rt
        t["n"] += 1
        if is_loss:
            t["loss"] += 1
    for name, t in (("fresh", ft), ("re-entry", rt)):
        pct = t["loss"] / t["n"] * 100 if t["n"] else 0
        print(f"  {name:<9} n={t['n']:>4} loss={t['loss']:>4} loss%={pct:>5.1f}%")

    print("\n=== loss exits by tag ===")
    tagc = defaultdict(lambda: [0, 0])
    for (ets, sym, kind, tag, is_loss) in results:
        tagc[tag][0] += 1
        if is_loss:
            tagc[tag][1] += 1
    for tag in sorted(tagc):
        print(f"  {tag:<20} n={tagc[tag][0]:>3} loss={tagc[tag][1]:>3}")

    print("\n=== unmatched exits (no open entry order) ===")
    for r in results:
        if r[0] is None:
            print(f"  {r[3]:<20} {r[1]}")

    print("\n=== entries never closed (open at end of log) ===")
    for sym, q in sorted(open_q.items()):
        if q:
            print(f"  {sym}: {len(q)} open order(s), first at {q[0][0]} CT ({et_hhmm(q[0][0])} ET) {q[0][2]}")


if __name__ == "__main__":
    main()
