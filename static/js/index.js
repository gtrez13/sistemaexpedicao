// =========================
// AGRUPAR "PRODUTOS PARECIDOS"
// =========================
function normalizeBaseName(name) {
    if (!name) return "";
    let s = String(name).toLowerCase();
    s = s.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    s = s.replace(/\b(tamanho|tam|nº|n\.|no\.|numero|número)\b/g, ' ');
    s = s.replace(/\b\d+([.,]\d+)?\b/g, ' ');
    s = s.replace(/[|:/\-\(\)\[\],.]+/g, ' ');
    s = s.replace(/\s+/g, ' ').trim();
    const tokens = s.split(' ').filter(Boolean);
    return tokens.slice(0, 4).join(' ');
}

function buildGroupBlock(title, totalQty) {
    const block = document.createElement('div');
    block.className = 'groupBlock';
    block.dataset.open = "1";
    block.innerHTML = `
    <div class="groupHead">
      <p class="groupTitle" title="${title.replace(/"/g, '&quot;')}">🧩 ${title}</p>
      <div style="display:flex; align-items:center; gap:10px;">
        <span class="groupBadge">${totalQty} un</span>
        <div class="caret" aria-hidden="true">▾</div>
      </div>
    </div>
    <div class="groupBody"></div>
  `;
    block.querySelector('.groupHead').addEventListener('click', () => {
        block.dataset.open = (block.dataset.open === "1") ? "0" : "1";
    });
    return block;
}

// =========================
// SYNC + FILTRO
// =========================
const syncBtn = document.getElementById('syncBtn');
const syncPill = document.getElementById('syncPill');
const syncMsg = document.getElementById('syncMsg');

function setSync(loading, msg) {
    if (!syncBtn) return;
    syncBtn.disabled = loading;
    if (syncPill) syncPill.style.display = loading ? 'inline-flex' : 'none';
    if (syncMsg) syncMsg.textContent = msg || '';
    syncBtn.textContent = loading ? 'Sincronizando...' : 'Sync Mercado Livre';
}

syncBtn?.addEventListener('click', async (e) => {
    e.preventDefault();
    setSync(true, "Iniciando sync...");
    try {
        const res = await fetch('/sincronizar', { method: 'POST', headers: { 'X-Requested-With': 'fetch' } });
        const data = await res.json();

        if (data.success) {
            toast(`✅ Sync ok • ${data.pedidos} pedidos / ${data.itens} itens`, "success");
            setSync(false, "✅ Sync ok");
            setTimeout(() => location.reload(), 450);
        } else {
            toast(`⚠️ ${data.message || 'Nada encontrado'}`, "warning");
            setSync(false, data.message || "Nada encontrado");
        }
    } catch {
        toast("❌ Falha no sync", "danger");
        setSync(false, "❌ Falha no sync");
    }
});

const tipoFilter = document.getElementById('tipoFilter');
const flexSection = document.getElementById('flexSection');
const coletaSection = document.getElementById('coletaSection');

function applyFilter(v) {

    document.querySelectorAll('.order').forEach(order => {

        const tipo = order.dataset.envio; // FLEX ou COLETA

        if (v === 'ALL') {
            order.style.display = '';
            return;
        }

        if (v === 'FLEX' && tipo === 'FLEX') {
            order.style.display = '';
        }
        else if (v === 'COLETA' && tipo === 'COLETA') {
            order.style.display = '';
        }
        else {
            order.style.display = 'none';
        }
    });

    localStorage.setItem('lhv_filter', v);
}

if (tipoFilter) {
    const saved = localStorage.getItem('lhv_filter') || 'ALL';
    tipoFilter.value = saved;
    applyFilter(saved);
    tipoFilter.addEventListener('change', (e) => applyFilter(e.target.value));
}
// =========================
// SCANNER TELA 1 (EXPEDIÇÃO)
// =========================

// Pedido selecionado (contexto obrigatório pra não bipar no pedido errado)
let CURRENT_ML_ID = null;

function setSelectedPedido(mlId) {
    CURRENT_ML_ID = (mlId || '').toString().trim();

    // marca visualmente o pedido selecionado
    document.querySelectorAll('.order').forEach(o => {
        const isSel = (o.dataset.mlId || '') === CURRENT_ML_ID;
        o.classList.toggle('selected', isSel);

        // fallback visual caso seu CSS não tenha .selected
        if (isSel) {
            o.style.outline = '2px solid rgba(13,110,253,.35)';
            o.style.outlineOffset = '2px';
        } else {
            o.style.outline = '';
            o.style.outlineOffset = '';
        }
    });
}

// Clique no card seleciona o pedido (ml_id) pro scanner
function bindPedidoSelection() {
    document.querySelectorAll('.order[data-ml-id]').forEach(order => {
        order.addEventListener('click', (e) => {
            // não roubar clique de botões/inputs/links
            if (e.target.closest('button, input, select, textarea, a, label')) return;
            setSelectedPedido(order.dataset.mlId);
            if (scanMsg) scanMsg.textContent = `📌 Pedido selecionado: ${CURRENT_ML_ID}`;
        });
    });
}

const scannerInput = document.getElementById('scannerInput');
const scanMsg = document.getElementById('scanMsg');
const focusBtn = document.getElementById('focusBtn');

function focusScanner() {
    try {
        scannerInput?.focus({ preventScroll: true });
    } catch {}
}

window.addEventListener('load', () => {
    bindPedidoSelection();

    const firstVisible = Array.from(document.querySelectorAll('.order[data-ml-id]'))
        .find(o => o.offsetParent !== null);

    if (firstVisible) {
        setSelectedPedido(firstVisible.dataset.mlId);
    }

    // 🔥 ATIVA SCANNER AUTOMATICAMENTE
    setTimeout(focusScanner, 200);
});
document.addEventListener('click', (e) => {

    // não rouba foco de botões, inputs etc
    if (e.target.closest('button, input, select, textarea, a, label')) return;

    focusScanner();
});
focusBtn?.addEventListener('click', focusScanner);

scannerInput?.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;

    e.preventDefault();

    const sku = (scannerInput.value || '').trim();
    scannerInput.value = '';

    if (!sku) return;

    // sem pedido selecionado não dá pra garantir que vai bipar no pedido certo

    if (scanMsg) scanMsg.textContent = "Processando...";

    try {
        const res = await fetch('/verificar_e_bipar', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sku, ml_id: CURRENT_ML_ID })
        });

        const data = await res.json();

        if (!data.success) {
            if (scanMsg) scanMsg.textContent = "❌ " + (data.error || "Erro");
            return;
        }

        if (scanMsg) scanMsg.textContent = "✅ Bipado!";

        // atualiza página
        setTimeout(() => location.reload(), 300);

    } catch (err) {
        if (scanMsg) scanMsg.textContent = "❌ Falha conexão";
    }

    focusScanner();
});