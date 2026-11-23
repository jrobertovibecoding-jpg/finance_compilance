"""initial saas multi-tenant schema

Revision ID: 0001_initial_saas_schema
Revises:
Create Date: 2026-11-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial_saas_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("nome_fantasia", sa.String(180), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("documento", sa.String(20), nullable=True),
        sa.Column("plano", sa.String(40), nullable=False, server_default="PRO"),
        sa.Column("status_assinatura", sa.String(30), nullable=False, server_default="ACTIVE"),
        sa.Column("is_bpo_partner", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("parent_bpo_tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"])

    op.create_table(
        "empresas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("razao_social", sa.String(200), nullable=False),
        sa.Column("nome_fantasia", sa.String(180), nullable=False),
        sa.Column("cnpj", sa.String(20), nullable=False),
        sa.Column("regime_tributario", sa.String(40), nullable=False, server_default="LUCRO_PRESUMIDO"),
        sa.Column("inscricao_municipal", sa.String(40), nullable=True),
        sa.Column("cidade", sa.String(100), nullable=False, server_default="SALVADOR"),
        sa.Column("uf", sa.String(2), nullable=False, server_default="BA"),
        sa.Column("cep", sa.String(10), nullable=False, server_default="40000000"),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "cnpj", name="uq_empresa_tenant_cnpj"),
    )
    op.create_index("ix_empresas_tenant_id", "empresas", ["tenant_id"])
    op.create_index("ix_empresas_tenant_ativo", "empresas", ["tenant_id", "ativo"])

    op.create_table(
        "centros_custo",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("empresa_id", sa.String(36), sa.ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("codigo", sa.String(30), nullable=False),
        sa.Column("nome", sa.String(150), nullable=False),
        sa.Column("tipo_unidade", sa.String(40), nullable=False, server_default="LOJA_SHOPPING"),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "empresa_id", "codigo", name="uq_ccusto_tenant_emp_cod"),
    )
    op.create_index("ix_centros_custo_tenant_id", "centros_custo", ["tenant_id"])
    op.create_index("ix_centros_custo_empresa_id", "centros_custo", ["empresa_id"])

    op.create_table(
        "usuarios",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("nome", sa.String(150), nullable=False),
        sa.Column("email", sa.String(180), nullable=False, unique=True),
        sa.Column("senha_hash", sa.String(255), nullable=False),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("ultimo_login", sa.DateTime(timezone=True), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usuarios_email", "usuarios", ["email"])

    op.create_table(
        "usuario_tenant_roles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("usuario_id", sa.String(36), sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(40), nullable=False, server_default="FINANCEIRO"),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("usuario_id", "tenant_id", name="uq_usuario_tenant_role"),
    )
    op.create_index("ix_usuario_tenant_roles_tenant_id", "usuario_tenant_roles", ["tenant_id"])
    op.create_index("ix_usuario_tenant_roles_usuario_id", "usuario_tenant_roles", ["usuario_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("usuario_id", sa.String(36), sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_token_id", sa.String(36), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("ip_origem", sa.String(64), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_refresh_tokens_tenant_id", "refresh_tokens", ["tenant_id"])
    op.create_index("ix_refresh_tokens_usuario_id", "refresh_tokens", ["usuario_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"])

    op.create_table(
        "plano_contas",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("codigo", sa.String(30), nullable=False),
        sa.Column("nome", sa.String(180), nullable=False),
        sa.Column("macro_grupo", sa.String(100), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False, server_default="DESPESA"),
        sa.Column("dre_grupo", sa.String(60), nullable=False, server_default="DESPESAS_OPERACIONAIS"),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "codigo", name="uq_planoconta_tenant_cod"),
    )
    op.create_index("ix_plano_contas_tenant_id", "plano_contas", ["tenant_id"])

    op.create_table(
        "contas_bancarias",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("empresa_id", sa.String(36), sa.ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("banco_codigo", sa.String(10), nullable=False, server_default="341"),
        sa.Column("banco_nome", sa.String(80), nullable=False, server_default="Banco Itaú"),
        sa.Column("agencia", sa.String(10), nullable=False),
        sa.Column("conta", sa.String(20), nullable=False),
        sa.Column("dac", sa.String(2), nullable=False, server_default="0"),
        sa.Column("convenio_sispag", sa.String(30), nullable=True),
        sa.Column("chave_pix_padrao", sa.String(140), nullable=True),
        sa.Column("saldo_inicial", sa.Numeric(15, 2), nullable=False, server_default="0.00"),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_contas_bancarias_tenant_id", "contas_bancarias", ["tenant_id"])
    op.create_index("ix_contas_bancarias_empresa_id", "contas_bancarias", ["empresa_id"])

    op.create_table(
        "fornecedores",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("razao_social", sa.String(200), nullable=False),
        sa.Column("nome_fantasia", sa.String(200), nullable=True),
        sa.Column("cnpj_cpf", sa.String(20), nullable=True),
        sa.Column("tipo", sa.String(30), nullable=False, server_default="FORNECEDOR"),
        sa.Column("regime_tributario", sa.String(40), nullable=True),
        sa.Column("optante_simples", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("categoria_padrao", sa.String(120), nullable=True),
        sa.Column("forma_pgto_padrao", sa.String(30), nullable=True),
        sa.Column("pix_chave", sa.String(140), nullable=True),
        sa.Column("dados_banco", sa.Text(), nullable=True),
        sa.Column("cod_operacao", sa.String(120), nullable=True),
        sa.Column("cnae", sa.String(40), nullable=True),
        sa.Column("item_lc116", sa.String(20), nullable=True),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "cnpj_cpf", name="uq_fornecedor_tenant_doc"),
    )
    op.create_index("ix_fornecedores_tenant_id", "fornecedores", ["tenant_id"])
    op.create_index("ix_fornecedores_tenant_razao", "fornecedores", ["tenant_id", "razao_social"])

    op.create_table(
        "titulos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("empresa_id", sa.String(36), sa.ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fornecedor_id", sa.String(36), sa.ForeignKey("fornecedores.id", ondelete="SET NULL"), nullable=True),
        sa.Column("centro_custo_id", sa.String(36), sa.ForeignKey("centros_custo.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plano_conta_id", sa.String(36), sa.ForeignKey("plano_contas.id", ondelete="SET NULL"), nullable=True),
        sa.Column("conta_bancaria_id", sa.String(36), sa.ForeignKey("contas_bancarias.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tipo_movimento", sa.String(16), nullable=False, server_default="PAGAR"),
        sa.Column("tipo_documento", sa.String(30), nullable=False, server_default="NFSE"),
        sa.Column("numero_tx", sa.String(60), nullable=True),
        sa.Column("numero_documento", sa.String(60), nullable=False),
        sa.Column("favorecido_nome", sa.String(200), nullable=False),
        sa.Column("favorecido_doc", sa.String(20), nullable=True),
        sa.Column("dt_emissao", sa.Date(), nullable=False),
        sa.Column("dt_vencimento", sa.Date(), nullable=False),
        sa.Column("dt_pagamento", sa.Date(), nullable=True),
        sa.Column("valor_bruto", sa.Numeric(15, 2), nullable=False),
        sa.Column("valor_liquido", sa.Numeric(15, 2), nullable=False),
        sa.Column("valor_pago", sa.Numeric(15, 2), nullable=False, server_default="0.00"),
        sa.Column("juros_multa", sa.Numeric(15, 2), nullable=False, server_default="0.00"),
        sa.Column("desconto", sa.Numeric(15, 2), nullable=False, server_default="0.00"),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDENTE"),
        sa.Column("forma_pgto", sa.String(30), nullable=False, server_default="PIX"),
        sa.Column("cod_barras", sa.String(80), nullable=True),
        sa.Column("pix_chave", sa.String(140), nullable=True),
        sa.Column("cod_operacao", sa.String(120), nullable=True),
        sa.Column("cnae", sa.String(40), nullable=True),
        sa.Column("item_lc116", sa.String(20), nullable=True),
        sa.Column("descricao", sa.Text(), nullable=True),
        sa.Column("observacao", sa.Text(), nullable=True),
        sa.Column("solicitante", sa.String(120), nullable=True),
        sa.Column("conciliado", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_titulos_tenant_id", "titulos", ["tenant_id"])
    op.create_index("ix_titulos_empresa_id", "titulos", ["empresa_id"])
    op.create_index("ix_titulos_tenant_venc_status", "titulos", ["tenant_id", "dt_vencimento", "status"])
    op.create_index("ix_titulos_tenant_empresa", "titulos", ["tenant_id", "empresa_id"])

    op.create_table(
        "titulo_impostos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("titulo_id", sa.String(36), sa.ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("aliquota", sa.Numeric(6, 4), nullable=False, server_default="0.0000"),
        sa.Column("valor", sa.Numeric(15, 2), nullable=False),
        sa.Column("dt_vencimento", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDENTE"),
        sa.Column("codigo_receita", sa.String(20), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_titulo_impostos_tenant_id", "titulo_impostos", ["tenant_id"])
    op.create_index("ix_titulo_impostos_titulo_id", "titulo_impostos", ["titulo_id"])

    op.create_table(
        "titulo_rateios",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("titulo_id", sa.String(36), sa.ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("centro_custo_id", sa.String(36), sa.ForeignKey("centros_custo.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plano_conta_id", sa.String(36), sa.ForeignKey("plano_contas.id", ondelete="SET NULL"), nullable=True),
        sa.Column("rubrica", sa.String(60), nullable=False),
        sa.Column("valor", sa.Numeric(15, 2), nullable=False),
        sa.Column("percentual", sa.Numeric(7, 4), nullable=False),
        sa.Column("observacao", sa.String(255), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_titulo_rateios_tenant_id", "titulo_rateios", ["tenant_id"])
    op.create_index("ix_titulo_rateios_titulo_id", "titulo_rateios", ["titulo_id"])

    op.create_table(
        "titulo_aprovacoes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("titulo_id", sa.String(36), sa.ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("aprovador_id", sa.String(36), sa.ForeignKey("usuarios.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("decisao", sa.String(20), nullable=False),
        sa.Column("canal", sa.String(20), nullable=False, server_default="WEB"),
        sa.Column("justificativa", sa.Text(), nullable=True),
        sa.Column("decidido_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_titulo_aprovacoes_tenant_id", "titulo_aprovacoes", ["tenant_id"])
    op.create_index("ix_titulo_aprovacoes_titulo_id", "titulo_aprovacoes", ["titulo_id"])

    op.create_table(
        "despesas_recorrentes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("empresa_id", sa.String(36), sa.ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fornecedor_id", sa.String(36), sa.ForeignKey("fornecedores.id", ondelete="SET NULL"), nullable=True),
        sa.Column("centro_custo_id", sa.String(36), sa.ForeignKey("centros_custo.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plano_conta_id", sa.String(36), sa.ForeignKey("plano_contas.id", ondelete="SET NULL"), nullable=True),
        sa.Column("nome", sa.String(180), nullable=False),
        sa.Column("tipo_valor", sa.String(20), nullable=False, server_default="FIXO"),
        sa.Column("valor_estimado", sa.Numeric(15, 2), nullable=False),
        sa.Column("periodicidade", sa.String(20), nullable=False, server_default="MENSAL"),
        sa.Column("dia_vencimento", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("indice_reajuste", sa.String(20), nullable=True),
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_despesas_recorrentes_tenant_id", "despesas_recorrentes", ["tenant_id"])
    op.create_index("ix_despesas_recorrentes_empresa_id", "despesas_recorrentes", ["empresa_id"])

    op.create_table(
        "adiantamentos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("empresa_id", sa.String(36), sa.ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False),
        sa.Column("centro_custo_id", sa.String(36), sa.ForeignKey("centros_custo.id", ondelete="SET NULL"), nullable=True),
        sa.Column("solicitante", sa.String(150), nullable=False),
        sa.Column("data_liberacao", sa.Date(), nullable=False),
        sa.Column("valor_concedido", sa.Numeric(15, 2), nullable=False),
        sa.Column("forma", sa.String(30), nullable=False, server_default="PIX"),
        sa.Column("motivo", sa.Text(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="ABERTO"),
        sa.Column("data_fechamento", sa.Date(), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_adiantamentos_tenant_id", "adiantamentos", ["tenant_id"])
    op.create_index("ix_adiantamentos_empresa_id", "adiantamentos", ["empresa_id"])

    op.create_table(
        "adiantamento_comprovantes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("adiantamento_id", sa.String(36), sa.ForeignKey("adiantamentos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("data_gasto", sa.Date(), nullable=False),
        sa.Column("tipo_doc", sa.String(40), nullable=False),
        sa.Column("descricao", sa.String(255), nullable=False),
        sa.Column("valor", sa.Numeric(15, 2), nullable=False),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_adiantamento_comprovantes_tenant_id", "adiantamento_comprovantes", ["tenant_id"])
    op.create_index("ix_adiantamento_comprovantes_adiantamento_id", "adiantamento_comprovantes", ["adiantamento_id"])

    op.create_table(
        "extratos_bancarios",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conta_bancaria_id", sa.String(36), sa.ForeignKey("contas_bancarias.id", ondelete="CASCADE"), nullable=False),
        sa.Column("origem", sa.String(30), nullable=False, server_default="OFX"),
        sa.Column("arquivo_nome", sa.String(200), nullable=True),
        sa.Column("periodo_inicio", sa.Date(), nullable=True),
        sa.Column("periodo_fim", sa.Date(), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_extratos_bancarios_tenant_id", "extratos_bancarios", ["tenant_id"])
    op.create_index("ix_extratos_bancarios_conta_bancaria_id", "extratos_bancarios", ["conta_bancaria_id"])

    op.create_table(
        "extrato_transacoes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("extrato_id", sa.String(36), sa.ForeignKey("extratos_bancarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conta_bancaria_id", sa.String(36), sa.ForeignKey("contas_bancarias.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fitid", sa.String(120), nullable=False),
        sa.Column("data_movimento", sa.Date(), nullable=False),
        sa.Column("tipo", sa.String(16), nullable=False),
        sa.Column("valor", sa.Numeric(15, 2), nullable=False),
        sa.Column("memo", sa.String(255), nullable=True),
        sa.Column("conciliado", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint("conta_bancaria_id", "fitid", name="uq_extrato_conta_fitid"),
    )
    op.create_index("ix_extrato_transacoes_tenant_id", "extrato_transacoes", ["tenant_id"])
    op.create_index("ix_extrato_transacoes_extrato_id", "extrato_transacoes", ["extrato_id"])
    op.create_index("ix_extrato_transacoes_conta_bancaria_id", "extrato_transacoes", ["conta_bancaria_id"])

    op.create_table(
        "conciliacao_itens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("extrato_transacao_id", sa.String(36), sa.ForeignKey("extrato_transacoes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("titulo_id", sa.String(36), sa.ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("valor_alocado", sa.Numeric(15, 2), nullable=False),
        sa.Column("juros_alocado", sa.Numeric(15, 2), nullable=False, server_default="0.00"),
        sa.Column("desconto_alocado", sa.Numeric(15, 2), nullable=False, server_default="0.00"),
        sa.Column("metodo", sa.String(30), nullable=False, server_default="MANUAL"),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conciliacao_itens_tenant_id", "conciliacao_itens", ["tenant_id"])
    op.create_index("ix_conciliacao_itens_extrato_transacao_id", "conciliacao_itens", ["extrato_transacao_id"])
    op.create_index("ix_conciliacao_itens_titulo_id", "conciliacao_itens", ["titulo_id"])

    op.create_table(
        "documentos_anexos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("empresa_id", sa.String(36), sa.ForeignKey("empresas.id", ondelete="SET NULL"), nullable=True),
        sa.Column("entidade_tipo", sa.String(40), nullable=False),
        sa.Column("entidade_id", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False, unique=True),
        sa.Column("nome_original", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("tamanho_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("uploaded_by", sa.String(36), nullable=False),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
        sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documentos_anexos_tenant_id", "documentos_anexos", ["tenant_id"])
    op.create_index("ix_documentos_anexos_entidade_id", "documentos_anexos", ["entidade_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("usuario_id", sa.String(36), nullable=True),
        sa.Column("usuario_email", sa.String(180), nullable=True),
        sa.Column("acao", sa.String(60), nullable=False),
        sa.Column("entidade", sa.String(60), nullable=False),
        sa.Column("entidade_id", sa.String(64), nullable=False),
        sa.Column("dados_antes", sa.JSON(), nullable=True),
        sa.Column("dados_depois", sa.JSON(), nullable=True),
        sa.Column("ip_origem", sa.String(64), nullable=True),
        sa.Column("criado_em", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    op.create_index("ix_audit_logs_tenant_criado", "audit_logs", ["tenant_id", "criado_em"])


def downgrade() -> None:
    for tbl in [
        "audit_logs",
        "documentos_anexos",
        "conciliacao_itens",
        "extrato_transacoes",
        "extratos_bancarios",
        "adiantamento_comprovantes",
        "adiantamentos",
        "despesas_recorrentes",
        "titulo_aprovacoes",
        "titulo_rateios",
        "titulo_impostos",
        "titulos",
        "fornecedores",
        "contas_bancarias",
        "plano_contas",
        "refresh_tokens",
        "usuario_tenant_roles",
        "usuarios",
        "centros_custo",
        "empresas",
        "tenants",
    ]:
        op.drop_table(tbl)
