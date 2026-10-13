"""Telegram HTML message builders.

Buy and trending messages open with the token header, the KOL line and the CA;
event messages open with the event name. Sections are separated by a blank
line. KOL wallets are never shown or linked, and KOL amounts are salted
(see salt.py).
"""

from html import escape

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

# Event type -> (emoji, name shown on the first line).
EVENTS = {
    'migrate': ('🎓', 'MIGRATION'),
    'create': ('🧪', 'TOKEN CREATED'),
    'add': ('💧', 'LIQUIDITY ADDED'),
    'remove': ('🏜', 'LIQUIDITY REMOVED'),
    'dex_paid': ('🦅', 'DEX PAID'),
    'cto': ('🏴‍☠️', 'COMMUNITY TAKEOVER'),
    'boost': ('⚡', 'DEX BOOST'),
    'ad': ('📣', 'DEX AD'),
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


def avg_entry(rows):
    """SOL-weighted average entry market cap over one or more positions."""
    priced = sum(r['sol_in_priced'] for r in rows)
    if priced <= 0:
        return None
    return sum(r['mcap_x_sol'] for r in rows) / priced


def position_sol(position):
    return salted(position['sol_in'], position['wallet'], position['mint'])


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


def header(icon, meta, kol_text):
    return section(f'{icon} {token_line(meta)}', kol_line(kol_text))


def event_header(type_, meta, kol_text):
    icon, name = EVENTS[type_]
    return section(f'{icon} <b>{name}</b>', token_line(meta), kol_line(kol_text))


def ca(mint):
    return f'<code>{mint}</code>'


def dex_paid_line(paid):
    if paid is None:
        return None
    return '🦅 DEX Paid ✅' if paid else '🦅 DEX Paid ❌'


def trade_block(trade, sol_usd):
    sol = salted(trade['sol'], trade['wallet'], trade['mint'])
    return section(f'🛒 Bought <b>{fmt_sol(sol)}</b>',
                   f'📊 MC {fmt_mcap(trade["mcap_sol"], sol_usd)}',
                   f'🏦 {venue_name(trade["venue"])}')


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


# --- messages ------------------------------------------------------------------
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


def migration(mint, meta, event, mcap_sol, sol_usd, holders):
    burned = event.get('burnedLiquidity')
    return message(
        event_header('migrate', meta, ', '.join(holders)),
        ca(mint),
        section(f'🏦 New pool: {venue_name(event.get("pool"))}',
                f'📊 MC {fmt_mcap(mcap_sol, sol_usd)}',
                f'🔥 Burned liquidity: {escape(str(burned))}' if burned is not None else None),
    )


def kol_create(kol, wallet, mint, meta, event, sol_usd, mcap_sol):
    return message(
        event_header('create', meta, wallet_role(kol, wallet)),
        ca(mint),
        section(f'🏦 {venue_name(event.get("pool"))}',
                f'📊 MC {fmt_mcap(mcap_sol, sol_usd)}'),
    )


def liquidity(kol, wallet, mint, meta, action, sol, tokens, event):
    return message(
        event_header(action, meta, wallet_role(kol, wallet)),
        ca(mint),
        section(f'💰 {fmt_sol(salted(sol, wallet.address, mint))} + '
                f'{fmt_num(salted(tokens or 0, wallet.address, mint))} tokens',
                f'🏦 {venue_name(event.get("pool"))}'),
    )


def dex_event(type_, mint, meta, item, holders):
    details = None
    if type_ == 'boost':
        details = f'⚡ +{item.get("amount", "?")} (total {item.get("totalAmount", "?")})'
    elif type_ == 'ad':
        details = f'📣 {escape(str(item.get("type") or "ad"))} · {item.get("durationHours", "?")}h'
    return message(event_header(type_, meta, ', '.join(holders)), ca(mint), details)
