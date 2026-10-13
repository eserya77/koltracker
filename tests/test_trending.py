import pytest

from kolbot.trending import TrendState, highest_step, observe, trending_base


@pytest.mark.parametrize('mult, step', [
    (1.29, None), (1.3, 1.3), (1.59, 1.3), (1.6, 1.6), (2.4, 2), (2.5, 2.5), (3.9, 3),
    (10, 10), (12.9, 10), (13, 13), (19.5, 19), (20, 20), (24, 20), (49, 40), (50, 50),
    (59.9, 50), (60, 60), (75, 70), (130, 130),
])
def test_highest_step(mult, step):
    assert highest_step(mult) == step


def test_observe_reports_highest_step_once_and_stops_at_drawdown():
    state = TrendState('m', base=100, ath=100, entered_ts=0)
    assert observe(state, 120) is None
    update = observe(state, 270)
    assert (update.kind, update.mult) == ('step', 2.5)
    assert observe(state, 260) is None
    assert observe(state, 1000).mult == 10
    assert observe(state, 301) is None
    stop = observe(state, 300)
    assert stop.kind == 'stop' and not state.active
    assert observe(state, 5000) is None


def test_observe_stops_below_min_mcap_and_tracks_current():
    state = TrendState('m', base=100, ath=100, entered_ts=0)
    assert state.entry == 100
    assert observe(state, 95, min_mcap=10) is None and state.mcap == 95
    stop = observe(state, 9, min_mcap=10)
    assert stop.kind == 'stop' and not state.active


def test_base_prefers_side_wallets():
    rows = [
        {'role': 'main', 'mcap_x_sol': 10 * 500, 'sol_in_priced': 10},
        {'role': 'side', 'mcap_x_sol': 1 * 100, 'sol_in_priced': 1},
        {'role': 'side', 'mcap_x_sol': 3 * 200, 'sol_in_priced': 3},
    ]
    assert trending_base(rows) == pytest.approx(175)
    assert trending_base(rows[:1]) == pytest.approx(500)
