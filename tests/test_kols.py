from kolbot.config import MAINS_PATH, SIDES_PATH
from kolbot.kols import build_registry, full_name, load_registry


def test_full_name_joins_text_suffix_only():
    assert full_name({'name': 'Ethan', 'emoji': 'Prosper7'}) == 'Ethan Prosper7'
    assert full_name({'name': 'Cented1', 'emoji': '📟'}) == 'Cented1'
    assert full_name({'name': 'King', 'emoji': '😵'}) == 'King'


def test_resolution_rules():
    mains = [{'address': 'M1', 'name': 'Cented', 'emoji': '🙂'}]
    sides = [
        {'address': 'S1', 'name': 'Cented1', 'emoji': '📟'},
        {'address': 'S2', 'name': 'Cented4', 'emoji': '🚛'},
        {'address': 'P0', 'name': 'Ethan', 'emoji': 'Prosper'},
        {'address': 'P1', 'name': 'Ethan', 'emoji': 'Prosper1'},
        {'address': 'H1', 'name': 'h141', 'emoji': '🙂'},
    ]
    kols, wallets = build_registry(mains, sides)
    assert [w.address for w in kols['cented'].mains] == ['M1']
    assert {w.address for w in kols['cented'].sides} == {'S1', 'S2'}
    assert [w.address for w in kols['ethan prosper'].mains] == ['P0']
    assert [w.address for w in kols['ethan prosper'].sides] == ['P1']
    assert kols['h'].mains == [] and wallets['H1'].role == 'side'


def test_real_files():
    kols, wallets = load_registry(MAINS_PATH, SIDES_PATH)
    assert len(wallets) == 1392
    assert wallets['8YcbyX92UHTU23HZv3ccP9o13qibErQkKaUjoxqxd7SJ'].role == 'side'  # Cented1
    assert kols['cented'].mains[0].address == 'CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o'
    with_sides = [k for k in kols.values() if k.sides]
    assert sorted(k.id for k in with_sides if not k.mains) == ['cryptovillain', 'fz', 'h']
