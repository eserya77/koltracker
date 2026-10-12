"""Entry point: python -m kolbot.main [--dry-run] [--record FILE] [--replay FILE]"""

import argparse
import asyncio
import logging
import os
import time

import websockets

from . import config
from .db import Store
from .dexscreener import DexScreener
from .kols import load_registry
from .telegram_out import TelegramOut
from .tracker import Tracker, log_stats_line

log = logging.getLogger('kolbot')


async def consume_relay(url, on_raw, recorder=None):
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, max_size=None, ping_interval=20, ping_timeout=20,
                                          close_timeout=1, compression=None) as ws:
                log.info('Connected to PumpAPI relay %s', url)
                backoff = 1
                async for raw in ws:
                    if recorder:
                        recorder.write(raw if isinstance(raw, str) else raw.decode())
                        recorder.write('\n')
                    on_raw(raw)
            log.warning('PumpAPI relay closed the connection')
        except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError) as exc:
            log.warning('PumpAPI relay unavailable: %s', exc)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


def handle_raw_safely(tracker):
    def on_raw(raw):
        try:
            tracker.handle_raw(raw)
        except ValueError:
            log.warning('Skipping non-JSON frame')
    return on_raw


async def replay(path, on_raw):
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            if line.strip():
                on_raw(line)
                await asyncio.sleep(0)
    log.info('Replay of %s finished', path)


async def periodic(interval, fn):
    while True:
        await asyncio.sleep(interval)
        fn()


async def run(args):
    config.load_env()
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token and not args.dry_run:
        raise SystemExit('TELEGRAM_BOT_TOKEN is not set')

    kols, wallets = load_registry(config.MAINS_PATH, config.SIDES_PATH)
    store = Store(config.DB_PATH)
    store.sync_registry(kols, wallets)
    log.info('Loaded %d KOLs, %d wallets (%d main / %d side)', len(kols), len(wallets),
             sum(w.role == 'main' for w in wallets.values()),
             sum(w.role == 'side' for w in wallets.values()))

    out = TelegramOut(token, dry_run=args.dry_run)
    dex = DexScreener()
    tracker = Tracker(store, kols, wallets, out, dex)
    on_raw = handle_raw_safely(tracker)
    started = time.monotonic()

    recorder = open(args.record, 'a', encoding='utf-8') if args.record else None
    tasks = [
        asyncio.create_task(out.run()),
        asyncio.create_task(tracker.poll_sol_price(config.SOL_PRICE_POLL_SECONDS)),
        asyncio.create_task(periodic(30, tracker.persist_trends)),
        asyncio.create_task(periodic(60, lambda: log.info(log_stats_line(tracker, started)))),
    ]
    if args.replay:
        source = asyncio.create_task(replay(args.replay, on_raw))
    else:
        tasks.append(asyncio.create_task(
            tracker.poll_dexscreener(config.DEXSCREENER_POLL_SECONDS)))
        source = asyncio.create_task(consume_relay(config.RELAY_URL, on_raw, recorder))
    try:
        await source
        if args.replay:
            while out.queue.qsize() or tracker._tasks:
                await asyncio.sleep(0.5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tracker.persist_trends()
        await out.close()
        await dex.close()
        store.close()
        if recorder:
            recorder.close()


def main():
    parser = argparse.ArgumentParser(description='KOL tracking Telegram bot')
    parser.add_argument('--dry-run', action='store_true', help='log messages instead of sending')
    parser.add_argument('--record', help='append raw relay frames to this file')
    parser.add_argument('--replay', help='process frames from a recorded file instead of the relay')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
