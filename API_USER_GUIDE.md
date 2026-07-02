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

### 1.1 Create a Bot
POST /bots/{bot_name}/manage/create

Only `llm_model_id`, `embedding_model_id`, and `rerank_model_id` are required — get valid IDs from `GET /models/list` (§7.1), which only lists models actually configured on Ragflow's web UI for your tenant. Every hyperparameter below is optional and falls back to the default shown if omitted (see the full list in `criabot/database/bots/tables/bot_params.py`), including the web-search and FAQ-fallback toggles.

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/my-new-bot/manage/create" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "llm_model_id": 1,
    "embedding_model_id": 2,
    "rerank_model_id": 3,
    "parent_bot_names": ["optional-parent-name"],

    "temperature": 0.9,
    "top_n": 3,
    "min_n": 0.7,
    "llm_generate_related_prompts": true,
    "web_search_global_enabled": true,
    "web_search_enabled": false,
    "faq_fallback_enabled": true,
    "faq_fallback_threshold": 0.5
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

Same hyperparameters as create (all optional/partial) except the model IDs, `use_knowledge_graph`, and `requires_documents`, which are fixed at creation time.

Request:
```bash
curl -X PATCH "${HOST}:${PORT}/bots/my-new-bot/manage/update" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "parent_bot_names": ["optional-parent-name-1", "optional-parent-name-2"],
    "web_search_enabled": true
}'
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully updated the bot info.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS"
}
```

### 1.3 Delete a Bot
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
  "message": "Successfully retrieved the bot info.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "about": {
    "info": {
      "name": "test-bot-gemini",
      "id": 165,
      "created": "2025-11-19T14:05:26"
    },
    "params": {
      "max_input_tokens": 2000,
      "max_reply_tokens": 1024,
      "temperature": 0.9,
      "top_p": 0.0,
      "top_k": 10,
      "min_k": 0.5,
      "top_n": 3,
      "min_n": 0.7,
      "llm_generate_related_prompts": true,
      "no_context_message": "Sorry, I'm not sure about that.",
      "no_context_use_message": false,
      "no_context_llm_guess": false,
      "system_message": null,
      "bot_id": 165,
      "id": 165
    }
  }
}
```

### 1.5 List Parent Bots
GET /bots/{bot_name}/manage/parents

Request:
```bash
curl "${HOST}:${PORT}/bots/my-new-bot/manage/parents" \
  -H "X-API-Key: ${API_KEY}"
```

Sample Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully retrieved parent bots.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "parents": ["parent-bot-a", "parent-bot-b"]
}
```

### 1.6 List Child Bots
GET /bots/{bot_name}/manage/children

Request:
```bash
curl "${HOST}:${PORT}/bots/my-new-bot/manage/children" \
  -H "X-API-Key: ${API_KEY}"
```

Sample Response (200 OK):
```json
{
  "status": 200,
  "message": "Successfully retrieved child bots.",
  "timestamp": "<timestamp>",
  "code": "SUCCESS",
  "children": ["child-bot-a", "child-bot-b"]
}
```

---

## 2. Chat Endpoints

### 2.1 Start a chat with a bot
POST /bots/chats/start

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/chats/start" \
  -H "X-API-Key: ${API_KEY}"
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

Same request/response shape as §2.3 Send, below. `extra_bots` is merged automatically with the bot's inherited parent bots, so you only need to list *additional* bots here.

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/chats/your-chat-id/query" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{
    "prompt": "What is the capital of France?",
    "bot_name": "my-new-bot-name",
    "extra_bots": ["another-bot-name"]
}'
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
    "prompt": "Hello, bot!",
    "content": {
      "role": "assistant",
      "content": "Hello! How can I help you today?",
      "assets": [],
      "additional_kwargs": {},
      "metadata": {}
    },
    "token_usage": [{"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15}],
    "total_usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
    "search_units": 1,
    "history": ["... ChatMessage entries ..."],
    "related_prompts": [
      {"label": "Follow-up", "prompt": "What else can you tell me?", "llm_generated": true}
    ],
    "context": null,
    "group_responses": {},
    "verified_response": true,
    "faq_fallback_used": false,
    "faq_sources": []
  }
}
```

> **Note if you integrated against an older version of this guide:** the reply text moved from a top-level `message` field to `reply.content.content`, and token usage moved from `reply.completion_usage` to `reply.total_usage`. `related_prompts` are now genuinely LLM-generated (not a static/empty stub). When web search fires (see below), `reply.group_responses.WEB_SEARCH.nodes` carries the retrieved snippets with `source_url` metadata. `faq_fallback_used`/`faq_sources` are set when the reply came from the FAQ crawl index instead of RAG (§5).

### 2.3.1 Stream a chat with live reasoning + citations
POST /bots/chats/{chat_id}/stream

Same request body as §2.3, but the response is a Server-Sent Events stream — useful for showing retrieval progress and citations live instead of waiting for one JSON payload.

Request:
```bash
curl -N -X POST "${HOST}:${PORT}/bots/chats/your-chat-id/stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{"prompt": "Hello, bot!", "bot_name": "my-new-bot-name"}'
```

Each line is `data: {...}` with a `type` of `status` (retrieval/synthesis progress), `chunk` (partial response text), `citations` (source list), or `done` (elapsed time). On error you get a plain JSON error body instead of a stream.

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
      "blocks": [
        {
          "block_type": "text",
          "text": "Hello, bot!"
        }
      ],
      "additional_kwargs": {},
      "metadata": {
        "token_count": 4
      }
    },
    {
      "role": "assistant",
      "blocks": [
        {
          "block_type": "text",
          "text": "Hello! How can I help you today?"
        }
      ],
      "additional_kwargs": {},
      "metadata": {
        "token_count": 9
      }
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

### 3.1.1 Upload a raw file for native Ragflow parsing
POST /bots/{bot_name}/documents/upload/file

Preferred over §3.1 for real documents — upload the raw file and let Ragflow parse it natively (PDF, DOCX, HTML, etc.), instead of pre-constructing node/text JSON yourself.

Request:
```bash
curl -X POST "${HOST}:${PORT}/bots/my-new-bot/documents/upload/file" \
  -H "X-API-Key: ${API_KEY}" \
  -F "file=@./syllabus.pdf" \
  -F "strategy=GENERIC"
```

Response (200 OK):
```json
{
  "status": 200,
  "message": "File queued for native Ragflow parsing.",
  "code": "SUCCESS",
  "document_name": "syllabus.pdf"
}
```

`strategy` is optional (`GENERIC`, `ALSYLLABUS`, `ALSYLLABUSFR`, `PARAGRAPH`); HTML files are always routed through `PARAGRAPH` regardless of what you pass, since Ragflow has no native HTML parser.

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

## 5. FAQ Management

Crawls a configured website into a bot's knowledge base, and also serves as a fallback answer source when RAG retrieval confidence is low (below the bot's `faq_fallback_threshold`).

### 5.1 Update FAQ sync config
PATCH /faq/config

```bash
curl -X PATCH "${HOST}:${PORT}/faq/config" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{"source_url": "https://example.edu/faq", "group_name": "my-bot-faq", "max_pages": 50, "enabled": true}'
```

### 5.2 Trigger an FAQ sync
POST /faq/sync

```bash
curl -X POST "${HOST}:${PORT}/faq/sync" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{"trigger_graph_build": true}'
```

Response (200 OK):
```json
{
  "status": 200,
  "code": "SUCCESS",
  "state": "READY",
  "source_url": "https://example.edu/faq",
  "group_name": "my-bot-faq",
  "pages_crawled": 12,
  "indexed_files": 12,
  "duplicate_files": 0,
  "graph_build_job": null
}
```

### 5.3 Get FAQ sync status
GET /faq/status

```bash
curl "${HOST}:${PORT}/faq/status" -H "X-API-Key: ${API_KEY}"
```

Returns the current lifecycle `state` (`NOT_RUN`/`READY`/etc.), last run/success timestamps, whether the index is `stale`, and recent run history.

---

## 6. Gradebook

A multi-turn, state-machine-driven session that turns Moodle course activities into a proposed gradebook category structure. Every step after "start" is keyed by the `session_id` it returns.

**What you get back:** most responses below carry a `proposal` and/or `content_mapping` field. These aren't opaque — `proposal` is always a `{categories: [...], not_graded_items: [...], notes: [...], aggregation_method: 13}` object (each category has `name`, `weight`, `items`, `subcategories`, plus drop-lowest/hidden/locked/formula settings), and `content_mapping` is always `{graded_activities: [...], unmatched_activities: [...], uncategorized_activities: [...], validation_errors: [...], llm_mapper_chat_id, validation: {errors, warnings, can_proceed}}` where each activity row has `activity_name`, `suggested_category`/`confirmed_category`, `confidence`, `mapping_method` (`deterministic`/`llm`/`manual`), etc. `§6.8`'s `session` field is the full session record (course/professor/bot IDs, phase, resources, activities, plus `proposal`/`content_mapping`/`chat_history`). See API_SPECIFICATION.md §6.0 for the exact field-by-field shape.

### 6.1 Start a session
POST /gradebook/sessions/start
```bash
curl -X POST "${HOST}:${PORT}/gradebook/sessions/start" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${API_KEY}" \
  -d '{"course_id": "101", "professor_id": "42", "bot_name": "my-new-bot", "moodle_resources": [], "course_activities": []}'
```

### 6.2 Chat to refine the proposal
POST /gradebook/sessions/{session_id}/chat
```bash
curl -X POST "${HOST}:${PORT}/gradebook/sessions/${SESSION_ID}/chat" \
  -H "Content-Type: application/json" -H "X-API-Key: ${API_KEY}" \
  -d '{"prompt": "Weight assignments at 40% and exams at 60%"}'
```

### 6.3 Get the current proposal
GET /gradebook/sessions/{session_id}/proposal

### 6.4 Accept the proposal
POST /gradebook/sessions/{session_id}/accept — generates the activity-to-category content mapping.

### 6.5 Finalize
POST /gradebook/sessions/{session_id}/finalize
```bash
curl -X POST "${HOST}:${PORT}/gradebook/sessions/${SESSION_ID}/finalize" \
  -H "Content-Type: application/json" -H "X-API-Key: ${API_KEY}" \
  -d '{"confirmed_mapping": [{"grade_item_id": 1, "category": "Assignments"}], "create_categories": true}'
```
`phase` reaches `COMPLETED` on success; otherwise `message` explains the first validation error.

### 6.6 Upload a syllabus document
POST /gradebook/sessions/{session_id}/upload — body is `{"filename": "...", "filetype": "pdf", "base64": "..."}`.

### 6.7 Reset
POST /gradebook/sessions/{session_id}/reset — body `{"keep_extraction": true}`.

### 6.8 Get status
GET /gradebook/sessions/{session_id}/status

### 6.9 Sync Moodle context
POST /gradebook/sessions/{session_id}/sync — body `{"course_activities": [...], "confirmed_mapping": null}`.

### 6.10 Delete a session
DELETE /gradebook/sessions/{session_id}

All of the above return 404 (`NOT_FOUND`) if the session doesn't exist.

---

## 7. Model Discovery

### 7.1 List available models
GET /models/list

```bash
curl "${HOST}:${PORT}/models/list" -H "X-API-Key: ${API_KEY}"
```

Master-key gated. Lists only models/providers actually configured on Ragflow's web UI for your tenant — use the returned `id`s for `llm_model_id`/`embedding_model_id`/`rerank_model_id` when creating a bot (§1.1). Models added directly in Criadex (Azure/Cohere/manual generic rows) are intentionally excluded.

```json
{
  "status": 200,
  "code": "SUCCESS",
  "models": [
    {"id": 12, "provider_type": "ragflow", "config": {"api_model": "gpt-4o", "llm_type": "chat"}}
  ]
}
```

---

## 8. API Documentation & Health

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
GET /health_check

Request:
```bash
curl "${HOST}:${PORT}/health_check"
```

Response (200 OK):
```text
Pong!
```

---

End of Bot API User Guide. For more details, see `API_SPECIFICATION.md` or the `/docs` UI.
