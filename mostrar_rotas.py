"""Só leitura: mostra o código das rotas pedidas (e das funções do projeto que elas chamam, 1 nível).
Uso: venv/bin/python -I mostrar_rotas.py "/sync/vendas-internas" "/pedidos/limpar" > /tmp/rotas.txt"""
import ast
import os
import sys

alvos = sys.argv[1:]
arquivos = []
for raiz, dirs, fs in os.walk("."):
    dirs[:] = [d for d in dirs if d not in ("venv", ".git", "node_modules", "__pycache__", "downloads", "static")]
    arquivos += [os.path.join(raiz, f) for f in fs if f.endswith(".py") and ".bak" not in f]

defs, fontes = {}, {}
for a in arquivos:
    try:
        src = open(a, encoding="utf-8").read()
        t = ast.parse(src)
    except Exception:
        continue
    fontes[a] = src
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(n.name, []).append((a, n))


def mostrar(a, n, titulo):
    seg = ast.get_source_segment(fontes[a], n, padded=True) or ""
    deco = "\n".join(f"@{ast.get_source_segment(fontes[a], d)}" for d in n.decorator_list)
    linhas = seg.splitlines()
    print(f"\n######## {titulo}: {a}:{n.lineno} ({len(linhas)} linhas)")
    print(deco)
    print("\n".join(linhas[:260]))
    if len(linhas) > 260:
        print(f"... (+{len(linhas) - 260} linhas)")


vistos = set()
for alvo in alvos:
    for nome, lst in defs.items():
        for a, n in lst:
            decos = " ".join(ast.get_source_segment(fontes[a], d) or "" for d in n.decorator_list)
            if alvo in decos and (a, n.lineno) not in vistos:
                vistos.add((a, n.lineno))
                mostrar(a, n, f"ROTA {alvo}")
                chamadas = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", None)
                            for c in ast.walk(n) if isinstance(c, ast.Call)}
                for ch in sorted(x for x in chamadas if x and x in defs and not x.startswith("__")):
                    for a2, n2 in defs[ch][:2]:
                        if (a2, n2.lineno) not in vistos and len((ast.get_source_segment(fontes[a2], n2) or "").splitlines()) > 3:
                            vistos.add((a2, n2.lineno))
                            mostrar(a2, n2, f"  chamada por {alvo} -> {ch}")
