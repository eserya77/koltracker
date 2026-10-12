"""Trending multiplier ladder and per-token market cap tracking."""

from dataclasses import dataclass

from .config import TRENDING_STOP_DRAWDOWN

LADDER_HEAD = (1.3, 1.6, 2, 2.5, 3, 4, 5, 6, 7, 8, 9, 10, 13, 16, 19, 20, 25, 30, 40, 50)
LADDER_TAIL_STEP = 10


def highest_step(mult):
    """Highest ladder step <= mult, or None below the first step."""
    if mult < LADDER_HEAD[0]:
        return None
    if mult < LADDER_HEAD[-1]:
        return max(step for step in LADDER_HEAD if step <= mult)
    return LADDER_HEAD[-1] + int((mult - LADDER_HEAD[-1]) // LADDER_TAIL_STEP) * LADDER_TAIL_STEP


def weighted_entry(positions):
    """SOL-weighted average entry market cap over position rows."""
    total = sum(p['sol_in_priced'] for p in positions)
    if total <= 0:
        return None
    return sum(p['mcap_x_sol'] for p in positions) / total


def trending_base(positions):
    """Average entry of side wallets; falls back to every KOL wallet."""
    return weighted_entry([p for p in positions if p['role'] == 'side']) or weighted_entry(positions)


@dataclass
class TrendState:
    mint: str
    base: float
    ath: float
    entered_ts: int
    last_mult: float = 1.0
    active: bool = True


@dataclass
class TrendUpdate:
    kind: str  # 'step' | 'stop'
    mult: float
    mcap: float
    ath: float


def observe(state, mcap):
    """Feed a new market cap. Returns a TrendUpdate when something is reportable."""
    if not state.active or mcap is None or mcap <= 0:
        return None
    if mcap > state.ath:
        state.ath = mcap
    step = highest_step(mcap / state.base)
    if step is not None and step > state.last_mult:
        state.last_mult = step
        return TrendUpdate('step', step, mcap, state.ath)
    if mcap <= state.ath * (1 - TRENDING_STOP_DRAWDOWN):
        state.active = False
        return TrendUpdate('stop', state.last_mult, mcap, state.ath)
    return None
