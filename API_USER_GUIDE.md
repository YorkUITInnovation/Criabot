# CriaBot API User Guide

This guide shows how to interact with the CriaBot HTTP API using `curl`. For each endpoint, you’ll see required headers, request examples, and sample responses.

## Prerequisites
- You must have a valid API key. Set it in `X-API-Key` header or as query parameter `api_key`.
- `HOST` and `PORT` point to your running service (default `http://localhost:25575`).

Example environment variables:
```bash
export HOST=http://localhost
export PORT=25575
export API_KEY=your_api_key_here
```

## Common Headers
```
Content-Type: application/json
X-API-Key: ${API_KEY}
```

---

## 1. Bot Management Endpoints

### 1.1 Create a Cria Bot
POST /bots/{bot_name}/manage/create

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/my-new-bot/manage/create" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "llm_model_id": 1,
    "embedding_model_id": 2,
    "rerank_model_id": 3
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully created the bot & their indexes.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "bot_api_key": "<generated_api_key>"
}
```

### 1.2 Configure Bot Hyperparameters
PATCH /bots/{bot_name}/manage/update

Request:
```bash
curl -X PATCH "${HOST}:${PORT}/bots/my-new-bot/manage/update" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "llm_model_id": 4,
    "embedding_model_id": 5,
    "rerank_model_id": 6
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully updated the bot.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS"
}
```

### 1.3 Delete a Cria Bot
DELETE /bots/{bot_name}/manage/delete

Request:
```bash
curl -X DELETE "${HOST}:${PORT}/bots/my-new-bot/manage/delete" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully deleted the bot.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS"
}
```

### 1.4 Get Bot Info
GET /bots/{bot_name}/manage/about

Request:
```bash
curl "${HOST}:${PORT}/bots/my-new-bot/manage/about" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully retrieved bot information.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "bot_info": {
    "bot_name": "my-new-bot-name",
    "llm_model_id": 4,
    "embedding_model_id": 5,
    "rerank_model_id": 6,
    "created_at": "<timestamp>",
    "updated_at": "<timestamp>"
  }
}
```

---

## 2. Chat Endpoints

### 2.1 Start a chat with a bot
POST /bots/chats/start

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/chats/start" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "bot_name": "my-new-bot-name"
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully started a new chat.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "chat_id": "<generated_chat_id>"
}
```

### 2.2 Query a bot
POST /bots/chats/{chat_id}/query

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/chats/your-chat-id/query" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "prompt": "What is the capital of France?",
    "bot_name": "my-new-bot-name",
    "extra_bots": []
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully sent the query",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "reply": {
    "message": "Paris",
    "completion_usage": {
      "prompt_tokens": 7,
      "completion_tokens": 2,
      "total_tokens": 9
    },
    "related_prompts": []
  }
}
```

### 2.3 Send a chat to a bot
POST /bots/chats/{chat_id}/send

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/chats/your-chat-id/send" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "prompt": "Hello, bot!",
    "bot_name": "my-new-bot-name",
    "extra_bots": []
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully sent the chat",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "reply": {
    "message": "Hello! How can I help you today?",
    "completion_usage": {
      "prompt_tokens": 5,
      "completion_tokens": 10,
      "total_tokens": 15
    },
    "related_prompts": []
  }
}
```

### 2.4 End a chat with a bot
DELETE /bots/chats/{chat_id}/end

Request:
```bash
curl -X DELETE "${HOST}:${PORT}/bots/chats/your-chat-id/end" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully ended the chat.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS"
}
```

### 2.5 Get the current buffered history of a chat
GET /bots/chats/{chat_id}/history

Request:
```bash
curl "${HOST}:${PORT}/bots/chats/your-chat-id/history" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully send the chat",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "history": [
    {
      "role": "user",
      "content": "Hello, bot!"
    },
    {
      "role": "assistant",
      "content": "Hello! How can I help you today?"
    }
  ]
}
```

### 2.6 Check if the chat with a given Id exists
GET /bots/chats/{chat_id}/exists

Request:
```bash
curl "${HOST}:${PORT}/bots/chats/your-chat-id/exists" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Checked if the chat 'your-chat-id' is active!",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "exists": true
}
```

---

## 3. Bot Content - Documents
CRUD endpoints for document assets.

### 3.1 Upload a document to the bot
POST /bots/{bot_name}/documents/upload

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/my-new-bot/documents/upload" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "file_name": "my-test-document.json",
    "file_contents": {
      "nodes": [
        {
          "text": "This is a test node.",
          "metadata": {}
        }
      ],
      "assets": []
    },
    "file_metadata": {}
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully added to the index. Save the 'document_name' field to be able to update it!",
  "code": "SUCCESS",
  "document_name": "my-test-document.json",
  "token_usage": 1
}
```

### 3.2 Update a document on the bot
PATCH /bots/{bot_name}/documents/update

Request:
```bash
curl -X PATCH "${HOST}:${PORT}/bots/my-new-bot/documents/update" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "file_name": "my-test-document.json",
    "file_contents": {
      "nodes": [
        {
          "text": "This is the updated content of the document.",
          "metadata": {}
        }
      ],
      "assets": []
    },
    "file_metadata": {}
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully updated the document.",
  "code": "SUCCESS",
  "document_name": "my-test-document.json",
  "token_usage": 1
}
```

### 3.3 Delete a document on the bot
DELETE /bots/{bot_name}/documents/delete

Request:
```bash
curl -X DELETE "${HOST}:${PORT}/bots/my-new-bot/documents/delete?document_name=my-test-document.json" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully deleted the documents from the index.",
  "code": "SUCCESS"
}
```

### 3.4 List documents stored in the bot
GET /bots/{bot_name}/documents/list

Request:
```bash
curl "${HOST}:${PORT}/bots/my-new-bot/documents/list" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully retrieved all documents names.",
  "code": "SUCCESS",
  "document_names": [
    "my-test-document.json"
  ]
}
```

---

## 4. Bot Content - Questions
CRUD endpoints for question assets.

### 4.1 Upload a question to the bot
POST /bots/{bot_name}/questions/upload

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/my-new-bot/questions/upload" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "file_name": "my-test-question.json",
    "file_contents": {
      "questions": [
        "What is the capital of France?"
      ],
      "answer": "Paris"
    },
    "file_metadata": {}
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully added to the index. Save the 'document_name' field to be able to update it!",
  "code": "SUCCESS",
  "document_name": "my-test-question.json",
  "token_usage": 1
}
```

### 4.2 Update a question on the bot
PATCH /bots/{bot_name}/questions/update

Request:
```bash
curl -X PATCH "${HOST}:${PORT}/bots/my-new-bot/questions/update" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "file_name": "my-test-question.json",
    "file_contents": {
      "questions": [
        "What is the capital of France?",
        "What is the largest city in France?"
      ],
      "answer": "Paris"
    },
    "file_metadata": {}
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully updated the document.",
  "code": "SUCCESS",
  "document_name": "my-test-question.json",
  "token_usage": 1
}
```

### 4.3 Delete a question on the bot
DELETE /bots/{bot_name}/questions/delete

Request:
```bash
curl -X DELETE "${HOST}:${PORT}/bots/my-new-bot/questions/delete?document_name=my-test-question.json" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully deleted the documents from the index.",
  "code": "SUCCESS"
}
```

### 4.4 List questions stored in the bot
GET /bots/{bot_name}/questions/list

Request:
```bash
curl "${HOST}:${PORT}/bots/my-new-bot/questions/list" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully retrieved all documents names.",
  "code": "SUCCESS",
  "document_names": [
    "my-test-question.json"
  ]
}
```

---

## 5. API Documentation & Health

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
curl "${HOST}:${PORT}/health" \
  -H "X-API-Key: ${API_KEY}"
```

Response (200 OK):
```json
{ "status": "ok", "uptime": "...", "version": "1.0.0" }
```

---

End of CriaBot API User Guide. For more details, see `API_SPECIFICATION.md` or the `/docs` UI.