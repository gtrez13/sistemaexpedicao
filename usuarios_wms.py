"""Usuários do WMS: listar, nova senha, renomear. Guarda o que muda num arquivo antes (desfazer volta tudo).

  venv/bin/python -u usuarios_wms.py --lista
  venv/bin/python -u usuarios_wms.py --senha marcio nathaniel
  venv/bin/python -u usuarios_wms.py --renomear "jucie silva" --nome "Eduardo Inacio Vicentine de Oliveira" --usuario eduardo --senha [--email x@y]
  venv/bin/python -u usuarios_wms.py --desfazer usuarios_bak_<data>.json

A senha nova é gerada aqui no servidor, no mesmo formato das que já existem (confere antes de gravar) e aparece só
nesta tela. Não mostra nem guarda senha de ninguém em texto: o arquivo de backup tem só o hash antigo (dono: só você)."""
import glob
import json
import os
import secrets
import sys
import time
import unicodedata
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

PASS = ["password_hash", "senha_hash", "hashed_password", "pwd_hash", "password", "senha"]
LOGIN = ["username", "usuario", "login", "user_name"]
NOME = ["full_name", "nome", "nome_completo", "name"]
EMAIL = ["email", "e_mail"]
ROLE = ["role", "papel", "perfil"]
ATIVO = ["ativo", "active", "is_active"]
ALFA = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


def sem_acento(s):
    return "".join(ch for ch in unicodedata.normalize("NFD", str(s or "")) if unicodedata.category(ch) != "Mn").lower().strip()


def colunas(c):
    try:
        rows = c.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'").fetchall()
        tabs = {}
        for r in rows:
            tabs.setdefault(r["table_name"], []).append(r["column_name"])
        return tabs
    except Exception:
        tabs = {}
        for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            tabs[r["name"]] = [x[1] for x in c.execute(f"PRAGMA table_info({r['name']})").fetchall()]
        return tabs


def achar_tabela(c):
    tabs = colunas(c)
    cand = [(t, cols) for t, cols in tabs.items() if any(p in cols for p in PASS) and any(l in cols for l in LOGIN)
            and not t.startswith(("bkp_", "backup"))]
    if not cand:
        sys.exit("Não achei a tabela de usuários (coluna de senha + usuário).")
    cand.sort(key=lambda x: (x[0] not in ("users", "usuarios", "user"), x[0]))
    t, cols = cand[0]
    pega = lambda lst: next((x for x in lst if x in cols), None)
    return {"tabela": t, "id": "id" if "id" in cols else None, "senha": pega(PASS), "login": pega(LOGIN), "nome": pega(NOME),
            "email": pega(EMAIL), "role": pega(ROLE), "ativo": pega(ATIVO)}


def esquema_hash(c, T):
    """Mesmo formato das senhas que já existem + o que o código do servidor usa para conferir."""
    h = next((r[0] for r in c.execute(f"SELECT {T['senha']} FROM {T['tabela']} WHERE {T['senha']} IS NOT NULL").fetchall() if r[0]), "")
    fonte = ""
    for f in ["app.py", "user_routes.py", "auth_routes.py", "models.py"] + glob.glob("*auth*.py") + glob.glob("*user*.py"):
        try:
            fonte += open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            pass
    if h.startswith(("pbkdf2:", "scrypt:")):
        if fonte and "check_password_hash" not in fonte:
            sys.exit("As senhas são do werkzeug, mas não achei check_password_hash no código do servidor: parei sem mexer.")
        from werkzeug.security import check_password_hash, generate_password_hash
        metodo = h.split("$", 1)[0]
        return lambda s: generate_password_hash(s, method=metodo), check_password_hash, f"werkzeug {metodo}"
    if h.startswith(("$2a$", "$2b$", "$2y$")):
        if fonte and "bcrypt" not in fonte:
            sys.exit("As senhas são bcrypt, mas não achei bcrypt no código do servidor: parei sem mexer.")
        import bcrypt
        rodadas = int(h[4:6])
        return (lambda s: bcrypt.hashpw(s.encode(), bcrypt.gensalt(rodadas)).decode(),
                lambda hh, s: bcrypt.checkpw(s.encode(), hh.encode()), f"bcrypt {rodadas}")
    sys.exit(f"Formato de senha que não conheço ({h[:7]}...): parei sem mexer.")


def achar(c, T, quem):
    rows = [dict(r) for r in c.execute(f"SELECT * FROM {T['tabela']}").fetchall()]
    q = sem_acento(quem)
    exato = [r for r in rows if sem_acento(r[T["login"]]) == q]
    if exato:
        return exato
    return [r for r in rows if q in sem_acento(r[T["login"]]) or (T["nome"] and q in sem_acento(r[T["nome"]]))
            or (T["email"] and q in sem_acento(r[T["email"]]))]


def linha(T, r):
    return (f"{str(r[T['login']]):18} {str(r.get(T['nome']) or '')[:36]:36} {str(r.get(T['email']) or '-'):30} "
            f"{str(r.get(T['role']) or ''):11} {'ativo' if (not T['ativo'] or r.get(T['ativo']) in (1, True, '1', 't', 'true')) else 'INATIVO'}")


def nova_senha():
    return "".join(secrets.choice(ALFA) for _ in range(10))


a = sys.argv[1:]
if not a or a[0] in ("-h", "--help"):
    print(__doc__); sys.exit(0)
arg = lambda k: a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else None

if a[0] == "--desfazer":            # fora do bloco principal: sys.exit dentro do "with" desfaz a gravação
    bk = json.load(open(a[1], encoding="utf-8"))
    with db_session() as c:
        for r in bk["linhas"]:
            sets = [k for k in r if k != "_chave"]
            c.execute(f"UPDATE {bk['tabela']} SET {', '.join(f'{k}=?' for k in sets)} WHERE {bk['chave']}=?",
                      tuple(r[k] for k in sets) + (r["_chave"],))
    print(f"Voltou como estava: {len(bk['linhas'])} usuário(s).")
    sys.exit(0)

with db_session() as c:
    T = achar_tabela(c)
    chave = T["id"] or T["login"]
    if a[0] == "--lista":
        print(f"Tabela {T['tabela']}:")
        for r in c.execute(f"SELECT * FROM {T['tabela']} ORDER BY {chave}").fetchall():
            print("   " + linha(T, dict(r)))
        sys.exit(0)

    alvos = []                       # (linha, {coluna: valor novo}, senha_nova)
    gerar = conferir = None
    if a[0] == "--senha" or "--senha" in a:
        gerar, conferir, esquema = esquema_hash(c, T)
    if a[0] == "--senha":
        for quem in [x for x in a[1:] if not x.startswith("--")]:
            ach = achar(c, T, quem)
            if len(ach) != 1:
                print(f"'{quem}': {'nenhum usuário' if not ach else 'mais de um'} — não mexi. Candidatos:")
                for r in ach:
                    print("   " + linha(T, r))
                continue
            s = nova_senha()
            alvos.append((ach[0], {T["senha"]: gerar(s)}, s))
    elif a[0] == "--renomear":
        ach = achar(c, T, a[1])
        if len(ach) != 1:
            print(f"'{a[1]}': {'nenhum usuário' if not ach else 'mais de um'} — não mexi.")
            for r in ach:
                print("   " + linha(T, r))
            sys.exit(1)
        muda = {}
        if arg("--nome") and T["nome"]:
            muda[T["nome"]] = arg("--nome").strip()
        if arg("--usuario"):
            novo = arg("--usuario").strip()
            outro = [r for r in achar(c, T, novo) if sem_acento(r[T["login"]]) == sem_acento(novo) and r[chave] != ach[0][chave]]
            if outro:
                sys.exit(f"Já existe outro usuário '{novo}': não mexi.")
            muda[T["login"]] = novo
        if arg("--email") is not None and T["email"]:
            muda[T["email"]] = arg("--email").strip() or None
        s = None
        if "--senha" in a:
            s = nova_senha()
            muda[T["senha"]] = gerar(s)
        if not muda:
            sys.exit("Nada para mudar (use --nome, --usuario e/ou --senha).")
        alvos.append((ach[0], muda, s))
    else:
        print(__doc__); sys.exit(1)

    if not alvos:
        sys.exit("Nada foi mudado.")
    for _, muda, s in alvos:            # confere a senha nova com o mesmo verificador do login antes de gravar
        if s and not conferir(muda[T["senha"]], s):
            sys.exit("A senha gerada não passou na conferência: parei sem mexer.")
    arq = f"usuarios_bak_{time.strftime('%Y%m%d%H%M%S')}.json"
    n = 1
    while os.path.exists(arq):
        n += 1
        arq = f"usuarios_bak_{time.strftime('%Y%m%d%H%M%S')}_{n}.json"
    bk = {"tabela": T["tabela"], "chave": chave, "linhas": [{"_chave": r[chave], **{k: r.get(k) for k in muda}} for r, muda, _ in alvos]}
    with open(arq, "w", encoding="utf-8") as f:
        json.dump(bk, f, ensure_ascii=False, default=str)
    os.chmod(arq, 0o600)
    for r, muda, _ in alvos:
        c.execute(f"UPDATE {T['tabela']} SET {', '.join(f'{k}=?' for k in muda)} WHERE {chave}=?", tuple(muda.values()) + (r[chave],))
    print("Feito:\n")
    for r, muda, s in alvos:
        print(f"   Usuário: {muda.get(T['login'], r[T['login']])}")
        print(f"   Nome:    {muda.get(T['nome'], r.get(T['nome']) if T['nome'] else '')}")
        print(f"   E-mail:  {muda.get(T['email'], r.get(T['email'])) or '(sem e-mail cadastrado)' if T['email'] else '(o WMS não guarda e-mail)'}")
        if s:
            print(f"   Senha:   {s}")
        print()
    print(f"Desfazer: venv/bin/python -u usuarios_wms.py --desfazer {arq}")
