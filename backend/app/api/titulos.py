import calendar
import io
import logging
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations
from typing import List, Optional

import ofxparse
import requests
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..core import config
from ..core.audit import registrar_auditoria
from ..core.database import get_connection, transaction
from ..core.itau_sispag import filtrar_titulos_cnab_validos, gerar_cnab240_sispag_itau
from ..core.security import obter_usuario_logado
from ..core.validators import validar_documento

logger = logging.getLogger("jrfincloud.titulos")

router = APIRouter(tags=["Títulos & Contas a Pagar"])

MAX_OFX_BYTES = 10 * 1024 * 1024  # 10 MB


class ImpostoItem(BaseModel):
    tipo: str
    aliquota: Optional[float] = 0.0
    valor: float
    dt_venc_imp: Optional[str] = None
    historico: Optional[str] = None


class DocumentoEntrada(BaseModel):
    tipo_doc: str = Field(..., description="NFSE, NFE, RECIBO, CONTRATO, FATURA, DESPESA_INTERNA")
    numero_doc: str
    favorecido: str
    cnpj: str
    data_emissao: str
    data_entrada: Optional[str] = None
    vencimento: str
    valor_bruto: float
    historico: Optional[str] = ""
    descricao: Optional[str] = ""
    ccusto: Optional[str] = "Geral"
    solicitante: Optional[str] = ""
    contrato: Optional[str] = ""
    forma_pgto: Optional[str] = "PIX"
    dados_banco: Optional[str] = ""
    linha_dig: Optional[str] = ""
    cod_operacao: Optional[str] = "1.933 - Aquisição de serviço tributado pelo ISSQN"
    cnae: Optional[str] = ""
    item_lc116: Optional[str] = ""
    impostos: Optional[List[ImpostoItem]] = []


class ConciliacaoGrupoRequest(BaseModel):
    titulo_ids: List[int] = Field(..., description="IDs dos títulos no banco a vincular")
    extrato_fitid: Optional[str] = ""
    extrato_valor: float = Field(..., description="Valor do débito no extrato bancário")
    extrato_data: Optional[str] = ""
    extrato_memo: Optional[str] = ""
    tipo_diferenca: Optional[str] = Field(None, description="JUROS, MULTA, DESCONTO, ARREDONDAMENTO")
    operador: Optional[str] = "Sistema"


class TituloEdicao(BaseModel):
    numero_doc: Optional[str] = None
    favorecido: Optional[str] = None
    cnpj: Optional[str] = None
    cod_operacao: Optional[str] = None
    cnae: Optional[str] = None
    item_lc116: Optional[str] = None
    categoria: Optional[str] = None
    descricao: Optional[str] = None
    historico: Optional[str] = None
    ccusto: Optional[str] = None
    solicitante: Optional[str] = None
    vencimento: Optional[str] = None
    data_emissao: Optional[str] = None
    valor: Optional[float] = None
    forma_pgto: Optional[str] = None
    linha_dig: Optional[str] = None
    dados_banco: Optional[str] = None


def _extrair_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _normalizar_data_iso(data_str: Optional[str]) -> str:
    raw = (data_str or "").strip()[:10]
    if raw and "/" in raw:
        parts = raw.split("/")
        if len(parts) == 3 and len(parts[2]) == 4:
            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return raw


def _parse_data_date(data_str: Optional[str]):
    iso = _normalizar_data_iso(data_str)
    if not iso:
        return None
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _to_decimal(val) -> Decimal:
    return Decimal(str(val or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _to_cents(val) -> int:
    return int((_to_decimal(val) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _ratear_valores_grupo(titulos_encontrados: list[dict], extrato_valor: float) -> list[dict]:
    """
    Rateia proporcionalmente o valor efetivamente pago no extrato e eventuais juros/descontos
    entre os títulos do grupo, garantindo que a soma das parcelas em centavos seja exata.
    """
    ext_cents = _to_cents(extrato_valor)
    nominais_cents = [
        max(0, _to_cents(t.get("valor_liquido") if t.get("valor_liquido") is not None else t.get("valor_bruto")))
        for t in titulos_encontrados
    ]
    soma_cents = sum(nominais_cents)
    diff_cents = ext_cents - soma_cents
    n = len(titulos_encontrados)

    pagos_cents: list[int] = []
    juros_cents: list[int] = []
    descs_cents: list[int] = []

    if soma_cents > 0:
        for i, nom_c in enumerate(nominais_cents):
            if i == n - 1:
                pago_c = ext_cents - sum(pagos_cents)
            else:
                pago_c = (ext_cents * nom_c) // soma_cents
            pagos_cents.append(pago_c)
    else:
        base_c = ext_cents // n
        for i in range(n):
            pagos_cents.append(ext_cents - sum(pagos_cents) if i == n - 1 else base_c)

    if diff_cents > 0:
        total_juros = diff_cents
        for i, nom_c in enumerate(nominais_cents):
            if i == n - 1:
                jc = total_juros - sum(juros_cents)
            elif soma_cents > 0:
                jc = (total_juros * nom_c) // soma_cents
            else:
                jc = total_juros // n
            juros_cents.append(jc)
            descs_cents.append(0)
    elif diff_cents < 0:
        total_desc = abs(diff_cents)
        for i, nom_c in enumerate(nominais_cents):
            if i == n - 1:
                dc = total_desc - sum(descs_cents)
            elif soma_cents > 0:
                dc = (total_desc * nom_c) // soma_cents
            else:
                dc = total_desc // n
            descs_cents.append(dc)
            juros_cents.append(0)
    else:
        juros_cents = [0] * n
        descs_cents = [0] * n

    alocados = []
    for idx, t in enumerate(titulos_encontrados):
        alocados.append({
            "titulo": t,
            "valor_pago": float(Decimal(pagos_cents[idx]) / Decimal(100)),
            "juros_multa": float(Decimal(juros_cents[idx]) / Decimal(100)),
            "desconto": float(Decimal(descs_cents[idx]) / Decimal(100)),
        })
    return alocados


@router.get("")
@router.get("/titulos")
def listar_titulos(
    status: Optional[str] = None,
    filtro_data: Optional[str] = None,
    busca: Optional[str] = None,
):
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT id, numero_tx, numero_nf, fornecedor, cnpj, dt_emissao, dt_vencimento,
                   valor_bruto, valor_liquido, status, categoria, filial, responsavel,
                   descricao, forma_pgto, cod_barras, pix_chave, observacao,
                   conciliada, dt_pagamento, cod_operacao, cnae, item_lc116,
                   valor_pago, juros_multa, desconto, grupo_conciliacao
            FROM notas
            WHERE (is_previsao = 0 OR is_previsao IS NULL)
        """)
        rows_notas = cur.fetchall()

        cur.execute("SELECT id, nota_id, tipo, aliquota, valor, dt_venc_imp, status, numero_doc FROM nota_impostos")
        rows_impostos = cur.fetchall()

        impostos_por_nota = {}
        for imp in rows_impostos:
            nid = imp["nota_id"]
            if nid not in impostos_por_nota:
                impostos_por_nota[nid] = []
            impostos_por_nota[nid].append(dict(imp))

        conn.close()

        hoje_date = datetime.now().date()
        semana_date = hoje_date + timedelta(days=7)
        mes_atual_str = datetime.now().strftime("%Y-%m")

        titulos = []

        for n in rows_notas:
            nota_dict = dict(n)
            n_id = nota_dict["id"]
            n_venc = _normalizar_data_iso(nota_dict.get("dt_vencimento"))
            n_conciliada = bool(nota_dict.get("conciliada"))
            is_pago = (nota_dict.get("status") in ("PAGO", "CONCILIADO")) or n_conciliada
            n_status = "CONCILIADO" if is_pago else "PENDENTE"
            n_situacao = "BAIXADO" if is_pago else "ABERTO"

            val_liq = nota_dict.get("valor_liquido")
            val_bru = nota_dict.get("valor_bruto")
            val_exib = val_liq if val_liq is not None else (val_bru or 0.0)

            titulos.append({
                "id": f"NOTA_{n_id}",
                "db_id": n_id,
                "origem": "NOTA",
                "tipo": "FORNECEDOR",
                "tipo_doc": nota_dict.get("categoria") or "NFSE",
                "numero_doc": nota_dict.get("numero_nf") or nota_dict.get("numero_tx"),
                "cod_operacao": nota_dict.get("cod_operacao") or "1.933 - Aquisição de serviço tributado pelo ISSQN",
                "cnae": nota_dict.get("cnae") or "",
                "item_lc116": nota_dict.get("item_lc116") or "",
                "favorecido": nota_dict.get("fornecedor"),
                "cnpj": nota_dict.get("cnpj"),
                "vencimento": n_venc,
                "valor": val_exib,
                "valor_bruto": val_bru or 0.0,
                "historico": nota_dict.get("observacao") or "",
                "descricao": nota_dict.get("descricao") or "",
                "ccusto": nota_dict.get("filial") or "",
                "solicitante": nota_dict.get("responsavel") or "",
                "forma_pgto": nota_dict.get("forma_pgto") or "PIX",
                "linha_dig": nota_dict.get("cod_barras") or "",
                "status": n_status,
                "situacao": n_situacao,
                "conciliada": n_conciliada,
                "data_baixa": nota_dict.get("dt_pagamento"),
                "loteId": nota_dict.get("numero_tx"),
                "valor_pago": nota_dict.get("valor_pago"),
                "juros_multa": nota_dict.get("juros_multa") or 0,
                "desconto": nota_dict.get("desconto") or 0,
                "grupo_conciliacao": nota_dict.get("grupo_conciliacao"),
            })

            for imp in impostos_por_nota.get(n_id, []):
                venc_imp = _normalizar_data_iso(imp.get("dt_venc_imp")) or n_venc
                tipo_imp = imp.get("tipo", "IMPOSTO")
                titulos.append({
                    "id": f"IMP_{imp['id']}",
                    "db_id": imp["id"],
                    "nota_pai_id": n_id,
                    "origem": "IMPOSTO",
                    "tipo": f"IMPOSTO_{tipo_imp}",
                    "tipo_doc": "GUIA_TRIBUTO",
                    "numero_doc": nota_dict.get("numero_nf") or nota_dict.get("numero_tx"),
                    "favorecido": f"Guia de Retenção - {tipo_imp} ({nota_dict.get('fornecedor')})",
                    "cnpj": nota_dict.get("cnpj"),
                    "vencimento": venc_imp,
                    "valor": imp.get("valor", 0.0),
                    "valor_bruto": nota_dict.get("valor_bruto") or 0.0,
                    "historico": f"Vlr. ref. retenção de {tipo_imp} s/ doc {nota_dict.get('numero_nf')} de {nota_dict.get('fornecedor')}",
                    "descricao": f"Retenção Tributária {tipo_imp}",
                    "ccusto": nota_dict.get("filial") or "",
                    "solicitante": nota_dict.get("responsavel") or "",
                    "forma_pgto": "BOLETO",
                    "status": "CONCILIADO" if imp.get("status") == "PAGO" else "PENDENTE",
                    "loteId": nota_dict.get("numero_tx"),
                })

        if status:
            titulos = [t for t in titulos if t["status"].upper() == status.upper()]

        if filtro_data:
            resultado_filtrado = []
            for t in titulos:
                v_date = _parse_data_date(t.get("vencimento"))
                if not v_date:
                    continue
                if filtro_data == "vencidos" and v_date < hoje_date and t["status"] == "PENDENTE":
                    resultado_filtrado.append(t)
                elif filtro_data == "hoje" and v_date == hoje_date:
                    resultado_filtrado.append(t)
                elif filtro_data == "semana" and hoje_date <= v_date <= semana_date:
                    resultado_filtrado.append(t)
                elif filtro_data == "mes" and v_date.strftime("%Y-%m") == mes_atual_str:
                    resultado_filtrado.append(t)
            titulos = resultado_filtrado

        if busca:
            b_low = busca.lower()
            titulos = [
                t for t in titulos
                if b_low in str(t["favorecido"]).lower() or b_low in str(t["numero_doc"]).lower()
            ]

        titulos.sort(key=lambda x: x.get("vencimento") or "9999-99-99")
        return {"success": True, "total": len(titulos), "titulos": titulos}

    except Exception as e:
        logger.exception("Erro ao listar títulos: %s", e)
        return {"success": False, "error": str(e), "titulos": []}


@router.get("/dashboard")
def dashboard_kpis(ref_date: Optional[str] = Query(None, description="Data de referência YYYY-MM-DD (opcional)")):
    try:
        conn = get_connection()
        cur = conn.cursor()
        hoje = _parse_data_date(ref_date) if ref_date else datetime.now().date()
        if not hoje:
            hoje = datetime.now().date()

        hoje_str = hoje.strftime("%Y-%m-%d")
        semana_str = (hoje + timedelta(days=7)).strftime("%Y-%m-%d")
        mes_ini = hoje.strftime("%Y-%m-01")
        ultimo_dia = calendar.monthrange(hoje.year, hoje.month)[1]
        mes_fim = hoje.strftime(f"%Y-%m-{ultimo_dia:02d}")

        cur.execute("""
            SELECT id, fornecedor, dt_vencimento, valor_bruto, valor_liquido,
                   status, conciliada, dt_pagamento
            FROM notas
            WHERE (is_previsao = 0 OR is_previsao IS NULL)
        """)
        rows = cur.fetchall()
        conn.close()

        total_aberto = Decimal("0.00")
        total_vencido = Decimal("0.00")
        total_hoje = Decimal("0.00")
        total_semana = Decimal("0.00")
        total_mes = Decimal("0.00")
        total_baixado_mes = Decimal("0.00")
        qtd_aberto = 0
        qtd_vencido = 0
        qtd_hoje = 0
        qtd_baixado = 0

        venc_map: dict[str, dict] = {}
        forn_map: dict[str, dict] = {}

        for r in rows:
            rd = dict(r)
            val_raw = rd.get("valor_liquido") if rd.get("valor_liquido") is not None else rd.get("valor_bruto")
            valor = _to_decimal(val_raw)
            venc = _normalizar_data_iso(rd.get("dt_vencimento"))
            is_pago = rd.get("status") in ("PAGO", "CONCILIADO") or bool(rd.get("conciliada"))
            fornecedor = rd.get("fornecedor") or "Sem nome"

            if not is_pago:
                if fornecedor not in forn_map:
                    forn_map[fornecedor] = {"total": Decimal("0.00"), "qtd": 0}
                forn_map[fornecedor]["total"] += valor
                forn_map[fornecedor]["qtd"] += 1

            if is_pago:
                qtd_baixado += 1
                dt_pgto = _normalizar_data_iso(rd.get("dt_pagamento")) or venc
                if dt_pgto and mes_ini <= dt_pgto <= mes_fim:
                    total_baixado_mes += valor
                continue

            qtd_aberto += 1
            total_aberto += valor

            if not venc:
                continue

            limite_14d = (hoje + timedelta(days=14)).strftime("%Y-%m-%d")
            if hoje_str <= venc <= limite_14d:
                if venc not in venc_map:
                    venc_map[venc] = {"total": Decimal("0.00"), "qtd": 0}
                venc_map[venc]["total"] += valor
                venc_map[venc]["qtd"] += 1

            if venc < hoje_str:
                total_vencido += valor
                qtd_vencido += 1
            elif venc == hoje_str:
                total_hoje += valor
                qtd_hoje += 1

            if hoje_str <= venc <= semana_str:
                total_semana += valor

            if mes_ini <= venc <= mes_fim:
                total_mes += valor

        venc_14dias = []
        for i in range(15):
            d = (hoje + timedelta(days=i)).strftime("%Y-%m-%d")
            entry = venc_map.get(d, {"total": Decimal("0.00"), "qtd": 0})
            venc_14dias.append({"data": d, "total": float(entry["total"]), "qtd": entry["qtd"]})

        top_forn = sorted(forn_map.items(), key=lambda x: x[1]["total"], reverse=True)[:10]
        top_fornecedores = [{"nome": k, "total": float(v["total"]), "qtd": v["qtd"]} for k, v in top_forn]

        return {
            "success": True,
            "kpis": {
                "total_aberto": float(total_aberto),
                "total_vencido": float(total_vencido),
                "total_vence_hoje": float(total_hoje),
                "total_semana": float(total_semana),
                "total_mes": float(total_mes),
                "total_baixado_mes": float(total_baixado_mes),
                "qtd_aberto": qtd_aberto,
                "qtd_vencido": qtd_vencido,
                "qtd_vence_hoje": qtd_hoje,
                "qtd_baixado": qtd_baixado,
            },
            "vencimentos_14dias": venc_14dias,
            "top_fornecedores": top_fornecedores,
        }
    except Exception as e:
        logger.exception("Erro ao calcular dashboard: %s", e)
        return {"success": False, "error": str(e)}


@router.post("/gravar-documento")
@router.post("/titulos/gravar-documento")
def gravar_documento(
    doc: DocumentoEntrada,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    if doc.valor_bruto <= 0:
        raise HTTPException(status_code=422, detail="O valor bruto deve ser maior que zero.")
    if doc.cnpj and doc.cnpj.strip() and not validar_documento(doc.cnpj):
        raise HTTPException(status_code=422, detail=f"CNPJ/CPF inválido: {doc.cnpj}")

    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT id FROM notas WHERE cnpj = ? AND numero_nf = ?",
            (doc.cnpj.strip(), doc.numero_doc.strip()),
        )
        if cur.fetchone():
            raise HTTPException(
                status_code=400,
                detail=f"Bloqueio de Duplicidade: O documento nº {doc.numero_doc} para o CNPJ {doc.cnpj} já está cadastrado.",
            )

        val_bruto = _to_decimal(doc.valor_bruto)
        tot_ret = sum((_to_decimal(i.valor) for i in (doc.impostos or []) if i.valor > 0), Decimal("0.00"))
        valor_liquido = max(Decimal("0.00"), val_bruto - tot_ret)
        lote_id = f"TX_{int(time.time() * 100)}"

        with transaction(conn):
            cur.execute("""
                INSERT INTO notas (
                    numero_tx, tipo, fornecedor, cnpj, dt_emissao, dt_vencimento,
                    valor_bruto, valor_liquido, status, categoria, observacao,
                    responsavel, descricao, filial, forma_pgto, cod_barras,
                    pix_chave, numero_nf, cod_operacao, cnae, item_lc116, is_previsao, conciliada
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """, (
                lote_id,
                "FORNECEDOR" if doc.tipo_doc == "NFSE" else "DESPESA_GERAL",
                doc.favorecido.strip(),
                doc.cnpj.strip(),
                doc.data_emissao,
                doc.vencimento,
                float(val_bruto),
                float(valor_liquido),
                "PENDENTE",
                doc.tipo_doc,
                doc.historico,
                doc.solicitante,
                doc.descricao,
                doc.ccusto,
                doc.forma_pgto,
                doc.linha_dig,
                doc.dados_banco,
                doc.numero_doc.strip(),
                doc.cod_operacao or "1.933 - Aquisição de serviço tributado pelo ISSQN",
                doc.cnae or "",
                doc.item_lc116 or "",
            ))
            nota_id = cur.lastrowid

            impostos_count = 0
            for imp in (doc.impostos or []):
                if imp.valor > 0:
                    impostos_count += 1
                    cur.execute("""
                        INSERT INTO nota_impostos (
                            nota_id, tipo, aliquota, valor, dt_venc_imp, status, numero_doc
                        ) VALUES (?, ?, ?, ?, ?, 'PENDENTE', ?)
                    """, (
                        nota_id,
                        imp.tipo.upper(),
                        imp.aliquota,
                        float(_to_decimal(imp.valor)),
                        imp.dt_venc_imp or doc.vencimento,
                        doc.numero_doc.strip(),
                    ))

            try:
                f_cnpj = doc.cnpj.strip() if doc.cnpj else ""
                f_razao = doc.favorecido.strip() if doc.favorecido else ""
                if f_razao:
                    f_exist = None
                    if f_cnpj:
                        cur.execute("SELECT id FROM fornecedores WHERE cnpj_cpf = ?", (f_cnpj,))
                        f_exist = cur.fetchone()
                    if not f_exist:
                        cur.execute("SELECT id FROM fornecedores WHERE razao_social = ? COLLATE NOCASE", (f_razao,))
                        f_exist = cur.fetchone()

                    if f_exist:
                        cur.execute("""
                            UPDATE fornecedores SET
                                razao_social = COALESCE(NULLIF(?, ''), razao_social),
                                cnpj_cpf = CASE WHEN (cnpj_cpf IS NULL OR cnpj_cpf = '') AND ? != '' THEN ? ELSE cnpj_cpf END,
                                categoria = COALESCE(NULLIF(?, ''), categoria),
                                ccusto = COALESCE(NULLIF(?, ''), ccusto),
                                responsavel = COALESCE(NULLIF(?, ''), responsavel),
                                forma_pgto = COALESCE(NULLIF(?, ''), forma_pgto),
                                dados_banco = COALESCE(NULLIF(?, ''), dados_banco),
                                cod_operacao = COALESCE(NULLIF(?, ''), cod_operacao),
                                cnae = COALESCE(NULLIF(?, ''), cnae),
                                item_lc116 = COALESCE(NULLIF(?, ''), item_lc116),
                                atualizado_em = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (
                            f_razao,
                            f_cnpj, f_cnpj,
                            doc.tipo_doc or "",
                            doc.ccusto or "",
                            doc.solicitante or "",
                            doc.forma_pgto or "",
                            doc.dados_banco or "",
                            doc.cod_operacao or "",
                            doc.cnae or "",
                            doc.item_lc116 or "",
                            f_exist["id"],
                        ))
                    else:
                        cur.execute("""
                            INSERT INTO fornecedores (
                                razao_social, nome_fantasia, cnpj_cpf, tipo, categoria,
                                ccusto, responsavel, forma_pgto, dados_banco,
                                cod_operacao, cnae, item_lc116
                            ) VALUES (?, ?, ?, 'FORNECEDOR', ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            f_razao,
                            f_razao,
                            f_cnpj if f_cnpj else None,
                            doc.tipo_doc or "",
                            doc.ccusto or "",
                            doc.solicitante or "",
                            doc.forma_pgto or "",
                            doc.dados_banco or "",
                            doc.cod_operacao or "",
                            doc.cnae or "",
                            doc.item_lc116 or "",
                        ))
            except Exception as e_forn:
                logger.warning("Erro ao auto-salvar fornecedor: %s", e_forn)

            registrar_auditoria(
                conn,
                usuario,
                "GRAVAR_DOCUMENTO",
                "notas",
                str(nota_id),
                f"Doc {doc.numero_doc.strip()} - {doc.favorecido.strip()}",
                float(valor_liquido),
                ip=ip,
            )

        return {
            "success": True,
            "message": "Documento e retenções tributárias gravados com sucesso no banco de dados!",
            "nota_id": nota_id,
            "lote_tx": lote_id,
            "valor_liquido": float(valor_liquido),
            "impostos_gravados": impostos_count,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao gravar documento: %s", e)
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")
    finally:
        conn.close()


@router.post("/{identificador}/baixa")
@router.post("/titulos/{identificador}/baixa")
def alternar_baixa(
    identificador: str,
    request: Request,
    forcar_baixa: Optional[bool] = Query(False),
    usuario: dict = Depends(obter_usuario_logado),
):
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        hoje_str = datetime.now().strftime("%Y-%m-%d")

        with transaction(conn):
            if identificador.startswith("IMP_"):
                imp_id = int(identificador.replace("IMP_", ""))
                cur.execute("SELECT status, valor FROM nota_impostos WHERE id = ?", (imp_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Imposto não localizado.")
                novo_status = "PAGO" if forcar_baixa else ("PENDENTE" if row["status"] == "PAGO" else "PAGO")
                cur.execute("UPDATE nota_impostos SET status = ? WHERE id = ?", (novo_status, imp_id))
                registrar_auditoria(
                    conn,
                    usuario,
                    "BAIXA_IMPOSTO",
                    "nota_impostos",
                    str(imp_id),
                    f"Status alterado para {novo_status}",
                    float(row["valor"] or 0.0),
                    ip=ip,
                )
            else:
                clean_id = identificador.replace("NOTA_", "").strip()
                row = None
                nota_id = None
                if clean_id.isdigit():
                    nota_id = int(clean_id)
                    cur.execute("SELECT id, status, valor_liquido, valor_bruto FROM notas WHERE id = ?", (nota_id,))
                    row = cur.fetchone()

                if not row:
                    cur.execute(
                        "SELECT id, status, valor_liquido, valor_bruto FROM notas WHERE numero_nf = ? OR numero_tx = ?",
                        (identificador, identificador),
                    )
                    row = cur.fetchone()
                    if row:
                        nota_id = row["id"]

                if not row:
                    raise HTTPException(status_code=404, detail="Título não localizado.")

                novo_status = (
                    "CONCILIADO"
                    if forcar_baixa
                    else ("PENDENTE" if row["status"] in ("PAGO", "CONCILIADO") else "CONCILIADO")
                )
                cur.execute("""
                    UPDATE notas
                    SET status = ?, conciliada = ?, dt_pagamento = ?
                    WHERE id = ?
                """, (
                    novo_status,
                    1 if novo_status == "CONCILIADO" else 0,
                    hoje_str if novo_status == "CONCILIADO" else None,
                    nota_id,
                ))
                val_ref = float(row["valor_liquido"] if row["valor_liquido"] is not None else (row["valor_bruto"] or 0.0))
                registrar_auditoria(
                    conn,
                    usuario,
                    "BAIXA_TITULO",
                    "notas",
                    str(nota_id),
                    f"Status alterado para {novo_status}",
                    val_ref,
                    ip=ip,
                )

        return {"success": True, "novo_status": novo_status}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao alternar baixa: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.delete("/{identificador}")
@router.delete("/titulos/{identificador}")
def excluir_titulo(
    identificador: str,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()

        with transaction(conn):
            if identificador.startswith("IMP_"):
                imp_id = int(identificador.replace("IMP_", ""))
                cur.execute("SELECT id, valor FROM nota_impostos WHERE id = ?", (imp_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Imposto não localizado.")
                cur.execute("DELETE FROM nota_impostos WHERE id = ?", (imp_id,))
                registrar_auditoria(
                    conn,
                    usuario,
                    "EXCLUIR_IMPOSTO",
                    "nota_impostos",
                    str(imp_id),
                    "Guia de imposto excluída",
                    float(row["valor"] or 0.0),
                    ip=ip,
                )
            else:
                nota_id = int(identificador.replace("NOTA_", ""))
                cur.execute("SELECT id, fornecedor, valor_bruto FROM notas WHERE id = ?", (nota_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Título não localizado.")
                cur.execute("DELETE FROM nota_impostos WHERE nota_id = ?", (nota_id,))
                cur.execute("DELETE FROM notas WHERE id = ?", (nota_id,))
                registrar_auditoria(
                    conn,
                    usuario,
                    "EXCLUIR_TITULO",
                    "notas",
                    str(nota_id),
                    f"Título excluído ({row['fornecedor']})",
                    float(row["valor_bruto"] or 0.0),
                    ip=ip,
                )

        return {"success": True, "message": "Título excluído com sucesso."}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao excluir título: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.put("/{identificador}")
@router.put("/titulos/{identificador}")
@router.post("/{identificador}/editar")
@router.post("/titulos/{identificador}/editar")
def editar_titulo(
    identificador: str,
    dados: TituloEdicao,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    if dados.cnpj is not None and dados.cnpj.strip() and not validar_documento(dados.cnpj):
        raise HTTPException(status_code=422, detail=f"CNPJ/CPF inválido: {dados.cnpj}")

    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()

        with transaction(conn):
            if identificador.startswith("IMP_"):
                imp_id = int(identificador.replace("IMP_", ""))
                cur.execute("SELECT * FROM nota_impostos WHERE id = ?", (imp_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Imposto não localizado.")

                cur.execute("""
                    UPDATE nota_impostos
                    SET valor = COALESCE(?, valor),
                        dt_venc_imp = COALESCE(?, dt_venc_imp),
                        numero_doc = COALESCE(?, numero_doc)
                    WHERE id = ?
                """, (dados.valor, dados.vencimento, dados.numero_doc, imp_id))

                registrar_auditoria(
                    conn,
                    usuario,
                    "EDITAR_IMPOSTO",
                    "nota_impostos",
                    str(imp_id),
                    "Edição de guia tributária",
                    float(dados.valor if dados.valor is not None else (row["valor"] or 0.0)),
                    ip=ip,
                )
            else:
                clean_id = identificador.replace("NOTA_", "").strip()
                nota_id = None
                if clean_id.isdigit():
                    nota_id = int(clean_id)
                else:
                    cur.execute("SELECT id FROM notas WHERE numero_nf = ? OR numero_tx = ?", (identificador, identificador))
                    r = cur.fetchone()
                    if r:
                        nota_id = r["id"]

                if not nota_id:
                    raise HTTPException(status_code=404, detail="Título não localizado.")

                cur.execute("SELECT id, valor_bruto, valor_liquido FROM notas WHERE id = ?", (nota_id,))
                nota_atual = cur.fetchone()
                if not nota_atual:
                    raise HTTPException(status_code=404, detail="Título não localizado.")

                novo_bruto = None
                novo_liquido = None
                if dados.valor is not None:
                    cur.execute("SELECT COALESCE(SUM(valor), 0) AS tot_imp FROM nota_impostos WHERE nota_id = ?", (nota_id,))
                    row_imp = cur.fetchone()
                    tot_imp = _to_decimal(row_imp["tot_imp"] if row_imp else 0)
                    val_bruto_dec = _to_decimal(dados.valor)
                    val_liq_dec = max(Decimal("0.00"), val_bruto_dec - tot_imp)
                    novo_bruto = float(val_bruto_dec)
                    novo_liquido = float(val_liq_dec)

                cur.execute("""
                    UPDATE notas
                    SET fornecedor = COALESCE(?, fornecedor),
                        cnpj = COALESCE(?, cnpj),
                        numero_nf = COALESCE(?, numero_nf),
                        dt_vencimento = COALESCE(?, dt_vencimento),
                        dt_emissao = COALESCE(?, dt_emissao),
                        valor_bruto = COALESCE(?, valor_bruto),
                        valor_liquido = COALESCE(?, valor_liquido),
                        categoria = COALESCE(?, categoria),
                        descricao = COALESCE(?, descricao),
                        observacao = COALESCE(?, observacao),
                        filial = COALESCE(?, filial),
                        responsavel = COALESCE(?, responsavel),
                        forma_pgto = COALESCE(?, forma_pgto),
                        cod_barras = COALESCE(?, cod_barras),
                        cod_operacao = COALESCE(?, cod_operacao),
                        cnae = COALESCE(?, cnae),
                        item_lc116 = COALESCE(?, item_lc116)
                    WHERE id = ?
                """, (
                    dados.favorecido.strip() if dados.favorecido else None,
                    dados.cnpj.strip() if dados.cnpj else None,
                    dados.numero_doc.strip() if dados.numero_doc else None,
                    dados.vencimento if dados.vencimento else None,
                    dados.data_emissao if dados.data_emissao else None,
                    novo_bruto,
                    novo_liquido,
                    dados.categoria if dados.categoria else None,
                    dados.descricao if dados.descricao else None,
                    dados.historico if dados.historico else None,
                    dados.ccusto if dados.ccusto else None,
                    dados.solicitante if dados.solicitante else None,
                    dados.forma_pgto if dados.forma_pgto else None,
                    dados.linha_dig if dados.linha_dig else None,
                    dados.cod_operacao if dados.cod_operacao else None,
                    dados.cnae if dados.cnae else None,
                    dados.item_lc116 if dados.item_lc116 else None,
                    nota_id,
                ))

                if dados.favorecido:
                    try:
                        f_cnpj = dados.cnpj.strip() if dados.cnpj else ""
                        f_razao = dados.favorecido.strip()
                        f_exist = None
                        if f_cnpj:
                            cur.execute("SELECT id FROM fornecedores WHERE cnpj_cpf = ?", (f_cnpj,))
                            f_exist = cur.fetchone()
                        if not f_exist:
                            cur.execute("SELECT id FROM fornecedores WHERE razao_social = ? COLLATE NOCASE", (f_razao,))
                            f_exist = cur.fetchone()

                        if f_exist:
                            cur.execute("""
                                UPDATE fornecedores SET
                                    razao_social = COALESCE(NULLIF(?, ''), razao_social),
                                    cnpj_cpf = CASE WHEN (cnpj_cpf IS NULL OR cnpj_cpf = '') AND ? != '' THEN ? ELSE cnpj_cpf END,
                                    categoria = COALESCE(NULLIF(?, ''), categoria),
                                    ccusto = COALESCE(NULLIF(?, ''), ccusto),
                                    responsavel = COALESCE(NULLIF(?, ''), responsavel),
                                    forma_pgto = COALESCE(NULLIF(?, ''), forma_pgto),
                                    cod_operacao = COALESCE(NULLIF(?, ''), cod_operacao),
                                    cnae = COALESCE(NULLIF(?, ''), cnae),
                                    item_lc116 = COALESCE(NULLIF(?, ''), item_lc116),
                                    atualizado_em = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (
                                f_razao,
                                f_cnpj, f_cnpj,
                                dados.categoria or "",
                                dados.ccusto or "",
                                dados.solicitante or "",
                                dados.forma_pgto or "",
                                dados.cod_operacao or "",
                                dados.cnae or "",
                                dados.item_lc116 or "",
                                f_exist["id"],
                            ))
                        else:
                            cur.execute("""
                                INSERT INTO fornecedores (
                                    razao_social, nome_fantasia, cnpj_cpf, tipo, categoria,
                                    ccusto, responsavel, forma_pgto, cod_operacao, cnae, item_lc116
                                ) VALUES (?, ?, ?, 'FORNECEDOR', ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                f_razao,
                                f_razao,
                                f_cnpj if f_cnpj else None,
                                dados.categoria or "",
                                dados.ccusto or "",
                                dados.solicitante or "",
                                dados.forma_pgto or "",
                                dados.cod_operacao or "",
                                dados.cnae or "",
                                dados.item_lc116 or "",
                            ))
                    except Exception as e_forn:
                        logger.warning("Erro ao auto-salvar fornecedor: %s", e_forn)

                val_aud = novo_liquido if novo_liquido is not None else float(nota_atual["valor_liquido"] or nota_atual["valor_bruto"] or 0.0)
                registrar_auditoria(
                    conn,
                    usuario,
                    "EDITAR_TITULO",
                    "notas",
                    str(nota_id),
                    "Atualização de título",
                    val_aud,
                    ip=ip,
                )

        return {"success": True, "message": "Título atualizado com sucesso!"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao editar título: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.get("/exportar-lote-itau")
@router.post("/exportar-lote-itau")
def exportar_lote_itau(
    request: Request,
    data_pagamento: Optional[str] = Query(None, description="Data de pagamento (YYYY-MM-DD)"),
    filial: Optional[str] = Query(None),
    cnpj_empresa: Optional[str] = Query(None),
    razao_social: Optional[str] = Query(None),
    agencia: Optional[str] = Query(None),
    conta: Optional[str] = Query(None),
    dac: Optional[str] = Query(None),
    usuario: dict = Depends(obter_usuario_logado),
):
    """
    Gera arquivo de remessa CNAB 240 / SISPAG Itaú contendo apenas títulos
    em aberto para o filtro especificado e com código de barras válido.
    """
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()

        query = """
            SELECT id, numero_tx, numero_nf, fornecedor, cnpj, dt_emissao, dt_vencimento,
                   valor_bruto, valor_liquido, status, categoria, filial, forma_pgto,
                   cod_barras, pix_chave
            FROM notas
            WHERE (is_previsao = 0 OR is_previsao IS NULL)
              AND (status = 'PENDENTE' OR status = 'EM ABERTO' OR status IS NULL OR status = '' OR status = 'ABERTO')
        """
        params = []
        if data_pagamento:
            query += " AND (dt_vencimento LIKE ? OR dt_vencimento = ?)"
            params.extend([f"{data_pagamento}%", data_pagamento])
        if filial:
            query += " AND filial = ?"
            params.append(filial)

        cur.execute(query, tuple(params))
        rows = [dict(r) for r in cur.fetchall()]

        titulos_validos, _ = filtrar_titulos_cnab_validos(rows)
        if not titulos_validos:
            raise HTTPException(
                status_code=404,
                detail="Nenhum título em aberto com código de barras válido encontrado para a data/filtro selecionado.",
            )

        dt_pgto = data_pagamento or datetime.now().strftime("%Y-%m-%d")
        try:
            remessa_conteudo = gerar_cnab240_sispag_itau(
                titulos_validos,
                cnpj_empresa=cnpj_empresa,
                razao_social=razao_social,
                agencia=agencia,
                conta=conta,
                dac=dac,
                data_pagamento=dt_pgto,
            )
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))

        total_lote = sum(
            _to_decimal(t.get("valor_liquido") if t.get("valor_liquido") is not None else t.get("valor_bruto"))
            for t in titulos_validos
        )
        nome_arquivo = f"LOTE_ITAU_{dt_pgto.replace('-', '')}.REM"

        registrar_auditoria(
            conn,
            usuario,
            "EXPORTAR_CNAB",
            "remessa_itau",
            nome_arquivo,
            f"Gerado CNAB 240 com {len(titulos_validos)} título(s)",
            float(total_lote),
            ip=ip,
        )
        conn.commit()

        return Response(
            content=remessa_conteudo,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao gerar lote Itaú: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


def _processar_ofx_sync(conteudo: bytes, usuario: dict, ip: str) -> dict:
    ofx = ofxparse.OfxParser.parse(io.BytesIO(conteudo))

    debitos_encontrados = []
    for account in ofx.accounts:
        for tx in account.statement.transactions:
            if tx.amount < 0:
                debitos_encontrados.append({
                    "data": tx.date.strftime("%Y-%m-%d"),
                    "valor": float(_to_decimal(abs(float(tx.amount)))),
                    "descricao": tx.memo or "",
                    "checknum": tx.checknum or tx.id or "",
                    "fitid": str(tx.id or "").strip(),
                })

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("SELECT fitid FROM ofx_conciliados")
        fitids_existentes = {str(r["fitid"]) for r in cur.fetchall() if r["fitid"]}

        cur.execute("""
            SELECT id, fornecedor, dt_vencimento, valor_liquido, valor_bruto, numero_nf
            FROM notas
            WHERE (status = 'PENDENTE' OR status = 'EM ABERTO' OR status IS NULL OR status = '' OR status = 'ABERTO')
              AND (is_previsao = 0 OR is_previsao IS NULL)
        """)
        pendentes = [dict(r) for r in cur.fetchall()]
        ids_usados: set[int] = set()

        titulos_baixados = []
        titulos_baixados_soma = []
        titulos_baixados_juros = []
        ja_conciliados = []
        nao_localizados = []

        with transaction(conn):
            for deb in debitos_encontrados:
                val = deb["valor"]
                dt = deb["data"]
                dt_deb = _parse_data_date(dt)
                fitid = deb["fitid"]

                if fitid and fitid in fitids_existentes:
                    ja_conciliados.append(deb)
                    continue

                matched = False

                # === ESTRATÉGIA 1: Match exato por valor (±R$ 0,05) ===
                candidatos_exatos = []
                for p in pendentes:
                    if p["id"] in ids_usados:
                        continue
                    v_liq = p.get("valor_liquido") if p.get("valor_liquido") is not None else 0
                    v_bru = p.get("valor_bruto") or 0
                    if abs(round(v_liq, 2) - round(val, 2)) < 0.06 or abs(round(v_bru, 2) - round(val, 2)) < 0.06:
                        p_date = _parse_data_date(p.get("dt_vencimento"))
                        dist = abs((dt_deb - p_date).days) if (dt_deb and p_date) else 9999
                        candidatos_exatos.append((dist, p))

                if candidatos_exatos:
                    candidatos_exatos.sort(key=lambda item: item[0])
                    p = candidatos_exatos[0][1]
                    tit_id = p["id"]
                    cur.execute("""
                        UPDATE notas
                        SET status = 'PAGO', conciliada = 1, dt_pagamento = ?, valor_pago = ?
                        WHERE id = ?
                    """, (dt, val, tit_id))
                    if fitid:
                        cur.execute(
                            "INSERT INTO ofx_conciliados (fitid, nota_id, grupo_id, valor, data_extrato) VALUES (?, ?, NULL, ?, ?)",
                            (fitid, tit_id, val, dt),
                        )
                        fitids_existentes.add(fitid)
                    ids_usados.add(tit_id)
                    titulos_baixados.append({
                        "id": tit_id, "fornecedor": p["fornecedor"],
                        "numero_nf": p["numero_nf"], "valor": val,
                        "dt_pagamento": dt, "descricao_banco": deb["descricao"],
                        "match_tipo": "EXATO",
                    })
                    matched = True

                if matched:
                    continue

                # === ESTRATÉGIA 2: Match por soma de 2-3 títulos (janela + cap para evitar O(n^3)) ===
                disponiveis_bruto = [
                    p for p in pendentes
                    if p["id"] not in ids_usados
                    and 0 < float(p.get("valor_liquido") if p.get("valor_liquido") is not None else (p.get("valor_bruto") or 0)) <= val + 0.11
                ]

                def _score_candidato(cand):
                    c_date = _parse_data_date(cand.get("dt_vencimento"))
                    dist_dias = abs((dt_deb - c_date).days) if (dt_deb and c_date) else 365
                    return (0 if dist_dias <= 60 else 1, dist_dias)

                disponiveis_bruto.sort(key=_score_candidato)
                disponiveis = [
                    p for p in disponiveis_bruto
                    if _score_candidato(p)[0] == 0
                ][:30]
                if len(disponiveis) < 2:
                    disponiveis = disponiveis_bruto[:30]

                found_combo = False
                for n in (2, 3):
                    if found_combo or len(disponiveis) < n:
                        break
                    for combo in combinations(disponiveis, n):
                        soma = sum(
                            float(c.get("valor_liquido") if c.get("valor_liquido") is not None else (c.get("valor_bruto") or 0))
                            for c in combo
                        )
                        if abs(round(soma, 2) - round(val, 2)) < 0.11:
                            grupo_id = f"GRP_{int(time.time() * 1000)}_{fitid or len(titulos_baixados_soma)}"
                            combo_list = list(combo)
                            combo_ids = [c["id"] for c in combo_list]
                            rateados = _ratear_valores_grupo(combo_list, val)

                            for item in rateados:
                                tit = item["titulo"]
                                cur.execute("""
                                    UPDATE notas
                                    SET status = 'PAGO', conciliada = 1, dt_pagamento = ?,
                                        valor_pago = ?, juros_multa = ?, desconto = ?,
                                        grupo_conciliacao = ?
                                    WHERE id = ?
                                """, (
                                    dt,
                                    item["valor_pago"],
                                    item["juros_multa"],
                                    item["desconto"],
                                    grupo_id,
                                    tit["id"],
                                ))
                                ids_usados.add(tit["id"])

                            diferenca_grupo = float(_to_decimal(val) - _to_decimal(soma))
                            cur.execute("""
                                INSERT INTO conciliacao_grupos
                                    (id, extrato_fitid, extrato_valor, extrato_data, extrato_memo,
                                     soma_titulos, diferenca, tipo_diferenca, qtd_titulos, operador)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                grupo_id, fitid, val, dt,
                                deb["descricao"], round(soma, 2),
                                round(diferenca_grupo, 2), None, n, "Sistema (Auto OFX)",
                            ))
                            if fitid:
                                cur.execute(
                                    "INSERT INTO ofx_conciliados (fitid, nota_id, grupo_id, valor, data_extrato) VALUES (?, NULL, ?, ?, ?)",
                                    (fitid, grupo_id, val, dt),
                                )
                                fitids_existentes.add(fitid)

                            titulos_baixados_soma.append({
                                "grupo_id": grupo_id, "titulo_ids": combo_ids,
                                "soma_titulos": round(soma, 2), "valor_extrato": val,
                                "dt_pagamento": dt, "descricao_banco": deb["descricao"],
                                "match_tipo": "SOMA",
                            })
                            found_combo = True
                            break

                if found_combo:
                    continue

                # === ESTRATÉGIA 3: Match com tolerância de juros (até 5%) + vínculo temporal/fornecedor ===
                memo_upper = (deb["descricao"] or "").upper()
                for p in pendentes:
                    if p["id"] in ids_usados:
                        continue
                    v_ref = float(p.get("valor_liquido") if p.get("valor_liquido") is not None else (p.get("valor_bruto") or 0))
                    if v_ref <= 0:
                        continue
                    diff = val - v_ref
                    pct = abs(diff / v_ref) * 100
                    if 0.06 < abs(diff) and pct <= 5.0:
                        p_date = _parse_data_date(p.get("dt_vencimento"))
                        perto_data = bool(dt_deb and p_date and abs((dt_deb - p_date).days) <= 45)
                        tokens = [tok for tok in re.split(r"\W+", (p.get("fornecedor") or "").upper()) if len(tok) >= 4]
                        match_memo = any(tok in memo_upper for tok in tokens)
                        if not (perto_data or match_memo):
                            continue

                        tit_id = p["id"]
                        tipo_dif = "JUROS" if diff > 0 else "DESCONTO"
                        cur.execute("""
                            UPDATE notas
                            SET status = 'PAGO', conciliada = 1, dt_pagamento = ?,
                                valor_pago = ?, juros_multa = ?, desconto = ?
                            WHERE id = ?
                        """, (
                            dt, val,
                            round(diff, 2) if diff > 0 else 0,
                            round(abs(diff), 2) if diff < 0 else 0,
                            tit_id,
                        ))
                        if fitid:
                            cur.execute(
                                "INSERT INTO ofx_conciliados (fitid, nota_id, grupo_id, valor, data_extrato) VALUES (?, ?, NULL, ?, ?)",
                                (fitid, tit_id, val, dt),
                            )
                            fitids_existentes.add(fitid)

                        ids_usados.add(tit_id)
                        titulos_baixados_juros.append({
                            "id": tit_id, "fornecedor": p["fornecedor"],
                            "numero_nf": p["numero_nf"],
                            "valor_titulo": v_ref, "valor_pago": val,
                            "diferenca": round(diff, 2),
                            "tipo_diferenca": tipo_dif,
                            "percentual": round(pct, 2),
                            "dt_pagamento": dt, "descricao_banco": deb["descricao"],
                            "match_tipo": "JUROS_TOLERANCIA",
                        })
                        matched = True
                        break

                if not matched:
                    nao_localizados.append(deb)

            total_conc = len(titulos_baixados) + len(titulos_baixados_soma) + len(titulos_baixados_juros)
            registrar_auditoria(
                conn,
                usuario,
                "CONCILIAR_OFX",
                "conciliacao_ofx",
                "",
                f"OFX: {total_conc} conciliados, {len(ja_conciliados)} já conciliados, {len(nao_localizados)} não localizados",
                sum(d["valor"] for d in debitos_encontrados),
                ip=ip,
            )

        return {
            "success": True,
            "total_debitos_ofx": len(debitos_encontrados),
            "total_conciliados_exato": len(titulos_baixados),
            "total_conciliados_soma": len(titulos_baixados_soma),
            "total_conciliados_juros": len(titulos_baixados_juros),
            "total_ja_conciliados": len(ja_conciliados),
            "total_conciliados": len(titulos_baixados) + len(titulos_baixados_soma) + len(titulos_baixados_juros),
            "titulos_baixados": titulos_baixados,
            "titulos_baixados_soma": titulos_baixados_soma,
            "titulos_baixados_juros": titulos_baixados_juros,
            "ja_conciliados": ja_conciliados,
            "nao_localizados": nao_localizados,
        }
    finally:
        conn.close()


@router.post("/conciliar-ofx")
async def conciliar_extrato_ofx(
    request: Request,
    arquivo: UploadFile = File(...),
    usuario: dict = Depends(obter_usuario_logado),
):
    """
    Recebe um arquivo OFX, extrai os débitos de pagamentos e dá baixa automática
    nos títulos correspondentes com deduplicação por FITID.
    """
    try:
        conteudo = await arquivo.read()
        if len(conteudo) > MAX_OFX_BYTES:
            raise HTTPException(status_code=413, detail="Arquivo OFX excede o limite máximo de 10 MB.")
        ip = _extrair_ip(request)
        return await run_in_threadpool(_processar_ofx_sync, conteudo, usuario, ip)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao conciliar OFX: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/titulos-pendentes-para-conciliacao")
def listar_pendentes_conciliacao(
    valor_aprox: Optional[float] = Query(None, description="Valor aproximado para filtrar"),
    fornecedor: Optional[str] = Query(None, description="Filtro por nome de fornecedor"),
    tolerancia_pct: Optional[float] = Query(50.0, description="Tolerância % para filtro por valor"),
):
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, fornecedor, cnpj, dt_vencimento, valor_liquido, valor_bruto,
                   numero_nf, descricao, categoria, forma_pgto
            FROM notas
            WHERE (status = 'PENDENTE' OR status = 'EM ABERTO' OR status IS NULL
                   OR status = '' OR status = 'ABERTO')
              AND (is_previsao = 0 OR is_previsao IS NULL)
            ORDER BY dt_vencimento ASC
        """)
        rows = cur.fetchall()
        conn.close()

        resultados = []
        for r in rows:
            rd = dict(r)
            v = rd.get("valor_liquido") if rd.get("valor_liquido") is not None else (rd.get("valor_bruto") or 0)

            if valor_aprox is not None and valor_aprox > 0 and v > 0:
                diff_pct = abs(v - valor_aprox) / valor_aprox * 100
                if diff_pct > tolerancia_pct:
                    continue

            if fornecedor:
                nome = (rd.get("fornecedor") or "").lower()
                if fornecedor.lower() not in nome:
                    continue

            resultados.append({
                "id": rd["id"],
                "fornecedor": rd.get("fornecedor"),
                "cnpj": rd.get("cnpj"),
                "vencimento": rd.get("dt_vencimento"),
                "valor": v,
                "valor_bruto": rd.get("valor_bruto") or 0,
                "numero_nf": rd.get("numero_nf"),
                "descricao": rd.get("descricao"),
                "categoria": rd.get("categoria"),
            })

        return {"success": True, "total": len(resultados), "titulos": resultados}
    except Exception as e:
        logger.exception("Erro ao listar pendentes para conciliação: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/conciliar-grupo")
def conciliar_grupo(
    req: ConciliacaoGrupoRequest,
    request: Request,
    usuario: dict = Depends(obter_usuario_logado),
):
    """
    Vincula N títulos do sistema a 1 débito do extrato bancário, rateando
    proporcionalmente valor pago, juros e descontos para evitar duplicidade contábil.
    """
    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        if not req.titulo_ids:
            raise HTTPException(status_code=400, detail="Nenhum título informado.")

        unique_ids = list(dict.fromkeys(req.titulo_ids))
        cur = conn.cursor()
        hoje_str = datetime.now().strftime("%Y-%m-%d")
        dt_pgto = req.extrato_data or hoje_str
        fitid = (req.extrato_fitid or "").strip()

        if fitid:
            cur.execute("SELECT fitid FROM ofx_conciliados WHERE fitid = ?", (fitid,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail=f"Movimento FITID '{fitid}' já foi conciliado.")

        placeholders = ",".join(["?" for _ in unique_ids])
        cur.execute(f"""
            SELECT id, fornecedor, valor_liquido, valor_bruto, numero_nf, status, conciliada
            FROM notas WHERE id IN ({placeholders})
        """, tuple(unique_ids))
        titulos_encontrados = [dict(r) for r in cur.fetchall()]

        if len(titulos_encontrados) != len(unique_ids):
            ids_encontrados = {t["id"] for t in titulos_encontrados}
            ids_faltando = [i for i in unique_ids if i not in ids_encontrados]
            raise HTTPException(status_code=404, detail=f"Títulos não encontrados: {ids_faltando}")

        ja_pagos = [t["id"] for t in titulos_encontrados if t.get("status") in ("PAGO", "CONCILIADO") or t.get("conciliada")]
        if ja_pagos:
            raise HTTPException(status_code=409, detail=f"Títulos já baixados/conciliados: {ja_pagos}")

        soma = sum(
            _to_decimal(t.get("valor_liquido") if t.get("valor_liquido") is not None else t.get("valor_bruto"))
            for t in titulos_encontrados
        )
        extrato_dec = _to_decimal(req.extrato_valor)
        diferenca = float(extrato_dec - soma)

        tipo_dif = req.tipo_diferenca
        if tipo_dif is None and abs(diferenca) > 0.05:
            tipo_dif = "JUROS" if diferenca > 0 else "DESCONTO"

        grupo_id = f"GRP_{int(time.time() * 1000)}"
        operador_nome = usuario.get("nome") or req.operador or "Sistema"
        rateados = _ratear_valores_grupo(titulos_encontrados, float(extrato_dec))

        with transaction(conn):
            cur.execute("""
                INSERT INTO conciliacao_grupos
                    (id, extrato_fitid, extrato_valor, extrato_data, extrato_memo,
                     soma_titulos, diferenca, tipo_diferenca, qtd_titulos, operador)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                grupo_id, fitid, float(extrato_dec), dt_pgto,
                req.extrato_memo, float(soma), diferenca, tipo_dif,
                len(unique_ids), operador_nome,
            ))

            if fitid:
                cur.execute(
                    "INSERT INTO ofx_conciliados (fitid, nota_id, grupo_id, valor, data_extrato) VALUES (?, NULL, ?, ?, ?)",
                    (fitid, grupo_id, float(extrato_dec), dt_pgto),
                )

            titulos_result = []
            for item in rateados:
                t = item["titulo"]
                cur.execute("""
                    UPDATE notas
                    SET status = 'PAGO', conciliada = 1, dt_pagamento = ?,
                        valor_pago = ?, juros_multa = ?, desconto = ?,
                        grupo_conciliacao = ?
                    WHERE id = ?
                """, (
                    dt_pgto,
                    item["valor_pago"],
                    item["juros_multa"],
                    item["desconto"],
                    grupo_id,
                    t["id"],
                ))
                titulos_result.append({
                    "id": t["id"],
                    "fornecedor": t["fornecedor"],
                    "numero_nf": t["numero_nf"],
                    "valor_titulo": float(_to_decimal(t.get("valor_liquido") if t.get("valor_liquido") is not None else t.get("valor_bruto"))),
                    "valor_pago_rateado": item["valor_pago"],
                    "juros_rateado": item["juros_multa"],
                    "desconto_rateado": item["desconto"],
                })

            registrar_auditoria(
                conn,
                usuario,
                "CONCILIAR_GRUPO",
                "conciliacao_grupos",
                grupo_id,
                f"Grupo {len(unique_ids)} títulos vs extrato R$ {float(extrato_dec):.2f}",
                float(extrato_dec),
                ip=ip,
            )

        return {
            "success": True,
            "grupo_id": grupo_id,
            "qtd_titulos": len(unique_ids),
            "soma_titulos": float(soma),
            "valor_extrato": float(extrato_dec),
            "diferenca": diferenca,
            "tipo_diferenca": tipo_dif,
            "titulos": titulos_result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao conciliar grupo: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/disparar-whatsapp-diretoria")
def disparar_whatsapp_diretoria(
    request: Request,
    numero: Optional[str] = Query(None, description="Número com DDI+DDD"),
    data_pagamento: Optional[str] = Query(None),
    usuario: dict = Depends(obter_usuario_logado),
):
    """
    Gera resumo consolidado de títulos a pagar e envia via API oficial/configurada de WhatsApp.
    """
    whatsapp_url = config.WHATSAPP_API_URL
    if not whatsapp_url:
        raise HTTPException(
            status_code=503,
            detail="Integração WhatsApp não configurada. Defina WHATSAPP_API_URL no ambiente.",
        )

    destino = (numero or config.WHATSAPP_DEFAULT_NUMBER or "").strip()
    if not destino:
        raise HTTPException(
            status_code=400,
            detail="Número de destino do WhatsApp não informado e WHATSAPP_DEFAULT_NUMBER não configurado.",
        )

    ip = _extrair_ip(request)
    conn = get_connection()
    try:
        cur = conn.cursor()
        hoje_str = data_pagamento or datetime.now().strftime("%Y-%m-%d")

        cur.execute("""
            SELECT id, fornecedor, dt_vencimento, valor_liquido, valor_bruto, forma_pgto
            FROM notas
            WHERE (is_previsao = 0 OR is_previsao IS NULL)
              AND (status = 'PENDENTE' OR status = 'EM ABERTO' OR status IS NULL OR status = '' OR status = 'ABERTO')
              AND (dt_vencimento LIKE ? OR dt_vencimento = ?)
        """, (f"{hoje_str}%", hoje_str))

        titulos = [dict(r) for r in cur.fetchall()]
        total_despesas = sum(
            _to_decimal(t.get("valor_liquido") if t.get("valor_liquido") is not None else t.get("valor_bruto"))
            for t in titulos
        )

        msg = (
            f"*{config.COMPANY_DISPLAY_NAME} - AUTORIZAÇÃO DE PAGAMENTOS*\n"
            f"Data: {hoje_str}\n\n"
            f"Total a Pagar: R$ {float(total_despesas):,.2f}\n"
            f"Quantidade de Títulos: {len(titulos)}\n\n"
            f"Acesse o painel para conferência e aprovação:\n"
            f"{config.APP_PUBLIC_URL}\n"
        )

        headers = {"Content-Type": "application/json"}
        if config.WHATSAPP_API_TOKEN:
            headers["Authorization"] = f"Bearer {config.WHATSAPP_API_TOKEN}"

        resp = requests.post(
            whatsapp_url,
            json={"number": destino, "message": msg},
            headers=headers,
            timeout=10,
        )

        registrar_auditoria(
            conn,
            usuario,
            "DISPARAR_WHATSAPP",
            "notificacoes",
            destino,
            f"Resumo de {len(titulos)} título(s) enviado para diretoria",
            float(total_despesas),
            ip=ip,
        )
        conn.commit()

        return {
            "success": resp.status_code < 400,
            "mensagem_enviada": msg,
            "whatsapp_status": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Erro ao disparar WhatsApp: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
