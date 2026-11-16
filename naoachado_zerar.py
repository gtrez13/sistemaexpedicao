"""Zera no Bling os "Não achados" ABERTOS que ainda não foram zerados (os relatados antes do zerar automático, ou que deram erro).

  venv/bin/python -u naoachado_zerar.py                prévia: lista e mostra o saldo de agora no Bling (NÃO muda nada)
  venv/bin/python -u naoachado_zerar.py --aplicar      zera: saldo maior que 0 vira 0 no depósito da LHVMed (guarda o de antes)
  venv/bin/python -u naoachado_zerar.py --ids 7,9 [--aplicar]   só esses

Desfazer: em Não achados, botão "Achei: devolver N no Bling" (lança a entrada do saldo de antes).
Saldo zero ou negativo não mexe (zerar um negativo seria dar ENTRADA no estoque)."""
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
import naoachado_routes as N

a = sys.argv[1:]
arg = lambda k: a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else None
dt = lambda t: time.strftime("%d/%m %H:%M", time.localtime(int(t))) if t else "-"
aplicar = "--aplicar" in a

with db_session() as c:
    sql = ("SELECT * FROM nao_achado WHERE status='aberto' "
           "AND COALESCE(estoque_status,'') NOT IN ('ok','ja_zerado','restaurado','zerando')")
    args = []
    if arg("--ids"):
        ids = [int(x) for x in arg("--ids").split(",") if x.strip()]
        sql += f" AND id IN ({', '.join('?' for _ in ids)})"; args += ids
    rows = [dict(r) for r in c.execute(sql + " ORDER BY id", tuple(args)).fetchall()]

if not rows:
    print("Nenhum Não achado aberto esperando zerar no Bling.")
    sys.exit(0)

from services.bling_service import BlingService
bs = BlingService()
dep = N._deposito()
print(f"{len(rows)} aberto(s) ainda sem zerar (depósito {dep}):")
for r in rows:
    try:
        saldo = N._saldo_no_deposito(bs, N._produto_id(bs, r["sku"]), dep)
        s = f"saldo agora {saldo:g}" + ("" if saldo > 0 else "  (não mexe)")
    except Exception as e:
        s = f"não li o saldo: {e}"
    print(f"   id {r['id']:<5} {str(r['sku']):16} {str(r['nome'] or '')[:34]:34} por {r['reportado_por']} em {dt(r['reportado_em'])}  {s}")
    time.sleep(0.4)

if not aplicar:
    print("\nNada mudou. Para zerar: acrescente --aplicar")
    sys.exit(0)

os.environ["NAO_ACHADO_ZERAR_BLING"] = "1"           # o serviço liga isso pelo systemd; aqui é o --aplicar que autoriza
print("\nZerando…")
for r in rows:
    N._zerar_agora(r["id"])
    with db_session() as c:
        x = dict(c.execute("SELECT estoque_status, estoque_msg FROM nao_achado WHERE id=?", (r["id"],)).fetchone())
    print(f"   id {r['id']:<5} {str(r['sku']):16} {x['estoque_status']}: {x['estoque_msg'] or ''}")
    time.sleep(0.4)
print("\nDesfazer um: Não achados > \"Achei: devolver N no Bling\".")
