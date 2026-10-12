# koltracker

Telegram bot that tracks KOL main and side wallets from the local PumpAPI relay (`ws://127.0.0.1:18739`, provided by automated_signals) and posts to the group topics: Trending, Main wallets, Side wallets and Events.

```
python -m kolbot.main            # live
python -m kolbot.main --dry-run  # log messages instead of sending
python -m pytest -q
```

Requires `TELEGRAM_BOT_TOKEN` in `.env`. Optional: `KOL_SALT_SECRET`.
