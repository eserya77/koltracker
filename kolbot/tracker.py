"""Turns PumpAPI stream events into DB rows and Telegram messages."""

import asyncio
import json
import logging
import time

from . import formatting as fmt
from .config import (DEX_WATCH_SECONDS, NATIVE_SOL, TOPIC_EVENTS, TOPIC_MAINS, TOPIC_SIDES,
                     TOPIC_TRENDING, TRENDING_MIN_KOLS, TRENDING_MIN_MCAP_USD, USD_QUOTES, WSOL)
from .db import now_ms
from .dexscreener import feed_dedupe_key
from .trending import TrendState, observe, trending_base

log = logging.getLogger(__name__)

TRADE_ACTIONS = {'buy', 'sell', 'create'}
LIQUIDITY_ACTIONS = {'add', 'remove'}
SOL_QUOTES = {WSOL, NATIVE_SOL}


def _float(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Tracker:
    def __init__(self, store, kols, wallets, out, dex):
        self.store = store
        self.kols = kols
        self.wallets = wallets
        self.out = out
        self.dex = dex
        self.spectral = None
        self.sol_usd = None
        self.trends = {}
        self.kol_mints = store.kol_mints()
        self.wallet_mints = {}
        self._meta = {}
        self._tasks = set()
        for row in store.trending_rows():
            token = store.token(row['mint'])
            self.trends[row['mint']] = TrendState(
                row['mint'], row['base_mcap_sol'], row['ath_mcap_sol'], row['entered_ts'],
                row['last_mult'], True, row['entry_mcap_sol'], row['last_mcap_sol'],
                token['last_venue'] if token else None)
        for mint in self.kol_mints:
            for row in store.token_positions(mint):
                self.wallet_mints.setdefault(row['wallet'], set()).add(mint)
        self.stats = {'events': 0, 'kol_trades': 0}

    # --- conversions --------------------------------------------------------
    def to_sol(self, amount, quote_mint):
        amount = _float(amount)
        if amount is None:
            return None
        if quote_mint in SOL_QUOTES:
            return amount
        if quote_mint in USD_QUOTES and self.sol_usd:
            return amount / self.sol_usd
        return None

    def mcap_sol(self, event):
        return self.to_sol(event.get('marketCapQuote'), event.get('quoteMint'))

    def min_trend_mcap_sol(self):
        return TRENDING_MIN_MCAP_USD / self.sol_usd if self.sol_usd else None

    # --- stream entry point -------------------------------------------------
    def handle_raw(self, raw):
        data = json.loads(raw)
        for event in data if isinstance(data, list) else (data,):
            if isinstance(event, dict):
                self.stats['events'] += 1
                try:
                    self.handle_event(event)
                except Exception:
                    log.exception('Failed to process event %s', event.get('signature'))

    def handle_event(self, event):
        action = event.get('action')
        mint = event.get('mint')
        ts = int(event.get('timestamp') or now_ms())

        kol_legs = [b for b in event.get('breakdown') or () if b.get('trader') in self.wallets]
        if action in TRADE_ACTIONS:
            for leg in kol_legs:
                if leg.get('action') in ('buy', 'sell'):
                    self.on_kol_trade(event, leg, ts)
            if action == 'create' and event.get('txSigner') in self.wallets:
                self.on_kol_create(event, ts)
        elif action in LIQUIDITY_ACTIONS:
            for leg in kol_legs:
                if leg.get('action') in LIQUIDITY_ACTIONS:
                    self.on_kol_liquidity(event, leg, ts)
        elif action == 'migrate' and mint in self.kol_mints:
            self.on_migration(event, ts)

        self.update_balances(event)
        if mint in self.trends and action in TRADE_ACTIONS:
            self.trends[mint].venue = event.get('pool') or self.trends[mint].venue
            self.on_trend_price(mint, self.mcap_sol(event), ts)

    def update_balances(self, event):
        post = event.get('postBalances')
        if not isinstance(post, dict):
            return
        for wallet, balances in post.items():
            mints = self.wallet_mints.get(wallet)
            if not mints or not isinstance(balances, dict):
                continue
            for mint in mints.intersection(balances):
                balance = _float(balances[mint])
                if balance is not None:
                    self.store.set_balance(wallet, mint, balance)

    # --- KOL trades ---------------------------------------------------------
    def on_kol_trade(self, event, leg, ts):
        mint = event.get('mint')
        wallet = self.wallets[leg['trader']]
        side = leg['action']
        sol = self.to_sol(leg.get('quoteAmount'), event.get('quoteMint'))
        tokens = _float(leg.get('tokenAmount'))
        if not mint or sol is None or tokens is None:
            return
        mcap = self.mcap_sol(event)
        venue = event.get('pool')
        if not self.store.record_trade(event.get('signature'), wallet.address, mint, side,
                                       sol, tokens, mcap, venue, ts):
            return
        self.stats['kol_trades'] += 1
        self.store.upsert_token(mint, ts, event.get('symbol'), event.get('name'), mcap, venue)
        if event.get('symbol'):
            self._meta[mint] = {'symbol': event.get('symbol'), 'name': event.get('name')}
        self.kol_mints.add(mint)
        self.wallet_mints.setdefault(wallet.address, set()).add(mint)
        log.info('KOL %s %s %s %.3f SOL on %s', wallet.label, side, mint, sol, venue)
        if side != 'buy':
            return
        self.maybe_start_trending(mint, mcap, ts)
        trade = {'mint': mint, 'wallet': wallet.address, 'sol': sol, 'mcap_sol': mcap,
                 'venue': venue, 'signature': event.get('signature'), 'ts': ts}
        self.spawn(self.announce_buy(wallet, trade))

    async def announce_buy(self, wallet, trade):
        mint = trade['mint']
        meta, paid = await asyncio.gather(self.token_meta(mint), self.dex.dex_paid(mint))
        kol = self.kols[wallet.kol_id]
        kol_rows = self.store.kol_positions(kol.id, mint)
        text = fmt.buy(kol, wallet, trade, meta, kol_rows, self.sol_usd, paid, trade['ts'])
        self.publish(TOPIC_MAINS if wallet.role == 'main' else TOPIC_SIDES, text, mint, forward=False)
        if self.spectral:
            self.spectral.on_kol_buy(kol, wallet, trade, meta, paid)

    async def token_meta(self, mint):
        meta = self._meta.get(mint)
        if meta:
            return meta
        row = self.store.token(mint)
        if row and row['symbol']:
            meta = {'symbol': row['symbol'], 'name': row['name']}
        else:
            meta = await self.dex.token_meta(mint)
            if meta:
                self.store.upsert_token(mint, now_ms(), meta.get('symbol'), meta.get('name'))
        if meta:
            self._meta[mint] = meta
        return meta

    # --- trending -----------------------------------------------------------
    def maybe_start_trending(self, mint, mcap, ts):
        if mint in self.trends or self.store.is_trending_known(mint):
            return
        positions = self.store.token_positions(mint)
        if len({p['kol_id'] for p in positions}) < TRENDING_MIN_KOLS:
            return
        base = trending_base(positions)
        if not base:
            return
        current = mcap or base
        floor = self.min_trend_mcap_sol()
        if floor and current < floor:
            return
        if self.store.start_trending(mint, base, current, ts):
            self.trends[mint] = TrendState(mint, base, max(base, current), ts,
                                           entry=current, mcap=current, venue=self.store.token(mint)['last_venue'])
            log.info('TRENDING START %s base %.2f SOL', mint, base)
            self.on_trend_price(mint, mcap, ts)

    def on_trend_price(self, mint, mcap, ts):
        state = self.trends[mint]
        update = observe(state, mcap, self.min_trend_mcap_sol())
        if update is None:
            return
        if update.kind == 'stop':
            self.store.stop_trending(mint, state.ath, ts)
            del self.trends[mint]
            log.info('TRENDING STOP %s at %.2f SOL (ATH %.2f)', mint, mcap, state.ath)
            return
        self.store.update_trending(mint, state.ath, state.last_mult, state.mcap)
        self.store.record_trending_alert(mint, update.mult, update.mcap, ts)
        log.info('TRENDING %s %sx', mint, update.mult)
        self.spawn(self.announce_trend(mint, state, update, ts))

    async def announce_trend(self, mint, state, update, ts):
        meta = await self.token_meta(mint)
        self.publish(TOPIC_TRENDING, fmt.trending_step(mint, meta, state, update,
                                                        self.kol_names(mint), self.sol_usd, ts), mint)

    def persist_trends(self):
        for state in self.trends.values():
            self.store.update_trending(state.mint, state.ath, state.last_mult, state.mcap)

    # --- events -------------------------------------------------------------
    def on_kol_create(self, event, ts):
        mint = event.get('mint')
        wallet = self.wallets[event['txSigner']]
        if not mint or not self.store.record_event(mint, 'create', f'create:{mint}',
                                                   {'wallet': wallet.address}, ts):
            return
        meta = {'symbol': event.get('symbol'), 'name': event.get('name')}
        self.publish(TOPIC_EVENTS, fmt.kol_create(self.kols[wallet.kol_id], wallet, mint, meta,
                                                   event, self.sol_usd, self.mcap_sol(event)), mint)

    def on_kol_liquidity(self, event, leg, ts):
        mint = event.get('mint')
        wallet = self.wallets[leg['trader']]
        action = leg['action']
        sol = self.to_sol(leg.get('quoteAmount'), event.get('quoteMint'))
        tokens = _float(leg.get('tokenAmount'))
        if not mint or not self.store.record_liquidity(
                event.get('signature'), wallet.address, mint, action, sol, tokens,
                event.get('pool'), event.get('poolId'), ts):
            return
        log.info('KOL %s liquidity %s on %s', wallet.label, action, mint)
        self.spawn(self.announce_liquidity(wallet, mint, action, sol, tokens, event))

    async def announce_liquidity(self, wallet, mint, action, sol, tokens, event):
        meta = await self.token_meta(mint)
        self.publish(TOPIC_EVENTS, fmt.liquidity(self.kols[wallet.kol_id], wallet, mint, meta,
                                                  action, sol, tokens, event), mint)

    def on_migration(self, event, ts):
        mint = event['mint']
        if not self.store.record_event(mint, 'migrate', f'migrate:{mint}',
                                       {'pool': event.get('pool'), 'poolId': event.get('poolId'),
                                        'signature': event.get('signature')}, ts):
            return
        log.info('MIGRATION %s -> %s', mint, event.get('pool'))
        self.spawn(self.announce_migration(mint, event, self.mcap_sol(event)))

    async def announce_migration(self, mint, event, mcap):
        meta = await self.token_meta(mint)
        self.publish(TOPIC_EVENTS, fmt.migration(mint, meta, event, mcap, self.sol_usd,
                                                  self.kol_names(mint)), mint)

    async def poll_dexscreener(self, interval):
        while True:
            try:
                watched = self.store.recent_kol_mints(now_ms() - DEX_WATCH_SECONDS * 1000)
                if watched:
                    for type_ in ('dex_paid', 'cto', 'boost', 'ad'):
                        for item in await self.dex.feed(type_):
                            mint = item['tokenAddress']
                            if mint in watched:
                                await self.on_dex_event(type_, mint, item)
            except Exception:
                log.exception('DexScreener poll failed')
            await asyncio.sleep(interval)

    async def on_dex_event(self, type_, mint, item):
        if not self.store.record_event(mint, type_, feed_dedupe_key(type_, item), item, now_ms()):
            return
        if type_ == 'dex_paid':
            self.dex.mark_paid(mint)
        meta = await self.token_meta(mint)
        log.info('DEX %s %s', type_, mint)
        self.publish(TOPIC_EVENTS, fmt.dex_event(type_, mint, meta, item, self.kol_names(mint)), mint)

    async def poll_sol_price(self, interval):
        while True:
            try:
                price = await self.dex.sol_usd()
                if price:
                    self.sol_usd = price
            except Exception as exc:
                log.warning('SOL price update failed: %s', exc)
            await asyncio.sleep(interval)

    # --- helpers ------------------------------------------------------------
    def publish(self, topic, text, mint, forward=True):
        """Posts to the KOL group and forwards events to Spectral (never trending alerts)."""
        self.out.post(topic, text, mint)
        if self.spectral and forward and topic != TOPIC_TRENDING:
            self.spectral.forward(mint, text)

    def kol_names(self, mint):
        return sorted({self.kols[p['kol_id']].name for p in self.store.token_positions(mint)})

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error('Background task failed', exc_info=task.exception())


def log_stats_line(tracker, started):
    return (f'events={tracker.stats["events"]} kol_trades={tracker.stats["kol_trades"]} '
            f'trending={len(tracker.trends)} queue={tracker.out.queue.qsize()} '
            f'sol_usd={tracker.sol_usd} uptime={int(time.monotonic() - started)}s')
