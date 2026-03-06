import os
import sqlite3
from contextlib import contextmanager

# db/wms.db
DB_PATH = os.path.join(os.path.dirname(__file__), "wms.db")

# =========================
# Conexão
# =========================
def get_db():
    """
    Conexão padrão para Flask.
    - row_factory = sqlite3.Row (acesso por nome)
    - WAL melhora MUITO leitura+escrita concorrente
    - busy_timeout evita "database is locked"
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # PRAGMAs (performance + estabilidade)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")  # 30s
    return conn


@contextmanager
def db_session():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =========================
# Helpers de schema
# =========================
def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _columns(conn, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] if not isinstance(r, sqlite3.Row) else r["name"] for r in rows}


def _user_version(conn) -> int:
    row = conn.execute("PRAGMA user_version;").fetchone()
    # sqlite pode devolver tuple
    if isinstance(row, sqlite3.Row):
        return int(row[0])
    return int(row[0])


def _set_user_version(conn, v: int):
    conn.execute(f"PRAGMA user_version = {int(v)};")


# =========================
# Schema v1 (base)
# =========================
def _create_schema_v1(conn):
    # PRODUTOS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE NOT NULL COLLATE NOCASE,
            nome TEXT NOT NULL,
            estoque INTEGER DEFAULT 0 CHECK (estoque >= 0),
            localizacao TEXT DEFAULT 'Geral',
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # PEDIDOS
    # (OBS: sem CHECK no status -> evita rebuild chato em migrations; valida no app)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ml_id TEXT UNIQUE NOT NULL,
            cliente_nome TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDENTE',
            logistic_type TEXT,
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
            atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # PEDIDO_PRODUTOS (base)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedido_produtos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pedido_id INTEGER NOT NULL,
            produto_id INTEGER NOT NULL,
            quantidade INTEGER NOT NULL DEFAULT 1 CHECK (quantidade > 0),
            quantidade_bipada INTEGER NOT NULL DEFAULT 0 CHECK (quantidade_bipada >= 0),
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY(pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE,
            FOREIGN KEY(produto_id) REFERENCES produtos(id) ON DELETE RESTRICT
        )
    """)

    # Índices base
    conn.execute("CREATE INDEX IF NOT EXISTS idx_produtos_sku ON produtos(sku);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_status ON pedidos(status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_pedido ON pedido_produtos(pedido_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pp_produto ON pedido_produtos(produto_id);")

    # Trigger para atualizado_em
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_pedidos_atualizado
        AFTER UPDATE ON pedidos
        FOR EACH ROW
        BEGIN
            UPDATE pedidos
            SET atualizado_em = CURRENT_TIMESTAMP
            WHERE id = OLD.id;
        END;
    """)


# =========================
# Migrações versionadas
# =========================
def _migrate_to_v2(conn):
    """
    v2: campos ML e Bling em pedidos + estoque_bling em produtos
    """
    cols_ped = _columns(conn, "pedidos")
    cols_prod = _columns(conn, "produtos")

    # produtos.estoque_bling
    if "estoque_bling" not in cols_prod:
        conn.execute("ALTER TABLE produtos ADD COLUMN estoque_bling INTEGER;")

    # pedidos: campos ML shipping + task + pv
    add_cols = {
        "ml_shipping_id": "TEXT",
        "ml_ship_status": "TEXT",
        "ml_ship_substatus": "TEXT",
        "ml_task": "TEXT",
        "bling_pedido_venda_id": "INTEGER",
    }
    for c, typ in add_cols.items():
        if c not in cols_ped:
            conn.execute(f"ALTER TABLE pedidos ADD COLUMN {c} {typ};")

    # índices úteis
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_ml_id ON pedidos(ml_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_task ON pedidos(ml_task);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_ship ON pedidos(ml_shipping_id);")


def _migrate_to_v3(conn):
    """
    v3: campos necessários pra seu fluxo de:
    - separar (tela 1) -> conferir (tela 2)
    - kits (parent_sku/parent_nome)
    - travar quantidade separada
    """
    cols_pp = _columns(conn, "pedido_produtos")

    if "quantidade_separada" not in cols_pp:
        conn.execute("ALTER TABLE pedido_produtos ADD COLUMN quantidade_separada INTEGER NOT NULL DEFAULT 0;")

    if "parent_sku" not in cols_pp:
        conn.execute("ALTER TABLE pedido_produtos ADD COLUMN parent_sku TEXT;")

    if "parent_nome" not in cols_pp:
        conn.execute("ALTER TABLE pedido_produtos ADD COLUMN parent_nome TEXT;")

    # Importante: seu código faz UPSERT manual por (pedido_id, produto_id).
    # Mas kit pode repetir o mesmo produto em pais diferentes. O ideal:
    # - normal (parent_sku IS NULL): único por (pedido_id, produto_id)
    # - kit (parent_sku IS NOT NULL): único por (pedido_id, produto_id, parent_sku)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pp_normal
        ON pedido_produtos(pedido_id, produto_id)
        WHERE parent_sku IS NULL;
    """)

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pp_kit
        ON pedido_produtos(pedido_id, produto_id, parent_sku)
        WHERE parent_sku IS NOT NULL;
    """)

    # índice de bipagem “faltando”
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pp_faltando
        ON pedido_produtos(pedido_id)
        WHERE quantidade_bipada < quantidade;
    """)


def migrate_db(conn):
    """
    Migração automática por versão.
    - Usa PRAGMA user_version
    - Não precisa ficar lembrando “qual migration rodou”
    """
    # Se não tem tabelas, cria base
    if not _table_exists(conn, "pedidos") or not _table_exists(conn, "produtos") or not _table_exists(conn, "pedido_produtos"):
        _create_schema_v1(conn)
        _set_user_version(conn, 1)

    v = _user_version(conn)

    if v < 1:
        _create_schema_v1(conn)
        v = 1
        _set_user_version(conn, v)

    if v < 2:
        _migrate_to_v2(conn)
        v = 2
        _set_user_version(conn, v)

    if v < 3:
        _migrate_to_v3(conn)
        v = 3
        _set_user_version(conn, v)

    # Se quiser: printa versão final
    return v


def init_db():
    """
    Chamado no boot do app (seu app.py já chama).
    """
    with db_session() as conn:
        # cria base se não existir
        _create_schema_v1(conn)
        final_v = migrate_db(conn)

    print(f"✅ WMS Database inicializado/migrado (schema v{final_v})!", flush=True)


if __name__ == "__main__":
    init_db()