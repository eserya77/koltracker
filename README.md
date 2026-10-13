# koltracker

Telegram bot that tracks KOL main and side wallets from the local PumpAPI relay (`ws://127.0.0.1:18739`, provided by automated_signals) and posts to the group topics: Trending, Main wallets, Side wallets and Events.

```
python -m kolbot.main            # live
python -m kolbot.main --dry-run  # log messages instead of sending
python -m pytest -q
```

Requires `TELEGRAM_BOT_TOKEN` in `.env`. Optional: `KOL_SALT_SECRET`, and `SPECTRAL_BOT_TOKEN` to run Spectral.

Spectral is a second bot in the same process. Each user gets KOL tokens that fit their filters, once,
and then that token's updates while it still fits. Filters (open ranges allowed, `any` = no limit):

- `/time 10` min minutes tracked (default 10)
- `/kols 3` min distinct KOLs (default 2; 1 includes single-KOL tokens, which are tracked but never posted
  to the group)
- `/range 0.7, 2` multiplier since tracking started
- `/mcap 20k, 100k` USD market cap (`any, 100k` = up to, `20k, any` = or more)
- `/venues` and `/alerts` toggle menus (venues a token must trade on; which updates to receive)

Tokens that already fit when a user starts or changes filters are not sent (no burst); `/show` lists them,
5 per page.
