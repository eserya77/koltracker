import asyncio
import sqlite3

import pytest

from kolbot.botapi import TelegramError
from kolbot.db import Store
from kolbot.kols import build_registry
from kolbot.spectral import (COMMANDS, SpectralBot, matches, page_keyboard, panel_keyboard, parse_bounds,
                             parse_kols, parse_minutes, toggle_alert, toggle_venue)
from kolbot.trending import TrendState

MIN = 60_000
NOW = 10_000 * MIN


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr('kolbot.spectral.now_ms', lambda: NOW)


def user(**overrides):
    base = {'min_minutes': 10, 'min_mult': None, 'max_mult': None, 'min_mcap': None, 'max_mcap': None,
            'venues': None, 'min_kols': 2, 'muted': None, 'active': 1}
    base.update(overrides)
    return base


def trend(mint='M', entry=50, mcap=110, age_min=15, venue='pump-amm', kols=2):
    return TrendState(mint, base=40, ath=120, entered_ts=NOW - age_min * MIN, entry=entry, mcap=mcap,
                      venue=venue, kols=kols)


# --- filters -------------------------------------------------------------------
def test_matches_time_and_multiplier_range():
    assert matches(user(), trend(), NOW, 200)
    assert not matches(user(min_minutes=20), trend(), NOW, 200)
    # Tracked at 50k, now 110k (2.2x), filter 0.7x-2x -> not sent.
    assert not matches(user(min_mult=0.7, max_mult=2), trend(), NOW, 200)
    assert matches(user(min_mult=0.7, max_mult=3), trend(), NOW, 200)
    assert not matches(user(), trend(mcap=None), NOW, 200)


def test_matches_open_market_cap_ranges():
    token = trend(mcap=110)  # 110 SOL * $200 = $22K
    assert matches(user(max_mcap=100_000), token, NOW, 200)
    assert matches(user(min_mcap=20_000, max_mcap=100_000), token, NOW, 200)
    assert matches(user(min_mcap=20_000), token, NOW, 200)
    assert not matches(user(min_mcap=50_000), token, NOW, 200)
    assert not matches(user(max_mcap=100_000), token, NOW, None)       # no SOL price yet
    assert matches(user(), token, NOW, None)


def test_matches_min_kols():
    assert matches(user(min_kols=1), trend(kols=1), NOW, 200)
    assert not matches(user(), trend(kols=1), NOW, 200)
    assert not matches(user(min_kols=3), trend(kols=2), NOW, 200)
    assert matches(user(min_kols=3), trend(kols=4), NOW, 200)


def test_venue_filter_and_toggle():
    assert matches(user(venues='pumpswap'), trend(venue='pump-amm'), NOW, 200)
    assert not matches(user(venues='pumpfun,raydium'), trend(venue='pump-amm'), NOW, 200)
    assert matches(user(venues='raydium'), trend(venue='raydium-cpmm'), NOW, 200)
    all_but_pumpfun = toggle_venue(user(), 'pumpfun')
    assert 'pumpfun' not in all_but_pumpfun and toggle_venue(user(venues=all_but_pumpfun), 'pumpfun') is None
    with pytest.raises(ValueError):
        toggle_venue(user(venues='orca'), 'orca')


def test_alert_toggle():
    muted = toggle_alert(user(), 'boost')
    assert muted == 'boost'
    assert toggle_alert(user(muted='boost,ad'), 'boost') == 'ad'
    assert toggle_alert(user(muted='ad'), 'ad') is None
    assert toggle_alert(user(muted='ad,cto'), 'all') is None


@pytest.mark.parametrize('text, expected', [
    ('20k, 100k', (20_000, 100_000)), ('20k,100k', (20_000, 100_000)), ('$20k , $1.5m', (20_000, 1_500_000)),
    ('any, 100k', (None, 100_000)), (', 100k', (None, 100_000)), ('20k, any', (20_000, None)),
    ('20k,', (20_000, None)), ('any', (None, None)), ('20k - 100k', (20_000, 100_000)),
    ('20k to 100k', (20_000, 100_000)), ('20000, 100000', (20_000, 100_000)),
])
def test_parse_mcap_ranges(text, expected):
    assert parse_bounds(text, 'mcap') == expected


@pytest.mark.parametrize('text, expected', [
    ('0.7, 2', (0.7, 2)), ('0.7x, 2x', (0.7, 2)), ('1, any', (1, None)), ('any, 3', (None, 3)), ('any', (None, None)),
])
def test_parse_mult_ranges(text, expected):
    assert parse_bounds(text, 'mult') == expected


@pytest.mark.parametrize('text', ['100k, 20k', 'abc', '20k', '0, 1k', '20,000, 100,000', '1, 2, 3'])
def test_parse_bounds_rejects(text):
    with pytest.raises(ValueError):
        parse_bounds(text, 'mcap')


def test_parse_minutes_and_kols():
    assert parse_minutes('12') == 12 and parse_minutes('15 min') == 15
    assert parse_kols('3') == 3 and parse_kols('4+') == 4
    for bad in ('', 'x', '-1', '5000'):
        with pytest.raises(ValueError):
            parse_minutes(bad)
    for bad in ('0', 'x', '51'):
        with pytest.raises(ValueError):
            parse_kols(bad)


def test_keyboards_and_commands():
    assert COMMANDS[0]['command'] == 'start'
    assert {c['command'] for c in COMMANDS} >= {'start', 'show', 'kols', 'alerts', 'venues'}
    assert all(len(row) <= 2 for row in panel_keyboard()['inline_keyboard'])
    assert page_keyboard(0, 1)['inline_keyboard'] == [[{'text': '🔄 Refresh', 'callback_data': 'page:0'},
                                                       {'text': '⚙️ Filters', 'callback_data': 'settings_new'}]]
    assert [b['callback_data'] for b in page_keyboard(1, 3)['inline_keyboard'][0]] == ['page:0', 'noop', 'page:2']


# --- bot -----------------------------------------------------------------------
class FakeApi:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, mint=None, reply_markup=None):
        self.calls.append(('send', chat_id, text, mint))

    async def call(self, method, payload=None, **kwargs):
        self.calls.append((method, payload))

    async def close(self):
        pass

    def texts(self, chat_id):
        return [c[2] for c in self.calls if c[0] == 'send' and c[1] == chat_id]


class FakeDex:
    async def dex_paid(self, mint):
        return True


class FakeTracker:
    def __init__(self, trends):
        self.trends = {t.mint: t for t in trends}
        self.sol_usd = 200
        self.dex = FakeDex()
        self.kols = {}

    def tracked(self, mint):
        return self.trends.get(mint)

    def candidates(self):
        return list(self.trends.values())

    async def token_meta(self, mint):
        return {'symbol': mint, 'name': mint.lower()}


def make_bot(trends):
    bot = SpectralBot('token', Store(':memory:'), FakeTracker(trends))
    bot.api = FakeApi()
    return bot


def drain(bot):
    items = []
    while not bot.queue.empty():
        _, chat_id, text, mint, _ = bot.queue.get_nowait()
        items.append((chat_id, mint, text))
    return items


def start(bot, chat_id):
    return bot.on_message({'chat': {'id': chat_id, 'username': f'u{chat_id}'}, 'text': '/start'})


def test_start_is_one_message_and_sends_no_burst():
    async def scenario():
        bot = make_bot([trend('OLD'), trend('YOUNG', mcap=60, age_min=3)])
        await start(bot, 1)
        assert len(bot.api.texts(1)) == 1 and 'Welcome to Spectral' in bot.api.texts(1)[0]
        await bot.deliver_due(NOW)
        assert drain(bot) == []                                  # OLD already matched: skipped

        await bot.deliver_due(NOW + 10 * MIN)                    # YOUNG crosses 10 min later
        sent = drain(bot)
        assert [(c, m) for c, m, _ in sent] == [(1, 'YOUNG')]
        assert sent[0][2].startswith('🎯 <b>New match · $YOUNG</b>')

        bot.forward('YOUNG', 'event', 'migrate')
        bot.forward('OLD', 'not followed', 'migrate')
        assert [(c, m) for c, m, _ in drain(bot)] == [(1, 'YOUNG')]

    asyncio.run(scenario())


def test_changing_filters_does_not_burst():
    async def scenario():
        bot = make_bot([trend('AAA')])
        await start(bot, 1)
        await bot.on_message({'chat': {'id': 1}, 'text': '/range 3, any'})   # nothing matches
        bot.tracker.trends['BBB'] = trend('BBB', mcap=200)
        await bot.on_message({'chat': {'id': 1}, 'text': '/range any'})      # AAA and BBB match now
        await bot.deliver_due(NOW)
        assert drain(bot) == []

    asyncio.run(scenario())


def test_typed_filters_with_prompt_and_cancel():
    async def scenario():
        bot = make_bot([])
        await start(bot, 3)
        msg = {'message_id': 5, 'chat': {'id': 3}}
        await bot.on_callback({'id': 'q', 'data': 'edit:mcap', 'message': msg})
        method, payload = bot.api.calls[-1]
        assert method == 'editMessageText' and 'min, max' in payload['text'] and bot.pending[3] == 'mcap'
        await bot.on_message({'chat': {'id': 3}, 'text': '100k, 20k'})
        assert bot.api.texts(3)[-1].startswith('⚠️') and bot.pending[3] == 'mcap'
        await bot.on_message({'chat': {'id': 3}, 'text': 'any, 100k'})
        saved = bot.store.spectral_user(3)
        assert (saved['min_mcap'], saved['max_mcap']) == (None, 100_000)
        assert bot.api.texts(3)[-1].startswith('✅ <b>Saved</b>') and 'Up to $100K' in bot.api.texts(3)[-1]

        await bot.on_callback({'id': 'q', 'data': 'edit:kols', 'message': msg})
        await bot.on_callback({'id': 'q', 'data': 'cancel', 'message': msg})
        assert 3 not in bot.pending and '⚙️ <b>Your filters</b>' in bot.api.calls[-1][1]['text']

        await bot.on_message({'chat': {'id': 3}, 'text': '/kols 3'})
        assert bot.store.spectral_user(3)['min_kols'] == 3
        await bot.on_message({'chat': {'id': 3}, 'text': 'hello'})
        assert '/kols 3' in bot.api.texts(3)[-1]

    asyncio.run(scenario())


def test_alert_menu_and_muted_updates():
    async def scenario():
        bot = make_bot([trend('TOK')])
        await start(bot, 1)
        bot.follow(1, 'TOK', NOW)
        msg = {'message_id': 4, 'chat': {'id': 1}}
        await bot.on_callback({'id': 'q', 'data': 'alert:boost', 'message': msg})
        assert bot.store.spectral_user(1)['muted'] == 'boost'
        assert '▫️ Boosts' in str(bot.api.calls[-1][1]['reply_markup'])
        bot.forward('TOK', 'boost text', 'boost')
        bot.forward('TOK', 'migration text', 'migrate')
        assert [t for _, _, t in drain(bot)] == ['migration text']

    asyncio.run(scenario())


def test_show_paginates_without_token_buttons():
    async def scenario():
        bot = make_bot([trend(f'T{i}', age_min=20 + i) for i in range(12)])
        await start(bot, 1)
        await bot.on_message({'chat': {'id': 1}, 'text': '/show'})
        _, chat_id, text, mint = bot.api.calls[-1]
        assert 'My matches · 12' in text and text.count('<code>') == 5 and mint is None
        assert 'Page 1 of 3' in text
        assert 'href="https://t.me/archont_sol_bot?start=t_8060490164_T11"' in text   # oldest first

        await bot.on_callback({'id': 'q', 'data': 'page:2', 'message': {'message_id': 7, 'chat': {'id': 1}}})
        method, payload = bot.api.calls[-1]
        assert method == 'editMessageText' and payload['message_id'] == 7
        assert payload['text'].count('<code>') == 2 and '<b>11.</b>' in payload['text']
        assert all('url' not in b for row in payload['reply_markup']['inline_keyboard'] for b in row)

    asyncio.run(scenario())


def test_venue_menu_callbacks():
    async def scenario():
        bot = make_bot([])
        await start(bot, 1)
        msg = {'message_id': 4, 'chat': {'id': 1}}
        await bot.on_callback({'id': 'q', 'data': 'venues', 'message': msg})
        await bot.on_callback({'id': 'q', 'data': 'venue:pumpfun', 'message': msg})
        assert 'pumpfun' not in bot.store.spectral_user(1)['venues']
        assert '▫️ Pumpfun' in str(bot.api.calls[-1][1]['reply_markup'])
        await bot.on_callback({'id': 'q', 'data': 'venue:all', 'message': msg})
        await bot.on_callback({'id': 'q', 'data': 'settings', 'message': msg})
        assert 'Venues: <b>All</b>' in bot.api.calls[-1][1]['text']

    asyncio.run(scenario())


def test_kol_buy_only_for_tokens_inside_user_filters():
    async def scenario():
        kols, wallets = build_registry([{'address': 'AM', 'name': 'Alice'}, {'address': 'BM', 'name': 'Bob'}],
                                       [{'address': 'AS', 'name': 'Alice2'}])
        bot = make_bot([trend('TOK', mcap=110)])  # 2.2x, $22K
        bot.store.sync_registry(kols, wallets)
        bot.tracker.kols = kols
        for sig, wallet, sol in (('s1', 'AS', 1.0), ('s2', 'BM', 2.0)):
            bot.store.record_trade(sig, wallet, 'TOK', 'buy', sol, 1000, 100, 'pump', NOW)
        bot.store.record_trade('s3', 'BM', 'TOK', 'sell', 2.0, 1000, 100, 'pump', NOW)
        for chat_id in (1, 2, 3):
            await start(bot, chat_id)
            bot.follow(chat_id, 'TOK', NOW)
        bot.set_filters(2, max_mcap=20_000)        # token is $22K: outside user 2's range
        bot.set_filters(3, muted='buy')            # user 3 turned KOL buys off
        drain(bot)

        trade = {'mint': 'TOK', 'wallet': 'AS', 'sol': 1.0, 'mcap_sol': 110, 'venue': 'pump'}
        bot.on_kol_buy(kols['alice'], wallets['AS'], trade, {'symbol': 'TOK'}, True)
        sent = drain(bot)
        assert [c for c, _, _ in sent] == [1]
        text = sent[0][2]
        assert text.startswith('🔵 <b>Alice bought $TOK</b>\n<i>First buy · side wallet</i>')
        assert '1 in, 1 out' in text and '<b>Alice</b>' in text and '🔴 Bob' in text

        bot.tracker.trends['TOK'].active = False     # no longer tracked: no more updates
        bot.forward('TOK', 'event', 'migrate')
        assert drain(bot) == []

    asyncio.run(scenario())


def test_blocked_user_is_deactivated():
    async def scenario():
        bot = make_bot([])
        await start(bot, 1)

        async def blocked(*args, **kwargs):
            raise TelegramError('sendMessage', 403, 'Forbidden: bot was blocked by the user')

        bot.api.send_message = blocked
        await bot._send(1, 'hi')
        assert not bot.users[1]['active'] and not bot.store.spectral_user(1)['active']

    asyncio.run(scenario())


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / 'old.sqlite3'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE spectral_users (chat_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, '
                 'min_minutes INTEGER NOT NULL, min_mult REAL, max_mult REAL, active INTEGER NOT NULL DEFAULT 1, '
                 'created_ts INTEGER NOT NULL)')
    conn.execute("INSERT INTO spectral_users(chat_id, min_minutes, created_ts) VALUES (1, 10, 1)")
    conn.execute('CREATE TABLE spectral_deliveries (chat_id INTEGER NOT NULL, mint TEXT NOT NULL, '
                 'ts INTEGER NOT NULL, PRIMARY KEY (chat_id, mint))')
    conn.execute("INSERT INTO spectral_deliveries VALUES (1, 'M', 1)")
    conn.commit()
    conn.close()
    store = Store(path)
    row = store.spectral_user(1)
    assert row['min_kols'] == 2 and row['muted'] is None and row['venues'] is None
    assert store.spectral_deliveries()[0]['sent'] == 1
