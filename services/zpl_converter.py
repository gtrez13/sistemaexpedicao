import os
import re
import requests
from typing import List, Optional


class ZPLConverterService:
    # ---------------------------
    # Helpers de parsing / seleção
    # ---------------------------

    @staticmethod
    def _split_labels(zpl: str) -> List[str]:
        """Captura blocos ^XA ... ^XZ (multi-linha)."""
        return re.findall(r"\^XA.*?\^XZ", zpl, flags=re.S)

    @staticmethod
    def _keep_only_first_and_last_label(zpl: str) -> str:
        """
        Mantém somente o primeiro e o último ^XA...^XZ
        (útil quando você só quer cortar labels intermediárias).
        """
        labels = ZPLConverterService._split_labels(zpl)
        if len(labels) <= 2:
            return zpl
        return labels[0] + "\n" + labels[-1]

    @staticmethod
    def _apply_y_offset(label_zpl: str, y_offset: int) -> str:
        """
        Desce o conteúdo via ^LT (Label Top).
        - Se já tiver ^LT<number>, substitui o primeiro.
        - Se não tiver, injeta logo após ^XA.
        """
        if y_offset == 0:
            return label_zpl

        if re.search(r"\^LT\d+", label_zpl):
            return re.sub(r"\^LT\d+", f"^LT{y_offset}", label_zpl, count=1)

        return re.sub(r"(\^XA)", rf"\1^LT{y_offset}", label_zpl, count=1)

    @staticmethod
    def _is_danfe(s: str) -> bool:
        s_up = s.upper()
        return ("DANFE" in s_up) or ("NFE:" in s_up) or ("PROTOCOLO" in s_up)

    @staticmethod
    def _is_etiqueta_ml(s: str) -> bool:
        s_up = s.upper()
        return ("^BQN" in s_up) or ("MELI" in s_up) or ("ENVIO" in s_up) or ("CEP" in s_up)

    @staticmethod
    def build_combo_2paginas(zpl_string: str) -> str:
        """
        Gera um ZPL com exatamente 2 páginas:
          - DANFE Simplificado
          - Etiqueta ML
        """
        if not zpl_string or "^XA" not in zpl_string:
            raise Exception("ZPL inválido")

        labels = ZPLConverterService._split_labels(zpl_string)
        if not labels:
            raise Exception("Não encontrei blocos ^XA...^XZ no ZPL")

        danfe = next((l for l in labels if ZPLConverterService._is_danfe(l)), None)
        etiqueta = next((l for l in labels if ZPLConverterService._is_etiqueta_ml(l)), None)

        # Fallbacks (bem compatível com o padrão do Bling)
        if not danfe or not etiqueta:
            if len(labels) >= 3:
                # muito comum: [0]=danfe, [1]=outra coisa, [2]=etiqueta
                danfe = danfe or labels[0]
                etiqueta = etiqueta or labels[2]
            elif len(labels) == 2:
                # se só tiver 2 labels, assume que já são as 2 páginas desejadas
                danfe = danfe or labels[0]
                etiqueta = etiqueta or labels[1]
            else:
                raise Exception("Não consegui identificar DANFE e Etiqueta no ZPL.")

        y_offset = int(os.getenv("ZPL_Y_OFFSET", "20"))
        danfe = ZPLConverterService._apply_y_offset(danfe, y_offset)
        etiqueta = ZPLConverterService._apply_y_offset(etiqueta, y_offset)

        return danfe + "\n" + etiqueta

    # ---------------------------
    # API principal
    # ---------------------------

    @staticmethod
    def normalize_zpl_for_pdf(zpl_string: str) -> str:
        """
        Normaliza o ZPL antes de converter:
        - Preferência: gerar combo de 2 páginas (DANFE + Etiqueta).
        - Se falhar, cai para "primeiro e último" (remove intermediários).
        """
        if not zpl_string or "^XA" not in zpl_string:
            raise Exception("ZPL inválido")

        try:
            return ZPLConverterService.build_combo_2paginas(zpl_string)
        except Exception:
            # fallback simples e seguro: remove intermediárias
            return ZPLConverterService._keep_only_first_and_last_label(zpl_string)

    @staticmethod
    def zpl_to_pdf(zpl_string: str) -> bytes:
        """
        Converte ZPL -> PDF usando Labelary.
        """
        zpl_string = ZPLConverterService.normalize_zpl_for_pdf(zpl_string)

        dpmm = int(os.getenv("LABELARY_DPmm", "8"))
        size = (os.getenv("LABELARY_SIZE", "4x6") or "4x6").strip()

        url = f"https://api.labelary.com/v1/printers/{dpmm}dpmm/labels/{size}/"
        headers = {"Accept": "application/pdf"}

        r = requests.post(url, data=zpl_string.encode("utf-8"), headers=headers, timeout=60)
        if r.status_code != 200:
            raise Exception(f"Labelary falhou: {r.status_code} - {r.text}")

        return r.content