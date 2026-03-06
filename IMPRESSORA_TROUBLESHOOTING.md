# 🖨️ Guia de Troubleshooting - Impressão Automática

## ⚠️ Problemas Comuns e Soluções

### 1. **"Nenhuma impressora padrão disponível"**

**Problema:** A mensagem de erro diz que não há impressora padrão

**Solução:**
```powershell
# No Windows, abra PowerShell como admin:
Get-NetPrinter | Select-Object Name
Get-PrinterPort

# Ou vá em:
# Configurações > Dispositivos > Impressoras e scanners
# E defina uma impressora como "padrão"
```

**Alternativa:** Configure `PRINTER_NAME` no `.env`:
```
PRINTER_NAME=Sua Impressora PDF
```

---

### 2. **"O arquivo PDF fica em modo TEST mas não imprime"**

**Problema:** PDF está sendo salvo em `debug_prints/` mas não está sendo impresso

**Verificação do Modo:**
```bash
# Seu .env tem:
PRINT_MODE=TEST    # PDF salvo em debug_prints/
PRINT_MODE=PROD    # PDF enviado para impressora (padrão)
```

**Para testar impressão real:**
1. Mude `.env` para `PRINT_MODE=PROD`
2. Certifique-se que tem uma impressora configurada
3. Execute o script de diagnóstico

---

### 3. **"Erro na impressão" mas sem detalhes claros**

**Novo Log Detalhado:**

Os logs agora mostram:
- ✅ Se o PDF foi criado
- ✅ Qual impressora está sendo usada  
- ✅ Se o comando foi enviado
- ❌ Qual foi a exceção se falhar

**Verifique o console/logs:**
```
📋 INICIANDO GERAÇÃO DE DOCUMENTOS
   Pedido ML: 12345678
   Bling PV ID: 999
=====================================
📄 PDF criado em: C:\Users\...\tmp12345.pdf
🖨️ Enviando para impressora: HP LaserJet
✅ Comando enviado para impressora
```

---

### 4. **"PDF foi criado mas não saiu na impressora"**

**Possíveis causas:**

| Causa | Solução |
|-------|---------|
| Impressora offline | Ligue a impressora ou use modo MOCK |
| Papel vazio | Coloque papel na bandeja |
| Driver antigo | Atualize driver no Windows |
| Fila travada | Execute em PowerShell: `Remove-PrintJob -PrinterName "Sua Impressora"` |
| Sem suporte a PDF direto | Use impressora virtual (como "Print to PDF") |

---

### 5. **"Como testar tudo antes de usar em produção?"**

**Passo 1: Testar diagnóstico**
```bash
python teste_impressora.py
```

**Passo 2: Testar com modo MOCK**
```bash
# No .env:
DOCS_MODE=MOCK
PRINT_MODE=TEST

# Vai salvar em debug_prints/ sem precisar de impressora
```

**Passo 3: Testar com impressora real**
```bash
# No .env:
DOCS_MODE=MOCK
PRINT_MODE=PROD

# Vai tentar imprimir labels_exemplo.txt
```

**Passo 4: Testar com Bling (produção)**
```bash
# No .env:
DOCS_MODE=REAL
PRINT_MODE=PROD

# Vai trazer dados reais da Bling e imprimir
```

---

## 📝 Configurações Recomendadas

### Para Desenvolvimento (Casa)
```
PRINT_MODE=TEST
DOCS_MODE=MOCK
# PDF fica em debug_prints/, sem depender de impressora
```

### Para Testes com Impressora
```
PRINT_MODE=PROD
DOCS_MODE=MOCK
# Testa impressão com labels_exemplo.txt
# Depois mude DOCS_MODE=REAL
```

### Para Produção (Completo)
```
PRINT_MODE=PROD
DOCS_MODE=REAL
# Traz dados reais da Bling e imprime
```

---

## 🔍 Interpretando os Logs

### ✅ Sucesso
```
📋 INICIANDO GERAÇÃO DE DOCUMENTOS
   Pedido ML: 12345678
   Bling PV ID: 999
✅ NF-e impressa com sucesso
✅ Etiqueta impressa com sucesso
✅ Pedido 12345678 marcado como CONCLUIDO
```

### ⚠️ Advertência (Impressão OK mas PDF não retornou)
```
❌ NF-e PDF não retornou
❌ Etiqueta PDF não retornou
⚠️ Geração de documentos retornou sucesso=False
```

### ❌ Erro Crítico
```
❌ ERRO NA IMPRESSÃO AUTOMÁTICA: 
   Tipo: FileNotFoundError
   Traceback completo abaixo
```

---

## 🛠️ Comandos Úteis (Windows PowerShell)

```powershell
# Ver impressoras disponíveis
Get-NetPrinter

# Ver fila de impressão
Get-PrintJob -PrinterName "Sua Impressora"

# Limpar fila travada
Remove-PrintJob -PrinterName "Sua Impressora"

# Resetar spooler
Get-Service spooler | Restart-Service
```

---

## 📞 Checklist Final

- [ ] `test_impressora.py` passou em todos os testes
- [ ] Impressora padrão está configurada no Windows
- [ ] Arquivo `.env` tem `PRINT_MODE` e `DOCS_MODE` corretos
- [ ] Pasta `debug_prints/` existe e tem permissão de escrita
- [ ] `labels_exemplo.txt` existe (para modo MOCK)
- [ ] Pywin32 está instalado: `pip list | grep pywin32`
- [ ] Impressora tem papel e está ligada

**Qualquer erro?** Veja os logs no console - agora muito mais descritivos! ✅

