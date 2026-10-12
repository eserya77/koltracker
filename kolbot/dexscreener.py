"""DexScreener REST client (token metadata, SOL price, paid orders, latest feeds)."""

import asyncio
import logging
import time

import httpx

from .config import WSOL

log = logging.getLogger(__name__)

BASE = 'https://api.dexscreener.com'

# feed type -> endpoint; each returns the latest items for every chain.
FEEDS = {
    'dex_paid': '/token-profiles/latest/v1',
    'cto': '/community-takeovers/latest/v1',
    'boost': '/token-boosts/latest/v1',
    'ad': '/ads/latest/v1',
}


def _items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('data', 'items', 'pairs', 'orders'):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def feed_dedupe_key(type_, item):
    mint = item.get('tokenAddress')
    if type_ == 'boost':
        return f'boost:{mint}:{item.get("totalAmount")}'
    if type_ == 'ad':
        return f'ad:{mint}:{item.get("date") or item.get("id")}'
    return f'{type_}:{mint}'


class DexScreener:
    def __init__(self):
        self._client = httpx.AsyncClient(base_url=BASE, timeout=10)
        self._orders_sem = asyncio.Semaphore(2)
        self._paid_cache = {}

    async def close(self):
        await self._client.aclose()

    async def _get(self, path):
        resp = await self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    async def token_meta(self, mint):
        """Returns {'symbol', 'name'} from the token's most liquid pair, or None."""
        try:
            pairs = _items(await self._get(f'/tokens/v1/solana/{mint}'))
        except (httpx.HTTPError, ValueError) as exc:
            log.debug('token_meta %s failed: %s', mint, exc)
            return None
        for pair in sorted(pairs, key=lambda p: (p.get('liquidity') or {}).get('usd') or 0, reverse=True):
            for side in ('baseToken', 'quoteToken'):
                token = pair.get(side) or {}
                if token.get('address') == mint:
                    return {'symbol': token.get('symbol'), 'name': token.get('name')}
        return None

    async def sol_usd(self):
        pairs = _items(await self._get(f'/tokens/v1/solana/{WSOL}'))
        best = max(pairs, key=lambda p: (p.get('liquidity') or {}).get('usd') or 0, default=None)
        if not best or (best.get('baseToken') or {}).get('address') != WSOL:
            return None
        return float(best['priceUsd'])

    async def dex_paid(self, mint, max_age=600):
        """True when an approved token profile order exists; None if unknown."""
        cached = self._paid_cache.get(mint)
        if cached and (cached[1] or time.monotonic() - cached[0] < max_age):
            return cached[1]
        async with self._orders_sem:
            try:
                orders = _items(await self._get(f'/orders/v1/solana/{mint}'))
            except (httpx.HTTPError, ValueError) as exc:
                log.debug('orders %s failed: %s', mint, exc)
                return None
        paid = any(o.get('type') == 'tokenProfile' and o.get('status') == 'approved' for o in orders)
        self._paid_cache[mint] = (time.monotonic(), paid)
        return paid

    def mark_paid(self, mint):
        self._paid_cache[mint] = (time.monotonic(), True)

    async def feed(self, type_):
        try:
            items = _items(await self._get(FEEDS[type_]))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning('DexScreener %s feed failed: %s', type_, exc)
            return []
        return [i for i in items if i.get('chainId') == 'solana' and i.get('tokenAddress')]
