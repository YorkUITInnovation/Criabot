# Criabot <img src="https://i.imgur.com/9XOI3qg.png" width=30>

Criabot is the central orchestration service for a suite of applications designed for Retrieval-Augmented Generation (RAG). It manages bots, authentication, and coordinates between various backend services.

## Architecture Overview

This project uses a microservice architecture orchestrated with Docker Compose. The core services include:

- **Criabot**: The main bot management and API gateway service.
- **Criadex**: A backend service providing core business logic and wrapping RAG functionalities.
- **Ragflow**: The core RAG engine, responsible for document processing, search, and generation.
- **Elasticsearch**: Serves as the primary vector store and search index for documents.
- **MinIO**: An S3-compatible object storage service used by Ragflow to store documents and other assets.
- **MySQL**: The relational database used for storing metadata for bots, users, and other system components.
- **Redis**: Used for caching and as a message broker for background tasks.
- **Other Services**: Includes `CriaParse` for document parsing, `CriaEmbed` for handling embeddings, and the `Cria` Moodle frontend.

## Prerequisites

- Docker
- Docker Compose

## Setup and Configuration

1.  **Environment File**: Before launching the stack, you must create a `.env` file in the root of this `Criabot` project directory.

2.  **Content for `.env` file**: Copy the following content into the `.env` file. These are the default credentials used by the services in the `docker-compose.yml` file.

    ```
    ELASTIC_PASSWORD=elastic
    REDIS_PASSWORD=password
    MYSQL_ROOT_PASSWORD=cria
    MINIO_ROOT_USER=admin
    MINIO_ROOT_PASSWORD=password
    ```

3.  **MySQL Data Volume (Important)**: On the very first run, the MySQL container may fail to initialize correctly if an old data volume exists. If you encounter issues with the `rag_flow` database not being found, you may need to fully stop the stack and remove the old MySQL data directory before restarting:
    ```sh
    # Warning: This deletes all local database data!
    sudo docker-compose down
    sudo rm -rf ./mysql_data
    ```

    After recreating the database, you will need to restore any necessary data from your backups.

## Running the Stack

Once the `.env` file is created, you can start the entire application stack with a single command:

```sh
sudo docker-compose up -d
```

## Accessing Services

Once the containers are running, the various web interfaces can be accessed at the following URLs:

- **Main Frontend (Moodle):** `http://127.0.0.1:80`
- **Ragflow API:** `http://127.0.0.1:8080` (Note: The `v0.20.5` image is API-only and does not serve a web UI.)
- **MinIO Console (Object Storage):** `http://127.0.0.1:9001`
- **MailHog (Email Testing):** `http://127.0.0.1:8025`