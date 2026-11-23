"""
Script ETL Idempotente: SQLite Legado (bimer.db) -> JR FinCloud SaaS v2 (PostgreSQL / SQLAlchemy).
Migra plano de contas, fornecedores, usuários, títulos, impostos e previsões mantendo
conferência exata dos somatórios em Decimal.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.validators import limpar_documento
from app.db.base import Base
from app.db.models import (
    CentroCusto,
    DespesaRecorrente,
    Empresa,
    Fornecedor,
    PlanoConta,
    Tenant,
    Titulo,
    TituloImposto,
    Usuario,
    UsuarioTenantRole,
)


def _to_dec(val) -> Decimal:
    return Decimal(str(val or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_date(val: str | None) -> date:
    raw = (val or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        if raw:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                pass
    return date.today()


def migrar_para_saas_v2(
    sqlite_path: str,
    target_url: str,
    tenant_nome: str = "JR Solutions Varejo",
    tenant_slug: str = "jr-solutions",
    empresa_cnpj: str = "11222333000181",
    plano_contas_json: str | None = None,
) -> dict:
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"Banco SQLite de origem não encontrado: {sqlite_path}")

    engine = create_engine(target_url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    src_conn = sqlite3.connect(sqlite_path)
    src_conn.row_factory = sqlite3.Row
    src_cur = src_conn.cursor()

    cnpj_limpo = limpar_documento(empresa_cnpj)
    db = SessionLocal()

    stats = {
        "plano_contas": 0,
        "fornecedores": 0,
        "usuarios": 0,
        "titulos": 0,
        "impostos": 0,
        "recorrentes": 0,
        "soma_origem_bruto": Decimal("0.00"),
        "soma_destino_bruto": Decimal("0.00"),
    }

    try:
        tenant = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not tenant:
            tenant = Tenant(
                nome_fantasia=tenant_nome,
                slug=tenant_slug,
                documento=cnpj_limpo,
                plano="PRO",
                status_assinatura="ACTIVE",
            )
            db.add(tenant)
            db.flush()

        empresa = (
            db.query(Empresa)
            .filter(Empresa.tenant_id == tenant.id, Empresa.cnpj == cnpj_limpo)
            .first()
        )
        if not empresa:
            empresa = Empresa(
                tenant_id=tenant.id,
                razao_social=tenant_nome,
                nome_fantasia=tenant_nome,
                cnpj=cnpj_limpo,
            )
            db.add(empresa)
            db.flush()

        cc_matriz = (
            db.query(CentroCusto)
            .filter(CentroCusto.tenant_id == tenant.id, CentroCusto.empresa_id == empresa.id, CentroCusto.codigo == "MATRIZ")
            .first()
        )
        if not cc_matriz:
            cc_matriz = CentroCusto(
                tenant_id=tenant.id,
                empresa_id=empresa.id,
                codigo="MATRIZ",
                nome=f"{tenant_nome} - Matriz",
                tipo_unidade="MATRIZ",
            )
            db.add(cc_matriz)
            db.flush()

        # 1. Plano de Contas (grupos_categorias.json)
        if plano_contas_json and os.path.exists(plano_contas_json):
            with open(plano_contas_json, "r", encoding="utf-8") as fp:
                categorias = json.load(fp)
            codigos_vistos: set[str] = set()
            for item in categorias:
                cod = str(item.get("codigo") or "").strip()
                if not cod or cod in codigos_vistos:
                    continue
                codigos_vistos.add(cod)
                exists = (
                    db.query(PlanoConta)
                    .filter(PlanoConta.tenant_id == tenant.id, PlanoConta.codigo == cod)
                    .first()
                )
                if not exists:
                    db.add(
                        PlanoConta(
                            tenant_id=tenant.id,
                            codigo=cod,
                            nome=str(item.get("nome") or cod)[:180],
                            macro_grupo=str(item.get("macro") or "GERAL")[:100],
                            tipo="DESPESA",
                        )
                    )
                    stats["plano_contas"] += 1
            db.flush()

        # 2. Usuários -> Usuarios + UsuarioTenantRole
        src_cur.execute("SELECT id, nome, email, senha_hash, perfil, ativo FROM usuarios")
        for u in src_cur.fetchall():
            email = (u["email"] or "").strip().lower()
            if not email:
                continue
            usr = db.query(Usuario).filter(Usuario.email == email).first()
            if not usr:
                usr = Usuario(
                    nome=u["nome"] or email,
                    email=email,
                    senha_hash=u["senha_hash"],
                    ativo=bool(u["ativo"]),
                )
                db.add(usr)
                db.flush()
                stats["usuarios"] += 1

            role_exists = (
                db.query(UsuarioTenantRole)
                .filter(UsuarioTenantRole.usuario_id == usr.id, UsuarioTenantRole.tenant_id == tenant.id)
                .first()
            )
            if not role_exists:
                perfil = (u["perfil"] or "OWNER").upper()
                role_v2 = "OWNER" if perfil == "MASTER" else perfil
                db.add(
                    UsuarioTenantRole(
                        tenant_id=tenant.id,
                        usuario_id=usr.id,
                        role=role_v2,
                        ativo=bool(u["ativo"]),
                    )
                )
        db.flush()

        # 3. Fornecedores
        forn_map: dict[str, Fornecedor] = {}
        src_cur.execute("SELECT * FROM fornecedores")
        for f in src_cur.fetchall():
            doc = limpar_documento(f["cnpj_cpf"] or "") or None
            razao = (f["razao_social"] or "").strip()
            if not razao:
                continue

            forn = None
            if doc:
                forn = (
                    db.query(Fornecedor)
                    .filter(Fornecedor.tenant_id == tenant.id, Fornecedor.cnpj_cpf == doc)
                    .first()
                )
            if not forn:
                forn = Fornecedor(
                    tenant_id=tenant.id,
                    razao_social=razao[:200],
                    nome_fantasia=(f["nome_fantasia"] or razao)[:200],
                    cnpj_cpf=doc,
                    tipo=(f["tipo"] or "FORNECEDOR")[:30],
                    categoria_padrao=(f["categoria"] or "")[:120] or None,
                    forma_pgto_padrao=(f["forma_pgto"] or "")[:30] or None,
                    pix_chave=(f["pix_chave"] or "")[:140] or None,
                    dados_banco=f["dados_banco"],
                    cod_operacao=(f["cod_operacao"] or "")[:120] or None,
                    cnae=(f["cnae"] or "")[:40] or None,
                    item_lc116=(f["item_lc116"] or "")[:20] or None,
                    ativo=bool(f["ativo"] if f["ativo"] is not None else 1),
                )
                db.add(forn)
                db.flush()
                stats["fornecedores"] += 1

            if doc:
                forn_map[doc] = forn
            forn_map[razao.lower()] = forn

        # 4. Notas -> Titulos + TituloImpostos | Previsões -> DespesaRecorrente
        src_cur.execute("SELECT * FROM notas")
        notas_rows = src_cur.fetchall()
        src_cur.execute("SELECT * FROM nota_impostos")
        imp_por_nota: dict[int, list] = {}
        for imp in src_cur.fetchall():
            imp_por_nota.setdefault(imp["nota_id"], []).append(imp)

        for n in notas_rows:
            is_prev = bool(n["is_previsao"]) or (n["status"] == "PREVISAO")
            bruto = _to_dec(n["valor_bruto"])
            liquido = _to_dec(n["valor_liquido"] if n["valor_liquido"] is not None else n["valor_bruto"])

            if is_prev:
                nome_prev = (n["descricao"] or n["fornecedor"] or "Despesa Recorrente")[:180]
                exists_prev = (
                    db.query(DespesaRecorrente)
                    .filter(DespesaRecorrente.tenant_id == tenant.id, DespesaRecorrente.nome == nome_prev)
                    .first()
                )
                if not exists_prev:
                    dt_v = _parse_date(n["dt_vencimento"])
                    db.add(
                        DespesaRecorrente(
                            tenant_id=tenant.id,
                            empresa_id=empresa.id,
                            centro_custo_id=cc_matriz.id,
                            nome=nome_prev,
                            valor_estimado=bruto,
                            dia_vencimento=dt_v.day,
                        )
                    )
                    stats["recorrentes"] += 1
                continue

            stats["soma_origem_bruto"] += bruto
            tx_ref = (n["numero_tx"] or f"LEGADO_{n['id']}")[:60]
            doc_num = (n["numero_nf"] or tx_ref)[:60]

            titulo_existente = (
                db.query(Titulo)
                .filter(Titulo.tenant_id == tenant.id, Titulo.numero_tx == tx_ref)
                .first()
            )
            if titulo_existente:
                stats["soma_destino_bruto"] += _to_dec(titulo_existente.valor_bruto)
                continue

            doc_forn = limpar_documento(n["cnpj"] or "")
            forn_obj = forn_map.get(doc_forn) or forn_map.get((n["fornecedor"] or "").strip().lower())
            is_pago = (n["status"] in ("PAGO", "CONCILIADO")) or bool(n["conciliada"])

            titulo = Titulo(
                tenant_id=tenant.id,
                empresa_id=empresa.id,
                fornecedor_id=forn_obj.id if forn_obj else None,
                centro_custo_id=cc_matriz.id,
                tipo_movimento="PAGAR",
                tipo_documento=(n["categoria"] or "NFSE")[:30],
                numero_tx=tx_ref,
                numero_documento=doc_num,
                favorecido_nome=(n["fornecedor"] or "Sem Fornecedor")[:200],
                favorecido_doc=(n["cnpj"] or "")[:20] or None,
                dt_emissao=_parse_date(n["dt_emissao"]),
                dt_vencimento=_parse_date(n["dt_vencimento"]),
                dt_pagamento=_parse_date(n["dt_pagamento"]) if n["dt_pagamento"] else None,
                valor_bruto=bruto,
                valor_liquido=liquido,
                valor_pago=_to_dec(n["valor_pago"]),
                juros_multa=_to_dec(n["juros_multa"]),
                desconto=_to_dec(n["desconto"]),
                status="CONCILIADO" if is_pago else "PENDENTE",
                forma_pgto=(n["forma_pgto"] or "PIX")[:30],
                cod_barras=(n["cod_barras"] or "")[:80] or None,
                pix_chave=(n["pix_chave"] or "")[:140] or None,
                cod_operacao=(n["cod_operacao"] or "")[:120] or None,
                cnae=(n["cnae"] or "")[:40] or None,
                item_lc116=(n["item_lc116"] or "")[:20] or None,
                descricao=n["descricao"],
                observacao=n["observacao"],
                solicitante=(n["responsavel"] or "")[:120] or None,
                conciliado=is_pago,
            )
            db.add(titulo)
            db.flush()
            stats["titulos"] += 1
            stats["soma_destino_bruto"] += bruto

            for imp in imp_por_nota.get(n["id"], []):
                db.add(
                    TituloImposto(
                        tenant_id=tenant.id,
                        titulo_id=titulo.id,
                        tipo=(imp["tipo"] or "ISS")[:20],
                        aliquota=Decimal(str(imp["aliquota"] or "0")).quantize(Decimal("0.0001")),
                        valor=_to_dec(imp["valor"]),
                        dt_vencimento=_parse_date(imp["dt_venc_imp"]) if imp["dt_venc_imp"] else titulo.dt_vencimento,
                        status=(imp["status"] or "PENDENTE")[:20],
                    )
                )
                stats["impostos"] += 1

        if stats["soma_origem_bruto"] != stats["soma_destino_bruto"]:
            raise RuntimeError(
                f"Divergência de somatório na migração: origem={stats['soma_origem_bruto']} destino={stats['soma_destino_bruto']}"
            )

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        src_conn.close()
        engine.dispose()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="ETL SQLite legado -> JR FinCloud SaaS v2")
    parser.add_argument("--sqlite-path", default=str(ROOT_DIR / "database" / "bimer.db"))
    parser.add_argument("--target-url", default=os.getenv("DATABASE_URL", f"sqlite:///{ROOT_DIR / 'database' / 'jr_fincloud_v2.db'}"))
    parser.add_argument("--tenant-nome", default="JR Solutions Varejo")
    parser.add_argument("--tenant-slug", default="jr-solutions")
    parser.add_argument("--empresa-cnpj", default="11222333000181")
    parser.add_argument("--plano-contas-json", default=str(ROOT_DIR / "frontend" / "grupos_categorias.json"))
    args = parser.parse_args()

    res = migrar_para_saas_v2(
        sqlite_path=args.sqlite_path,
        target_url=args.target_url,
        tenant_nome=args.tenant_nome,
        tenant_slug=args.tenant_slug,
        empresa_cnpj=args.empresa_cnpj,
        plano_contas_json=args.plano_contas_json,
    )
    print("Migração concluída com sucesso:", {k: str(v) for k, v in res.items()})


if __name__ == "__main__":
    main()
