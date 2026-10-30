# Aviso "estoque zerado" — WMS LHVMed → Velaplace

Quando o admin confirma que um produto **não existe em estoque**, o WMS zera o saldo no Bling
e avisa o Velaplace (para o Gustavo ver). O Velaplace precisa expor:

`POST {VELAPLACE_API_URL}/api/estoque/zerado`  (caminho configurável: `VELAPLACE_ESTOQUE_PATH`)

Cabeçalho: `Authorization: Bearer <VELAPLACE_TOKEN>` (o mesmo token compartilhado da integração do StockMind).

```json
{
  "evento": "estoque_zerado",
  "sku": "175661521443",
  "produto": "Copo Becker Plástico 50ml",
  "quantidade_anterior": 7,
  "motivo": "texto da explicação do supervisor",
  "ocorrencia_id": 12,
  "origem": "wms-lhvmed",
  "confirmado_por": "biel",
  "confirmado_em": 1790000000
}
```

Resposta esperada: `200/201/202/204`. Qualquer outro código é registrado como erro e o admin pode
clicar em "Tentar de novo" (o WMS não zera o Bling de novo, só reenvia o aviso). A chamada é idempotente
do lado do WMS por `ocorrencia_id`: o Velaplace deve ignorar repetições do mesmo id.

Sem `VELAPLACE_API_URL` e `VELAPLACE_TOKEN` no serviço, o aviso fica "simulado" (só registrado).
