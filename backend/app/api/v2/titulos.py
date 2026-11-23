from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...core.auth_v2 import TenantContext, get_tenant_context, require_v2_roles
from ...core.itau_sispag import filtrar_titulos_cnab_validos, gerar_cnab240_sispag_itau
from ...core.validators import validar_documento
from ...db.models import (
    Titulo,
    TituloAprovacao,
    TituloImposto,
    TituloRateio,
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

router = APIRouter(tags=["JR FinCloud v2 - Títulos, Rateios, Alçadas & CNAB"])


class ImpostoV2Item(BaseModel):
    tipo: str
    aliquota: Decimal = Decimal("0.0000")
    valor: Decimal
    dt_vencimento: Optional[date] = None


class TituloV2CreateRequest(BaseModel):
    empresa_id: str
    centro_custo_id: Optional[str] = None
    fornecedor_id: Optional[str] = None
    plano_conta_id: Optional[str] = None
    conta_bancaria_id: Optional[str] = None
    tipo_movimento: str = "PAGAR"
    tipo_documento: str = "NFSE"
    numero_documento: str
    favorecido_nome: str
    favorecido_doc: Optional[str] = None
    dt_emissao: date
    dt_vencimento: date
    valor_bruto: Decimal
    forma_pgto: str = "PIX"
    cod_barras: Optional[str] = None
    descricao: Optional[str] = None
    impostos: List[ImpostoV2Item] = []


class TituloV2UpdateRequest(BaseModel):
    centro_custo_id: Optional[str] = None
    fornecedor_id: Optional[str] = None
    plano_conta_id: Optional[str] = None
    conta_bancaria_id: Optional[str] = None
    numero_documento: Optional[str] = None
    favorecido_nome: Optional[str] = None
    favorecido_doc: Optional[str] = None
    dt_vencimento: Optional[date] = None
    valor_bruto: Optional[Decimal] = None
    forma_pgto: Optional[str] = None
    cod_barras: Optional[str] = None
    descricao: Optional[str] = None


class RateioItemRequest(BaseModel):
    centro_custo_id: Optional[str] = None
    plano_conta_id: Optional[str] = None
    rubrica: str = Field(..., description="ALUGUEL, CONDOMINIO, FPP, IPTU, ENERGIA, AR_CONDICIONADO, AGUA, OUTROS")
    valor: Decimal
    observacao: Optional[str] = None


class TituloRateioSaveRequest(BaseModel):
    itens: List[RateioItemRequest]


class AprovacaoDecisaoRequest(BaseModel):
    canal: str = "WEB"
    justificativa: Optional[str] = None


class ReprovacaoDecisaoRequest(BaseModel):
    canal: str = "WEB"
    justificativa: str


class RemessaCnabRequest(BaseModel):
    conta_bancaria_id: str
    data_pagamento: date
    somente_aprovados: bool = True
    titulo_ids: Optional[List[str]] = None


def _serializar_titulo(t: Titulo) -> dict:
    return {
        "id": t.id,
        "empresa_id": t.empresa_id,
        "centro_custo_id": t.centro_custo_id,
        "fornecedor_id": t.fornecedor_id,
        "plano_conta_id": t.plano_conta_id,
        "tipo_movimento": t.tipo_movimento,
        "tipo_documento": t.tipo_documento,
        "numero_documento": t.numero_documento,
        "favorecido_nome": t.favorecido_nome,
        "favorecido_doc": t.favorecido_doc,
        "dt_emissao": t.dt_emissao.isoformat(),
        "dt_vencimento": t.dt_vencimento.isoformat(),
        "dt_pagamento": t.dt_pagamento.isoformat() if t.dt_pagamento else None,
        "valor_bruto": str(to_dec(t.valor_bruto)),
        "valor_liquido": str(to_dec(t.valor_liquido)),
        "valor_pago": str(to_dec(t.valor_pago)),
        "juros_multa": str(to_dec(t.juros_multa)),
        "desconto": str(to_dec(t.desconto)),
        "status": t.status,
        "conciliado": t.conciliado,
        "forma_pgto": t.forma_pgto,
        "cod_barras": t.cod_barras,
        "descricao": t.descricao,
    }


@router.get("/titulos")
def listar_titulos_v2(
    empresa_id: Optional[str] = Query(None),
    centro_custo_id: Optional[str] = Query(None),
    status_filtro: Optional[str] = Query(None, alias="status"),
    tipo_movimento: Optional[str] = Query(None),
    venc_inicio: Optional[date] = Query(None),
    venc_fim: Optional[date] = Query(None),
    busca: Optional[str] = Query(None),
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    q = db.query(Titulo).filter(Titulo.tenant_id == ctx.tenant_id)
    if empresa_id:
        q = q.filter(Titulo.empresa_id == empresa_id)
    if centro_custo_id:
        q = q.filter(Titulo.centro_custo_id == centro_custo_id)
    if status_filtro:
        q = q.filter(Titulo.status == status_filtro.strip().upper())
    if tipo_movimento:
        q = q.filter(Titulo.tipo_movimento == tipo_movimento.strip().upper())
    if venc_inicio:
        q = q.filter(Titulo.dt_vencimento >= venc_inicio)
    if venc_fim:
        q = q.filter(Titulo.dt_vencimento <= venc_fim)
    if busca and busca.strip():
        termo = f"%{busca.strip()}%"
        q = q.filter(
            or_(
                Titulo.favorecido_nome.ilike(termo),
                Titulo.numero_documento.ilike(termo),
                Titulo.descricao.ilike(termo),
            )
        )

    titulos = q.order_by(Titulo.dt_vencimento.asc()).all()
    return {
        "success": True,
        "total": len(titulos),
        "titulos": [_serializar_titulo(t) for t in titulos],
    }


@router.post("/titulos", status_code=status.HTTP_201_CREATED)
def criar_titulo_v2(
    req: TituloV2CreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    emp = obter_empresa_tenant(db, ctx.tenant_id, req.empresa_id)
    if req.centro_custo_id:
        obter_ccusto_tenant(db, ctx.tenant_id, req.centro_custo_id)
    if req.fornecedor_id:
        obter_fornecedor_tenant(db, ctx.tenant_id, req.fornecedor_id)
    if req.plano_conta_id:
        obter_plano_conta_tenant(db, ctx.tenant_id, req.plano_conta_id)
    if req.conta_bancaria_id:
        obter_conta_tenant(db, ctx.tenant_id, req.conta_bancaria_id)
    if req.favorecido_doc and req.favorecido_doc.strip() and not validar_documento(req.favorecido_doc):
        raise HTTPException(status_code=422, detail=f"CPF/CNPJ do favorecido inválido: {req.favorecido_doc}")

    if req.valor_bruto <= Decimal("0.00"):
        raise HTTPException(status_code=422, detail="Valor bruto deve ser maior que zero.")

    bruto = to_dec(req.valor_bruto)
    tot_ret = sum((to_dec(imp.valor) for imp in req.impostos if imp.valor > 0), Decimal("0.00"))
    liquido = max(Decimal("0.00"), bruto - tot_ret)

    titulo = Titulo(
        tenant_id=ctx.tenant_id,
        empresa_id=emp.id,
        centro_custo_id=req.centro_custo_id,
        fornecedor_id=req.fornecedor_id,
        plano_conta_id=req.plano_conta_id,
        conta_bancaria_id=req.conta_bancaria_id,
        tipo_movimento=req.tipo_movimento.strip().upper(),
        tipo_documento=req.tipo_documento.strip().upper(),
        numero_documento=req.numero_documento.strip(),
        favorecido_nome=req.favorecido_nome.strip(),
        favorecido_doc=req.favorecido_doc.strip() if req.favorecido_doc else None,
        dt_emissao=req.dt_emissao,
        dt_vencimento=req.dt_vencimento,
        valor_bruto=bruto,
        valor_liquido=liquido,
        forma_pgto=req.forma_pgto,
        cod_barras=req.cod_barras,
        descricao=req.descricao,
        status="PENDENTE",
    )
    db.add(titulo)
    db.flush()

    for imp in req.impostos:
        if imp.valor > 0:
            db.add(
                TituloImposto(
                    tenant_id=ctx.tenant_id,
                    titulo_id=titulo.id,
                    tipo=imp.tipo.upper(),
                    aliquota=imp.aliquota,
                    valor=to_dec(imp.valor),
                    dt_vencimento=imp.dt_vencimento or req.dt_vencimento,
                )
            )

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_TITULO_V2",
        "titulos",
        titulo.id,
        dados_depois={"valor_bruto": str(bruto), "valor_liquido": str(liquido)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "id": titulo.id,
        "valor_bruto": str(titulo.valor_bruto),
        "valor_liquido": str(titulo.valor_liquido),
    }


@router.get("/titulos/{titulo_id}")
def obter_titulo_detalhado_v2(
    titulo_id: str,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    titulo = obter_titulo_tenant(db, ctx.tenant_id, titulo_id)
    impostos = (
        db.query(TituloImposto)
        .filter(TituloImposto.titulo_id == titulo.id, TituloImposto.tenant_id == ctx.tenant_id)
        .all()
    )
    rateios = (
        db.query(TituloRateio)
        .filter(TituloRateio.titulo_id == titulo.id, TituloRateio.tenant_id == ctx.tenant_id)
        .all()
    )
    aprovacoes = (
        db.query(TituloAprovacao)
        .filter(TituloAprovacao.titulo_id == titulo.id, TituloAprovacao.tenant_id == ctx.tenant_id)
        .order_by(TituloAprovacao.decidido_em.asc())
        .all()
    )
    return {
        "success": True,
        "titulo": _serializar_titulo(titulo),
        "impostos": [
            {"id": i.id, "tipo": i.tipo, "aliquota": str(i.aliquota), "valor": str(to_dec(i.valor)), "status": i.status}
            for i in impostos
        ],
        "rateios": [
            {
                "id": r.id,
                "centro_custo_id": r.centro_custo_id,
                "plano_conta_id": r.plano_conta_id,
                "rubrica": r.rubrica,
                "valor": str(to_dec(r.valor)),
                "percentual": str(r.percentual),
            }
            for r in rateios
        ],
        "aprovacoes": [
            {
                "id": a.id,
                "aprovador_id": a.aprovador_id,
                "decisao": a.decisao,
                "canal": a.canal,
                "justificativa": a.justificativa,
                "decidido_em": a.decidido_em.isoformat(),
            }
            for a in aprovacoes
        ],
    }


@router.put("/titulos/{titulo_id}")
def atualizar_titulo_v2(
    titulo_id: str,
    req: TituloV2UpdateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    titulo = obter_titulo_tenant(db, ctx.tenant_id, titulo_id)

    if req.valor_bruto is not None and (titulo.conciliado or titulo.status in ("PAGO", "CONCILIADO")):
        raise HTTPException(status_code=409, detail="Não é permitido alterar o valor de um título já pago ou conciliado.")

    if req.centro_custo_id:
        obter_ccusto_tenant(db, ctx.tenant_id, req.centro_custo_id)
        titulo.centro_custo_id = req.centro_custo_id
    if req.fornecedor_id:
        obter_fornecedor_tenant(db, ctx.tenant_id, req.fornecedor_id)
        titulo.fornecedor_id = req.fornecedor_id
    if req.plano_conta_id:
        obter_plano_conta_tenant(db, ctx.tenant_id, req.plano_conta_id)
        titulo.plano_conta_id = req.plano_conta_id
    if req.conta_bancaria_id:
        obter_conta_tenant(db, ctx.tenant_id, req.conta_bancaria_id)
        titulo.conta_bancaria_id = req.conta_bancaria_id
    if req.favorecido_doc is not None:
        if req.favorecido_doc.strip() and not validar_documento(req.favorecido_doc):
            raise HTTPException(status_code=422, detail=f"CPF/CNPJ inválido: {req.favorecido_doc}")
        titulo.favorecido_doc = req.favorecido_doc.strip() or None

    if req.numero_documento is not None:
        titulo.numero_documento = req.numero_documento.strip()
    if req.favorecido_nome is not None:
        titulo.favorecido_nome = req.favorecido_nome.strip()
    if req.dt_vencimento is not None:
        titulo.dt_vencimento = req.dt_vencimento
    if req.forma_pgto is not None:
        titulo.forma_pgto = req.forma_pgto
    if req.cod_barras is not None:
        titulo.cod_barras = req.cod_barras
    if req.descricao is not None:
        titulo.descricao = req.descricao

    if req.valor_bruto is not None:
        novo_bruto = to_dec(req.valor_bruto)
        if novo_bruto <= Decimal("0.00"):
            raise HTTPException(status_code=422, detail="Valor bruto deve ser maior que zero.")
        impostos = (
            db.query(TituloImposto)
            .filter(TituloImposto.titulo_id == titulo.id, TituloImposto.tenant_id == ctx.tenant_id)
            .all()
        )
        tot_ret = sum((to_dec(i.valor) for i in impostos), Decimal("0.00"))
        titulo.valor_bruto = novo_bruto
        titulo.valor_liquido = max(Decimal("0.00"), novo_bruto - tot_ret)

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "EDITAR_TITULO_V2",
        "titulos",
        titulo.id,
        dados_depois={"valor_bruto": str(titulo.valor_bruto), "valor_liquido": str(titulo.valor_liquido)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "titulo": _serializar_titulo(titulo)}


@router.delete("/titulos/{titulo_id}")
def excluir_titulo_v2(
    titulo_id: str,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    titulo = obter_titulo_tenant(db, ctx.tenant_id, titulo_id)
    if titulo.conciliado or titulo.status in ("PAGO", "CONCILIADO", "PARCIAL"):
        raise HTTPException(status_code=409, detail="Título já liquidado/parcial não pode ser excluído.")

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "EXCLUIR_TITULO_V2",
        "titulos",
        titulo.id,
        dados_antes={"numero_documento": titulo.numero_documento, "valor_bruto": str(titulo.valor_bruto)},
        ip=extrair_ip(request),
    )
    db.delete(titulo)
    db.commit()
    return {"success": True, "message": "Título excluído com sucesso."}


# ---------------------------------------------------------------------------
# RATEIO MULTI-RUBRICA (PAUTA SHOPPING)
# ---------------------------------------------------------------------------

@router.get("/titulos/{titulo_id}/rateios")
def listar_rateios_titulo(
    titulo_id: str,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    titulo = obter_titulo_tenant(db, ctx.tenant_id, titulo_id)
    rateios = (
        db.query(TituloRateio)
        .filter(TituloRateio.titulo_id == titulo.id, TituloRateio.tenant_id == ctx.tenant_id)
        .all()
    )
    return {
        "success": True,
        "titulo_id": titulo.id,
        "itens": [
            {
                "id": r.id,
                "centro_custo_id": r.centro_custo_id,
                "plano_conta_id": r.plano_conta_id,
                "rubrica": r.rubrica,
                "valor": str(to_dec(r.valor)),
                "percentual": str(r.percentual),
                "observacao": r.observacao,
            }
            for r in rateios
        ],
    }


@router.post("/titulos/{titulo_id}/rateios")
def salvar_rateio_titulo(
    titulo_id: str,
    req: TituloRateioSaveRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    titulo = obter_titulo_tenant(db, ctx.tenant_id, titulo_id)

    if not req.itens:
        raise HTTPException(status_code=422, detail="Informe ao menos uma linha de rateio.")

    bruto = to_dec(titulo.valor_bruto)
    soma_rateio = Decimal("0.00")
    linhas_validadas = []

    for item in req.itens:
        val_item = to_dec(item.valor)
        if val_item <= Decimal("0.00"):
            raise HTTPException(status_code=422, detail="Todos os itens do rateio devem ter valor positivo.")
        if item.centro_custo_id:
            obter_ccusto_tenant(db, ctx.tenant_id, item.centro_custo_id)
        if item.plano_conta_id:
            obter_plano_conta_tenant(db, ctx.tenant_id, item.plano_conta_id)
        soma_rateio += val_item
        pct = ((val_item / bruto) * Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        linhas_validadas.append((item, val_item, pct))

    if soma_rateio != bruto:
        raise HTTPException(
            status_code=422,
            detail=f"A soma do rateio (R$ {soma_rateio}) deve bater exatamente com o valor bruto do título (R$ {bruto}).",
        )

    db.query(TituloRateio).filter(TituloRateio.titulo_id == titulo.id, TituloRateio.tenant_id == ctx.tenant_id).delete()
    novos = []
    for item, val_item, pct in linhas_validadas:
        r = TituloRateio(
            tenant_id=ctx.tenant_id,
            titulo_id=titulo.id,
            centro_custo_id=item.centro_custo_id,
            plano_conta_id=item.plano_conta_id,
            rubrica=item.rubrica.strip().upper(),
            valor=val_item,
            percentual=pct,
            observacao=item.observacao,
        )
        db.add(r)
        novos.append({"rubrica": r.rubrica, "valor": str(val_item), "percentual": str(pct)})

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "SALVAR_RATEIO",
        "titulo_rateios",
        titulo.id,
        dados_depois={"itens": novos},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "titulo_id": titulo.id, "total_rateado": str(soma_rateio), "itens": novos}


# ---------------------------------------------------------------------------
# WORKFLOW DE ALÇADAS & APROVAÇÃO
# ---------------------------------------------------------------------------

@router.post("/titulos/{titulo_id}/submeter-aprovacao")
def submeter_aprovacao(
    titulo_id: str,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "SOLICITANTE")),
    db: Session = Depends(get_db),
):
    titulo = obter_titulo_tenant(db, ctx.tenant_id, titulo_id)
    if titulo.conciliado or titulo.status in ("PAGO", "CONCILIADO"):
        raise HTTPException(status_code=409, detail="Título já liquidado não pode voltar para aprovação.")

    antes = titulo.status
    titulo.status = "AGUARDANDO_APROVACAO"
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "SUBMETER_APROVACAO",
        "titulos",
        titulo.id,
        dados_antes={"status": antes},
        dados_depois={"status": titulo.status},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": titulo.id, "status": titulo.status}


@router.post("/titulos/{titulo_id}/aprovar")
def aprovar_titulo(
    titulo_id: str,
    req: AprovacaoDecisaoRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR")),
    db: Session = Depends(get_db),
):
    titulo = obter_titulo_tenant(db, ctx.tenant_id, titulo_id)
    if titulo.conciliado or titulo.status in ("PAGO", "CONCILIADO"):
        raise HTTPException(status_code=409, detail="Título já está liquidado.")

    antes = titulo.status
    titulo.status = "APROVADO"
    aprov = TituloAprovacao(
        tenant_id=ctx.tenant_id,
        titulo_id=titulo.id,
        aprovador_id=ctx.usuario.id,
        decisao="APROVADO",
        canal=(req.canal or "WEB").strip().upper(),
        justificativa=req.justificativa,
    )
    db.add(aprov)
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "APROVAR_TITULO",
        "titulos",
        titulo.id,
        dados_antes={"status": antes},
        dados_depois={"status": "APROVADO", "canal": aprov.canal},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": titulo.id, "status": titulo.status, "aprovacao_id": aprov.id}


@router.post("/titulos/{titulo_id}/reprovar")
def reprovar_titulo(
    titulo_id: str,
    req: ReprovacaoDecisaoRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR")),
    db: Session = Depends(get_db),
):
    if not (req.justificativa or "").strip():
        raise HTTPException(status_code=422, detail="Justificativa obrigatória ao reprovar um título.")

    titulo = obter_titulo_tenant(db, ctx.tenant_id, titulo_id)
    if titulo.conciliado or titulo.status in ("PAGO", "CONCILIADO"):
        raise HTTPException(status_code=409, detail="Título já está liquidado.")

    antes = titulo.status
    titulo.status = "REPROVADO"
    aprov = TituloAprovacao(
        tenant_id=ctx.tenant_id,
        titulo_id=titulo.id,
        aprovador_id=ctx.usuario.id,
        decisao="REPROVADO",
        canal=(req.canal or "WEB").strip().upper(),
        justificativa=req.justificativa.strip(),
    )
    db.add(aprov)
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "REPROVAR_TITULO",
        "titulos",
        titulo.id,
        dados_antes={"status": antes},
        dados_depois={"status": "REPROVADO", "justificativa": aprov.justificativa},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": titulo.id, "status": titulo.status, "aprovacao_id": aprov.id}


# ---------------------------------------------------------------------------
# REMESSA CNAB 240 MULTI-EMPRESA (ITAÚ SISPAG)
# ---------------------------------------------------------------------------

@router.post("/remessas/itau-cnab240")
def exportar_remessa_itau_v2(
    req: RemessaCnabRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    conta = obter_conta_tenant(db, ctx.tenant_id, req.conta_bancaria_id)
    empresa = obter_empresa_tenant(db, ctx.tenant_id, conta.empresa_id)

    status_permitidos = ["APROVADO"] if req.somente_aprovados else ["PENDENTE", "APROVADO"]
    q = db.query(Titulo).filter(
        Titulo.tenant_id == ctx.tenant_id,
        Titulo.empresa_id == empresa.id,
        Titulo.tipo_movimento == "PAGAR",
        Titulo.conciliado.is_(False),
        Titulo.status.in_(status_permitidos),
    )
    if req.titulo_ids:
        q = q.filter(Titulo.id.in_(req.titulo_ids))

    candidatos = [
        {
            "id": t.id,
            "numero_nf": t.numero_documento,
            "numero_tx": t.numero_tx,
            "fornecedor": t.favorecido_nome,
            "dt_vencimento": t.dt_vencimento.isoformat(),
            "valor_liquido": str(to_dec(t.valor_liquido)),
            "valor_bruto": str(to_dec(t.valor_bruto)),
            "cod_barras": t.cod_barras or "",
        }
        for t in q.all()
    ]

    validos, _ = filtrar_titulos_cnab_validos(candidatos)
    if not validos:
        raise HTTPException(
            status_code=404,
            detail="Nenhum título elegível com código de barras válido encontrado para os critérios informados.",
        )

    try:
        remessa = gerar_cnab240_sispag_itau(
            validos,
            cnpj_empresa=empresa.cnpj,
            razao_social=empresa.razao_social,
            agencia=conta.agencia,
            conta=conta.conta,
            dac=conta.dac,
            data_pagamento=req.data_pagamento.isoformat(),
            cidade=empresa.cidade,
            cep=empresa.cep,
            uf=empresa.uf,
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    total_remessa = sum((to_dec(v["valor_liquido"]) for v in validos), Decimal("0.00"))
    nome_arq = f"LOTE_ITAU_{req.data_pagamento:%Y%m%d}.REM"

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "EXPORTAR_CNAB_V2",
        "contas_bancarias",
        conta.id,
        dados_depois={"arquivo": nome_arq, "qtd_titulos": len(validos), "total": str(total_remessa)},
        ip=extrair_ip(request),
    )
    db.commit()

    return Response(
        content=remessa,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{nome_arq}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
