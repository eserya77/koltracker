import asyncio
import sqlite3

import pytest

from kolbot.db import Store
from kolbot.spectral import (SpectralBot, matches, parse_mcap, parse_range, parse_time,
                             settings_keyboard, show_keyboard)
from kolbot.trending import TrendState

MIN = 60_000
NOW = 10_000 * MIN


def user(**overrides):
    base = {'min_minutes': 10, 'min_mult': None, 'max_mult': None, 'min_mcap': None,
            'max_mcap': None, 'active': 1}
    base.update(overrides)
    return base


def trend(mint='M', entry=50, mcap=110, age_min=15):
    return TrendState(mint, base=40, ath=120, entered_ts=NOW - age_min * MIN, entry=entry, mcap=mcap)


def test_matches_time_and_multiplier_range():
    assert matches(user(), trend(), NOW, 200)
    assert not matches(user(min_minutes=20), trend(), NOW, 200)
    # The spec example: tracked at 50k, now 110k (2.2x), filter 0.7x-2x -> not sent.
    assert not matches(user(min_mult=0.7, max_mult=2), trend(), NOW, 200)
    assert matches(user(min_mult=0.7, max_mult=3), trend(), NOW, 200)
    assert not matches(user(min_mult=2.5), trend(), NOW, 200)
    assert not matches(user(), trend(mcap=None), NOW, 200)


def test_matches_open_market_cap_ranges():
    token = trend(mcap=110)  # 110 SOL * $200 = $22K
    assert matches(user(max_mcap=100_000), token, NOW, 200)            # up to 100k
    assert matches(user(min_mcap=20_000, max_mcap=100_000), token, NOW, 200)
    assert matches(user(min_mcap=20_000), token, NOW, 200)             # 20k or more
    assert not matches(user(min_mcap=50_000), token, NOW, 200)
    assert not matches(user(max_mcap=20_000), token, NOW, 200)
    assert not matches(user(max_mcap=100_000), token, NOW, None)       # no SOL price yet
    assert matches(user(), token, NOW, None)                           # no mcap filter


def test_parsers():
    assert parse_time(['12']) == 12
    for bad in ([], ['x'], ['-1'], ['5000']):
        with pytest.raises(ValueError):
            parse_time(bad)
    assert parse_range(['0.7', '2']) == (0.7, 2)
    assert parse_range(['1x', 'any']) == (1, None)
    assert parse_range(['any']) == (None, None)
    assert parse_mcap(['20k', '100k']) == (20_000, 100_000)
    assert parse_mcap(['any', '$1.5M']) == (None, 1_500_000)
    assert parse_mcap(['20000', 'any']) == (20_000, None)
    assert parse_mcap(['any']) == (None, None)
    for bad in (['100k', '20k'], ['x', '1k'], ['0', '1k'], ['20k']):
        with pytest.raises(ValueError):
            parse_mcap(bad)
    for bad in (['2', '1'], ['0', '2'], ['a', 'b'], ['1']):
        with pytest.raises(ValueError):
            parse_range(bad)


def test_settings_keyboard_marks_current_values():
    rows = settings_keyboard(user(min_minutes=15, min_mult=0.7, max_mcap=100_000))['inline_keyboard']
    selected = [b['callback_data'] for row in rows for b in row if b['text'].startswith('✅')]
    assert selected == ['set:time:15', 'set:min:0.7', 'set:max:any', 'set:minmc:any', 'set:maxmc:100000']
    assert all(len(b['callback_data']) <= 64 for row in rows for b in row)


class FakeApi:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, mint=None, reply_markup=None):
        self.calls.append(('send', chat_id, text, mint))

    async def call(self, method, payload=None, **kwargs):
        self.calls.append((method, payload))

    async def close(self):
        pass


class FakeDex:
    async def dex_paid(self, mint):
        return True


class FakeTracker:
    def __init__(self, trends):
        self.trends = {t.mint: t for t in trends}
        self.sol_usd = 200
        self.dex = FakeDex()

    async def token_meta(self, mint):
        return {'symbol': mint, 'name': mint.lower()}

    def kol_names(self, mint):
        return ['Cented', 'Ethan Prosper']


def make_bot(trends):
    store = Store(':memory:')
    bot = SpectralBot('token', store, FakeTracker(trends))
    bot.api = FakeApi()
    for chat_id, filters in ((1, {}), (2, {'max_mult': 2.0})):
        bot.ensure_user({'id': chat_id, 'username': f'u{chat_id}'})
        if filters:
            bot.set_filters(chat_id, **filters)
    return bot


def drain(bot):
    items = []
    while not bot.queue.empty():
        _, chat_id, text, mint, _ = bot.queue.get_nowait()
        items.append((chat_id, mint, text))
    return items


def test_delivery_once_and_event_forwarding():
    async def scenario():
        bot = make_bot([trend('AAA', mcap=110), trend('BBB', mcap=60, age_min=3)])
        await bot.deliver_due(NOW)
        sent = drain(bot)
        # AAA (2.2x, 15m) goes to user 1 only; BBB is too young for both.
        assert [(c, m) for c, m, _ in sent] == [(1, 'AAA')]
        assert sent[0][2].startswith('🔮 <b>$AAA</b>') and '2.20x' in sent[0][2]
        await bot.deliver_due(NOW + MIN)
        assert drain(bot) == []

        bot.forward('AAA', 'migration text')
        bot.forward('BBB', 'not followed')
        assert [(c, m) for c, m, _ in drain(bot)] == [(1, 'AAA')]

        await bot.deliver_due(NOW + 10 * MIN)  # BBB now 13m old, 1.2x -> both users
        assert sorted(c for c, m, _ in drain(bot)) == [1, 2]
        assert bot.store.conn.execute('SELECT COUNT(*) FROM spectral_deliveries').fetchone()[0] == 3

    asyncio.run(scenario())


def test_commands_and_settings_callback():
    async def scenario():
        bot = make_bot([trend('AAA', mcap=110)])
        await bot.on_message({'chat': {'id': 3, 'username': 'new'}, 'text': '/range 0.7 2.5'})
        assert (bot.users[3]['min_mult'], bot.users[3]['max_mult']) == (0.7, 2.5)
        await bot.on_message({'chat': {'id': 3}, 'text': '/range 3 1'})
        assert 'lower than' in bot.api.calls[-1][2]

        await bot.on_callback({'id': 'q', 'data': 'set:time:30',
                               'message': {'message_id': 9, 'chat': {'id': 3}}})
        assert bot.users[3]['min_minutes'] == 30
        assert bot.api.calls[-1][0] == 'editMessageText'
        assert bot.store.spectral_user(3)['min_minutes'] == 30

        await bot.on_callback({'id': 'q', 'data': 'set:maxmc:100000',
                               'message': {'message_id': 9, 'chat': {'id': 3}}})
        assert bot.users[3]['max_mcap'] == 100_000
        await bot.on_message({'chat': {'id': 3}, 'text': '/mcap 20k any'})
        saved = bot.store.spectral_user(3)
        assert (saved['min_mcap'], saved['max_mcap']) == (20_000, None)
        assert 'Market cap: <b>$20K or more</b>' in bot.api.calls[-1][2]

        await bot.on_message({'chat': {'id': 1}, 'text': '/show'})
        method, chat_id, text, _ = bot.api.calls[-1]
        assert chat_id == 1 and 'Matching tokens</b> (1)' in text
        assert 1 in bot.followers['AAA']
        assert show_keyboard([({'symbol': 'AAA'}, bot.tracker.trends['AAA'])])['inline_keyboard'] == [
            [{'text': '⚡ Trade $AAA on Archon',
              'url': 'https://t.me/archont_sol_bot?start=t_8060490164_AAA'}]]

    asyncio.run(scenario())


def test_blocked_user_is_deactivated():
    async def scenario():
        from kolbot.botapi import TelegramError

        bot = make_bot([])

        async def blocked(*args, **kwargs):
            raise TelegramError('sendMessage', 403, 'Forbidden: bot was blocked by the user')

        bot.api.send_message = blocked
        await bot._send(1, 'hi')
        assert not bot.users[1]['active'] and not bot.store.spectral_user(1)['active']

    asyncio.run(scenario())


def test_old_database_gets_new_trending_columns(tmp_path):
    path = tmp_path / 'old.sqlite3'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE trending (mint TEXT PRIMARY KEY, base_mcap_sol REAL NOT NULL, '
                 'ath_mcap_sol REAL NOT NULL, entered_ts INTEGER NOT NULL, last_mult REAL NOT NULL '
                 'DEFAULT 1, active INTEGER NOT NULL DEFAULT 1, stopped_ts INTEGER)')
    conn.execute("INSERT INTO trending(mint, base_mcap_sol, ath_mcap_sol, entered_ts) VALUES ('M', 5, 9, 1)")
    conn.commit()
    conn.close()
    row = Store(path).trending_rows()[0]
    assert row['entry_mcap_sol'] is None and row['last_mcap_sol'] is None
