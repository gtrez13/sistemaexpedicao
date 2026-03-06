from db.database import get_db

db = get_db()

print("\n=== PRAGMA database_list ===")
for r in db.execute("PRAGMA database_list;").fetchall():
    # r = (seq, name, file)
    print(tuple(r))

print("\n=== PRAGMA table_info(pedido_produtos) ===")
for c in db.execute("PRAGMA table_info(pedido_produtos);").fetchall():
    # c = (cid, name, type, notnull, dflt_value, pk)
    print(tuple(c))

db.close()
