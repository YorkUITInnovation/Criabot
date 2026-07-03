#!/bin/bash

################################################################################
# CRIA Stack Installation Script
# 
# Sets up all required directories, configuration files, and databases for the
# complete CRIA stack (Criabot, Criadex, CriaEmbed, Ragflow).
# 
# Usage: ./install.sh
################################################################################

set -euo pipefail

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Service credentials (safe defaults; can be changed in docker.env files)
MYSQL_ROOT_PASSWORD="cria"
MYSQL_CRIABOT_USER="root"
MYSQL_CRIABOT_DB="criabot"
MYSQL_CRIADEX_DB="criadex"
MYSQL_CRIAEMBED_DB="criaembed"
MYSQL_RAGFLOW_DB="rag_flow"

REDIS_PASSWORD="password"
ELASTIC_PASSWORD="elastic"
MINIO_ROOT_USER="minio_user"
MINIO_ROOT_PASSWORD="password"

# Criadex settings
CRIADEX_API_PORT="25574"
CRIADEX_API_KEY="password"
CRIADEX_MASTER_API_KEY="password"

# Criabot settings
CRIABOT_API_PORT="25575"
CRIABOT_INITIAL_MASTER_KEY="password"

# Ragflow (will be populated by user after initial setup)
RAGFLOW_PLACEHOLDER_KEY="[UPDATE_ME_FROM_RAGFLOW_UI]"
RAGFLOW_PLACEHOLDER_TENANT="[UPDATE_ME_FROM_RAGFLOW_UI]"
RAGFLOW_SECRET_KEY="a7f9e2c1d5b8f4a9c2e6f1b3d7a9c0e2f4b6d8e0f2a4c6e8f1b3d5a7c9e1f3"

################################################################################
# Helper Functions
################################################################################

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[✓]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

log_error() {
    echo -e "${RED}[✗]${NC} $1"
}

wait_for_mysql() {
    local max_attempts=30
    local attempt=1
    
    log_step "Waiting for MySQL to be ready..."
    
    while [ $attempt -le $max_attempts ]; do
        if docker exec cria_mysql_1 mysqladmin ping -u"$MYSQL_CRIABOT_USER" -p"$MYSQL_ROOT_PASSWORD" &>/dev/null; then
            log_success "MySQL is ready"
            return 0
        fi
        
        echo -n "."
        sleep 2
        ((attempt++))
    done
    
    log_error "MySQL did not become ready after ${max_attempts} attempts"
    return 1
}

database_exists() {
    local db_name="$1"
    docker exec cria_mysql_1 mysql -u"$MYSQL_CRIABOT_USER" -p"$MYSQL_ROOT_PASSWORD" -e "USE \`$db_name\`;" 2>/dev/null
    return $?
}

database_has_tables() {
    local db_name="$1"
    local table_count=$(docker exec cria_mysql_1 mysql -u"$MYSQL_CRIABOT_USER" -p"$MYSQL_ROOT_PASSWORD" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='\`$db_name\`' AND table_type='BASE TABLE';" 2>/dev/null | tail -1)
    [ "$table_count" -gt 0 ] 2>/dev/null
    return $?
}

initialize_ragflow_schema() {
    local db_name="$1"
    
    log_step "Initializing Ragflow schema (note: Ragflow will create additional tables)..."
    
    docker exec cria_mysql_1 mysql -u"$MYSQL_CRIABOT_USER" -p"$MYSQL_ROOT_PASSWORD" "$db_name" << 'EOSQL'
-- Core Ragflow tables
CREATE TABLE IF NOT EXISTS `tenant` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `user` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `email` varchar(255) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `user_tenant` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `user_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `tenant_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `knowledgebase` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `tenant_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tenant` (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `document` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `kb_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_kb` (`kb_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `file` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `kb_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_kb` (`kb_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `conversation` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `tenant_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tenant` (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `dialog` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `conversation_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `kb_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_conversation` (`conversation_id`),
  KEY `idx_kb` (`kb_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `llm` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `tenant_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tenant` (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `api_token` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci,
  `tenant_id` varchar(100) COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_tenant` (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `system_settings` (
  `id` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `key` varchar(255) COLLATE utf8mb4_unicode_ci,
  `value` longtext COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `key` (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
EOSQL
    
    if [ $? -eq 0 ]; then
        log_success "Ragflow schema initialized (minimal base tables)"
    else
        log_error "Failed to initialize Ragflow schema"
        return 1
    fi
}

create_ragflow_database() {
    local db_name="$MYSQL_RAGFLOW_DB"

    if database_exists "$db_name"; then
        if database_has_tables "$db_name"; then
            log_success "Database '$db_name' exists with proper structure"
            return 0
        else
            log_success "Database '$db_name' exists (empty - initializing schema)"
            initialize_ragflow_schema "$db_name"
            return $?
        fi
    else
        log_step "Creating database: $db_name (Ragflow RAG engine database)"

        if docker exec cria_mysql_1 mysql -u"$MYSQL_CRIABOT_USER" -p"$MYSQL_ROOT_PASSWORD" -e "CREATE DATABASE IF NOT EXISTS \`$db_name\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null; then
            log_success "Database '$db_name' created - initializing schema"
            initialize_ragflow_schema "$db_name"
            return $?
        else
            log_error "Failed to create database '$db_name'"
            return 1
        fi
    fi
}

################################################################################
# Main Installation Steps
################################################################################

echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║         CRIA Stack Installation & Configuration Setup             ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Step 0: Pull all Docker images before making any other changes.
# `docker compose pull` blocks until every image finishes pulling, so no
# separate wait loop is needed — the command simply doesn't return until done.
log_step "Pulling Docker images (this may take a while)..."

if [ ! -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    log_error "docker-compose.yml not found at $SCRIPT_DIR"
    exit 1
fi

if ! docker compose -f "$SCRIPT_DIR/docker-compose.yml" pull; then
    log_error "Failed to pull Docker images"
    exit 1
fi
log_success "All Docker images pulled"

# Step 1: Create required directories
log_step "Creating required directories..."

mkdir -p "$SCRIPT_DIR/criabot_data"
mkdir -p "$SCRIPT_DIR/criadex_data"
mkdir -p "$SCRIPT_DIR/criaembed-api_data"
mkdir -p "$SCRIPT_DIR/ragflow_data"
mkdir -p "$SCRIPT_DIR/nginx"
mkdir -p "$SCRIPT_DIR/searxng_data"

log_success "All directories created"

# Step 2: Create .env file (compose-level environment variables)
log_step "Creating .env file (compose-level environment variables)..."

if [ -f "$SCRIPT_DIR/.env" ]; then
    log_success ".env already exists"
else
    cat > "$SCRIPT_DIR/.env" << EOF
# Criabot API Settings
APP_API_MODE=PRODUCTION
APP_API_PORT=$CRIABOT_API_PORT

# Criabot Settings
CRIADEX_API_BASE=http://criadex:$CRIADEX_API_PORT
CRIADEX_API_KEY=$CRIADEX_API_KEY
CRIADEX_MASTER_API_KEY=$CRIADEX_MASTER_API_KEY

# Redis Credentials (Cache)
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_USERNAME=default
REDIS_PASSWORD=$REDIS_PASSWORD

# MySQL Credentials (Management)
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_USERNAME=$MYSQL_CRIABOT_USER
MYSQL_PASSWORD=$MYSQL_ROOT_PASSWORD
MYSQL_DATABASE=$MYSQL_CRIABOT_DB

# Chat expiry time
# Format: <number> <unit>
# Example: 1h = 1 hour, 2d = 2 days, 3w = 3 weeks, 4m = 4 months, 5y = 5 years
# Default is 2 hours.
CHAT_EXPIRE_TIME=2h

# Initial API Key
APP_INITIAL_MASTER_KEY=$CRIABOT_INITIAL_MASTER_KEY

# Criadex timeout (seconds)
CRIADEX_IO_TIMEOUT=300

# Environment variables for docker-compose.yml
ELASTIC_PASSWORD=$ELASTIC_PASSWORD
REDIS_PASSWORD=$REDIS_PASSWORD
MYSQL_ROOT_PASSWORD=$MYSQL_ROOT_PASSWORD
RAGFLOW_API_KEY=$RAGFLOW_PLACEHOLDER_KEY
MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASSWORD
MINIO_ROOT_USER=$MINIO_ROOT_USER

# Secret key used by Ragflow for signing tokens. Copied from the Ragflow
# container settings so Criadex can produce the serializer-wrapped
# Authorization header Ragflow expects.
RAGFLOW_SECRET_KEY=$RAGFLOW_SECRET_KEY

# Ragflow tenant ID for dialog creation
RAGFLOW_TENANT_ID=$RAGFLOW_PLACEHOLDER_TENANT

# Criadex service URL
CRIADEX_URL=http://criadex:$CRIADEX_API_PORT

# Graph RAG usage in chat retrieval
GRAPH_RAG_CHAT_ENABLED=true
GRAPH_RAG_CHAT_AUTO_BUILD=true

# Redis caching behavior
# Sliding expiry: refresh a session's TTL on read so active chats/gradebooks
# don't expire mid-use. Set false for a fixed TTL from creation. Default true.
CACHE_TOUCH_ON_READ=true
# Cache expensive, deterministic retrieval steps (best-effort; never blocks the
# request). Rerank key includes node fingerprints so index changes miss safely.
RERANK_CACHE_ENABLED=true
RERANK_CACHE_TTL_SECONDS=600
WEB_SEARCH_CACHE_ENABLED=true
WEB_SEARCH_CACHE_TTL_SECONDS=900
# Share the FAQ fallback cache across workers via Redis (L2) atop in-process L1.
FAQ_REDIS_CACHE_ENABLED=true
# Redis connection pool tuning (optional; sensible defaults in code).
REDIS_MAX_CONNECTIONS=50
REDIS_SOCKET_TIMEOUT_SECONDS=5
REDIS_CONNECT_TIMEOUT_SECONDS=5
REDIS_HEALTH_CHECK_INTERVAL_SECONDS=30
# Redis container memory ceiling (used by docker-compose).
REDIS_MAXMEMORY=512mb
EOF
    log_success ".env file created"
fi

# Step 3: Copy/Create nginx configuration
log_step "Setting up Nginx configuration..."

if [ -f "$SCRIPT_DIR/nginx/nginx.conf" ]; then
    log_success "Nginx config already exists"
else
    cat > "$SCRIPT_DIR/nginx/nginx.conf" << 'EOF'
user  root;
worker_processes  auto;

error_log  /var/log/nginx/error.log notice;
pid        /var/run/nginx.pid;


events {
    worker_connections  1024;
}


http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';

    access_log  /var/log/nginx/access.log  main;

    sendfile        on;
    #tcp_nopush     on;

    keepalive_timeout  65;

    #gzip  on;
    client_max_body_size 1024M;

    include /etc/nginx/conf.d/ragflow.conf;
}
EOF
    log_success "Nginx config created"
fi

if [ -f "$SCRIPT_DIR/nginx/ragflow.conf" ]; then
    log_success "Ragflow Nginx config already exists"
else
    cat > "$SCRIPT_DIR/nginx/ragflow.conf" << 'EOF'
server {
    listen 80;
    server_name _;
    root /ragflow/web/dist;

    gzip on;
    gzip_min_length 1k;
    gzip_comp_level 9;
    gzip_types text/plain application/javascript application/x-javascript text/css application/xml text/javascript application/x-httpd-php image/jpeg image/gif image/png;
    gzip_vary on;
    gzip_disable "MSIE [1-6]\..";

    location ~ ^/api/v1/admin {
        proxy_pass http://localhost:9381;
        include proxy.conf;
    }

    location ~ ^/(v1|api) {
        proxy_pass http://localhost:9380;
        include proxy.conf;
    }

    location / {
        index index.html;
        try_files $uri $uri/ /index.html;
    }

    location ~ ^/static/(css|js|media)/ {
        expires 10y;
        access_log off;
    }
}
EOF
    log_success "Ragflow Nginx config created"
fi

if [ -f "$SCRIPT_DIR/nginx/proxy.conf" ]; then
    log_success "Proxy config already exists"
else
    cat > "$SCRIPT_DIR/nginx/proxy.conf" << 'EOF'
proxy_set_header Host $host;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_buffer_size 1024k;
proxy_buffers 16 1024k;
proxy_busy_buffers_size 2048k;
proxy_temp_file_write_size 2048k;
EOF
    log_success "Proxy config created"
fi

# Step 4: Copy/Create SearXNG configuration
log_step "Setting up SearXNG configuration..."

if [ -f "$SCRIPT_DIR/searxng_data/settings.yml" ]; then
    log_success "SearXNG settings already exist"
else
    cat > "$SCRIPT_DIR/searxng_data/settings.yml" << 'EOF'
# SearXNG settings (override defaults safely)
# DuckDuckGo omitted: frequent CAPTCHA blocks from Docker/host IPs add log noise
# without improving reliability; Google/Bing/Wikipedia provide stable fallback coverage.
use_default_settings:
  engines:
    keep_only:
      - google
      - bing
      #- duckduckgo # bug from searxng
      - wikipedia
      - youtube

# `keep_only` doesn't clear an engine's own `disabled` flag — Bing ships
# disabled by default, so it never actually ran without this override.
engines:
  - name: bing
    disabled: false

server:
  port: 8080
  bind_address: "0.0.0.0"
  # secret_key is used for session encryption and as a seed for hashing algorithms.
  # For internal Docker-only services, a static key in the repo is acceptable for consistency.
  secret_key: "6164d11270278783e449a5b672727670783510e140d348a604f32e2c562507e1"
  image_proxy: true
  limiter: false

search:
  safe_search: 0
  autocomplete: ""
  formats:
    - html
    - json

# Valkey URL is provided via SEARXNG_VALKEY_URL in docker-compose.
EOF
    log_success "SearXNG settings created"
fi

if [ -f "$SCRIPT_DIR/searxng_data/limiter.toml" ]; then
    log_success "SearXNG limiter config already exists"
else
    cat > "$SCRIPT_DIR/searxng_data/limiter.toml" << 'EOF'
[botdetection]

# The prefix defines the number of leading bits in an address that are compared
# to determine whether or not an address is part of a (client) network.

ipv4_prefix = 32
ipv6_prefix = 48

# If the request IP is in trusted_proxies list, the client IP address is
# extracted from the X-Forwarded-For and X-Real-IP headers. This should be
# used if SearXNG is behind a reverse proxy or load balancer.

# Docker bridge + loopback: lets SearXNG read X-Forwarded-For / X-Real-IP from Criabot.
trusted_proxies = [
  '127.0.0.0/8',
  '::1',
  '172.16.0.0/12',
  '10.0.0.0/8',
]

[botdetection.ip_limit]

# To get unlimited access in a local network, by default link-local addresses
# (networks) are not monitored by the ip_limit
filter_link_local = false

# activate link_token method in the ip_limit method
link_token = false

[botdetection.ip_lists]

# In the limiter, the ip_lists method has priority over all other methods.
block_ip = []

pass_ip = []

# Activate passlist of (hardcoded) IPs from the SearXNG organization,
# e.g. `check.searx.space`.
pass_searxng_org = true
EOF
    log_success "SearXNG limiter config created"
fi

# Step 5: Create docker.env files with safe defaults
log_step "Creating docker.env configuration files..."

# Criabot docker.env
if [ -f "$SCRIPT_DIR/criabot_data/docker.env" ]; then
    log_success "Criabot docker.env already exists"
else
    cat > "$SCRIPT_DIR/criabot_data/docker.env" << EOF
# Criadex API Settings
APP_API_MODE=PRODUCTION
APP_API_PORT=$CRIABOT_API_PORT

# Criadex Settings
CRIADEX_API_BASE=http://criadex:$CRIADEX_API_PORT
CRIADEX_API_KEY=$CRIADEX_API_KEY

# Redis Credentials (Cache)
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_USERNAME=default
REDIS_PASSWORD=$REDIS_PASSWORD

# MySQL Credentials (Management)
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_USERNAME=$MYSQL_CRIABOT_USER
MYSQL_PASSWORD=$MYSQL_ROOT_PASSWORD
MYSQL_DATABASE=$MYSQL_CRIABOT_DB

# Elasticsearch
ELASTICSEARCH_HOST=elasticsearch
ELASTICSEARCH_PORT=9200
ELASTICSEARCH_API_KEY=None
ELASTICSEARCH_USERNAME=elastic
ELASTICSEARCH_PASSWORD=$ELASTIC_PASSWORD

# Initial API Key
APP_INITIAL_MASTER_KEY=$CRIABOT_INITIAL_MASTER_KEY
CRIADEX_MASTER_API_KEY=$CRIADEX_MASTER_API_KEY

# Criadex timeout (seconds) — applies to all Criadex HTTP calls from Criabot
CRIADEX_IO_TIMEOUT=300

# Chat expiry time.
# Format: <number> <unit>
# Example: 1h = 1 hour, 2d -= 2 days, 3w = 3 weeks, 4m = 4 months, 5y = 5 years
# Default is 2 hours.
CHAT_EXPIRE_TIME=2d

# Ragflow integration
RAGFLOW_TENANT_ID=$RAGFLOW_PLACEHOLDER_TENANT
CRIADEX_URL=http://criadex:$CRIADEX_API_PORT

# Graph RAG usage in chat retrieval
GRAPH_RAG_CHAT_ENABLED=true
GRAPH_RAG_CHAT_AUTO_BUILD=true

# Web search fallback settings
WEB_SEARCH_GLOBAL_ENABLED=true
WEB_SEARCH_PROVIDER=searxng
WEB_SEARCH_URL=http://searxng:8080
WEB_SEARCH_TIMEOUT_SECONDS=8
WEB_SEARCH_MAX_RESULTS=5
WEB_SEARCH_FALLBACK_ONLY=true
WEB_SEARCH_FALLBACK_SCORE_THRESHOLD=0.5

# Redis caching behavior
# Sliding expiry: refresh a session's TTL whenever it is read so active
# chats/gradebooks don't expire mid-use. Set to false to keep a fixed TTL
# from creation. Defaults to true in code if unset.
CACHE_TOUCH_ON_READ=true
# Cache expensive, deterministic retrieval steps (best-effort; never blocks
# the request). Rerank key includes node fingerprints so index changes miss.
RERANK_CACHE_ENABLED=true
RERANK_CACHE_TTL_SECONDS=600
WEB_SEARCH_CACHE_ENABLED=true
WEB_SEARCH_CACHE_TTL_SECONDS=900
# Share the FAQ fallback cache across workers via Redis (L2) on top of the
# in-process L1 cache.
FAQ_REDIS_CACHE_ENABLED=true
# Redis connection pool tuning (optional; sensible defaults in code).
REDIS_MAX_CONNECTIONS=50
REDIS_SOCKET_TIMEOUT_SECONDS=5
REDIS_CONNECT_TIMEOUT_SECONDS=5
REDIS_HEALTH_CHECK_INTERVAL_SECONDS=30

# Gradebook category alias mapping
GRADEBOOK_CATEGORY_ALIASES_JSON={"assignment":"Assignments","assignments":"Assignments","homework":"Assignments","hw":"Assignments","coursework":"Assignments","lab":"Labs","labs":"Labs","lap":"Labs","laboratory":"Labs","midterm":"Midterm","mid term":"Midterm","exam":"Final Exam","final":"Final Exam","final exam":"Final Exam","summative exam":"Final Exam"}
EOF
    log_success "Criabot docker.env created"
fi

# Criadex docker.env
if [ -f "$SCRIPT_DIR/criadex_data/docker.env" ]; then
    log_success "Criadex docker.env already exists"
else
    cat > "$SCRIPT_DIR/criadex_data/docker.env" << EOF
# Criadex API Settings
APP_API_MODE=PRODUCTION
APP_API_PORT=$CRIADEX_API_PORT

# MySQL Credentials
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_USERNAME=$MYSQL_CRIABOT_USER
MYSQL_PASSWORD=$MYSQL_ROOT_PASSWORD
MYSQL_DATABASE=$MYSQL_CRIADEX_DB

# Elasticsearch Credentials (Vector Database)
ELASTICSEARCH_HOST=elasticsearch
ELASTICSEARCH_PORT=9200
ELASTICSEARCH_API_KEY=None
ELASTICSEARCH_USERNAME=elastic
ELASTICSEARCH_PASSWORD=$ELASTIC_PASSWORD

# Ragflow env
RAGFLOW_URL=http://ragflow:9380
RAGFLOW_API_KEY=$RAGFLOW_PLACEHOLDER_KEY
RAGFLOW_KB_SYNC_ENABLED=true

# Allow Criadex to sign access tokens the same way Ragflow expects
# so it can send the serializer output in the Authorization header.
RAGFLOW_SECRET_KEY=$RAGFLOW_SECRET_KEY

# Graph RAG runtime flags
GRAPH_RAG_ENABLED=true
GRAPH_RAG_FALLBACK_ENABLED=true
GRAPH_BUILD_TIMEOUT_SECONDS=120
GRAPH_BUILD_POLL_INTERVAL_SECONDS=3
GRAPH_BUILD_MAX_RETRIES=2

# RAGFlow graph endpoints
RAGFLOW_GRAPH_BUILD_URL_TEMPLATE=http://ragflow:80/api/v1/datasets/{group_name}/graph/build
RAGFLOW_GRAPH_STATUS_URL_TEMPLATE=http://ragflow:80/api/v1/datasets/{group_name}/graph/status
RAGFLOW_GRAPH_SEARCH_URL_TEMPLATE=http://ragflow:80/api/v1/datasets/{group_name}/graph/search

# Initial API Key
APP_INITIAL_MASTER_KEY=password

# Ragflow DB access (for dialog creation)
RAGFLOW_DB_HOST=mysql
RAGFLOW_DB_USER=$MYSQL_CRIABOT_USER
RAGFLOW_DB_PASSWORD=$MYSQL_ROOT_PASSWORD
RAGFLOW_DB_NAME=$MYSQL_RAGFLOW_DB
RAGFLOW_TENANT_ID=$RAGFLOW_PLACEHOLDER_TENANT

# Redis search cache (DB 2 — separate from Criabot session cache on DB 0)
# REDIS_PASSWORD is resolved from the compose-level REDIS_PASSWORD variable and
# set in the container environment; load_dotenv won't override it.
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=2
EOF
    log_success "Criadex docker.env created"
fi

# CriaEmbed docker.env
if [ -f "$SCRIPT_DIR/criaembed-api_data/docker.env" ]; then
    log_success "CriaEmbed docker.env already exists"
else
    cat > "$SCRIPT_DIR/criaembed-api_data/docker.env" << EOF
# MySQL Credentials
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_USERNAME=$MYSQL_CRIABOT_USER
MYSQL_PASSWORD=$MYSQL_ROOT_PASSWORD
MYSQL_DATABASE=$MYSQL_CRIAEMBED_DB

CRIA_SERVER_URL="http://criadex:$CRIADEX_API_PORT/"
CRIA_SERVER_TOKEN=$CRIADEX_API_KEY
CRIA_BOT_SERVER_URL="http://criabot:$CRIABOT_API_PORT/"
CRIA_BOT_SERVER_TOKEN=$CRIABOT_INITIAL_MASTER_KEY
THIS_APP_URL="http://localhost:3003/embed-api"
WEB_APP_URL="http://localhost:4000/embed"
ASSETS_FOLDER_PATH="./dist/src/assets/"

DEFAULT_BOT_GREETING="Hello there! Got a question?"
APP_MODE=TEST

RATE_LIMIT_MINUTE_MAX=30
RATE_LIMIT_HOUR_MAX=120
RATE_LIMIT_DAY_MAX=1000

RATE_LIMIT_EMBED_MINUTE_MAX=15
RATE_LIMIT_EMBED_HOUR_MAX=100
RATE_LIMIT_EMBED_DAY_MAX=200

RATE_LIMIT_CHAT_MINUTE_MAX=50
RATE_LIMIT_CHAT_HOUR_MAX=100
RATE_LIMIT_CHAT_DAY_MAX=1000

AZURE_SPEECH_API_URL="https://canadacentral.tts.speech.microsoft.com/cognitives"
AZURE_SPEECH_API_KEY=

REDIS_HOST=redis
REDIS_PORT=6379
REDIS_USERNAME=default
REDIS_PASSWORD=$REDIS_PASSWORD

DEBUG_ENABLED=false

ELASTICSEARCH_HOST=elasticsearch
ELASTICSEARCH_PORT=9200
ELASTIC_PASSWORD=\${ELASTIC_PASSWORD}
EOF
    log_success "CriaEmbed docker.env created"
fi

# Ragflow docker.env
if [ -f "$SCRIPT_DIR/ragflow_data/docker.env" ]; then
    log_success "Ragflow docker.env already exists"
else
    cat > "$SCRIPT_DIR/ragflow_data/docker.env" << EOF
# !!! IMPORTANT: Update RAGFLOW_API_KEY from Ragflow UI after initial setup !!!
RAGFLOW_API_KEY=$RAGFLOW_PLACEHOLDER_KEY
RAGFLOW_SERVER_CONF_PATH=/ragflow/conf/service_conf.yaml

# Elasticsearch
ES_HOST=elasticsearch
ELASTIC_PASSWORD=$ELASTIC_PASSWORD

# MinIO (S3-compatible object storage)
MINIO_ROOT_USER=$MINIO_ROOT_USER
MINIO_ROOT_PASSWORD=$MINIO_ROOT_PASSWORD
MINIO_HOST=minio
MINIO_PORT=9000
MINIO_USER=$MINIO_ROOT_USER
MINIO_PASSWORD=$MINIO_ROOT_PASSWORD

# Redis
REDIS_PASSWORD=$REDIS_PASSWORD

# MySQL
MYSQL_PASSWORD=$MYSQL_ROOT_PASSWORD

# MinIO Configuration
MINERU_OUTPUT_DIR=/ragflow/data/mineru_output
STORAGE_IMPL=MINIO
EOF
    log_success "Ragflow docker.env created"
fi

# Step 6: Start the Docker Compose stack
echo ""
log_warning "Before proceeding, ensure Docker is running and ready"
log_step "Starting Docker Compose stack..."

if ! docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d; then
    log_error "Failed to start Docker Compose stack"
    exit 1
fi
log_success "Docker Compose stack started"

# Step 7: Wait for MySQL and create the Ragflow database
# Criabot, Criadex, and CriaEmbed each create their own database and base
# tables automatically on first startup (see their respective service code) —
# nothing to do for them here. Ragflow does not self-manage its schema, so
# install.sh still bootstraps it.
if ! wait_for_mysql; then
    log_warning "MySQL did not become ready. Some services may fail to initialize."
    log_warning "Please check MySQL container logs: docker logs cria_mysql_1"
else
    echo ""
    log_step "Initializing Ragflow database..."

    create_ragflow_database

    log_success "Ragflow database created/verified"
    echo ""
    log_step "Waiting for services to apply migrations (30 seconds)..."
    sleep 30

    echo ""
    log_step "Verifying database structure..."

    # Check rag_flow tables
    if docker exec cria_mysql_1 mysql -u"$MYSQL_CRIABOT_USER" -p"$MYSQL_ROOT_PASSWORD" -e "USE \`$MYSQL_RAGFLOW_DB\`; SELECT COUNT(*) as table_count FROM information_schema.tables WHERE table_schema='\`$MYSQL_RAGFLOW_DB\`' AND table_type='BASE TABLE';" 2>/dev/null | tail -1 | grep -q '[1-9]'; then
        log_success "Ragflow database has tables"
    else
        log_warning "Ragflow database tables not yet initialized (will be created by Ragflow service)"
    fi

    # Criabot/Criadex/CriaEmbed self-initialize on startup — just report status.
    for pair in "Criabot:$MYSQL_CRIABOT_DB:cria_criabot_1" "Criadex:$MYSQL_CRIADEX_DB:cria_criadex_1" "CriaEmbed:$MYSQL_CRIAEMBED_DB:cria_criaembed-api_1"; do
        IFS=':' read -r service_name db_name container_name <<< "$pair"
        if docker exec cria_mysql_1 mysql -u"$MYSQL_CRIABOT_USER" -p"$MYSQL_ROOT_PASSWORD" -e "USE \`$db_name\`; SELECT COUNT(*) as table_count FROM information_schema.tables WHERE table_schema='\`$db_name\`' AND table_type='BASE TABLE';" 2>/dev/null | tail -1 | grep -q '[1-9]'; then
            log_success "$service_name database has tables (self-initialized)"
        else
            log_warning "$service_name database not yet initialized — will self-create on first startup. Check: docker logs $container_name"
        fi
    done
fi

# Final summary
echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}✓ Installation Complete!${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}DATABASE STATUS:${NC}"
echo "   • Criabot:      $MYSQL_CRIABOT_DB (self-initialized by the Criabot service on startup)"
echo "   • Criadex:      $MYSQL_CRIADEX_DB (self-initialized by the Criadex service on startup)"
echo "   • CriaEmbed:    $MYSQL_CRIAEMBED_DB (self-initialized by the CriaEmbed service on startup)"
echo "   • Ragflow:      $MYSQL_RAGFLOW_DB (bootstrapped by this script; managed by Ragflow)"
echo ""
echo "   Note: Criabot/Criadex/CriaEmbed create their own database and tables"
echo "   automatically on first startup. Check container logs if a service's"
echo "   tables are not appearing:"
echo "   ${BLUE}docker logs cria_criabot_1${NC}"
echo "   ${BLUE}docker logs cria_criadex_1${NC}"
echo "   ${BLUE}docker logs cria_criaembed-api_1${NC}"
echo "   ${BLUE}docker logs cria_ragflow_1${NC}"
echo ""
echo -e "${YELLOW}NEXT STEPS - CRITICAL!${NC}"
echo ""
echo "1. Access Ragflow UI to obtain API credentials:"
echo "   ${BLUE}http://localhost:9381${NC}"
echo ""
echo "2. Update Ragflow API Key and Tenant ID in the following files:"
echo "   - $SCRIPT_DIR/criadex_data/docker.env"
echo "     Update: RAGFLOW_API_KEY and RAGFLOW_TENANT_ID"
echo ""
echo "   - $SCRIPT_DIR/criabot_data/docker.env"
echo "     Update: RAGFLOW_TENANT_ID"
echo ""
echo "   - $SCRIPT_DIR/ragflow_data/docker.env"
echo "     Update: RAGFLOW_API_KEY"
echo ""
echo "3. Restart the Docker Compose stack to apply changes:"
echo "   ${BLUE}docker compose -f $SCRIPT_DIR/docker-compose.yml down${NC}"
echo "   ${BLUE}docker compose -f $SCRIPT_DIR/docker-compose.yml up -d${NC}"
echo ""
echo -e "${YELLOW}Service URLs:${NC}"
echo "   Criabot API:        ${BLUE}http://localhost:$CRIABOT_API_PORT${NC}"
echo "   Criadex API:        ${BLUE}http://localhost:$CRIADEX_API_PORT${NC}"
echo "   Ragflow:            ${BLUE}http://localhost:9381${NC}"
echo "   SearXNG:            ${BLUE}http://localhost:8080${NC}"
echo "   Elasticsearch:      ${BLUE}http://localhost:9200${NC}"
echo "   Redis:              ${BLUE}localhost:6379${NC}"
echo "   MySQL:              ${BLUE}localhost:3306${NC}"
echo ""
echo -e "${YELLOW}Default Credentials:${NC}"
echo "   MySQL User:         ${BLUE}root${NC}"
echo "   MySQL Password:     ${BLUE}cria${NC}"
echo "   Elasticsearch User: ${BLUE}elastic${NC}"
echo "   Elasticsearch Pass: ${BLUE}elastic${NC}"
echo "   Redis Password:     ${BLUE}password${NC}"
echo ""
log_success "Installation setup complete!"
echo ""
