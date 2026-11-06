#!/usr/bin/env python3
"""
Lê a caixa de e-mail onde os fornecedores mandam XML (ex.: financeiro@lhvmed.com.br) e importa toda NF-e
para o RECEBIMENTO do WMS (status "aguardando"). Roda por cron a cada 10 min. Só lê; não apaga, não move.

.env:
    RECEB_IMAP_HOST=email-ssl.com.br      (Locaweb)   RECEB_IMAP_PORT=993
    RECEB_IMAP_USER=financeiro@lhvmed.com.br
    RECEB_IMAP_PASS=...
    RECEB_IMAP_PASTA=INBOX                 RECEB_IMAP_DIAS=30   (quantos dias olhar pra trás na 1ª vez)

    venv/bin/python receb_email_sync.py          # importa o que ainda não foi lido
    venv/bin/python receb_email_sync.py --dry    # só mostra
"""
from __future__ import annotations

import email
import imaplib
import io
import os
import sys
import time
import zipfile
from datetime import date, timedelta
from email.header import decode_header

try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass

from db import db_session
import recebimento_routes as rr


def _dec(s):
    if not s:
        return ""
    out = []
    for txt, enc in decode_header(s):
        out.append(txt.decode(enc or "utf-8", "ignore") if isinstance(txt, bytes) else txt)
    return "".join(out)


def xmls_do_email(msg) -> list[tuple[str, str]]:
    """[(nome, conteudo)] de anexos .xml (inclusive dentro de .zip) que parecem NF-e."""
    out = []
    for part in msg.walk():
        nome = _dec(part.get_filename() or "")
        if not nome:
            continue
        dados = part.get_payload(decode=True) or b""
        if nome.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(dados)) as z:
                    for n in z.namelist():
                        if n.lower().endswith(".xml"):
                            out.append((n, z.read(n).decode("utf-8", "ignore")))
            except Exception:
                pass
        elif nome.lower().endswith(".xml"):
            out.append((nome, dados.decode("utf-8", "ignore")))
    return [(n, t) for n, t in out if "infNFe" in t and "<emit>" in t]


def main():
    dry = "--dry" in sys.argv
    host = os.environ.get("RECEB_IMAP_HOST", "email-ssl.com.br")
    port = int(os.environ.get("RECEB_IMAP_PORT", "993"))
    user, pwd = os.environ.get("RECEB_IMAP_USER"), os.environ.get("RECEB_IMAP_PASS")
    pasta = os.environ.get("RECEB_IMAP_PASTA", "INBOX")
    dias = int(os.environ.get("RECEB_IMAP_DIAS", "30"))
    if not user or not pwd:
        print("Configure RECEB_IMAP_USER e RECEB_IMAP_PASS no .env"); sys.exit(2)

    rr.garantir_tabelas()
    with db_session() as c:
        c.execute("CREATE TABLE IF NOT EXISTS receb_email_uids (uid TEXT PRIMARY KEY, lido_em BIGINT)")
        lidos = {r["uid"] for r in c.execute("SELECT uid FROM receb_email_uids").fetchall()}

    m = imaplib.IMAP4_SSL(host, port)
    m.login(user, pwd)
    m.select(pasta, readonly=True)
    desde = (date.today() - timedelta(days=dias)).strftime("%d-%b-%Y")
    st, dados = m.uid("search", None, f'(SINCE {desde})')
    uids = [u.decode() for u in (dados[0].split() if dados and dados[0] else [])]
    novos = [u for u in uids if u not in lidos]
    print(f">> {len(uids)} e-mails desde {desde}, {len(novos)} ainda não lidos", flush=True)
    importadas = 0
    for u in novos:
        st, raw = m.uid("fetch", u, "(BODY.PEEK[])")
        if st != "OK" or not raw or not raw[0]:
            continue
        msg = email.message_from_bytes(raw[0][1])
        xmls = xmls_do_email(msg)
        for nome, txt in xmls:
            try:
                if dry:
                    nf = rr.parse_nfe(txt); print(f"   [dry] {nome}: NF {nf['numero']} {nf['fornecedor_nome']} {nf['valor']}")
                else:
                    r = rr._importar_xml(txt, "email")
                    if not r["ja_existia"]:
                        importadas += 1
                        print(f"   + {nome} -> nota #{r['id']}  ({_dec(msg.get('From'))[:50]})", flush=True)
            except Exception as e:
                print(f"   ! {nome}: {e}", flush=True)
        if not dry:
            with db_session() as c:
                c.execute("INSERT INTO receb_email_uids (uid, lido_em) VALUES (?,?) ON CONFLICT (uid) DO NOTHING", (u, int(time.time())))
    m.logout()
    print(f">> {importadas} nota(s) nova(s) no recebimento.", flush=True)


if __name__ == "__main__":
    main()
