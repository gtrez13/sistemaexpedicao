"""Troca um nome de pessoa em todo o banco do WMS (Biel 17/11: "o nome do jucie precisa ser mudado para eduardo").

Procura em TODAS as tabelas e colunas de texto (menos senha) o nome antigo e mostra onde aparece.
Só troca o valor que é EXATAMENTE o nome antigo (sem diferença de maiúscula), ex.: "jucie" -> "eduardo" no usuário que bipou,
que separou, que criou... Texto livre que só contém o nome (uma justificativa, uma observação) é mostrado mas não é mexido.

  venv/bin/python -u trocar_nome.py jucie=eduardo "jucie silva=Eduardo Inacio Vicentine de Oliveira"            (PRÉVIA)
  venv/bin/python -u trocar_nome.py jucie=eduardo "jucie silva=Eduardo Inacio Vicentine de Oliveira" --aplicar  (troca; backup em JSON)
  venv/bin/python -u trocar_nome.py --restaurar trocar_nome_bak_AAAAMMDDHHMMSS.json                               (desfaz)
Tabela sem chave primária: mostra, mas não troca (não daria para desfazer com segurança)."""
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
from db import db_session

SENHA = ("password", "senha", "hash", "token", "secret")


def colunas_texto(c):
    """{tabela: ([colunas de texto], [chave primária])}"""
    try:
        rows = c.execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                             WHERE table_schema='public' AND data_type IN ('text','character varying','character')""").fetchall()
        pk = c.execute("""SELECT tc.table_name, kcu.column_name FROM information_schema.table_constraints tc
                            JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                           WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'""").fetchall()
        out = {}
        for r in rows:
            if any(s in r["column_name"].lower() for s in SENHA):
                continue
            out.setdefault(r["table_name"], ([], []))[0].append(r["column_name"])
        for r in pk:
            if r["table_name"] in out:
                out[r["table_name"]][1].append(r["column_name"])
        return out
    except Exception:
        out = {}
        for t in [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]:
            info = c.execute(f"PRAGMA table_info({t})").fetchall()
            cols = [x[1] for x in info if "CHAR" in str(x[2]).upper() or "TEXT" in str(x[2]).upper() or not x[2]]
            cols = [x for x in cols if not any(s in x.lower() for s in SENHA)]
            pks = [x[1] for x in info if x[5]]
            if cols:
                out[t] = (cols, pks)
        return out


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); return
    if args[0] == "--restaurar":
        bak = json.load(open(args[1]))
        with db_session() as c:
            for x in reversed(bak["linhas"]):                 # ao contrário: a chave volta primeiro
                pk = x.get("pk_depois") or x["pk"]            # a coluna trocada era a própria chave: procura pelo valor novo
                where = " AND ".join(f'"{k}"=?' for k in pk)
                c.execute(f'UPDATE "{x["tabela"]}" SET "{x["coluna"]}"=? WHERE {where}', (x["antes"], *pk.values()))
        print(f"Restaurado: {len(bak['linhas'])} valores voltaram como estavam."); return
    aplicar = "--aplicar" in args
    pares = [a.split("=", 1) for a in args if "=" in a and not a.startswith("--")]
    pares = [(de.strip(), para.strip()) for de, para in pares if de.strip() and para.strip()]
    if not pares:
        print("Passe NOME_ANTIGO=NOME_NOVO (um ou mais)."); return
    linhas, parciais, sem_pk = [], [], set()
    with db_session() as c:
        tabs = colunas_texto(c)
        for t, (cols, pks) in sorted(tabs.items()):
            for col in cols:
                for de, para in pares:
                    try:
                        exatos = c.execute(f'SELECT {", ".join(chr(34) + k + chr(34) for k in pks) or "1 AS x"} FROM "{t}" WHERE LOWER("{col}") = LOWER(?)', (de,)).fetchall()
                        n_parc = c.execute(f'SELECT COUNT(*) AS n FROM "{t}" WHERE LOWER("{col}") LIKE LOWER(?) AND LOWER("{col}") <> LOWER(?)',
                                           (f"%{de}%", de)).fetchone()["n"]
                    except Exception:
                        continue
                    if exatos:
                        if not pks:
                            sem_pk.add(f"{t}.{col} ({len(exatos)})")
                        else:
                            for r in exatos:
                                linhas.append({"tabela": t, "coluna": col, "pk": {k: r[k] for k in pks}, "antes": None, "de": de, "para": para})
                    if n_parc:
                        parciais.append(f"{t}.{col}: {n_parc} texto(s) que contêm '{de}' (não mexo)")
        # valor exato de antes (para o desfazer)
        for x in linhas:
            where = " AND ".join(f'"{k}"=?' for k in x["pk"])
            x["antes"] = c.execute(f'SELECT "{x["coluna"]}" AS v FROM "{x["tabela"]}" WHERE {where}', tuple(x["pk"].values())).fetchone()["v"]
    resumo = {}
    for x in linhas:
        resumo[(x["tabela"], x["coluna"], x["de"], x["para"])] = resumo.get((x["tabela"], x["coluna"], x["de"], x["para"]), 0) + 1
    print(f"{'APLICANDO' if aplicar else 'PRÉVIA (nada muda)'}: {len(linhas)} valores exatos para trocar")
    for (t, col, de, para), n in sorted(resumo.items()):
        print(f"   {t}.{col}: {n} x '{de}' -> '{para}'")
    for p in parciais:
        print("   (só mostra) " + p)
    for s in sorted(sem_pk):
        print(f"   (sem chave, não troco) {s}")
    if not aplicar:
        print("\nPara trocar, rode de novo com --aplicar no fim."); return
    # a coluna que é chave (ex.: usuário) troca por último, senão as outras colunas da mesma linha não acham mais a linha
    linhas.sort(key=lambda x: x["coluna"] in x["pk"])
    for x in linhas:
        if x["coluna"] in x["pk"]:
            x["pk_depois"] = {**x["pk"], x["coluna"]: x["para"]}
    arq = f"trocar_nome_bak_{time.strftime('%Y%m%d%H%M%S')}.json"
    json.dump({"pares": pares, "linhas": linhas}, open(arq, "w"), ensure_ascii=False, default=str)
    os.chmod(arq, 0o600)
    with db_session() as c:
        for x in linhas:
            where = " AND ".join(f'"{k}"=?' for k in x["pk"])
            c.execute(f'UPDATE "{x["tabela"]}" SET "{x["coluna"]}"=? WHERE {where}', (x["para"], *x["pk"].values()))
    print(f"\nTrocado. Backup: {arq}")
    print(f"Desfazer: venv/bin/python -u trocar_nome.py --restaurar {arq}")


if __name__ == "__main__":
    main()
