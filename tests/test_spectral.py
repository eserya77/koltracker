import asyncio
import sqlite3

import pytest

from kolbot.botapi import TelegramError
from kolbot.db import Store
from kolbot.spectral import (COMMANDS, SpectralBot, matches, page_keyboard, parse_bounds, parse_minutes,
                             toggle_venue)
from kolbot.trending import TrendState

MIN = 60_000
NOW = 10_000 * MIN


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr('kolbot.spectral.now_ms', lambda: NOW)


def user(**overrides):
    base = {'min_minutes': 10, 'min_mult': None, 'max_mult': None, 'min_mcap': None,
            'max_mcap': None, 'venues': None, 'active': 1}
    base.update(overrides)
    return base


def trend(mint='M', entry=50, mcap=110, age_min=15, venue='pump-amm'):
    return TrendState(mint, base=40, ath=120, entered_ts=NOW - age_min * MIN, entry=entry, mcap=mcap,
                      venue=venue)


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


@pytest.mark.parametrize('text, expected', [
    ('20k-100k', (20_000, 100_000)), ('20k - 100k', (20_000, 100_000)), ('20k to 100k', (20_000, 100_000)),
    ('20k 100k', (20_000, 100_000)), ('-100k', (None, 100_000)), ('<100k', (None, 100_000)),
    ('20k-', (20_000, None)), ('>20k', (20_000, None)), ('$1.5M-', (1_500_000, None)),
    ('any', (None, None)), ('any 100k', (None, 100_000)), ('20000-any', (20_000, None)),
])
def test_parse_mcap_ranges(text, expected):
    assert parse_bounds(text, 'mcap') == expected


@pytest.mark.parametrize('text, expected', [
    ('0.7-2', (0.7, 2)), ('0.7x-2x', (0.7, 2)), ('1-', (1, None)), ('-3', (None, 3)), ('any', (None, None)),
])
def test_parse_mult_ranges(text, expected):
    assert parse_bounds(text, 'mult') == expected


@pytest.mark.parametrize('text', ['100k-20k', 'abc', '20k', '0-1k', '1-2-3'])
def test_parse_bounds_rejects(text):
    with pytest.raises(ValueError):
        parse_bounds(text, 'mcap')


def test_parse_minutes():
    assert parse_minutes('12') == 12 and parse_minutes('15m') == 15
    for bad in ('', 'x', '-1', '5000'):
        with pytest.raises(ValueError):
            parse_minutes(bad)


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

    async def token_meta(self, mint):
        return {'symbol': mint, 'name': mint.lower()}

    def kol_names(self, mint):
        return ['Cented', 'Ethan Prosper']


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


def test_start_does_not_send_tokens_that_already_match():
    async def scenario():
        bot = make_bot([trend('OLD', mcap=110), trend('YOUNG', mcap=60, age_min=3)])
        await start(bot, 1)
        await bot.deliver_due(NOW)
        assert drain(bot) == []                                  # OLD already matched: skipped
        assert 'Welcome to Spectral' in bot.api.texts(1)[0]

        await bot.deliver_due(NOW + 10 * MIN)                    # YOUNG crosses 10 min later
        sent = drain(bot)
        assert [(c, m) for c, m, _ in sent] == [(1, 'YOUNG')]
        assert sent[0][2].startswith('🎯 <b>New match</b>\n<i>')

        bot.forward('YOUNG', 'event')
        bot.forward('OLD', 'not followed')
        assert [(c, m) for c, m, _ in drain(bot)] == [(1, 'YOUNG')]

        await bot.on_message({'chat': {'id': 1}, 'text': '/show'})   # /show follows OLD too
        assert 'Your matches</b> · 1' in bot.api.texts(1)[-1]  # YOUNG is 3m old at NOW
        bot.forward('OLD', 'now followed')
        assert [(c, m) for c, m, _ in drain(bot)] == [(1, 'OLD')]

    asyncio.run(scenario())


def test_changing_filters_does_not_burst():
    async def scenario():
        bot = make_bot([trend('AAA', mcap=110)])
        await start(bot, 1)
        await bot.on_message({'chat': {'id': 1}, 'text': '/range 3-'})   # nothing matches
        bot.tracker.trends['BBB'] = trend('BBB', mcap=200)               # 4x, already old
        await bot.on_message({'chat': {'id': 1}, 'text': '/range any'})  # AAA and BBB match now
        await bot.deliver_due(NOW)
        assert drain(bot) == []

    asyncio.run(scenario())


def test_typed_filters_via_buttons():
    async def scenario():
        bot = make_bot([])
        await start(bot, 3)
        await bot.on_callback({'id': 'q', 'data': 'edit:mcap', 'message': {'chat': {'id': 3}}})
        assert 'Market cap range' in bot.api.texts(3)[-1]
        await bot.on_message({'chat': {'id': 3}, 'text': '100k-20k'})
        assert bot.api.texts(3)[-1].startswith('⚠️') and bot.pending[3] == 'mcap'
        await bot.on_message({'chat': {'id': 3}, 'text': '-100k'})
        saved = bot.store.spectral_user(3)
        assert (saved['min_mcap'], saved['max_mcap']) == (None, 100_000)
        assert bot.api.texts(3)[-1].startswith('✅ <b>Saved</b>') and 'Up to $100K' in bot.api.texts(3)[-1]
        assert 3 not in bot.pending

        await bot.on_callback({'id': 'q', 'data': 'edit:time', 'message': {'chat': {'id': 3}}})
        await bot.on_message({'chat': {'id': 3}, 'text': '25'})
        assert bot.store.spectral_user(3)['min_minutes'] == 25

        await bot.on_message({'chat': {'id': 3}, 'text': '/range 0.7-2'})
        assert (bot.users[3]['min_mult'], bot.users[3]['max_mult']) == (0.7, 2)
        await bot.on_message({'chat': {'id': 3}, 'text': 'hello'})
        assert '<b>Commands</b>' in bot.api.texts(3)[-1]

    asyncio.run(scenario())


def test_command_menu_includes_start():
    assert COMMANDS[0]['command'] == 'start'
    assert {c['command'] for c in COMMANDS} >= {'start', 'settings', 'show', 'venues'}


def test_venue_filter_and_toggle():
    assert matches(user(venues='pumpswap'), trend(venue='pump-amm'), NOW, 200)
    assert not matches(user(venues='pumpfun,raydium'), trend(venue='pump-amm'), NOW, 200)
    assert matches(user(venues='raydium'), trend(venue='raydium-cpmm'), NOW, 200)
    assert not matches(user(venues='pumpfun'), trend(venue=None), NOW, 200)
    assert matches(user(), trend(venue=None), NOW, 200)

    everything_but_pumpfun = toggle_venue(user(), 'pumpfun')
    assert 'pumpfun' not in everything_but_pumpfun and 'pumpswap' in everything_but_pumpfun
    assert toggle_venue(user(venues=everything_but_pumpfun), 'pumpfun') is None   # back to all
    assert toggle_venue(user(venues='orca'), 'all') is None
    with pytest.raises(ValueError):
        toggle_venue(user(venues='orca'), 'orca')


def test_page_keyboard():
    assert page_keyboard(0, 1) == {'inline_keyboard': [[{'text': '🔄 Refresh', 'callback_data': 'page:0'}]]}
    nav = page_keyboard(1, 3)['inline_keyboard'][0]
    assert [b['callback_data'] for b in nav] == ['page:0', 'noop', 'page:2']
    assert [b['text'] for b in page_keyboard(0, 3)['inline_keyboard'][0]] == ['1 / 3', 'Next ▶️']


def test_show_paginates_without_token_buttons():
    async def scenario():
        bot = make_bot([trend(f'T{i}', age_min=20 + i) for i in range(12)])
        await start(bot, 1)
        await bot.on_message({'chat': {'id': 1}, 'text': '/show'})
        _, chat_id, text, mint = bot.api.calls[-1]
        assert 'Your matches</b> · 12' in text and text.count('<code>') == 5 and mint is None
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
        assert bot.api.calls[-1][0] == 'editMessageText'
        await bot.on_callback({'id': 'q', 'data': 'venue:pumpfun', 'message': msg})
        assert 'pumpfun' not in bot.store.spectral_user(1)['venues']
        assert '▫️ Pumpfun' in str(bot.api.calls[-1][1]['reply_markup'])
        await bot.on_callback({'id': 'q', 'data': 'venue:all', 'message': msg})
        assert bot.store.spectral_user(1)['venues'] is None
        await bot.on_callback({'id': 'q', 'data': 'settings', 'message': msg})
        assert 'All venues' in bot.api.calls[-1][1]['text']

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
    conn.execute('CREATE TABLE trending (mint TEXT PRIMARY KEY, base_mcap_sol REAL NOT NULL, '
                 'ath_mcap_sol REAL NOT NULL, entered_ts INTEGER NOT NULL, last_mult REAL NOT NULL '
                 'DEFAULT 1, active INTEGER NOT NULL DEFAULT 1, stopped_ts INTEGER)')
    conn.execute('CREATE TABLE spectral_deliveries (chat_id INTEGER NOT NULL, mint TEXT NOT NULL, '
                 'ts INTEGER NOT NULL, PRIMARY KEY (chat_id, mint))')
    conn.execute("INSERT INTO spectral_deliveries VALUES (1, 'M', 1)")
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.trending_rows() == []
    assert store.spectral_deliveries()[0]['sent'] == 1      # old deliveries keep their events


def test_kol_buy_only_for_tokens_inside_user_filters():
    from kolbot.kols import build_registry

    async def scenario():
        kols, wallets = build_registry([{'address': 'AM', 'name': 'Alice'}, {'address': 'BM', 'name': 'Bob'}],
                                       [{'address': 'AS', 'name': 'Alice2'}])
        bot = make_bot([trend('TOK', mcap=110)])  # 2.2x, $22K
        bot.store.sync_registry(kols, wallets)
        bot.tracker.kols = kols
        for sig, wallet, sol in (('s1', 'AS', 1.0), ('s2', 'BM', 2.0)):
            bot.store.record_trade(sig, wallet, 'TOK', 'buy', sol, 1000, 100, 'pump', NOW)
        bot.store.record_trade('s3', 'BM', 'TOK', 'sell', 2.0, 1000, 100, 'pump', NOW)
        for chat_id in (1, 2):
            await start(bot, chat_id)
            bot.follow(chat_id, 'TOK', NOW)
        bot.set_filters(2, max_mcap=20_000)        # token is $22K: outside user 2's range
        drain(bot)

        trade = {'mint': 'TOK', 'wallet': 'AS', 'sol': 1.0, 'mcap_sol': 110, 'venue': 'pump'}
        bot.on_kol_buy(kols['alice'], wallets['AS'], trade, {'symbol': 'TOK'}, True)
        sent = drain(bot)
        assert [c for c, _, _ in sent] == [1]
        text = sent[0][2]
        assert text.startswith('🔵 <b>KOL buy · side wallet</b>\n<i>Alice just entered')
        assert '1 holding, 1 out' in text and '<b>Alice</b>' in text and 'Bob' in text and '🔴 sold' in text

        bot.forward('TOK', 'event')
        assert [c for c, _, _ in drain(bot)] == [1]
        bot.tracker.trends['TOK'].active = False     # left trending: no more updates
        bot.forward('TOK', 'event')
        assert drain(bot) == []

    asyncio.run(scenario())
