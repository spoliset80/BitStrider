from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    ticker:      str
    action:      str               # BUY | SELL
    option_type: Optional[str]    # CALL | PUT | None (equity)
    strike:      Optional[float]
    expiry_str:  Optional[str]    # raw string from message
    expiry_date: Optional[date]   # resolved date
    occ:         Optional[str]    # e.g. PANW  270115C00265000
    entry_price: Optional[float]
    confidence:  int
    stop:        Optional[float] = None
    targets:     list[float] = field(default_factory=list)
    notes:       list[str] = field(default_factory=list)  # reasoning trace

    @property
    def is_option(self) -> bool:
        return bool(self.occ)

    def __str__(self) -> str:
        if self.is_option:
            return (f"{self.action} {self.ticker} {self.option_type} "
                    f"${self.strike} exp={self.expiry_str} @ {self.entry_price} "
                    f"[conf={self.confidence}%] OCC={self.occ}")
        return (f"{self.action} {self.ticker} equity "
                f"@ {self.entry_price} [conf={self.confidence}%]")


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}

def _third_friday(yr: int, mon: int) -> date:
    d = date(yr, mon, 1)
    first_fri = d + timedelta(days=(4 - d.weekday()) % 7)
    return first_fri + timedelta(weeks=2)

def _next_monthly() -> date:
    today = date.today()
    mon = today.month if today.day < 15 else (today.month % 12) + 1
    yr  = today.year if mon >= today.month else today.year + 1
    return _third_friday(yr, mon)

def this_week_friday() -> date:
    """Nearest upcoming Friday (today if today is Friday)."""
    today = date.today()
    days_ahead = (4 - today.weekday()) % 7
    return today + timedelta(days=days_ahead)

_this_week_friday = this_week_friday

# Natural: "July 24th", "Jan-2027", "Mar 2027", "Dec-2027"
_RE_NAT = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[.\-]?\s*"
    r"(\d{1,4})(?:st|nd|rd|th)?(?:[,\s]+(\d{2,4}))?",
    re.I,
)
# Slash: 7/24, 7/24/26
_RE_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")

_RE_RELATIVE = re.compile(r"\b(this\s+week|next\s+week|eow|end\s+of\s+week|tomorrow)\b", re.I)

def resolve_expiry(text: str) -> Optional[date]:
    m = _RE_RELATIVE.search(text)
    if m:
        phrase = re.sub(r"\s+", " ", m.group(1).lower())
        today = date.today()
        if phrase == "tomorrow":
            return today + timedelta(days=1)
        if phrase == "next week":
            return _this_week_friday() + timedelta(weeks=1)
        # "this week", "eow", "end of week"
        return _this_week_friday()

    m = _RE_NAT.search(text)
    if m:
        mon    = _MONTHS[m.group(1).lower()[:3]]
        raw2   = int(m.group(2))
        yr_raw = m.group(3)
        if raw2 > 31:            # "Jan-2027" → month + year only → 3rd Friday
            yr = raw2 + 2000 if raw2 < 100 else raw2
            return _third_friday(yr, mon)
        day = raw2
        if yr_raw:
            yr = int(yr_raw); yr = yr + 2000 if yr < 100 else yr
        else:
            today = date.today()
            yr = today.year
            if date(yr, mon, day) < today:
                yr += 1
        return date(yr, mon, day)
    
    m = _RE_SLASH.search(text)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        yr_raw = m.group(3)
        if yr_raw:
            yr = int(yr_raw); yr = yr + 2000 if yr < 100 else yr
        else:
            today = date.today()
            yr = today.year
            if date(yr, mon, day) < today:
                yr += 1
        return date(yr, mon, day)
    return None

def build_occ(ticker: str, expiry: date, opt_type: str, strike: float) -> str:
    cp = "C" if opt_type == "CALL" else "P"
    # Alpaca OCC format: no spaces — ticker immediately followed by date/type/strike
    yymmdd = f"{expiry.year % 100:02d}{expiry.month:02d}{expiry.day:02d}"
    return f"{ticker.upper()}{yymmdd}{cp}{int(round(strike * 1000)):08d}"


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_NOISE = re.compile(
    r"###.*?(?=@everyone|[A-Z]{2,5}[:\s])|" 
    r"\*+|"                                  
    r"@(?:everyone|here)\s*",
    re.I,
)

# Ticker extraction: avoid capturing numeric option styles ($42C) as tickers
_RE_SYMBOL_TAG  = re.compile(r"Symbol:\s*\$([A-Z]{1,5})\b", re.I)
_RE_DOLLAR_TKR  = re.compile(r"\$([A-Z]{1,5})\b", re.I)
_RE_COLON_TKR   = re.compile(r"^([A-Z]{1,5})\s*:", re.M)

# Action keywords
_RE_ACTION = re.compile(
    r"\b(BUY|SELL|Entered|Exited|Bought|Sold|Closed?|close|added|Out)\b", re.I
)

# Explicit "Out $TICKER calls/puts @ price" exit pattern (e.g. "Out $MSFT calls @ 13.40")
_RE_OUT_EXIT = re.compile(r"\bOut\s+\$?([A-Z]{1,5})\s+(calls?|puts?)\b", re.I)

# Option type + strike (Stricter matching to prevent overlap with standard numbers)
_RE_OPT_DOLLAR = re.compile(r"\$(\d{1,4}(?:\.\d+)?)(C|P)\b", re.I)          # $42C
_RE_OPT_BARE   = re.compile(r"\b(\d{1,4}(?:\.\d+)?)(c|p)\b(?!\d)", re.I)    # 265c
_RE_OPT_WORD_STRIKE = re.compile(
    r"\$?(\d{1,4}(?:\.\d+)?)\s*(CALLS?|PUTS?)\b", re.I
)                                                                            # $90 Calls
_RE_OPT_WORD   = re.compile(r"\b(CALLS?|PUTS?|CSP|LEAPS?)\b", re.I)         # CALLS

# Expiry phrase regex engines
_RE_EXPIRY = re.compile(
    r"(?:expiring?(?:\s+in)?|exp\.?)\s*"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[.\-]?\s*\d{1,4}(?:[,\s]+\d{2,4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?|this\s+week|next\s+week|eow|end\s+of\s+week|tomorrow)",
    re.I,
) 
_RE_MON_YEAR   = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[.\-]\s*(\d{4})\b", re.I
)
_RE_DATE_SLASH = re.compile(r"\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b")
_RE_DATE_LONG  = re.compile(
    r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:[,\s]+\d{2,4})?)\b",
    re.I,
)

_RE_PRICE = re.compile(r"(?:@|\bat)\s*\$?(\d+(?:\.\d+)?)\b", re.I)

_RE_STOP = re.compile(
    r"\bstops?\b[:\s]*(?:below|under|above|over|@|at)?\s*\$?(\d+(?:[.,]\d+)?)", re.I
)
_RE_TARGETS = re.compile(r"\btargets?\b[:\s]*(.{0,60})", re.I | re.S)
_RE_NUM = re.compile(r"\$?(\d+(?:\.\d+)?)")

_SKIP = {
    "THE","AND","FOR","ARE","BUT","NOT","YOU","ALL","CAN","WAS","ONE","OUR",
    "OUT","DAY","GET","HAS","HOW","MAY","NEW","OLD","SEE","TWO","WHO","DID",
    "LET","SAY","SHE","TOO","USE","ATM","OTM","ITM","EOD","EOW","CEO","CFO",
    "IPO","ETF","EPS","GDP","CPI","PPI","FED","SEC","FDA","BOT","TOP","LOW",
    "HIGH","TYPE","MID","LONG","TERM","SWING","STOP","SYMBOL","TARGET","WILL",
    "HAVE","BEEN","FROM","JUST","ALSO","INTO","OVER","THIS","THAT","WITH",
    "LEAPS","CALLS","PUTS","CALL","CLOSE","CLOSED","LIMIT","AUTO","BELOW",
    "ABOVE","ENTRY","ADDED","BACK","FIND","KNOW","GIVEN","ALERT",
    "TRADING","TRADE","TOMORROW","TODAY","YEAR","MONTH","WEEK",
}


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def parse_trade(raw: str) -> Optional[Trade]:
    notes: list[str] = []

    # 1. Clean noise
    text = _NOISE.sub(" ", raw).strip()
    text = re.sub(r"\s{2,}", " ", text)

    # 1b. Fast-track "Out $TICKER calls/puts @ price" exit pattern
    out_m = _RE_OUT_EXIT.search(raw)
    if out_m:
        ticker     = out_m.group(1).upper()
        opt_word   = out_m.group(2).upper()
        option_type = "PUT" if opt_word.startswith("PUT") else "CALL"
        price_m    = _RE_PRICE.search(raw)
        entry_price = float(price_m.group(1)) if price_m else None
        notes.append(f"fast-path Out exit → SELL {ticker} {option_type}")
        return Trade(
            ticker=ticker, action="SELL", option_type=option_type,
            strike=None, expiry_str=None, expiry_date=None, occ=None,
            entry_price=entry_price, confidence=90, notes=notes,
        )

    # 2. Extract option details FIRST to prevent ticker collisions
    option_type = None
    strike = None

    m = _RE_OPT_DOLLAR.search(text)
    if m:
        strike = float(m.group(1))
        option_type = "CALL" if m.group(2).upper() == "C" else "PUT"
        notes.append(f"option via $strike+cp → {option_type} ${strike}")

    if not option_type:
        m = _RE_OPT_BARE.search(text)
        if m:
            strike = float(m.group(1))
            option_type = "CALL" if m.group(2).lower() == "c" else "PUT"
            notes.append(f"option via bare strike+cp → {option_type} ${strike}")

    if not option_type:
        m = _RE_OPT_WORD_STRIKE.search(text)
        if m:
            strike = float(m.group(1))
            option_type = "PUT" if m.group(2).upper().startswith("PUT") else "CALL"
            notes.append(f"option via strike + keyword → {option_type} ${strike}")

    if not option_type:
        m = _RE_OPT_WORD.search(text)
        if m:
            w = m.group(1).upper()
            option_type = "PUT" if w.startswith("PUT") else "CALL"
            notes.append(f"option via keyword → {option_type}")

    # 3. Extract ticker (safe from reading option parameters now)
    ticker = None

    m = _RE_SYMBOL_TAG.search(text)
    if m:
        ticker = m.group(1).upper()
        notes.append(f"ticker via Symbol: tag → {ticker}")

    if not ticker:
        # Avoid picking up strings that are actually part of option parameters
        for token in _RE_DOLLAR_TKR.findall(text):
            if token.upper() not in _SKIP and not re.match(r"^\d", token):
                ticker = token.upper()
                notes.append(f"ticker via $TKR → {ticker}")
                break

    if not ticker:
        m = _RE_COLON_TKR.search(text)
        if m and m.group(1).upper() not in _SKIP:
            ticker = m.group(1).upper()
            notes.append(f"ticker via COLON → {ticker}")

    if not ticker:
        for w in re.findall(r"\b([A-Z]{2,5})\b", text):
            if w.upper() not in _SKIP:
                ticker = w.upper()
                notes.append(f"ticker via bare word → {ticker}")
                break

    if not ticker:
        return None

    # 4. Extract action
    action = None
    m = _RE_ACTION.search(text)
    if m:
        v = m.group(1).upper()
        action = "BUY" if v in ("BUY","ENTERED","BOUGHT","ADDED") else "SELL"
        notes.append(f"action via keyword '{m.group(1)}' → {action}")

    if not action and strike and option_type:
        action = "BUY"
        notes.append("action inferred BUY (strike present, no keyword)")

    if not action:
        return None

    # 5. Extract expiry
    expiry_str = None
    expiry_date = None

    m = _RE_EXPIRY.search(text)
    if m:
        expiry_str = m.group(1).strip()
        notes.append(f"expiry via 'expiring' phrase → {expiry_str}")

    if not expiry_str:
        m = _RE_MON_YEAR.search(text)
        if m:
            expiry_str = m.group(0).strip()
            notes.append(f"expiry via Mon-YYYY → {expiry_str}")

    if not expiry_str:
        m = _RE_DATE_LONG.search(text)
        if m:
            expiry_str = m.group(1).strip()
            notes.append(f"expiry via long date → {expiry_str}")

    if not expiry_str:
        m = _RE_DATE_SLASH.search(text)
        if m:
            expiry_str = m.group(1).strip()
            notes.append(f"expiry via slash date → {expiry_str}")

    if expiry_str:
        expiry_date = resolve_expiry(expiry_str)
        if expiry_date:
            notes.append(f"resolved expiry → {expiry_date}")

    if not expiry_date and option_type and action == "BUY":
        expiry_date = _next_monthly()
        expiry_str  = expiry_date.strftime("%Y-%m-%d")
        notes.append(f"expiry defaulted to next monthly → {expiry_date}")

    # 6. Build OCC
    occ = None
    if option_type and strike and expiry_date:
        occ = build_occ(ticker, expiry_date, option_type, strike)
        notes.append(f"OCC → {occ}")

    # 7. Extract entry price
    entry_price = None
    m = _RE_PRICE.search(text)
    if m:
        entry_price = float(m.group(1))
        notes.append(f"price → {entry_price}")

    # 8. Extract stop / targets (used for risk-reward scoring)
    stop = None
    m = _RE_STOP.search(text)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            stop = float(raw)
            notes.append(f"stop → {stop}")
        except ValueError:
            pass

    targets: list[float] = []
    m = _RE_TARGETS.search(text)
    if m:
        targets = [float(n) for n in _RE_NUM.findall(m.group(1))][:3]
        if targets:
            notes.append(f"targets → {targets}")

    # 9. Confidence score
    score = 50
    if strike:      score += 20
    if expiry_date: score += 15
    if entry_price: score += 10
    if action:      score +=  5
    score = min(score, 100)

    return Trade(
        ticker=ticker,
        action=action,
        option_type=option_type,
        strike=strike,
        expiry_str=expiry_str,
        expiry_date=expiry_date,
        occ=occ,
        entry_price=entry_price,
        confidence=score,
        stop=stop,
        targets=targets,
        notes=notes,
    )