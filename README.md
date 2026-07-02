# Criabot <img src="https://i.imgur.com/9XOI3qg.png" width=30>

Criabot is the central orchestration service for a suite of applications designed for Retrieval-Augmented Generation (RAG). It manages bots, authentication, and coordinates between various backend services.

## Architecture Overview

This project uses a microservice architecture orchestrated with Docker Compose. The core services include:

- **Criabot**: The main bot management and API gateway service. Talks to Criadex over plain HTTP (`criabot/criadex_client.py`) — no SDK dependency.
- **Criadex**: A backend service providing core business logic and wrapping Ragflow's RAG functionality (retrieval, models, native document parsing).
- **Ragflow**: The core RAG engine (`infiniflow/ragflow`), responsible for document processing, search, and generation. Criabot/Criadex are Ragflow-native — legacy `CriadexSDK`/`CriaParse` document parsing have been fully retired.
- **SearXNG**: Self-hosted meta search engine used for the bot web-search feature (`criabot/bot/chat/web_search.py`), scoped to a small engine allowlist (google, bing, wikipedia, youtube).
- **Elasticsearch**: Serves as the primary vector store and search index for documents.
- **MinIO**: An S3-compatible object storage service used by Ragflow to store documents and other assets.
- **MySQL**: The relational database used for storing metadata for bots, users, and other system components.
- **Redis**: Used for caching (chat sessions, web-search/rerank/FAQ results) and as the Ragflow secret-key/limiter backing store.
- **Cria**: The Moodle frontend (`local_cria` plugin), mounted into the `cria` container.

## Prerequisites

- Docker
- Docker Compose

## Setup and Configuration

1.  **Environment File**: Before launching the stack, you must create a `.env` file in the root of this `Criabot` project directory.

2.  **Content for `.env` file**: Copy the following content into the `.env` file. These are the credentials/keys consumed by services in `docker-compose.yml`.

    ```
    ELASTIC_PASSWORD=elastic
    REDIS_PASSWORD=password
    MYSQL_ROOT_PASSWORD=cria
    MINIO_ROOT_USER=admin
    MINIO_ROOT_PASSWORD=password
    APP_INITIAL_MASTER_KEY=<criadex master API key>
    RAGFLOW_API_KEY=<ragflow API key, generated from the Ragflow UI/DB after first boot>
    RAGFLOW_SECRET_KEY=<criadex's credential for authenticating to ragflow>
    # Optional, defaults shown:
    REDIS_MAXMEMORY=512mb
    ```

3.  **MySQL Data Volume (Important)**: On the very first run, the MySQL container may fail to initialize correctly if an old data volume exists. If you encounter issues with the `rag_flow` database not being found, you may need to fully stop the stack and remove the old MySQL data directory before restarting:
    ```sh
    # Warning: This deletes all local database data!
    docker compose down
    rm -rf ./mysql_data
    ```

    After recreating the database, you will need to restore any necessary data from your backups.

## Running the Stack

Once the `.env` file is created, start the stack with:

```sh
docker compose up -d
```

If you're using a local dev override file instead of the tracked `docker-compose.yml`:

```sh
docker compose -f docker-compose.dev.yml up -d
```

## Accessing Services

Once the containers are running, the various web interfaces can be accessed at the following URLs:

- **Main Frontend (Moodle):** `http://127.0.0.1:80`
- **Ragflow API:** `http://127.0.0.1:8080`
- **Ragflow Web UI:** `http://127.0.0.1:9381`
- **MinIO Console (Object Storage):** `http://127.0.0.1:9001`
- **MailHog (Email Testing):** `http://127.0.0.1:8025`
- **SearXNG:** `http://127.0.0.1:8082`