from kolbot.salt import SALT_MAX, SALT_MIN, salt_factor, salted


def test_salt_only_adds_and_is_stable_per_position():
    factors = [salt_factor(f'wallet{i}', 'mint') for i in range(200)]
    assert all(1 + SALT_MIN <= f <= 1 + SALT_MAX for f in factors)
    assert len(set(factors)) > 150
    assert salted(2.0, 'w', 'm') == salted(2.0, 'w', 'm') > 2.0
    assert salted(None, 'w', 'm') is None


def test_salt_depends_on_secret(monkeypatch):
    before = salt_factor('w', 'm')
    monkeypatch.setenv('KOL_SALT_SECRET', 'other')
    assert salt_factor('w', 'm') != before
