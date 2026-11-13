"""Separação só com o que sai HOJE (Mercado Livre) e só o Comercial "Em andamento" no Bling.

  --previa    mostra o que tiraria e o que devolveria (NÃO muda nada)
  --aplicar   faz agora (o mesmo que a tela faz ao abrir a Separação / Atualizar pedidos)
  --lista     mostra o que está fora da Separação agora e por quê
  --devolver  DESFAZER: põe todos que o sistema tirou de volta no status de antes

Uso: venv/bin/python -u separacao_so_hoje.py --previa"""
import sys
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

OCULTOS = ("OUTRO_DIA", "FORA_BLING")


def devolver():
    with db_session() as c:
        c.execute("CREATE TABLE IF NOT EXISTS sep_fora (ml_id TEXT PRIMARY KEY, tipo TEXT NOT NULL, status_antes TEXT NOT NULL, "
                  "motivo TEXT, dia TEXT, em BIGINT)")
        fora = [dict(r) for r in c.execute("SELECT * FROM sep_fora").fetchall()]
        n = 0
        for f in fora:
            c.execute("UPDATE pedidos SET status=? WHERE ml_id=? AND status IN (?, ?)", (f["status_antes"] or "PENDENTE", f["ml_id"], *OCULTOS))
            n += 1
        sobra = c.execute("SELECT COUNT(*) AS n FROM pedidos WHERE status IN (?, ?)", OCULTOS).fetchone()["n"]
        if sobra:
            c.execute("UPDATE pedidos SET status='PENDENTE' WHERE status IN (?, ?)", OCULTOS)
        c.execute("DELETE FROM sep_fora")
    print(f"Devolvidos: {n} (+{sobra} sem registro, voltaram para PENDENTE). A Separação mostra tudo como antes.")


def mostrar(res):
    print(f"Hoje: {res['hoje']}  ({'APLICADO' if res['aplicado'] else 'PRÉVIA, nada mudou'})\n")
    for k, nome in (("ml", "Mercado Livre"), ("comercial", "Comercial")):
        r = res[k]
        extra = f", Em andamento no Bling: {r['em_andamento_bling']}" if "em_andamento_bling" in r else ""
        print(f"{nome}: conferidos {r['conferidos']} pendentes sem bip{extra}" + (f"  ERRO: {r['erro']}" if r["erro"] else ""))
        for x in r["saiu"]:
            print(f"   sai da Separação  {x['ml_id']}  {str(x.get('cliente') or '')[:32]:32}  {x['motivo']}")
        for x in r["voltou"]:
            print(f"   volta             {x['ml_id']}  {str(x.get('cliente') or '')[:32]:32}")
        if not r["saiu"] and not r["voltou"]:
            print("   nada a mudar")
    print()


if __name__ == "__main__":
    op = (sys.argv[1:] or ["--previa"])[0]
    if op == "--devolver":
        devolver()
        sys.exit(0)
    import mapa_routes as m
    if op == "--lista":
        fora = m._sep_fora_lista()
        print(f"Fora da Separação agora: {len(fora)}")
        for x in fora:
            print(f"   {x['tipo']:9} {x['ml_id']}  {str(x.get('cliente') or '')[:32]:32}  {x.get('motivo') or ''}")
    elif op in ("--previa", "--aplicar"):
        mostrar(m._so_hoje_rodar(aplicar=(op == "--aplicar"), log=lambda s: None))
    else:
        print(__doc__)
