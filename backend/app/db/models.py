from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    nome_fantasia: Mapped[str] = mapped_column(String(180), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    documento: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    plano: Mapped[str] = mapped_column(String(40), default="PRO", nullable=False)
    status_assinatura: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    is_bpo_partner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    parent_bpo_tenant_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    config_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    empresas: Mapped[list["Empresa"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class Empresa(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "empresas"

    razao_social: Mapped[str] = mapped_column(String(200), nullable=False)
    nome_fantasia: Mapped[str] = mapped_column(String(180), nullable=False)
    cnpj: Mapped[str] = mapped_column(String(20), nullable=False)
    regime_tributario: Mapped[str] = mapped_column(String(40), default="LUCRO_PRESUMIDO", nullable=False)
    inscricao_municipal: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    cidade: Mapped[str] = mapped_column(String(100), default="SALVADOR", nullable=False)
    uf: Mapped[str] = mapped_column(String(2), default="BA", nullable=False)
    cep: Mapped[str] = mapped_column(String(10), default="40000000", nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenant: Mapped["Tenant"] = relationship(back_populates="empresas")
    centros_custo: Mapped[list["CentroCusto"]] = relationship(back_populates="empresa", cascade="all, delete-orphan")
    contas_bancarias: Mapped[list["ContaBancaria"]] = relationship(back_populates="empresa", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("tenant_id", "cnpj", name="uq_empresa_tenant_cnpj"),
        Index("ix_empresas_tenant_ativo", "tenant_id", "ativo"),
    )


class CentroCusto(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "centros_custo"

    empresa_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    codigo: Mapped[str] = mapped_column(String(30), nullable=False)
    nome: Mapped[str] = mapped_column(String(150), nullable=False)
    tipo_unidade: Mapped[str] = mapped_column(String(40), default="LOJA_SHOPPING", nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    empresa: Mapped["Empresa"] = relationship(back_populates="centros_custo")

    __table_args__ = (
        UniqueConstraint("tenant_id", "empresa_id", "codigo", name="uq_ccusto_tenant_emp_cod"),
    )


class Usuario(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "usuarios"

    nome: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(180), unique=True, nullable=False, index=True)
    senha_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ultimo_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    roles: Mapped[list["UsuarioTenantRole"]] = relationship(back_populates="usuario", cascade="all, delete-orphan")


class UsuarioTenantRole(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "usuario_tenant_roles"

    usuario_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(40), default="FINANCEIRO", nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    usuario: Mapped["Usuario"] = relationship(back_populates="roles")

    __table_args__ = (
        UniqueConstraint("usuario_id", "tenant_id", name="uq_usuario_tenant_role"),
    )


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "refresh_tokens"

    usuario_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_token_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_origem: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PlanoConta(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "plano_contas"

    codigo: Mapped[str] = mapped_column(String(30), nullable=False)
    nome: Mapped[str] = mapped_column(String(180), nullable=False)
    macro_grupo: Mapped[str] = mapped_column(String(100), nullable=False)
    tipo: Mapped[str] = mapped_column(String(20), default="DESPESA", nullable=False)
    dre_grupo: Mapped[str] = mapped_column(String(60), default="DESPESAS_OPERACIONAIS", nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "codigo", name="uq_planoconta_tenant_cod"),
    )


class ContaBancaria(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "contas_bancarias"

    empresa_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    banco_codigo: Mapped[str] = mapped_column(String(10), default="341", nullable=False)
    banco_nome: Mapped[str] = mapped_column(String(80), default="Banco Itaú", nullable=False)
    agencia: Mapped[str] = mapped_column(String(10), nullable=False)
    conta: Mapped[str] = mapped_column(String(20), nullable=False)
    dac: Mapped[str] = mapped_column(String(2), default="0", nullable=False)
    convenio_sispag: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    chave_pix_padrao: Mapped[Optional[str]] = mapped_column(String(140), nullable=True)
    saldo_inicial: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    empresa: Mapped["Empresa"] = relationship(back_populates="contas_bancarias")


class Fornecedor(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "fornecedores"

    razao_social: Mapped[str] = mapped_column(String(200), nullable=False)
    nome_fantasia: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    cnpj_cpf: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    tipo: Mapped[str] = mapped_column(String(30), default="FORNECEDOR", nullable=False)
    regime_tributario: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    optante_simples: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    categoria_padrao: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    forma_pgto_padrao: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    pix_chave: Mapped[Optional[str]] = mapped_column(String(140), nullable=True)
    dados_banco: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cod_operacao: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    cnae: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    item_lc116: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "cnpj_cpf", name="uq_fornecedor_tenant_doc"),
        Index("ix_fornecedores_tenant_razao", "tenant_id", "razao_social"),
    )


class Titulo(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "titulos"

    empresa_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fornecedor_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("fornecedores.id", ondelete="SET NULL"), nullable=True, index=True
    )
    centro_custo_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("centros_custo.id", ondelete="SET NULL"), nullable=True, index=True
    )
    plano_conta_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("plano_contas.id", ondelete="SET NULL"), nullable=True
    )
    conta_bancaria_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("contas_bancarias.id", ondelete="SET NULL"), nullable=True
    )
    tipo_movimento: Mapped[str] = mapped_column(String(16), default="PAGAR", nullable=False)
    tipo_documento: Mapped[str] = mapped_column(String(30), default="NFSE", nullable=False)
    numero_tx: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    numero_documento: Mapped[str] = mapped_column(String(60), nullable=False)
    favorecido_nome: Mapped[str] = mapped_column(String(200), nullable=False)
    favorecido_doc: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    dt_emissao: Mapped[date] = mapped_column(Date, nullable=False)
    dt_vencimento: Mapped[date] = mapped_column(Date, nullable=False)
    dt_pagamento: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    valor_bruto: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    valor_liquido: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    valor_pago: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    juros_multa: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    desconto: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDENTE", nullable=False)
    forma_pgto: Mapped[str] = mapped_column(String(30), default="PIX", nullable=False)
    cod_barras: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    pix_chave: Mapped[Optional[str]] = mapped_column(String(140), nullable=True)
    cod_operacao: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    cnae: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    item_lc116: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    descricao: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    observacao: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    solicitante: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    conciliado: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    impostos: Mapped[list["TituloImposto"]] = relationship(back_populates="titulo", cascade="all, delete-orphan")
    itens: Mapped[list["TituloItem"]] = relationship(back_populates="titulo", cascade="all, delete-orphan")
    rateios: Mapped[list["TituloRateio"]] = relationship(back_populates="titulo", cascade="all, delete-orphan")
    aprovacoes: Mapped[list["TituloAprovacao"]] = relationship(back_populates="titulo", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_titulos_tenant_venc_status", "tenant_id", "dt_vencimento", "status"),
        Index("ix_titulos_tenant_empresa", "tenant_id", "empresa_id"),
    )


class TituloImposto(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "titulo_impostos"

    titulo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)  # ISS, IRRF, PIS, COFINS, CSLL, INSS, IBS, CBS
    aliquota: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.0000"), nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    dt_vencimento: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDENTE", nullable=False)
    codigo_receita: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    titulo: Mapped["Titulo"] = relationship(back_populates="impostos")


class TituloItem(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "titulo_itens"

    titulo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    numero_item: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    codigo_produto: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    descricao: Mapped[str] = mapped_column(String(300), nullable=False)
    ncm: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    cfop: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    unidade: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    quantidade: Mapped[Decimal] = mapped_column(Numeric(15, 4), default=Decimal("1.0000"), nullable=False)
    valor_unitario: Mapped[Decimal] = mapped_column(Numeric(15, 4), default=Decimal("0.0000"), nullable=False)
    valor_total: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    fornecedor_nome: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    numero_nf: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    dt_emissao: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    titulo: Mapped["Titulo"] = relationship(back_populates="itens")

    __table_args__ = (
        Index("ix_titulo_itens_tenant_titulo", "tenant_id", "titulo_id"),
    )


class TituloRateio(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "titulo_rateios"

    titulo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    centro_custo_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("centros_custo.id", ondelete="SET NULL"), nullable=True
    )
    plano_conta_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("plano_contas.id", ondelete="SET NULL"), nullable=True
    )
    rubrica: Mapped[str] = mapped_column(String(60), nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    percentual: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    observacao: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    titulo: Mapped["Titulo"] = relationship(back_populates="rateios")


class TituloAprovacao(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "titulo_aprovacoes"

    titulo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    aprovador_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("usuarios.id", ondelete="RESTRICT"), nullable=False
    )
    decisao: Mapped[str] = mapped_column(String(20), nullable=False)  # APROVADO | REPROVADO
    canal: Mapped[str] = mapped_column(String(20), default="WEB", nullable=False)  # WEB | WHATSAPP
    justificativa: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decidido_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    titulo: Mapped["Titulo"] = relationship(back_populates="aprovacoes")


class DespesaRecorrente(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "despesas_recorrentes"

    empresa_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fornecedor_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("fornecedores.id", ondelete="SET NULL"), nullable=True
    )
    centro_custo_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("centros_custo.id", ondelete="SET NULL"), nullable=True
    )
    plano_conta_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("plano_contas.id", ondelete="SET NULL"), nullable=True
    )
    nome: Mapped[str] = mapped_column(String(180), nullable=False)
    tipo_valor: Mapped[str] = mapped_column(String(20), default="FIXO", nullable=False)
    valor_estimado: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    periodicidade: Mapped[str] = mapped_column(String(20), default="MENSAL", nullable=False)
    dia_vencimento: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    indice_reajuste: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Adiantamento(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "adiantamentos"

    empresa_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    centro_custo_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("centros_custo.id", ondelete="SET NULL"), nullable=True
    )
    solicitante: Mapped[str] = mapped_column(String(150), nullable=False)
    data_liberacao: Mapped[date] = mapped_column(Date, nullable=False)
    valor_concedido: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    forma: Mapped[str] = mapped_column(String(30), default="PIX", nullable=False)
    motivo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="ABERTO", nullable=False)
    data_fechamento: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    comprovantes: Mapped[list["AdiantamentoComprovante"]] = relationship(
        back_populates="adiantamento", cascade="all, delete-orphan"
    )


class AdiantamentoComprovante(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "adiantamento_comprovantes"

    adiantamento_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("adiantamentos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_gasto: Mapped[date] = mapped_column(Date, nullable=False)
    tipo_doc: Mapped[str] = mapped_column(String(40), nullable=False)
    descricao: Mapped[str] = mapped_column(String(255), nullable=False)
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)

    adiantamento: Mapped["Adiantamento"] = relationship(back_populates="comprovantes")


class ExtratoBancario(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "extratos_bancarios"

    conta_bancaria_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("contas_bancarias.id", ondelete="CASCADE"), nullable=False, index=True
    )
    origem: Mapped[str] = mapped_column(String(30), default="OFX", nullable=False)
    arquivo_nome: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    periodo_inicio: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    periodo_fim: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    transacoes: Mapped[list["ExtratoTransacao"]] = relationship(
        back_populates="extrato", cascade="all, delete-orphan"
    )


class ExtratoTransacao(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "extrato_transacoes"

    extrato_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("extratos_bancarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conta_bancaria_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("contas_bancarias.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fitid: Mapped[str] = mapped_column(String(120), nullable=False)
    data_movimento: Mapped[date] = mapped_column(Date, nullable=False)
    tipo: Mapped[str] = mapped_column(String(16), nullable=False)  # DEBIT | CREDIT
    valor: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    memo: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    conciliado: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    extrato: Mapped["ExtratoBancario"] = relationship(back_populates="transacoes")

    __table_args__ = (
        UniqueConstraint("conta_bancaria_id", "fitid", name="uq_extrato_conta_fitid"),
    )


class ConciliacaoItem(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "conciliacao_itens"

    extrato_transacao_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("extrato_transacoes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    titulo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("titulos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    valor_alocado: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    juros_alocado: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    desconto_alocado: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    metodo: Mapped[str] = mapped_column(String(30), default="MANUAL", nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class DocumentoAnexo(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "documentos_anexos"

    empresa_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("empresas.id", ondelete="SET NULL"), nullable=True
    )
    entidade_tipo: Mapped[str] = mapped_column(String(40), nullable=False)  # TITULO | ADIANTAMENTO | EXTRATO
    entidade_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    nome_original: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    tamanho_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(36), nullable=False)


class AuditLog(UUIDPrimaryKeyMixin, TenantMixin, Base):
    __tablename__ = "audit_logs"

    usuario_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    usuario_email: Mapped[Optional[str]] = mapped_column(String(180), nullable=True)
    acao: Mapped[str] = mapped_column(String(60), nullable=False)
    entidade: Mapped[str] = mapped_column(String(60), nullable=False)
    entidade_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dados_antes: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    dados_depois: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip_origem: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_audit_logs_tenant_criado", "tenant_id", "criado_em"),
    )


TENANT_SCOPED_TABLES = [
    "empresas",
    "centros_custo",
    "usuario_tenant_roles",
    "refresh_tokens",
    "plano_contas",
    "contas_bancarias",
    "fornecedores",
    "titulos",
    "titulo_impostos",
    "titulo_rateios",
    "titulo_aprovacoes",
    "despesas_recorrentes",
    "adiantamentos",
    "adiantamento_comprovantes",
    "extratos_bancarios",
    "extrato_transacoes",
    "conciliacao_itens",
    "documentos_anexos",
    "audit_logs",
]
