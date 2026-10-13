"""Throttled sender for the KOL bot's forum topics."""

import asyncio
import logging
import time

from .botapi import BotApi, TelegramError
from .config import CHAT_ID, MAX_MESSAGE_AGE_SECONDS, SEND_INTERVAL_SECONDS

log = logging.getLogger(__name__)


class TelegramOut:
    def __init__(self, token, chat_id=CHAT_ID, dry_run=False, interval=SEND_INTERVAL_SECONDS):
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.interval = interval
        self.queue = asyncio.Queue()
        self.api = BotApi(token)

    def post(self, topic_id, text, mint=None):
        self.queue.put_nowait((time.monotonic(), topic_id, text, mint))

    async def close(self):
        await self.api.close()

    async def run(self):
        while True:
            created, topic_id, text, mint = await self.queue.get()
            age = time.monotonic() - created
            if age > MAX_MESSAGE_AGE_SECONDS:
                log.warning('Dropping message for topic %s queued %.0fs ago', topic_id, age)
                continue
            if self.dry_run:
                log.info('[DRY RUN] topic %s (button: %s):\n%s', topic_id, mint, text)
                continue
            try:
                await self.api.send_message(self.chat_id, text, mint=mint, thread_id=topic_id)
            except TelegramError as exc:
                log.error('Telegram rejected message for topic %s: %s', topic_id, exc)
            await asyncio.sleep(self.interval)
