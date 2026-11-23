from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...core.auth_v2 import TenantContext, get_tenant_context, require_v2_roles
from ...core.validators import limpar_documento, validar_documento
from ...db.models import ContaBancaria, Fornecedor, PlanoConta
from ...db.session import get_db
from .deps import (
    extrair_ip,
    obter_empresa_tenant,
    obter_fornecedor_tenant,
    registrar_audit_v2,
    to_dec,
)

router = APIRouter(tags=["JR FinCloud v2 - Cadastros & Motor Fiscal"])


class PlanoContaCreateRequest(BaseModel):
    codigo: str
    nome: str
    macro_grupo: str
    tipo: str = "DESPESA"
    dre_grupo: str = "DESPESAS_OPERACIONAIS"


class ContaBancariaCreateRequest(BaseModel):
    empresa_id: str
    banco_codigo: str = "341"
    banco_nome: str = "Banco Itaú"
    agencia: str
    conta: str
    dac: str = "0"
    convenio_sispag: Optional[str] = None
    chave_pix_padrao: Optional[str] = None
    saldo_inicial: Decimal = Decimal("0.00")


class FornecedorV2Request(BaseModel):
    razao_social: str
    nome_fantasia: Optional[str] = None
    cnpj_cpf: Optional[str] = None
    tipo: str = "FORNECEDOR"
    regime_tributario: Optional[str] = None
    optante_simples: bool = False
    categoria_padrao: Optional[str] = None
    forma_pgto_padrao: Optional[str] = None
    pix_chave: Optional[str] = None
    dados_banco: Optional[str] = None
    cod_operacao: Optional[str] = None
    cnae: Optional[str] = None
    item_lc116: Optional[str] = None


class CalculoRetencoesRequest(BaseModel):
    valor_bruto: Decimal
    fornecedor_id: Optional[str] = None
    optante_simples: Optional[bool] = None
    regime_tributario: Optional[str] = None
    iss_aliquota: Decimal = Decimal("0.00")
    reter_iss: bool = False
    reter_federais: bool = True
    incluir_reforma_2026: bool = True


# ---------------------------------------------------------------------------
# PLANO DE CONTAS
# ---------------------------------------------------------------------------

@router.get("/plano-contas")
def listar_plano_contas(
    tipo: Optional[str] = Query(None),
    macro_grupo: Optional[str] = Query(None),
    busca: Optional[str] = Query(None),
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    q = db.query(PlanoConta).filter(PlanoConta.tenant_id == ctx.tenant_id, PlanoConta.ativo.is_(True))
    if tipo:
        q = q.filter(PlanoConta.tipo == tipo.strip().upper())
    if macro_grupo:
        q = q.filter(PlanoConta.macro_grupo.ilike(f"%{macro_grupo.strip()}%"))
    if busca:
        termo = f"%{busca.strip()}%"
        q = q.filter(or_(PlanoConta.codigo.ilike(termo), PlanoConta.nome.ilike(termo)))

    contas = q.order_by(PlanoConta.codigo.asc()).all()
    return {
        "success": True,
        "total": len(contas),
        "contas": [
            {
                "id": c.id,
                "codigo": c.codigo,
                "nome": c.nome,
                "macro_grupo": c.macro_grupo,
                "tipo": c.tipo,
                "dre_grupo": c.dre_grupo,
            }
            for c in contas
        ],
    }


@router.post("/plano-contas", status_code=status.HTTP_201_CREATED)
def criar_plano_conta(
    req: PlanoContaCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    cod = req.codigo.strip()
    if not cod:
        raise HTTPException(status_code=422, detail="Código contábil obrigatório.")

    if db.query(PlanoConta).filter(PlanoConta.tenant_id == ctx.tenant_id, PlanoConta.codigo == cod).first():
        raise HTTPException(status_code=409, detail=f"Código '{cod}' já cadastrado no plano de contas.")

    pc = PlanoConta(
        tenant_id=ctx.tenant_id,
        codigo=cod,
        nome=req.nome.strip(),
        macro_grupo=req.macro_grupo.strip(),
        tipo=req.tipo.strip().upper(),
        dre_grupo=req.dre_grupo.strip().upper(),
    )
    db.add(pc)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_PLANO_CONTA",
        "plano_contas",
        pc.id,
        dados_depois={"codigo": cod, "nome": pc.nome},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": pc.id, "codigo": pc.codigo}


# ---------------------------------------------------------------------------
# CONTAS BANCÁRIAS POR EMPRESA
# ---------------------------------------------------------------------------

@router.get("/contas-bancarias")
def listar_contas_bancarias(
    empresa_id: Optional[str] = Query(None),
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    q = db.query(ContaBancaria).filter(ContaBancaria.tenant_id == ctx.tenant_id, ContaBancaria.ativo.is_(True))
    if empresa_id:
        q = q.filter(ContaBancaria.empresa_id == empresa_id)
    contas = q.order_by(ContaBancaria.banco_codigo, ContaBancaria.agencia).all()
    return {
        "success": True,
        "contas": [
            {
                "id": c.id,
                "empresa_id": c.empresa_id,
                "banco_codigo": c.banco_codigo,
                "banco_nome": c.banco_nome,
                "agencia": c.agencia,
                "conta": c.conta,
                "dac": c.dac,
                "convenio_sispag": c.convenio_sispag,
                "chave_pix_padrao": c.chave_pix_padrao,
                "saldo_inicial": str(to_dec(c.saldo_inicial)),
            }
            for c in contas
        ],
    }


@router.post("/contas-bancarias", status_code=status.HTTP_201_CREATED)
def criar_conta_bancaria(
    req: ContaBancariaCreateRequest,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    obter_empresa_tenant(db, ctx.tenant_id, req.empresa_id)
    conta = ContaBancaria(
        tenant_id=ctx.tenant_id,
        empresa_id=req.empresa_id,
        banco_codigo=req.banco_codigo.strip(),
        banco_nome=req.banco_nome.strip(),
        agencia=req.agencia.strip(),
        conta=req.conta.strip(),
        dac=req.dac.strip()[:2] or "0",
        convenio_sispag=req.convenio_sispag,
        chave_pix_padrao=req.chave_pix_padrao,
        saldo_inicial=to_dec(req.saldo_inicial),
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
    return {"success": True, "id": conta.id}


# ---------------------------------------------------------------------------
# FORNECEDORES & PRESTADORES
# ---------------------------------------------------------------------------

def _serializar_fornecedor(f: Fornecedor) -> dict:
    return {
        "id": f.id,
        "razao_social": f.razao_social,
        "nome_fantasia": f.nome_fantasia,
        "cnpj_cpf": f.cnpj_cpf,
        "tipo": f.tipo,
        "regime_tributario": f.regime_tributario,
        "optante_simples": f.optante_simples,
        "categoria_padrao": f.categoria_padrao,
        "forma_pgto_padrao": f.forma_pgto_padrao,
        "pix_chave": f.pix_chave,
        "cnae": f.cnae,
        "item_lc116": f.item_lc116,
    }


@router.get("/fornecedores")
def listar_fornecedores_v2(
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
    return {"success": True, "total": len(itens), "fornecedores": [_serializar_fornecedor(f) for f in itens]}


@router.post("/fornecedores", status_code=status.HTTP_201_CREATED)
def criar_fornecedor_v2(
    req: FornecedorV2Request,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    doc_raw = (req.cnpj_cpf or "").strip()
    doc_limpo = None
    if doc_raw:
        if not validar_documento(doc_raw):
            raise HTTPException(status_code=422, detail=f"CPF/CNPJ inválido: {doc_raw}")
        doc_limpo = limpar_documento(doc_raw)
        if db.query(Fornecedor).filter(Fornecedor.tenant_id == ctx.tenant_id, Fornecedor.cnpj_cpf == doc_limpo).first():
            raise HTTPException(status_code=409, detail="Fornecedor com este CPF/CNPJ já cadastrado no workspace.")

    forn = Fornecedor(
        tenant_id=ctx.tenant_id,
        razao_social=req.razao_social.strip(),
        nome_fantasia=(req.nome_fantasia or req.razao_social).strip(),
        cnpj_cpf=doc_limpo,
        tipo=req.tipo.strip().upper(),
        regime_tributario=req.regime_tributario,
        optante_simples=req.optante_simples,
        categoria_padrao=req.categoria_padrao,
        forma_pgto_padrao=req.forma_pgto_padrao,
        pix_chave=req.pix_chave,
        dados_banco=req.dados_banco,
        cod_operacao=req.cod_operacao,
        cnae=req.cnae,
        item_lc116=req.item_lc116,
    )
    db.add(forn)
    db.flush()
    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "CRIAR_FORNECEDOR",
        "fornecedores",
        forn.id,
        dados_depois={"razao_social": forn.razao_social, "cnpj_cpf": doc_limpo, "optante_simples": forn.optante_simples},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "id": forn.id, "fornecedor": _serializar_fornecedor(forn)}


@router.put("/fornecedores/{fornecedor_id}")
def atualizar_fornecedor_v2(
    fornecedor_id: str,
    req: FornecedorV2Request,
    request: Request,
    ctx: TenantContext = Depends(require_v2_roles("OWNER", "DIRETOR_APROVADOR", "FINANCEIRO")),
    db: Session = Depends(get_db),
):
    forn = obter_fornecedor_tenant(db, ctx.tenant_id, fornecedor_id)
    doc_raw = (req.cnpj_cpf or "").strip()
    doc_limpo = None
    if doc_raw:
        if not validar_documento(doc_raw):
            raise HTTPException(status_code=422, detail=f"CPF/CNPJ inválido: {doc_raw}")
        doc_limpo = limpar_documento(doc_raw)
        colisao = (
            db.query(Fornecedor)
            .filter(
                Fornecedor.tenant_id == ctx.tenant_id,
                Fornecedor.cnpj_cpf == doc_limpo,
                Fornecedor.id != forn.id,
            )
            .first()
        )
        if colisao:
            raise HTTPException(status_code=409, detail="Já existe outro fornecedor com este CPF/CNPJ no workspace.")

    forn.razao_social = req.razao_social.strip()
    forn.nome_fantasia = (req.nome_fantasia or req.razao_social).strip()
    forn.cnpj_cpf = doc_limpo
    forn.tipo = req.tipo.strip().upper()
    forn.regime_tributario = req.regime_tributario
    forn.optante_simples = req.optante_simples
    forn.categoria_padrao = req.categoria_padrao
    forn.forma_pgto_padrao = req.forma_pgto_padrao
    forn.pix_chave = req.pix_chave
    forn.dados_banco = req.dados_banco
    forn.cod_operacao = req.cod_operacao
    forn.cnae = req.cnae
    forn.item_lc116 = req.item_lc116

    registrar_audit_v2(
        db,
        ctx.tenant_id,
        ctx.usuario,
        "ATUALIZAR_FORNECEDOR",
        "fornecedores",
        forn.id,
        dados_depois={"razao_social": forn.razao_social, "optante_simples": forn.optante_simples},
        ip=extrair_ip(request),
    )
    db.commit()
    return {"success": True, "fornecedor": _serializar_fornecedor(forn)}


# ---------------------------------------------------------------------------
# MOTOR DE SIMULAÇÃO TRIBUTÁRIA (SIMPLES/MEI + DARF MÍNIMO + IBS/CBS 2026)
# ---------------------------------------------------------------------------

@router.post("/fiscal/calcular-retencoes")
def calcular_retencoes_fiscais(
    req: CalculoRetencoesRequest,
    ctx: TenantContext = Depends(get_tenant_context),
    db: Session = Depends(get_db),
):
    bruto = to_dec(req.valor_bruto)
    if bruto <= Decimal("0.00"):
        raise HTTPException(status_code=422, detail="O valor bruto deve ser maior que zero.")

    opt_simples = req.optante_simples
    regime = req.regime_tributario
    if req.fornecedor_id:
        forn = obter_fornecedor_tenant(db, ctx.tenant_id, req.fornecedor_id)
        if opt_simples is None:
            opt_simples = forn.optante_simples
        if regime is None:
            regime = forn.regime_tributario

    is_simples_ou_mei = bool(opt_simples) or (str(regime or "").upper() in ("SIMPLES_NACIONAL", "MEI"))
    dispensa_motivo = None

    if is_simples_ou_mei:
        irrf = pis = cofins = csll = Decimal("0.00")
        dispensa_motivo = "Retenções federais (IRRF/PIS/COFINS/CSLL) dispensadas para prestador optante pelo Simples Nacional ou MEI."
    elif not req.reter_federais:
        irrf = pis = cofins = csll = Decimal("0.00")
        dispensa_motivo = "Retenções federais não aplicáveis à natureza do serviço."
    else:
        irrf_calc = to_dec(bruto * Decimal("0.015"))
        irrf = irrf_calc if irrf_calc >= Decimal("10.00") else Decimal("0.00")

        pis_calc = to_dec(bruto * Decimal("0.0065"))
        cofins_calc = to_dec(bruto * Decimal("0.0300"))
        csll_calc = to_dec(bruto * Decimal("0.0100"))
        pcc_total = pis_calc + cofins_calc + csll_calc
        if pcc_total >= Decimal("10.00"):
            pis, cofins, csll = pis_calc, cofins_calc, csll_calc
        else:
            pis = cofins = csll = Decimal("0.00")
            dispensa_motivo = "PCC dispensado por valor consolidado inferior ao mínimo de DARF (R$ 10,00)."

    iss = (
        to_dec(bruto * (req.iss_aliquota / Decimal("100")))
        if (req.reter_iss and req.iss_aliquota > Decimal("0"))
        else Decimal("0.00")
    )

    total_retencoes = irrf + pis + cofins + csll + iss
    valor_liquido = max(Decimal("0.00"), bruto - total_retencoes)

    cbs_2026 = to_dec(bruto * Decimal("0.0090")) if req.incluir_reforma_2026 else Decimal("0.00")
    ibs_2026 = to_dec(bruto * Decimal("0.0010")) if req.incluir_reforma_2026 else Decimal("0.00")

    return {
        "success": True,
        "valor_bruto": str(bruto),
        "valor_liquido": str(valor_liquido),
        "total_retencoes": str(total_retencoes),
        "optante_simples_ou_mei": is_simples_ou_mei,
        "dispensa_federal_motivo": dispensa_motivo,
        "retencoes": {
            "irrf": str(irrf),
            "pis": str(pis),
            "cofins": str(cofins),
            "csll": str(csll),
            "iss": str(iss),
        },
        "reforma_tributaria_2026_informativo": {
            "cbs_0_90": str(cbs_2026),
            "ibs_0_10": str(ibs_2026),
            "total_iva_dual": str(cbs_2026 + ibs_2026),
        },
    }
