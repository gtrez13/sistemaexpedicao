"""Zera os bipes de uma remessa Full (depois de testes) para recomeçar a separar do zero.

  venv/bin/python -u full_zerar.py                    lista as remessas Full
  venv/bin/python -u full_zerar.py 3                  prévia: o que a remessa 3 tem bipado (não muda nada)
  venv/bin/python -u full_zerar.py 3 --aplicar        zera: separado, etiquetado, unidades dos kits, validade digitada,
                                                      bipes e fila de etiquetas, lote consumido no FEFO (FULL-3); volta para "aberta"
  venv/bin/python -u full_zerar.py --restaurar ARQ    desfaz (volta tudo como estava, pelo backup JSON)

A remessa e os itens continuam (mesmo frete, mesmas quantidades). Etiquetas que já saíram no papel: jogue fora."""
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
dt = lambda t: time.strftime("%d/%m %H:%M", time.localtime(int(t))) if t else "-"


def linhas(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def inserir(c, tabela, row):
    cols = list(row)
    c.execute(f"INSERT INTO {tabela} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", tuple(row[k] for k in cols))


if "--restaurar" in a:
    arq = a[a.index("--restaurar") + 1]
    b = json.load(open(arq))
    rid = b["remessa"]["id"]
    with db_session() as c:
        c.execute("DELETE FROM full_bips WHERE remessa_id=?", (rid,))
        for r in b["bips"]:
            inserir(c, "full_bips", r)
        c.execute("DELETE FROM fefo_bip WHERE ml_id=?", (f"FULL-{rid}",))
        for r in b["fefo"]:
            inserir(c, "fefo_bip", r)
        for it in b["itens"]:
            cols = [k for k in ("separado", "etiquetado", "comp", "validade_etq") if k in it]
            c.execute(f"UPDATE full_itens SET {', '.join(k + '=?' for k in cols)} WHERE id=?", tuple(it[k] for k in cols) + (it["id"],))
        c.execute("UPDATE full_remessas SET status=?, atualizado_em=? WHERE id=?", (b["remessa"]["status"], int(time.time()), rid))
    print(f"Remessa {rid} voltou como estava antes de zerar ({len(b['bips'])} bipes).")
    sys.exit(0)

if not a or not a[0].isdigit():
    with db_session() as c:
        rs = linhas(c, "SELECT id, nome, frete, status, criado_em FROM full_remessas ORDER BY id DESC LIMIT 30")
        print("Remessas Full:")
        for r in rs:
            n = c.execute("SELECT COUNT(*) AS n FROM full_bips WHERE remessa_id=? AND desfeito=0", (r["id"],)).fetchone()["n"]
            print(f"   {r['id']:<4} {str(r['nome'])[:34]:34} {r['status']:10} criada {dt(r['criado_em'])}   {n} bipes")
    print("\nPrévia de uma: venv/bin/python -u full_zerar.py ID")
    sys.exit(0)

rid = int(a[0])
with db_session() as c:
    rem = c.execute("SELECT * FROM full_remessas WHERE id=?", (rid,)).fetchone()
    if not rem:
        print(f"Remessa {rid} não existe.")
    else:
        rem = dict(rem)
        itens = linhas(c, "SELECT * FROM full_itens WHERE remessa_id=? ORDER BY id", (rid,))
        bips = linhas(c, "SELECT * FROM full_bips WHERE remessa_id=? ORDER BY id", (rid,))
        try:
            fefo = linhas(c, "SELECT * FROM fefo_bip WHERE ml_id=? ORDER BY id", (f"FULL-{rid}",))
        except Exception:
            fefo = []
if not rem:
    sys.exit(1)

sep = sum(int(i["separado"] or 0) for i in itens)
etq = sum(int(i.get("etiquetado") or 0) for i in itens)
print(f"Remessa {rid}: {rem['nome']} ({rem['status']})")
print(f"   {len(itens)} produtos · {sep} separadas · {etq} etiquetadas · {len(bips)} bipes · {len(fefo)} lançamentos de lote no FEFO")
if "--aplicar" not in a:
    print(f"\nNada mudou. Para zerar: venv/bin/python -u full_zerar.py {rid} --aplicar")
    sys.exit(0)

arq = f"full_zerar_bak_{rid}_{time.strftime('%Y%m%d%H%M%S')}.json"
with open(arq, "w") as f:
    json.dump({"remessa": rem, "itens": itens, "bips": bips, "fefo": fefo}, f, ensure_ascii=False, default=str)
os.chmod(arq, 0o600)
with db_session() as c:
    for it in itens:
        comp = it.get("comp")
        if comp:
            try:
                comp = json.dumps([dict(x, bip=0) for x in json.loads(comp)], ensure_ascii=False)
            except Exception:
                pass
        sets = ["separado=0"] + [x for k, x in (("etiquetado", "etiquetado=0"), ("validade_etq", "validade_etq=NULL")) if k in it]
        args = ()
        if "comp" in it:
            sets.append("comp=?"); args = (comp,)
        c.execute(f"UPDATE full_itens SET {', '.join(sets)} WHERE id=?", args + (it["id"],))     # só as colunas que existem
    c.execute("DELETE FROM full_bips WHERE remessa_id=?", (rid,))
    if fefo:
        c.execute("DELETE FROM fefo_bip WHERE ml_id=?", (f"FULL-{rid}",))
    c.execute("UPDATE full_remessas SET status='aberta', atualizado_em=?, fechado_por=NULL, fechado_em=NULL WHERE id=? AND status IN ('aberta','separando','separada')",
              (int(time.time()), rid))
print(f"\nZerada: remessa {rid} volta a 0 separadas e 0 etiquetadas, status aberta. Backup: {arq}")
print(f"Desfazer: venv/bin/python -u full_zerar.py --restaurar {arq}")
