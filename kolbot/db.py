import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kols (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    kol_id TEXT NOT NULL REFERENCES kols(id),
    label TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('main', 'side'))
);
CREATE INDEX IF NOT EXISTS wallets_kol ON wallets(kol_id);

CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    first_seen_ts INTEGER NOT NULL,
    last_mcap_sol REAL,
    last_venue TEXT
);

-- One row per KOL wallet action taken from a PumpAPI event breakdown.
CREATE TABLE IF NOT EXISTS trades (
    signature TEXT NOT NULL,
    wallet TEXT NOT NULL REFERENCES wallets(address),
    mint TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    sol REAL NOT NULL,
    tokens REAL NOT NULL,
    mcap_sol REAL,
    venue TEXT,
    ts INTEGER NOT NULL,
    PRIMARY KEY (signature, wallet, mint, side)
);
CREATE INDEX IF NOT EXISTS trades_mint ON trades(mint, ts);

-- Running position per wallet and token.
CREATE TABLE IF NOT EXISTS positions (
    wallet TEXT NOT NULL REFERENCES wallets(address),
    mint TEXT NOT NULL,
    buy_count INTEGER NOT NULL DEFAULT 0,
    sell_count INTEGER NOT NULL DEFAULT 0,
    bought_tokens REAL NOT NULL DEFAULT 0,
    sold_tokens REAL NOT NULL DEFAULT 0,
    sol_in REAL NOT NULL DEFAULT 0,
    sol_out REAL NOT NULL DEFAULT 0,
    -- SOL-weighted sum of market caps at each buy; avg = mcap_x_sol / sol_in_priced.
    mcap_x_sol REAL NOT NULL DEFAULT 0,
    sol_in_priced REAL NOT NULL DEFAULT 0,
    balance REAL,
    first_buy_ts INTEGER,
    last_trade_ts INTEGER,
    PRIMARY KEY (wallet, mint)
);
CREATE INDEX IF NOT EXISTS positions_mint ON positions(mint);

CREATE TABLE IF NOT EXISTS trending (
    mint TEXT PRIMARY KEY,
    base_mcap_sol REAL NOT NULL,
    ath_mcap_sol REAL NOT NULL,
    entered_ts INTEGER NOT NULL,
    last_mult REAL NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    stopped_ts INTEGER,
    entry_mcap_sol REAL,
    last_mcap_sol REAL
);

CREATE TABLE IF NOT EXISTS trending_alerts (
    mint TEXT NOT NULL REFERENCES trending(mint),
    mult REAL NOT NULL,
    mcap_sol REAL NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (mint, mult)
);

-- Migrations, DexScreener events (dex paid, CTO, boosts, ads) and KOL liquidity.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    ts INTEGER NOT NULL
);

-- Spectral bot users and their filters (NULL bound = open range; market cap in USD).
CREATE TABLE IF NOT EXISTS spectral_users (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    min_minutes INTEGER NOT NULL,
    min_mult REAL,
    max_mult REAL,
    min_mcap REAL,
    max_mcap REAL,
    active INTEGER NOT NULL DEFAULT 1,
    created_ts INTEGER NOT NULL
);

-- Tokens already sent to each Spectral user; their events follow.
CREATE TABLE IF NOT EXISTS spectral_deliveries (
    chat_id INTEGER NOT NULL REFERENCES spectral_users(chat_id),
    mint TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (chat_id, mint)
);
CREATE INDEX IF NOT EXISTS spectral_deliveries_mint ON spectral_deliveries(mint);

CREATE TABLE IF NOT EXISTS liquidity (
    signature TEXT NOT NULL,
    wallet TEXT NOT NULL REFERENCES wallets(address),
    mint TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'remove')),
    sol REAL,
    tokens REAL,
    venue TEXT,
    pool_id TEXT,
    ts INTEGER NOT NULL,
    PRIMARY KEY (signature, wallet, mint, action)
);
"""


def now_ms():
    return int(time.time() * 1000)


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Add columns introduced after a database was created."""
        added = {'trending': ('entry_mcap_sol', 'last_mcap_sol'),
                 'spectral_users': ('min_mcap', 'max_mcap')}
        for table, new_columns in added.items():
            columns = {r['name'] for r in self.conn.execute(f'PRAGMA table_info({table})')}
            for column in new_columns:
                if column not in columns:
                    self.conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} REAL')

    def close(self):
        self.conn.close()

    # --- registry -----------------------------------------------------------
    def sync_registry(self, kols, wallets):
        with self.conn:
            self.conn.execute('BEGIN')
            self.conn.executemany(
                'INSERT INTO kols(id, name) VALUES (?, ?) '
                'ON CONFLICT(id) DO UPDATE SET name = excluded.name',
                [(k.id, k.name) for k in kols.values()])
            self.conn.executemany(
                'INSERT INTO wallets(address, kol_id, label, role) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(address) DO UPDATE SET kol_id = excluded.kol_id, '
                'label = excluded.label, role = excluded.role',
                [(w.address, w.kol_id, w.label, w.role) for w in wallets.values()])

    # --- tokens -------------------------------------------------------------
    def upsert_token(self, mint, ts, symbol=None, name=None, mcap_sol=None, venue=None):
        self.conn.execute(
            'INSERT INTO tokens(mint, symbol, name, first_seen_ts, last_mcap_sol, last_venue) '
            'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(mint) DO UPDATE SET '
            'symbol = COALESCE(excluded.symbol, symbol), name = COALESCE(excluded.name, name), '
            'last_mcap_sol = COALESCE(excluded.last_mcap_sol, last_mcap_sol), '
            'last_venue = COALESCE(excluded.last_venue, last_venue)',
            (mint, symbol, name, ts, mcap_sol, venue))

    def token(self, mint):
        return self.conn.execute('SELECT * FROM tokens WHERE mint = ?', (mint,)).fetchone()

    # --- trades / positions -------------------------------------------------
    def record_trade(self, signature, wallet, mint, side, sol, tokens, mcap_sol, venue, ts):
        """Insert a trade and update the position. Returns False for duplicates."""
        with self.conn:
            self.conn.execute('BEGIN')
            cur = self.conn.execute(
                'INSERT OR IGNORE INTO trades(signature, wallet, mint, side, sol, tokens, '
                'mcap_sol, venue, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (signature, wallet, mint, side, sol, tokens, mcap_sol, venue, ts))
            if cur.rowcount == 0:
                return False
            self.conn.execute(
                'INSERT OR IGNORE INTO positions(wallet, mint) VALUES (?, ?)', (wallet, mint))
            if side == 'buy':
                priced = sol if mcap_sol is not None else 0.0
                self.conn.execute(
                    'UPDATE positions SET buy_count = buy_count + 1, '
                    'bought_tokens = bought_tokens + ?, sol_in = sol_in + ?, '
                    'mcap_x_sol = mcap_x_sol + ?, sol_in_priced = sol_in_priced + ?, '
                    'first_buy_ts = COALESCE(first_buy_ts, ?), last_trade_ts = ? '
                    'WHERE wallet = ? AND mint = ?',
                    (tokens, sol, priced * (mcap_sol or 0.0), priced, ts, ts, wallet, mint))
            else:
                self.conn.execute(
                    'UPDATE positions SET sell_count = sell_count + 1, '
                    'sold_tokens = sold_tokens + ?, sol_out = sol_out + ?, last_trade_ts = ? '
                    'WHERE wallet = ? AND mint = ?',
                    (tokens, sol, ts, wallet, mint))
            return True

    def set_balance(self, wallet, mint, balance):
        self.conn.execute(
            'UPDATE positions SET balance = ? WHERE wallet = ? AND mint = ?',
            (balance, wallet, mint))

    def position(self, wallet, mint):
        return self.conn.execute(
            'SELECT * FROM positions WHERE wallet = ? AND mint = ?', (wallet, mint)).fetchone()

    def kol_positions(self, kol_id, mint):
        return self.conn.execute(
            'SELECT p.*, w.label, w.role FROM positions p JOIN wallets w ON w.address = p.wallet '
            'WHERE w.kol_id = ? AND p.mint = ? AND p.buy_count > 0 ORDER BY w.role, w.label',
            (kol_id, mint)).fetchall()

    def token_positions(self, mint):
        return self.conn.execute(
            'SELECT p.*, w.label, w.role, w.kol_id FROM positions p '
            'JOIN wallets w ON w.address = p.wallet WHERE p.mint = ? AND p.buy_count > 0',
            (mint,)).fetchall()

    def held_mints(self, wallet):
        return [r[0] for r in self.conn.execute(
            'SELECT mint FROM positions WHERE wallet = ?', (wallet,))]

    def recent_kol_mints(self, since_ts):
        return {r[0] for r in self.conn.execute(
            'SELECT DISTINCT mint FROM positions WHERE first_buy_ts >= ?', (since_ts,))}

    def kol_mints(self):
        return {r[0] for r in self.conn.execute('SELECT DISTINCT mint FROM positions')}

    # --- trending -----------------------------------------------------------
    def start_trending(self, mint, base_mcap_sol, mcap_sol, ts):
        cur = self.conn.execute(
            'INSERT OR IGNORE INTO trending(mint, base_mcap_sol, ath_mcap_sol, entered_ts, '
            'entry_mcap_sol, last_mcap_sol) VALUES (?, ?, ?, ?, ?, ?)',
            (mint, base_mcap_sol, max(base_mcap_sol, mcap_sol), ts, mcap_sol, mcap_sol))
        return cur.rowcount == 1

    def update_trending(self, mint, ath_mcap_sol, last_mult, last_mcap_sol=None):
        self.conn.execute(
            'UPDATE trending SET ath_mcap_sol = ?, last_mult = ?, '
            'last_mcap_sol = COALESCE(?, last_mcap_sol) WHERE mint = ?',
            (ath_mcap_sol, last_mult, last_mcap_sol, mint))

    def stop_trending(self, mint, ath_mcap_sol, ts):
        self.conn.execute(
            'UPDATE trending SET active = 0, stopped_ts = ?, ath_mcap_sol = ? WHERE mint = ?',
            (ts, ath_mcap_sol, mint))

    def trending_rows(self, active_only=True):
        sql = 'SELECT * FROM trending' + (' WHERE active = 1' if active_only else '')
        return self.conn.execute(sql).fetchall()

    def is_trending_known(self, mint):
        return self.conn.execute(
            'SELECT 1 FROM trending WHERE mint = ?', (mint,)).fetchone() is not None

    def record_trending_alert(self, mint, mult, mcap_sol, ts):
        self.conn.execute(
            'INSERT OR IGNORE INTO trending_alerts(mint, mult, mcap_sol, ts) VALUES (?, ?, ?, ?)',
            (mint, mult, mcap_sol, ts))

    # --- events -------------------------------------------------------------
    def record_event(self, mint, type_, dedupe_key, payload, ts):
        """Returns True only the first time a dedupe_key is seen."""
        cur = self.conn.execute(
            'INSERT OR IGNORE INTO events(mint, type, dedupe_key, payload, ts) '
            'VALUES (?, ?, ?, ?, ?)', (mint, type_, dedupe_key, json.dumps(payload), ts))
        return cur.rowcount == 1

    def record_liquidity(self, signature, wallet, mint, action, sol, tokens, venue, pool_id, ts):
        cur = self.conn.execute(
            'INSERT OR IGNORE INTO liquidity(signature, wallet, mint, action, sol, tokens, venue, '
            'pool_id, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (signature, wallet, mint, action, sol, tokens, venue, pool_id, ts))
        return cur.rowcount == 1

    # --- spectral -----------------------------------------------------------
    def spectral_register(self, chat_id, username, first_name, min_minutes, ts):
        """Creates the user with default filters, or reactivates it. Returns True if new."""
        cur = self.conn.execute(
            'INSERT OR IGNORE INTO spectral_users(chat_id, username, first_name, min_minutes, '
            'created_ts) VALUES (?, ?, ?, ?, ?)', (chat_id, username, first_name, min_minutes, ts))
        if cur.rowcount == 0:
            self.conn.execute(
                'UPDATE spectral_users SET username = ?, first_name = ?, active = 1 WHERE chat_id = ?',
                (username, first_name, chat_id))
        return cur.rowcount == 1

    def spectral_users(self, active_only=True):
        sql = 'SELECT * FROM spectral_users' + (' WHERE active = 1' if active_only else '')
        return self.conn.execute(sql).fetchall()

    def spectral_user(self, chat_id):
        return self.conn.execute(
            'SELECT * FROM spectral_users WHERE chat_id = ?', (chat_id,)).fetchone()

    def spectral_update_filters(self, chat_id, min_minutes, min_mult, max_mult, min_mcap, max_mcap):
        self.conn.execute(
            'UPDATE spectral_users SET min_minutes = ?, min_mult = ?, max_mult = ?, '
            'min_mcap = ?, max_mcap = ? WHERE chat_id = ?',
            (min_minutes, min_mult, max_mult, min_mcap, max_mcap, chat_id))

    def spectral_deactivate(self, chat_id):
        self.conn.execute('UPDATE spectral_users SET active = 0 WHERE chat_id = ?', (chat_id,))

    def spectral_record_delivery(self, chat_id, mint, ts):
        cur = self.conn.execute(
            'INSERT OR IGNORE INTO spectral_deliveries(chat_id, mint, ts) VALUES (?, ?, ?)',
            (chat_id, mint, ts))
        return cur.rowcount == 1

    def spectral_deliveries(self):
        return self.conn.execute('SELECT chat_id, mint FROM spectral_deliveries').fetchall()
