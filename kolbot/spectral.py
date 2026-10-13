"""Spectral: a second bot that DMs each user the trending tokens matching their filters.

A token is sent once per user, when it has been in trending for at least the
user's minimum time, its multiplier since tracking started (current MC / MC
when trending began) is inside the user's multiplier range and its USD market
cap is inside the user's market cap range. Any range bound may be open.

Tokens that already match when a user starts the bot or changes filters are
marked as seen without being sent, so there is no burst; /show lists them.

After a token is sent, the user also gets its events (KOL buys, migration,
KOL liquidity, DEX paid, CTO, boosts, ads) for as long as the token is still in
trending and inside the user's multiplier and market cap ranges. Trending
multiplier alerts are not forwarded.
"""

import asyncio
import logging
import time

from . import formatting as fmt
from .botapi import BotApi, TelegramError
from .config import (ARCHON_BUTTON_TEXT, ARCHON_URL, MAX_MESSAGE_AGE_SECONDS,
                     SPECTRAL_CHECK_SECONDS, SPECTRAL_DEFAULT_MIN_MINUTES,
                     SPECTRAL_SEND_INTERVAL_SECONDS, SPECTRAL_SHOW_LIMIT)
from .db import now_ms

log = logging.getLogger(__name__)

COMMANDS = [
    {'command': 'settings', 'description': 'See and change your filters'},
    {'command': 'show', 'description': 'Tokens matching your filters now'},
    {'command': 'time', 'description': 'Min minutes in trending, e.g. /time 10'},
    {'command': 'range', 'description': 'Multiplier range, e.g. /range 0.7-2'},
    {'command': 'mcap', 'description': 'Market cap range, e.g. /mcap 20k-100k'},
]
MAX_MINUTES = 24 * 60
_SUFFIXES = {'k': 1e3, 'm': 1e6, 'b': 1e9}

# Filter being edited -> (prompt, command for the same change).
PROMPTS = {
    'time': ('⏱ <b>Min time in trending</b>\n\n'
             'Send the minimum minutes a token must have been in trending.\n\n'
             'Example: <code>10</code>'),
    'mult': ('📈 <b>Multiplier range</b>\n\n'
             'Multiplier = current market cap ÷ market cap when trending started.\n'
             'Send a range; leave a side empty to keep it open.\n\n'
             '<code>0.7-2</code> · between 0.7x and 2x\n'
             '<code>1-</code> · 1x or more\n'
             '<code>-3</code> · up to 3x\n'
             '<code>any</code> · no limit'),
    'mcap': ('💰 <b>Market cap range (USD)</b>\n\n'
             'Send a range; leave a side empty to keep it open.\n\n'
             '<code>20k-100k</code> · between $20K and $100K\n'
             '<code>-100k</code> · up to $100K\n'
             '<code>20k-</code> · $20K or more\n'
             '<code>any</code> · no limit'),
}


# --- filters -------------------------------------------------------------------
def multiplier(state):
    if state is None or not state.mcap or not state.entry:
        return None
    return state.mcap / state.entry


def _in_range(value, low, high):
    return (low is None or value >= low) and (high is None or value <= high)


def in_ranges(user, state, sol_usd):
    """True while a token is in trending with its multiplier and market cap inside the user's ranges."""
    mult = multiplier(state)
    if state is None or not state.active or mult is None:
        return False
    if not _in_range(mult, user['min_mult'], user['max_mult']):
        return False
    if user['min_mcap'] is None and user['max_mcap'] is None:
        return True
    return bool(sol_usd) and _in_range(state.mcap * sol_usd, user['min_mcap'], user['max_mcap'])


def matches(user, state, now, sol_usd):
    """True when a trending token passes the user's time, multiplier and market cap filters."""
    return (now - state.entered_ts >= user['min_minutes'] * 60_000
            and in_ranges(user, state, sol_usd))


def parse_minutes(text):
    text = text.strip().lower().removesuffix('m').strip()
    if not text.isdigit() or int(text) > MAX_MINUTES:
        raise ValueError(f'Send a whole number of minutes between 0 and {MAX_MINUTES}, e.g. 10')
    return int(text)


def _mult_value(text):
    return float(text.removesuffix('x'))


def _usd_value(text):
    text = text.lstrip('$').replace(',', '')
    factor = _SUFFIXES.get(text[-1:], 1)
    return float(text[:-1] if factor != 1 else text) * factor


def parse_bounds(text, kind):
    """'a-b', 'a b', 'a-' (a or more), '-b' (up to b), '<b', '>a' or 'any'. Returns (low, high)."""
    value = _mult_value if kind == 'mult' else _usd_value
    what = 'multiplier' if kind == 'mult' else 'market cap'
    text = ' '.join(text.strip().lower().replace(' to ', '-').split())
    try:
        if text in ('any', 'all', 'none', '-', ''):
            return None, None
        if text[0] in '<>':
            bound = value(text[1:].strip())
            low, high = (None, bound) if text[0] == '<' else (bound, None)
        elif '-' in text:
            left, right = (part.strip() for part in text.split('-', 1))
            low = None if left in ('', 'any') else value(left)
            high = None if right in ('', 'any') else value(right)
        else:
            parts = text.split()
            if len(parts) != 2:
                raise ValueError
            low, high = (None if p == 'any' else value(p) for p in parts)
    except (ValueError, IndexError):
        raise ValueError(f'I could not read that {what} range. Examples: '
                         + ('0.7-2 · 1- · -3 · any' if kind == 'mult' else '20k-100k · -100k · 20k- · any')) from None
    if any(b is not None and b <= 0 for b in (low, high)):
        raise ValueError(f'The {what} values must be greater than 0.')
    check_range(low, high, what)
    return low, high


def check_range(low, high, what):
    if low is not None and high is not None and low >= high:
        raise ValueError(f'The minimum {what} must be lower than the maximum.')


def settings_keyboard():
    return {'inline_keyboard': [
        [{'text': '⏱ Change min time', 'callback_data': 'edit:time'}],
        [{'text': '📈 Change multiplier range', 'callback_data': 'edit:mult'}],
        [{'text': '💰 Change market cap range', 'callback_data': 'edit:mcap'}],
        [{'text': '📋 Show my matches', 'callback_data': 'show'}],
    ]}


def show_keyboard(items):
    """One Archon button per listed token, labeled with its symbol."""
    text = ARCHON_BUTTON_TEXT.replace('Trade', 'Trade {symbol}')
    rows = [[{'text': text.format(symbol=f'${(meta or {}).get("symbol") or state.mint[:6]}'),
              'url': ARCHON_URL.format(mint=state.mint)}] for meta, state, *_ in items]
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
        self.followers = {}  # mint -> chats that were sent the token (they get its events)
        self.seen = {}       # chat -> mints already sent or skipped
        self.pending = {}    # chat -> filter waiting for a typed value
        for row in store.spectral_deliveries():
            self.seen.setdefault(row['chat_id'], set()).add(row['mint'])
            if row['sent']:
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

    def listeners(self, mint):
        """Users who received the token and for whom it still fits (in trending, inside their ranges)."""
        state = self.tracker.trends.get(mint)
        return [chat_id for chat_id in self.followers.get(mint, ())
                if self.users.get(chat_id, {}).get('active')
                and in_ranges(self.users[chat_id], state, self.tracker.sol_usd)]

    def forward(self, mint, text):
        """Called by the tracker for every event message (not trending alerts, not buys)."""
        for chat_id in self.listeners(mint):
            self.enqueue(chat_id, text, mint)

    def on_kol_buy(self, kol, wallet, trade, meta, paid):
        mint = trade['mint']
        targets = self.listeners(mint)
        if not targets:
            return
        first_buy = sum(r['buy_count'] for r in self.store.kol_positions(kol.id, mint)) == 1
        text = fmt.spectral_buy(kol, wallet, trade, meta, self.tracker.trends[mint],
                                self.kols_in(mint), self.tracker.sol_usd, paid, first_buy)
        for chat_id in targets:
            self.enqueue(chat_id, text, mint)

    def kols_in(self, mint):
        return fmt.kols_in_token(self.store.token_positions(mint), self.tracker.kols)

    def follow(self, chat_id, mint, ts):
        self.store.spectral_record_delivery(chat_id, mint, ts, sent=True)
        self.followers.setdefault(mint, set()).add(chat_id)
        self.seen.setdefault(chat_id, set()).add(mint)

    def mark_current_as_seen(self, chat_id, now):
        """Skip what already matches so starting or changing filters doesn't cause a burst."""
        user = self.users[chat_id]
        seen = self.seen.setdefault(chat_id, set())
        for state in list(self.tracker.trends.values()):
            if state.mint not in seen and matches(user, state, now, self.tracker.sol_usd):
                self.store.spectral_record_delivery(chat_id, state.mint, now, sent=False)
                seen.add(state.mint)

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
            targets = [chat_id for chat_id, user in self.users.items()
                       if user['active'] and state.mint not in self.seen.get(chat_id, ())
                       and matches(user, state, now, self.tracker.sol_usd)]
            if not targets:
                continue
            for chat_id in targets:
                self.follow(chat_id, state.mint, now)
            meta, paid = await asyncio.gather(self.tracker.token_meta(state.mint),
                                              self.tracker.dex.dex_paid(state.mint))
            text = fmt.spectral_match(state.mint, meta, state, self.kols_in(state.mint),
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
            kol_count = len({p['kol_id'] for p in self.store.token_positions(state.mint)})
            items.append((await self.tracker.token_meta(state.mint), state, kol_count))
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
            return False
        new = self.store.spectral_register(chat_id, chat.get('username'), chat.get('first_name'),
                                           SPECTRAL_DEFAULT_MIN_MINUTES, now_ms())
        self.users[chat_id] = dict(self.store.spectral_user(chat_id))
        self.mark_current_as_seen(chat_id, now_ms())
        log.info('Spectral %s user %s (@%s)', 'new' if new else 'returning', chat_id, chat.get('username'))
        return True

    async def on_message(self, message):
        text = (message.get('text') or '').strip()
        if not text:
            return
        chat_id = message['chat']['id']
        joined = self.ensure_user(message['chat'])
        if not text.startswith('/'):
            if chat_id in self.pending:
                await self.apply(chat_id, self.pending[chat_id], text)
            else:
                await self._send(chat_id, fmt.spectral_help())
            return
        self.pending.pop(chat_id, None)
        command, _, args = text.partition(' ')
        command = command.split('@')[0].lower()
        if command == '/start':
            if joined:
                await self._send(chat_id, fmt.spectral_welcome())
            await self.send_settings(chat_id)
        elif command == '/settings':
            await self.send_settings(chat_id)
        elif command == '/show':
            await self.show(chat_id)
        elif command in ('/time', '/range', '/mcap'):
            field = {'/time': 'time', '/range': 'mult', '/mcap': 'mcap'}[command]
            if args.strip():
                await self.apply(chat_id, field, args)
            else:
                await self.ask(chat_id, field)
        else:
            await self._send(chat_id, fmt.spectral_help())

    async def ask(self, chat_id, field):
        self.pending[chat_id] = field
        await self._send(chat_id, PROMPTS[field])

    async def apply(self, chat_id, field, text):
        try:
            if field == 'time':
                self.set_filters(chat_id, min_minutes=parse_minutes(text))
            elif field == 'mult':
                low, high = parse_bounds(text, 'mult')
                self.set_filters(chat_id, min_mult=low, max_mult=high)
            else:
                low, high = parse_bounds(text, 'mcap')
                self.set_filters(chat_id, min_mcap=low, max_mcap=high)
        except ValueError as exc:
            await self._send(chat_id, f'⚠️ {exc}')
            return
        self.pending.pop(chat_id, None)
        await self.send_settings(chat_id, saved=True)

    async def send_settings(self, chat_id, saved=False):
        await self._send(chat_id, fmt.spectral_settings(self.users[chat_id], saved),
                         markup=settings_keyboard())

    def set_filters(self, chat_id, **changes):
        user = dict(self.users[chat_id], **changes)
        check_range(user['min_mult'], user['max_mult'], 'multiplier')
        check_range(user['min_mcap'], user['max_mcap'], 'market cap')
        self.store.spectral_update_filters(chat_id, user['min_minutes'], user['min_mult'],
                                           user['max_mult'], user['min_mcap'], user['max_mcap'])
        self.users[chat_id] = user
        self.mark_current_as_seen(chat_id, now_ms())

    async def on_callback(self, query):
        data = query.get('data') or ''
        chat = (query.get('message') or {}).get('chat') or query['from']
        chat_id = chat['id']
        self.ensure_user(chat)
        await self.api.call('answerCallbackQuery', {'callback_query_id': query['id']})
        if data == 'show':
            await self.show(chat_id)
        elif data.startswith('edit:') and data[5:] in PROMPTS:
            await self.ask(chat_id, data[5:])
