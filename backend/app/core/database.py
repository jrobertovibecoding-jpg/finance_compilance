import os
import sqlite3
import base64
import logging
from contextlib import contextmanager
import requests
from . import config

logger = logging.getLogger("jrfincloud.database")

_TURSO_SESSION = requests.Session()


def _to_turso_arg(v):
    if v is None:
        return {"type": "null"}
    elif isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    elif isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    elif isinstance(v, float):
        return {"type": "float", "value": v}
    elif isinstance(v, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(v).decode("ascii")}
    else:
        return {"type": "text", "value": str(v)}


def _from_turso_val(val_dict):
    if not isinstance(val_dict, dict):
        return val_dict
    t = val_dict.get("type")
    if t == "null":
        return None
    elif t == "integer":
        return int(val_dict.get("value", 0))
    elif t == "float":
        return float(val_dict.get("value", 0.0))
    elif t == "text":
        return val_dict.get("value", "")
    elif t == "blob":
        return base64.b64decode(val_dict.get("base64", ""))
    return val_dict.get("value")


def normalize_turso_url(url: str) -> str:
    url = url.strip()
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    if not url.endswith("/v2/pipeline"):
        url = url.rstrip("/") + "/v2/pipeline"
    return url


class TursoRow(dict):
    """Permite acesso tanto por nome de coluna (dict / row['col']) quanto por índice (row[0])."""

    def __init__(self, cols, vals):
        super().__init__(zip(cols, vals))
        self._vals = list(vals)
        self._keys = list(cols)

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._vals[item]
        return super().__getitem__(item)

    def keys(self):
        return self._keys

    def values(self):
        return self._vals


class TursoCursor:
    def __init__(self, conn_wrapper):
        self.conn = conn_wrapper
        self.session = conn_wrapper.session
        self.endpoint = conn_wrapper.endpoint
        self.headers = conn_wrapper.headers
        self.description = None
        self.lastrowid = None
        self.rowcount = 0
        self._rows = []
        self._idx = 0

    def execute(self, sql, params=()):
        args = [_to_turso_arg(p) for p in params] if params else []
        reqs = [{"type": "execute", "stmt": {"sql": sql, "args": args}}]
        if not self.conn.in_transaction:
            reqs.append({"type": "close"})

        payload = {"requests": reqs}
        if self.conn.in_transaction and self.conn._baton:
            payload["baton"] = self.conn._baton

        res = self.session.post(self.endpoint, headers=self.headers, json=payload, timeout=30)
        if res.status_code != 200:
            raise sqlite3.OperationalError(f"Erro Turso {res.status_code}: {res.text}")
        data = res.json()
        if self.conn.in_transaction and data.get("baton"):
            self.conn._baton = data["baton"]

        results = data.get("results", [])
        if not results:
            return self
        first = results[0]
        if first.get("type") == "error":
            err_msg = first.get("error", {}).get("message", "Erro SQL desconhecido")
            raise sqlite3.OperationalError(err_msg)

        exec_res = first.get("response", {}).get("result", {})
        cols = [c["name"] for c in exec_res.get("cols", [])]
        self.description = [(c, None, None, None, None, None, None) for c in cols]

        last_id = exec_res.get("last_insert_rowid")
        self.lastrowid = int(last_id) if last_id is not None else None
        self.rowcount = exec_res.get("affected_row_count", 0)

        raw_rows = exec_res.get("rows", [])
        parsed_rows = []
        for r in raw_rows:
            vals = [_from_turso_val(v) for v in r]
            parsed_rows.append(TursoRow(cols, vals))

        self._rows = parsed_rows
        self._idx = 0
        return self

    def executemany(self, sql, seq_of_params):
        stmts = []
        for params in seq_of_params:
            stmts.append({
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": [_to_turso_arg(p) for p in params] if params else [],
                },
            })
        if not stmts:
            return self
        if not self.conn.in_transaction:
            stmts.append({"type": "close"})
        payload = {"requests": stmts}
        if self.conn.in_transaction and self.conn._baton:
            payload["baton"] = self.conn._baton
        res = self.session.post(self.endpoint, headers=self.headers, json=payload, timeout=60)
        if res.status_code != 200:
            raise sqlite3.OperationalError(f"Erro Turso executemany {res.status_code}: {res.text}")
        data = res.json()
        if self.conn.in_transaction and data.get("baton"):
            self.conn._baton = data["baton"]
        for item in data.get("results", []):
            if item.get("type") == "error":
                raise sqlite3.OperationalError(item.get("error", {}).get("message", "Erro SQL em lote"))
        return self

    def fetchone(self):
        if self._idx < len(self._rows):
            r = self._rows[self._idx]
            self._idx += 1
            return r
        return None

    def fetchall(self):
        r = self._rows[self._idx:]
        self._idx = len(self._rows)
        return r

    def fetchmany(self, size=None):
        if size is None:
            size = 1
        end = self._idx + size
        r = self._rows[self._idx:end]
        self._idx = min(end, len(self._rows))
        return r


class TursoConnectionWrapper:
    def __init__(self, endpoint: str, token: str):
        self.endpoint = endpoint
        self.token = token
        self.session = _TURSO_SESSION
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.in_transaction = False
        self._baton = None

    def cursor(self):
        return TursoCursor(self)

    def _send_tx_cmd(self, sql: str, close_stream: bool = False):
        reqs = [{"type": "execute", "stmt": {"sql": sql, "args": []}}]
        if close_stream:
            reqs.append({"type": "close"})
        payload = {"requests": reqs}
        if self._baton:
            payload["baton"] = self._baton
        res = self.session.post(self.endpoint, headers=self.headers, json=payload, timeout=30)
        if res.status_code != 200:
            raise sqlite3.OperationalError(f"Erro Turso transação {res.status_code}: {res.text}")
        data = res.json()
        if not close_stream and data.get("baton"):
            self._baton = data["baton"]
        elif close_stream:
            self._baton = None
        for item in data.get("results", []):
            if item.get("type") == "error":
                raise sqlite3.OperationalError(item.get("error", {}).get("message", "Erro transacional Turso"))

    def begin(self):
        if not self.in_transaction:
            self.in_transaction = True
            self._baton = None
            self._send_tx_cmd("BEGIN IMMEDIATE", close_stream=False)

    def commit(self):
        if self.in_transaction:
            try:
                self._send_tx_cmd("COMMIT", close_stream=True)
            finally:
                self.in_transaction = False
                self._baton = None

    def rollback(self):
        if self.in_transaction:
            try:
                self._send_tx_cmd("ROLLBACK", close_stream=True)
            except Exception as exc:
                logger.warning("Erro durante ROLLBACK Turso: %s", exc)
            finally:
                self.in_transaction = False
                self._baton = None

    def close(self):
        if self.in_transaction:
            self.rollback()


@contextmanager
def transaction(conn):
    """Context manager transacional unificado para SQLite local e Turso LibSQL."""
    if isinstance(conn, TursoConnectionWrapper):
        conn.begin()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _resolve_turso_credentials():
    if "TURSO_DATABASE_URL" in os.environ:
        url = os.environ["TURSO_DATABASE_URL"].strip()
    else:
        url = config.TURSO_DATABASE_URL

    if "TURSO_AUTH_TOKEN" in os.environ:
        token = os.environ["TURSO_AUTH_TOKEN"].strip()
    else:
        token = config.TURSO_AUTH_TOKEN
    return url, token


def _resolve_sqlite_path() -> str:
    env_path = os.getenv("DATABASE_PATH", "").strip()
    if env_path:
        return env_path
    return config.DATABASE_PATH


def get_connection():
    turso_url, turso_token = _resolve_turso_credentials()
    if turso_url and turso_token:
        endpoint = normalize_turso_url(turso_url)
        return TursoConnectionWrapper(endpoint, turso_token)

    db_file = _resolve_sqlite_path()
    os.makedirs(os.path.dirname(os.path.abspath(db_file)), exist_ok=True)
    conn = sqlite3.connect(db_file, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute('''
    CREATE TABLE IF NOT EXISTS notas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero_tx TEXT,
        tipo TEXT,
        fornecedor TEXT,
        cnpj TEXT,
        dt_emissao TEXT,
        dt_vencimento TEXT,
        dt_pagamento TEXT,
        valor_bruto REAL,
        valor_liquido REAL,
        status TEXT DEFAULT 'PENDENTE',
        categoria TEXT,
        observacao TEXT,
        responsavel TEXT,
        descricao TEXT,
        filial TEXT,
        forma_pgto TEXT,
        cod_barras TEXT,
        pix_chave TEXT,
        numero_nf TEXT,
        is_previsao INTEGER DEFAULT 0,
        conciliada INTEGER DEFAULT 0
    )
    ''')

    for col, table in [
        ("cod_operacao", "notas"), ("cnae", "notas"), ("item_lc116", "notas"),
        ("valor_pago", "notas"), ("juros_multa", "notas"),
        ("desconto", "notas"), ("grupo_conciliacao", "notas"),
    ]:
        try:
            ddl = (
                f"ALTER TABLE {table} ADD COLUMN {col} TEXT"
                if col in ("cod_operacao", "cnae", "item_lc116", "grupo_conciliacao")
                else f"ALTER TABLE {table} ADD COLUMN {col} REAL DEFAULT 0"
            )
            cur.execute(ddl)
            conn.commit()
        except Exception:
            pass

    cur.execute('''
    CREATE TABLE IF NOT EXISTS nota_impostos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nota_id INTEGER,
        tipo TEXT,
        aliquota REAL,
        valor REAL,
        dt_venc_imp TEXT,
        status TEXT DEFAULT 'PENDENTE',
        numero_doc TEXT,
        FOREIGN KEY (nota_id) REFERENCES notas(id) ON DELETE CASCADE
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS conciliacao_grupos (
        id TEXT PRIMARY KEY,
        extrato_fitid TEXT,
        extrato_valor REAL,
        extrato_data TEXT,
        extrato_memo TEXT,
        soma_titulos REAL,
        diferenca REAL DEFAULT 0,
        tipo_diferenca TEXT,
        qtd_titulos INTEGER DEFAULT 1,
        dt_conciliacao TEXT DEFAULT (datetime('now')),
        operador TEXT DEFAULT 'Sistema'
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS ofx_conciliados (
        fitid TEXT PRIMARY KEY,
        nota_id INTEGER,
        grupo_id TEXT,
        valor REAL,
        data_extrato TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS adiantamentos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fornecedor TEXT,
        data TEXT,
        valor REAL,
        status TEXT DEFAULT 'ABERTO',
        observacao TEXT,
        ccusto TEXT,
        forma TEXT,
        motivo TEXT,
        data_fechamento TEXT
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS adiantamento_comprovantes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        adiantamento_id INTEGER,
        data TEXT,
        tipo_doc TEXT,
        descricao TEXT,
        valor REAL,
        data_criacao TEXT,
        FOREIGN KEY (adiantamento_id) REFERENCES adiantamentos(id) ON DELETE CASCADE
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT DEFAULT '',
        acao TEXT DEFAULT '',
        entidade TEXT DEFAULT '',
        entidade_id TEXT DEFAULT '',
        descricao TEXT DEFAULT '',
        valor REAL DEFAULT 0,
        ip TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nome TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        senha_hash TEXT NOT NULL,
        perfil TEXT NOT NULL DEFAULT 'MASTER',
        ativo INTEGER DEFAULT 1,
        ultimo_login TEXT,
        criado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    cur.execute('''
    CREATE TABLE IF NOT EXISTS fornecedores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        razao_social TEXT NOT NULL,
        nome_fantasia TEXT,
        cnpj_cpf TEXT UNIQUE,
        tipo TEXT DEFAULT 'FORNECEDOR',
        categoria TEXT,
        ccusto TEXT,
        responsavel TEXT,
        forma_pgto TEXT,
        pix_chave TEXT,
        dados_banco TEXT,
        cod_operacao TEXT,
        cnae TEXT,
        item_lc116 TEXT,
        ativo INTEGER DEFAULT 1,
        criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
        atualizado_em TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_notas_vencimento ON notas(dt_vencimento)",
        "CREATE INDEX IF NOT EXISTS idx_notas_status ON notas(status)",
        "CREATE INDEX IF NOT EXISTS idx_notas_fornecedor ON notas(fornecedor)",
        "CREATE INDEX IF NOT EXISTS idx_notas_cnpj ON notas(cnpj)",
        "CREATE INDEX IF NOT EXISTS idx_notas_is_previsao ON notas(is_previsao)",
        "CREATE INDEX IF NOT EXISTS idx_notas_grupo_conc ON notas(grupo_conciliacao)",
        "CREATE INDEX IF NOT EXISTS idx_fornecedores_cnpj ON fornecedores(cnpj_cpf)",
        "CREATE INDEX IF NOT EXISTS idx_fornecedores_razao ON fornecedores(razao_social)",
        "CREATE INDEX IF NOT EXISTS idx_impostos_nota ON nota_impostos(nota_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_conc_grupos_fitid ON conciliacao_grupos(extrato_fitid)",
        "CREATE INDEX IF NOT EXISTS idx_ofx_conc_nota ON ofx_conciliados(nota_id)",
    ]:
        try:
            cur.execute(idx_sql)
        except Exception:
            pass

    conn.commit()
    conn.close()
