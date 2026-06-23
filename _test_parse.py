import sys; sys.path.insert(0, '.')
from scripts.discord_parser import parse_trade

tests = [
    ("1",  "@everyone ORCL: Closed at 63.70 from 41"),
    ("2",  "@everyone DELL: 407.5c at 7.30, stop below 5 auto stop"),
    ("3",  "@everyone NOW: Closed at 32.90"),
    ("4",  "@everyone DELL: Closed at 8"),
    ("5",  "@everyone ORCL: 230c at 7.80, stop below 5"),
    ("6",  "@everyone ORCL: Closed at 9.90"),
    ("7",  "@everyone AI: 12.5c expiring Dec-2027 at 4.40"),
    ("8",  "@everyone PAYC: Closed at 28.20"),
    ("9",  "@everyone CRWV: 123c at 6"),
    ("10", "@everyone CRWV: Closed at 7"),
    ("11", "@everyone ORCL: 200c was added at 41.5, now trading at 74. I have closed it at 74 from 41.5"),
    ("12", "@everyone IBM: 400c expiring Jan-2027 at 31.75"),
    ("13", "@everyone CRWV: 125c at 5.80, will close it tomorrow."),
    ("14", "@everyone CRWV: Limit to close at 6"),
    ("15", "@everyone CRWV: 123c at 5.15"),
    ("16", "@everyone INTC: 110c expiring Mar-2027 at 30.60"),
    ("17", "@everyone CRWV: 120c expiring Mar-2027 at 38"),
    ("18", "@everyone AVGO: 415c at 7"),
    ("19", "@everyone TSLA: 422.5c at 5.70"),
    ("20", "@everyone TSLA: Closed at 7.30"),
    ("21", "@everyone TSLA: 422.5c at 4.60"),
    ("22", "@everyone RDDT: 180c expiring Jul-2026 at 18.90"),
    ("23", "@everyone AVGO: Closed at 8.70"),
    ("24", "@everyone NASA: 37c expiring Mar-2027 at 10.60"),
    ("25", "@everyone IGV: 100c expiring Jul-2026 at 5.30"),
    ("26", "@everyone RDDT: Closed at 20.40"),
    ("A",  "**Entered $HIMS $42C Expiring July 24th @ 2.05, stop @ 50%**"),
    ("B",  "**@everyone Bought $HAL $37C expiring Jan-2028 @ 3.10**"),
    ("C",  "**@everyone TYPE: (Mid term Swing) Symbol: $ZETA Entry:  Entered @ 47.50**"),
    ("D",  "PANW: 265c expiring Jan-2027 at 45.30"),
    ("E",  "NOW: 95c expiring Mar-2027 at 19.90"),
]

ok = fail = 0
for num, msg in tests:
    t = parse_trade(msg)
    if t and t.ticker and t.action:
        ok += 1
        occ_part = f"  OCC={t.occ}" if t.occ else ""
        print(f"[OK] #{num:2s} {str(t)}{occ_part}")
    else:
        fail += 1
        print(f"[FAIL] #{num:2s} '{msg[:60]}'")

print(f"\n{ok}/{ok+fail} passed")
