import calendar
import io
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

import ofxparse
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ...core.auth_v2 import TenantContext, get_tenant_context, require_v2_roles
from ...db.models import (
    Adiantamento,
    AdiantamentoComprovante,
    ConciliacaoItem,
    DespesaRecorrente,
    ExtratoBancario,
    ExtratoTransacao,
    Fornecedor,
    Titulo,
)
from ...db.session import get_db
from .deps import (
    extrair_ip,
    obter_ccusto_tenant,
    obter_conta_tenant,
    obter_empresa_tenant,
    obter_fornecedor_tenant,
    obter_plano_conta_tenant,
    obter_titulo_tenant,
    registrar_audit_v2,
    to_dec,
)

router = APIRouter(tags=["JR FinCloud v2 - Recorrências, Adiantamentos & Conciliação"])

MAX_OFX_BYTES = 10 * 1024 * 1024  # 10 MB


class DespesaRecorrenteCreateRequest(BaseModel):
    empresa_id: str
    fornecedor_id: Optional[str] = None
    centro_custo_id: Optional[str] = None
    plano_conta_id: Optional[str] = None
    nome: str
    tipo_valor: str = "FIXO"
    valor_estimado: Decimal
    periodicidade: str = "MENSAL"
    dia_vencimento: int = Field(10, ge=1, le=31)
    indice_reajuste: Optional[str] = None


class GerarCompetenciaRequest(BaseModel):
    ano: int = Field(..., ge=2020, le=2100)
    mes: int = Field(..., ge=1, le=12)
    empresa_id: Optional[str] = None


class AdiantamentoV2CreateRequest(BaseModel):
    empresa_id: str
    centro_custo_id: Optional[str] = None
    solicitante: str
    data_liberacao: date
    valor_concedido: Decimal
    forma: str = "PIX"
    motivo: Optional[str] = None


class ComprovanteV2CreateRequest(BaseModel):
    data_gasto: date
    tipo_doc: str
    descricao: str
    valor: Decimal


class FecharAdiantamentoV2Request(BaseModel):
    gerar_reembolso_cp: bool = True


class AlocacaoConciliacaoItem(BaseModel):
    titulo_id: str
    valor_alocado: Decimal
    juros_alocado: Decimal = Decimal("0.00")
    desconto_alocado: Decimal = Decimal("0.00")


class ConciliarVincularRequest(BaseModel):
    extrato_transacao_id: str
    alocacoes: List[AlocacaoConciliacaoItem]
    marcar_transacao_conciliada: bool = True
    metodo: str = "MANUAL"


# ---------------------------------------------------------------------------
# DESPESAS RECORRENTES (GERAÇÃO IDEMPOTENTE POR COMPETÊNCIA)
# ---------------------------------------------------------------------------

@router.get("/despesas-recorrentes")
def listar_despesas_recorrentes(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    itens = (
        db.query(DespesaRecorrente)
        .filter(DespesaRecorrente.tenant_id == ctx.tenant_id, DespesaRecorrente.ativo.is_(True))
        .order_by(DespesaRecorrente.dia_vencimento.asc(), DespesaRecorrente.nome.asc())
        .all()
    )
    return {
        "success": True,
        "despesas_recorrentes": [
            {
                "id": d.id,
                "empresa_id": d.empresa_id,
                "nome": d.nome,
                "tipo_valor": d.tipo_valor,
                "valor_estimado": str(to_dec(d.valor_estimado)),
                "periodicidade": d.periodicidade,
                "dia_vencimento": d.dia_vencimento,
                "indice_reajuste": d.indice_reajuste,
            }
            for d in itens
        ],
    }


@router.post("/despesas-recorrentes", status_code=status.HTTP_201_CREATED)
def criar_despesa_recorrente(
    req: DespesaRecorrenteCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    obter_empresa_tenant(db, ctx.tenant_id, req.empresa_id)
    if req.fornecedor_id:
        obter_fornecedor_tenant(db, ctx.tenant_id, req.fornecedor_id)
    if req.centro_custo_id:
        obter_ccusto_tenant(db, ctx.tenant_id, req.centro_custo_id)
    if req.plano_conta_id:
        obter_plano_conta_tenant(db, ctx.tenant_id, req.plano_conta_id)
    if req.valor_estimado <= Decimal("0.00"):
        raise HTTPException(status_code=422, detail="Valor estimado deve ser maior que zero.")

    rec = DespesaRecorrente(
        tenant_id=ctx.tenant_id,
        empresa_id=req.empresa_id,
        fornecedor_id=req.fornecedor_id,
        centro_custo_id=req.centro_custo_id,
        plano_conta_id=req.plano_conta_id,
        nome=req.nome.strip(),
        tipo_valor=req.tipo_valor.strip().upper(),
        valor_estimado=to_dec(req.valor_estimado),
        periodicidade=req.periodicidade.strip().upper(),
        dia_vencimento=req.dia_vencimento,
        indice_reajuste=req.indice_reajuste,
    )
    db.add(rec)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_DESPESA_RECORRENTE",
        "despesas_recorrentes",
        rec.id,
        dados_depois={"nome": rec.nome, "valor_estimado": str(rec.valor_estimado)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": rec.id}


@router.post("/despesas-recorrentes/gerar-competencia")
def gerar_competencia_recorrencias(
    req: GerarCompetenciaRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    q = db.query(DespesaRecorrente).filter(
        DespesaRecorrente.tenant_id == ctx.tenant_id,
        DespesaRecorrente.ativo.is_(True),
    )
    if req.empresa_id:
        obter_empresa_tenant(db, ctx.tenant_id, req.empresa_id)
        q = q.filter(DespesaRecorrente.empresa_id == req.empresa_id)

    recorrentes = q.all()
    ultimo_dia = calendar.monthrange(req.ano, req.mes)[1]
    dt_emissao = date(req.ano, req.mes, 1)

    criados_ids: list[str] = []
    ignorados = 0

    for rec in recorrentes:
        tx_code = f"REC_{rec.id[:8]}_{req.ano}{req.mes:02d}"
        ja_existe = (
            db.query(Titulo)
            .filter(Titulo.tenant_id == ctx.tenant_id, Titulo.numero_tx == tx_code)
            .first()
        )
        if ja_existe:
            ignorados += 1
            continue

        forn_nome = rec.nome
        forn_doc = None
        if rec.fornecedor_id:
            forn = db.query(Fornecedor).filter(Fornecedor.id == rec.fornecedor_id, Fornecedor.tenant_id == ctx.tenant_id).first()
            if forn:
                forn_nome = forn.razao_social
                forn_doc = forn.cnpj_cpf

        dia_efetivo = min(max(1, rec.dia_vencimento), ultimo_dia)
        dt_venc = date(req.ano, req.mes, dia_efetivo)
        val = to_dec(rec.valor_estimado)

        titulo = Titulo(
            tenant_id=ctx.tenant_id,
            empresa_id=rec.empresa_id,
            fornecedor_id=rec.fornecedor_id,
            centro_custo_id=rec.centro_custo_id,
            plano_conta_id=rec.plano_conta_id,
            tipo_movimento="PAGAR",
            tipo_documento="DESPESA_FIXA",
            numero_tx=tx_code,
            numero_documento=f"{req.ano}{req.mes:02d}-{rec.id[:4].upper()}",
            favorecido_nome=forn_nome,
            favorecido_doc=forn_doc,
            dt_emissao=dt_emissao,
            dt_vencimento=dt_venc,
            valor_bruto=val,
            valor_liquido=val,
            status="PENDENTE",
            descricao=rec.nome,
        )
        db.add(titulo)
        db.flush()
        criados_ids.append(titulo.id)

    if criados_ids:
        registrar_audit_v2(
            db,
            ctx.tenant_id,
            ctx.usuario,
            "GERAR_RECORRENCIAS_COMPETENCIA",
            "titulos",
            f"{req.ano}-{req.mes:02d}",
            dados_depois={"qtd_criados": len(criados_ids), "titulos_ids": criados_ids},
            ip=extrair_ip(request),
        )
    db.commit()
    return {
        "success": True,
        "competencia": f"{req.ano}-{req.mes:02d}",
        "criados": len(criados_ids),
        "ignorados_existentes": ignorados,
        "titulos_ids": criados_ids,
    }


# ---------------------------------------------------------------------------
# ADIANTAMENTOS & PRESTAÇÃO DE CONTAS
# ---------------------------------------------------------------------------

def _obter_adiantamento_tenant(db: Session, tenant_id: str, adiantamento_id: str) -> Adiantamento:
    ad = db.query(Adiantamento).filter(Adiantamento.id == adiantamento_id, Adiantamento.tenant_id == tenant_id).first()
    if not ad:
        raise HTTPException(status_code=404, detail="Adiantamento não encontrado neste workspace.")
    return ad


@router.get("/adiantamentos")
def listar_adiantamentos_v2(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    ads = (
        db.query(Adiantamento)
        .filter(Adiantamento.tenant_id == ctx.tenant_id)
        .order_by(Adiantamento.data_liberacao.desc())
        .all()
    )
    comps = (
        db.query(AdiantamentoComprovante)
        .filter(AdiantamentoComprovante.tenant_id == ctx.tenant_id)
        .all()
    )
    comps_por_ad: dict[str, list] = {}
    for c in comps:
        comps_por_ad.setdefault(c.adiantamento_id, []).append(c)

    resultado = []
    for ad in ads:
        gastos = comps_por_ad.get(ad.id, [])
        tot_comp = sum((to_dec(g.valor) for g in gastos), Decimal("0.00"))
        saldo = to_dec(ad.valor_concedido) - tot_comp
        resultado.append({
            "id": ad.id,
            "empresa_id": ad.empresa_id,
            "centro_custo_id": ad.centro_custo_id,
            "solicitante": ad.solicitante,
            "data_liberacao": ad.data_liberacao.isoformat(),
            "valor_concedido": str(to_dec(ad.valor_concedido)),
            "total_comprovado": str(tot_comp),
            "saldo": str(saldo),
            "status": ad.status,
            "data_fechamento": ad.data_fechamento.isoformat() if ad.data_fechamento else None,
            "gastos": [
                {
                    "id": g.id,
                    "data_gasto": g.data_gasto.isoformat(),
                    "tipo_doc": g.tipo_doc,
                    "descricao": g.descricao,
                    "valor": str(to_dec(g.valor)),
                }
                for g in gastos
            ],
        })
    return {"success": True, "total": len(resultado), "adiantamentos": resultado}


@router.post("/adiantamentos", status_code=status.HTTP_201_CREATED)
def criar_adiantamento_v2(
    req: AdiantamentoV2CreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    obter_empresa_tenant(db, ctx.tenant_id, req.empresa_id)
    if req.centro_custo_id:
        obter_ccusto_tenant(db, ctx.tenant_id, req.centro_custo_id)
    if req.valor_concedido <= Decimal("0.00"):
        raise HTTPException(status_code=422, detail="Valor do adiantamento deve ser maior que zero.")

    ad = Adiantamento(
        tenant_id=ctx.tenant_id,
        empresa_id=req.empresa_id,
        centro_custo_id=req.centro_custo_id,
        solicitante=req.solicitante.strip(),
        data_liberacao=req.data_liberacao,
        valor_concedido=to_dec(req.valor_concedido),
        forma=req.forma,
        motivo=req.motivo,
        status="ABERTO",
    )
    db.add(ad)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_ADIANTAMENTO_V2",
        "adiantamentos",
        ad.id,
        dados_depois={"solicitante": ad.solicitante, "valor": str(ad.valor_concedido)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": ad.id}


@router.post("/adiantamentos/{adiantamento_id}/comprovantes", status_code=status.HTTP_201_CREATED)
def adicionar_comprovante_adiantamento_v2(
    adiantamento_id: str,
    req: ComprovanteV2CreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "SOLICITANTE")),
    db: Session = Depends(get_db),
):
    ad = _obter_adiantamento_tenant(db, ctx.tenant_id, adiantamento_id)
    if ad.status != "ABERTO":
        raise HTTPException(status_code=409, detail="Não é permitido lançar comprovantes em adiantamento já encerrado.")
    if req.valor <= Decimal("0.00"):
        raise HTTPException(status_code=422, detail="Valor do comprovante deve ser positivo.")

    comp = AdiantamentoComprovante(
        tenant_id=ctx.tenant_id,
        adiantamento_id=ad.id,
        data_gasto=req.data_gasto,
        tipo_doc=req.tipo_doc.strip().upper(),
        descricao=req.descricao.strip(),
        valor=to_dec(req.valor),
    )
    db.add(comp)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "LANCAR_COMPROVANTE_V2",
        "adiantamento_comprovantes",
        comp.id,
        dados_depois={"adiantamento_id": ad.id, "valor": str(comp.valor)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": comp.id}


@router.post("/adiantamentos/{adiantamento_id}/fechar")
def fechar_adiantamento_v2(
    adiantamento_id: str,
    req: FecharAdiantamentoV2Request,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    ad = _obter_adiantamento_tenant(db, ctx.tenant_id, adiantamento_id)
    if ad.status != "ABERTO":
        raise HTTPException(status_code=409, detail="Adiantamento já foi encerrado anteriormente.")

    comps = (
        db.query(AdiantamentoComprovante)
        .filter(AdiantamentoComprovante.adiantamento_id == ad.id, AdiantamentoComprovante.tenant_id == ctx.tenant_id)
        .all()
    )
    tot_comp = sum((to_dec(c.valor) for c in comps), Decimal("0.00"))
    saldo = to_dec(ad.valor_concedido) - tot_comp

    if saldo > Decimal("0.00"):
        status_final = "QUITADO_DEVOLUCAO"
    elif saldo < Decimal("0.00"):
        status_final = "QUITADO_REEMBOLSO"
    else:
        status_final = "QUITADO_EXATO"

    hoje = date.today()
    ad.status = status_final
    ad.data_fechamento = hoje

    reembolso_titulo_id = None
    if saldo < Decimal("0.00") and req.gerar_reembolso_cp:
        dif = abs(saldo)
        tit_reemb = Titulo(
            tenant_id=ctx.tenant_id,
            empresa_id=ad.empresa_id,
            centro_custo_id=ad.centro_custo_id,
            tipo_movimento="PAGAR",
            tipo_documento="REEMBOLSO",
            numero_tx=f"REEMB_{ad.id[:8]}",
            numero_documento=f"REEMB-{ad.id[:6].upper()}",
            favorecido_nome=ad.solicitante,
            dt_emissao=hoje,
            dt_vencimento=hoje,
            valor_bruto=dif,
            valor_liquido=dif,
            status="PENDENTE",
            forma_pgto="PIX",
            descricao=f"Reembolso de adiantamento #{ad.id[:8]}",
        )
        db.add(tit_reemb)
        db.flush()
        reembolso_titulo_id = tit_reemb.id

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "FECHAR_ADIANTAMENTO_V2",
        "adiantamentos",
        ad.id,
        dados_depois={"status": status_final, "saldo": str(saldo), "reembolso_titulo_id": reembolso_titulo_id},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "id": ad.id,
        "status": status_final,
        "total_comprovado": str(tot_comp),
        "saldo": str(saldo),
        "reembolso_titulo_id": reembolso_titulo_id,
    }


# ---------------------------------------------------------------------------
# CONCILIAÇÃO BANCÁRIA PERSISTENTE (OFX + N:1 / 1:N)
# ---------------------------------------------------------------------------

def _parse_ofx_bytes(conteudo: bytes) -> list[dict]:
    ofx = ofxparse.OfxParser.parse(io.BytesIO(conteudo))
    movimentos = []
    for account in ofx.accounts:
        for tx in account.statement.transactions:
            amt = Decimal(str(tx.amount))
            movimentos.append({
                "fitid": str(tx.id or "").strip(),
                "data": tx.date.date() if isinstance(tx.date, datetime) else tx.date,
                "tipo": "DEBIT" if amt < 0 else "CREDIT",
                "valor": to_dec(abs(amt)),
                "memo": (tx.memo or "")[:255],
            })
    return movimentos


@router.post("/conciliacao/importar-ofx")
async def importar_ofx_v2(
    request: Request,
    conta_bancaria_id: str = Query(...),
    arquivo: UploadFile = File(...),
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    conta = obter_conta_tenant(db, ctx.tenant_id, conta_bancaria_id)
    conteudo = await arquivo.read()
    if len(conteudo) > MAX_OFX_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo OFX excede o limite máximo de 10 MB.")

    movimentos = await run_in_threadpool(_parse_ofx_bytes, conteudo)

    datas = [m["data"] for m in movimentos if m.get("data")]
    extrato = ExtratoBancario(
        tenant_id=ctx.tenant_id,
        conta_bancaria_id=conta.id,
        origem="OFX",
        arquivo_nome=(arquivo.filename or "extrato.ofx")[:200],
        periodo_inicio=min(datas) if datas else None,
        periodo_fim=max(datas) if datas else None,
    )
    db.add(extrato)
    db.flush()

    existentes_rows = (
        db.query(ExtratoTransacao.fitid)
        .filter(ExtratoTransacao.tenant_id == ctx.tenant_id, ExtratoTransacao.conta_bancaria_id == conta.id)
        .all()
    )
    fitids_existentes = {r[0] for r in existentes_rows}

    importadas = 0
    duplicadas = 0
    for idx, m in enumerate(movimentos):
        fitid = m["fitid"] or f"NOFITID_{extrato.id[:8]}_{idx}"
        if fitid in fitids_existentes:
            duplicadas += 1
            continue
        fitids_existentes.add(fitid)
        db.add(
            ExtratoTransacao(
                tenant_id=ctx.tenant_id,
                extrato_id=extrato.id,
                conta_bancaria_id=conta.id,
                fitid=fitid,
                data_movimento=m["data"],
                tipo=m["tipo"],
                valor=m["valor"],
                memo=m["memo"],
                conciliado=False,
            )
        )
        importadas += 1

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "IMPORTAR_OFX_V2",
        "extratos_bancarios",
        extrato.id,
        dados_depois={"importadas": importadas, "duplicadas": duplicadas},
        ip=extrair_ip(request),
    )
    db.commit()

    return {
        "success": True,
        "extrato_id": extrato.id,
        "importadas": importadas,
        "duplicadas": duplicadas,
    }


@router.get("/conciliacao/transacoes")
def listar_transacoes_conciliacao_v2(
    conta_bancaria_id: Optional[str] = Query(None),
    conciliado: Optional[bool] = Query(None),
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    q = db.query(ExtratoTransacao).filter(ExtratoTransacao.tenant_id == ctx.tenant_id)
    if conta_bancaria_id:
        q = q.filter(ExtratoTransacao.conta_bancaria_id == conta_bancaria_id)
    if conciliado is not None:
        q = q.filter(ExtratoTransacao.conciliado.is_(conciliado))
    rows = q.order_by(ExtratoTransacao.data_movimento.desc()).all()
    return {
        "success": True,
        "total": len(rows),
        "transacoes": [
            {
                "id": t.id,
                "conta_bancaria_id": t.conta_bancaria_id,
                "fitid": t.fitid,
                "data_movimento": t.data_movimento.isoformat(),
                "tipo": t.tipo,
                "valor": str(to_dec(t.valor)),
                "memo": t.memo,
                "conciliado": t.conciliado,
            }
            for t in rows
        ],
    }


@router.post("/conciliacao/vincular")
def vincular_conciliacao_v2(
    req: ConciliarVincularRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    if not req.alocacoes:
        raise HTTPException(status_code=422, detail="Informe ao menos uma alocação de título.")

    tx = (
        db.query(ExtratoTransacao)
        .filter(ExtratoTransacao.id == req.extrato_transacao_id, ExtratoTransacao.tenant_id == ctx.tenant_id)
        .first()
    )
    if not tx:
        raise HTTPException(status_code=404, detail="Transação de extrato não encontrada neste workspace.")
    if tx.conciliado:
        raise HTTPException(status_code=409, detail="Transação bancária já conciliada integralmente.")

    itens_existentes = (
        db.query(ConciliacaoItem)
        .filter(ConciliacaoItem.extrato_transacao_id == tx.id, ConciliacaoItem.tenant_id == ctx.tenant_id)
        .all()
    )
    ja_alocado_tx = sum((to_dec(i.valor_alocado) for i in itens_existentes), Decimal("0.00"))
    soma_nova = sum((to_dec(a.valor_alocado) for a in req.alocacoes), Decimal("0.00"))
    tx_valor = to_dec(tx.valor)

    if ja_alocado_tx + soma_nova > tx_valor:
        raise HTTPException(
            status_code=422,
            detail=f"Soma alocada (R$ {ja_alocado_tx + soma_nova}) excede o valor da transação bancária (R$ {tx_valor}).",
        )
    if req.marcar_transacao_conciliada and (ja_alocado_tx + soma_nova != tx_valor):
        raise HTTPException(
            status_code=422,
            detail=f"Para marcar a transação como conciliada, a soma alocada (R$ {ja_alocado_tx + soma_nova}) deve igualar R$ {tx_valor}.",
        )

    titulos_atualizados = []
    for aloc in req.alocacoes:
        val_aloc = to_dec(aloc.valor_alocado)
        juros_aloc = to_dec(aloc.juros_alocado)
        desc_aloc = to_dec(aloc.desconto_alocado)
        if val_aloc <= Decimal("0.00"):
            raise HTTPException(status_code=422, detail="Valor alocado deve ser maior que zero.")

        titulo = obter_titulo_tenant(db, ctx.tenant_id, aloc.titulo_id)

        db.add(
            ConciliacaoItem(
                tenant_id=ctx.tenant_id,
                extrato_transacao_id=tx.id,
                titulo_id=titulo.id,
                valor_alocado=val_aloc,
                juros_alocado=juros_aloc,
                desconto_alocado=desc_aloc,
                metodo=req.metodo.strip().upper(),
            )
        )

        titulo.valor_pago = to_dec(titulo.valor_pago) + val_aloc
        titulo.juros_multa = to_dec(titulo.juros_multa) + juros_aloc
        titulo.desconto = to_dec(titulo.desconto) + desc_aloc

        efetivo = to_dec(titulo.valor_pago) + to_dec(titulo.desconto) - to_dec(titulo.juros_multa)
        if efetivo >= to_dec(titulo.valor_liquido):
            titulo.status = "CONCILIADO"
            titulo.conciliado = True
            titulo.dt_pagamento = tx.data_movimento
        else:
            titulo.status = "PARCIAL"
            titulo.conciliado = False

        titulos_atualizados.append({
            "titulo_id": titulo.id,
            "status": titulo.status,
            "valor_pago": str(titulo.valor_pago),
        })

    if req.marcar_transacao_conciliada or (ja_alocado_tx + soma_nova == tx_valor):
        tx.conciliado = True

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CONCILIAR_VINCULAR_V2",
        "extrato_transacoes",
        tx.id,
        dados_depois={"titulos": titulos_atualizados, "transacao_conciliada": tx.conciliado},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "extrato_transacao_id": tx.id,
        "transacao_conciliada": tx.conciliado,
        "titulos": titulos_atualizados,
    }
