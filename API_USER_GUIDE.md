# CriaBot API User Guide

This guide shows how to interact with the CriaBot HTTP API using `curl`. For each endpoint, you’ll see required headers, request examples, and sample responses.

## Prerequisites
- You must have a valid API key. Set it in `X-API-Key` header or as query parameter `api_key`.
- `HOST` and `PORT` point to your running service (default `http://localhost:8000`).

Example environment variables:
```bash	export HOST=http://localhost
	export PORT=8000
	export API_KEY=your_api_key_here
```

## Common Headers
```
Content-Type: application/json
X-API-Key: ${API_KEY}
```

---

## 1. Chat Endpoints

### 1.1 Start a New Chat
POST /chats/start

Request:
```bash
curl -X POST "${HOST}:${PORT}/chats/start" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "bot_id": "my_bot",
    "user_id": "user123",
    "parameters": { "temperature": 0.7 }
}'
```

Response (201):
```json
{
  "chat_id": "chat_abc123",
  "created_at": "2025-08-11T12:00:00Z"
}
```

### 1.2 Send a Message
POST /chats/send

Request:
```bash
curl -X POST "${HOST}:${PORT}/chats/send" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "chat_id": "chat_abc123",
    "user_id": "user123",
    "message": "Hello, how are you?"
}'
```

Response (200):
```json
{
  "chat_id": "chat_abc123",
  "message_id": "msg_001",
  "reply": "I’m doing well, thank you! How can I assist you today?",
  "timestamp": "2025-08-11T12:01:00Z"
}
```

### 1.3 Retrieve Chat History
GET /chats/history

Request:
```bash
curl "${HOST}:${PORT}/chats/history?chat_id=chat_abc123&limit=20" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200):
```json
[
  {"message_id":"msg_000","sender":"user","text":"Hi","timestamp":"2025-08-11T12:00:00Z"},
  {"message_id":"msg_001","sender":"bot","text":"Hello!","timestamp":"2025-08-11T12:00:05Z"}
]
```

### 1.4 Check Chat Existence
GET /chats/exists

Request:
```bash
curl "${HOST}:${PORT}/chats/exists?chat_id=chat_abc123" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200):
```json
{ "exists": true }
```

---

## 2. Content Management

### 2.1 Documents

#### Upload a Document
POST /content/documents/upload

Request:
```bash
curl -X POST "${HOST}:${PORT}/content/documents/upload" \
  -H "X-API-Key: ${API_KEY}" \
  -F "file=@/path/to/file.pdf" \
  -F "metadata={\"title\":\"My Doc\"};type=application/json"
```

Response (201):
```json
{ "document_id": "doc_123", "status": "pending" }
```

#### List Documents
GET /content/documents/list

Request:
```bash
curl "${HOST}:${PORT}/content/documents/list?page=1&size=10" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200):
```json
[
  {"document_id":"doc_123","filename":"file.pdf","uploaded_at":"...","status":"processed"},
  ...
]
```

#### Update Document Metadata
PUT /content/documents/update

Request:
```bash
curl -X PUT "${HOST}:${PORT}/content/documents/update" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "document_id": "doc_123",
    "metadata": {"title": "Updated Title", "tags": ["tag1", "tag2"]}
}'
```

Response (200):
```json
{ "success": true }
```

#### Delete a Document
DELETE /content/documents/delete

Request:
```bash
curl -X DELETE "${HOST}:${PORT}/content/documents/delete?document_id=doc_123" \
  -H "X-API-Key: ${API_KEY}"
```

Response (204): No content

### 2.2 Questions
Endpoints mirror documents (`upload`, `list`, `update`, `delete`) under `/content/questions`.
Use `question_id` in place of `document_id`.

---

## 3. Bot Management

### 3.1 Create a Bot
POST /manage/create

Request:
```bash
curl -X POST "${HOST}:${PORT}/manage/create" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "bot_id": "my_bot",
    "name": "SupportBot",
    "parameters": {"temperature": 0.5}
}'
```

Response (201):
```json
{ "bot_id": "my_bot", "created_at": "2025-08-11T12:05:00Z" }
```

### 3.2 Update a Bot
PUT /manage/update

Request:
```bash
curl -X PUT "${HOST}:${PORT}/manage/update" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "bot_id": "my_bot",
    "parameters": {"temperature": 0.9}
}'
```

Response (200):
```json
{ "success": true }
```

### 3.3 Delete a Bot
DELETE /manage/delete

Request:
```bash
curl -X DELETE "${HOST}:${PORT}/manage/delete?bot_id=my_bot" \
  -H "X-API-Key: ${API_KEY}"
```

Response (204): No content

### 3.4 Get Bot Info
GET /manage/about

Request:
```bash
curl "${HOST}:${PORT}/manage/about?bot_id=my_bot" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200):
```json
{
  "bot_id": "my_bot",
  "name": "SupportBot",
  "parameters": {"temperature": 0.9},
  "created_at": "2025-08-11T12:05:00Z"
}
```

---

## 4. Utility & Docs

### Swagger UI
Visit:
```
${HOST}:${PORT}/docs
```

### OpenAPI JSON
Fetch:
```bash
curl "${HOST}:${PORT}/openapi.json"
```

### Health Check
GET /health

Request:
```bash
curl "${HOST}:${PORT}/health"
```

Response (200):
```json
{ "status": "ok", "uptime": "1h23m", "version": "1.0.0" }
```

---

End of CriaBot API User Guide. For more details, see `API_SPECIFICATION.md` or the `/docs` UI.
