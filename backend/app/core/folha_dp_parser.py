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


BENEFICIARIOS_DP_CONHECIDOS = [
    ("TICKET SERVICOS", "TICKET SERVICOS SA", "47866934000174", "21405 - Alimentação"),
    ("CONSORCIO SALVADOR", "CONSORCIO SALVADOR TRANSCARD", "22267284000110", "21404 - Vale Transporte"),
    ("SALVADOR TRANSCARD", "CONSORCIO SALVADOR TRANSCARD", "22267284000110", "21404 - Vale Transporte"),
    ("METROPASSE", "CONSORCIO METROPASSE", "31872549000108", "21404 - Vale Transporte"),
    ("SST MAIS", "SST MAIS SAUDE E SEGURANCA", "48618124000161", "21414 - Exames Clínicos (Dem/Adm/Per)"),
    ("BEM MAIS", "BEM MAIS GESTORA DE BENEFICIOS", "", "21417 - Sindicatos"),
    ("SINDECOLF", "BEM MAIS GESTORA / SINDECOLF", "", "21417 - Sindicatos"),
    ("CAIXA ECONOMICA", "CAIXA ECONOMICA FEDERAL (FGTS)", "00360305000104", "21406 - FGTS"),
]


def parse_boleto_dp_pdf(pdf_bytes: bytes, filename: str = "") -> dict[str, Any] | None:
    """Extrai dados de boleto de benefício/DP (Ticket, Salvadorcard, Metropasse, SST Mais, Bem Mais)."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    texto = "\n".join((p.extract_text() or "") for p in reader.pages)
    texto_up = texto.upper()

    # 1. Linha digitável (47 dígitos boleto bancário ou 48 dígitos arrecadação)
    m_47 = re.search(r"(\d{5}[\.\s]?\d{5}\s+\d{5}[\.\s]?\d{6}\s+\d{5}[\.\s]?\d{6}\s+\d\s+\d{14})", texto)
    m_48 = re.search(r"(\d{11}[-\s]?\d\s+\d{11}[-\s]?\d\s+\d{11}[-\s]?\d\s+\d{11}[-\s]?\d)", texto)
    linha_dig = ""
    valor = 0.0
    if m_47:
        linha_dig = re.sub(r"\D", "", m_47.group(1))
        if len(linha_dig) == 47:
            valor = round(int(linha_dig[-10:]) / 100.0, 2)
    elif m_48:
        linha_dig = re.sub(r"\D", "", m_48.group(1))

    if valor <= 0:
        m_tot = re.search(r"(?:Total|Valor Cobrado|Valor do Documento|TOTAL DA FATURA)\s*[:\n\r\s]*(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*,\d{2})", texto, re.IGNORECASE)
        if m_tot:
            valor = _br_float(m_tot.group(1))
        else:
            vals = [_br_float(v) for v in re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", texto)]
            vals = [v for v in vals if 1.0 <= v < 500000.0]
            if vals:
                valor = max(vals)

    if valor <= 0 and not linha_dig:
        return None

    # 2. Favorecido, CNPJ e Categoria
    favorecido = ""
    cnpj_fav = ""
    categoria = "21418 - Extras"
    for chave, nome_std, cnpj_std, cat_std in BENEFICIARIOS_DP_CONHECIDOS:
        if chave in texto_up:
            favorecido = nome_std
            cnpj_fav = cnpj_std
            categoria = cat_std
            break
    if not favorecido:
        favorecido = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE).replace("_", " ").strip()[:80]

    # 3. Empresa pagadora (LALUA Filial vs SOLAR)
    cnpjs_texto = [re.sub(r"\D", "", c) for c in re.findall(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", texto)]
    filial_label, ccusto = "LALUA - Matriz", "Boah - Matriz"
    eh_solar = False
    cnpj_pagador = ""
    for c_t in cnpjs_texto:
        if c_t in FILIAL_POR_CNPJ:
            filial_label, ccusto = FILIAL_POR_CNPJ[c_t]
            cnpj_pagador = c_t
            eh_solar = c_t.startswith("26607445")
            break
    if not cnpj_pagador and ("SOLAR" in texto_up or "SOLAR" in filename.upper()):
        filial_label, ccusto = "SOLAR - Adm", "Solar - Geral"
        eh_solar = True
        cnpj_pagador = "26607445000128"

    # 4. Vencimento
    vencimento_iso = ""
    m_venc = re.search(r"(?:Vencimento|DATA DE VENCIMENTO)\s*[:\n\r\s]*(\d{2}/\d{2}/\d{4})", texto, re.IGNORECASE)
    if m_venc:
        d, m_, y = m_venc.group(1).split("/")
        vencimento_iso = f"{y}-{m_}-{d}"
    else:
        datas = re.findall(r"\b(\d{2})/(\d{2})/(20\d{2})\b", texto)
        if datas:
            d, m_, y = datas[-1]
            vencimento_iso = f"{y}-{m_}-{d}"

    # Se o próprio PDF do boleto/fatura tiver uma lista de beneficiários (Nome + Valor), extrai automaticamente
    colabs_extra: list[dict[str, Any]] = []
    for ln in texto.splitlines():
        ln_s = ln.strip()
        if not ln_s or any(k in ln_s.upper() for k in ("TOTAL", "SUBTOTAL", "CNPJ", "PAGINA", "EMISSAO", "BOLETO", "JUROS", "MULTA", "DESCONTO")):
            continue
        m_lin = re.match(r"^([A-ZÁÀÃÂÉÊÍÓÔÕÚÇ][A-ZÁÀÃÂÉÊÍÓÔÕÚÇa-záàãâéêíóôõúç\s\.]{7,55}?)\s+(?:(\d{3}\.?\d{3}\.?\d{3}-?\d{2})\s+)?(?:R\$\s*)?(\d{1,3}(?:\.\d{3})*,\d{2})\s*$", ln_s)
        if m_lin:
            v_item = _br_float(m_lin.group(3))
            if 10.0 <= v_item < valor + 0.05:
                colabs_extra.append({
                    "nome": re.sub(r"\s+", " ", m_lin.group(1)).strip().upper(),
                    "cpf": re.sub(r"\D", "", m_lin.group(2) or ""),
                    "proventos": v_item,
                    "descontos": 0.0,
                    "liquido": v_item,
                    "fgts": 0.0,
                })

    return {
        "arquivo": filename,
        "tipo_pdf": "BOLETO_DP",
        "filial": filial_label,
        "ccusto": ccusto,
        "eh_solar": eh_solar,
        "cnpj": cnpj_pagador,
        "favorecido": favorecido,
        "cnpj_favorecido": cnpj_fav,
        "categoria": categoria,
        "forma_pgto": "Boleto Itaú" if linha_dig.startswith("341") else "Boleto Outros Bancos",
        "linha_dig": linha_dig,
        "vencimento": vencimento_iso,
        "competencia": "",
        "colaboradores": colabs_extra,
        "total_liquido": valor,
        "total_fgts": 0.0,
    }


def parse_relatorio_beneficios_pdf(pdf_bytes: bytes, filename: str = "") -> dict[str, Any] | None:
    """
    Lê relatórios de apoio do DP (sem boleto/linha digitável) que trazem a lista de funcionários:
      - Ticket Serviços S.A. ('Relatório de Pedidos')
      - Consórcio Salvador / Metropasse ('VT Funcionário' / 'Pedido de Carga')
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    texto = "\n".join((p.extract_text() or "") for p in reader.pages)
    texto_up = texto.upper()

    eh_ticket = "TICKET SERVI" in texto_up and "RELATÓRIO DE PEDIDOS" in texto_up or ("COLABORADORES (" in texto_up and "TICKET" in texto_up)
    eh_vt = "VT FUNCION" in texto_up or "CARTÃO VT" in texto_up or "CARTAO VT" in texto_up
    if not (eh_ticket or eh_vt):
        return None

    cnpjs = [re.sub(r"\D", "", c) for c in re.findall(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}", texto)]
    cnpjs_pag = [c for c in cnpjs if c != "47866934000174"]
    cnpj_pagador = next((c for c in cnpjs_pag if c in FILIAL_POR_CNPJ), cnpjs_pag[0] if cnpjs_pag else "")
    eh_solar = cnpj_pagador.startswith("26607445") or "SOLAR" in texto_up or "SOLAR" in filename.upper()

    colaboradores: list[dict[str, Any]] = []
    if eh_ticket:
        for ln in texto.splitlines():
            m = re.match(
                r"^\s*([A-ZÁÀÃÂÉÊÍÓÔÕÚÇ][A-ZÁÀÃÂÉÊÍÓÔÕÚÇ\s\.\-]+?)\s+\d+\s+(\d{3}\.?\d{3}\.?\d{3}-?\d{2})\s+\d+\s+R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s*$",
                ln.strip(),
            )
            if m:
                v = _br_float(m.group(3))
                if v > 0:
                    colaboradores.append({
                        "nome": re.sub(r"\s+", " ", m.group(1)).strip().upper(),
                        "cpf": re.sub(r"\D", "", m.group(2)),
                        "proventos": v,
                        "descontos": 0.0,
                        "liquido": v,
                        "fgts": 0.0,
                    })
        m_ped = re.search(r"N[ºo°]\s*do\s*Pedido:\s*(\d+)", texto, re.IGNORECASE)
        m_tot = re.search(r"Valor total:\s*R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})", texto, re.IGNORECASE)
        tipo_serv = "TICKET"
    else:
        # Salvadorcard / Metropasse: blocos iniciam com nº de cartão (12-16 dígitos)
        linhas = [ln.strip() for ln in texto.splitlines() if ln.strip()]
        i = 0
        while i < len(linhas):
            if re.fullmatch(r"\d{10,18}", linhas[i]):
                # Próxima(s) linhas: matrícula, nome (pode quebrar), tipo utilização, valor
                bloco_vals: list[str] = []
                j = i + 1
                while j < len(linhas) and not re.fullmatch(r"\d{10,18}", linhas[j]) and not linhas[j].lower().startswith("total:"):
                    bloco_vals.append(linhas[j])
                    if re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}", linhas[j]):
                        break
                    j += 1
                if bloco_vals and re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{2}", bloco_vals[-1]):
                    v = _br_float(bloco_vals[-1])
                    # Extrai matrícula/CPF se houver e junta linhas de nome antes de 'VT'
                    cpf_b = ""
                    nome_partes: list[str] = []
                    for tok in bloco_vals[:-1]:
                        tok_up = tok.upper()
                        if re.fullmatch(r"[\d\.\-]{3,16}", tok) or re.fullmatch(r"T\s*\d+", tok_up) or re.fullmatch(r"\d{1,2}", tok):
                            digs = re.sub(r"\D", "", tok)
                            if len(digs) >= 9:
                                cpf_b = (cpf_b + digs)[-11:]
                            continue
                        if tok_up in ("VT", "FUNCION", "ÁRIO", "ARIO", "FUNCIONÁRIO", "FUNCIONARIO", "VT FUNCIONÁRIO", "VT FUNCIONARIO"):
                            continue
                        nome_partes.append(tok)
                    nome_b = re.sub(r"\s+", " ", " ".join(nome_partes)).strip().upper()
                    if nome_b and v > 0:
                        colaboradores.append({
                            "nome": nome_b,
                            "cpf": cpf_b if len(cpf_b) == 11 else "",
                            "proventos": v,
                            "descontos": 0.0,
                            "liquido": v,
                            "fgts": 0.0,
                        })
                i = j + 1
            else:
                i += 1
        m_ped = re.search(r"Pedido\s*[:\n\r\s]+(\d+)", texto, re.IGNORECASE)
        m_tot = re.search(r"Valor\s*[:\n\r\s]+(\d{1,3}(?:\.\d{3})*,\d{2})", texto, re.IGNORECASE)
        tipo_serv = "METROPASSE" if "METROPASSE" in texto_up or "METROPASSE" in filename.upper() else "SALVADORCARD"

    if not colaboradores:
        return None

    return {
        "arquivo": filename,
        "tipo_serv": tipo_serv,
        "eh_solar": eh_solar,
        "pedido": m_ped.group(1) if m_ped else "",
        "total": _br_float(m_tot.group(1)) if m_tot else round(sum(c["liquido"] for c in colaboradores), 2),
        "colaboradores": colaboradores,
    }


def parse_pacote_folha_dp(raw_bytes: bytes, filename: str) -> list[dict[str, Any]]:
    """
    Recebe um .ZIP (com PDFs de Folha OU Boletos + Relatórios de Transporte/Alimentação) ou um único .PDF.
    Associa automaticamente relatórios de beneficiários aos respectivos boletos (sem duplicar lançamentos).
    """
    extratos: dict[str, dict[str, Any]] = {}
    liquidos_fb: dict[str, dict[str, Any]] = {}
    boletos_dp: list[dict[str, Any]] = []
    relatorios_apoio: list[dict[str, Any]] = []

    def _processar_pdf(b: bytes, fname: str):
        low = fname.lower()
        if "extrato" in low:
            res = parse_extrato_mensal_pdf(b, fname)
            if res["colaboradores"]:
                extratos[res["filial"]] = res
                return
        if "liquido" in low or "líquido" in low:
            res = parse_relatorio_liquidos_pdf(b, fname)
            if res["colaboradores"]:
                liquidos_fb[res["filial"]] = res
                return
        # Tenta Extrato / Líquidos primeiro
        res = parse_extrato_mensal_pdf(b, fname)
        if res["colaboradores"]:
            extratos[res["filial"]] = res
            return
        res2 = parse_relatorio_liquidos_pdf(b, fname)
        if res2["colaboradores"]:
            liquidos_fb[res2["filial"]] = res2
            return
        # Verifica se é relatório de apoio (Ticket / Salvadorcard / Metropasse) antes de tratar como boleto
        rel = parse_relatorio_beneficios_pdf(b, fname)
        if rel:
            relatorios_apoio.append(rel)
            return
        bol = parse_boleto_dp_pdf(b, fname)
        if bol:
            boletos_dp.append(bol)

    if filename.lower().endswith(".zip") or raw_bytes[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for nome in sorted(zf.namelist()):
                if nome.lower().endswith(".pdf") and not nome.startswith("__MACOSX"):
                    _processar_pdf(zf.read(nome), nome)
    else:
        _processar_pdf(raw_bytes, filename)

    # Casa cada relatório de apoio (lista de beneficiários) ao boleto correspondente
    for rel in relatorios_apoio:
        melhor_bol = None
        for bol in boletos_dp:
            if bol["eh_solar"] != rel["eh_solar"]:
                continue
            fav_up = (bol.get("favorecido") or "").upper()
            mesmo_serv = (
                (rel["tipo_serv"] == "TICKET" and "TICKET" in fav_up)
                or (rel["tipo_serv"] == "METROPASSE" and "METROPASSE" in fav_up)
                or (rel["tipo_serv"] == "SALVADORCARD" and ("SALVADOR" in fav_up or "TRANSCARD" in fav_up))
            )
            if not mesmo_serv:
                continue
            if rel["pedido"] and rel["pedido"] in bol["arquivo"]:
                melhor_bol = bol
                break
            if abs(bol["total_liquido"] - rel["total"]) < 0.10:
                melhor_bol = bol
                break
            if melhor_bol is None:
                melhor_bol = bol
        if melhor_bol is not None and not melhor_bol["colaboradores"]:
            melhor_bol["colaboradores"] = rel["colaboradores"]

    resultado = list(extratos.values())
    for fil, res in liquidos_fb.items():
        if fil not in extratos:
            resultado.append(res)
    resultado.extend(boletos_dp)

    resultado.sort(key=lambda x: (1 if x["eh_solar"] else 0, x["tipo_pdf"], x["filial"]))
    return resultado
