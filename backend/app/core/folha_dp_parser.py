"""
Parser de Folha de Pagamento / Despesas com Pessoal (Domínio Sistemas / DMF)
Lê arquivos .ZIP ou .PDF individuais:
  - Extrato Mensal_*.pdf (Paseo, Barra, Vilas, Horto, MT, SDB, Solar)
  - Relatório de Líquidos_*.pdf (fallback quando não houver Extrato Mensal para a filial)

Extrai por filial/empresa:
  - Competência (MM/AAAA)
  - CNPJ e Razão Social
  - Lista de colaboradores: nome, cpf, liquido, proventos, descontos, fgts
  - Total líquido da filial/empresa
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from pypdf import PdfReader


FILIAL_POR_CNPJ = {
    "10436619000105": ("LALUA - Matriz", "Boah - Matriz"),
    "10436619000288": ("LALUA - Barra", "Boah - Barra"),
    "10436619000369": ("LALUA - Paseo", "Boah - Paseo"),
    "10436619000440": ("LALUA - Horto", "Boah - Horto"),
    "10436619000520": ("LALUA - SDB", "Boah - SDB"),
    "10436619000601": ("LALUA - Vilas", "Boah - Vilas"),
    "26607445000128": ("SOLAR - Adm", "Solar - Geral"),
}


def _br_float(s: str) -> float:
    try:
        return round(float(str(s).replace(".", "").replace(",", ".").strip()), 2)
    except Exception:
        return 0.0


def _inferir_filial_nome_arquivo(filename: str, cnpj: str) -> tuple[str, str]:
    c_limpo = re.sub(r"\D", "", cnpj or "")
    if c_limpo in FILIAL_POR_CNPJ:
        return FILIAL_POR_CNPJ[c_limpo]
    low = (filename or "").lower()
    if "solar" in low or c_limpo.startswith("26607445"):
        return "SOLAR - Adm", "Solar - Geral"
    if "paseo" in low:
        return "LALUA - Paseo", "Boah - Paseo"
    if "barra" in low:
        return "LALUA - Barra", "Boah - Barra"
    if "vilas" in low:
        return "LALUA - Vilas", "Boah - Vilas"
    if "horto" in low:
        return "LALUA - Horto", "Boah - Horto"
    if "sdb" in low:
        return "LALUA - SDB", "Boah - SDB"
    if "mt" in low or "matriz" in low:
        return "LALUA - Matriz", "Boah - Matriz"
    return "LALUA - Geral", "Geral"


def parse_extrato_mensal_pdf(pdf_bytes: bytes, filename: str = "") -> dict[str, Any]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    texto_full = "\n".join((p.extract_text() or "") for p in reader.pages)

    m_comp = re.search(r"Todos\s*\n\s*(\d{2}/\d{4})", texto_full) or re.search(r"Compet[êe]ncia:\s*(\d{2}/\d{4})", texto_full) or re.search(r"(?<!\d/)(\d{2}/20\d{2})", texto_full)
    competencia = m_comp.group(1) if m_comp else ""

    m_cnpj = re.search(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", texto_full)
    cnpj = m_cnpj.group(1) if m_cnpj else ""

    filial_label, ccusto = _inferir_filial_nome_arquivo(filename, cnpj)
    eh_solar = "solar" in filial_label.lower() or re.sub(r"\D", "", cnpj).startswith("26607445")

    # Cada colaborador no Extrato Mensal começa com:
    # "<codigo> <NOME COMPLETO>Empr.: <data>Adm:<CPF>" (ou Contribuinte)
    pat_inicio = re.compile(
        r"(?:^|\n)\s*(\d{1,5})\s+([A-ZÁÀÃÂÉÊÍÓÔÕÚÇ][A-ZÁÀÃÂÉÊÍÓÔÕÚÇ\s\.\-]+?)Empr\.:\s*\d{2}/\d{2}/\d{4}Adm:(\d{3}\.\d{3}\.\d{3}-\d{2})",
        re.MULTILINE,
    )
    matches = list(pat_inicio.finditer(texto_full))
    colaboradores: list[dict[str, Any]] = []
    vistos_cpf: set[str] = set()

    for idx, m in enumerate(matches):
        ini = m.start()
        fim = matches[idx + 1].start() if idx + 1 < len(matches) else len(texto_full)
        bloco = texto_full[ini:fim]

        nome = re.sub(r"\s+", " ", m.group(2)).strip()
        cpf = re.sub(r"\D", "", m.group(3))

        # Linha ND: 0 Proventos: 2.343,00 Líquido:Descontos: 794,36 Informativa: 528,24 Informativa Dedutora: 0 1.548,64
        m_liq = re.search(
            r"Proventos:\s*([\d\.,]+).*?Descontos:\s*([\d\.,]+).*?Informativa Dedutora:\s*[\d\.,]+\s+([\d\.,]+)",
            bloco,
            re.DOTALL,
        )
        if not m_liq:
            # Fallback: último número monetário na linha ND:
            m_nd = re.search(r"ND:[^\n]+", bloco)
            if m_nd:
                vals = re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", m_nd.group(0))
                liquido = _br_float(vals[-1]) if vals else 0.0
                proventos = _br_float(vals[0]) if vals else 0.0
                descontos = _br_float(vals[1]) if len(vals) > 1 else 0.0
            else:
                continue
        else:
            proventos = _br_float(m_liq.group(1))
            descontos = _br_float(m_liq.group(2))
            liquido = _br_float(m_liq.group(3))

        m_fgts = re.search(r"Valor FGTS:\s*([\d\.,]+)", bloco)
        fgts = _br_float(m_fgts.group(1)) if m_fgts else 0.0

        if liquido <= 0:
            continue
        if cpf in vistos_cpf:
            continue
        vistos_cpf.add(cpf)

        colaboradores.append({
            "nome": nome,
            "cpf": cpf,
            "proventos": proventos,
            "descontos": descontos,
            "liquido": liquido,
            "fgts": fgts,
        })

    # Também tenta capturar Contribuintes (pró-labore / autônomos) se presentes no bloco final
    total_liq = round(sum(c["liquido"] for c in colaboradores), 2)
    total_fgts = round(sum(c["fgts"] for c in colaboradores), 2)

    return {
        "arquivo": filename,
        "tipo_pdf": "EXTRATO_MENSAL",
        "filial": filial_label,
        "ccusto": ccusto,
        "eh_solar": eh_solar,
        "cnpj": cnpj,
        "competencia": competencia,
        "colaboradores": colaboradores,
        "total_liquido": total_liq,
        "total_fgts": total_fgts,
    }


def parse_relatorio_liquidos_pdf(pdf_bytes: bytes, filename: str = "") -> dict[str, Any]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    texto_full = "\n".join((p.extract_text() or "") for p in reader.pages)

    m_comp = re.search(r"Compet[êe]ncia:\s*(\d{2}/\d{4})", texto_full)
    competencia = m_comp.group(1) if m_comp else ""
    m_cnpj = re.search(r"CNPJ:\s*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", texto_full)
    cnpj = m_cnpj.group(1) if m_cnpj else ""

    filial_label, ccusto = _inferir_filial_nome_arquivo(filename, cnpj)
    eh_solar = "solar" in filial_label.lower()

    colaboradores: list[dict[str, Any]] = []
    for ln in texto_full.splitlines():
        m = re.match(r"^\s*\d{1,4}\s+([A-ZÁÀÃÂÉÊÍÓÔÕÚÇ][A-ZÁÀÃÂÉÊÍÓÔÕÚÇ\s\.\-]+?)\s+(\d{1,3}(?:\.\d{3})*,\d{2})", ln.strip())
        if m:
            nome = re.sub(r"\s+", " ", m.group(1)).strip()
            liq = _br_float(m.group(2))
            if liq > 0:
                colaboradores.append({
                    "nome": nome,
                    "cpf": "",
                    "proventos": liq,
                    "descontos": 0.0,
                    "liquido": liq,
                    "fgts": 0.0,
                })

    return {
        "arquivo": filename,
        "tipo_pdf": "RELATORIO_LIQUIDOS",
        "filial": filial_label,
        "ccusto": ccusto,
        "eh_solar": eh_solar,
        "cnpj": cnpj,
        "competencia": competencia,
        "colaboradores": colaboradores,
        "total_liquido": round(sum(c["liquido"] for c in colaboradores), 2),
        "total_fgts": 0.0,
    }


def parse_pacote_folha_dp(raw_bytes: bytes, filename: str) -> list[dict[str, Any]]:
    """
    Recebe um .ZIP (com vários PDFs do DP) ou um único .PDF.
    Prioriza 'Extrato Mensal' por filial (tem CPF + Líquido + FGTS) e usa 'Relatório de Líquidos'
    apenas se a filial não tiver Extrato Mensal.
    """
    extratos: dict[str, dict[str, Any]] = {}
    liquidos_fb: dict[str, dict[str, Any]] = {}

    def _processar_pdf(b: bytes, fname: str):
        low = fname.lower()
        if "extrato" in low:
            res = parse_extrato_mensal_pdf(b, fname)
            if res["colaboradores"]:
                extratos[res["filial"]] = res
        elif "liquido" in low or "líquido" in low:
            res = parse_relatorio_liquidos_pdf(b, fname)
            if res["colaboradores"]:
                liquidos_fb[res["filial"]] = res
        else:
            res = parse_extrato_mensal_pdf(b, fname)
            if res["colaboradores"]:
                extratos[res["filial"]] = res
            else:
                res2 = parse_relatorio_liquidos_pdf(b, fname)
                if res2["colaboradores"]:
                    liquidos_fb[res2["filial"]] = res2

    if filename.lower().endswith(".zip") or raw_bytes[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for nome in sorted(zf.namelist()):
                if nome.lower().endswith(".pdf") and not nome.startswith("__MACOSX"):
                    _processar_pdf(zf.read(nome), nome)
    else:
        _processar_pdf(raw_bytes, filename)

    resultado = list(extratos.values())
    for fil, res in liquidos_fb.items():
        if fil not in extratos:
            resultado.append(res)

    resultado.sort(key=lambda x: (1 if x["eh_solar"] else 0, x["filial"]))
    return resultado
