#!/usr/bin/env python3
"""Apaga do WMS uma nota de entrada que foi só teste (Biel 19/11: "temos que tirar isso do banco, foi só teste").

    venv/bin/python receb_apagar_nota.py --lista                 # notas abertas (para conferir)
    venv/bin/python receb_apagar_nota.py --numero 275089         # prévia: o que seria apagado (nada muda)
    venv/bin/python receb_apagar_nota.py --numero 275089 --aplicar
    venv/bin/python receb_apagar_nota.py --restaurar receb_apagar_bak_<ts>.json

Apaga só do WMS (receb_notas, receb_itens, receb_etq, receb_entrada_log); NÃO mexe no Bling. Nota que já lançou entrada
(status lancada ou etapa de entrada começada) não apaga sem --forcar. Backup em JSON antes; --restaurar volta tudo.
"""
import argparse, json, os, sys, time
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except Exception:
    pass
sys.path.insert(0, os.getcwd())
try:
    from db import db_session
except ImportError:
    from db.database import db_session

TABS = [("receb_itens", "nota_id"), ("receb_etq", "nota_id"), ("receb_entrada_log", "nota_id")]


def achar(c, a):
    if a.id:
        rows = c.execute("SELECT * FROM receb_notas WHERE id=?", (a.id,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM receb_notas WHERE numero=? OR numero=?", (str(a.numero), str(a.numero).lstrip("0"))).fetchall()
        if a.serie:
            rows = [r for r in rows if str(r["serie"] or "") == str(a.serie)]
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lista", action="store_true"); ap.add_argument("--numero"); ap.add_argument("--serie"); ap.add_argument("--id", type=int)
    ap.add_argument("--aplicar", action="store_true"); ap.add_argument("--forcar", action="store_true"); ap.add_argument("--restaurar")
    a = ap.parse_args()
    with db_session() as c:
        if a.restaurar:
            bk = json.load(open(a.restaurar, encoding="utf-8"))
            for tab, linhas in bk.items():
                for ln in linhas:
                    cols = list(ln)
                    c.execute(f"INSERT INTO {tab} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(ln[k] for k in cols))
            print("restaurado:", {t: len(l) for t, l in bk.items()}); return 0
        if a.lista or not (a.numero or a.id):
            for r in c.execute("SELECT id, numero, serie, fornecedor_nome, valor, status, entrada_etapa, criado_em FROM receb_notas ORDER BY id DESC LIMIT 40").fetchall():
                print(f"  id {r['id']:>4}  NF {r['numero']}/{r['serie'] or '-'}  {str(r['fornecedor_nome'])[:34]:<34}  R$ {float(r['valor'] or 0):>10,.2f}  {r['status']:<11} entrada={r['entrada_etapa'] or '-'}")
            return 0
        notas = achar(c, a)
        if not notas:
            print("não achei essa nota no WMS"); return 1
        if len(notas) > 1 and not a.id:
            print("mais de uma nota com esse número: use --serie ou --id"); [print(f"  id {n['id']} NF {n['numero']}/{n['serie']} {n['fornecedor_nome']}") for n in notas]; return 1
        n = notas[0]
        print(f"NF {n['numero']}/{n['serie'] or '-'} · {n['fornecedor_nome']} · R$ {float(n['valor'] or 0):,.2f} · status {n['status']} · entrada {n['entrada_etapa'] or '-'}")
        if (n["status"] == "lancada" or n.get("entrada_etapa")) and not a.forcar:
            print("essa nota já começou/terminou a entrada no Bling: não apago sem --forcar (apagar aqui NÃO desfaz nada no Bling)"); return 1
        bk = {"receb_notas": [n]}
        for tab, col in TABS:
            try:
                bk[tab] = [dict(r) for r in c.execute(f"SELECT * FROM {tab} WHERE {col}=?", (n["id"],)).fetchall()]
            except Exception:
                bk[tab] = []
        print("vai apagar:", {t: len(l) for t, l in bk.items()})
        if not a.aplicar:
            print("(prévia: nada mudou; repita com --aplicar)"); return 0
        arq = f"receb_apagar_bak_{n['id']}_{time.strftime('%Y%m%d%H%M%S')}.json"
        json.dump(bk, open(arq, "w", encoding="utf-8"), ensure_ascii=False, default=str)
        os.chmod(arq, 0o600)
        for tab, col in TABS:
            if bk.get(tab):
                c.execute(f"DELETE FROM {tab} WHERE {col}=?", (n["id"],))
        c.execute("DELETE FROM receb_notas WHERE id=?", (n["id"],))
        print(f"apagado. backup: {arq}  (voltar: --restaurar {arq})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
