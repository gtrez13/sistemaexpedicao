"""Apaga ocorrências de "Não achado" de TESTE. Guarda tudo num arquivo antes (dá para voltar).

  venv/bin/python -u naoachado_apagar.py                         lista as ocorrências (não apaga)
  venv/bin/python -u naoachado_apagar.py --de gabrielteste       mostra só as desse usuário (não apaga)
  venv/bin/python -u naoachado_apagar.py --de gabrielteste --apagar
  venv/bin/python -u naoachado_apagar.py --ids 12,13 --apagar
  venv/bin/python -u naoachado_apagar.py --restaurar naoachado_apagados_<data>.json   DESFAZER"""
import json
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass
try:
    from db import db_session
except ImportError:
    from db.database import db_session

a = sys.argv[1:]
arg = lambda k: a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else None
dt = lambda t: time.strftime("%d/%m %H:%M", time.localtime(int(t))) if t else "-"

if "--restaurar" in a:              # fora do bloco principal: sys.exit dentro do "with" desfaz a gravação
    rows = json.load(open(arg("--restaurar"), encoding="utf-8"))
    with db_session() as c:
        for r in rows:
            cols = list(r.keys())
            c.execute(f"INSERT INTO nao_achado ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", tuple(r[k] for k in cols))
    print(f"Voltaram {len(rows)} ocorrência(s).")
    sys.exit(0)

try:                                # mostra qual banco está lendo (tem que ser o mesmo do serviço)
    import db.database as _D
    print("Banco:", getattr(_D, "DB_PATH", None) or ("Postgres" if os.environ.get("DATABASE_URL") else "?"))
except Exception:
    pass

with db_session() as c:
    sql, args = "SELECT * FROM nao_achado", []
    if arg("--de"):
        sql += " WHERE LOWER(reportado_por)=LOWER(?)"; args.append(arg("--de"))
    elif arg("--ids"):
        ids = [int(x) for x in arg("--ids").split(",") if x.strip()]
        sql += f" WHERE id IN ({', '.join('?' for _ in ids)})"; args += ids
    rows = [dict(r) for r in c.execute(sql + " ORDER BY id", tuple(args)).fetchall()]
    print(f"{len(rows)} ocorrência(s):")
    for r in rows:
        print(f"   id {r['id']:<5} {str(r['sku']):16} {str(r['nome'] or '')[:34]:34} por {r['reportado_por']} em {dt(r['reportado_em'])}"
              f"  {r['status']}  estoque Bling: {r.get('estoque_status') or 'não mexido'}")
    if "--apagar" not in a:
        print("\nNada apagado." + (" Para apagar: acrescente --apagar" if (arg("--de") or arg("--ids")) else " Escolha com --de USUARIO ou --ids 1,2"))
        sys.exit(0)
    if not (arg("--de") or arg("--ids")):
        print("Para apagar, diga quais: --de USUARIO ou --ids 1,2"); sys.exit(1)
    if not rows:
        sys.exit(0)
    arq = f"naoachado_apagados_{time.strftime('%Y%m%d%H%M%S')}.json"
    json.dump(rows, open(arq, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    c.execute(f"DELETE FROM nao_achado WHERE id IN ({', '.join('?' for _ in rows)})", tuple(r["id"] for r in rows))
    print(f"\nApagadas {len(rows)}. Cópia em {arq}")
    if any(r.get("estoque_status") == "ok" for r in rows):
        print("ATENÇÃO: alguma delas tinha zerado o estoque no Bling. Apagar não devolve o estoque (use 'Achei' antes, ou lance no Bling).")
    print(f"Desfazer: venv/bin/python -u naoachado_apagar.py --restaurar {arq}")
