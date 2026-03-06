import sqlite3

DB = "db/wms.db"

def col_exists(cur, table, col):
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    return col in cols

def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    if not col_exists(cur, "pedidos", "bling_pedido_venda_id"):
        print("🛠️ Criando coluna pedidos.bling_pedido_venda_id...")
        cur.execute("ALTER TABLE pedidos ADD COLUMN bling_pedido_venda_id INTEGER")

    conn.commit()
    conn.close()
    print("✅ OK! pedidos.bling_pedido_venda_id existe.")

if __name__ == "__main__":
    main()