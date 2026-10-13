"""Spectral: a second bot that DMs each user the trending tokens matching their filters.

A token is sent once per user, when it has been in trending for at least the
user's minimum time, its multiplier since tracking started (current MC / MC
when trending began) is inside the user's multiplier range and its USD market
cap is inside the user's market cap range. Any range bound may be open. After
that, the user also
gets that token's events (KOL buys, migration, liquidity, DEX paid, CTO, boosts,
ads). Trending multiplier alerts are not forwarded.
"""

import asyncio
import logging
import time

from . import formatting as fmt
from .botapi import BotApi, TelegramError
from .config import (ARCHON_BUTTON_TEXT, ARCHON_URL, MAX_MESSAGE_AGE_SECONDS,
                     SPECTRAL_CHECK_SECONDS, SPECTRAL_DEFAULT_MIN_MINUTES,
                     SPECTRAL_MAX_MCAP_OPTIONS, SPECTRAL_MAX_MULT_OPTIONS,
                     SPECTRAL_MIN_MCAP_OPTIONS, SPECTRAL_MIN_MULT_OPTIONS,
                     SPECTRAL_SEND_INTERVAL_SECONDS, SPECTRAL_SHOW_LIMIT, SPECTRAL_TIME_OPTIONS)
from .db import now_ms

log = logging.getLogger(__name__)

COMMANDS = [
    {'command': 'settings', 'description': 'Change your filters'},
    {'command': 'show', 'description': 'Tokens matching your filters now'},
    {'command': 'time', 'description': 'Min minutes in trending, e.g. /time 12'},
    {'command': 'range', 'description': 'Multiplier range, e.g. /range 0.7 2'},
    {'command': 'mcap', 'description': 'Market cap range, e.g. /mcap 20k 100k'},
]
MAX_MINUTES = 24 * 60
_SUFFIXES = {'k': 1e3, 'm': 1e6, 'b': 1e9}


# --- filters -------------------------------------------------------------------
def multiplier(state):
    if not state.mcap or not state.entry:
        return None
    return state.mcap / state.entry


def _in_range(value, low, high):
    return (low is None or value >= low) and (high is None or value <= high)


def matches(user, state, now, sol_usd):
    """True when a trending token passes the user's time, multiplier and market cap filters."""
    mult = multiplier(state)
    if not state.active or mult is None:
        return False
    if now - state.entered_ts < user['min_minutes'] * 60_000:
        return False
    if not _in_range(mult, user['min_mult'], user['max_mult']):
        return False
    if user['min_mcap'] is None and user['max_mcap'] is None:
        return True
    return bool(sol_usd) and _in_range(state.mcap * sol_usd, user['min_mcap'], user['max_mcap'])


def parse_time(args):
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) > MAX_MINUTES:
        raise ValueError(f'Use /time followed by minutes between 0 and {MAX_MINUTES}, e.g. /time 12')
    return int(args[0])


def _parse_mult(value):
    if value.lower() in ('any', '-'):
        return None
    mult = float(value.lower().rstrip('x'))
    if mult <= 0:
        raise ValueError
    return mult


def parse_range(args):
    """'/range any' clears both bounds; '/range 0.7 2', '/range 1 any', '/range any 3'."""
    usage = 'Use /range MIN MAX, e.g. /range 0.7 2 · /range 1 any · /range any'
    try:
        if len(args) == 1 and args[0].lower() == 'any':
            return None, None
        if len(args) != 2:
            raise ValueError
        low, high = _parse_mult(args[0]), _parse_mult(args[1])
    except ValueError:
        raise ValueError(usage) from None
    check_range(low, high, 'multiplier')
    return low, high


def _parse_usd(value):
    """'any' -> None; '20k', '$1.5m', '100000' -> float USD."""
    value = value.lower().lstrip('$').replace(',', '')
    if value in ('any', '-'):
        return None
    factor = _SUFFIXES.get(value[-1:], 1)
    amount = float(value[:-1] if factor != 1 else value) * factor
    if amount <= 0:
        raise ValueError
    return amount


def parse_mcap(args):
    """'/mcap any' clears both bounds; '/mcap 20k 100k', '/mcap any 100k', '/mcap 20k any'."""
    usage = 'Use /mcap MIN MAX, e.g. /mcap 20k 100k · /mcap any 100k · /mcap 20k any · /mcap any'
    try:
        if len(args) == 1 and args[0].lower() == 'any':
            return None, None
        if len(args) != 2:
            raise ValueError
        low, high = _parse_usd(args[0]), _parse_usd(args[1])
    except ValueError:
        raise ValueError(usage) from None
    check_range(low, high, 'market cap')
    return low, high


def check_range(low, high, what):
    if low is not None and high is not None and low >= high:
        raise ValueError(f'The minimum {what} must be lower than the maximum.')


def _mult_label(value):
    return 'Any' if value is None else fmt.fmt_mult(value)


def _usd_label(value):
    return 'Any' if value is None else f'${fmt.fmt_num(value)}'


def _data(value):
    return 'any' if value is None else f'{value:g}'


def settings_keyboard(user):
    def button(label, data, selected):
        return {'text': f'✅ {label}' if selected else label, 'callback_data': data}

    def title(text):
        return [{'text': text, 'callback_data': 'noop'}]

    return {'inline_keyboard': [
        title('⏱ Min time in trending'),
        [button(f'{m}m', f'set:time:{m}', user['min_minutes'] == m) for m in SPECTRAL_TIME_OPTIONS],
        title('📉 Min multiplier'),
        [button(_mult_label(v), f'set:min:{_data(v)}', user['min_mult'] == v)
         for v in SPECTRAL_MIN_MULT_OPTIONS],
        title('📈 Max multiplier'),
        [button(_mult_label(v), f'set:max:{_data(v)}', user['max_mult'] == v)
         for v in SPECTRAL_MAX_MULT_OPTIONS],
        title('💰 Min market cap'),
        [button(_usd_label(v), f'set:minmc:{_data(v)}', user['min_mcap'] == v)
         for v in SPECTRAL_MIN_MCAP_OPTIONS],
        title('💰 Max market cap'),
        [button(_usd_label(v), f'set:maxmc:{_data(v)}', user['max_mcap'] == v)
         for v in SPECTRAL_MAX_MCAP_OPTIONS],
        [{'text': '🔮 Show matching tokens', 'callback_data': 'show'}],
    ]}


def show_keyboard(items):
    """One Archon button per listed token, labeled with its symbol."""
    text = ARCHON_BUTTON_TEXT.replace('Trade', 'Trade {symbol}')
    rows = [[{'text': text.format(symbol=f'${(meta or {}).get("symbol") or state.mint[:6]}'),
              'url': ARCHON_URL.format(mint=state.mint)}] for meta, state in items]
    return {'inline_keyboard': rows} if rows else None


class SpectralBot:
    def __init__(self, token, store, tracker, dry_run=False, interval=SPECTRAL_SEND_INTERVAL_SECONDS):
        self.api = BotApi(token)
        self.store = store
        self.tracker = tracker
        self.dry_run = dry_run
        self.interval = interval
        self.queue = asyncio.Queue()
        self.users = {row['chat_id']: dict(row) for row in store.spectral_users()}
        self.followers = {}
        for row in store.spectral_deliveries():
            self.followers.setdefault(row['mint'], set()).add(row['chat_id'])

    async def close(self):
        await self.api.close()

    # --- outgoing -----------------------------------------------------------
    def enqueue(self, chat_id, text, mint=None, reply_markup=None):
        self.queue.put_nowait((time.monotonic(), chat_id, text, mint, reply_markup))

    async def run_sender(self):
        while True:
            created, chat_id, text, mint, markup = await self.queue.get()
            if time.monotonic() - created > MAX_MESSAGE_AGE_SECONDS:
                continue
            if self.dry_run:
                log.info('[SPECTRAL DRY RUN] chat %s (button: %s):\n%s', chat_id, mint, text)
                continue
            await self._send(chat_id, text, mint, markup)
            await asyncio.sleep(self.interval)

    async def _send(self, chat_id, text, mint=None, markup=None):
        try:
            await self.api.send_message(chat_id, text, mint=mint, reply_markup=markup)
        except TelegramError as exc:
            if exc.code == 403 or (exc.code == 400 and 'chat not found' in (exc.description or '')):
                log.info('Spectral user %s unreachable (%s); deactivating', chat_id, exc.description)
                self.deactivate(chat_id)
            else:
                log.error('Spectral send to %s failed: %s', chat_id, exc)

    def deactivate(self, chat_id):
        self.store.spectral_deactivate(chat_id)
        if chat_id in self.users:
            self.users[chat_id]['active'] = 0

    def forward(self, mint, text):
        """Called by the tracker for every non-trending message about a token."""
        for chat_id in self.followers.get(mint, ()):
            if self.users.get(chat_id, {}).get('active'):
                self.enqueue(chat_id, text, mint)

    def follow(self, chat_id, mint, ts):
        self.store.spectral_record_delivery(chat_id, mint, ts)
        self.followers.setdefault(mint, set()).add(chat_id)

    # --- token delivery -----------------------------------------------------
    async def deliver_loop(self):
        while True:
            try:
                await self.deliver_due(now_ms())
            except Exception:
                log.exception('Spectral delivery check failed')
            await asyncio.sleep(SPECTRAL_CHECK_SECONDS)

    async def deliver_due(self, now):
        for state in list(self.tracker.trends.values()):
            followers = self.followers.get(state.mint, ())
            targets = [chat_id for chat_id, user in self.users.items()
                       if user['active'] and chat_id not in followers
                       and matches(user, state, now, self.tracker.sol_usd)]
            if not targets:
                continue
            for chat_id in targets:
                self.follow(chat_id, state.mint, now)
            meta, paid = await asyncio.gather(self.tracker.token_meta(state.mint),
                                              self.tracker.dex.dex_paid(state.mint))
            text = fmt.spectral_token(state.mint, meta, state, self.tracker.kol_names(state.mint),
                                      self.tracker.sol_usd, paid, now)
            log.info('SPECTRAL %s -> %d user(s) at %.2fx', state.mint, len(targets), multiplier(state))
            for chat_id in targets:
                self.enqueue(chat_id, text, state.mint)

    async def show(self, chat_id):
        user = self.users[chat_id]
        now = now_ms()
        found = sorted((s for s in list(self.tracker.trends.values())
                        if matches(user, s, now, self.tracker.sol_usd)),
                       key=lambda s: s.entered_ts)
        shown = found[:SPECTRAL_SHOW_LIMIT]
        items = []
        for state in shown:
            self.follow(chat_id, state.mint, now)
            items.append((await self.tracker.token_meta(state.mint), state))
        await self._send(chat_id, fmt.spectral_list(items, len(found), self.tracker.sol_usd, now),
                         markup=show_keyboard(items))

    # --- incoming -----------------------------------------------------------
    async def poll_updates(self):
        try:
            await self.api.call('setMyCommands', {'commands': COMMANDS})
        except TelegramError as exc:
            log.warning('Spectral setMyCommands failed: %s', exc)
        offset = None
        while True:
            try:
                updates = await self.api.call(
                    'getUpdates', {'timeout': 50, 'offset': offset,
                                   'allowed_updates': ['message', 'callback_query']}, timeout=65)
            except TelegramError as exc:
                log.warning('Spectral getUpdates failed: %s', exc)
                await asyncio.sleep(5)
                continue
            for update in updates or ():
                offset = update['update_id'] + 1
                try:
                    if 'message' in update:
                        await self.on_message(update['message'])
                    elif 'callback_query' in update:
                        await self.on_callback(update['callback_query'])
                except Exception:
                    log.exception('Spectral update %s failed', update.get('update_id'))

    def ensure_user(self, chat):
        chat_id = chat['id']
        if self.users.get(chat_id, {}).get('active'):
            return self.users[chat_id]
        new = self.store.spectral_register(chat_id, chat.get('username'), chat.get('first_name'),
                                           SPECTRAL_DEFAULT_MIN_MINUTES, now_ms())
        self.users[chat_id] = dict(self.store.spectral_user(chat_id))
        if new:
            log.info('Spectral new user %s (@%s)', chat_id, chat.get('username'))
        return self.users[chat_id]

    async def on_message(self, message):
        text = (message.get('text') or '').strip()
        if not text.startswith('/'):
            return
        chat_id = message['chat']['id']
        self.ensure_user(message['chat'])
        parts = text.split()
        command = parts[0].split('@')[0].lower()
        args = parts[1:]
        if command == '/start':
            await self._send(chat_id, fmt.spectral_welcome())
            await self.send_settings(chat_id)
        elif command == '/settings':
            await self.send_settings(chat_id)
        elif command == '/show':
            await self.show(chat_id)
        elif command in ('/time', '/range', '/mcap'):
            try:
                if command == '/time':
                    self.set_filters(chat_id, min_minutes=parse_time(args))
                elif command == '/range':
                    low, high = parse_range(args)
                    self.set_filters(chat_id, min_mult=low, max_mult=high)
                else:
                    low, high = parse_mcap(args)
                    self.set_filters(chat_id, min_mcap=low, max_mcap=high)
            except ValueError as exc:
                await self._send(chat_id, f'⚠️ {exc}')
                return
            await self.send_settings(chat_id)
        else:
            await self._send(chat_id, fmt.spectral_welcome())

    async def send_settings(self, chat_id):
        user = self.users[chat_id]
        await self._send(chat_id, fmt.spectral_settings(user), markup=settings_keyboard(user))

    def set_filters(self, chat_id, **changes):
        user = dict(self.users[chat_id], **changes)
        check_range(user['min_mult'], user['max_mult'], 'multiplier')
        check_range(user['min_mcap'], user['max_mcap'], 'market cap')
        self.store.spectral_update_filters(chat_id, user['min_minutes'], user['min_mult'],
                                           user['max_mult'], user['min_mcap'], user['max_mcap'])
        self.users[chat_id] = user

    async def on_callback(self, query):
        data = query.get('data') or ''
        message = query.get('message') or {}
        chat = message.get('chat') or query['from']
        chat_id = chat['id']
        self.ensure_user(chat)
        notice = None
        if data == 'show':
            await self.api.call('answerCallbackQuery', {'callback_query_id': query['id']})
            await self.show(chat_id)
            return
        if data.startswith('set:'):
            _, field, value = data.split(':', 2)
            try:
                if field == 'time':
                    self.set_filters(chat_id, min_minutes=int(value))
                elif field in ('min', 'max'):
                    self.set_filters(chat_id, **{f'{field}_mult': _parse_mult(value)})
                elif field in ('minmc', 'maxmc'):
                    self.set_filters(chat_id, **{f'{field[:3]}_mcap': _parse_usd(value)})
            except ValueError as exc:
                notice = str(exc)
        await self.api.call('answerCallbackQuery', {'callback_query_id': query['id'],
                                                    'text': notice or '', 'show_alert': bool(notice)})
        if data.startswith('set:') and not notice and message.get('message_id'):
            user = self.users[chat_id]
            try:
                await self.api.call('editMessageText', {
                    'chat_id': chat_id, 'message_id': message['message_id'],
                    'text': fmt.spectral_settings(user), 'parse_mode': 'HTML',
                    'link_preview_options': {'is_disabled': True},
                    'reply_markup': settings_keyboard(user)})
            except TelegramError as exc:
                if 'not modified' not in (exc.description or ''):
                    log.warning('Spectral settings edit failed: %s', exc)
