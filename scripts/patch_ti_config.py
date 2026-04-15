"""One-shot script: insert Trade Ideas config block into engine/config.py."""
import pathlib

cfg = pathlib.Path(__file__).parent.parent / "engine" / "config.py"
content = cfg.read_text(encoding="utf-8")

MARKER = "SENTIMENT_BULLISH_THRESHOLD = 0.6\n"
if "USE_TRADEIDEAS_DISCOVERY" in content:
    print("Already patched — skipping.")
    raise SystemExit(0)

pos = content.find(MARKER)
if pos == -1:
    print("ERROR: marker not found in config.py")
    raise SystemExit(1)
pos += len(MARKER)

block = (
    "\n"
    "# Trade Ideas Discovery\n"
    "# Scrapes TIPro highshortfloat + marketscope360 with Selenium.\n"
    "# Requires: pip install selenium webdriver-manager pillow\n"
    "USE_TRADEIDEAS_DISCOVERY      = __import__('os').getenv('USE_TRADEIDEAS_DISCOVERY', 'false').lower() == 'true'\n"
    "TRADEIDEAS_SCAN_INTERVAL_MIN  = 30\n"
    "TRADEIDEAS_HEADLESS           = True\n"
    "TRADEIDEAS_CHROME_PROFILE     = __import__('os').getenv('TRADEIDEAS_CHROME_PROFILE', '')\n"
    "TRADEIDEAS_UPDATE_CONFIG_FILE = False\n"
)

cfg.write_text(content[:pos] + block + content[pos:], encoding="utf-8")
print("Done — Trade Ideas config block inserted.")
