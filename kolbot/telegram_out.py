"""Throttled Telegram Bot API sender for the forum topics."""

import asyncio
import logging
import time

import httpx

from .config import (ARCHONT_BUTTON_TEXT, ARCHONT_URL, CHAT_ID, MAX_MESSAGE_AGE_SECONDS,
                     SEND_INTERVAL_SECONDS)

log = logging.getLogger(__name__)


class TelegramOut:
    def __init__(self, token, chat_id=CHAT_ID, dry_run=False, interval=SEND_INTERVAL_SECONDS):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.interval = interval
        self.queue = asyncio.Queue()
        self._client = httpx.AsyncClient(timeout=15)

    def post(self, topic_id, text, mint=None):
        self.queue.put_nowait((time.monotonic(), topic_id, text, mint))

    async def close(self):
        await self._client.aclose()

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
            await self._send(topic_id, text, mint)
            await asyncio.sleep(self.interval)

    async def _send(self, topic_id, text, mint=None):
        payload = {
            'chat_id': self.chat_id,
            'message_thread_id': topic_id,
            'text': text,
            'parse_mode': 'HTML',
            'link_preview_options': {'is_disabled': True},
        }
        if mint:
            payload['reply_markup'] = {'inline_keyboard': [[
                {'text': ARCHONT_BUTTON_TEXT, 'url': ARCHONT_URL.format(mint=mint)}]]}
        url = f'https://api.telegram.org/bot{self.token}/sendMessage'
        for attempt in range(5):
            try:
                resp = await self._client.post(url, json=payload)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning('Telegram send failed (%s), attempt %d', type(exc).__name__, attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if data.get('ok'):
                return True
            retry_after = (data.get('parameters') or {}).get('retry_after')
            if resp.status_code == 429 and retry_after:
                log.warning('Telegram rate limited, sleeping %ss', retry_after)
                await asyncio.sleep(float(retry_after) + 0.5)
                continue
            log.error('Telegram rejected message for topic %s: %s', topic_id, data.get('description'))
            return False
        return False
