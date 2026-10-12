import asyncio
import json

from kolbot.config import TOPIC_EVENTS, TOPIC_MAINS, TOPIC_SIDES, TOPIC_TRENDING, WSOL
from kolbot.db import Store
from kolbot.kols import build_registry
from kolbot.tracker import Tracker

MINT = 'Mint1111111111111111111111111111111111111pump'


class FakeOut:
    def __init__(self):
        self.posts = []

    def post(self, topic, text, mint=None):
        assert mint == MINT
        self.posts.append((topic, text))


class FakeDex:
    async def token_meta(self, mint):
        return {'symbol': 'TEST', 'name': 'Test'}

    async def dex_paid(self, mint):
        return False


def trade(sig, trader, action, sol, tokens, mcap, pool='pump', post=None, event_action=None):
    event = {
        'signature': sig, 'action': event_action or action, 'mint': MINT, 'quoteMint': WSOL,
        'pool': pool, 'marketCapQuote': mcap, 'timestamp': 1_000_000 + int(sig[1:]) * 1000,
        'breakdown': [{'action': action, 'trader': trader, 'tokenAmount': tokens, 'quoteAmount': sol}],
    }
    if post is not None:
        event['postBalances'] = {trader: {'sol': 1.0, MINT: post}}
    return json.dumps(event)


def make_tracker():
    mains = [{'address': 'AM', 'name': 'Alice'}, {'address': 'BM', 'name': 'Bob'}]
    sides = [{'address': 'AS', 'name': 'Alice2'}, {'address': 'BS', 'name': 'Bob2'}]
    kols, wallets = build_registry(mains, sides)
    store = Store(':memory:')
    store.sync_registry(kols, wallets)
    out = FakeOut()
    return Tracker(store, kols, wallets, out, FakeDex()), out


def test_end_to_end_flow():
    async def scenario():
        tracker, out = make_tracker()
        feed = tracker.handle_raw

        feed(trade('s1', 'AS', 'buy', 1.0, 1000, 40))           # Alice side
        feed(trade('s2', 'AM', 'buy', 2.0, 1000, 50))           # Alice main
        assert MINT not in tracker.trends                       # still one KOL
        feed(trade('s3', 'BS', 'buy', 3.0, 1000, 60))           # Bob side -> trending
        assert MINT in tracker.trends
        assert tracker.trends[MINT].base == (1 * 40 + 3 * 60) / 4  # sides only: 55
        feed(trade('s4', 'XX', 'buy', 5.0, 1000, 56, pool='pump-amm'))  # non-KOL trade moves mcap
        feed(trade('s5', 'XX', 'buy', 5.0, 1000, 115))          # 2.09x -> 2x
        feed(trade('s6', 'AS', 'sell', 0.5, 400, 110, post=600))  # partial sell
        feed(trade('s6', 'AS', 'sell', 0.5, 400, 110, post=600))  # duplicate ignored
        feed(json.dumps({'action': 'migrate', 'mint': MINT, 'pool': 'pump-amm',
                         'poolCreatedBy': 'pump', 'signature': 'm1', 'timestamp': 2_000_000}))
        feed(json.dumps({'action': 'add', 'mint': MINT, 'quoteMint': WSOL, 'pool': 'pump-amm',
                         'poolId': 'P', 'signature': 'l1', 'timestamp': 2_000_001,
                         'breakdown': [{'action': 'add', 'trader': 'BM', 'tokenAmount': 10,
                                        'quoteAmount': 1}]}))
        feed(trade('s7', 'XX', 'sell', 5.0, 1000, 30))          # -74% from ATH 115 -> stop
        await asyncio.sleep(0.05)

        assert MINT not in tracker.trends
        topics = [t for t, _ in out.posts]
        assert topics.count(TOPIC_SIDES) == 2 and topics.count(TOPIC_MAINS) == 1
        trend_posts = [text for t, text in out.posts if t == TOPIC_TRENDING]
        assert len(trend_posts) == 1 and '<b>2x</b>' in trend_posts[0]
        event_posts = [text for t, text in out.posts if t == TOPIC_EVENTS]
        assert len(event_posts) == 2
        assert any('Migrated to PumpSwap' in p for p in event_posts)
        assert any('Liquidity added' in p for p in event_posts)

        main_post = next(text for t, text in out.posts if t == TOPIC_MAINS)
        assert '<blockquote expandable>' in main_post and 'Alice2' in main_post
        assert 'Avg entry: 40 SOL' in main_post
        assert 'href' not in main_post
        pos = tracker.store.position('AS', MINT)
        assert pos['sell_count'] == 1 and pos['balance'] == 600
        assert tracker.store.conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0] == 4

    asyncio.run(scenario())
