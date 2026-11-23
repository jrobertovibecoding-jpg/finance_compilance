"""
Gerador de Arquivo de Remessa CNAB 240 / SISPAG Itaú
Permite o agendamento em lote de boletos no Itaú Empresas.
Layout Padrão Febraban / Itaú SISPAG (240 posições por linha).
"""

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from . import config
from .validators import limpar_documento, validar_cnpj


def linha_digitavel_para_codigo_barras(linha: Optional[str]) -> str:
    """
    Converte linha digitável de boleto bancário (47 dígitos) ou arrecadação (48 dígitos)
    para código de barras (44 dígitos).
    """
    if not linha:
        return ""
    limpa = re.sub(r"\D", "", str(linha))
    if len(limpa) == 47:
        banco_moeda = limpa[0:4]       # AAAB
        dv_geral = limpa[32:33]        # K
        fator_valor = limpa[33:47]     # UUUUVVVVVVVVVV
        campo1 = limpa[4:9]            # CCCCC
        campo2 = limpa[10:20]          # DDDDDDDDDD
        campo3 = limpa[21:31]          # EEEEEEEEEE
        return f"{banco_moeda}{dv_geral}{fator_valor}{campo1}{campo2}{campo3}"
    elif len(limpa) == 48:
        return limpa[0:11] + limpa[12:23] + limpa[24:35] + limpa[36:47]
    elif len(limpa) == 44:
        return limpa
    return ""


def formatar_campo(texto, tamanho: int, tipo: str = "X") -> str:
    """
    Formata campo para o CNAB:
    tipo="X": Alfa (preenchido com espaços à direita)
    tipo="9": Numérico (preenchido com zeros à esquerda)
    """
    if texto is None:
        texto = ""
    texto_str = str(texto)
    if tipo == "9":
        texto_limpo = re.sub(r"\D", "", texto_str)
        return texto_limpo.zfill(tamanho)[-tamanho:]
    nfkd = unicodedata.normalize("NFKD", texto_str)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.upper().ljust(tamanho)[:tamanho]


def filtrar_titulos_cnab_validos(titulos: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separa títulos aptos (com código de barras válido de 44 dígitos) dos rejeitados."""
    validos = []
    rejeitados = []
    for tit in titulos:
        linha_dig = tit.get("linha_dig") or tit.get("cod_barras") or ""
        codigo_barras = linha_digitavel_para_codigo_barras(linha_dig)
        if len(codigo_barras) == 44 and codigo_barras.isdigit():
            item = dict(tit)
            item["_codigo_barras_44"] = codigo_barras
            validos.append(item)
        else:
            rejeitados.append({
                "id": tit.get("id"),
                "fornecedor": tit.get("fornecedor"),
                "motivo": "Código de barras / linha digitável ausente ou inválido",
            })
    return validos, rejeitados


def _para_centavos(valor) -> int:
    dec = Decimal(str(valor or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if dec < Decimal("0.00"):
        raise ValueError("Valor de título inválido (negativo) para remessa CNAB 240.")
    return int((dec * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _anexar_linha(linhas: list[str], linha: str, rotulo: str) -> None:
    if len(linha) != 240:
        raise ValueError(f"Linha CNAB 240 inválida ({rotulo}): esperado 240 caracteres, obtido {len(linha)}.")
    linhas.append(linha)


def gerar_cnab240_sispag_itau(
    titulos: list[dict],
    cnpj_empresa: Optional[str] = None,
    razao_social: Optional[str] = None,
    agencia: Optional[str] = None,
    conta: Optional[str] = None,
    dac: Optional[str] = None,
    data_pagamento: Optional[str] = None,
    cidade: Optional[str] = None,
    cep: Optional[str] = None,
    uf: Optional[str] = None,
) -> str:
    """
    Gera as linhas do arquivo CNAB 240 / SISPAG para pagamento de fornecedores (Itaú).
    Exige dados bancários reais configurados e títulos com código de barras válido.
    """
    cnpj_limpo = limpar_documento(cnpj_empresa if cnpj_empresa is not None else config.EMPRESA_CNPJ)
    razao_limpa = (razao_social if razao_social is not None else config.EMPRESA_RAZAO_SOCIAL).strip()
    ag_limpa = re.sub(r"\D", "", agencia if agencia is not None else config.ITAU_AGENCIA)
    cc_limpa = re.sub(r"\D", "", conta if conta is not None else config.ITAU_CONTA)
    dac_limpo = (dac if dac is not None else config.ITAU_DAC).strip()[:1] or "0"
    cidade_limpa = (cidade if cidade is not None else config.EMPRESA_CIDADE).strip() or "SALVADOR"
    cep_limpo = (cep if cep is not None else config.EMPRESA_CEP).strip() or "40000000"
    uf_limpa = (uf if uf is not None else config.EMPRESA_UF).strip()[:2] or "BA"

    if not validar_cnpj(cnpj_limpo):
        raise ValueError("CNPJ da empresa pagadora não configurado ou inválido para remessa CNAB 240.")
    if not razao_limpa:
        raise ValueError("Razão Social da empresa pagadora não configurada para remessa CNAB 240.")
    if not ag_limpa or ag_limpa == "0" * len(ag_limpa):
        raise ValueError("Agência Itaú não configurada para remessa CNAB 240.")
    if not cc_limpa or cc_limpa == "0" * len(cc_limpa):
        raise ValueError("Conta corrente Itaú não configurada para remessa CNAB 240.")

    titulos_validos, _ = filtrar_titulos_cnab_validos(titulos)
    if not titulos_validos:
        raise ValueError("Nenhum título possui código de barras / linha digitável válido (44, 47 ou 48 dígitos).")

    linhas: list[str] = []
    hoje = datetime.now()
    data_geracao = hoje.strftime("%d%m%Y")
    hora_geracao = hoje.strftime("%H%M%S")

    if not data_pagamento:
        dt_pgto_str = data_geracao
    elif "-" in data_pagamento:
        dt_obj = datetime.strptime(data_pagamento[:10], "%Y-%m-%d")
        dt_pgto_str = dt_obj.strftime("%d%m%Y")
    elif "/" in data_pagamento:
        parts = data_pagamento.split("/")
        dt_pgto_str = f"{parts[0].zfill(2)}{parts[1].zfill(2)}{parts[2]}"
    else:
        dt_pgto_str = formatar_campo(data_pagamento, 8, "9")

    # 1. HEADER DE ARQUIVO (Registro 0)
    linha_header_arq = (
        "341"
        + "0000"
        + "0"
        + (" " * 9)
        + "2"
        + formatar_campo(cnpj_limpo, 14, "9")
        + formatar_campo("", 20, "X")
        + formatar_campo(ag_limpa, 5, "9")
        + " "
        + formatar_campo(cc_limpa, 12, "9")
        + formatar_campo(dac_limpo, 1, "X")
        + " "
        + formatar_campo(razao_limpa, 30, "X")
        + formatar_campo("BANCO ITAU SA", 30, "X")
        + (" " * 10)
        + "1"
        + data_geracao
        + hora_geracao
        + ("0" * 9)
        + "00000"
        + (" " * 69)
    )
    _anexar_linha(linhas, linha_header_arq, "Header Arquivo")

    # 2. HEADER DE LOTE (Registro 1) - Pagamento Fornecedores
    linha_header_lote = (
        "341"
        + "0001"
        + "1"
        + "C"
        + "20"
        + "30"
        + "040"
        + " "
        + "2"
        + formatar_campo(cnpj_limpo, 14, "9")
        + formatar_campo("", 20, "X")
        + formatar_campo(ag_limpa, 5, "9")
        + " "
        + formatar_campo(cc_limpa, 12, "9")
        + formatar_campo(dac_limpo, 1, "X")
        + " "
        + formatar_campo(razao_limpa, 30, "X")
        + formatar_campo("", 40, "X")
        + formatar_campo("", 30, "X")
        + formatar_campo("0", 5, "9")
        + formatar_campo("", 15, "X")
        + formatar_campo(cidade_limpa, 20, "X")
        + formatar_campo(cep_limpo, 8, "9")
        + formatar_campo(uf_limpa, 2, "X")
        + (" " * 18)
    )
    _anexar_linha(linhas, linha_header_lote, "Header Lote")

    # 3. DETALHES (Segmento J para Boletos / Títulos)
    seq_registro = 0
    total_centavos = 0

    for tit in titulos_validos:
        seq_registro += 1
        valor_raw = tit.get("valor_liquido") if tit.get("valor_liquido") is not None else tit.get("valor_bruto")
        valor_centavos = _para_centavos(valor_raw)
        if valor_centavos <= 0:
            raise ValueError(f"Título #{tit.get('id')} com valor zerado não pode integrar remessa CNAB 240.")
        total_centavos += valor_centavos

        codigo_barras = tit["_codigo_barras_44"]
        dt_venc_raw = tit.get("dt_vencimento") or ""
        venc_digits = re.sub(r"\D", "", dt_venc_raw)
        if len(venc_digits) == 8:
            if "/" in dt_venc_raw:
                dt_venc_str = venc_digits
            else:
                dt_venc_str = f"{venc_digits[6:8]}{venc_digits[4:6]}{venc_digits[0:4]}"
        else:
            dt_venc_str = dt_pgto_str

        favorecido = tit.get("fornecedor") or "FORNECEDOR"
        num_doc = str(tit.get("numero_nf") or tit.get("numero_tx") or tit.get("id") or seq_registro)

        linha_detalhe = (
            "341"
            + "0001"
            + "3"
            + str(seq_registro).zfill(5)
            + "J"
            + "0"
            + "00"
            + formatar_campo(codigo_barras, 44, "X")
            + formatar_campo(favorecido, 30, "X")
            + formatar_campo(dt_venc_str, 8, "9")
            + formatar_campo(str(valor_centavos), 15, "9")
            + ("0" * 15)
            + ("0" * 15)
            + formatar_campo(dt_pgto_str, 8, "9")
            + formatar_campo(str(valor_centavos), 15, "9")
            + ("0" * 15)
            + formatar_campo(num_doc, 20, "X")
            + (" " * 20)
            + "09"
            + (" " * 16)
        )
        _anexar_linha(linhas, linha_detalhe, f"Detalhe J #{seq_registro}")

    # 4. TRAILLER DE LOTE (Registro 5)
    total_registros_lote = seq_registro + 2
    linha_trailler_lote = (
        "341"
        + "0001"
        + "5"
        + (" " * 9)
        + str(total_registros_lote).zfill(6)
        + formatar_campo(str(total_centavos), 18, "9")
        + ("0" * 18)
        + (" " * 181)
    )
    _anexar_linha(linhas, linha_trailler_lote, "Trailler Lote")

    # 5. TRAILLER DE ARQUIVO (Registro 9)
    total_registros_arquivo = len(linhas) + 1
    linha_trailler_arquivo = (
        "341"
        + "9999"
        + "9"
        + (" " * 9)
        + "000001"
        + str(total_registros_arquivo).zfill(6)
        + ("0" * 6)
        + (" " * 205)
    )
    _anexar_linha(linhas, linha_trailler_arquivo, "Trailler Arquivo")

    return "\r\n".join(linhas) + "\r\n"
