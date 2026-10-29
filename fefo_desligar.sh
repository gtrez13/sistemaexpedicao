#!/usr/bin/env bash
# DESLIGA o FEFO por completo (kill switch). A bipagem volta a ser a de antes. Nao apaga dados.
set -uo pipefail
sudo rm -f /etc/systemd/system/wms-russia.service.d/fefo.conf
sudo systemctl daemon-reload && sudo systemctl restart wms-russia.service && sleep 3
systemctl is-active wms-russia.service && echo "FEFO desligado."
