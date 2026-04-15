"""
Probe Trade Ideas DOM to figure out how ticker symbols are embedded.
Run once to diagnose, then we update the main extractor.
"""
import re
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

opts = Options()
opts.add_argument("--start-maximized")
opts.add_argument("--disable-blink-features=AutomationControlled")
opts.add_experimental_option("excludeSwitches", ["enable-automation"])
svc    = Service(ChromeDriverManager().install())
driver = webdriver.Chrome(service=svc, options=opts)

for url, label in [
    ("https://www.trade-ideas.com/TIPro/highshortfloat/", "high_short_float"),
    ("https://www.trade-ideas.com/TIPro/marketscope360/", "market_scope"),
]:
    print(f"\n{'='*60}")
    print(f"URL: {url}")
    driver.get(url)
    time.sleep(14)

    # --- Strategy 1: SVG <text> elements ---
    svg_texts = driver.execute_script(
        "return Array.from(document.querySelectorAll('svg text'))"
        ".map(e => e.textContent.trim()).filter(t => t.length > 0);"
    )
    print(f"SVG texts ({len(svg_texts)}): {svg_texts[:20]}")

    # --- Strategy 2: data-symbol / data-ticker attributes ---
    data_attrs = driver.execute_script("""
        var r = [];
        document.querySelectorAll('[data-symbol],[data-ticker],[data-code]').forEach(function(el){
            r.push(el.getAttribute('data-symbol') || el.getAttribute('data-ticker') || el.getAttribute('data-code'));
        });
        return r;
    """)
    print(f"data-symbol/ticker attrs ({len(data_attrs)}): {data_attrs[:20]}")

    # --- Strategy 3: scan all element text for uppercase 1-5 char words ---
    all_text = driver.execute_script(
        "return document.body.innerText;"
    )
    IGNORE = {"A","AND","OR","NOT","THE","FOR","ALL","NEW","NO","PM","AM","NA",
              "GO","BE","IN","ON","TO","AT","BY","IF","IS","IT","MIN","RACE",
              "MON","TUE","WED","THU","FRI","SAT","SUN","USD","EST","ETF",
              "HIGH","LOW","BUY","SELL","OPEN","CLOSE","POST","PRE","MARKET",
              "PRICE","FLOAT","SHORT","CHANGE","VOLUME","SCAN","TRADE","IDEAS","$DJI","$SPX","$NDX"}
    tickers = []
    seen = set()
    for m in re.finditer(r'\b([A-Z]{2,5})\b', all_text):
        t = m.group(1)
        if t not in IGNORE and t not in seen:
            seen.add(t)
            tickers.append(t)
    print(f"Body text uppercase words ({len(tickers)}): {tickers[:30]}")

    # --- Strategy 4: JSON patterns in page source ---
    src = driver.page_source
    pat_sym  = re.compile(r'"symbol"\s*:\s*"([A-Z]{1,5})"')
    pat_tick = re.compile(r'"ticker"\s*:\s*"([A-Z]{1,5})"')
    pat_code = re.compile(r'"code"\s*:\s*"([A-Z]{1,5})"')
    json_syms  = list(dict.fromkeys(pat_sym.findall(src)))
    json_ticks = list(dict.fromkeys(pat_tick.findall(src)))
    json_codes = list(dict.fromkeys(pat_code.findall(src)))
    print(f'JSON "symbol" ({len(json_syms)}): {json_syms[:20]}')
    print(f'JSON "ticker" ({len(json_ticks)}): {json_ticks[:20]}')
    print(f'JSON "code"   ({len(json_codes)}): {json_codes[:20]}')

    # --- Strategy 5: canvas / React root innerHTML hint ---
    canvas_count = driver.execute_script("return document.querySelectorAll('canvas').length;")
    print(f"Canvas elements: {canvas_count}")
    react_root = driver.execute_script(
        "var r = document.querySelector('#root,#app,[data-reactroot]');"
        "return r ? r.childElementCount : -1;"
    )
    print(f"React root child count: {react_root}")

driver.quit()
print("\nDone.")
