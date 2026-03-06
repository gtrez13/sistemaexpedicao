# services/serial_store.py
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional


class SerialStore:
    """
    Guarda seriais por pedido (ml_id) em arquivo JSON.
    Estrutura:
      {
        "2000015...": {
           "SKU1": ["S1","S2"],
           "SKU2": ["X1"]
        }
      }
    """

    def __init__(self, filename: str = "serial_store.json"):
        root = os.path.dirname(os.path.dirname(__file__))  # raiz do projeto
        self.path = os.path.join(root, filename)

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f) or {}

    def _save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, ml_id: str) -> Dict[str, List[str]]:
        data = self._load()
        return data.get(str(ml_id), {}) or {}

    def set(self, ml_id: str, serials_by_sku: Dict[str, List[str]]) -> None:
        data = self._load()
        data[str(ml_id)] = serials_by_sku
        self._save(data)

    def clear(self, ml_id: str) -> None:
        data = self._load()
        if str(ml_id) in data:
            del data[str(ml_id)]
            self._save(data)