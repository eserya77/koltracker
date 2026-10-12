"""Groups the wallets of mains.json and kolsides.json into KOLs.

The exported lists split long names: ``name`` holds the first word and
``emoji`` the rest when it is plain text (``Ethan`` + ``Prosper7``).

Main wallet resolution, per KOL (name without trailing digits):
1. the wallet(s) in mains.json with that name;
2. otherwise the kolsides wallet whose name has no trailing number;
3. otherwise the KOL only has side wallets.
"""

import json
import re
from dataclasses import dataclass, field

_TRAILING_DIGITS = re.compile(r'\d+$')


@dataclass
class Wallet:
    address: str
    label: str
    kol_id: str
    role: str  # 'main' | 'side'


@dataclass
class Kol:
    id: str
    name: str
    mains: list = field(default_factory=list)
    sides: list = field(default_factory=list)


def full_name(entry):
    name = (entry.get('name') or '').strip()
    extra = (entry.get('emoji') or '').strip()
    if extra and extra.isascii() and extra.isprintable():
        return f'{name} {extra}'.strip()
    return name


def kol_key(label):
    return _TRAILING_DIGITS.sub('', label).strip().lower()


def has_number(label):
    return bool(_TRAILING_DIGITS.search(label))


def build_registry(mains, sides):
    kols = {}
    wallets = {}

    def kol_for(key, display):
        kol = kols.get(key)
        if kol is None:
            kol = kols[key] = Kol(id=key, name=display)
        return kol

    def add(entry, kol, role):
        address = entry['address']
        if address in wallets:
            return
        wallet = Wallet(address, full_name(entry), kol.id, role)
        wallets[address] = wallet
        (kol.mains if role == 'main' else kol.sides).append(wallet)

    for entry in mains:
        label = full_name(entry)
        add(entry, kol_for(kol_key(label), label), 'main')

    grouped = {}
    for entry in sides:
        grouped.setdefault(kol_key(full_name(entry)), []).append(entry)

    for key, entries in grouped.items():
        has_main = key in kols
        display = _TRAILING_DIGITS.sub('', full_name(entries[0])).strip() or full_name(entries[0])
        kol = kol_for(key, display)
        for entry in entries:
            role = 'main' if not has_main and not has_number(full_name(entry)) else 'side'
            add(entry, kol, role)

    return kols, wallets


def load_registry(mains_path, sides_path):
    with open(mains_path, encoding='utf-8') as fh:
        mains = json.load(fh)
    with open(sides_path, encoding='utf-8') as fh:
        sides = json.load(fh)
    return build_registry(mains, sides)
