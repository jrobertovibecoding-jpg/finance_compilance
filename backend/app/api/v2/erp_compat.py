from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from itertools import combinations
import io
import os
from pathlib import Path
import re
from typing import Any, List, Optional, Union
import uuid

import ofxparse
import requests
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ...core import config
from ...core.auth_v2 import TenantContext, get_tenant_context, require_v2_roles
from ...core.cnab240_multibanco import (
    BANCOS_CNAB240,
    classificar_titulos_cnab,
    gerar_cnab240_multibanco,
    obter_perfil_banco,
)
from ...core.itau_sispag import filtrar_titulos_cnab_validos, gerar_cnab240_sispag_itau
from ...core.security import gerar_hash_senha, verificar_senha
from ...core.validators import limpar_documento, validar_documento
from ...db.models import (
    Adiantamento,
    AdiantamentoComprovante,
    CentroCusto,
    ConciliacaoItem,
    ContaBancaria,
    DespesaRecorrente,
    DocumentoAnexo,
    Empresa,
    ExtratoBancario,
    ExtratoTransacao,
    Fornecedor,
    PlanoConta,
    Titulo,
    TituloImposto,
    TituloItem,
    TituloRateio,
)
from ...db.session import get_db
from .auth_workspaces import LoginV2Request, login_v2
from .deps import extrair_ip, registrar_audit_v2, slugify, to_dec

router = APIRouter(tags=["JR FinCloud v2 - Ponte Compatibilidade ERP"])

MAX_OFX_BYTES = 10 * 1024 * 1024


class AlterarSenhaV2Request(BaseModel):
    senha_atual: str
    nova_senha: str


class FornecedorCompatRequest(BaseModel):
    id: Optional[str] = None
    razao_social: str
    nome_fantasia: Optional[str] = ""
    cnpj_cpf: Optional[str] = ""
    tipo: Optional[str] = "FORNECEDOR"
    categoria: Optional[str] = ""
    ccusto: Optional[str] = ""
    responsavel: Optional[str] = ""
    forma_pgto: Optional[str] = ""
    pix_chave: Optional[str] = ""
    dados_banco: Optional[str] = ""
    cod_operacao: Optional[str] = ""
    cnae: Optional[str] = ""
    item_lc116: Optional[str] = ""
    optante_simples: Optional[bool] = False
    regime_tributario: Optional[str] = None
    ativo: Optional[int] = 1


class ImpostoCompatItem(BaseModel):
    tipo: str
    aliquota: Optional[float] = 0.0
    valor: float
    dt_venc_imp: Optional[str] = None
    historico: Optional[str] = None


class ItemNFCompatItem(BaseModel):
    numero_item: Optional[int] = 1
    codigo_produto: Optional[str] = ""
    descricao: str
    ncm: Optional[str] = ""
    cfop: Optional[str] = ""
    unidade: Optional[str] = "UN"
    quantidade: float = 1.0
    valor_unitario: float = 0.0
    valor_total: float = 0.0


class ParcelaCompatItem(BaseModel):
    numero: Optional[str] = ""
    vencimento: str
    valor: float
    linha_dig: Optional[str] = ""
    forma_pgto: Optional[str] = ""


class RateioCompatItem(BaseModel):
    loja: Optional[str] = ""
    categoria: Optional[str] = ""
    rubrica: Optional[str] = ""
    valor: float = 0.0
    percentual: Optional[float] = 0.0
    observacao: Optional[str] = ""


class DocumentoCompatEntrada(BaseModel):
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
    impostos: Optional[List[ImpostoCompatItem]] = []
    itens: Optional[List[ItemNFCompatItem]] = []
    parcelas: Optional[List[ParcelaCompatItem]] = []
    rateios: Optional[List[RateioCompatItem]] = []


class TituloCompatEdicao(BaseModel):
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
    pix_chave: Optional[str] = None
    dados_banco: Optional[str] = None


class PrevisaoCompatEntrada(BaseModel):
    nome: str
    favorecido: str
    cnpj: Optional[str] = ""
    mesRef: str
    vencimento: str
    valor: float
    tipoValor: Optional[str] = "FIXO"
    ccusto: Optional[str] = "Administrativo"
    obs: Optional[str] = ""


class AdiantamentoCompatEntrada(BaseModel):
    solicitante: str
    ccusto: Optional[str] = "Geral"
    data: str
    valor: float
    forma: Optional[str] = "PIX"
    motivo: Optional[str] = "Adiantamento para despesas operacionais"


class ComprovanteCompatEntrada(BaseModel):
    adiantamento_id: Union[str, int]
    data: str
    tipo: str
    descricao: str
    valor: float


class FechamentoCompatEntrada(BaseModel):
    tipo_fechamento: str = Field(..., description="DEVOLUCAO, REEMBOLSO, EXATO")
    saldo: float
    gerar_reembolso_cp: Optional[bool] = False


class ConciliacaoGrupoCompatRequest(BaseModel):
    titulo_ids: List[Union[str, int]] = Field(..., description="IDs dos títulos no banco a vincular")
    extrato_fitid: Optional[str] = ""
    extrato_valor: float = Field(..., description="Valor do débito no extrato bancário")
    extrato_data: Optional[str] = ""
    extrato_memo: Optional[str] = ""
    tipo_diferenca: Optional[str] = Field(None, description="JUROS, MULTA, DESCONTO, ARREDONDAMENTO")
    operador: Optional[str] = "Sistema"


def _parse_date(raw_str: Optional[str], default: Optional[date] = None) -> Optional[date]:
    txt = (raw_str or "").strip()
    if not txt:
        return default
    if "T" in txt:
        txt = txt.split("T", 1)[0]
    if "/" in txt and len(txt) >= 10:
        parts = txt[:10].split("/")
        if len(parts) == 3 and len(parts[2]) == 4:
            txt = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    try:
        return date.fromisoformat(txt[:10])
    except ValueError:
        return default


def _strip_ref_id(id_ref: str) -> tuple[str, str]:
    raw = (id_ref or "").strip()
    if raw.startswith("IMP_"):
        return "IMPOSTO", raw[4:].strip()
    if raw.startswith("NOTA_"):
        return "NOTA", raw[5:].strip()
    return "NOTA", raw


def _resolver_empresa_padrao(db: Session, tenant_id: str) -> Empresa:
    emp = (
        db.query(Empresa)
        .filter(Empresa.tenant_id == tenant_id, Empresa.ativo.is_(True))
        .order_by(Empresa.criado_em.asc())
        .first()
    )
    if not emp:
        emp = db.query(Empresa).filter(Empresa.tenant_id == tenant_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Nenhuma empresa configurada para este workspace.")
    return emp


def _resolver_plano_e_ccusto(
    db: Session, tenant_id: str, empresa_id: str, texto_ref: Optional[str]
) -> tuple[Optional[CentroCusto], Optional[PlanoConta]]:
    raw = (texto_ref or "").strip()
    if not raw:
        return None, None

    prefixo = raw.split("-", 1)[0].strip() if "-" in raw else raw
    pc = (
        db.query(PlanoConta)
        .filter(
            PlanoConta.tenant_id == tenant_id,
            or_(PlanoConta.codigo == prefixo, PlanoConta.nome.ilike(raw)),
        )
        .first()
    )

    cod_cc = slugify(prefixo or raw).upper()[:30]
    cc = (
        db.query(CentroCusto)
        .filter(
            CentroCusto.tenant_id == tenant_id,
            or_(
                CentroCusto.id == raw,
                CentroCusto.codigo == raw[:30],
                CentroCusto.codigo == cod_cc,
                CentroCusto.nome.ilike(raw[:150]),
            ),
        )
        .first()
    )
    if not cc:
        cc = CentroCusto(
            tenant_id=tenant_id,
            empresa_id=empresa_id,
            codigo=cod_cc,
            nome=raw[:150],
            tipo_unidade="OPERACIONAL",
            ativo=True,
        )
        db.add(cc)
        db.flush()
    return cc, pc


def _upsert_fornecedor_v2(
    db: Session,
    tenant_id: str,
    razao_social: str,
    nome_fantasia: Optional[str] = None,
    cnpj_cpf: Optional[str] = None,
    tipo: str = "FORNECEDOR",
    categoria: Optional[str] = None,
    forma_pgto: Optional[str] = None,
    pix_chave: Optional[str] = None,
    dados_banco: Optional[str] = None,
    cod_operacao: Optional[str] = None,
    cnae: Optional[str] = None,
    item_lc116: Optional[str] = None,
    optante_simples: Optional[bool] = None,
    regime_tributario: Optional[str] = None,
) -> Fornecedor:
    razao = razao_social.strip()[:200]
    doc_raw = (cnpj_cpf or "").strip()
    doc_limpo: Optional[str] = None
    if doc_raw:
        if not validar_documento(doc_raw):
            raise HTTPException(status_code=422, detail=f"CPF/CNPJ inválido: {doc_raw}")
        doc_limpo = limpar_documento(doc_raw)

    forn = None
    if doc_limpo:
        forn = (
            db.query(Fornecedor)
            .filter(Fornecedor.tenant_id == tenant_id, Fornecedor.cnpj_cpf == doc_limpo)
            .first()
        )
    if not forn:
        forn = (
            db.query(Fornecedor)
            .filter(Fornecedor.tenant_id == tenant_id, func.lower(Fornecedor.razao_social) == razao.lower())
            .first()
        )

    if forn:
        forn.razao_social = razao or forn.razao_social
        if nome_fantasia:
            forn.nome_fantasia = nome_fantasia.strip()[:200]
        if doc_limpo and not forn.cnpj_cpf:
            forn.cnpj_cpf = doc_limpo
        if categoria:
            forn.categoria_padrao = categoria.strip()[:120]
        if forma_pgto:
            forn.forma_pgto_padrao = forma_pgto.strip()[:30]
        if pix_chave:
            forn.pix_chave = pix_chave.strip()[:140]
        if dados_banco:
            forn.dados_banco = dados_banco.strip()
        if cod_operacao:
            forn.cod_operacao = cod_operacao.strip()[:120]
        if cnae:
            forn.cnae = cnae.strip()[:40]
        if item_lc116:
            forn.item_lc116 = item_lc116.strip()[:20]
        if optante_simples is not None:
            forn.optante_simples = optante_simples
        if regime_tributario:
            forn.regime_tributario = regime_tributario.strip()[:40]
    else:
        forn = Fornecedor(
            tenant_id=tenant_id,
            razao_social=razao,
            nome_fantasia=(nome_fantasia or razao).strip()[:200],
            cnpj_cpf=doc_limpo,
            tipo=(tipo or "FORNECEDOR").strip().upper()[:30],
            regime_tributario=regime_tributario.strip()[:40] if regime_tributario else None,
            optante_simples=bool(optante_simples),
            categoria_padrao=categoria.strip()[:120] if categoria else None,
            forma_pgto_padrao=forma_pgto.strip()[:30] if forma_pgto else None,
            pix_chave=pix_chave.strip()[:140] if pix_chave else None,
            dados_banco=dados_banco.strip() if dados_banco else None,
            cod_operacao=cod_operacao.strip()[:120] if cod_operacao else None,
            cnae=cnae.strip()[:40] if cnae else None,
            item_lc116=item_lc116.strip()[:20] if item_lc116 else None,
            ativo=True,
        )
        try:
            with db.begin_nested():
                db.add(forn)
                db.flush()
        except IntegrityError:
            if doc_limpo:
                forn = (
                    db.query(Fornecedor)
                    .filter(Fornecedor.tenant_id == tenant_id, Fornecedor.cnpj_cpf == doc_limpo)
                    .first()
                )
            if not forn:
                forn = (
                    db.query(Fornecedor)
                    .filter(Fornecedor.tenant_id == tenant_id, func.lower(Fornecedor.razao_social) == razao.lower())
                    .first()
                )
            if not forn:
                raise
    db.flush()
    return forn


def _to_cents(val: Any) -> int:
    return int((to_dec(val) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _ratear_cents_titulos(titulos: list[Titulo], extrato_valor: Decimal) -> list[dict]:
    ext_cents = _to_cents(extrato_valor)
    nominais_cents = [max(0, _to_cents(t.valor_liquido)) for t in titulos]
    soma_cents = sum(nominais_cents)
    diff_cents = ext_cents - soma_cents
    n = len(titulos)

    pagos_cents: list[int] = []
    if soma_cents > 0:
        alocado = 0
        for idx, nom in enumerate(nominais_cents):
            if idx == n - 1:
                pagos_cents.append(ext_cents - alocado)
            else:
                parc = int(round((Decimal(ext_cents) * Decimal(nom)) / Decimal(soma_cents)))
                pagos_cents.append(parc)
                alocado += parc
    else:
        base = ext_cents // n
        resto = ext_cents - base * n
        pagos_cents = [base + (1 if idx == n - 1 else 0) * resto for idx in range(n)]

    resultados = []
    for idx, t in enumerate(titulos):
        p_c = pagos_cents[idx]
        n_c = nominais_cents[idx]
        d_c = p_c - n_c if abs(diff_cents) > 5 else 0
        resultados.append({
            "titulo": t,
            "valor_pago": Decimal(p_c) / Decimal(100),
            "juros_multa": Decimal(d_c) / Decimal(100) if d_c > 0 else Decimal("0.00"),
            "desconto": Decimal(abs(d_c)) / Decimal(100) if d_c < 0 else Decimal("0.00"),
        })
    return resultados


def _obter_ou_criar_conta_ofx(db: Session, tenant_id: str) -> ContaBancaria:
    conta = (
        db.query(ContaBancaria)
        .filter(ContaBancaria.tenant_id == tenant_id, ContaBancaria.ativo.is_(True))
        .order_by(ContaBancaria.criado_em.asc())
        .first()
    )
    if conta:
        return conta
    emp = _resolver_empresa_padrao(db, tenant_id)
    conta = ContaBancaria(
        tenant_id=tenant_id,
        empresa_id=emp.id,
        banco_codigo="341",
        banco_nome="Banco Itaú",
        agencia=(config.ITAU_AGENCIA or "0000")[:10],
        conta=(config.ITAU_CONTA or "00000")[:20],
        dac=(config.ITAU_DAC or "0")[:2],
        saldo_inicial=Decimal("0.00"),
        ativo=True,
    )
    db.add(conta)
    db.flush()
    return conta


# ---------------------------------------------------------------------------
# AUTH COMPATÍVEL COM O PORTAL ERP
# ---------------------------------------------------------------------------

@router.post("/auth/login")
def compat_login(
    req: LoginV2Request,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    res = login_v2(req=req, request=request, response=response, db=db)
    usuario = dict(res.get("usuario") or {})
    usuario["perfil"] = res.get("role")
    usuario["role"] = res.get("role")
    if res.get("tenant"):
        usuario["tenant_id"] = res["tenant"]["id"]
    res["usuario"] = usuario
    return res


@router.get("/auth/me")
def compat_me(ctx: TenantContext = Depends(get_tenant_context)):
    return {
        "success": True,
        "usuario": {
            "id": ctx.usuario.id,
            "nome": ctx.usuario.nome,
            "email": ctx.usuario.email,
            "perfil": ctx.role,
            "role": ctx.role,
            "tenant_id": ctx.tenant_id,
        },
    }


@router.post("/auth/alterar-senha")
def compat_alterar_senha(
    dados: AlterarSenhaV2Request,
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    if len(dados.nova_senha or "") < 8:
        raise HTTPException(status_code=400, detail="A nova senha deve ter no mínimo 8 caracteres.")
    if not verificar_senha(dados.senha_atual, ctx.usuario.senha_hash):
        raise HTTPException(status_code=400, detail="Senha atual incorreta.")

    ctx.usuario.senha_hash = gerar_hash_senha(dados.nova_senha)
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "SENHA_ALTERADA_V2",
        "usuarios",
        ctx.usuario.id,
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "message": "Senha alterada com sucesso!"}


# ---------------------------------------------------------------------------
# FORNECEDORES
# ---------------------------------------------------------------------------

@router.get("/fornecedores")
def compat_listar_fornecedores(
    busca: Optional[str] = Query(None),
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    q = db.query(Fornecedor).filter(Fornecedor.tenant_id == ctx.tenant_id, Fornecedor.ativo.is_(True))
    if busca and busca.strip():
        termo = f"%{busca.strip()}%"
        q = q.filter(
            or_(
                Fornecedor.razao_social.ilike(termo),
                Fornecedor.nome_fantasia.ilike(termo),
                Fornecedor.cnpj_cpf.ilike(termo),
            )
        )
    itens = q.order_by(Fornecedor.razao_social.asc()).all()
    return {
        "success": True,
        "total": len(itens),
        "fornecedores": [
            {
                "id": f.id,
                "razao_social": f.razao_social,
                "nome_fantasia": f.nome_fantasia or f.razao_social,
                "cnpj_cpf": f.cnpj_cpf or "",
                "tipo": f.tipo,
                "categoria": f.categoria_padrao or "",
                "ccusto": "",
                "responsavel": "",
                "forma_pgto": f.forma_pgto_padrao or "",
                "pix_chave": f.pix_chave or "",
                "dados_banco": f.dados_banco or "",
                "cod_operacao": f.cod_operacao or "",
                "cnae": f.cnae or "",
                "item_lc116": f.item_lc116 or "",
                "optante_simples": f.optante_simples,
                "regime_tributario": f.regime_tributario,
                "ativo": 1 if f.ativo else 0,
            }
            for f in itens
        ],
    }


@router.post("/fornecedores")
def compat_salvar_fornecedor(
    f: FornecedorCompatRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    if not (f.razao_social or "").strip():
        raise HTTPException(status_code=400, detail="Razão Social é obrigatória.")

    forn = _upsert_fornecedor_v2(
        db=db,
        tenant_id=ctx.tenant_id,
        razao_social=f.razao_social,
        nome_fantasia=f.nome_fantasia,
        cnpj_cpf=f.cnpj_cpf,
        tipo=f.tipo or "FORNECEDOR",
        categoria=f.categoria,
        forma_pgto=f.forma_pgto,
        pix_chave=f.pix_chave,
        dados_banco=f.dados_banco,
        cod_operacao=f.cod_operacao,
        cnae=f.cnae,
        item_lc116=f.item_lc116,
        optante_simples=f.optante_simples,
        regime_tributario=f.regime_tributario,
    )
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "SALVAR_FORNECEDOR_ERP",
        "fornecedores",
        forn.id,
        dados_depois={"razao_social": forn.razao_social, "cnpj_cpf": forn.cnpj_cpf},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": forn.id}


# ---------------------------------------------------------------------------
# TÍTULOS A PAGAR & IMPOSTOS RETIDOS
# ---------------------------------------------------------------------------

@router.get("/titulos")
def compat_listar_titulos(
    status: Optional[str] = Query(None),
    filtro_data: Optional[str] = Query(None),
    busca: Optional[str] = Query(None),
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    hoje_date = date.today()
    semana_date = hoje_date + timedelta(days=7)
    mes_atual_str = hoje_date.strftime("%Y-%m")

    titulos_db = (
        db.query(Titulo)
        .filter(Titulo.tenant_id == ctx.tenant_id)
        .order_by(Titulo.dt_vencimento.asc())
        .all()
    )
    tit_ids = [t.id for t in titulos_db]

    impostos_map: dict[str, list[TituloImposto]] = {}
    itens_map: dict[str, list[TituloItem]] = {}
    rateios_count: dict[str, int] = {}
    anexos_count: dict[str, int] = {}
    if tit_ids:
        for imp in (
            db.query(TituloImposto)
            .filter(TituloImposto.tenant_id == ctx.tenant_id, TituloImposto.titulo_id.in_(tit_ids))
            .all()
        ):
            impostos_map.setdefault(imp.titulo_id, []).append(imp)
        for it in (
            db.query(TituloItem)
            .filter(TituloItem.tenant_id == ctx.tenant_id, TituloItem.titulo_id.in_(tit_ids))
            .order_by(TituloItem.numero_item.asc())
            .all()
        ):
            itens_map.setdefault(it.titulo_id, []).append(it)
        for tid, cnt in (
            db.query(TituloRateio.titulo_id, func.count(TituloRateio.id))
            .filter(TituloRateio.tenant_id == ctx.tenant_id, TituloRateio.titulo_id.in_(tit_ids))
            .group_by(TituloRateio.titulo_id)
            .all()
        ):
            rateios_count[tid] = cnt
        for tid, cnt in (
            db.query(DocumentoAnexo.entidade_id, func.count(DocumentoAnexo.id))
            .filter(
                DocumentoAnexo.tenant_id == ctx.tenant_id,
                DocumentoAnexo.entidade_tipo == "TITULO",
                DocumentoAnexo.entidade_id.in_(tit_ids),
            )
            .group_by(DocumentoAnexo.entidade_id)
            .all()
        ):
            anexos_count[tid] = cnt

    cc_map = {
        c.id: c.nome
        for c in db.query(CentroCusto).filter(CentroCusto.tenant_id == ctx.tenant_id).all()
    }
    pc_map = {
        p.id: f"{p.codigo} - {p.nome}"
        for p in db.query(PlanoConta).filter(PlanoConta.tenant_id == ctx.tenant_id).all()
    }

    saida = []
    for t in titulos_db:
        is_pago = bool(t.conciliado) or t.status in ("PAGO", "CONCILIADO")
        n_status = "CONCILIADO" if is_pago else "PENDENTE"
        n_situacao = "BAIXADO" if is_pago else "ABERTO"
        cc_label = pc_map.get(t.plano_conta_id or "") or cc_map.get(t.centro_custo_id or "") or "Geral"

        saida.append({
            "id": f"NOTA_{t.id}",
            "db_id": t.id,
            "v2_id": t.id,
            "empresa_id": t.empresa_id,
            "centro_custo_id": t.centro_custo_id,
            "plano_conta_id": t.plano_conta_id,
            "rateios_count": rateios_count.get(t.id, 0),
            "anexos_count": anexos_count.get(t.id, 0),
            "origem": "NOTA",
            "tipo": "FORNECEDOR",
            "tipo_doc": t.tipo_documento or "NFSE",
            "numero_doc": t.numero_documento or t.numero_tx,
            "cod_operacao": t.cod_operacao or "1.933 - Aquisição de serviço tributado pelo ISSQN",
            "cnae": t.cnae or "",
            "item_lc116": t.item_lc116 or "",
            "favorecido": t.favorecido_nome,
            "cnpj": t.favorecido_doc or "",
            "vencimento": t.dt_vencimento.isoformat(),
            "valor": float(to_dec(t.valor_liquido)),
            "valor_bruto": float(to_dec(t.valor_bruto)),
            "historico": t.observacao or "",
            "descricao": t.descricao or "",
            "ccusto": cc_label,
            "solicitante": t.solicitante or "",
            "forma_pgto": t.forma_pgto or "PIX",
            "linha_dig": t.cod_barras or "",
            "pix_chave": t.pix_chave or "",
            "status": n_status,
            "status_aprovacao": t.status,
            "situacao": n_situacao,
            "conciliada": is_pago,
            "data_baixa": t.dt_pagamento.isoformat() if t.dt_pagamento else None,
            "loteId": t.numero_tx,
            "valor_pago": float(to_dec(t.valor_pago)) if t.valor_pago else 0.0,
            "juros_multa": float(to_dec(t.juros_multa)),
            "desconto": float(to_dec(t.desconto)),
            "data_emissao": t.dt_emissao.isoformat() if t.dt_emissao else None,
            "itens": [
                {
                    "id": it.id,
                    "numero_item": it.numero_item,
                    "codigo": it.codigo_produto or "",
                    "descricao": it.descricao,
                    "ncm": it.ncm or "",
                    "cfop": it.cfop or "",
                    "unidade": it.unidade or "UN",
                    "quantidade": float(it.quantidade or 0),
                    "valor_unitario": float(it.valor_unitario or 0),
                    "valor_total": float(it.valor_total or 0),
                }
                for it in itens_map.get(t.id, [])
            ],
        })

        for imp in impostos_map.get(t.id, []):
            venc_imp = imp.dt_vencimento or t.dt_vencimento
            imp_pago = imp.status in ("PAGO", "CONCILIADO")
            saida.append({
                "id": f"IMP_{imp.id}",
                "db_id": imp.id,
                "v2_id": imp.id,
                "nota_pai_id": t.id,
                "origem": "IMPOSTO",
                "tipo": f"IMPOSTO_{imp.tipo}",
                "tipo_doc": "GUIA_TRIBUTO",
                "numero_doc": t.numero_documento or t.numero_tx,
                "favorecido": f"Guia de Retenção - {imp.tipo} ({t.favorecido_nome})",
                "cnpj": t.favorecido_doc or "",
                "vencimento": venc_imp.isoformat(),
                "valor": float(to_dec(imp.valor)),
                "valor_bruto": float(to_dec(t.valor_bruto)),
                "historico": f"Vlr. ref. retenção de {imp.tipo} s/ doc {t.numero_documento} de {t.favorecido_nome}",
                "descricao": f"Retenção Tributária {imp.tipo}",
                "ccusto": cc_label,
                "solicitante": t.solicitante or "",
                "forma_pgto": "BOLETO",
                "status": "CONCILIADO" if imp_pago else "PENDENTE",
                "status_aprovacao": imp.status,
                "situacao": "BAIXADO" if imp_pago else "ABERTO",
                "conciliada": imp_pago,
                "loteId": t.numero_tx,
            })

    if status:
        saida = [x for x in saida if x["status"].upper() == status.upper()]
    if filtro_data:
        filtrados = []
        for x in saida:
            vd = _parse_date(x.get("vencimento"))
            if not vd:
                continue
            if filtro_data == "vencidos" and vd < hoje_date and x["status"] == "PENDENTE":
                filtrados.append(x)
            elif filtro_data == "hoje" and vd == hoje_date:
                filtrados.append(x)
            elif filtro_data == "semana" and hoje_date <= vd <= semana_date:
                filtrados.append(x)
            elif filtro_data == "mes" and x["vencimento"][:7] == mes_atual_str:
                filtrados.append(x)
        saida = filtrados
    if busca:
        b_low = busca.lower()
        saida = [
            x
            for x in saida
            if b_low in str(x["favorecido"]).lower() or b_low in str(x["numero_doc"]).lower()
        ]

    saida.sort(key=lambda x: x.get("vencimento") or "9999-99-99")
    return {"success": True, "total": len(saida), "titulos": saida}


@router.post("/gravar-documento")
def compat_gravar_documento(
    doc: DocumentoCompatEntrada,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "SOLICITANTE")),
    db: Session = Depends(get_db),
):
    if doc.cnpj and doc.cnpj.strip() and not validar_documento(doc.cnpj):
        raise HTTPException(status_code=422, detail=f"CNPJ/CPF inválido: {doc.cnpj}")

    emp = _resolver_empresa_padrao(db, ctx.tenant_id)
    ccusto_obj, plano_obj = _resolver_plano_e_ccusto(db, ctx.tenant_id, emp.id, doc.ccusto)

    forn = _upsert_fornecedor_v2(
        db=db,
        tenant_id=ctx.tenant_id,
        razao_social=doc.favorecido,
        cnpj_cpf=doc.cnpj,
        tipo="PRESTADOR" if doc.tipo_doc == "NFSE" else "FORNECEDOR",
        categoria=doc.ccusto,
        forma_pgto=doc.forma_pgto,
        dados_banco=doc.dados_banco,
        cod_operacao=doc.cod_operacao,
        cnae=doc.cnae,
        item_lc116=doc.item_lc116,
    )

    bruto = to_dec(doc.valor_bruto)
    if bruto <= Decimal("0.00"):
        raise HTTPException(status_code=422, detail="O valor bruto deve ser maior que zero.")

    impostos_lista = doc.impostos or []
    total_impostos = sum((to_dec(i.valor) for i in impostos_lista), Decimal("0.00"))
    valor_liquido = max(Decimal("0.00"), bruto - total_impostos)

    dt_emissao = _parse_date(doc.data_emissao, date.today()) or date.today()
    dt_venc = _parse_date(doc.vencimento, dt_emissao) or dt_emissao

    parcelas_lista = [p for p in (doc.parcelas or []) if to_dec(p.valor) > Decimal("0.00")]
    itens_lista = doc.itens or []
    lote_tx = f"TX_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    titulos_criados: list[Titulo] = []

    if len(parcelas_lista) > 1:
        total_parc = len(parcelas_lista)
        for idx, parc in enumerate(parcelas_lista, start=1):
            v_parc_bruto = to_dec(parc.valor)
            v_parc_liq = max(Decimal("0.00"), v_parc_bruto - total_impostos) if idx == 1 else v_parc_bruto
            dt_venc_parc = _parse_date(parc.vencimento, dt_venc) or dt_venc
            num_doc_parc = f"{doc.numero_doc.strip()} ({idx}/{total_parc})"[:60]
            t_parc = Titulo(
                tenant_id=ctx.tenant_id,
                empresa_id=emp.id,
                fornecedor_id=forn.id,
                centro_custo_id=ccusto_obj.id if ccusto_obj else None,
                plano_conta_id=plano_obj.id if plano_obj else None,
                tipo_movimento="PAGAR",
                tipo_documento=(doc.tipo_doc or "NFSE").strip().upper()[:30],
                numero_tx=lote_tx,
                numero_documento=num_doc_parc,
                favorecido_nome=doc.favorecido.strip()[:200],
                favorecido_doc=limpar_documento(doc.cnpj) if doc.cnpj else None,
                dt_emissao=dt_emissao,
                dt_vencimento=dt_venc_parc,
                valor_bruto=v_parc_bruto,
                valor_liquido=v_parc_liq,
                status="PENDENTE",
                forma_pgto=(parc.forma_pgto or doc.forma_pgto or "PIX").strip().upper()[:30],
                cod_barras=((parc.linha_dig or (doc.linha_dig if idx == 1 else "") or "").strip()[:80] or None),
                cod_operacao=doc.cod_operacao[:120] if doc.cod_operacao else None,
                cnae=doc.cnae[:40] if doc.cnae else None,
                item_lc116=doc.item_lc116[:20] if doc.item_lc116 else None,
                descricao=doc.descricao,
                observacao=(f"[Parcela {idx}/{total_parc} • Total Doc: R$ {bruto:,.2f}] {doc.historico or ''}").strip(),
                solicitante=doc.solicitante[:120] if doc.solicitante else None,
            )
            db.add(t_parc)
            titulos_criados.append(t_parc)
        db.flush()
        titulo = titulos_criados[0]
    else:
        titulo = Titulo(
            tenant_id=ctx.tenant_id,
            empresa_id=emp.id,
            fornecedor_id=forn.id,
            centro_custo_id=ccusto_obj.id if ccusto_obj else None,
            plano_conta_id=plano_obj.id if plano_obj else None,
            tipo_movimento="PAGAR",
            tipo_documento=(doc.tipo_doc or "NFSE").strip().upper()[:30],
            numero_tx=lote_tx,
            numero_documento=doc.numero_doc.strip()[:60],
            favorecido_nome=doc.favorecido.strip()[:200],
            favorecido_doc=limpar_documento(doc.cnpj) if doc.cnpj else None,
            dt_emissao=dt_emissao,
            dt_vencimento=dt_venc,
            valor_bruto=bruto,
            valor_liquido=valor_liquido,
            status="PENDENTE",
            forma_pgto=(doc.forma_pgto or "PIX").strip().upper()[:30],
            cod_barras=doc.linha_dig.strip()[:80] if doc.linha_dig else None,
            cod_operacao=doc.cod_operacao[:120] if doc.cod_operacao else None,
            cnae=doc.cnae[:40] if doc.cnae else None,
            item_lc116=doc.item_lc116[:20] if doc.item_lc116 else None,
            descricao=doc.descricao,
            observacao=doc.historico,
            solicitante=doc.solicitante[:120] if doc.solicitante else None,
        )
        db.add(titulo)
        db.flush()
        titulos_criados.append(titulo)

    for imp in impostos_lista:
        db.add(
            TituloImposto(
                tenant_id=ctx.tenant_id,
                titulo_id=titulo.id,
                tipo=imp.tipo.strip().upper()[:20],
                aliquota=Decimal(str(imp.aliquota or 0)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                valor=to_dec(imp.valor),
                dt_vencimento=_parse_date(imp.dt_venc_imp, dt_venc),
                status="PENDENTE",
            )
        )

    for idx_it, it in enumerate(itens_lista, start=1):
        desc_it = (it.descricao or "").strip()[:300]
        if not desc_it:
            continue
        db.add(
            TituloItem(
                tenant_id=ctx.tenant_id,
                titulo_id=titulo.id,
                numero_item=it.numero_item or idx_it,
                codigo_produto=(it.codigo_produto or "").strip()[:60] or None,
                descricao=desc_it,
                ncm=(it.ncm or "").strip()[:16] or None,
                cfop=(it.cfop or "").strip()[:10] or None,
                unidade=(it.unidade or "UN").strip()[:16] or "UN",
                quantidade=Decimal(str(it.quantidade or 0)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                valor_unitario=Decimal(str(it.valor_unitario or 0)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
                valor_total=to_dec(it.valor_total),
                fornecedor_nome=doc.favorecido.strip()[:200],
                numero_nf=doc.numero_doc.strip()[:60],
                dt_emissao=dt_emissao,
            )
        )

    rateios_lista = [r for r in (doc.rateios or []) if to_dec(r.valor) > Decimal("0.00")]
    if rateios_lista:
        soma_rateio = sum((to_dec(r.valor) for r in rateios_lista), Decimal("0.00"))
        if abs(soma_rateio - bruto) > Decimal("0.05"):
            raise HTTPException(
                status_code=422,
                detail=f"A soma do rateio (R$ {soma_rateio}) deve bater com o valor bruto do documento (R$ {bruto}).",
            )
        for r_item in rateios_lista:
            val_r = to_dec(r_item.valor)
            loja_txt = (r_item.loja or "").strip()
            cat_txt = (r_item.categoria or "").strip()
            cc_r = _resolver_plano_e_ccusto(db, ctx.tenant_id, emp.id, loja_txt)[0] if loja_txt else ccusto_obj
            pc_r = _resolver_plano_e_ccusto(db, ctx.tenant_id, emp.id, cat_txt)[1] if cat_txt else plano_obj
            pct_r = ((val_r / bruto) * Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            rubrica_txt = (r_item.rubrica or loja_txt or cat_txt or "RATEIO").strip()[:60]
            obs_parts = [p for p in [cat_txt, (r_item.observacao or "").strip()] if p]
            db.add(
                TituloRateio(
                    tenant_id=ctx.tenant_id,
                    titulo_id=titulo.id,
                    centro_custo_id=cc_r.id if cc_r else None,
                    plano_conta_id=pc_r.id if pc_r else None,
                    rubrica=rubrica_txt,
                    valor=val_r,
                    percentual=pct_r,
                    observacao=" • ".join(obs_parts)[:255] or None,
                )
            )

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "GRAVAR_DOCUMENTO_ERP",
        "titulos",
        titulo.id,
        dados_depois={
            "numero_documento": titulo.numero_documento,
            "valor_bruto": str(bruto),
            "qtd_impostos": len(impostos_lista),
            "qtd_itens": len(itens_lista),
            "qtd_parcelas": len(titulos_criados),
            "qtd_rateios": len(rateios_lista),
        },
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "nota_id": titulo.id,
        "lote_tx": titulo.numero_tx,
        "valor_liquido_fornecedor": float(valor_liquido),
        "impostos_gravados": len(impostos_lista),
        "itens_gravados": len(itens_lista),
        "parcelas_geradas": len(titulos_criados),
        "rateios_gravados": len(rateios_lista),
    }


@router.put("/titulos/{id_ref}/editar")
@router.post("/titulos/{id_ref}/editar")
def compat_editar_titulo(
    id_ref: str,
    dados: TituloCompatEdicao,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    if dados.cnpj is not None and dados.cnpj.strip() and not validar_documento(dados.cnpj):
        raise HTTPException(status_code=422, detail=f"CNPJ/CPF inválido: {dados.cnpj}")

    tipo_ref, obj_id = _strip_ref_id(id_ref)
    if tipo_ref == "IMPOSTO":
        imp = (
            db.query(TituloImposto)
            .filter(TituloImposto.id == obj_id, TituloImposto.tenant_id == ctx.tenant_id)
            .first()
        )
        if not imp:
            raise HTTPException(status_code=404, detail="Imposto não localizado.")
        if dados.valor is not None:
            imp.valor = to_dec(dados.valor)
        if dados.vencimento:
            imp.dt_vencimento = _parse_date(dados.vencimento, imp.dt_vencimento)
        registrar_audit_v2(
            db,
            ctx.tenant_id,
            ctx.usuario,
            "EDITAR_IMPOSTO_ERP",
            "titulo_impostos",
            imp.id,
            ip=extrair_ip(request),
        )
        db.commit()
        return {"success": True, "message": "Guia tributária atualizada com sucesso!"}

    titulo = (
        db.query(Titulo)
        .filter(
            Titulo.tenant_id == ctx.tenant_id,
            or_(Titulo.id == obj_id, Titulo.numero_documento == id_ref, Titulo.numero_tx == id_ref),
        )
        .first()
    )
    if not titulo:
        raise HTTPException(status_code=404, detail="Título não localizado.")

    if dados.numero_doc:
        titulo.numero_documento = dados.numero_doc.strip()[:60]
    if dados.favorecido:
        titulo.favorecido_nome = dados.favorecido.strip()[:200]
    if dados.cnpj is not None and dados.cnpj.strip():
        titulo.favorecido_doc = limpar_documento(dados.cnpj)
    if dados.vencimento:
        dt_v = _parse_date(dados.vencimento)
        if dt_v:
            titulo.dt_vencimento = dt_v
    if dados.data_emissao:
        dt_e = _parse_date(dados.data_emissao)
        if dt_e:
            titulo.dt_emissao = dt_e
    if dados.cod_operacao is not None:
        titulo.cod_operacao = dados.cod_operacao[:120]
    if dados.cnae is not None:
        titulo.cnae = dados.cnae[:40]
    if dados.item_lc116 is not None:
        titulo.item_lc116 = dados.item_lc116[:20]
    if dados.categoria:
        titulo.tipo_documento = dados.categoria.strip().upper()[:30]
    if dados.descricao is not None:
        titulo.descricao = dados.descricao
    if dados.historico is not None:
        titulo.observacao = dados.historico
    if dados.solicitante is not None:
        titulo.solicitante = dados.solicitante[:120]
    if dados.forma_pgto:
        titulo.forma_pgto = dados.forma_pgto.strip().upper()[:30]
    if dados.linha_dig is not None:
        titulo.cod_barras = dados.linha_dig.strip()[:80]
    if dados.pix_chave is not None:
        titulo.pix_chave = dados.pix_chave.strip()[:140]
    if dados.ccusto:
        cc_obj, pc_obj = _resolver_plano_e_ccusto(db, ctx.tenant_id, titulo.empresa_id, dados.ccusto)
        if cc_obj:
            titulo.centro_custo_id = cc_obj.id
        if pc_obj:
            titulo.plano_conta_id = pc_obj.id

    if dados.valor is not None:
        impostos = (
            db.query(TituloImposto)
            .filter(TituloImposto.titulo_id == titulo.id, TituloImposto.tenant_id == ctx.tenant_id)
            .all()
        )
        tot_imp = sum((to_dec(i.valor) for i in impostos), Decimal("0.00"))
        bruto = to_dec(dados.valor)
        titulo.valor_bruto = bruto
        titulo.valor_liquido = max(Decimal("0.00"), bruto - tot_imp)

    if dados.favorecido:
        _upsert_fornecedor_v2(
            db=db,
            tenant_id=ctx.tenant_id,
            razao_social=dados.favorecido,
            cnpj_cpf=dados.cnpj,
            categoria=dados.categoria or dados.ccusto,
            forma_pgto=dados.forma_pgto,
            pix_chave=dados.pix_chave,
            dados_banco=dados.dados_banco,
            cod_operacao=dados.cod_operacao,
            cnae=dados.cnae,
            item_lc116=dados.item_lc116,
        )

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "EDITAR_TITULO_ERP",
        "titulos",
        titulo.id,
        dados_depois={"valor_bruto": str(titulo.valor_bruto), "valor_liquido": str(titulo.valor_liquido)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "message": "Título atualizado com sucesso!"}


@router.post("/titulos/{id_ref}/baixa")
def compat_baixar_titulo(
    id_ref: str,
    request: Request,
    forcar_baixa: bool = Query(False),
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    tipo_ref, obj_id = _strip_ref_id(id_ref)
    hoje = date.today()

    if tipo_ref == "IMPOSTO":
        imp = (
            db.query(TituloImposto)
            .filter(TituloImposto.id == obj_id, TituloImposto.tenant_id == ctx.tenant_id)
            .first()
        )
        if not imp:
            raise HTTPException(status_code=404, detail="Imposto não encontrado.")
        novo_status = "PAGO" if (forcar_baixa or imp.status != "PAGO") else "PENDENTE"
        imp.status = novo_status
        registrar_audit_v2(
            db,
            ctx.tenant_id,
            ctx.usuario,
            "BAIXAR_IMPOSTO_ERP",
            "titulo_impostos",
            imp.id,
            dados_depois={"status": novo_status},
            ip=extrair_ip(request),
        )
        db.commit()
        return {"success": True, "status": "CONCILIADO" if novo_status == "PAGO" else "PENDENTE"}

    titulo = (
        db.query(Titulo)
        .filter(Titulo.id == obj_id, Titulo.tenant_id == ctx.tenant_id)
        .first()
    )
    if not titulo:
        raise HTTPException(status_code=404, detail="Título não encontrado.")

    ja_pago = bool(titulo.conciliado) or titulo.status in ("PAGO", "CONCILIADO")
    liquidar = True if forcar_baixa else (not ja_pago)
    if liquidar:
        titulo.status = "PAGO"
        titulo.conciliado = True
        titulo.dt_pagamento = hoje
        titulo.valor_pago = to_dec(titulo.valor_liquido)
    else:
        titulo.status = "PENDENTE"
        titulo.conciliado = False
        titulo.dt_pagamento = None
        titulo.valor_pago = Decimal("0.00")

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "BAIXAR_TITULO_ERP" if liquidar else "ESTORNAR_BAIXA_ERP",
        "titulos",
        titulo.id,
        dados_depois={"status": titulo.status, "conciliado": titulo.conciliado},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "status": "CONCILIADO" if liquidar else "PENDENTE"}


@router.delete("/titulos/{id_ref}")
def compat_excluir_titulo(
    id_ref: str,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    tipo_ref, obj_id = _strip_ref_id(id_ref)
    if tipo_ref == "IMPOSTO":
        imp = (
            db.query(TituloImposto)
            .filter(TituloImposto.id == obj_id, TituloImposto.tenant_id == ctx.tenant_id)
            .first()
        )
        if not imp:
            raise HTTPException(status_code=404, detail="Guia tributária não localizada.")
        db.delete(imp)
        registrar_audit_v2(db, ctx.tenant_id, ctx.usuario, "EXCLUIR_IMPOSTO_ERP", "titulo_impostos", obj_id, ip=extrair_ip(request))
        db.commit()
        return {"success": True, "message": "Guia tributária excluída com sucesso."}

    titulo = db.query(Titulo).filter(Titulo.id == obj_id, Titulo.tenant_id == ctx.tenant_id).first()
    if not titulo:
        raise HTTPException(status_code=404, detail="Título não localizado.")

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "EXCLUIR_TITULO_ERP",
        "titulos",
        titulo.id,
        dados_antes={"numero_documento": titulo.numero_documento, "valor_bruto": str(titulo.valor_bruto)},
        ip=extrair_ip(request),
    )
    db.delete(titulo)
    db.commit()
    return {"success": True, "message": "Título excluído com sucesso."}


# ---------------------------------------------------------------------------
# DESPESAS FIXAS / PREVISÕES
# ---------------------------------------------------------------------------

@router.get("/previsoes")
def compat_listar_previsoes(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    recs = (
        db.query(DespesaRecorrente)
        .filter(DespesaRecorrente.tenant_id == ctx.tenant_id, DespesaRecorrente.ativo.is_(True))
        .order_by(DespesaRecorrente.dia_vencimento.asc())
        .all()
    )
    cc_map = {c.id: c.nome for c in db.query(CentroCusto).filter(CentroCusto.tenant_id == ctx.tenant_id).all()}
    hoje = date.today()
    previsoes = []
    for r in recs:
        dia = min(max(1, r.dia_vencimento), 28)
        previsoes.append({
            "id": r.id,
            "nome": r.nome,
            "favorecido": r.nome,
            "cnpj": "",
            "mesRef": f"{hoje.year}-{hoje.month:02d}",
            "vencimento": f"{hoje.year}-{hoje.month:02d}-{dia:02d}",
            "valor": float(to_dec(r.valor_estimado)),
            "tipoValor": r.tipo_valor,
            "ccusto": cc_map.get(r.centro_custo_id or "") or "Geral",
            "status": "PREVISTO",
            "obs": r.indice_reajuste or "",
        })
    return {"success": True, "total": len(previsoes), "previsoes": previsoes}


@router.post("/previsoes")
def compat_criar_previsao(
    prev: PrevisaoCompatEntrada,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    emp = _resolver_empresa_padrao(db, ctx.tenant_id)
    cc_obj, pc_obj = _resolver_plano_e_ccusto(db, ctx.tenant_id, emp.id, prev.ccusto)
    dt_v = _parse_date(prev.vencimento, date.today()) or date.today()

    rec = DespesaRecorrente(
        tenant_id=ctx.tenant_id,
        empresa_id=emp.id,
        centro_custo_id=cc_obj.id if cc_obj else None,
        plano_conta_id=pc_obj.id if pc_obj else None,
        nome=prev.nome.strip()[:180],
        tipo_valor=(prev.tipoValor or "FIXO").strip().upper()[:20],
        valor_estimado=to_dec(prev.valor),
        periodicidade="MENSAL",
        dia_vencimento=dt_v.day,
        indice_reajuste=(prev.obs or "")[:20] or None,
        ativo=True,
    )
    db.add(rec)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_PREVISAO_ERP",
        "despesas_recorrentes",
        rec.id,
        dados_depois={"nome": rec.nome, "valor": str(rec.valor_estimado)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": rec.id}


# ---------------------------------------------------------------------------
# ADIANTAMENTOS
# ---------------------------------------------------------------------------

@router.get("/adiantamentos")
def compat_listar_adiantamentos(
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
    comps_map: dict[str, list] = {}
    for c in comps:
        comps_map.setdefault(c.adiantamento_id, []).append(c)

    cc_map = {c.id: c.nome for c in db.query(CentroCusto).filter(CentroCusto.tenant_id == ctx.tenant_id).all()}
    lista = []
    for a in ads:
        gastos = comps_map.get(a.id, [])
        tot_gastos = sum((to_dec(g.valor) for g in gastos), Decimal("0.00"))
        saldo = to_dec(a.valor_concedido) - tot_gastos
        lista.append({
            "id": a.id,
            "solicitante": a.solicitante,
            "ccusto": cc_map.get(a.centro_custo_id or "") or "Geral",
            "dataLib": a.data_liberacao.isoformat(),
            "valor": float(to_dec(a.valor_concedido)),
            "forma": a.forma,
            "motivo": a.motivo or "",
            "status": a.status,
            "dataFechamento": a.data_fechamento.isoformat() if a.data_fechamento else None,
            "totalComprovado": float(tot_gastos),
            "saldo": float(saldo),
            "gastos": [
                {
                    "id": g.id,
                    "data": g.data_gasto.isoformat(),
                    "tipo": g.tipo_doc,
                    "desc": g.descricao,
                    "valor": float(to_dec(g.valor)),
                }
                for g in gastos
            ],
        })
    return {"success": True, "total": len(lista), "adiantamentos": lista}


@router.post("/adiantamentos")
def compat_criar_adiantamento(
    ad: AdiantamentoCompatEntrada,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    emp = _resolver_empresa_padrao(db, ctx.tenant_id)
    cc_obj, _ = _resolver_plano_e_ccusto(db, ctx.tenant_id, emp.id, ad.ccusto)
    dt_lib = _parse_date(ad.data, date.today()) or date.today()
    novo = Adiantamento(
        tenant_id=ctx.tenant_id,
        empresa_id=emp.id,
        centro_custo_id=cc_obj.id if cc_obj else None,
        solicitante=ad.solicitante.strip()[:150],
        data_liberacao=dt_lib,
        valor_concedido=to_dec(ad.valor),
        forma=(ad.forma or "PIX").strip().upper()[:30],
        motivo=ad.motivo,
        status="ABERTO",
    )
    db.add(novo)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_ADIANTAMENTO_ERP",
        "adiantamentos",
        novo.id,
        dados_depois={"solicitante": novo.solicitante, "valor": str(novo.valor_concedido)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": novo.id}


@router.post("/adiantamentos/comprovantes")
def compat_lancar_comprovante(
    comp: ComprovanteCompatEntrada,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "SOLICITANTE")),
    db: Session = Depends(get_db),
):
    ad = (
        db.query(Adiantamento)
        .filter(Adiantamento.id == str(comp.adiantamento_id), Adiantamento.tenant_id == ctx.tenant_id)
        .first()
    )
    if not ad:
        raise HTTPException(status_code=404, detail="Adiantamento não encontrado.")
    novo = AdiantamentoComprovante(
        tenant_id=ctx.tenant_id,
        adiantamento_id=ad.id,
        data_gasto=_parse_date(comp.data, date.today()) or date.today(),
        tipo_doc=comp.tipo.strip().upper()[:40],
        descricao=comp.descricao.strip()[:255],
        valor=to_dec(comp.valor),
    )
    db.add(novo)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "LANCAR_COMPROVANTE_ERP",
        "adiantamento_comprovantes",
        novo.id,
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": novo.id}


@router.post("/adiantamentos/{ad_id}/fechar")
def compat_fechar_adiantamento(
    ad_id: str,
    fechamento: FechamentoCompatEntrada,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    ad = (
        db.query(Adiantamento)
        .filter(Adiantamento.id == ad_id, Adiantamento.tenant_id == ctx.tenant_id)
        .first()
    )
    if not ad:
        raise HTTPException(status_code=404, detail="Adiantamento não encontrado.")
    hoje = date.today()
    status_final = f"QUITADO_{fechamento.tipo_fechamento.upper()}"[:30]
    ad.status = status_final
    ad.data_fechamento = hoje

    if fechamento.gerar_reembolso_cp and fechamento.saldo < 0:
        dif = to_dec(abs(fechamento.saldo))
        db.add(
            Titulo(
                tenant_id=ctx.tenant_id,
                empresa_id=ad.empresa_id,
                centro_custo_id=ad.centro_custo_id,
                tipo_movimento="PAGAR",
                tipo_documento="DESPESA_INTERNA",
                numero_tx=f"REEMB_{ad.id[:8]}",
                numero_documento=f"REEMB-{ad.id[:6].upper()}",
                favorecido_nome=ad.solicitante,
                dt_emissao=hoje,
                dt_vencimento=hoje,
                valor_bruto=dif,
                valor_liquido=dif,
                status="PENDENTE",
                forma_pgto="PIX",
                observacao=f"Reembolso de saldo complementar do adiantamento #{ad.id[:8]}",
            )
        )
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "FECHAR_ADIANTAMENTO_ERP",
        "adiantamentos",
        ad.id,
        dados_depois={"status": status_final},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "status": status_final}


# ---------------------------------------------------------------------------
# CONCILIAÇÃO BANCÁRIA (GRUPO & OFX)
# ---------------------------------------------------------------------------

@router.post("/conciliar-grupo")
def compat_conciliar_grupo(
    req: ConciliacaoGrupoCompatRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    if not req.titulo_ids:
        raise HTTPException(status_code=400, detail="Selecione ao menos um título para conciliar.")

    ids_limpos = list(dict.fromkeys(_strip_ref_id(str(x))[1] for x in req.titulo_ids))
    titulos = (
        db.query(Titulo)
        .filter(Titulo.tenant_id == ctx.tenant_id, Titulo.id.in_(ids_limpos))
        .all()
    )
    if len(titulos) != len(ids_limpos):
        raise HTTPException(status_code=404, detail="Um ou mais títulos não foram encontrados neste workspace.")

    extrato_val = to_dec(req.extrato_valor)
    soma = sum((to_dec(t.valor_liquido) for t in titulos), Decimal("0.00"))
    dif = float(extrato_val - soma)
    dt_pgto = _parse_date(req.extrato_data, date.today()) or date.today()
    rateados = _ratear_cents_titulos(titulos, extrato_val)

    fitid = (req.extrato_fitid or "").strip()[:120]
    tx_db = None
    if fitid:
        conta = _obter_ou_criar_conta_ofx(db, ctx.tenant_id)
        tx_db = (
            db.query(ExtratoTransacao)
            .filter(
                ExtratoTransacao.tenant_id == ctx.tenant_id,
                ExtratoTransacao.conta_bancaria_id == conta.id,
                ExtratoTransacao.fitid == fitid,
            )
            .first()
        )
        if not tx_db:
            extrato = ExtratoBancario(
                tenant_id=ctx.tenant_id,
                conta_bancaria_id=conta.id,
                origem="OFX_MANUAL",
                arquivo_nome="conciliacao-grupo",
            )
            db.add(extrato)
            db.flush()
            tx_db = ExtratoTransacao(
                tenant_id=ctx.tenant_id,
                extrato_id=extrato.id,
                conta_bancaria_id=conta.id,
                fitid=fitid,
                data_movimento=dt_pgto,
                tipo="DEBIT",
                valor=extrato_val,
                memo=(req.extrato_memo or "")[:255],
                conciliado=True,
            )
            db.add(tx_db)
            db.flush()
        else:
            tx_db.conciliado = True

    itens_resp = []
    for item in rateados:
        t = item["titulo"]
        t.status = "PAGO"
        t.conciliado = True
        t.dt_pagamento = dt_pgto
        t.valor_pago = item["valor_pago"]
        t.juros_multa = item["juros_multa"]
        t.desconto = item["desconto"]
        if tx_db:
            db.add(
                ConciliacaoItem(
                    tenant_id=ctx.tenant_id,
                    extrato_transacao_id=tx_db.id,
                    titulo_id=t.id,
                    valor_alocado=item["valor_pago"],
                    juros_alocado=item["juros_multa"],
                    desconto_alocado=item["desconto"],
                    metodo="MANUAL_N1",
                )
            )
        itens_resp.append({
            "id": t.id,
            "fornecedor": t.favorecido_nome,
            "numero_nf": t.numero_documento,
            "valor_pago_rateado": float(item["valor_pago"]),
        })

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CONCILIAR_GRUPO_ERP",
        "titulos",
        ",".join(ids_limpos[:3]),
        dados_depois={"qtd_titulos": len(titulos), "valor_extrato": str(extrato_val)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "qtd_titulos": len(titulos),
        "soma_titulos": float(soma),
        "valor_extrato": float(extrato_val),
        "diferenca": dif,
        "titulos": itens_resp,
    }


def _parse_debitos_ofx(conteudo: bytes) -> list[dict]:
    ofx = ofxparse.OfxParser.parse(io.BytesIO(conteudo))
    debitos = []
    for account in ofx.accounts:
        for tx in account.statement.transactions:
            if tx.amount < 0:
                debitos.append({
                    "data": tx.date.date() if isinstance(tx.date, datetime) else tx.date,
                    "valor": to_dec(abs(tx.amount)),
                    "descricao": (tx.memo or tx.payee or "Débito em Extrato")[:255],
                    "fitid": str(tx.id or "").strip()[:120],
                })
    return debitos


@router.post("/conciliar-ofx")
async def compat_conciliar_ofx(
    request: Request,
    arquivo: UploadFile = File(...),
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    conteudo = await arquivo.read()
    if len(conteudo) > MAX_OFX_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo OFX excede o limite máximo de 10 MB.")

    debitos = await run_in_threadpool(_parse_debitos_ofx, conteudo)
    conta = _obter_ou_criar_conta_ofx(db, ctx.tenant_id)

    extrato = ExtratoBancario(
        tenant_id=ctx.tenant_id,
        conta_bancaria_id=conta.id,
        origem="OFX",
        arquivo_nome=(arquivo.filename or "extrato.ofx")[:200],
    )
    db.add(extrato)
    db.flush()

    fitids_existentes = {
        r[0]
        for r in db.query(ExtratoTransacao.fitid)
        .filter(ExtratoTransacao.tenant_id == ctx.tenant_id, ExtratoTransacao.conta_bancaria_id == conta.id)
        .all()
    }
    pendentes = (
        db.query(Titulo)
        .filter(
            Titulo.tenant_id == ctx.tenant_id,
            Titulo.tipo_movimento == "PAGAR",
            Titulo.conciliado.is_(False),
            Titulo.status.in_(["PENDENTE", "APROVADO"]),
        )
        .all()
    )

    usados: set[str] = set()
    baixados = []
    baixados_soma = []
    baixados_juros = []
    ja_conciliados = []
    nao_localizados = []

    for idx, deb in enumerate(debitos):
        fitid = deb["fitid"] or f"OFX_{extrato.id[:8]}_{idx}"
        if fitid in fitids_existentes:
            ja_conciliados.append({"data": deb["data"].isoformat(), "valor": float(deb["valor"]), "fitid": fitid})
            continue
        fitids_existentes.add(fitid)

        val_dec = deb["valor"]
        dt_deb = deb["data"] or date.today()
        tx_row = ExtratoTransacao(
            tenant_id=ctx.tenant_id,
            extrato_id=extrato.id,
            conta_bancaria_id=conta.id,
            fitid=fitid,
            data_movimento=dt_deb,
            tipo="DEBIT",
            valor=val_dec,
            memo=deb["descricao"],
            conciliado=False,
        )
        db.add(tx_row)
        db.flush()

        # 1) Exato
        cands = [
            p
            for p in pendentes
            if p.id not in usados and abs(to_dec(p.valor_liquido) - val_dec) <= Decimal("0.05")
        ]
        if cands:
            cands.sort(key=lambda t: abs((dt_deb - t.dt_vencimento).days))
            alvo = cands[0]
            alvo.status = "PAGO"
            alvo.conciliado = True
            alvo.dt_pagamento = dt_deb
            alvo.valor_pago = val_dec
            tx_row.conciliado = True
            db.add(
                ConciliacaoItem(
                    tenant_id=ctx.tenant_id,
                    extrato_transacao_id=tx_row.id,
                    titulo_id=alvo.id,
                    valor_alocado=val_dec,
                    metodo="AUTO_OFX_EXATO",
                )
            )
            usados.add(alvo.id)
            baixados.append({"id": alvo.id, "fornecedor": alvo.favorecido_nome, "valor": float(val_dec)})
            continue

        # 2) N:1 até 3 títulos
        disps = [
            p for p in pendentes if p.id not in usados and Decimal("0.00") < to_dec(p.valor_liquido) <= val_dec + Decimal("0.10")
        ]
        disps.sort(key=lambda t: abs((dt_deb - t.dt_vencimento).days))
        disps = disps[:30]
        achou_combo = False
        for n in (2, 3):
            if achou_combo or len(disps) < n:
                break
            for combo in combinations(disps, n):
                soma_c = sum((to_dec(c.valor_liquido) for c in combo), Decimal("0.00"))
                if abs(soma_c - val_dec) <= Decimal("0.10"):
                    combo_list = list(combo)
                    for item in _ratear_cents_titulos(combo_list, val_dec):
                        tit = item["titulo"]
                        tit.status = "PAGO"
                        tit.conciliado = True
                        tit.dt_pagamento = dt_deb
                        tit.valor_pago = item["valor_pago"]
                        tit.juros_multa = item["juros_multa"]
                        tit.desconto = item["desconto"]
                        usados.add(tit.id)
                        db.add(
                            ConciliacaoItem(
                                tenant_id=ctx.tenant_id,
                                extrato_transacao_id=tx_row.id,
                                titulo_id=tit.id,
                                valor_alocado=item["valor_pago"],
                                juros_alocado=item["juros_multa"],
                                desconto_alocado=item["desconto"],
                                metodo="AUTO_OFX_N1",
                            )
                        )
                    tx_row.conciliado = True
                    baixados_soma.append({"titulo_ids": [c.id for c in combo_list], "valor": float(val_dec)})
                    achou_combo = True
                    break
        if achou_combo:
            continue

        # 3) Tolerância 5% + proximidade temporal/fornecedor
        memo_up = (deb["descricao"] or "").upper()
        matched_juros = False
        for p in pendentes:
            if p.id in usados:
                continue
            v_ref = to_dec(p.valor_liquido)
            if v_ref <= Decimal("0.00"):
                continue
            diff = val_dec - v_ref
            pct = (abs(diff) / v_ref) * Decimal("100")
            if abs(diff) > Decimal("0.05") and pct <= Decimal("5.00"):
                perto_data = abs((dt_deb - p.dt_vencimento).days) <= 45
                tokens = [tok for tok in re.split(r"\W+", (p.favorecido_nome or "").upper()) if len(tok) >= 4]
                if not (perto_data or any(tok in memo_up for tok in tokens)):
                    continue
                p.status = "PAGO"
                p.conciliado = True
                p.dt_pagamento = dt_deb
                p.valor_pago = val_dec
                p.juros_multa = diff if diff > 0 else Decimal("0.00")
                p.desconto = abs(diff) if diff < 0 else Decimal("0.00")
                tx_row.conciliado = True
                db.add(
                    ConciliacaoItem(
                        tenant_id=ctx.tenant_id,
                        extrato_transacao_id=tx_row.id,
                        titulo_id=p.id,
                        valor_alocado=val_dec,
                        juros_alocado=p.juros_multa,
                        desconto_alocado=p.desconto,
                        metodo="AUTO_OFX_JUROS",
                    )
                )
                usados.add(p.id)
                baixados_juros.append({"id": p.id, "fornecedor": p.favorecido_nome, "valor": float(val_dec)})
                matched_juros = True
                break
        if not matched_juros:
            nao_localizados.append({"data": dt_deb.isoformat(), "valor": float(val_dec), "fitid": fitid})

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CONCILIAR_OFX_ERP",
        "extratos_bancarios",
        extrato.id,
        dados_depois={"conciliados": len(baixados) + len(baixados_soma) + len(baixados_juros)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "total_debitos_ofx": len(debitos),
        "total_conciliados": len(baixados) + len(baixados_soma) + len(baixados_juros),
        "total_conciliados_exato": len(baixados),
        "total_conciliados_soma": len(baixados_soma),
        "total_conciliados_juros": len(baixados_juros),
        "total_ja_conciliados": len(ja_conciliados),
        "titulos_baixados": baixados,
        "titulos_baixados_soma": baixados_soma,
        "titulos_baixados_juros": baixados_juros,
        "ja_conciliados": ja_conciliados,
        "nao_localizados": nao_localizados,
    }


# ---------------------------------------------------------------------------
# CNAB 240 ITAÚ & WHATSAPP DIRETORIA
# ---------------------------------------------------------------------------

@router.get("/exportar-lote-itau")
@router.post("/exportar-lote-itau")
def compat_exportar_lote_itau(
    request: Request,
    data_pagamento: Optional[str] = Query(None),
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    emp = _resolver_empresa_padrao(db, ctx.tenant_id)
    conta = (
        db.query(ContaBancaria)
        .filter(ContaBancaria.tenant_id == ctx.tenant_id, ContaBancaria.empresa_id == emp.id, ContaBancaria.ativo.is_(True))
        .first()
    )
    agencia = conta.agencia if conta else config.ITAU_AGENCIA
    num_conta = conta.conta if conta else config.ITAU_CONTA
    dac = conta.dac if conta else (config.ITAU_DAC or "0")
    if not agencia or not num_conta:
        raise HTTPException(
            status_code=400,
            detail="Cadastre uma Conta Bancária da empresa na Central SaaS v2 (ou defina ITAU_AGENCIA/ITAU_CONTA) para gerar remessa CNAB 240.",
        )

    q = db.query(Titulo).filter(
        Titulo.tenant_id == ctx.tenant_id,
        Titulo.empresa_id == emp.id,
        Titulo.tipo_movimento == "PAGAR",
        Titulo.conciliado.is_(False),
        Titulo.status.in_(["PENDENTE", "APROVADO"]),
    )
    if data_pagamento:
        dt_alvo = _parse_date(data_pagamento)
        if dt_alvo:
            q = q.filter(Titulo.dt_vencimento == dt_alvo)

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
        raise HTTPException(status_code=404, detail="Nenhum título pendente com código de barras válido encontrado.")

    dt_pgto = (data_pagamento or date.today().isoformat())[:10]
    try:
        remessa = gerar_cnab240_sispag_itau(
            validos,
            cnpj_empresa=emp.cnpj,
            razao_social=emp.razao_social,
            agencia=agencia,
            conta=num_conta,
            dac=dac,
            data_pagamento=dt_pgto,
            cidade=emp.cidade,
            cep=emp.cep,
            uf=emp.uf,
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    nome_arquivo = f"LOTE_ITAU_{dt_pgto.replace('-', '')}.REM"
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "EXPORTAR_CNAB_ERP",
        "titulos",
        nome_arquivo,
        dados_depois={"qtd_titulos": len(validos)},
        ip=extrair_ip(request),
    )
    db.commit()
    return Response(
        content=remessa,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.post("/disparar-whatsapp-diretoria")
def compat_disparar_whatsapp(
    request: Request,
    numero: Optional[str] = Query(None),
    data_pagamento: Optional[str] = Query(None),
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    if not config.WHATSAPP_API_URL:
        raise HTTPException(
            status_code=503,
            detail="Integração WhatsApp não configurada. Defina WHATSAPP_API_URL no ambiente.",
        )
    destino = (numero or config.WHATSAPP_DEFAULT_NUMBER or "").strip()
    if not destino:
        raise HTTPException(status_code=400, detail="Número de destino do WhatsApp não informado.")

    dt_alvo = _parse_date(data_pagamento, date.today()) or date.today()
    titulos = (
        db.query(Titulo)
        .filter(
            Titulo.tenant_id == ctx.tenant_id,
            Titulo.tipo_movimento == "PAGAR",
            Titulo.conciliado.is_(False),
            Titulo.status.in_(["PENDENTE", "APROVADO", "AGUARDANDO_APROVACAO"]),
            Titulo.dt_vencimento == dt_alvo,
        )
        .all()
    )
    total = sum((to_dec(t.valor_liquido) for t in titulos), Decimal("0.00"))
    msg = (
        f"*{config.COMPANY_DISPLAY_NAME} - AUTORIZAÇÃO DE PAGAMENTOS*\n"
        f"Data: {dt_alvo.isoformat()}\n\n"
        f"Total a Pagar: R$ {float(total):,.2f}\n"
        f"Quantidade de Títulos: {len(titulos)}\n\n"
        f"Acesse o painel para conferência e aprovação:\n"
        f"{config.APP_PUBLIC_URL}\n"
    )
    headers = {"Content-Type": "application/json"}
    if config.WHATSAPP_API_TOKEN:
        headers["Authorization"] = f"Bearer {config.WHATSAPP_API_TOKEN}"
    resp = requests.post(config.WHATSAPP_API_URL, json={"number": destino, "message": msg}, headers=headers, timeout=10)
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "DISPARAR_WHATSAPP_ERP",
        "notificacoes",
        destino,
        dados_depois={"qtd_titulos": len(titulos), "total": str(total)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": resp.status_code < 400,
        "mensagem_enviada": msg,
        "whatsapp_status": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
    }


def _diretorio_anexos_base() -> Path:
    base = Path("/app/data/anexos") if Path("/app/data").exists() else Path(__file__).resolve().parents[3] / "data" / "anexos"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _supabase_storage_cfg() -> tuple[str, str, str]:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    bucket = (os.getenv("SUPABASE_STORAGE_BUCKET") or "dossie-anexos").strip()
    return url, key, bucket


@router.get("/titulos/{id_ref}/anexos")
def compat_listar_anexos_titulo(
    id_ref: str,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    _, raw_id = _strip_ref_id(id_ref)
    anexos = (
        db.query(DocumentoAnexo)
        .filter(
            DocumentoAnexo.tenant_id == ctx.tenant_id,
            DocumentoAnexo.entidade_tipo == "TITULO",
            DocumentoAnexo.entidade_id == raw_id,
        )
        .order_by(DocumentoAnexo.criado_em.desc())
        .all()
    )
    return {
        "success": True,
        "anexos": [
            {
                "id": a.id,
                "nome_original": a.nome_original,
                "mime_type": a.mime_type,
                "tamanho_bytes": a.tamanho_bytes,
                "criado_em": a.criado_em.isoformat() if a.criado_em else "",
            }
            for a in anexos
        ],
    }


@router.post("/titulos/{id_ref}/anexos")
async def compat_upload_anexo_titulo(
    id_ref: str,
    request: Request,
    file: UploadFile = File(...),
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO", "SOLICITANTE")),
    db: Session = Depends(get_db),
):
    _, raw_id = _strip_ref_id(id_ref)
    titulo = db.query(Titulo).filter(Titulo.tenant_id == ctx.tenant_id, Titulo.id == raw_id).first()
    if not titulo:
        raise HTTPException(status_code=404, detail="Título não encontrado.")

    nome_orig = (file.filename or "documento.pdf").strip()[:255]
    ext = Path(nome_orig).suffix.lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".xml"):
        raise HTTPException(status_code=422, detail="Formato não permitido. Use PDF, JPG, PNG, WEBP ou XML.")

    conteudo = await file.read()
    if not conteudo:
        raise HTTPException(status_code=422, detail="Arquivo vazio.")
    if len(conteudo) > 10 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="Arquivo excede o limite de 10 MB.")

    sha = hashlib.sha256(conteudo).hexdigest()
    anexo_uid = str(uuid.uuid4())
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", nome_orig)[:120]
    rel_key = f"{ctx.tenant_id}/{titulo.id}/{anexo_uid}_{safe_name}"
    mime_final = (file.content_type or "application/octet-stream").lower()[:120]

    sb_url, sb_key, sb_bucket = _supabase_storage_cfg()
    if sb_url and sb_key:
        up_resp = requests.post(
            f"{sb_url}/storage/v1/object/{sb_bucket}/{rel_key}",
            headers={
                "Authorization": f"Bearer {sb_key}",
                "apikey": sb_key,
                "Content-Type": mime_final,
                "x-upsert": "true",
            },
            data=conteudo,
            timeout=25,
        )
        if up_resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Falha ao gravar no Supabase Storage: {up_resp.text[:200]}")
    else:
        dest = _diretorio_anexos_base() / rel_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(conteudo)

    anexo = DocumentoAnexo(
        id=anexo_uid,
        tenant_id=ctx.tenant_id,
        empresa_id=titulo.empresa_id,
        entidade_tipo="TITULO",
        entidade_id=titulo.id,
        storage_key=rel_key,
        nome_original=nome_orig,
        mime_type=mime_final,
        tamanho_bytes=len(conteudo),
        sha256=sha,
        uploaded_by=ctx.usuario.id,
    )
    db.add(anexo)
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "UPLOAD_ANEXO_TITULO",
        "documentos_anexos",
        anexo.id,
        dados_depois={"titulo_id": titulo.id, "nome_original": nome_orig, "tamanho_bytes": len(conteudo)},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "anexo": {
            "id": anexo.id,
            "nome_original": anexo.nome_original,
            "mime_type": anexo.mime_type,
            "tamanho_bytes": anexo.tamanho_bytes,
            "criado_em": anexo.criado_em.isoformat() if anexo.criado_em else "",
        },
    }


@router.get("/anexos/{anexo_id}/download")
def compat_download_anexo(
    anexo_id: str,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    anexo = (
        db.query(DocumentoAnexo)
        .filter(DocumentoAnexo.id == anexo_id, DocumentoAnexo.tenant_id == ctx.tenant_id)
        .first()
    )
    if not anexo:
        raise HTTPException(status_code=404, detail="Anexo não encontrado.")
    sb_url, sb_key, sb_bucket = _supabase_storage_cfg()
    if sb_url and sb_key:
        dl_resp = requests.get(
            f"{sb_url}/storage/v1/object/{sb_bucket}/{anexo.storage_key}",
            headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key},
            timeout=25,
        )
        if dl_resp.status_code >= 400:
            raise HTTPException(status_code=404, detail="Arquivo não encontrado no Supabase Storage.")
        safe_dl = re.sub(r'["\\\r\n]+', "_", anexo.nome_original or "documento")
        return Response(
            content=dl_resp.content,
            media_type=anexo.mime_type or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_dl}"'},
        )
    caminho = _diretorio_anexos_base() / anexo.storage_key
    if not caminho.exists():
        raise HTTPException(status_code=404, detail="Arquivo físico não encontrado no servidor.")
    return FileResponse(caminho, media_type=anexo.mime_type, filename=anexo.nome_original)


@router.delete("/anexos/{anexo_id}")
def compat_excluir_anexo(
    anexo_id: str,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    anexo = (
        db.query(DocumentoAnexo)
        .filter(DocumentoAnexo.id == anexo_id, DocumentoAnexo.tenant_id == ctx.tenant_id)
        .first()
    )
    if not anexo:
        raise HTTPException(status_code=404, detail="Anexo não encontrado.")
    sb_url, sb_key, sb_bucket = _supabase_storage_cfg()
    if sb_url and sb_key:
        try:
            requests.delete(
                f"{sb_url}/storage/v1/object/{sb_bucket}/{anexo.storage_key}",
                headers={"Authorization": f"Bearer {sb_key}", "apikey": sb_key},
                timeout=15,
            )
        except Exception:
            pass
    else:
        caminho = _diretorio_anexos_base() / anexo.storage_key
        try:
            if caminho.exists():
                os.remove(caminho)
        except OSError:
            pass
    db.delete(anexo)
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "EXCLUIR_ANEXO_TITULO",
        "documentos_anexos",
        anexo_id,
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True}


# ---------------------------------------------------------------------------
# REMESSA BANCÁRIA CNAB 240 MULTI-BANCO (CONTAS + PREVIEW + GERAÇÃO)
# ---------------------------------------------------------------------------

class ContaBancariaCompatRequest(BaseModel):
    banco_codigo: str = "341"
    banco_nome: Optional[str] = None
    agencia: str
    conta: str
    dac: Optional[str] = "0"
    convenio_sispag: Optional[str] = ""


class RemessaMultibancoRequest(BaseModel):
    conta_bancaria_id: Optional[str] = None
    banco_codigo: Optional[str] = None
    data_pagamento: Optional[str] = None
    todos_pendentes: bool = False
    titulo_ids: Optional[List[str]] = None


def _carregar_candidatos_remessa(
    db: Session,
    tenant_id: str,
    empresa_id: str,
    data_pagamento: Optional[str],
    todos_pendentes: bool,
    titulo_ids: Optional[List[str]],
) -> list[dict]:
    q = db.query(Titulo).filter(
        Titulo.tenant_id == tenant_id,
        Titulo.empresa_id == empresa_id,
        Titulo.tipo_movimento == "PAGAR",
        Titulo.conciliado.is_(False),
        Titulo.status.in_(["PENDENTE", "APROVADO"]),
    )
    if titulo_ids:
        raw_ids = []
        for tid in titulo_ids:
            _, raw_id = _strip_ref_id(tid)
            if raw_id.startswith("NF_"):
                raw_id = raw_id[3:].strip()
            raw_ids.append(raw_id)
        q = q.filter(Titulo.id.in_(raw_ids))
    elif data_pagamento and not todos_pendentes:
        dt_alvo = _parse_date(data_pagamento)
        if dt_alvo:
            q = q.filter(Titulo.dt_vencimento == dt_alvo)

    titulos_db = q.order_by(Titulo.dt_vencimento.asc(), Titulo.favorecido_nome.asc()).all()

    # Mapa de fornecedores/colaboradores (Chave PIX + Dados Bancários) por ID, CPF/CNPJ ou Nome
    def _norm_nome_chave(s: str) -> str:
        import unicodedata
        nfkd = unicodedata.normalize("NFKD", str(s or ""))
        sem = "".join(c for c in nfkd if not unicodedata.combining(c))
        toks = [t for t in re.sub(r"[^A-Za-z0-9\s]", " ", sem).upper().split() if t not in {"DE", "DA", "DO", "DOS", "DAS"}]
        return " ".join(toks)

    forn_all = db.query(Fornecedor).filter(Fornecedor.tenant_id == tenant_id).all()
    forn_by_id = {f.id: f for f in forn_all}
    forn_by_doc = {limpar_documento(f.cnpj_cpf): f for f in forn_all if f.cnpj_cpf}
    forn_by_nome = {_norm_nome_chave(f.razao_social): f for f in forn_all if f.razao_social}

    candidatos = []
    for t in titulos_db:
        forn_obj = (
            forn_by_id.get(t.fornecedor_id or "")
            or forn_by_doc.get(limpar_documento(t.favorecido_doc or ""))
            or forn_by_nome.get(_norm_nome_chave(t.favorecido_nome or ""))
        )
        pix_final = (t.pix_chave or "").strip() or (forn_obj.pix_chave.strip() if forn_obj and forn_obj.pix_chave else "")
        dados_banco_final = (forn_obj.dados_banco.strip() if forn_obj and forn_obj.dados_banco else "")
        forn_tipo = (forn_obj.tipo if forn_obj else "") or ""
        doc_final = (t.favorecido_doc or "").strip() or (forn_obj.cnpj_cpf if forn_obj and forn_obj.cnpj_cpf else "")
        candidatos.append({
            "id": t.id,
            "db_id": t.id,
            "numero_nf": t.numero_documento,
            "numero_doc": t.numero_documento,
            "numero_tx": t.numero_tx,
            "fornecedor": t.favorecido_nome,
            "fornecedor_tipo": forn_tipo,
            "favorecido_doc": doc_final,
            "dt_vencimento": t.dt_vencimento.isoformat(),
            "valor_liquido": str(to_dec(t.valor_liquido)),
            "valor_bruto": str(to_dec(t.valor_bruto)),
            "forma_pgto": t.forma_pgto or "",
            "cod_barras": t.cod_barras or "",
            "pix_chave": pix_final,
            "dados_banco": dados_banco_final,
        })
    return candidatos


def _resolver_conta_remessa(
    db: Session,
    tenant_id: str,
    empresa_id: str,
    conta_bancaria_id: Optional[str],
    banco_codigo_override: Optional[str],
) -> tuple[Optional[ContaBancaria], dict[str, str]]:
    conta = None
    if conta_bancaria_id:
        conta = (
            db.query(ContaBancaria)
            .filter(
                ContaBancaria.tenant_id == tenant_id,
                ContaBancaria.id == conta_bancaria_id,
                ContaBancaria.ativo.is_(True),
            )
            .first()
        )
    if not conta:
        conta = (
            db.query(ContaBancaria)
            .filter(
                ContaBancaria.tenant_id == tenant_id,
                ContaBancaria.empresa_id == empresa_id,
                ContaBancaria.ativo.is_(True),
            )
            .order_by(ContaBancaria.criado_em.asc())
            .first()
        )

    cod = banco_codigo_override or (conta.banco_codigo if conta else "341")
    nome = conta.banco_nome if (conta and not banco_codigo_override) else None
    perfil = obter_perfil_banco(cod, nome)
    return conta, perfil


@router.get("/contas-bancarias")
def compat_listar_contas_bancarias(
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    emp = _resolver_empresa_padrao(db, ctx.tenant_id)
    contas = (
        db.query(ContaBancaria)
        .filter(
            ContaBancaria.tenant_id == ctx.tenant_id,
            ContaBancaria.empresa_id == emp.id,
            ContaBancaria.ativo.is_(True),
        )
        .order_by(ContaBancaria.criado_em.asc())
        .all()
    )
    return {
        "success": True,
        "contas": [
            {
                "id": c.id,
                "banco_codigo": c.banco_codigo,
                "banco_nome": c.banco_nome,
                "agencia": c.agencia,
                "conta": c.conta,
                "dac": c.dac,
                "convenio_sispag": c.convenio_sispag or "",
            }
            for c in contas
        ],
        "bancos_suportados": [
            {"codigo": b["codigo"], "label": b["label"], "nome": b["nome"], "sigla": b["sigla"]}
            for b in BANCOS_CNAB240.values()
        ],
    }


@router.post("/contas-bancarias")
def compat_salvar_conta_bancaria(
    req: ContaBancariaCompatRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    emp = _resolver_empresa_padrao(db, ctx.tenant_id)
    perfil = obter_perfil_banco(req.banco_codigo, req.banco_nome)
    ag = re.sub(r"\D", "", req.agencia or "")
    cc = re.sub(r"\D", "", req.conta or "")
    dac = (req.dac or "0").strip()[:2] or "0"
    if not ag or not cc:
        raise HTTPException(status_code=422, detail="Informe Agência e Conta Corrente válidas.")

    conta = ContaBancaria(
        tenant_id=ctx.tenant_id,
        empresa_id=emp.id,
        banco_codigo=perfil["codigo"],
        banco_nome=(req.banco_nome or perfil["nome"]).strip()[:80],
        agencia=ag[:10],
        conta=cc[:20],
        dac=dac,
        convenio_sispag=(req.convenio_sispag or "").strip()[:30] or None,
        ativo=True,
    )
    db.add(conta)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_CONTA_BANCARIA",
        "contas_bancarias",
        conta.id,
        dados_depois={"banco": conta.banco_codigo, "agencia": conta.agencia, "conta": conta.conta},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "conta": {
            "id": conta.id,
            "banco_codigo": conta.banco_codigo,
            "banco_nome": conta.banco_nome,
            "agencia": conta.agencia,
            "conta": conta.conta,
            "dac": conta.dac,
            "convenio_sispag": conta.convenio_sispag or "",
        },
    }


@router.post("/remessa/preview")
def compat_preview_remessa_multibanco(
    req: RemessaMultibancoRequest,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    emp = _resolver_empresa_padrao(db, ctx.tenant_id)
    conta, perfil = _resolver_conta_remessa(
        db, ctx.tenant_id, emp.id, req.conta_bancaria_id, req.banco_codigo
    )
    candidatos = _carregar_candidatos_remessa(
        db, ctx.tenant_id, emp.id, req.data_pagamento, req.todos_pendentes, req.titulo_ids
    )
    validos, rejeitados = classificar_titulos_cnab(candidatos, banco_pagador=perfil["codigo"])

    lotes_map: dict[str, dict] = {}
    rotulos = {
        "BOLETO_MESMO_BANCO": f"Boletos Mesmo Banco ({perfil['sigla']})",
        "BOLETO_OUTRO_BANCO": "Boletos Outros Bancos",
        "ARRECADACAO": "Concessionárias / Tributos (Seg. O)",
        "PIX_CHAVE": "PIX Transferência por Chave (Seg. A+B)",
        "FOLHA_CREDITO_CC": "Folha Pagamento Crédito C/C (Seg. A+B)",
    }
    validos_out = []
    total_val = 0.0
    for v in validos:
        val_f = round(v["_valor_centavos"] / 100.0, 2)
        total_val += val_f
        tp = v["_tipo_lote"]
        if tp not in lotes_map:
            lotes_map[tp] = {"tipo": tp, "rotulo": rotulos.get(tp, tp), "qtd": 0, "valor": 0.0}
        lotes_map[tp]["qtd"] += 1
        lotes_map[tp]["valor"] = round(lotes_map[tp]["valor"] + val_f, 2)
        validos_out.append({
            "id": v["id"],
            "numero_doc": v["numero_doc"],
            "fornecedor": v["fornecedor"],
            "dt_vencimento": v["dt_vencimento"],
            "valor": val_f,
            "tipo_lote": tp,
            "rotulo_lote": rotulos.get(tp, tp),
            "identificador_pgto": v.get("_identificador_pgto", ""),
        })

    return {
        "success": True,
        "conta_configurada": bool(conta or (config.ITAU_AGENCIA and config.ITAU_CONTA)),
        "banco": perfil,
        "conta": (
            {
                "id": conta.id,
                "banco_codigo": conta.banco_codigo,
                "banco_nome": conta.banco_nome,
                "agencia": conta.agencia,
                "conta": conta.conta,
                "dac": conta.dac,
                "convenio_sispag": conta.convenio_sispag or "",
            }
            if conta
            else None
        ),
        "lotes": list(lotes_map.values()),
        "validos": validos_out,
        "rejeitados": rejeitados,
        "totais": {
            "qtd_validos": len(validos_out),
            "qtd_rejeitados": len(rejeitados),
            "valor_total": round(total_val, 2),
        },
    }


@router.post("/remessa/gerar")
def compat_gerar_remessa_multibanco(
    req: RemessaMultibancoRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    emp = _resolver_empresa_padrao(db, ctx.tenant_id)
    conta, perfil = _resolver_conta_remessa(
        db, ctx.tenant_id, emp.id, req.conta_bancaria_id, req.banco_codigo
    )
    agencia = conta.agencia if conta else config.ITAU_AGENCIA
    num_conta = conta.conta if conta else config.ITAU_CONTA
    dac = conta.dac if conta else (config.ITAU_DAC or "0")
    convenio = (conta.convenio_sispag or "") if conta else ""
    if not agencia or not num_conta:
        raise HTTPException(
            status_code=400,
            detail="Cadastre uma Conta Bancária da empresa (botão '+ Nova Conta') para gerar a remessa CNAB 240.",
        )

    candidatos = _carregar_candidatos_remessa(
        db, ctx.tenant_id, emp.id, req.data_pagamento, req.todos_pendentes, req.titulo_ids
    )
    dt_pgto = (req.data_pagamento or date.today().isoformat())[:10]
    try:
        conteudo, resumo = gerar_cnab240_multibanco(
            candidatos,
            banco_codigo=perfil["codigo"],
            banco_nome=perfil["nome"],
            cnpj_empresa=emp.cnpj,
            razao_social=emp.razao_social,
            agencia=agencia,
            conta=num_conta,
            dac=dac,
            convenio=convenio,
            data_pagamento=dt_pgto,
            cidade=emp.cidade,
            cep=emp.cep,
            uf=emp.uf,
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

    nome_arquivo = f"REM_{resumo['banco_codigo']}_{resumo['banco_sigla']}_{dt_pgto.replace('-', '')}.REM"
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "EXPORTAR_CNAB_MULTIBANCO",
        "titulos",
        nome_arquivo,
        dados_depois=resumo,
        ip=extrair_ip(request),
    )
    db.commit()
    return Response(
        content=conteudo,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.post("/fornecedores/importar-favorecidos-itau")
async def compat_importar_favorecidos_itau(
    request: Request,
    file: UploadFile = File(...),
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    import csv
    import zipfile
    import xml.etree.ElementTree as ET
    from html.parser import HTMLParser

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Arquivo vazio.")

    def _ler_linhas_tabular(raw_bytes: bytes) -> list[list[str]]:
        if raw_bytes[:4] == b"\xd0\xcf\x11\xe0":
            try:
                import xlrd
                wb = xlrd.open_workbook(file_contents=raw_bytes)
                sh = wb.sheet_by_index(0)
                return [
                    [str(sh.cell_value(r, c)).strip() for c in range(sh.ncols)]
                    for r in range(sh.nrows)
                ]
            except Exception as e_xls:
                raise HTTPException(
                    status_code=422,
                    detail=f"Não foi possível ler o .xls binário ({e_xls}). Abra no Excel e salve como .xlsx ou .csv.",
                )
        # 1. XLSX (ZIP OOXML) usando apenas stdlib
        if raw_bytes[:2] == b"PK":
            try:
                with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                    shared: list[str] = []
                    if "xl/sharedStrings.xml" in zf.namelist():
                        root_ss = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                        for si in root_ss.iter():
                            if si.tag.endswith("}si") or si.tag == "si":
                                txt = "".join(t.text or "" for t in si.iter() if t.tag.endswith("}t") or t.tag == "t")
                                shared.append(txt)
                    sheet_names = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
                    if sheet_names:
                        root_sh = ET.fromstring(zf.read(sheet_names[0]))
                        rows_out: list[list[str]] = []
                        for row_el in root_sh.iter():
                            if row_el.tag.endswith("}row") or row_el.tag == "row":
                                row_vals: list[str] = []
                                for c_el in row_el:
                                    t_attr = c_el.attrib.get("t", "")
                                    v_txt = ""
                                    for ch in c_el.iter():
                                        if (ch.tag.endswith("}v") or ch.tag == "v" or ch.tag.endswith("}t") or ch.tag == "t") and ch.text:
                                            v_txt += ch.text
                                    if t_attr == "s" and v_txt.isdigit() and int(v_txt) < len(shared):
                                        v_txt = shared[int(v_txt)]
                                    row_vals.append(v_txt.strip())
                                if any(row_vals):
                                    rows_out.append(row_vals)
                        if rows_out:
                            return rows_out
            except Exception:
                pass

        # Decodifica texto para HTML (.xls) ou CSV/TXT
        texto = None
        for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                texto = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if texto is None:
            texto = raw_bytes.decode("latin-1", errors="replace")

        # 2. XLS em formato HTML (comum no Itaú)
        if "<table" in texto.lower() and "<tr" in texto.lower():
            class _TblParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.rows: list[list[str]] = []
                    self.cur_row: list[str] = []
                    self.cur_cell: list[str] = []
                    self.in_cell = False

                def handle_starttag(self, tag, attrs):
                    if tag in ("tr",):
                        self.cur_row = []
                    elif tag in ("td", "th"):
                        self.in_cell = True
                        self.cur_cell = []

                def handle_endtag(self, tag):
                    if tag in ("td", "th") and self.in_cell:
                        self.cur_row.append("".join(self.cur_cell).strip())
                        self.in_cell = False
                    elif tag == "tr" and self.cur_row:
                        self.rows.append(self.cur_row)

                def handle_data(self, data):
                    if self.in_cell:
                        self.cur_cell.append(data)

            p = _TblParser()
            p.feed(texto)
            if p.rows:
                return p.rows

        # 3. CSV / TXT
        linhas_txt = [ln for ln in texto.splitlines() if ln.strip()]
        delim = ";" if any(ln.count(";") > ln.count(",") for ln in linhas_txt[:15]) else ","
        return list(csv.reader(linhas_txt, delimiter=delim))

    rows_tab = _ler_linhas_tabular(raw)
    idx_header = None
    headers_norm: list[str] = []
    for i, r in enumerate(rows_tab):
        low_cells = [c.strip().lower() for c in r]
        joined = " ".join(low_cells)
        if ("nome do funcion" in joined or "favorecido" in joined) and "dados da conta" in joined:
            idx_header = i
            headers_norm = low_cells
            break

    if idx_header is None:
        raise HTTPException(
            status_code=422,
            detail="Cabeçalho 'nome do funcionário, CPF, dados da conta' não encontrado no arquivo (use CSV, TXT ou XLSX exportado do Itaú).",
        )

    registros = []
    for r in rows_tab[idx_header + 1:]:
        norm_row = {headers_norm[j]: (r[j].strip() if j < len(r) else "") for j in range(len(headers_norm))}
        registros.append(norm_row)

    importados = 0
    atualizados = 0
    titulos_vinculados = 0

    for norm_row in registros:
        nome = (
            norm_row.get("nome do funcionário")
            or norm_row.get("nome do funcionario")
            or norm_row.get("favorecido")
            or ""
        ).strip()
        cpf = limpar_documento(norm_row.get("cpf") or norm_row.get("cpf/cnpj") or "")
        dados_conta = norm_row.get("dados da conta") or ""
        if not nome or not dados_conta:
            continue

        forn = None
        if cpf:
            forn = (
                db.query(Fornecedor)
                .filter(Fornecedor.tenant_id == ctx.tenant_id, Fornecedor.cnpj_cpf == cpf)
                .first()
            )
        if not forn:
            forn = (
                db.query(Fornecedor)
                .filter(Fornecedor.tenant_id == ctx.tenant_id, func.lower(Fornecedor.razao_social) == nome.lower())
                .first()
            )

        if forn:
            forn.tipo = "COLABORADOR"
            forn.dados_banco = dados_conta[:120]
            if not forn.forma_pgto_padrao:
                forn.forma_pgto_padrao = "FOLHA ITAÚ (C/C)"
            if cpf and not forn.cnpj_cpf:
                forn.cnpj_cpf = cpf
            atualizados += 1
        else:
            forn = Fornecedor(
                tenant_id=ctx.tenant_id,
                razao_social=nome.upper()[:200],
                nome_fantasia=nome.upper()[:200],
                cnpj_cpf=cpf or None,
                tipo="COLABORADOR",
                forma_pgto_padrao="FOLHA ITAÚ (C/C)",
                dados_banco=dados_conta[:120],
                ativo=True,
            )
            db.add(forn)
            db.flush()
            importados += 1

        # Vincula títulos em aberto do colaborador que ainda estejam sem fornecedor_id ou CPF
        tits = (
            db.query(Titulo)
            .filter(
                Titulo.tenant_id == ctx.tenant_id,
                Titulo.conciliado.is_(False),
                func.lower(Titulo.favorecido_nome) == nome.lower(),
            )
            .all()
        )
        for t in tits:
            if not t.fornecedor_id:
                t.fornecedor_id = forn.id
            if cpf and not t.favorecido_doc:
                t.favorecido_doc = cpf
            titulos_vinculados += 1

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "IMPORTAR_FAVORECIDOS_ITAU",
        "fornecedores",
        file.filename or "itau_funcionarios.csv",
        dados_depois={"importados": importados, "atualizados": atualizados, "titulos_vinculados": titulos_vinculados},
        ip=extrair_ip(request),
    )
    db.commit()
    return {
        "success": True,
        "importados": importados,
        "atualizados": atualizados,
        "titulos_vinculados": titulos_vinculados,
    }
