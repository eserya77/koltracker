"""Spectral: a second bot that DMs each user the KOL tokens matching their filters.

Filters: minimum time tracked, minimum number of distinct KOLs, multiplier
since tracking started (current MC / MC when tracking began), USD market cap
and venues. Range bounds may be open. Tokens with 2+ KOLs are the KOL trending
tokens; tokens with a single KOL are tracked too, so "min KOLs = 1" works.

A token is sent once per user. Tokens that already match when a user starts
the bot or changes filters are marked as seen without being sent, so there is
no burst; /show lists them.

After a token is sent, the user also gets its updates (KOL buys, migration,
KOL liquidity, DEX paid, CTO, boosts, ads) for as long as it is still tracked
and fits the user's filters. Each update type can be turned off.
"""

import asyncio
import logging
import time

from . import formatting as fmt
from .botapi import BotApi, TelegramError
from .config import (ALERT_TYPES, MAX_MESSAGE_AGE_SECONDS, SPECTRAL_CHECK_SECONDS,
                     SPECTRAL_DEFAULT_MIN_MINUTES, SPECTRAL_PAGE_SIZE, SPECTRAL_SEND_INTERVAL_SECONDS,
                     VENUE_GROUPS)
from .db import now_ms

log = logging.getLogger(__name__)

COMMANDS = [
    {'command': 'start', 'description': 'Your filters'},
    {'command': 'show', 'description': 'Tokens that fit your filters now'},
    {'command': 'time', 'description': 'Min minutes tracked, e.g. /time 10'},
    {'command': 'kols', 'description': 'Min KOLs, e.g. /kols 3'},
    {'command': 'range', 'description': 'Multiplier, e.g. /range 0.7, 2'},
    {'command': 'mcap', 'description': 'Market cap, e.g. /mcap 20k, 100k'},
    {'command': 'venues', 'description': 'Pick venues'},
    {'command': 'alerts', 'description': 'Turn updates on or off'},
    {'command': 'help', 'description': 'How Spectral works'},
]
TYPED_FIELDS = ('time', 'kols', 'mult', 'mcap')
MAX_MINUTES = 24 * 60
MAX_KOLS = 50
_SUFFIXES = {'k': 1e3, 'm': 1e6, 'b': 1e9}
POOL_GROUP = {pool: key for key, (_, pools) in VENUE_GROUPS.items() for pool in pools}


# --- filters -------------------------------------------------------------------
def multiplier(state):
    if state is None or not state.mcap or not state.entry:
        return None
    return state.mcap / state.entry


def _in_range(value, low, high):
    return (low is None or value >= low) and (high is None or value <= high)


def _keys(value):
    return set(value.split(',')) - {''} if value else set()


def user_venues(user):
    """Selected venue keys, or None for all venues."""
    return _keys(user.get('venues')) or None


def alert_on(user, kind):
    return kind not in _keys(user.get('muted'))


def fits(user, state, sol_usd):
    """True while a token is tracked and inside the user's KOL, range and venue filters."""
    mult = multiplier(state)
    if mult is None or not state.active or state.kols < user['min_kols']:
        return False
    if not _in_range(mult, user['min_mult'], user['max_mult']):
        return False
    venues = user_venues(user)
    if venues is not None and POOL_GROUP.get(state.venue) not in venues:
        return False
    if user['min_mcap'] is None and user['max_mcap'] is None:
        return True
    return bool(sol_usd) and _in_range(state.mcap * sol_usd, user['min_mcap'], user['max_mcap'])


def matches(user, state, now, sol_usd):
    """fits() plus the minimum time tracked."""
    return now - state.entered_ts >= user['min_minutes'] * 60_000 and fits(user, state, sol_usd)


def parse_minutes(text):
    text = text.strip().lower().removesuffix('min').removesuffix('m').strip()
    if not text.isdigit() or int(text) > MAX_MINUTES:
        raise ValueError(f'Send a whole number of minutes between 0 and {MAX_MINUTES}, like <code>10</code>.')
    return int(text)


def parse_kols(text):
    text = text.strip().rstrip('+').strip()
    if not text.isdigit() or not 1 <= int(text) <= MAX_KOLS:
        raise ValueError(f'Send a whole number between 1 and {MAX_KOLS}, like <code>3</code>.')
    return int(text)


def _mult_value(text):
    return float(text.removesuffix('x'))


def _usd_value(text):
    text = text.lstrip('$')
    factor = _SUFFIXES.get(text[-1:], 1)
    return float(text[:-1] if factor != 1 else text) * factor


def parse_bounds(text, kind):
    """'min, max' with 'any' (or nothing) for an open side; 'any' alone clears both. Returns (low, high).

    'a - b' and 'a to b' are accepted too.
    """
    value = _mult_value if kind == 'mult' else _usd_value
    what = 'multiplier' if kind == 'mult' else 'market cap'
    examples = ('<code>0.7, 2</code> · <code>1, any</code> · <code>any, 3</code>' if kind == 'mult'
                else '<code>20k, 100k</code> · <code>20k, any</code> · <code>any, 100k</code>')
    text = ' '.join(text.strip().lower().replace(' to ', ',').split())
    try:
        if text in ('any', 'all', 'none', ''):
            return None, None
        separator = ',' if ',' in text else '-' if '-' in text else None
        if separator is None:
            raise ValueError
        parts = [part.strip() for part in text.split(separator)]
        if len(parts) != 2:
            raise ValueError
        low, high = (None if p in ('', 'any') else value(p) for p in parts)
    except (ValueError, IndexError):
        raise ValueError(f'Send <code>min, max</code>, like {examples}') from None
    if any(b is not None and b <= 0 for b in (low, high)):
        raise ValueError(f'The {what} values must be greater than 0.')
    check_range(low, high, what)
    return low, high


def check_range(low, high, what):
    if low is not None and high is not None and low >= high:
        raise ValueError(f'The minimum {what} must be lower than the maximum.')


def toggle_venue(user, key):
    """Returns the new stored venues value ('a,b' or None for all) after a tap on key."""
    if key == 'all':
        return None
    selected = user_venues(user) or set(VENUE_GROUPS)
    selected ^= {key}
    if not selected:
        raise ValueError('Keep at least one venue selected.')
    if selected == set(VENUE_GROUPS):
        return None
    return ','.join(k for k in VENUE_GROUPS if k in selected)


def toggle_alert(user, key):
    """Returns the new stored muted value ('a,b' or None when every alert is on)."""
    if key == 'all':
        return None
    muted = _keys(user.get('muted')) ^ {key}
    return ','.join(k for k in ALERT_TYPES if k in muted) or None


# --- keyboards -----------------------------------------------------------------
def _button(text, data):
    return {'text': text, 'callback_data': data}


def panel_keyboard():
    return {'inline_keyboard': [
        [_button('⏱ Time', 'edit:time'), _button('👥 KOLs', 'edit:kols')],
        [_button('📈 Multiplier', 'edit:mult'), _button('💰 Market cap', 'edit:mcap')],
        [_button('🏦 Venues', 'venues'), _button('🔔 Alerts', 'alerts')],
        [_button('📋 My matches', 'show')],
    ]}


def _toggle_rows(options, is_on, prefix):
    buttons = [_button(f'{"✅" if is_on(key) else "▫️"} {label}', f'{prefix}:{key}') for key, label in options]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def venues_keyboard(user):
    selected = user_venues(user) or set(VENUE_GROUPS)
    rows = _toggle_rows(((k, label) for k, (label, _) in VENUE_GROUPS.items()), selected.__contains__, 'venue')
    rows.append([_button('Select all', 'venue:all'), _button('⬅️ Back', 'settings')])
    return {'inline_keyboard': rows}


def alerts_keyboard(user):
    rows = _toggle_rows(ALERT_TYPES.items(), lambda key: alert_on(user, key), 'alert')
    rows.append([_button('Turn all on', 'alert:all'), _button('⬅️ Back', 'settings')])
    return {'inline_keyboard': rows}


def cancel_keyboard():
    return {'inline_keyboard': [[_button('✖️ Cancel', 'cancel')]]}


def page_keyboard(page, pages):
    rows = []
    if pages > 1:
        nav = [_button(f'{page + 1} / {pages}', 'noop')]
        if page > 0:
            nav.insert(0, _button('◀️', f'page:{page - 1}'))
        if page < pages - 1:
            nav.append(_button('▶️', f'page:{page + 1}'))
        rows.append(nav)
    rows.append([_button('🔄 Refresh', f'page:{page}'), _button('⚙️ Filters', 'settings_new')])
    return {'inline_keyboard': rows}


class SpectralBot:
    def __init__(self, token, store, tracker, dry_run=False, interval=SPECTRAL_SEND_INTERVAL_SECONDS):
        self.api = BotApi(token)
        self.store = store
        self.tracker = tracker
        self.dry_run = dry_run
        self.interval = interval
        self.queue = asyncio.Queue()
        self.users = {row['chat_id']: dict(row) for row in store.spectral_users()}
        self.followers = {}  # mint -> chats that were sent the token (they get its updates)
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

    async def _edit(self, chat_id, message_id, text, markup):
        try:
            await self.api.call('editMessageText', {
                'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': 'HTML',
                'link_preview_options': {'is_disabled': True}, 'reply_markup': markup})
        except TelegramError as exc:
            if 'not modified' not in (exc.description or ''):
                log.warning('Spectral edit failed: %s', exc)

    async def _show_screen(self, chat_id, message_id, text, markup):
        if message_id:
            await self._edit(chat_id, message_id, text, markup)
        else:
            await self._send(chat_id, text, markup=markup)

    def deactivate(self, chat_id):
        self.store.spectral_deactivate(chat_id)
        if chat_id in self.users:
            self.users[chat_id]['active'] = 0

    def listeners(self, mint, kind):
        """Users who received the token, still fit it and have this update type on."""
        state = self.tracker.tracked(mint)
        return [chat_id for chat_id in self.followers.get(mint, ())
                if self.users.get(chat_id, {}).get('active') and alert_on(self.users[chat_id], kind)
                and fits(self.users[chat_id], state, self.tracker.sol_usd)]

    def forward(self, mint, text, kind):
        """Called by the tracker for every event message, with its ALERT_TYPES key."""
        for chat_id in self.listeners(mint, kind):
            self.enqueue(chat_id, text, mint)

    def on_kol_buy(self, kol, wallet, trade, meta, paid):
        mint = trade['mint']
        targets = self.listeners(mint, 'buy')
        if not targets:
            return
        first_buy = sum(r['buy_count'] for r in self.store.kol_positions(kol.id, mint)) == 1
        text = fmt.spectral_buy(kol, wallet, trade, meta, self.tracker.tracked(mint),
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
        for state in self.tracker.candidates():
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
        for state in self.tracker.candidates():
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

    async def show(self, chat_id, page=0, message_id=None):
        """One page of the user's current matches; edits message_id in place when given."""
        user = self.users[chat_id]
        now = now_ms()
        found = sorted((s for s in self.tracker.candidates() if matches(user, s, now, self.tracker.sol_usd)),
                       key=lambda s: s.entered_ts)
        pages = max(1, -(-len(found) // SPECTRAL_PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        start = page * SPECTRAL_PAGE_SIZE
        items = []
        for state in found[start:start + SPECTRAL_PAGE_SIZE]:
            self.follow(chat_id, state.mint, now)
            items.append((await self.tracker.token_meta(state.mint), state))
        text = fmt.spectral_list(items, len(found), start, page, pages, self.tracker.sol_usd, now)
        await self._show_screen(chat_id, message_id, text, page_keyboard(page, pages))

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
        user = self.users[chat_id]
        typed = {'/time': 'time', '/kols': 'kols', '/range': 'mult', '/mcap': 'mcap'}
        if command in ('/start', '/settings'):
            await self.send_panel(chat_id, 'welcome' if joined else None)
        elif command == '/show':
            await self.show(chat_id)
        elif command == '/venues':
            await self._send(chat_id, fmt.spectral_venues(user), markup=venues_keyboard(user))
        elif command == '/alerts':
            await self._send(chat_id, fmt.spectral_alerts(user), markup=alerts_keyboard(user))
        elif command in typed:
            if args.strip():
                await self.apply(chat_id, typed[command], args)
            else:
                await self.ask(chat_id, typed[command])
        else:
            await self._send(chat_id, fmt.spectral_help())

    async def ask(self, chat_id, field, message_id=None):
        self.pending[chat_id] = field
        await self._show_screen(chat_id, message_id, fmt.spectral_prompt(field), cancel_keyboard())

    async def apply(self, chat_id, field, text):
        try:
            if field == 'time':
                self.set_filters(chat_id, min_minutes=parse_minutes(text))
            elif field == 'kols':
                self.set_filters(chat_id, min_kols=parse_kols(text))
            else:
                low, high = parse_bounds(text, field)
                self.set_filters(chat_id, **{f'min_{field}': low, f'max_{field}': high})
        except ValueError as exc:
            await self._send(chat_id, f'⚠️ {exc}', markup=cancel_keyboard())
            return
        self.pending.pop(chat_id, None)
        await self.send_panel(chat_id, 'saved')

    async def send_panel(self, chat_id, intro=None, message_id=None):
        await self._show_screen(chat_id, message_id, fmt.spectral_panel(self.users[chat_id], intro),
                                panel_keyboard())

    def set_filters(self, chat_id, **changes):
        user = dict(self.users[chat_id], **changes)
        check_range(user['min_mult'], user['max_mult'], 'multiplier')
        check_range(user['min_mcap'], user['max_mcap'], 'market cap')
        self.store.spectral_update_filters(chat_id, user)
        self.users[chat_id] = user
        self.mark_current_as_seen(chat_id, now_ms())

    async def on_callback(self, query):
        data = query.get('data') or ''
        message = query.get('message') or {}
        chat = message.get('chat') or query['from']
        chat_id = chat['id']
        message_id = message.get('message_id')
        self.ensure_user(chat)
        notice = None
        try:
            if data.startswith('venue:'):
                self.set_filters(chat_id, venues=toggle_venue(self.users[chat_id], data[6:]))
            elif data.startswith('alert:'):
                self.set_filters(chat_id, muted=toggle_alert(self.users[chat_id], data[6:]))
        except ValueError as exc:
            notice = str(exc)
        await self.api.call('answerCallbackQuery', {'callback_query_id': query['id'],
                                                    'text': notice or '', 'show_alert': bool(notice)})
        user = self.users[chat_id]
        if data == 'show':
            await self.show(chat_id)
        elif data.startswith('page:'):
            await self.show(chat_id, int(data[5:]), message_id)
        elif data.startswith('edit:') and data[5:] in TYPED_FIELDS:
            await self.ask(chat_id, data[5:], message_id)
        elif data == 'venues' or data.startswith('venue:'):
            await self._show_screen(chat_id, message_id, fmt.spectral_venues(user), venues_keyboard(user))
        elif data == 'alerts' or data.startswith('alert:'):
            await self._show_screen(chat_id, message_id, fmt.spectral_alerts(user), alerts_keyboard(user))
        elif data in ('settings', 'cancel'):
            self.pending.pop(chat_id, None)
            await self.send_panel(chat_id, message_id=message_id)
        elif data == 'settings_new':
            await self.send_panel(chat_id)
