// =========================
// AGRUPAR PRODUTOS PARECIDOS
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
      <p class="groupTitle">🧩 ${title}</p>
      <div style="display:flex; align-items:center; gap:10px;">
        <span class="groupBadge">${totalQty} un</span>
        <div class="caret">▾</div>
      </div>
    </div>
    <div class="groupBody"></div>
  `;
  block.querySelector('.groupHead').addEventListener('click', () => {
    block.dataset.open = block.dataset.open === "1" ? "0" : "1";
  });
  return block;
}

// trava uso fora da aba ativa
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    CURRENT_ML_ID = null;
  } else {
    loadPedidoAtual();
    focusScanner();
  }
});

function groupSimilarProducts() {
  document.querySelectorAll('.order').forEach(order => {

    const zone = order.querySelector('.normalZone');
    if (!zone) return;

    const rows = Array.from(zone.querySelectorAll('.itemRow'));
    if (rows.length <= 1) return;

    const map = new Map();

    rows.forEach(row => {
      const name = row.dataset.itemName || row.querySelector('.itemName')?.textContent || '';
      if (!name) return;

      const key = normalizeBaseName(name);
      const qty = Number(row.querySelector('.qtot')?.textContent || 0);

      if (!map.has(key)) map.set(key, { rows: [], qtySum: 0, firstName: name });

      const g = map.get(key);
      g.rows.push(row);
      g.qtySum += qty;
    });

    if (map.size <= 1) return;

    const fragment = document.createDocumentFragment();

    Array.from(map.entries())
      .sort((a, b) => b[1].qtySum - a[1].qtySum)
      .forEach(([_, g]) => {
        if (g.rows.length === 1) {
          fragment.appendChild(g.rows[0]);
          return;
        }
        const block = buildGroupBlock(g.firstName, g.qtySum);
        const body = block.querySelector('.groupBody');
        g.rows.forEach(r => body.appendChild(r));
        fragment.appendChild(block);
      });

    zone.innerHTML = '';
    zone.appendChild(fragment);
  });
}

window.addEventListener('DOMContentLoaded', () => {
  groupSimilarProducts();
  setTimeout(groupSimilarProducts, 300);
});


// =========================
// SCANNER CONFERÊNCIA
// =========================
const scannerInput = document.getElementById('scannerInput');
const focusBtn = document.getElementById('focusBtn');
const clearBtn = document.getElementById('clearBtn');
const scanStatus = document.getElementById('scanStatus');
const pedidoAtualTitle = document.getElementById('pedidoAtualTitle');
const faltasText = document.getElementById('faltasText');

let CURRENT_ML_ID = null;

document.querySelectorAll(".order-card").forEach(card => {

  card.addEventListener("click", () => {

    document.querySelectorAll(".order-card").forEach(c =>
      c.classList.remove("selected")
    );

    card.classList.add("selected");

    CURRENT_ML_ID = card.dataset.mlId;

    console.log("Pedido selecionado:", CURRENT_ML_ID);

  });

});
let BIP_LOCK = false;

// foco contínuo
function focusScanner() { try { scannerInput?.focus(); } catch { } }

window.addEventListener('load', () => {

  focusScanner();

  setInterval(() => {
    focusScanner();
  }, 3000);

  setTimeout(focusScanner, 300);
  setTimeout(focusScanner, 1000);

  verificarTodosPedidos(); // ← ADICIONE ISSO

});


focusBtn?.addEventListener('click', e => { e.preventDefault(); focusScanner(); });
clearBtn?.addEventListener('click', e => { e.preventDefault(); scannerInput.value = ''; focusScanner(); });

window.addEventListener('click', (e) => {
  if (!e.target.closest('select, option, input, button, textarea, a, label')) focusScanner();
});


// ---------------- STATUS UI ----------------
function setStatus(title, sub, type = "idle") {
  if (!scanStatus) return;
  const icon = type === "ok" ? "✅" : type === "warn" ? "⚠️" : type === "err" ? "⛔" : "⏳";
  scanStatus.innerHTML = `<div class="title">${icon} ${title || ""}</div><div class="sub">${sub || ""}</div>`;
}


// ---------------- CARREGAR PEDIDO ----------------
async function loadPedidoAtual() {

  document.body.classList.remove("pedido-finalizado");

  try {
    setStatus("Carregando pedido…", "Buscando próximo pedido…", "idle");

    const res = await fetch('/scanner/pedido_atual', { headers: { 'X-Requested-With': 'fetch' } });
    const data = await res.json();

    if (!data.success) {
      CURRENT_ML_ID = null;
      pedidoAtualTitle.textContent = "Nenhum pedido SEPARADO";
      faltasText.textContent = "Faltas: —";
      setStatus("Aguardando bip…", "Sem pedidos agora.", "warn");
      return;
    }

    CURRENT_ML_ID = data.pedido.ml_id;
    pedidoAtualTitle.textContent = `${CURRENT_ML_ID} • ${data.pedido.cliente_nome}`;
    faltasText.textContent = "Faltas: —";

    setStatus("Aguardando bip…", "Bipe um item.", "ok");

  } catch {
    setStatus("Erro", "Falha conexão", "err");
  }
}


// ---------------- MARCAR ITEM ----------------
function findItemRowBySku(sku) {

  if (!CURRENT_ML_ID) return null;

  return document.querySelector(
    `.item-row[data-ml-id="${CSS.escape(CURRENT_ML_ID)}"][data-sku="${CSS.escape(String(sku))}"]`
  );
}
function updateItemBip(sku) {

  const row = findItemRowBySku(sku);
  if (!row) return;

  const qb = row.querySelector('.qbip');
  const qt = row.querySelector('.qtot');
  if (!qb || !qt) return;

  let b = Number(qb.textContent || 0) + 1;

  qb.textContent = b;

  if (b >= Number(qt.textContent || 0)){
    row.classList.add('is-checked');
  }

  verificarPedidoFinalizado(CURRENT_ML_ID);

}


// ---------------- BIPAR ----------------

async function processarBip(sku) {

  if (!CURRENT_ML_ID) {

    const res = await fetch("/scanner/descobrir_pedido", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sku })
    })

    const data = await res.json()

    if (!data.pedidos.length) {
      alert("Produto não pertence a pedido separado")
      return
    }

    if (data.pedidos.length === 1) {
      CURRENT_ML_ID = data.pedidos[0].ml_id
    }
    else {
      CURRENT_ML_ID = escolherPedido(data.pedidos)
    }

  }

  await biparSKU_Tela2(sku)
}

function escolherPedido(pedidos) {

  let msg = "Escolha o pedido:\n\n"

  pedidos.forEach((p, i) => {
    msg += `${i + 1} - ${p.ml_id} (${p.cliente_nome})\n`
  })

  const escolha = prompt(msg)

  const index = Number(escolha) - 1

  if (pedidos[index]) {
    return pedidos[index].ml_id
  }

  return null
}

function verificarTodosPedidos() {

  document.querySelectorAll(".order-card").forEach(card => {

    const mlId = card.dataset.mlId;

    const rows = card.querySelectorAll(".item-row");

    let faltando = 0;

    rows.forEach(r => {

      const qb = Number(r.querySelector('.qbip')?.textContent || 0);
      const qt = Number(r.querySelector('.qtot')?.textContent || 0);

      faltando += (qt - qb);

    });

    if (faltando <= 0) {

      card.classList.add("done-order");

      const container = card.parentElement;

      container.appendChild(card);

    }

  });

}

function verificarPedidoFinalizado(mlId) {

  const rows = document.querySelectorAll(`.item-row[data-ml-id="${mlId}"]`);

  let faltando = 0;

  rows.forEach(r => {

    const qb = Number(r.querySelector('.qbip')?.textContent || 0);
    const qt = Number(r.querySelector('.qtot')?.textContent || 0);

    faltando += (qt - qb);

  });

  if (faltando <= 0) {

    const card = document.querySelector(`.order-card[data-ml-id="${mlId}"]`);

    if (card) {

      card.classList.add("done-order");

      const container = card.parentElement;

      container.appendChild(card);

    }

  }

}

async function biparSKU_Tela2(sku) {

  if (BIP_LOCK) return;
  BIP_LOCK = true;

  sku = String(sku || '').trim().toUpperCase();
  if (!sku || !CURRENT_ML_ID) { BIP_LOCK = false; return; }

  setStatus("Processando…", `SKU: ${sku}`, "idle");

  try {

    const r = await fetch('/scanner/bipar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sku, ml_id: String(CURRENT_ML_ID) })
    });

    const data = await r.json();

    if (!data.success) {
      setStatus("Rejeitado", data.error || "Falha", "err");
      return;
    }

    updateItemBip(sku);

    if (data.pedido_fechado) {

      const card = document.querySelector(`.order-card[data-ml-id="${CURRENT_ML_ID}"]`);

      if (card) {

        // deixa o pedido verde
        card.classList.add("done-order");

        // move para o final da lista
        const container = card.parentElement;
        container.appendChild(card);

      }

      setStatus("Pedido finalizado 📦", "Emitindo NF...", "ok");

      document.body.classList.add("pedido-finalizado");

      if (data.pdf) {

        const filename = data.pdf.split('/').pop();
        const url = `/print/${filename}`;

        if (window.__printingNow) return;
        window.__printingNow = true;
        setTimeout(() => window.__printingNow = false, 4000);

        const win = window.open(url, "_blank");

        if (win) {

          let tries = 0;

          const timer = setInterval(() => {

            tries++;

            try {
              win.focus();
              win.print();
              clearInterval(timer);
            } catch { }

            if (tries > 20) clearInterval(timer);

          }, 300);

        }
      }

      setTimeout(loadPedidoAtual, 2000);
      return;
    }

    let faltando = 0;

    document.querySelectorAll(`.item-row[data-ml-id="${CURRENT_ML_ID}"]`).forEach(r => {
      const qb = Number(r.querySelector('.qbip')?.textContent || 0);
      const qt = Number(r.querySelector('.qtot')?.textContent || 0);
      faltando += (qt - qb);
    });

    setStatus("OK", `Faltam ${faltando} unidades`, "ok");

  } catch {

    setStatus("Erro", "Falha conexão", "err");

  } finally {

    BIP_LOCK = false;

  }
}


// input leitor
scannerInput?.addEventListener('keydown', async e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    const sku = scannerInput.value.trim();
    scannerInput.value = '';
    if (sku) await processarBip(sku);
    focusScanner();
  }
});