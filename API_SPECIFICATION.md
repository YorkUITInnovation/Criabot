# CriaBot API Specification

## Base URL
All endpoints are rooted at:
```
https://{HOST}:{PORT}/
```
Typically served over HTTPS on port defined in `docker.env` or default `8000`.

Authentication: API key via HTTP header `X-API-Key` or query parameter `?api_key=`.

---

## 1. Chats
Group of endpoints to manage chat sessions and messages.

### 1.1 Start Chat
POST /chats/start
- Description: Initialize a new chat session with a given bot.
- Request Body (application/json):
  ```json
  {
    "bot_id": "string",
    "user_id": "string",
    "parameters": { /* optional bot parameters override */ }
  }
  ```
- Response 201 Created:
  ```json
  {
    "chat_id": "string",
    "created_at": "2025-08-11T12:00:00Z"
  }
  ```

### 1.2 Send Message
POST /chats/send
- Description: Send a user message to an existing chat session; returns bot reply.
- Request Body:
  ```json
  {
    "chat_id": "string",
    "user_id": "string",
    "message": "string"
  }
  ```
- Response 200 OK:
  ```json
  {
    "chat_id": "string",
    "message_id": "string",
    "reply": "string",
    "timestamp": "2025-08-11T12:01:00Z"
  }
  ```

### 1.3 Chat History
GET /chats/history?chat_id={chat_id}&limit={n}
- Description: Retrieve recent chat messages.
- Query Parameters:
  - `chat_id` (string, required)
  - `limit` (integer, optional, default=50)
- Response 200 OK:
  ```json
  [
    {"message_id":"...","sender":"user","text":"...","timestamp":"..."},
    {"message_id":"...","sender":"bot","text":"...","timestamp":"..."}
  ]
  ```

### 1.4 Check Chat Existence
GET /chats/exists?chat_id={chat_id}
- Description: Returns whether a chat session exists.
- Query Parameters:
  - `chat_id` (string, required)
- Response 200 OK:
  ```json
  {"exists": true}
  ```

---

## 2. Content Management
CRUD endpoints for document and question assets.

### 2.1 Documents

#### Upload Document
POST /content/documents/upload
- Description: Upload a new document (e.g., PDF, text) for processing.
- Request: multipart/form-data
  - `file` (file, required)
  - `metadata` (JSON, optional)
- Response 201 Created:
  ```json
  {"document_id":"string","status":"pending"}
  ```

#### List Documents
GET /content/documents/list?page={n}&size={m}
- Description: Paginated list of uploaded documents.
- Query Parameters: `page` (int), `size` (int)
- Response 200 OK:
  ```json
  [{"document_id":"...","filename":"...","uploaded_at":"...","status":"processed"}, ...]
  ```

#### Update Document
PUT /content/documents/update
- Description: Update document metadata or content.
- Request Body:
  ```json
  {"document_id":"string","metadata":{"title":...,"tags":[...]}}
  ```
- Response 200 OK:
  ```json
  {"success": true}
  ```

#### Delete Document
DELETE /content/documents/delete?document_id={id}
- Description: Remove a stored document and associated data.
- Response 204 No Content

### 2.2 Questions

Endpoints mirror document CRUD, replacing `documents` with `questions`.
- POST /content/questions/upload
- GET /content/questions/list
- PUT /content/questions/update
- DELETE /content/questions/delete

---

## 3. Bot Management
Manage bot definitions and lifecycle.

### 3.1 Create Bot
POST /manage/create
- Description: Register a new bot configuration.
- Body:
  ```json
  {"bot_id":"string","name":"string","parameters":{...}}
  ```
- Response 201 Created:
  ```json
  {"bot_id":"string","created_at":"..."}
  ```

### 3.2 Update Bot
PUT /manage/update
- Description: Modify existing bot parameters.
- Body:
  ```json
  {"bot_id":"string","parameters":{...}}
  ```
- Response 200 OK:
  ```json
  {"success":true}
  ```

### 3.3 Delete Bot
DELETE /manage/delete?bot_id={id}
- Description: Remove a bot and all related data.
- Response 204 No Content

### 3.4 Bot About
GET /manage/about?bot_id={id}
- Description: Retrieve metadata and status for a bot.
- Response 200 OK:
  ```json
  {"bot_id":"string","name":"...","parameters":{...},"created_at":"..."}
  ```

---

## 4. API Documentation & Health

### Swagger UI
GET /docs
- Serves interactive API docs.

### OpenAPI JSON
GET /openapi.json
- Returns raw OpenAPI specification.

### Health Check
GET /health
- Returns service health and version.
- Response 200 OK:
  ```json
  {"status":"ok","uptime":"...","version":"1.0.0"}
  ```
