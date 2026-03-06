import os
import sqlite3

# Ajuste se seu DB estiver em outro caminho
DB_PATH = os.path.join(os.path.dirname(__file__), "db", "wms.db")

def main():
    print("📌 Banco:", DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # (Opcional) ver se tem duplicatas que impedem criar o UNIQUE
    dup = cur.execute("""
        SELECT pedido_id, produto_id, COUNT(*) as c
        FROM pedido_produtos
        GROUP BY pedido_id, produto_id
        HAVING c > 1
        LIMIT 10
    """).fetchall()

    if dup:
        print("⚠️ Existem duplicatas (pedido_id, produto_id). Exemplo:")
        for d in dup:
            print("   ", d)
        print("\n❌ Não dá pra criar UNIQUE ainda. Me avisa que eu te mando o script de deduplicar seguro.")
        conn.close()
        return

    # Cria o índice UNIQUE (o que destrava o ON CONFLICT)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_pedido_produtos
        ON pedido_produtos(pedido_id, produto_id);
    """)

    conn.commit()
    conn.close()
    print("✅ Índice UNIQUE criado com sucesso!")

if __name__ == "__main__":
    main()