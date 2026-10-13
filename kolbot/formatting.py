"""Telegram HTML message builders.

KOL group buy and trending messages open with the token header, the KOL line
and the CA. Event and Spectral messages follow one layout:

    <emoji> <b>Title naming the token</b>
    <i>one short line explaining what happened</i>

    <code>CA</code>

    <blockquote>details, one "Label: value" per line</blockquote>

KOL wallets are never shown or linked, and KOL amounts are salted (see salt.py).
"""

from html import escape

from .config import ALERT_TYPES, ARCHON_URL, VENUE_GROUPS
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

# Event type -> (emoji, title template, explanation template). Templates use
# {sym}, {kol} and {venue}; values are HTML-escaped before formatting.
EVENTS = {
    'migrate': ('🎓', '{sym} migrated', 'Bonding curve complete. Now trading on {venue}.'),
    'create': ('🧪', '{kol} launched {sym}', 'A KOL created this token.'),
    'add': ('💧', '{kol} added liquidity to {sym}', 'A KOL deposited SOL and tokens into the pool.'),
    'remove': ('🚱', '{kol} pulled liquidity from {sym}', 'A KOL withdrew their SOL and tokens from the pool.'),
    'dex_paid': ('🦅', '{sym} is DEX paid', 'Someone paid for its DexScreener profile, usually the dev or community.'),
    'cto': ('🏴‍☠️', '{sym} community takeover', 'The dev left and the community took over its DexScreener page.'),
    'boost': ('⚡', '{sym} got boosted', 'Someone paid to push it up DexScreener trending.'),
    'ad': ('📣', '{sym} is running an ad', 'A paid ad for it is live on DexScreener.'),
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


def fmt_age(ms):
    """Compact age for lists: 45s, 12m, 3h 5m, 2d 4h."""
    minutes = max(int(ms // 60_000), 0)
    if minutes < 1:
        return f'{max(int(ms // 1000), 0)}s'
    if minutes < 60:
        return f'{minutes}m'
    return fmt_duration(ms)


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


def quote(*lines):
    body = section(*lines)
    return f'<blockquote>{body}</blockquote>' if body else None


def kv(label, value):
    return f'{label}: <b>{value}</b>' if value not in (None, '') else None


def symbol(meta, mint=''):
    return f'${escape((meta or {}).get("symbol") or mint[:6] or "???")}'


def token_line(meta):
    name = escape((meta or {}).get('name') or '')
    return f'<b>{symbol(meta)}</b>' + (f' | {name}' if name else '')


def kol_line(text):
    return f'💳 <b>{escape(text)}</b>' if text else None


def wallet_role(kol, wallet):
    if wallet.role == 'main':
        return f'{kol.name} · Main wallet'
    return f'{kol.name} · Side wallet ({wallet.label})'


def wallet_desc(wallet):
    return 'Main wallet' if wallet.role == 'main' else f'Side wallet ({escape(wallet.label)})'


def header(icon, meta, kol_text):
    return section(f'{icon} {token_line(meta)}', kol_line(kol_text))


def titled(icon, title, subtitle=None):
    return section(f'{icon} <b>{title}</b>', f'<i>{subtitle}</i>' if subtitle else None)


def event_title(type_, meta, mint, **values):
    icon, title, explanation = EVENTS[type_]
    safe = {k: escape(str(v)) for k, v in values.items()}
    safe['sym'] = symbol(meta, mint)
    return titled(icon, title.format(**safe), explanation.format(**safe))


def ca(mint):
    return f'<code>{mint}</code>'


def dex_paid_line(paid):
    if paid is None:
        return None
    return '🦅 DEX paid: ✅' if paid else '🦅 DEX paid: ❌'


def dex_paid_value(paid):
    return None if paid is None else ('Yes' if paid else 'No')


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


def kols_quote(entries, highlight=None, limit=8):
    """Who is in the token: holders first, then partial sellers, then those out."""
    if not entries:
        return None

    def rank(entry):
        _, _, sol, bought, held = entry
        ratio = held / bought if bought > 0 else 0
        return (0 if ratio >= 0.98 else 1 if ratio > 0.02 else 2, -sol)

    lines = []
    for kol_id, name, sol, bought, held in sorted(entries, key=rank)[:limit]:
        ratio = held / bought if bought > 0 else 0
        dot, note = ('🟢', '') if ratio >= 0.98 else ('🔴', ' · sold') if ratio <= 0.02 \
            else ('🟡', f' · {ratio:.0%} left')
        label = f'<b>{escape(name)}</b>' if kol_id == highlight else escape(name)
        lines.append(f'{dot} {label} · {fmt_sol(sol)}{note}')
    if len(entries) > limit:
        lines.append(f'<i>+{len(entries) - limit} more</i>')
    out = sum(1 for e in entries if rank(e)[0] == 2)
    return quote(f'<b>KOLs · {len(entries) - out} in, {out} out</b>', *lines)


def kols_in_value(names):
    return escape(', '.join(names)) if names else None


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
        event_title('migrate', meta, mint, venue=venue_name(event.get('pool'))),
        ca(mint),
        quote(kv('MC', fmt_mcap(mcap_sol, sol_usd)),
              kv('Liquidity burned', escape(str(burned)) if burned is not None else None),
              kv('KOLs in', kols_in_value(holders))),
    )


def kol_create(kol, wallet, mint, meta, event, sol_usd, mcap_sol):
    return message(
        event_title('create', meta, mint, kol=kol.name),
        ca(mint),
        quote(kv('Launchpad', venue_name(event.get('pool'))),
              kv('MC', fmt_mcap(mcap_sol, sol_usd)),
              kv('Wallet', wallet_desc(wallet))),
    )


def liquidity(kol, wallet, mint, meta, action, sol, tokens, event):
    return message(
        event_title(action, meta, mint, kol=kol.name),
        ca(mint),
        quote(kv('Amount', f'{fmt_sol(salted(sol, wallet.address, mint))} + '
                           f'{fmt_num(salted(tokens or 0, wallet.address, mint))} tokens'),
              kv('Pool', venue_name(event.get('pool'))),
              kv('Wallet', wallet_desc(wallet))),
    )


def dex_event(type_, mint, meta, item, holders):
    details = []
    if type_ == 'boost':
        details.append(kv('Boosts', f'+{item.get("amount", "?")} ({item.get("totalAmount", "?")} active)'))
    elif type_ == 'ad':
        details.append(kv('Ad', f'{escape(str(item.get("type") or "ad"))} · {item.get("durationHours", "?")}h'))
    return message(event_title(type_, meta, mint), ca(mint),
                   quote(*details, kv('KOLs in', kols_in_value(holders))))


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


def fmt_venues(user):
    if not user.get('venues'):
        return 'All'
    keys = user['venues'].split(',')
    return ', '.join(VENUE_GROUPS[k][0] for k in VENUE_GROUPS if k in keys)


def fmt_alerts(user):
    muted = set((user.get('muted') or '').split(',')) - {''}
    on = [label for key, label in ALERT_TYPES.items() if key not in muted]
    off = [label for key, label in ALERT_TYPES.items() if key in muted]
    if not off:
        return 'All on'
    if not on:
        return 'All off'
    return f'All except {", ".join(off)}' if len(off) <= len(on) else f'Only {", ".join(on)}'


def mult_text(state):
    return f'{state.mcap / state.entry:.2f}x'


def spectral_match(mint, meta, state, kols, sol_usd, paid, now):
    name = escape((meta or {}).get('name') or '')
    return message(
        titled('🎯', f'New match · {symbol(meta, mint)}', name or None),
        ca(mint),
        quote(f'MC: <b>{fmt_mcap(state.mcap, sol_usd)}</b> · {mult_text(state)} since tracked',
              kv('ATH', fmt_mcap(state.ath, sol_usd)),
              kv('Tracked', f'{fmt_age(now - state.entered_ts)} ago'),
              kv('Venue', venue_name(state.venue) if state.venue else None),
              kv('DEX paid', dex_paid_value(paid))),
        kols_quote(kols),
    )


def spectral_buy(kol, wallet, trade, meta, state, kols, sol_usd, paid, first_buy):
    sol = salted(trade['sol'], trade['wallet'], trade['mint'])
    side = 'main wallet' if wallet.role == 'main' else 'side wallet'
    return message(
        titled('🟢' if wallet.role == 'main' else '🔵',
               f'{escape(kol.name)} bought {symbol(meta, trade["mint"])}',
               f'First buy · {side}' if first_buy else f'Added more · {side}'),
        ca(trade['mint']),
        quote(kv('Amount', fmt_sol(sol)),
              f'MC: <b>{fmt_mcap(trade["mcap_sol"], sol_usd)}</b> · {mult_text(state)} since tracked',
              kv('Venue', venue_name(trade['venue'])),
              kv('DEX paid', dex_paid_value(paid))),
        kols_quote(kols, highlight=kol.id),
    )


def archon_link(mint, meta):
    return f'<a href="{ARCHON_URL.format(mint=mint)}">{symbol(meta, mint)}</a>'


def spectral_list(items, total, start, page, pages, sol_usd, now):
    """One page of matches. items: [(meta, state)]; start: index of the first item."""
    if not total:
        return titled('📋', 'My matches',
                      'Nothing fits your filters right now. New matches will arrive here.')
    entries = []
    for i, (meta, state) in enumerate(items, start + 1):
        name = escape((meta or {}).get('name') or '')
        entries.append(quote(
            f'<b>{i}.</b> {archon_link(state.mint, meta)}' + (f' · {name}' if name else ''),
            f'{fmt_mcap(state.mcap, sol_usd)} · {mult_text(state)} · {state.kols} KOLs · '
            f'{fmt_age(now - state.entered_ts)}',
            ca(state.mint)))
    subtitle = f'Page {page + 1} of {pages} · tap a symbol to trade' if pages > 1 else 'Tap a symbol to trade'
    return message(titled('📋', f'My matches · {total}', subtitle), *entries)


def spectral_panel(user, intro=None):
    """The main screen: current filters plus a short header (welcome, saved, ...)."""
    heads = {
        'welcome': titled('👋', 'Welcome to Spectral', 'Tokens KOLs are buying, filtered your way.'),
        'saved': titled('✅', 'Saved'),
        None: titled('⚙️', 'Your filters'),
    }
    min_kols = user['min_kols']
    return message(
        heads[intro],
        quote(kv('⏱ Min time tracked', f'{user["min_minutes"]} min'),
              kv('👥 Min KOLs', f'{min_kols}+'),
              kv('📈 Multiplier', fmt_range(user['min_mult'], user['max_mult'])),
              kv('💰 Market cap', fmt_usd_range(user['min_mcap'], user['max_mcap'])),
              kv('🏦 Venues', escape(fmt_venues(user))),
              kv('🔔 Alerts', escape(fmt_alerts(user)))),
        "<i>Tap a filter to change it. Tokens that already fit aren't sent; see them in My matches.</i>"
        if intro == 'welcome' else '<i>Tap a filter to change it.</i>',
    )


def spectral_venues(user):
    return message(titled('🏦', 'Venues', 'Get tokens only from the venues you pick.'),
                   quote(kv('Selected', escape(fmt_venues(user)))))


def spectral_alerts(user):
    return message(titled('🔔', 'Alerts', 'Updates for tokens you received. New matches always arrive.'),
                   quote(kv('On', escape(fmt_alerts(user)))))


def spectral_prompt(field):
    prompts = {
        'time': (titled('⏱', 'Min time tracked', 'How long a token must have been tracked before you get it.'),
                 section('Send minutes:', '<code>10</code>')),
        'kols': (titled('👥', 'Min KOLs', 'How many different KOLs must have bought the token.'),
                 section('Send a number:', '<code>1</code> any KOL · <code>3</code> three or more')),
        'mult': (titled('📈', 'Multiplier', 'Current market cap ÷ market cap when tracking started.'),
                 section('Send <code>min, max</code>:',
                         '<code>0.7, 2</code> from 0.7x to 2x',
                         '<code>1, any</code> 1x or more',
                         '<code>any, 3</code> up to 3x',
                         '<code>any</code> no limit')),
        'mcap': (titled('💰', 'Market cap', 'Market cap in USD.'),
                 section('Send <code>min, max</code>:',
                         '<code>20k, 100k</code> from $20K to $100K',
                         '<code>20k, any</code> $20K or more',
                         '<code>any, 100k</code> up to $100K',
                         '<code>any</code> no limit')),
    }
    return message(*prompts[field])


def spectral_help():
    return message(
        titled('🧭', 'Spectral', 'Tokens KOLs are buying, filtered your way.'),
        quote('/start · your filters',
              '/show · tokens that fit now',
              '/time 10 · min minutes tracked',
              '/kols 3 · min KOLs',
              '/range 0.7, 2 · multiplier',
              '/mcap 20k, 100k · market cap',
              '/venues · venues',
              '/alerts · updates you get'),
    )
