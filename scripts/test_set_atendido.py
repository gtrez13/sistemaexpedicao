from services.bling_service import BlingService

PV_ID = 25147529408  # troque por um PV de teste

b = BlingService()

print("🔎 Descobrindo módulo Pedidos/Vendas...")
mod_id = b.obter_modulo_id_pedidos_vendas()
print("mod_id:", mod_id)
if not mod_id:
    raise SystemExit("❌ Não achei o módulo de Pedidos/Vendas")

print("🔎 Buscando situação 'atendido'...")
sit_id = b.obter_situacao_id_por_nome(mod_id, "atendido")
print("sit_id:", sit_id)
if not sit_id:
    raise SystemExit("❌ Não achei situação 'atendido' (confere o nome no seu Bling)")

print("🛠️ Atualizando situação para ATENDIDO...")
ok = b.atualizar_situacao_pv(PV_ID, sit_id)
print("✅ OK" if ok else "❌ FALHOU")