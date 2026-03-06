from services.sku_alias_service import SkuAliasService
from db.database import get_db

class SKUResolver:

    def resolve(self, codigo_lido: str) -> list[str]:
        """
        Retorna todos SKUs possíveis que o código pode representar
        """

        codigo = (codigo_lido or "").strip().upper()
        if not codigo:
            return []

        candidatos = set()

        # 1 — direto
        candidatos.add(codigo)

        # 2 — alias manual
        alias = SkuAliasService().resolve(codigo)
        if alias:
            candidatos.add(alias.upper())

        db = get_db()

        # 3 — código de barras salvo no nome (muito comum)
        rows = db.execute("""
            SELECT sku FROM produtos
            WHERE sku LIKE ?
        """, (f"%{codigo}%",)).fetchall()

        for r in rows:
            candidatos.add(r["sku"].upper())

        db.close()

        return list(candidatos)