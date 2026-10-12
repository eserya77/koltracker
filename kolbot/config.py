import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MAINS_PATH = ROOT / 'mains.json'
SIDES_PATH = ROOT / 'kolsides.json'
DB_PATH = Path(os.environ.get('KOL_DB_PATH', ROOT / 'kol_tracking.sqlite3'))

# Local fan-out of the single PumpAPI stream held by automated_signals.
RELAY_URL = os.environ.get('PUMPAPI_RELAY_URL', 'ws://127.0.0.1:18739')

CHAT_ID = -1003922183481
TOPIC_TRENDING = 6
TOPIC_MAINS = 2
TOPIC_SIDES = 4
TOPIC_EVENTS = 8

# Every message with a CA gets a button to this bot.
ARCHONT_URL = 'https://t.me/archont_sol_bot?start=t_8060490164_{mint}'
ARCHONT_BUTTON_TEXT = '⚡ Trade on Archont'

# Telegram allows ~20 messages per minute in a single group.
SEND_INTERVAL_SECONDS = 3.1
# Messages waiting longer than this are dropped instead of posted late.
MAX_MESSAGE_AGE_SECONDS = 15 * 60

TRENDING_MIN_KOLS = 2
TRENDING_STOP_DRAWDOWN = 0.70

DEXSCREENER_POLL_SECONDS = 20
SOL_PRICE_POLL_SECONDS = 60
# Tokens bought by a KOL within this window are watched for DexScreener events.
DEX_WATCH_SECONDS = 48 * 3600

WSOL = 'So11111111111111111111111111111111111111112'
NATIVE_SOL = 'So11111111111111111111111111111111111111111'
USD_QUOTES = {
    'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',  # USDC
    'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB',  # USDT
}


def load_env(path=ROOT / '.env'):
    """Fill os.environ from a KEY=VALUE file without overriding real env vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
