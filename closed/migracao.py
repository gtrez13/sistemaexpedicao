import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "wms.db")


def column_exists(cur, table: str, column: str) -> bool:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    cols = {r[1] for r in rows}  # r[1] = name
    return column in cols


def main():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Banco não encontrado em: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        # 1) adiciona coluna se não existir
        if not column_exists(cur, "pedido_produtos", "quantidade_separada"):
            print("🛠️ Adicionando coluna pedido_produtos.quantidade_separada...")
            cur.execute("ALTER TABLE pedido_produtos ADD COLUMN quantidade_separada INTEGER DEFAULT 0")
            conn.commit()
            print("✅ Coluna adicionada.")
        else:
            print("ℹ️ Coluna quantidade_separada já existe. Nada a fazer.")

        # 2) (opcional) backfill: copia o que já estava bipado pra separada
        #    Se você NÃO quiser isso, comenta esse bloco.
        print("🧩 Backfill: copiando quantidade_bipada -> quantidade_separada (somente onde separada=0)...")
        cur.execute("""
            UPDATE pedido_produtos
               SET quantidade_separada = quantidade_bipada
             WHERE COALESCE(quantidade_separada, 0) = 0
               AND COALESCE(quantidade_bipada, 0) > 0
        """)
        conn.commit()
        print("✅ Backfill concluído.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()