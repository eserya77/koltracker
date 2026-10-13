"""Minimal async Telegram Bot API client shared by the KOL bot and Spectral."""

import asyncio
import logging

import httpx

from .config import ARCHON_BUTTON_TEXT, ARCHON_URL

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, method, code, description):
        super().__init__(f'{method}: {code} {description}')
        self.code = code
        self.description = description


def archon_keyboard(mint):
    return {'inline_keyboard': [[{'text': ARCHON_BUTTON_TEXT, 'url': ARCHON_URL.format(mint=mint)}]]}


class BotApi:
    def __init__(self, token, timeout=15):
        self.token = token
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self._client.aclose()

    async def call(self, method, payload=None, attempts=5, timeout=None):
        """Calls a Bot API method, retrying network errors and 429s. Raises TelegramError."""
        url = f'https://api.telegram.org/bot{self.token}/{method}'
        last_error = None
        for attempt in range(attempts):
            try:
                kwargs = {'timeout': timeout} if timeout else {}
                resp = await self._client.post(url, json=payload or {}, **kwargs)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = TelegramError(method, None, type(exc).__name__)
                log.warning('Telegram %s failed (%s), attempt %d', method, type(exc).__name__, attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if data.get('ok'):
                return data.get('result')
            retry_after = (data.get('parameters') or {}).get('retry_after')
            if data.get('error_code') == 429 and retry_after:
                log.warning('Telegram rate limited on %s, sleeping %ss', method, retry_after)
                await asyncio.sleep(float(retry_after) + 0.5)
                continue
            raise TelegramError(method, data.get('error_code'), data.get('description'))
        raise last_error or TelegramError(method, 429, 'too many retries')

    async def send_message(self, chat_id, text, mint=None, thread_id=None, reply_markup=None):
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'link_preview_options': {'is_disabled': True},
        }
        if thread_id is not None:
            payload['message_thread_id'] = thread_id
        if reply_markup is None and mint:
            reply_markup = archon_keyboard(mint)
        if reply_markup:
            payload['reply_markup'] = reply_markup
        return await self.call('sendMessage', payload)
