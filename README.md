# koltracker

Telegram bot that tracks KOL main and side wallets from the local PumpAPI relay (`ws://127.0.0.1:18739`, provided by automated_signals) and posts to the group topics: Trending, Main wallets, Side wallets and Events.

```
python -m kolbot.main            # live
python -m kolbot.main --dry-run  # log messages instead of sending
python -m pytest -q
```

Requires `TELEGRAM_BOT_TOKEN` in `.env`. Optional: `KOL_SALT_SECRET`, and `SPECTRAL_BOT_TOKEN` to run Spectral.

Spectral is a second bot in the same process. Each user gets the trending tokens that have been in trending
for at least their minimum time (default 10 min) with a multiplier since tracking started inside their range,
and then every event of those tokens (KOL buys, migration, liquidity, DEX paid, CTO, boosts, ads).
Users can also filter by USD market cap; every range can be open on either side.
Filters are typed: tap a button in `/settings` and send the value, or use `/time 10`, `/range 0.7-2`,
`/mcap 20k-100k` (`-100k` = up to, `20k-` = or more, `any` = no limit). Tokens that already match when a
user starts or changes filters are not sent (no burst); `/show` lists them, 5 per page. `/venues` picks
the venues (Pumpfun, PumpSwap, Raydium, Meteora, Orca…) a token must trade on. Events and KOL buys for a
received token keep arriving only while it is still in trending and inside the user's ranges.
