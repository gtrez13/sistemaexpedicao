#!/usr/bin/env bash
# O WMS roda com 1 worker "sync": enquanto um pedido fecha (NF-e + etiqueta, até 2 min), TODO o resto espera
# (reimprimir, bips, lista) e o proxy devolve 502. Isto liga 4 threads no mesmo processo (mesma memória).
# Backup da unidade do systemd + 1 comando para desfazer.
set -e
U=$(systemctl show -p FragmentPath wms-russia.service | cut -d= -f2)
[ -n "$U" ] && [ -f "$U" ] || { echo "não achei a unidade do wms-russia.service"; exit 1; }
echo "Unidade: $U"
grep -n "ExecStart" "$U"
if grep -qE "ExecStart=.*(-k |--worker-class)" "$U"; then echo "já tem worker-class definido: não mexi"; exit 0; fi
if grep -qE "ExecStart=.*--threads [2-9]" "$U"; then echo "já está com mais de 1 thread: não mexi"; exit 0; fi
BK="$HOME/wms-russia.service.bak_$(date +%Y%m%d%H%M%S)"
sudo cp "$U" "$BK"
if grep -qE "ExecStart=.*--threads 1( |$)" "$U"; then
  sudo sed -i -E '/^ExecStart=/ s/--threads 1( |$)/--threads 4\1/' "$U"
else
  sudo sed -i -E '/^ExecStart=.*gunicorn/ s/gunicorn /gunicorn --threads 4 /' "$U"
fi
grep -n "ExecStart" "$U"
sudo systemctl daemon-reload
sudo systemctl restart wms-russia.service
sleep 4
systemctl is-active wms-russia.service
sudo journalctl -u wms-russia.service --since "-20 s" --no-pager | grep -E "Using worker|Booting" | tail -3
echo "Para desfazer: sudo cp $BK $U && sudo systemctl daemon-reload && sudo systemctl restart wms-russia.service"
