from __future__ import annotations
from typing import Optional
from db.database import get_db


class SkuAliasService:
    """
    Resolve aliases de SKU:
    ML -> SKU real do Bling
    """

    def resolve(self, sku_or_ml_item_id: str, max_hops: int = 5) -> str:
        cur_val = (sku_or_ml_item_id or "").strip()
        if not cur_val:
            return ""

        seen = set()
        db = get_db()
        cur = db.cursor()

        hops = 0
        while hops < max_hops:

            # evita loop infinito
            if cur_val in seen:
                break
            seen.add(cur_val)

            row = cur.execute(
                "SELECT sku_bling FROM sku_alias WHERE ml_item_id=?",
                (cur_val,),
            ).fetchone()

            if not row:
                break

            nxt = str(row["sku_bling"]).strip()

            if not nxt or nxt == cur_val:
                break

            cur_val = nxt
            hops += 1

        return cur_val