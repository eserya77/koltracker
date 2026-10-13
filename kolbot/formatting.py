"""Telegram HTML message builders.

Buy and trending messages open with the token header, the KOL line and the CA.
Event and Spectral messages open with a title plus a one-line explanation of
what happened. Sections are separated by a blank line. KOL wallets are never
shown or linked, and KOL amounts are salted (see salt.py).
"""

from html import escape

from .config import ARCHON_URL, VENUE_GROUPS
from .salt import salted

VENUES = {
    'pump': 'Pumpfun',
    'pump-amm': 'PumpSwap',
    'raydium-launchpad': 'Raydium LaunchLab',
    'raydium-cpmm': 'Raydium CPMM',
    'raydium-clmm': 'Raydium CLMM',
    'raydium-v4': 'Raydium V4',
    'meteora-launchpad': 'Meteora DBC',
    'meteora-damm-v1': 'Meteora DAMM v1',
    'meteora-damm-v2': 'Meteora DAMM v2',
    'meteora-dlmm': 'Meteora DLMM',
    'orca-whirlpool': 'Orca',
}

# Event type -> (emoji, title, explanation template). Templates may use
# {kol}, {wallet} and {venue}; values are HTML-escaped before formatting.
EVENTS = {
    'migrate': ('🎓', 'Migrated',
                'The bonding curve is complete. The token now trades on {venue}.'),
    'create': ('🧪', 'New launch by a KOL', '{kol} created this token from {wallet}.'),
    'add': ('💧', 'Liquidity added', "{kol} deposited liquidity into this token's {venue} pool."),
    'remove': ('🚱', 'Liquidity removed', "{kol} withdrew liquidity from this token's {venue} pool."),
    'dex_paid': ('🦅', 'DEX paid',
                 "Someone paid for this token's DexScreener profile (logo, socials, banner). "
                 'Usually the dev or the community investing in it.'),
    'cto': ('🏴‍☠️', 'Community takeover',
            "The original dev left and the community now runs the token's DexScreener page."),
    'boost': ('⚡', 'DexScreener boost', "Someone paid to push this token up DexScreener's trending list."),
    'ad': ('📣', 'DexScreener ad', 'A paid ad for this token is running on DexScreener.'),
    'match': ('🎯', 'New match', '2+ KOLs are in this token and it fits your filters.'),
    'buy_entered': ('{icon}', 'KOL buy · {side}', '{kol} just entered a token you received.'),
    'buy_added': ('{icon}', 'KOL buy · {side}', '{kol} bought more of a token you received.'),
}


def venue_name(pool):
    return VENUES.get(pool or '', pool or '?')


def fmt_num(value):
    value = float(value)
    for limit, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if abs(value) >= limit:
            return f'{value / limit:.2f}'.rstrip('0').rstrip('.') + suffix
    return f'{value:.2f}'.rstrip('0').rstrip('.') if value else '0'


def fmt_sol(value):
    if value is None:
        return '?'
    return f'{value:.3f}'.rstrip('0').rstrip('.') + ' SOL' if value < 100 else f'{fmt_num(value)} SOL'


def fmt_mcap(mcap_sol, sol_usd):
    if mcap_sol is None:
        return '?'
    if sol_usd:
        return '$' + fmt_num(mcap_sol * sol_usd)
    return fmt_num(mcap_sol) + ' SOL'


def fmt_duration(ms):
    seconds = max(int(ms // 1000), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    if minutes:
        return f'{minutes}m {seconds}s'
    return f'{seconds}s'


def fmt_mult(mult):
    return f'{mult:g}x'


def fmt_buys(count):
    return f'{count} buy' if count == 1 else f'{count} buys'


# --- positions -----------------------------------------------------------------
def held_tokens(position):
    if position['balance'] is not None:
        return position['balance']
    return max(position['bought_tokens'] - position['sold_tokens'], 0.0)


def holding_status(bought, held):
    if bought <= 0:
        return '—'
    ratio = held / bought
    if ratio >= 0.98:
        return '🟢 Holding 100%'
    if ratio <= 0.02:
        return '🔴 Fully sold'
    return f'🟡 Holding {ratio:.0%}'


def short_status(bought, held):
    if bought <= 0:
        return '—'
    ratio = held / bought
    if ratio >= 0.98:
        return '🟢 holding'
    if ratio <= 0.02:
        return '🔴 sold'
    return f'🟡 {ratio:.0%} left'


def avg_entry(rows):
    """SOL-weighted average entry market cap over one or more positions."""
    priced = sum(r['sol_in_priced'] for r in rows)
    if priced <= 0:
        return None
    return sum(r['mcap_x_sol'] for r in rows) / priced


def position_sol(position):
    return salted(position['sol_in'], position['wallet'], position['mint'])


def kols_in_token(rows, kols):
    """Per-KOL totals for a token: [(kol_id, name, salted SOL, bought, held)], biggest first."""
    totals = {}
    for row in rows:
        entry = totals.setdefault(row['kol_id'], [0.0, 0.0, 0.0])
        entry[0] += position_sol(row)
        entry[1] += row['bought_tokens']
        entry[2] += held_tokens(row)
    return sorted(((kol_id, kols[kol_id].name, sol, bought, held)
                   for kol_id, (sol, bought, held) in totals.items()), key=lambda e: -e[2])


# --- building blocks -----------------------------------------------------------
def section(*lines):
    return '\n'.join(line for line in lines if line)


def message(*sections):
    return '\n\n'.join(part for part in sections if part)


def token_line(meta):
    symbol = escape((meta or {}).get('symbol') or '???')
    name = escape((meta or {}).get('name') or '')
    return f'<b>${symbol}</b>' + (f' | {name}' if name else '')


def kol_line(text):
    return f'💳 <b>{escape(text)}</b>' if text else None


def wallet_role(kol, wallet):
    if wallet.role == 'main':
        return f'{kol.name} · Main wallet'
    return f'{kol.name} · Side wallet ({wallet.label})'


def wallet_kind(wallet):
    return 'their main wallet' if wallet.role == 'main' else 'a side wallet'


def header(icon, meta, kol_text):
    return section(f'{icon} {token_line(meta)}', kol_line(kol_text))


def event_title(type_, **values):
    icon, title, explanation = EVENTS[type_]
    safe = {k: escape(str(v)) for k, v in values.items()}
    return section(f'{icon.format(**safe)} <b>{title.format(**safe)}</b>',
                   f'<i>{explanation.format(**safe)}</i>')


def token_block(mint, meta):
    return section(token_line(meta), ca(mint))


def kols_in_line(names):
    if not names:
        return None
    return f'👥 KOLs in: {escape(", ".join(names))}'


def ca(mint):
    return f'<code>{mint}</code>'


def dex_paid_line(paid):
    if paid is None:
        return None
    return '🦅 DEX paid: ✅' if paid else '🦅 DEX paid: ❌'


def trade_block(trade, sol_usd):
    sol = salted(trade['sol'], trade['wallet'], trade['mint'])
    return section(f'🛒 Bought: <b>{fmt_sol(sol)}</b>',
                   f'📊 MC: {fmt_mcap(trade["mcap_sol"], sol_usd)}',
                   f'🏦 Venue: {venue_name(trade["venue"])}')


def group_block(title, rows, sol_usd, now):
    """Combined position of a set of wallets (the main, or all side wallets)."""
    if not rows:
        return section(f'<b>{title}</b>', 'No position')
    bought = sum(r['bought_tokens'] for r in rows)
    first_buy = min((r['first_buy_ts'] for r in rows if r['first_buy_ts']), default=None)
    holding_for = None
    if first_buy and now - first_buy >= 60_000:
        holding_for = f'Holding for: {fmt_duration(now - first_buy)}'
    return section(f'<b>{title}</b>',
                   f'Total: {fmt_sol(sum(position_sol(r) for r in rows))} · '
                   f'{fmt_buys(sum(r["buy_count"] for r in rows))}',
                   f'Avg entry: {fmt_mcap(avg_entry(rows), sol_usd)}',
                   holding_for,
                   f'Status: {holding_status(bought, sum(held_tokens(r) for r in rows))}')


def side_breakdown(rows, sol_usd):
    """Collapsible per-wallet detail; only useful with two or more side wallets."""
    if len(rows) < 2:
        return None
    entries = [section(f'<b>{escape(r["label"])}</b>',
                       f'{fmt_sol(position_sol(r))} · {fmt_buys(r["buy_count"])}',
                       f'Avg entry: {fmt_mcap(avg_entry([r]), sol_usd)}',
                       holding_status(r['bought_tokens'], held_tokens(r)))
               for r in rows]
    return '<blockquote expandable><b>🔍 Side wallet breakdown</b>\n\n' + '\n\n'.join(entries) + '</blockquote>'


def kols_block(entries, highlight=None, limit=10):
    """'Who is in' list: KOLs still holding first, then those who sold."""
    if not entries:
        return None
    ordered = sorted(entries, key=lambda e: (e[4] <= 0.02 * e[3], -e[2]))
    lines = []
    for kol_id, name, sol, bought, held in ordered[:limit]:
        label = f'<b>{escape(name)}</b>' if kol_id == highlight else escape(name)
        lines.append(f'{label} · {fmt_sol(sol)} · {short_status(bought, held)}')
    if len(ordered) > limit:
        lines.append(f'+{len(ordered) - limit} more')
    inside = sum(1 for e in entries if e[4] > 0.02 * e[3])
    return section(f'👥 <b>KOLs in this token</b> · {inside} holding, {len(entries) - inside} out', *lines)


# --- KOL group messages --------------------------------------------------------
def buy(kol, wallet, trade, meta, kol_rows, sol_usd, paid, now):
    """Same layout for main and side buys; only the icon and buyer line differ."""
    mains = [r for r in kol_rows if r['role'] == 'main']
    sides = [r for r in kol_rows if r['role'] == 'side']
    side_title = f'🕶 Side wallets ({len(sides)})' if sides else '🕶 Side wallets'
    return message(
        header('🟢' if wallet.role == 'main' else '🔵', meta, wallet_role(kol, wallet)),
        ca(trade['mint']),
        trade_block(trade, sol_usd),
        group_block('👤 Main wallet', mains, sol_usd, now),
        group_block(side_title, sides, sol_usd, now),
        side_breakdown(sides, sol_usd),
        dex_paid_line(paid),
    )


def trending_step(mint, meta, state, update, kol_names, sol_usd, now):
    return message(
        header('🚀', meta, ', '.join(kol_names)),
        ca(mint),
        section(f'📈 <b>{fmt_mult(update.mult)}</b> in {fmt_duration(now - state.entered_ts)}'),
        section(f'🎯 Entry MC: {fmt_mcap(state.base, sol_usd)}',
                f'📊 Now: <b>{fmt_mcap(update.mcap, sol_usd)}</b>',
                f'🏔 ATH: {fmt_mcap(update.ath, sol_usd)}'),
    )


# --- events (KOL group Events topic and Spectral) -------------------------------
def migration(mint, meta, event, mcap_sol, sol_usd, holders):
    burned = event.get('burnedLiquidity')
    return message(
        event_title('migrate', venue=venue_name(event.get('pool'))),
        token_block(mint, meta),
        section(f'📊 MC: {fmt_mcap(mcap_sol, sol_usd)}',
                f'🔥 Liquidity burned: {escape(str(burned))}' if burned is not None else None),
        kols_in_line(holders),
    )


def kol_create(kol, wallet, mint, meta, event, sol_usd, mcap_sol):
    return message(
        event_title('create', kol=kol.name, wallet=wallet_kind(wallet)),
        token_block(mint, meta),
        section(f'🏦 Launchpad: {venue_name(event.get("pool"))}',
                f'📊 MC: {fmt_mcap(mcap_sol, sol_usd)}'),
        kol_line(wallet_role(kol, wallet)),
    )


def liquidity(kol, wallet, mint, meta, action, sol, tokens, event):
    return message(
        event_title(action, kol=kol.name, venue=venue_name(event.get('pool'))),
        token_block(mint, meta),
        section(f'💰 Amount: {fmt_sol(salted(sol, wallet.address, mint))} + '
                f'{fmt_num(salted(tokens or 0, wallet.address, mint))} tokens'),
        kol_line(wallet_role(kol, wallet)),
    )


def dex_event(type_, mint, meta, item, holders):
    details = None
    if type_ == 'boost':
        details = f'⚡ +{item.get("amount", "?")} boosts · {item.get("totalAmount", "?")} active'
    elif type_ == 'ad':
        details = f'📣 {escape(str(item.get("type") or "ad"))} · {item.get("durationHours", "?")}h'
    return message(event_title(type_), token_block(mint, meta), details, kols_in_line(holders))


# --- Spectral ------------------------------------------------------------------
def fmt_range(min_mult, max_mult):
    if min_mult is None and max_mult is None:
        return 'Any'
    if max_mult is None:
        return f'{fmt_mult(min_mult)} or more'
    if min_mult is None:
        return f'Up to {fmt_mult(max_mult)}'
    return f'{fmt_mult(min_mult)} – {fmt_mult(max_mult)}'


def fmt_usd_range(low, high):
    if low is None and high is None:
        return 'Any'
    if high is None:
        return f'${fmt_num(low)} or more'
    if low is None:
        return f'Up to ${fmt_num(high)}'
    return f'${fmt_num(low)} – ${fmt_num(high)}'


def market_line(state, sol_usd):
    return f'📊 MC: <b>{fmt_mcap(state.mcap, sol_usd)}</b> · {state.mcap / state.entry:.2f}x since tracked'


def venue_label(state):
    return f'🏦 Venue: {venue_name(state.venue)}' if state.venue else None


def spectral_match(mint, meta, state, kols, sol_usd, paid, now):
    return message(
        event_title('match'),
        token_block(mint, meta),
        section(market_line(state, sol_usd),
                f'🏁 Tracked at: {fmt_mcap(state.entry, sol_usd)} · 🏔 ATH: {fmt_mcap(state.ath, sol_usd)}',
                f'⏱ In trending: {fmt_duration(now - state.entered_ts)}',
                venue_label(state)),
        kols_block(kols),
        dex_paid_line(paid),
    )


def spectral_buy(kol, wallet, trade, meta, state, kols, sol_usd, paid, first_buy):
    sol = salted(trade['sol'], trade['wallet'], trade['mint'])
    return message(
        event_title('buy_entered' if first_buy else 'buy_added', kol=kol.name,
                    icon='🟢' if wallet.role == 'main' else '🔵',
                    side='main wallet' if wallet.role == 'main' else 'side wallet'),
        token_block(trade['mint'], meta),
        section(f'🛒 Bought: <b>{fmt_sol(sol)}</b> at {fmt_mcap(trade["mcap_sol"], sol_usd)} MC',
                f'🏦 Venue: {venue_name(trade["venue"])}'),
        kols_block(kols, highlight=kol.id),
        section(market_line(state, sol_usd), dex_paid_line(paid)),
    )


def archon_link(mint, meta):
    symbol = escape((meta or {}).get('symbol') or mint[:6])
    return f'<a href="{ARCHON_URL.format(mint=mint)}">${symbol}</a>'


def spectral_list(items, total, start, sol_usd, now):
    """One page of matches. items: [(meta, state, kol_count)]; start: index of the first item."""
    title = section(f'📋 <b>Your matches</b> · {total}' if total else '📋 <b>Your matches</b>',
                    '<i>Trending tokens that fit your filters right now. Tap a symbol to trade on Archon.</i>'
                    if total else '<i>No trending token fits your filters right now. '
                                  'New matches will arrive here automatically.</i>')
    entries = []
    for i, (meta, state, kol_count) in enumerate(items, start + 1):
        name = escape((meta or {}).get('name') or '')
        entries.append(section(
            f'<b>{i}.</b> {archon_link(state.mint, meta)}' + (f' · {name}' if name else ''),
            f'{fmt_mcap(state.mcap, sol_usd)} · {state.mcap / state.entry:.2f}x · '
            f'{fmt_duration(now - state.entered_ts)} · {kol_count} KOLs',
            ca(state.mint)))
    return message(title, *entries)


def fmt_venues(user):
    if not user.get('venues'):
        return 'All venues'
    keys = user['venues'].split(',')
    return ', '.join(VENUE_GROUPS[k][0] for k in VENUE_GROUPS if k in keys)


def spectral_settings(user, saved=False):
    return message(
        '✅ <b>Saved</b>' if saved else None,
        '⚙️ <b>Your filters</b>',
        section('⏱ <b>Min time in trending</b>', f'{user["min_minutes"]} min'),
        section('📈 <b>Multiplier since tracked</b>', fmt_range(user['min_mult'], user['max_mult'])),
        section('💰 <b>Market cap</b>', fmt_usd_range(user['min_mcap'], user['max_mcap'])),
        section('🏦 <b>Venues</b>', escape(fmt_venues(user))),
        '<i>A token reaches you once it has been in trending for the min time, both ranges fit and it '
        'trades on one of your venues. Tap a button to change a filter.</i>',
    )


def spectral_venues(user):
    return message(
        '🏦 <b>Venues</b>',
        "<i>Only get tokens trading on the venues you pick. Tap a venue to turn it on or off.</i>",
        section('<b>Selected</b>', escape(fmt_venues(user))),
    )


def spectral_welcome():
    return message(
        '👋 <b>Welcome to Spectral</b>',
        'Spectral follows hundreds of KOL wallets. When 2 or more KOLs buy the same token it '
        'enters trending, and you get the ones that fit your filters.',
        section('<b>What you will receive</b>',
                '🎯 New match: a trending token that fits your filters',
                '🟢🔵 KOL buys on the tokens you received',
                '🎓 Migrations · 💧 Liquidity · 🦅 DEX paid',
                '🏴‍☠️ Community takeovers · ⚡ Boosts · 📣 Ads'),
        '<i>Updates for a token stop once it leaves your filters or trending. Tokens that already fit '
        "right now aren't sent, so you start clean; use /show to see them.</i>",
    )


def spectral_help():
    return message(
        '🧭 <b>Commands</b>',
        section('/start · start Spectral',
                '/settings · your filters',
                '/show · tokens that fit your filters now',
                '/time 10 · min minutes in trending',
                '/range 0.7-2 · multiplier since tracked',
                '/mcap 20k-100k · market cap',
                '/venues · pick your venues'),
        '<i>Ranges can be open: <code>20k-</code> means 20K or more, <code>-100k</code> up to 100K, '
        '<code>any</code> no limit.</i>',
    )
