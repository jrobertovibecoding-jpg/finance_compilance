# JR FinCloud — Plataforma SaaS Financeira, Contas a Pagar & Compliance Fiscal

Plataforma de gestão financeira e compliance fiscal desenhada para **Varejo Multi-Loja / Shopping Centers** e **Escritórios de BPO Financeiro & Contadores** (JR Solutions).

---

## Principais Capacidades

- **Arquitetura Multi-Tenant (SaaS v2 `/api/v2`):** isolamento por `tenant_id` com **Row-Level Security (RLS)** no PostgreSQL 16, gestão de múltiplas empresas/filiais, centros de custo por loja e troca rápida de carteira para parceiros BPO (`/workspaces`, `/auth/switch-tenant`).
- **Rateio de Despesas de Shopping:** título financeiro único vinculado a múltiplas rubricas (`ALUGUEL`, `CONDOMINIO`, `FPP`, `IPTU`, `ENERGIA`, `AR_CONDICIONADO`) com trava matemática exata em `Decimal` e DRE Gerencial por Centro de Custo sem dupla contagem.
- **Motor Tributário & Reforma Tributária 2026:** cálculo de retenções (IRRF, PIS, COFINS, CSLL, ISS) com dispensa automática de retenções federais para optantes pelo **Simples Nacional / MEI**, limiar mínimo de DARF e demonstrativo informativo **IBS/CBS (EC 132/2023)**.
- **Workflow de Alçadas & Auditoria Imutável:** submissão, aprovação/reprovação por papel (`OWNER` / `DIRETOR_APROVADOR`), registro completo em `audit_logs`, Access JWT (15 min) e Refresh Token rotativo em cookie `HttpOnly` com detecção de reuso.
- **Automação Bancária & Conciliação:** remessa **CNAB 240 Itaú SISPAG** por empresa/conta bancária, importação OFX com deduplicação por `FITID`, conciliação 1:1, N:1 e pagamento parcial (1:N).
- **Storage Privado de Documentos:** emissão de *Presigned PUT/GET URLs* (AWS SigV4) compatível com **Supabase Storage S3** e **Cloudflare R2** para XML, PDF, OFX e comprovantes.

---

## Estrutura do Projeto

```text
├── .github/workflows/ci.yml              # CI (higiene Git, Alembic, JS check, pytest)
├── backend/
│   ├── alembic/                          # Migrações versionadas (schema + PostgreSQL RLS)
│   ├── app/
│   │   ├── api/                          # API v1 (/api/v1 & /api/contas-a-pagar) e v2 (/api/v2)
│   │   ├── core/                         # Config, segurança, rate limit, storage SigV4, CNAB 240
│   │   └── db/                           # SQLAlchemy 2.0 models, session e contexto RLS
│   ├── tests/                            # Suíte automatizada (segurança, financeiro e SaaS v2)
│   └── .env.example                      # Modelo de variáveis de ambiente
├── docs/ARQUITETURA_SAAS_JR_FINCLOUD.md  # Blueprint arquitetural e FinOps
├── frontend/                             # SPA/PWA de alta densidade visual
├── scripts/migrar_sqlite_para_postgres_saas.py  # ETL idempotente legado -> SaaS v2
├── Dockerfile                            # Build de produção para Railway
├── docker-compose.yml                    # Stack local (PostgreSQL 16 + FastAPI + Nginx)
├── railway.json                          # Config de deploy Railway
└── vercel.json                           # Config de deploy Vercel + security headers
```

---

## Como Rodar Localmente

### Opção 1: Docker Compose (PostgreSQL 16 + Backend + Frontend)
```bash
docker compose up --build
```
- Frontend: `http://localhost:3000`
- Backend / Swagger: `http://localhost:8000/docs`
- Healthcheck: `http://localhost:8000/health`

### Opção 2: Execução Direta em Python
```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```
Em outro terminal, sirva o frontend estático:
```bash
cd frontend
python -m http.server 3000
```

### Executar Testes Automatizados
```bash
pytest -v backend/tests
```

---

## Deploy em Produção (Supabase + Railway + Vercel)

1. **Banco & Storage (Supabase — `sa-east-1` São Paulo):** crie o projeto PostgreSQL, copie a *Connection String* (`postgresql+psycopg://...`) e crie o bucket privado `jr-fincloud-docs` gerando chaves S3.
2. **Backend (Railway):** conecte este repositório (`Dockerfile` + `railway.json`), configure as variáveis listadas em `backend/.env.example` (`ENV=production`, `JWT_SECRET_KEY`, `DATABASE_URL`, `CORS_ORIGINS`, chaves S3). O start executa `alembic upgrade head` automaticamente antes de subir o `uvicorn`.
3. **Frontend (Vercel):** conecte o repositório usando o `vercel.json` da raiz (que publica exclusivamente `frontend/` com headers de segurança) e configure a URL da API do Railway no frontend.
