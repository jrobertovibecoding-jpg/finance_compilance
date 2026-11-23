import calendar
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...core.auth_v2 import TenantContext, get_tenant_context, require_v2_roles
from ...db.models import AuditLog, CentroCusto, Titulo, TituloRateio
from ...db.session import get_db
from .deps import to_dec

router = APIRouter(tags=["JR FinCloud v2 - Relatórios, DRE & Auditoria"])


@router.get("/dashboard")
def dashboard_v2(
    ref_date: Optional[date] = Query(None),
    empresa_id: Optional[str] = Query(None),
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    hoje = ref_date or date.today()
    mes_ini = date(hoje.year, hoje.month, 1)
    ultimo_dia = calendar.monthrange(hoje.year, hoje.month)[1]
    mes_fim = date(hoje.year, hoje.month, ultimo_dia)

    q = db.query(Titulo).filter(Titulo.tenant_id == ctx.tenant_id)
    if empresa_id:
        q = q.filter(Titulo.empresa_id == empresa_id)

    titulos = q.all()

    total_pagar_aberto = Decimal("0.00")
    total_vencido = Decimal("0.00")
    total_vence_hoje = Decimal("0.00")
    total_pagar_mes = Decimal("0.00")
    total_receber_aberto = Decimal("0.00")
    total_receber_mes = Decimal("0.00")
    qtd_pagar_aberto = 0
    qtd_aguardando_aprovacao = 0

    for t in titulos:
        val = to_dec(t.valor_liquido)
        is_aberto = (not t.conciliado) and t.status not in ("PAGO", "CONCILIADO", "REPROVADO")

        if t.tipo_movimento == "PAGAR":
            if t.status == "AGUARDANDO_APROVACAO":
                qtd_aguardando_aprovacao += 1
            if not is_aberto:
                continue
            qtd_pagar_aberto += 1
            total_pagar_aberto += val
            if t.dt_vencimento < hoje:
                total_vencido += val
            elif t.dt_vencimento == hoje:
                total_vence_hoje += val
            if mes_ini <= t.dt_vencimento <= mes_fim:
                total_pagar_mes += val
        elif t.tipo_movimento == "RECEBER":
            if not is_aberto:
                continue
            total_receber_aberto += val
            if mes_ini <= t.dt_vencimento <= mes_fim:
                total_receber_mes += val

    saldo_previsto_mes = total_receber_mes - total_pagar_mes

    return {
        "success": True,
        "referencia": hoje.isoformat(),
        "kpis": {
            "total_pagar_aberto": str(total_pagar_aberto),
            "total_vencido": str(total_vencido),
            "total_vence_hoje": str(total_vence_hoje),
            "total_mes": str(total_pagar_mes),
            "total_receber_aberto": str(total_receber_aberto),
            "total_receber_mes": str(total_receber_mes),
            "saldo_previsto_mes": str(saldo_previsto_mes),
            "qtd_pagar_aberto": qtd_pagar_aberto,
            "qtd_aguardando_aprovacao": qtd_aguardando_aprovacao,
        },
    }


@router.get("/relatorios/dre-por-centro-custo")
def dre_por_centro_custo(
    ano: int = Query(..., ge=2020, le=2100),
    mes: int = Query(..., ge=1, le=12),
    empresa_id: Optional[str] = Query(None),
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    """
    DRE Gerencial por Centro de Custo e Rubrica:
    Respeita integralmente a regra da Pauta de Shopping (se o título tiver linhas
    em titulo_rateios, agrega os rateios sem duplicar com o valor do título pai).
    """
    dt_ini = date(ano, mes, 1)
    dt_fim = date(ano, mes, calendar.monthrange(ano, mes)[1])

    q = db.query(Titulo).filter(
        Titulo.tenant_id == ctx.tenant_id,
        Titulo.tipo_movimento == "PAGAR",
        Titulo.status != "REPROVADO",
        Titulo.dt_vencimento >= dt_ini,
        Titulo.dt_vencimento <= dt_fim,
    )
    if empresa_id:
        q = q.filter(Titulo.empresa_id == empresa_id)

    titulos = q.all()
    titulos_ids = [t.id for t in titulos]

    rateios_por_titulo: dict[str, list[TituloRateio]] = {}
    if titulos_ids:
        rateios = (
            db.query(TituloRateio)
            .filter(TituloRateio.tenant_id == ctx.tenant_id, TituloRateio.titulo_id.in_(titulos_ids))
            .all()
        )
        for r in rateios:
            rateios_por_titulo.setdefault(r.titulo_id, []).append(r)

    ccustos = db.query(CentroCusto).filter(CentroCusto.tenant_id == ctx.tenant_id).all()
    cc_nomes = {c.id: f"{c.codigo} - {c.nome}" for c in ccustos}

    total_geral = Decimal("0.00")
    por_cc: dict[str, dict[str, Decimal]] = {}
    por_rubrica: dict[str, Decimal] = {}

    def _acumular(cc_id: Optional[str], rubrica: str, valor: Decimal):
        nonlocal total_geral
        cc_key = cc_id or "SEM_CENTRO_CUSTO"
        rub_key = rubrica.strip().upper() or "OUTROS"
        total_geral += valor
        por_rubrica[rub_key] = por_rubrica.get(rub_key, Decimal("0.00")) + valor
        por_cc.setdefault(cc_key, {})
        por_cc[cc_key][rub_key] = por_cc[cc_key].get(rub_key, Decimal("0.00")) + valor

    for t in titulos:
        r_list = rateios_por_titulo.get(t.id)
        if r_list:
            for r in r_list:
                _acumular(r.centro_custo_id or t.centro_custo_id, r.rubrica, to_dec(r.valor))
        else:
            _acumular(t.centro_custo_id, t.tipo_documento or "DESPESA", to_dec(t.valor_bruto))

    centros_detalhe = []
    for cc_id, rub_map in sorted(por_cc.items()):
        total_cc = sum(rub_map.values(), Decimal("0.00"))
        centros_detalhe.append({
            "centro_custo_id": None if cc_id == "SEM_CENTRO_CUSTO" else cc_id,
            "centro_custo_nome": cc_nomes.get(cc_id, "Sem Centro de Custo"),
            "total": str(total_cc),
            "rubricas": {k: str(v) for k, v in sorted(rub_map.items())},
        })

    return {
        "success": True,
        "competencia": f"{ano}-{mes:02d}",
        "total_geral": str(total_geral),
        "por_centro_custo": centros_detalhe,
        "por_rubrica": {k: str(v) for k, v in sorted(por_rubrica.items())},
    }


@router.get("/audit-logs")
def listar_audit_logs_v2(
    limit: int = Query(50, ge=1, le=200),
    acao: Optional[str] = Query(None),
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "CONTADOR_LEITURA")),
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog).filter(AuditLog.tenant_id == ctx.tenant_id)
    if acao:
        q = q.filter(AuditLog.acao == acao.strip().upper())

    logs = q.order_by(AuditLog.criado_em.desc()).limit(limit).all()
    return {
        "success": True,
        "total": len(logs),
        "logs": [
            {
                "id": log.id,
                "usuario_email": log.usuario_email,
                "acao": log.acao,
                "entidade": log.entidade,
                "entidade_id": log.entidade_id,
                "dados_antes": log.dados_antes,
                "dados_depois": log.dados_depois,
                "ip_origem": log.ip_origem,
                "criado_em": log.criado_em.isoformat(),
            }
            for log in logs
        ],
    }
