"""
Gerador Universal de Arquivo de Remessa CNAB 240 (Multi-Banco FEBRABAN)
Suporta qualquer banco brasileiro compatível com FEBRABAN CNAB 240:
  - 341 Itaú Unibanco (SISPAG)
  - 001 Banco do Brasil
  - 237 Bradesco
  - 033 Santander
  - 104 Caixa Econômica Federal
  - 756 Sicoob
  - 748 Sicredi
  - 077 Banco Inter
  - 208 BTG Pactual
  - 422 Banco Safra
  - 041 Banrisul
  - Qualquer outro código COMPE (3 dígitos) via padrão FEBRABAN 240

Modalidades / Lotes suportados automaticamente:
  1. Boletos Bancários do mesmo banco (Forma 30 — Segmentos J + J-52)
  2. Boletos Bancários de outros bancos (Forma 31 — Segmentos J + J-52)
  3. Concessionárias e Tributos com código de barras 48 dígitos (Forma 11 — Segmento O)
  4. PIX Transferência via Chave Pix (Forma 45 — Segmentos A + B)
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from .validators import limpar_documento, validar_cnpj


BANCOS_CNAB240: dict[str, dict[str, str]] = {
    "341": {
        "codigo": "341",
        "nome": "BANCO ITAU SA",
        "label": "341 - Itaú Unibanco (SISPAG)",
        "sigla": "ITAU",
        "versao_arq": "080",
        "versao_lote_j": "040",
        "versao_lote_o": "030",
        "versao_lote_a": "040",
    },
    "001": {
        "codigo": "001",
        "nome": "BANCO DO BRASIL SA",
        "label": "001 - Banco do Brasil",
        "sigla": "BB",
        "versao_arq": "103",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
    "237": {
        "codigo": "237",
        "nome": "BANCO BRADESCO SA",
        "label": "237 - Bradesco",
        "sigla": "BRADESCO",
        "versao_arq": "089",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
    "033": {
        "codigo": "033",
        "nome": "BANCO SANTANDER BRASIL SA",
        "label": "033 - Santander",
        "sigla": "SANTANDER",
        "versao_arq": "089",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
    "104": {
        "codigo": "104",
        "nome": "CAIXA ECONOMICA FEDERAL",
        "label": "104 - Caixa Econômica Federal",
        "sigla": "CAIXA",
        "versao_arq": "080",
        "versao_lote_j": "030",
        "versao_lote_o": "012",
        "versao_lote_a": "041",
    },
    "756": {
        "codigo": "756",
        "nome": "BANCO COOPERATIVO SICOOB SA",
        "label": "756 - Sicoob",
        "sigla": "SICOOB",
        "versao_arq": "087",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
    "748": {
        "codigo": "748",
        "nome": "BANCO COOPERATIVO SICREDI SA",
        "label": "748 - Sicredi",
        "sigla": "SICREDI",
        "versao_arq": "087",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
    "077": {
        "codigo": "077",
        "nome": "BANCO INTER SA",
        "label": "077 - Banco Inter",
        "sigla": "INTER",
        "versao_arq": "103",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
    "208": {
        "codigo": "208",
        "nome": "BANCO BTG PACTUAL SA",
        "label": "208 - BTG Pactual",
        "sigla": "BTG",
        "versao_arq": "103",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
    "422": {
        "codigo": "422",
        "nome": "BANCO SAFRA SA",
        "label": "422 - Banco Safra",
        "sigla": "SAFRA",
        "versao_arq": "103",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
    "041": {
        "codigo": "041",
        "nome": "BANCO DO ESTADO DO RS SA",
        "label": "041 - Banrisul",
        "sigla": "BANRISUL",
        "versao_arq": "103",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    },
}


def obter_perfil_banco(banco_codigo: str, banco_nome: Optional[str] = None) -> dict[str, str]:
    cod = re.sub(r"\D", "", str(banco_codigo or "341")).zfill(3)[-3:]
    if cod in BANCOS_CNAB240:
        perfil = dict(BANCOS_CNAB240[cod])
        if banco_nome:
            perfil["nome"] = banco_nome.strip().upper()[:30]
        return perfil
    nome_clean = (banco_nome or f"BANCO {cod}").strip().upper()[:30]
    return {
        "codigo": cod,
        "nome": nome_clean,
        "label": f"{cod} - {nome_clean}",
        "sigla": f"B{cod}",
        "versao_arq": "103",
        "versao_lote_j": "040",
        "versao_lote_o": "012",
        "versao_lote_a": "045",
    }


def formatar_campo(texto, tamanho: int, tipo: str = "X") -> str:
    if texto is None:
        texto = ""
    texto_str = str(texto)
    if tipo == "9":
        limpo = re.sub(r"\D", "", texto_str)
        return limpo.zfill(tamanho)[-tamanho:]
    nfkd = unicodedata.normalize("NFKD", texto_str)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    sem_acento = re.sub(r"[^\x20-\x7E]", " ", sem_acento)
    return sem_acento.upper().ljust(tamanho)[:tamanho]


def linha_digitavel_para_codigo_barras(linha: Optional[str]) -> tuple[str, str]:
    """
    Retorna (codigo_barras_44, tipo_barras):
      - tipo_barras = 'BOLETO' (título bancário 47 -> 44 ou 44 não iniciado por 8)
      - tipo_barras = 'ARRECADACAO' (concessionária/tributo 48 -> 44 ou 44 iniciado por 8)
      - ('', '') se inválido
    """
    if not linha:
        return "", ""
    limpa = re.sub(r"\D", "", str(linha))
    if len(limpa) == 47:
        banco_moeda = limpa[0:4]
        dv_geral = limpa[32:33]
        fator_valor = limpa[33:47]
        campo1 = limpa[4:9]
        campo2 = limpa[10:20]
        campo3 = limpa[21:31]
        barras = f"{banco_moeda}{dv_geral}{fator_valor}{campo1}{campo2}{campo3}"
        return barras, "BOLETO"
    if len(limpa) == 48:
        barras = limpa[0:11] + limpa[12:23] + limpa[24:35] + limpa[36:47]
        return barras, "ARRECADACAO"
    if len(limpa) == 44:
        return barras_tipo(limpa)
    return "", ""


def barras_tipo(barras: str) -> tuple[str, str]:
    if len(barras) != 44 or not barras.isdigit():
        return "", ""
    return barras, ("ARRECADACAO" if barras.startswith("8") else "BOLETO")


def detectar_tipo_chave_pix(chave: Optional[str]) -> tuple[str, str]:
    """
    Identifica o código FEBRABAN da chave Pix (Segmento B - Forma 45):
      '01' = Telefone (+55...)
      '02' = E-mail
      '03' = CPF ou CNPJ
      '04' = Chave Aleatória (EVP)
    Retorna (codigo_tipo, chave_normalizada) ou ('', '').
    """
    if not chave:
        return "", ""
    raw = str(chave).strip()
    if not raw:
        return "", ""
    if "@" in raw and "." in raw:
        return "02", raw.lower()[:77]
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw):
        return "04", raw.lower()
    digs = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "01", f"+{digs}"[:14]
    if len(digs) in (11, 14):
        # Heurística: celular começa com DDD + 9 e tem 11 dígitos com formato de telefone
        if len(digs) == 11 and ("(" in raw or "-" in raw and raw.count("-") == 1 and "." not in raw):
            return "01", f"+55{digs}"
        return "03", digs
    if len(digs) in (12, 13) and digs.startswith("55"):
        return "01", f"+{digs}"
    return "", ""


def _para_centavos(valor) -> int:
    dec = Decimal(str(valor or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if dec < Decimal("0.00"):
        raise ValueError("Valor de título inválido (negativo) para remessa CNAB 240.")
    return int((dec * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _data_ddmmaaaa(raw: Optional[str], fallback: str) -> str:
    if not raw:
        return fallback
    s = str(raw).strip()
    if "-" in s and len(s) >= 10:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d%m%Y")
        except Exception:
            pass
    if "/" in s:
        p = s[:10].split("/")
        if len(p) == 3:
            return f"{p[0].zfill(2)}{p[1].zfill(2)}{p[2].zfill(4)}"
    digs = re.sub(r"\D", "", s)
    if len(digs) == 8:
        return digs
    return fallback


def _anexar_linha(linhas: list[str], linha: str, rotulo: str) -> None:
    if len(linha) != 240:
        raise ValueError(f"Linha CNAB 240 inválida ({rotulo}): esperado 240 caracteres, obtido {len(linha)}.")
    linhas.append(linha)


def classificar_titulos_cnab(
    titulos: list[dict],
    banco_pagador: str = "341",
) -> tuple[list[dict], list[dict]]:
    """
    Classifica títulos em aptos (validos) e rejeitados (com motivo claro).
    Cada item válido recebe:
      _tipo_lote: 'BOLETO_MESMO_BANCO' | 'BOLETO_OUTRO_BANCO' | 'ARRECADACAO' | 'PIX_CHAVE'
      _codigo_barras_44 / _pix_tipo / _pix_chave
    """
    cod_pagador = re.sub(r"\D", "", str(banco_pagador or "341")).zfill(3)[-3:]
    validos: list[dict] = []
    rejeitados: list[dict] = []

    for tit in titulos:
        valor_raw = tit.get("valor_liquido") if tit.get("valor_liquido") is not None else tit.get("valor_bruto")
        try:
            centavos = _para_centavos(valor_raw)
        except Exception:
            centavos = 0

        if centavos <= 0:
            rejeitados.append({
                "id": tit.get("id"),
                "numero_doc": tit.get("numero_nf") or tit.get("numero_doc") or "-",
                "fornecedor": tit.get("fornecedor") or "-",
                "valor": float(valor_raw or 0),
                "forma_pgto": tit.get("forma_pgto") or "",
                "motivo": "Valor líquido zerado ou inválido",
            })
            continue

        linha_dig = tit.get("linha_dig") or tit.get("cod_barras") or ""
        barras, tipo_barras = linha_digitavel_para_codigo_barras(linha_dig)
        forma = str(tit.get("forma_pgto") or "").upper()
        pix_raw = tit.get("pix_chave") or ""
        pix_tipo, pix_chave = detectar_tipo_chave_pix(pix_raw)

        item = dict(tit)
        item["_valor_centavos"] = centavos

        if tipo_barras == "ARRECADACAO":
            item["_tipo_lote"] = "ARRECADACAO"
            item["_codigo_barras_44"] = barras
            item["_identificador_pgto"] = f"Guia/Concessionária ({barras[:11]}...)"
            validos.append(item)
        elif tipo_barras == "BOLETO":
            banco_boleto = barras[:3]
            item["_tipo_lote"] = "BOLETO_MESMO_BANCO" if banco_boleto == cod_pagador else "BOLETO_OUTRO_BANCO"
            item["_codigo_barras_44"] = barras
            item["_identificador_pgto"] = f"Boleto Banco {banco_boleto}"
            validos.append(item)
        elif pix_tipo and (forma in ("PIX", "", "TED", "TRANSFERENCIA", "TRANSFERÊNCIA") or not linha_dig):
            item["_tipo_lote"] = "PIX_CHAVE"
            item["_pix_tipo"] = pix_tipo
            item["_pix_chave"] = pix_chave
            item["_identificador_pgto"] = f"PIX ({pix_chave[:28]})"
            validos.append(item)
        else:
            if "PIX" in forma:
                motivo = "Falta Chave PIX válida no título ou no cadastro do fornecedor"
            elif forma in ("DARF", "DAS", "DAM", "DARE", "GNRE", "FGTS", "GPS", "GUIA"):
                motivo = "Falta código de barras da guia (48 dígitos)"
            else:
                motivo = "Falta código de barras/linha digitável (47 ou 48 dígitos) ou Chave PIX"
            rejeitados.append({
                "id": tit.get("id"),
                "numero_doc": tit.get("numero_nf") or tit.get("numero_doc") or "-",
                "fornecedor": tit.get("fornecedor") or "-",
                "valor": centavos / 100.0,
                "forma_pgto": forma or "N/D",
                "motivo": motivo,
            })

    return validos, rejeitados


def _montar_header_lote(
    cod_banco: str,
    num_lote: int,
    tipo_servico: str,
    forma_lancamento: str,
    versao_lote: str,
    cnpj_limpo: str,
    convenio: str,
    ag_limpa: str,
    cc_limpa: str,
    dac_limpo: str,
    razao_limpa: str,
    cidade_limpa: str,
    cep_limpo: str,
    uf_limpa: str,
) -> str:
    return (
        formatar_campo(cod_banco, 3, "9")         # 001-003 Banco
        + formatar_campo(num_lote, 4, "9")        # 004-007 Lote
        + "1"                                     # 008     Tipo Registro (Header Lote)
        + "C"                                     # 009     Operação (Crédito/Pagamento)
        + formatar_campo(tipo_servico, 2, "9")    # 010-011 Tipo Serviço (20=Fornecedor, 22=Tributos)
        + formatar_campo(forma_lancamento, 2, "9")# 012-013 Forma (30/31=Boleto, 11=Concess., 45=PIX)
        + formatar_campo(versao_lote, 3, "9")     # 014-016 Versão layout lote
        + " "                                     # 017     Branco
        + "2"                                     # 018     Tipo Inscrição (2=CNPJ)
        + formatar_campo(cnpj_limpo, 14, "9")     # 019-032 CNPJ Empresa
        + formatar_campo(convenio, 20, "X")       # 033-052 Convênio no banco
        + formatar_campo(ag_limpa, 5, "9")        # 053-057 Agência
        + " "                                     # 058     DV Agência
        + formatar_campo(cc_limpa, 12, "9")       # 059-070 Conta
        + formatar_campo(dac_limpo, 1, "X")       # 071     DV Conta
        + " "                                     # 072     DV Ag/Conta
        + formatar_campo(razao_limpa, 30, "X")    # 073-102 Nome Empresa
        + formatar_campo("", 40, "X")             # 103-142 Mensagem / Finalidade
        + formatar_campo("", 30, "X")             # 143-172 Logradouro
        + formatar_campo("0", 5, "9")             # 173-177 Número
        + formatar_campo("", 15, "X")             # 178-192 Complemento
        + formatar_campo(cidade_limpa, 20, "X")   # 193-212 Cidade
        + formatar_campo(cep_limpo, 8, "9")       # 213-220 CEP
        + formatar_campo(uf_limpa, 2, "X")        # 221-222 UF
        + (" " * 18)                              # 223-240 Ocorrências / Uso FEBRABAN
    )


def _montar_trailler_lote(cod_banco: str, num_lote: int, qtd_registros_lote: int, total_centavos: int) -> str:
    return (
        formatar_campo(cod_banco, 3, "9")
        + formatar_campo(num_lote, 4, "9")
        + "5"
        + (" " * 9)
        + formatar_campo(qtd_registros_lote, 6, "9")
        + formatar_campo(total_centavos, 18, "9")
        + ("0" * 18)
        + (" " * 181)
    )


def gerar_cnab240_multibanco(
    titulos: list[dict],
    banco_codigo: str = "341",
    banco_nome: Optional[str] = None,
    cnpj_empresa: str = "",
    razao_social: str = "",
    agencia: str = "",
    conta: str = "",
    dac: str = "0",
    convenio: str = "",
    data_pagamento: Optional[str] = None,
    cidade: str = "SALVADOR",
    cep: str = "40000000",
    uf: str = "BA",
    sequencial_arquivo: int = 1,
) -> tuple[str, dict]:
    """
    Gera arquivo de remessa CNAB 240 FEBRABAN multi-banco e multi-lote.
    Retorna (conteudo_rem, resumo_dict).
    """
    perfil = obter_perfil_banco(banco_codigo, banco_nome)
    cod_banco = perfil["codigo"]
    cnpj_limpo = limpar_documento(cnpj_empresa)
    razao_limpa = (razao_social or "").strip()
    ag_limpa = re.sub(r"\D", "", agencia or "")
    cc_limpa = re.sub(r"\D", "", conta or "")
    dac_limpo = (dac or "0").strip()[:1] or "0"
    conv_limpo = (convenio or "").strip()[:20]
    cidade_limpa = (cidade or "SALVADOR").strip() or "SALVADOR"
    cep_limpo = re.sub(r"\D", "", cep or "40000000") or "40000000"
    uf_limpa = (uf or "BA").strip()[:2] or "BA"

    if not validar_cnpj(cnpj_limpo):
        raise ValueError("CNPJ da empresa pagadora não configurado ou inválido para remessa CNAB 240.")
    if not razao_limpa:
        raise ValueError("Razão Social da empresa pagadora não configurada para remessa CNAB 240.")
    if not ag_limpa or ag_limpa == "0" * len(ag_limpa):
        raise ValueError("Agência bancária não configurada para remessa CNAB 240.")
    if not cc_limpa or cc_limpa == "0" * len(cc_limpa):
        raise ValueError("Conta corrente não configurada para remessa CNAB 240.")

    validos, rejeitados = classificar_titulos_cnab(titulos, banco_pagador=cod_banco)
    if not validos:
        raise ValueError("Nenhum título apto (com código de barras válido ou Chave PIX) foi encontrado.")

    linhas: list[str] = []
    hoje = datetime.now()
    data_geracao = hoje.strftime("%d%m%Y")
    hora_geracao = hoje.strftime("%H%M%S")
    dt_pgto_str = _data_ddmmaaaa(data_pagamento, data_geracao)

    # 1. HEADER DE ARQUIVO (Registro 0)
    header_arq = (
        formatar_campo(cod_banco, 3, "9")             # 001-003 Código do Banco
        + "0000"                                      # 004-007 Lote de Serviço
        + "0"                                         # 008     Tipo de Registro
        + (" " * 9)                                   # 009-017 Uso exclusivo FEBRABAN
        + "2"                                         # 018     Tipo de Inscrição (2=CNPJ)
        + formatar_campo(cnpj_limpo, 14, "9")         # 019-032 CNPJ Empresa
        + formatar_campo(conv_limpo, 20, "X")         # 033-052 Convênio no Banco
        + formatar_campo(ag_limpa, 5, "9")            # 053-057 Agência
        + " "                                         # 058     DV Agência
        + formatar_campo(cc_limpa, 12, "9")           # 059-070 Conta Corrente
        + formatar_campo(dac_limpo, 1, "X")           # 071     DV Conta
        + " "                                         # 072     DV Ag/Conta
        + formatar_campo(razao_limpa, 30, "X")        # 073-102 Nome da Empresa
        + formatar_campo(perfil["nome"], 30, "X")     # 103-132 Nome do Banco
        + (" " * 10)                                  # 133-142 Uso exclusivo FEBRABAN
        + "1"                                         # 143     Código Remessa (1=Remessa)
        + data_geracao                                # 144-151 Data geração
        + hora_geracao                                # 152-157 Hora geração
        + formatar_campo(sequencial_arquivo, 6, "9")  # 158-163 NSA (Nº Sequencial Arquivo)
        + formatar_campo(perfil["versao_arq"], 3, "9")# 164-166 Versão layout arquivo
        + "00000"                                     # 167-171 Densidade de gravação
        + (" " * 69)                                  # 172-240 Reservado Banco / Empresa
    )
    _anexar_linha(linhas, header_arq, "Header Arquivo")

    # Agrupa por tipo de lote na ordem FEBRABAN
    grupos_ordem = [
        ("BOLETO_MESMO_BANCO", "20", "30", perfil["versao_lote_j"], "Boletos Mesmo Banco (J+J52)"),
        ("BOLETO_OUTRO_BANCO", "20", "31", perfil["versao_lote_j"], "Boletos Outros Bancos (J+J52)"),
        ("ARRECADACAO",        "22", "11", perfil["versao_lote_o"], "Tributos e Concessionárias (O)"),
        ("PIX_CHAVE",          "20", "45", perfil["versao_lote_a"], "PIX Transferência por Chave (A+B)"),
    ]

    num_lote = 0
    resumo_lotes = []
    total_geral_centavos = 0

    for chave_grupo, tipo_serv, forma_lanc, versao_lote, rotulo_lote in grupos_ordem:
        itens_lote = [x for x in validos if x["_tipo_lote"] == chave_grupo]
        if not itens_lote:
            continue

        num_lote += 1
        _anexar_linha(
            linhas,
            _montar_header_lote(
                cod_banco, num_lote, tipo_serv, forma_lanc, versao_lote,
                cnpj_limpo, conv_limpo, ag_limpa, cc_limpa, dac_limpo,
                razao_limpa, cidade_limpa, cep_limpo, uf_limpa,
            ),
            f"Header Lote {num_lote} ({chave_grupo})",
        )

        seq = 0
        soma_lote_centavos = 0

        for tit in itens_lote:
            centavos = tit["_valor_centavos"]
            soma_lote_centavos += centavos
            dt_venc_str = _data_ddmmaaaa(tit.get("dt_vencimento"), dt_pgto_str)
            favorecido = tit.get("fornecedor") or "FAVORECIDO"
            fav_doc = limpar_documento(tit.get("favorecido_doc") or tit.get("cnpj") or "")
            fav_tipo_insc = "2" if len(fav_doc) == 14 else ("1" if len(fav_doc) == 11 else "0")
            num_doc = str(tit.get("numero_nf") or tit.get("numero_tx") or tit.get("id") or (seq + 1))

            if chave_grupo in ("BOLETO_MESMO_BANCO", "BOLETO_OUTRO_BANCO"):
                # Segmento J (Boleto)
                seq += 1
                seg_j = (
                    formatar_campo(cod_banco, 3, "9")
                    + formatar_campo(num_lote, 4, "9")
                    + "3"
                    + formatar_campo(seq, 5, "9")
                    + "J"
                    + "0"                                     # Tipo movimento (0=Inclusão)
                    + "00"                                    # Código instrução
                    + formatar_campo(tit["_codigo_barras_44"], 44, "X")
                    + formatar_campo(favorecido, 30, "X")
                    + formatar_campo(dt_venc_str, 8, "9")
                    + formatar_campo(centavos, 15, "9")       # Valor nominal
                    + ("0" * 15)                              # Desconto
                    + ("0" * 15)                              # Acréscimos
                    + formatar_campo(dt_pgto_str, 8, "9")
                    + formatar_campo(centavos, 15, "9")       # Valor pagamento
                    + ("0" * 15)                              # Qtde moeda
                    + formatar_campo(num_doc, 20, "X")        # Seu número
                    + (" " * 20)                              # Nosso número
                    + "09"                                    # Moeda (09=Real)
                    + (" " * 16)                              # CNAB + Ocorrências
                )
                _anexar_linha(linhas, seg_j, f"Lote {num_lote} Seg J #{seq}")

                # Segmento J-52 (Obrigatório FEBRABAN/CIP para boletos)
                seq += 1
                seg_j52 = (
                    formatar_campo(cod_banco, 3, "9")         # 001-003 Banco
                    + formatar_campo(num_lote, 4, "9")        # 004-007 Lote
                    + "3"                                     # 008     Tipo Registro
                    + formatar_campo(seq, 5, "9")             # 009-013 Sequencial
                    + "J"                                     # 014     Segmento J
                    + " "                                     # 015     Branco
                    + "00"                                    # 016-017 Cód. Movimento
                    + "52"                                    # 018-019 Identificação J-52
                    + "2"                                     # 020     Tipo Inscrição Pagador (CNPJ)
                    + formatar_campo(cnpj_limpo, 15, "9")     # 021-035 CNPJ Pagador
                    + formatar_campo(razao_limpa, 40, "X")    # 036-075 Nome Pagador
                    + fav_tipo_insc                           # 076     Tipo Inscrição Beneficiário
                    + formatar_campo(fav_doc, 15, "9")        # 077-091 Doc Beneficiário
                    + formatar_campo(favorecido, 40, "X")     # 092-131 Nome Beneficiário
                    + "0"                                     # 132     Tipo Inscrição Sacador/Avalista
                    + ("0" * 15)                              # 133-147 Doc Sacador
                    + (" " * 40)                              # 148-187 Nome Sacador
                    + (" " * 53)                              # 188-240 Uso exclusivo FEBRABAN
                )
                _anexar_linha(linhas, seg_j52, f"Lote {num_lote} Seg J-52 #{seq}")

            elif chave_grupo == "ARRECADACAO":
                # Segmento O (Concessionárias / Tributos com código de barras 48 -> 44)
                seq += 1
                seg_o = (
                    formatar_campo(cod_banco, 3, "9")         # 001-003 Banco
                    + formatar_campo(num_lote, 4, "9")        # 004-007 Lote
                    + "3"                                     # 008     Tipo Registro
                    + formatar_campo(seq, 5, "9")             # 009-013 Sequencial
                    + "O"                                     # 014     Segmento O
                    + "0"                                     # 015     Tipo Movimento
                    + "00"                                    # 016-017 Código Instrução
                    + formatar_campo(tit["_codigo_barras_44"], 44, "X")  # 018-061 Código de Barras
                    + formatar_campo(favorecido, 30, "X")     # 062-091 Nome Concessionária/Órgão
                    + formatar_campo(dt_venc_str, 8, "9")     # 092-099 Data Vencimento
                    + formatar_campo(dt_pgto_str, 8, "9")     # 100-107 Data Pagamento
                    + formatar_campo(centavos, 15, "9")       # 108-122 Valor Pagamento
                    + formatar_campo(num_doc, 20, "X")        # 123-142 Seu Número
                    + (" " * 20)                              # 143-162 Nosso Número
                    + (" " * 68)                              # 163-230 Uso exclusivo FEBRABAN
                    + (" " * 10)                              # 231-240 Ocorrências
                )
                _anexar_linha(linhas, seg_o, f"Lote {num_lote} Seg O #{seq}")

            elif chave_grupo == "PIX_CHAVE":
                # Segmento A (PIX Transferência - Câmara 009)
                seq += 1
                seg_a = (
                    formatar_campo(cod_banco, 3, "9")         # 001-003 Banco
                    + formatar_campo(num_lote, 4, "9")        # 004-007 Lote
                    + "3"                                     # 008     Tipo Registro
                    + formatar_campo(seq, 5, "9")             # 009-013 Sequencial
                    + "A"                                     # 014     Segmento A
                    + "0"                                     # 015     Tipo Movimento
                    + "00"                                    # 016-017 Código Instrução
                    + "009"                                   # 018-020 Câmara Centralizadora (009=PIX)
                    + "000"                                   # 021-023 Banco Favorecido (000 qdo Chave PIX)
                    + ("0" * 5)                               # 024-028 Agência Favorecido
                    + " "                                     # 029     DV Agência
                    + ("0" * 12)                              # 030-041 Conta Favorecido
                    + " "                                     # 042     DV Conta
                    + " "                                     # 043     DV Ag/Conta
                    + formatar_campo(favorecido, 30, "X")     # 044-073 Nome Favorecido
                    + formatar_campo(num_doc, 20, "X")        # 074-093 Seu Número
                    + formatar_campo(dt_pgto_str, 8, "9")     # 094-101 Data Pagamento
                    + "BRL"                                   # 102-104 Tipo Moeda
                    + ("0" * 15)                              # 105-119 Qtde Moeda
                    + formatar_campo(centavos, 15, "9")       # 120-134 Valor Pagamento
                    + (" " * 20)                              # 135-154 Nosso Número
                    + ("0" * 8)                               # 155-162 Data Real
                    + ("0" * 15)                              # 163-177 Valor Real
                    + (" " * 40)                              # 178-217 Informação 2
                    + "  "                                    # 218-219 Finalidade DOC
                    + "     "                                 # 220-224 Finalidade TED
                    + "  "                                    # 225-226 Complemento
                    + "   "                                   # 227-229 CNAB
                    + "0"                                     # 230     Aviso ao Favorecido
                    + (" " * 10)                              # 231-240 Ocorrências
                )
                _anexar_linha(linhas, seg_a, f"Lote {num_lote} Seg A #{seq}")

                # Segmento B (Chave PIX FEBRABAN — Subtipo Forma 45)
                seq += 1
                seg_b = (
                    formatar_campo(cod_banco, 3, "9")             # 001-003 Banco
                    + formatar_campo(num_lote, 4, "9")            # 004-007 Lote
                    + "3"                                         # 008     Tipo Registro
                    + formatar_campo(seq, 5, "9")                 # 009-013 Sequencial
                    + "B"                                         # 014     Segmento B
                    + formatar_campo(tit["_pix_tipo"], 2, "9")    # 015-016 Tipo Chave PIX (01/02/03/04)
                    + " "                                         # 017     Branco
                    + fav_tipo_insc                               # 018     Tipo Inscrição Favorecido
                    + formatar_campo(fav_doc, 14, "9")            # 019-032 CPF/CNPJ Favorecido
                    + formatar_campo(f"PAGTO {num_doc}", 35, "X") # 033-067 TXID / Informação
                    + (" " * 60)                                  # 068-127 Informação entre usuários
                    + formatar_campo(tit["_pix_chave"], 99, "X")  # 128-226 Chave PIX (até 99 chars)
                    + (" " * 6)                                   # 227-232 CNAB
                    + ("0" * 8)                                   # 233-240 ISPB (opcional com chave)
                )
                _anexar_linha(linhas, seg_b, f"Lote {num_lote} Seg B-PIX #{seq}")

        _anexar_linha(
            linhas,
            _montar_trailler_lote(cod_banco, num_lote, seq + 2, soma_lote_centavos),
            f"Trailler Lote {num_lote}",
        )
        total_geral_centavos += soma_lote_centavos
        resumo_lotes.append({
            "lote": num_lote,
            "tipo": chave_grupo,
            "rotulo": rotulo_lote,
            "forma": forma_lanc,
            "qtd_titulos": len(itens_lote),
            "valor_total": round(soma_lote_centavos / 100.0, 2),
        })

    # TRAILLER DE ARQUIVO (Registro 9)
    total_registros_arquivo = len(linhas) + 1
    trailler_arq = (
        formatar_campo(cod_banco, 3, "9")
        + "9999"
        + "9"
        + (" " * 9)
        + formatar_campo(num_lote, 6, "9")
        + formatar_campo(total_registros_arquivo, 6, "9")
        + ("0" * 6)
        + (" " * 205)
    )
    _anexar_linha(linhas, trailler_arq, "Trailler Arquivo")

    resumo = {
        "banco_codigo": cod_banco,
        "banco_nome": perfil["nome"],
        "banco_sigla": perfil["sigla"],
        "data_pagamento": dt_pgto_str,
        "qtd_lotes": num_lote,
        "qtd_titulos": len(validos),
        "qtd_rejeitados": len(rejeitados),
        "valor_total": round(total_geral_centavos / 100.0, 2),
        "lotes": resumo_lotes,
    }
    return "\r\n".join(linhas) + "\r\n", resumo
