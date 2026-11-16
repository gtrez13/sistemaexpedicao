"""Virada do dia: arquiva (sem apagar) o que é de dias anteriores e já terminou. Regras em mapa_routes.py (_virada_rodar).

  --previa          mostra o que arquivaria (NÃO muda nada)
  --cron            roda se ainda não rodou hoje (é o que o cron da meia-noite chama)
  --aplicar         roda agora, mesmo que já tenha rodado hoje
  --lista [dias]    arquivados dos últimos dias (padrão 2)
  --desfazer DIA    DESFAZER: volta para a lista tudo que a virada arquivou nesse dia (AAAA-MM-DD)

Uso: venv/bin/python -u virada_dia.py --previa"""
import sys
import time
import warnings

warnings.filterwarnings("ignore")
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
except Exception:
    pass


def mostrar(r):
    print(f"Hoje: {r['hoje']}  ({'APLICADO' if r['aplicado'] else 'PRÉVIA, nada mudou'})")
    print(f"Conferidos no ML: {r['ml_conferidos']}   no Bling (Comercial): {r['com_conferidos']}")
    for e in r["erros"]:
        print(f"   ERRO {e}")
    print(f"Arquivados: {len(r['arquivados'])}")
    for x in r["arquivados"]:
        print(f"   {x['origem']:9} {x['ml_id']:22} {str(x.get('cliente') or '')[:28]:28} {x['status_antes']:10} {x['motivo']}")


if __name__ == "__main__":
    args = sys.argv[1:] or ["--previa"]
    op = args[0]
    import mapa_routes as m
    from db import db_session
    if op == "--previa":
        mostrar(m._virada_rodar(False, log=lambda s: None))
    elif op == "--aplicar":
        mostrar(m._virada_rodar(True, log=lambda s: None))
        with db_session() as c:
            c.execute("DELETE FROM virada_dia WHERE dia=?", (m._hoje(),))
            c.execute("INSERT INTO virada_dia (dia, em, resumo) VALUES (?,?,?)", (m._hoje(), int(time.time()), '{"manual": true}'))
    elif op == "--cron":
        r = m._virada_se_preciso(log=lambda s: None)
        print(time.strftime("%Y-%m-%d %H:%M"), "já tinha rodado hoje" if r is None else f"arquivou {len(r['arquivados'])}")
    elif op == "--lista":
        dias = int(args[1]) if len(args) > 1 else 2
        with db_session() as c:
            m._arq_tabela(c)
            rows = c.execute("SELECT * FROM pedidos_arquivo WHERE em >= ? ORDER BY em DESC", (int((time.time() - dias * 86400) * 1000),)).fetchall()
        print(f"Arquivados nos últimos {dias} dia(s): {len(rows)}")
        for r in rows:
            quando = time.strftime("%d/%m %H:%M", time.localtime(r["em"] // 1000))
            print(f"   {quando}  {r['ml_id']:22} {str(r['cliente'] or '')[:26]:26} {r['status_antes']:10} {r['motivo']}  "
                  f"({r['por']}){'  DESFEITO' if r['desfeito_em'] else ''}")
    elif op == "--desfazer":
        dia = args[1]
        ini = int(time.mktime(time.strptime(dia, "%Y-%m-%d")) * 1000)
        with db_session() as c:
            m._arq_tabela(c)
            rows = c.execute("SELECT * FROM pedidos_arquivo WHERE por='virada do dia' AND desfeito_em IS NULL AND em >= ? AND em < ?",
                             (ini - 6 * 3600 * 1000, ini + 30 * 3600 * 1000)).fetchall()
            n = 0
            for r in rows:
                if m._dia_ts(r["em"] // 1000) != dia:
                    continue
                c.execute("UPDATE pedidos SET status=? WHERE ml_id=? AND status='ARQUIVADO'", (r["status_antes"] or "PENDENTE", r["ml_id"]))
                c.execute("UPDATE pedidos_arquivo SET desfeito_em=?, desfeito_por='desfazer' WHERE ml_id=? AND em=?", (int(time.time()), r["ml_id"], r["em"]))
                n += 1
        print(f"Voltaram para a lista: {n}. A virada desse dia não roda de novo." if n else "Nada para desfazer nesse dia.")
    else:
        print(__doc__)
