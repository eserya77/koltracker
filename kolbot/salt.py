"""Inflates KOL amounts slightly so posted numbers can't be matched to on-chain wallets.

The factor is stable per (wallet, mint), so every message about the same
position shows consistent totals, and depends on a secret so it can't be undone.
"""

import hashlib
import os

SALT_MIN = 0.02
SALT_MAX = 0.08


def salt_factor(wallet, mint):
    secret = os.environ.get('KOL_SALT_SECRET', 'kolbot')
    digest = hashlib.sha256(f'{secret}:{wallet}:{mint}'.encode()).digest()
    fraction = int.from_bytes(digest[:4], 'big') / 0xFFFFFFFF
    return 1 + SALT_MIN + fraction * (SALT_MAX - SALT_MIN)


def salted(amount, wallet, mint):
    if amount is None:
        return None
    return amount * salt_factor(wallet, mint)
