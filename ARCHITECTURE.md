# CriaBot Architecture Document

## 1. Overview

CriaBot is a modular, microservice-inspired Python application designed to handle chat-based interactions, document management, and content workflows. It leverages FastAPI for HTTP APIs, Docker for containerization, and a lightweight caching and persistence layer.

Key features:
- HTTP-based API endpoints organized into controllers by domain (chats, content, docs, management, FAQ, gradebook, models)
- Central core for application setup, configuration, routing, and security
- Bot engine (`criabot`) for business logic around chat, document workflows, FAQ crawling, and AI-assisted gradebook setup
- Ragflow-native throughout: document parsing, model config, retrieval, and chat completions all go through Ragflow — no CriadexSDK or CriaParse dependency
- Cache layer (Redis-backed, with in-process fallback) for chats, web-search, rerank, and FAQ-search results
- Database layer for persistent storage of bots, chats, FAQ, and gradebook entities
- Automatic API documentation via OpenAPI/Swagger

## 2. Project Layout

```
/ (project root)
├─ app/                        # FastAPI application entrypoint and HTTP controllers
│  ├─ __main__.py              # CLI entry for running the app directly
│  ├─ controllers/             # HTTP routing handlers split by domain
│  │  ├─ chats/                # start, send, stream, query, history, exists, end
│  │  ├─ content/              # documents/ + questions/ CRUD (incl. native file upload)
│  │  ├─ manage/                # bot lifecycle (create, update, delete, about, parents, children)
│  │  ├─ faq/                   # FAQ crawl config, sync, status
│  │  ├─ gradebook/             # AI gradebook session workflow (10 endpoints)
│  │  ├─ models/                # Ragflow-tenant model listing for bot creation
│  │  └─ docs/                  # Swagger UI theming and OpenAPI overrides
│  └─ core/                    # Application core modules (config, app factory, middleware, security)
├─ criabot/                     # Core bot engine, schemas, cache, database, FAQ, gradebook
│  ├─ bot/                      # Bot/Chat orchestration
│  │  └─ chat/                  # chat.py, context.py, buffer.py, schemas.py, utils.py, web_search.py
│  ├─ cache/                    # Redis-backed cache layer (chats, web_searches, reranks, faq_searches)
│  ├─ database/                 # Persistence layer: bots/, faq/, gradebook/, table.py
│  ├─ faq/                      # FAQ site crawler, indexer, fallback matching
│  ├─ gradebook/                # Gradebook proposal/analysis/formula engine
│  ├─ migrations/               # Schema migration runner
│  ├─ criadex_client.py         # Inline HTTP client for Criadex (replaces the retired CriadexSDK)
│  └─ criadex_schemas.py        # Pydantic mirrors of Criadex's request/response shapes
├─ test/                        # Pytest suite
├─ build.sh                     # Build and local setup script
├─ Dockerfile                   # Container image build definition
├─ docker-compose.yml           # Generic, git-tracked deployment stack
├─ docker-compose.dev.yml       # Git-ignored local override (machine-specific network/resource tuning)
├─ searxng_data/                # SearXNG settings.yml / limiter.toml for the web-search service
├─ entrypoint.sh                # Container entrypoint setup
├─ requirements.txt             # Python dependencies
└─ README.md                    # Project overview and quickstart instructions
```

## 3. Deployment & Runtime

- **Docker & Docker-Compose**: Containerized via `Dockerfile` and orchestrated with `docker-compose.yml` (or a local `docker-compose.dev.yml` override). Environment configured via `docker.env` / `.env` per service.
- **Health Checks**: `docker-compose.yml` includes health checks for Criabot, Criadex, Ragflow, SearXNG, Elasticsearch, and MySQL to sequence startup correctly.
- **Entry Point**: `entrypoint.sh` initializes environment, applies migrations (`criabot/migrations/runner.py`), and starts the FastAPI server.
- **Build Script**: `build.sh` automates dependency installation, linting, and image building.
- **CI**: `.github/workflows/ci.yml` runs the pytest suite (`test/`) on push.

## 4. HTTP Layer and Routing

1. **app/core/app.py**: Creates FastAPI instance, applies middleware, mounts Swagger UI under `/`.
2. **app/controllers/**: Routers by domain, registered in `app/controllers/__init__.py`:
   - `/chats`: start, send, stream (SSE), query, history, exists, end
   - `/content`: CRUD for documents and questions, plus raw-file native upload (`documents/upload/file`)
   - `/manage`: bot lifecycle (create, update, delete, about, parents, children)
   - `/faq`: FAQ crawl config (`PATCH /faq/config`), sync (`POST /faq/sync`), status (`GET /faq/status`)
   - `/gradebook`: AI-assisted gradebook session workflow — start, chat, proposal, accept, finalize, upload, reset, delete, sync, status
   - `/models`: `GET /models/list` — Ragflow-tenant-scoped model/provider listing for bot creation dropdowns (excludes non-Ragflow-backed Azure/Cohere/manual rows)
   - `/docs`: custom Swagger UI theming and OpenAPI overrides
3. **Route Registration**: `app/core/route.py` provides `CriaRoute`/`CriaRouter` base classes used by every controller's `fastapi_restful.cbv`-based view.

## 5. Security & API Keys

- **API Key Auth**: `app/core/security/get_api_key.py` validates each request; handlers (master/bots/any) in `app/core/security/handlers`. Most controllers gate write/admin routes behind `GetApiKeyMaster`, and per-bot routes behind `GetApiKeyBots`.
- **Middleware**: `app/core/middleware.py` handles logging, CORS, and request tracing.
- **Rate Limiting**: Per-route limiters (`app/controllers/schemas.py`) applied via decorators, e.g. `chat_limiter`, `bot_management_limiter`, `general_limiter`.

## 6. Bot Engine (Criabot)

- **Orchestration**: `criabot/criabot.py` manages bot CRUD, sessions, parameters, Ragflow model sync, and FAQ/gradebook entry points.
- **Criadex Access**: `criabot/criadex_client.py` is a thin inline `httpx`-based client that replaced the retired `CriadexSDK`; `criabot/criadex_schemas.py` mirrors Criadex's Pydantic request/response models.
- **Schemas**: Pydantic schemas in `criabot/schemas.py` and `criabot/bot/schemas.py` for validation; bot parameters (including web-search and FAQ-fallback toggles) live in `criabot/database/bots/tables/bot_params.py`.
- **Chat Logic**: `criabot/bot/chat/chat.py` (session/reply orchestration) and `criabot/bot/chat/context.py` (retrieval, prompt building, reranking) — both are packages, not flat files.
- **Direct Context Replies**: Optimization in `chat.py` that bypasses LLM generation if a single highly relevant document node is found (under 300 chars) or if multiple facts (2-5 nodes) can be returned as a direct bulleted summary.
- **Prompt Splitting**: `context.py` implements multi-query retrieval by splitting complex user prompts (e.g., "Give me a summary of A, B, and C") into individual search sub-queries to improve document coverage.
- **Reranking**: `context.py`'s `hybrid_rerank()` — Ragflow has no standalone rerank API, so it reuses the bot's own LLM (a chat completion with a ranking prompt) to rank retrieved passages.
- **Related Prompts**: Follow-up question suggestions are genuinely LLM-generated via Criadex's `related_prompts` agent (not a stub).
- **Web Search**: `criabot/bot/chat/web_search.py` — `WebSearchClient` queries the SearXNG service for explicit "search the web" requests and as a low-confidence-retrieval fallback; results become `WEB_SEARCH`-group context nodes with `source_url` metadata. Gated by a global toggle and a per-bot toggle (`web_search_global_enabled` / `web_search_enabled`).
- **FAQ Subsystem**: `criabot/faq/` crawls a configured website, indexes pages into a bot's knowledge base, and provides fallback FAQ matching when RAG retrieval confidence is low (`faq_fallback_enabled` / `faq_fallback_threshold` bot params).
- **Gradebook Subsystem**: `criabot/gradebook/` runs a multi-turn, state-machine-driven session (analyzer, proposal, content mapper, formula parser/resolver) that turns Moodle course activities into a proposed gradebook category structure, exposed via `/gradebook/*`.
- **Buffering**: `criabot/bot/chat/buffer.py` for streaming and batched messages.

## 7. Caching Layer

- **Cache API**: `criabot/cache/api.py` exposes named cache objects (`chats`, `web_searches`, `reranks`, `faq_searches`) backed by Redis, with an in-process fallback when Redis isn't configured.
- **Core Logic**: `criabot/cache/core.py` / `criabot/cache/ttl.py` handle connection pooling, TTL, and key patterns; individual objects live under `criabot/cache/objects/`.

## 8. Persistence Layer

- **Bots**: `criabot/database/bots/` — tables/CRUD for bots, bot parameters, and parent/child relationships.
- **FAQ**: `criabot/database/faq/faq_db.py` — crawl run history and sync state.
- **Gradebook**: `criabot/database/gradebook/gradebook_db.py` — session/result persistence.
- **Migrations**: `criabot/migrations/` — versioned schema migrations applied at startup via `entrypoint.sh`.

## 9. Documentation & OpenAPI

- **Swagger UI**: Auto-generated by FastAPI, styled via `app/controllers/docs/theme.css`, served at `/` (master-key gated in production; `/docs` redirects to `/`).
- **Custom OpenAPI**: Overrides in `app/controllers/docs/openapi.py`.

## 10. Future Considerations

- Extend authentication (OAuth2/JWT)
- Scale with managed cache and DB services
- Integrate observability (Prometheus, OpenTelemetry)
- Genuine cross-encoder reranking via Ragflow's `POST /api/v1/retrieval` (`rerank_id`) instead of LLM-prompted ranking — captured as a future idea, not scheduled; see `Resource/future/ragflow_retrieval_swap.md`
- `local_cria` (Moodle plugin) refactor to call Criabot's `GET /models/list` instead of Criadex directly — deferred pending Patrick's plugin refactor

*Last updated 2026-07-02 — testing suite (pytest) and CI (GitHub Actions) are already in place; removed from this list as done.*
