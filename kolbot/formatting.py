"""Telegram HTML message builders.

Every message opens with the token header, the KOL line and the CA, and
separates sections with a blank line. KOL wallets are never shown or linked,
and KOL amounts are salted (see salt.py).
"""

from html import escape

from .salt import salted

VENUES = {
    'pump': 'Pump.fun',
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


def holding_status(position):
    bought = position['bought_tokens']
    if bought <= 0:
        return '—'
    balance = position['balance']
    held = balance if balance is not None else max(bought - position['sold_tokens'], 0.0)
    ratio = held / bought
    if ratio >= 0.98:
        return '🟢 Holding 100%'
    if ratio <= 0.02:
        return '🔴 Fully sold'
    return f'🟡 Holding {ratio:.0%}'


def avg_entry(position):
    if position['sol_in_priced'] <= 0:
        return None
    return position['mcap_x_sol'] / position['sol_in_priced']


def position_sol(position):
    return salted(position['sol_in'], position['wallet'], position['mint'])


# --- building blocks ---------------------------------------------------------
def section(*lines):
    return '\n'.join(line for line in lines if line)


def message(*sections):
    return '\n\n'.join(part for part in sections if part)


def header(icon, meta, kol_text):
    symbol = escape((meta or {}).get('symbol') or '???')
    name = escape((meta or {}).get('name') or '')
    title = f'{icon} <b>${symbol}</b>' + (f' | {name}' if name else '')
    return section(title, f'💳 <b>{escape(kol_text)}</b>' if kol_text else None)


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


def position_block(title, position, sol_usd, now=None):
    holding_for = None
    if now is not None and position['buy_count'] > 1 and position['first_buy_ts']:
        holding_for = f'Holding for: {fmt_duration(now - position["first_buy_ts"])}'
    return section(f'<b>{title}</b>',
                   f'Total: {fmt_sol(position_sol(position))} · {fmt_buys(position["buy_count"])}',
                   f'Avg entry: {fmt_mcap(avg_entry(position), sol_usd)}',
                   holding_for,
                   f'Status: {holding_status(position)}')


def wallets_quote(title, rows, sol_usd):
    """Collapsible Telegram quote with one entry per wallet."""
    if not rows:
        return None
    entries = [section(f'<b>{escape(row["label"])}</b>',
                       f'{fmt_sol(position_sol(row))} · {fmt_buys(row["buy_count"])}',
                       f'Avg entry: {fmt_mcap(avg_entry(row), sol_usd)}',
                       holding_status(row))
               for row in rows]
    return f'<blockquote expandable><b>{title}</b>\n\n' + '\n\n'.join(entries) + '</blockquote>'


# --- messages ----------------------------------------------------------------
def main_buy(kol, wallet, trade, meta, own, sides, sol_usd, paid, now):
    return message(
        header('🟢', meta, kol.name),
        ca(trade['mint']),
        trade_block(trade, sol_usd),
        position_block('👤 Main wallet', own, sol_usd, now),
        wallets_quote(f'🕶 Side wallets ({len(sides)})', sides, sol_usd),
        dex_paid_line(paid),
    )


def side_buy(kol, wallet, trade, meta, own, kol_rows, sol_usd, paid, now):
    side_rows = [r for r in kol_rows if r['role'] == 'side']
    main_rows = [r for r in kol_rows if r['role'] == 'main']
    others = [r for r in side_rows if r['wallet'] != wallet.address]
    totals = section(
        f'<b>📦 {escape(kol.name)} totals</b>',
        f'Side wallets: {fmt_sol(sum(position_sol(r) for r in side_rows))} · '
        f'{fmt_buys(sum(r["buy_count"] for r in side_rows))} · {len(side_rows)} '
        f'{"wallet" if len(side_rows) == 1 else "wallets"}',
        *(f'Main wallet: {fmt_sol(position_sol(r))} · {holding_status(r)}' for r in main_rows),
    )
    return message(
        header('🔵', meta, wallet.label),
        ca(trade['mint']),
        trade_block(trade, sol_usd),
        position_block('👤 This wallet', own, sol_usd, now),
        totals,
        wallets_quote(f'🕶 Other side wallets ({len(others)})', others, sol_usd),
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
        header('🎓', meta, ', '.join(holders)),
        ca(mint),
        section(f'🎓 <b>Migrated to {venue_name(event.get("pool"))}</b>',
                f'📊 MC {fmt_mcap(mcap_sol, sol_usd)}',
                f'🔥 Burned liquidity: {escape(str(burned))}' if burned is not None else None),
    )


def kol_create(kol, wallet, mint, meta, event, sol_usd, mcap_sol):
    return message(
        header('🆕', meta, wallet.label),
        ca(mint),
        section('🆕 <b>Token created</b>',
                f'🏦 {venue_name(event.get("pool"))}',
                f'📊 MC {fmt_mcap(mcap_sol, sol_usd)}'),
    )


def liquidity(kol, wallet, mint, meta, action, sol, tokens, event):
    icon, title = ('💧', 'Liquidity added') if action == 'add' else ('🏜', 'Liquidity removed')
    return message(
        header(icon, meta, wallet.label),
        ca(mint),
        section(f'{icon} <b>{title}</b>',
                f'💰 {fmt_sol(salted(sol, wallet.address, mint))} + '
                f'{fmt_num(salted(tokens or 0, wallet.address, mint))} tokens',
                f'🏦 {venue_name(event.get("pool"))}'),
    )


def dex_event(type_, mint, meta, item, holders):
    if type_ == 'dex_paid':
        icon, details = '🦅', section('🦅 <b>DEX Paid</b>')
    elif type_ == 'cto':
        icon, details = '🏴', section('🏴 <b>Community takeover (CTO)</b>')
    elif type_ == 'boost':
        icon, details = '⚡', section('⚡ <b>Boost</b>',
                                      f'+{item.get("amount", "?")} (total {item.get("totalAmount", "?")})')
    else:
        icon, details = '📣', section('📣 <b>Ad</b>',
                                      f'{escape(str(item.get("type") or "ad"))} · {item.get("durationHours", "?")}h')
    return message(header(icon, meta, ', '.join(holders)), ca(mint), details)
