# CriaBot API Specification

## Base URL
All endpoints are rooted at:
```
http://localhost:25575/
```
Typically served over HTTPS on port defined in `docker.env` or default `8000`.

Authentication: API key via HTTP header `X-API-Key` or query parameter `?api_key=`.

---

## 1. Bot Management
Endpoints to manage bot definitions and lifecycle.

### 1.1 Create a Bot
POST /bots/{bot_name}/manage/create
- Description: Register a new bot configuration. `llm_model_id`/`embedding_model_id`/`rerank_model_id` must reference models returned by `GET /models/list` (see §7).
- Path Parameters:
  - `bot_name` (string, required): The unique name for the bot.
- Request Body (application/json) — required fields plus every optional hyperparameter (defaults shown):
  ```json
  {
    "llm_model_id": 1,
    "embedding_model_id": 2,
    "rerank_model_id": 3,
    "use_knowledge_graph": true,
    "requires_documents": true,
    "parent_bot_names": ["optional-parent-name"],
    "parent_priorities": {"optional-parent-name": 1},

    "max_input_tokens": 2000,
    "max_reply_tokens": 1024,
    "temperature": 0.9,
    "top_p": 0,
    "top_k": 10,
    "min_k": 0.5,
    "top_n": 3,
    "min_n": 0.7,
    "llm_generate_related_prompts": true,
    "no_context_message": "Sorry, I'm not sure about that.",
    "no_context_use_message": false,
    "no_context_llm_guess": false,
    "system_message": "",
    "web_search_global_enabled": true,
    "web_search_enabled": false,
    "faq_fallback_enabled": true,
    "faq_fallback_threshold": 0.5
  }
  ```
  Only `llm_model_id`, `embedding_model_id`, and `rerank_model_id` are required — every other field falls back to the default shown. `web_search_enabled` opts this specific bot into web search (still gated by the global `web_search_global_enabled` toggle); `faq_fallback_threshold` is the minimum RAG confidence below which FAQ fallback kicks in.
- Response 200 OK:
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
- Description: Modify existing bot parameters. Accepts the same hyperparameter fields as create (all optional/partial) except the model IDs and `use_knowledge_graph`/`requires_documents`, which are immutable after creation; only `parent_bot_names`/`parent_priorities` plus the hyperparameters listed in §1.1 can be updated.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Request Body (application/json):
  ```json
  {
    "parent_bot_names": ["optional-parent-name-1", "optional-parent-name-2"],
    "temperature": 0.7,
    "web_search_enabled": true,
    "faq_fallback_enabled": false
  }
  ```
- Response 200 OK:
  ```json
  {
    "status": 200,
    "message": "Successfully updated the bot.",
    "timestamp": "<timestamp>",
    "code": "SUCCESS"
  }
  ```

### 1.5 List Parent Bots
GET /bots/{bot_name}/manage/parents
- Description: List direct parent bots for a given child.
- Path Parameters:
  - `bot_name` (string, required): The child bot name.
- Response 200 OK:
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
- Description: List direct child bots for a given parent.
- Path Parameters:
  - `bot_name` (string, required): The parent bot name.
- Response 200 OK:
  ```json
  {
    "status": 200,
    "message": "Successfully retrieved child bots.",
    "timestamp": "<timestamp>",
    "code": "SUCCESS",
    "children": ["child-bot-a", "child-bot-b"]
  }
  ```

### 1.3 Delete a Bot
DELETE /bots/{bot_name}/manage/delete
- Description: Remove a bot and all related data.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Response 200 OK:
  ```json
  {
    "status": 200,
    "message": "Successfully deleted the bot.",
    "timestamp": "<timestamp>",
    "code": "SUCCESS"
  }
  ```

### 1.4 About a Bot
GET /bots/{bot_name}/manage/about
- Description: Retrieve metadata and status for a bot.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Response 200 OK:
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

---

## 2. Bot Chats
Group of endpoints to manage chat sessions and messages.

### 2.1 Start a chat with a bot
POST /bots/chats/start
- Description: Initialize a new chat session.
- Request Body: None
- Response 200 OK:
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
- Description: Send a query to an existing chat session; returns bot reply. Same request/response shape as §2.3 Send.
- Path Parameters:
  - `chat_id` (string, required): The ID of the chat session.
- Request Body (application/json):
  ```json
  {
    "prompt": "What is the capital of France?",
    "bot_name": "my-new-bot-name",
    "extra_bots": ["another-bot-name"],
    "metadata_filter": {"must": [], "must_not": [], "should": []}
  }
  ```
  `extra_bots` is merged with the bot's own inherited parent bots automatically — you don't need to repeat parents here. `metadata_filter` is optional and passed through to Ragflow's retrieval filter.
- Response 200 OK: see §2.3.

### 2.3 Send a chat to a bot
POST /bots/chats/{chat_id}/send
- Description: Send a user message to an existing chat session; returns bot reply.
- Path Parameters:
  - `chat_id` (string, required): The ID of the chat session.
- Request Body (application/json): same shape as §2.2.
  ```json
  {
    "prompt": "Hello, bot!",
    "bot_name": "my-new-bot-name",
    "extra_bots": []
  }
  ```
- Response 200 OK:
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
      "token_usage": [
        {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15}
      ],
      "total_usage": {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15},
      "search_units": 1,
      "history": ["<ChatMessage, ...>"],
      "related_prompts": [
        {"label": "Follow-up", "prompt": "What else can you tell me?", "llm_generated": true}
      ],
      "context": null,
      "group_responses": {
        "WEB_SEARCH": {
          "nodes": [
            {"text": "[WEB RESULT #1] ...", "score": 1.0, "node": {"metadata": {"source_url": "https://...", "title": "...", "source_type": "web_search"}}}
          ]
        }
      },
      "verified_response": true,
      "faq_fallback_used": false,
      "faq_sources": []
    }
  }
  ```
  Notes:
  - **Reply shape changed** — the reply text is `reply.content.content`, not a top-level `message`. Token usage is `reply.total_usage` (a per-agent-call breakdown is in `reply.token_usage`), not `reply.completion_usage`.
  - **Direct Context Replies**: if a single highly relevant document node is found (under 300 chars) or 2-5 facts can be summarized directly, `reply.content.content` may be returned without LLM generation — `reply.verified_response` is `true` either way when this happens.
  - **`related_prompts`** are genuinely LLM-generated follow-up questions (via Ragflow chat completion), not a static/empty stub.
  - **`group_responses.WEB_SEARCH`** is present only when web search fired for this reply (see §2.3.1); each node's `metadata.source_url` links back to the source page.
  - **`faq_fallback_used`**/**`faq_sources`**: set when RAG confidence was below `faq_fallback_threshold` and the reply was instead answered from the bot's crawled FAQ index (see §6).

### 2.3.1 Stream a chat with reasoning + citations
POST /bots/chats/{chat_id}/stream
- Description: Same inputs as §2.3, but returns a Server-Sent Events (`text/event-stream`) stream of retrieval/synthesis status updates, response text chunks, and citations instead of a single JSON payload. Intended for UIs that want to show retrieval progress live.
- Path Parameters:
  - `chat_id` (string, required): The ID of the chat session.
- Request Body: same as §2.3.
- Response: `text/event-stream`, each line an SSE `data: {...}` event. Event `type`s:
  - `status` — `{"type": "status", "engine": "web_search", "state": "start|done", "message": "..."}`
  - `chunk` — `{"type": "chunk", "content": "<partial response text>"}`
  - `citations` — `{"type": "citations", "sources": [{"id": "...", "type": "file|web", "label": "...", "metadata": {...}}]}`
  - `done` — `{"type": "done", "elapsed_ms": 1234}`
  On error, returns a plain JSON error body (`{"code": ..., "status": ..., "message": ...}`) instead of a stream.

### 2.4 End a chat with a bot
DELETE /bots/chats/{chat_id}/end
- Description: End an existing chat session.
- Path Parameters:
  - `chat_id` (string, required): The ID of the chat session.
- Response 200 OK:
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
- Description: Retrieve recent chat messages.
- Path Parameters:
  - `chat_id` (string, required): The ID of the chat session.
- Response 200 OK:
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
- Description: Returns whether a chat session exists.
- Path Parameters:
  - `chat_id` (string, required): The ID of the chat session.
- Response 200 OK:
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
- Description: Upload a new document for processing.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Request Body (application/json):
  ```json
  {
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
  }
  ```
- Response 200 OK:
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
- Description: Upload a raw file (PDF, DOCX, HTML, etc.) directly — Criadex forwards the bytes to Ragflow for native parsing. This is the preferred path for real documents; §3.1's JSON `upload` is for pre-parsed node content (e.g. programmatically constructed text).
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Request Body (`multipart/form-data`):
  - `file` (file, required): the raw document.
  - `filename_override` (string, optional): overrides the stored document name (defaults to the uploaded filename).
  - `strategy` (string, optional): document parsing strategy override, e.g. `GENERIC`, `ALSYLLABUS`, `ALSYLLABUSFR`, `PARAGRAPH`. HTML files are routed through `PARAGRAPH` automatically regardless of the requested strategy, since Ragflow has no native HTML support.
- Response 200 OK:
  ```json
  {
    "status": 200,
    "message": "File queued for native Ragflow parsing.",
    "code": "SUCCESS",
    "document_name": "my-file.pdf"
  }
  ```
- Response 409 (duplicate name): `code: "DUPLICATE"`.

### 3.2 Update a document on the bot
PATCH /bots/{bot_name}/documents/update
- Description: Update document metadata or content.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Request Body (application/json):
  ```json
  {
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
  }
  ```
- Response 200 OK:
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
- Description: Remove a stored document and associated data.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Query Parameters:
  - `document_name` (string, required): The name of the document to delete.
- Response 200 OK:
  ```json
  {
    "status": 200,
    "message": "Successfully deleted the documents from the index.",
    "code": "SUCCESS"
  }
  ```

### 3.4 List documents stored in the bot
GET /bots/{bot_name}/documents/list
- Description: Paginated list of uploaded documents.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Response 200 OK:
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
- Description: Upload a new question for processing.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Request Body (application/json):
  ```json
  {
    "file_name": "my-test-question.json",
    "file_contents": {
      "questions": [
        "What is the capital of France?"
      ],
      "answer": "Paris"
    },
    "file_metadata": {}
  }
  ```
- Response 200 OK:
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
- Description: Update question metadata or content.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Request Body (application/json):
  ```json
  {
    "file_name": "my-test-question.json",
    "file_contents": {
      "questions": [
        "What is the capital of France?",
        "What is the largest city in France?"
      ],
      "answer": "Paris"
    },
    "file_metadata": {}
  }
  ```
- Response 200 OK:
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
- Description: Remove a stored question and associated data.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Query Parameters:
  - `document_name` (string, required): The name of the question to delete.
- Response 200 OK:
  ```json
  {
    "status": 200,
    "message": "Successfully deleted the documents from the index.",
    "code": "SUCCESS"
  }
  ```

### 4.4 List questions stored in the bot
GET /bots/{bot_name}/questions/list
- Description: Paginated list of uploaded questions.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Response 200 OK:
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
Crawl a configured website into a bot's knowledge base and serve as a low-confidence-retrieval fallback.

### 5.1 Update FAQ sync config
PATCH /faq/config
- Description: Update the source URL and crawl constraints used by `/faq/sync`. All fields optional/partial.
- Request Body (application/json):
  ```json
  {
    "source_url": "https://example.edu/faq",
    "group_name": "my-bot-faq",
    "max_pages": 50,
    "timeout_seconds": 30,
    "enabled": true,
    "interval_seconds": 21600,
    "stale_after_seconds": 43200,
    "failure_alert_threshold": 3
  }
  ```
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "message": "...", "config": {"...": "..."}}`

### 5.2 Trigger an FAQ sync
POST /faq/sync
- Description: Crawls the configured (or overridden) FAQ website, uploads pages to Criadex, and optionally triggers a graph build.
- Request Body (application/json):
  ```json
  {
    "source_url": "https://example.edu/faq",
    "group_name": "my-bot-faq",
    "max_pages": 50,
    "trigger_graph_build": true
  }
  ```
  All fields optional — omitted values fall back to the current stored config.
- Response 200 OK:
  ```json
  {
    "status": 200,
    "code": "SUCCESS",
    "message": "FAQ website sync completed successfully.",
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
- Description: Returns the latest FAQ sync lifecycle state, current config, and recent run history.
- Response 200 OK:
  ```json
  {
    "status": 200,
    "code": "SUCCESS",
    "state": "READY",
    "last_run_at": 1751500000,
    "last_success_at": 1751500000,
    "pages_crawled": 12,
    "indexed_files": 12,
    "duplicate_files": 0,
    "error": null,
    "stale": false,
    "consecutive_failures": 0,
    "alert_state": "IDLE",
    "scheduler_running": true,
    "recent_runs": [
      {"run_at": 1751500000, "completed_at": 1751500042, "state": "READY", "pages_crawled": 12, "indexed_files": 12, "duplicate_files": 0, "error": null}
    ],
    "config": {"source_url": "...", "group_name": "...", "max_pages": 50, "timeout_seconds": 30, "enabled": true, "interval_seconds": 21600, "stale_after_seconds": 43200, "failure_alert_threshold": 3}
  }
  ```

---

## 6. Gradebook
Multi-turn, state-machine-driven session that turns Moodle course activities into a proposed gradebook category structure. All endpoints are keyed by `session_id` (obtained from §6.1).

### 6.0 Shared response shapes
The `proposal`, `content_mapping`, and `session` fields below are not opaque — they're `dict`-typed in the response models but always carry these shapes (source: `criabot/gradebook/schemas.py`, `content_mapper.py`, `session.py`).

**`Proposal`** (`GradebookProposal`):
```json
{
  "categories": [
    {
      "name": "Assignments", "weight": 40.0,
      "items": ["Assignment 1"], "item_weights": {"Assignment 1": 100.0},
      "subcategories": [
        {"name": "Labs", "weight": 20.0, "items": [], "item_weights": {}, "aggregation_method": null}
      ],
      "drop_lowest": 0, "keep_highest": 0, "aggregate_only_graded": true, "aggregate_outcomes": false, "extra_credit": false,
      "grade_min": null, "grade_max": 100.0, "grade_pass": null,
      "hidden": false, "hidden_until": null, "locked": false, "lock_time": null,
      "display_type": 0, "decimals": -1,
      "calculation_formula": null, "formula_override": false, "formula_item_refs": [], "formula_unresolved_refs": []
    }
  ],
  "not_graded_items": [],
  "notes": [],
  "aggregation_method": 13
}
```

**`ContentMapping`** (built by `ContentMapper.build_mapping`; `validation` is only populated after §6.4 Accept runs):
```json
{
  "graded_activities": [
    {
      "moodle_cmid": 42, "grade_item_id": 1, "module_type": "assign", "itemtype": "mod",
      "activity_name": "Assignment 1", "activity_key": "assign:42", "item_source": "assign",
      "suggested_category": "Assignments", "confirmed_category": "Assignments",
      "suggested_subcategory": "", "confirmed_subcategory": "", "subcategory": "",
      "finalized": false, "confidence": 0.9, "reasoning": "...", "mapping_method": "deterministic"
    }
  ],
  "unmatched_activities": [],
  "uncategorized_activities": [],
  "resource_suggestions": [],
  "validation_errors": [
    {"activity_key": "assign:42", "moodle_cmid": 42, "activity_name": "Assignment 1", "missing_category": "Old Category", "reason": "Category was removed, renamed, or not present in the current proposal."}
  ],
  "llm_mapper_chat_id": null,
  "validation": {"errors": [], "warnings": [], "can_proceed": true}
}
```
`mapping_method` is one of `deterministic` / `llm` / `manual`. `validation_errors` rows carry either `missing_category` or (`missing_subcategory` + `parent_category`), never both.

**`Session`** (§6.8 only — `GradebookSessionRecord` plus `chat_history`):
```json
{
  "session_id": "...", "course_id": "...", "professor_id": "...", "bot_name": "...", "phase": "...",
  "moodle_resources": [], "course_activities": [],
  "extraction": {}, "proposal": null, "content_mapping": null,
  "last_touched_at": 1234567890, "uploaded_document_ids": [],
  "chat_history": []
}
```

### 6.1 Start a gradebook session
POST /gradebook/sessions/start
- Request Body: `{"course_id": "...", "professor_id": "...", "bot_name": "...", "moodle_resources": [], "course_activities": [], "baseline_snapshot": null, "import_mode": null}`
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session_id": "...", "phase": "...", "initial_message": "...", "baseline_available": false, "baseline_snapshot": null, "import_mode": null, "context_source": "..."}`

### 6.2 Send a gradebook chat prompt
POST /gradebook/sessions/{session_id}/chat
- Request Body: `{"prompt": "Use weighted categories: Assignments 40%, Exams 60%"}`
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session_id": "...", "phase": "...", "reply": "...", "proposal": Proposal, "proposal_changed": true, "content_mapping": ContentMapping, "chat_history": []}`

### 6.3 Get the current proposal
GET /gradebook/sessions/{session_id}/proposal
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session_id": "...", "phase": "...", "proposal": Proposal}`

### 6.4 Accept the proposal
POST /gradebook/sessions/{session_id}/accept
- Description: Marks the proposal accepted and generates the activity-to-category content mapping.
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session_id": "...", "phase": "...", "proposal": Proposal, "content_mapping": ContentMapping}`

### 6.5 Finalize the gradebook
POST /gradebook/sessions/{session_id}/finalize
- Request Body:
  ```json
  {
    "confirmed_mapping": [
      {"grade_item_id": 1, "moodle_cmid": 42, "activity_name": "Assignment 1", "itemtype": "mod", "item_source": "assign", "category": "Assignments", "subcategory": null}
    ],
    "create_categories": true,
    "reorganize_resources": false
  }
  ```
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session_id": "...", "phase": "COMPLETED", "summary": {"categories_to_create": 3, "activities_mapped": 12, "graded_count": 10, "not_graded_count": 2, "total_weight": 100.0, "create_categories": true, "reorganize_resources": false, "finalized_at": "2026-07-02T12:00:00+00:00"}, "proposal": Proposal, "content_mapping": ContentMapping}`. If validation fails, `phase` stays non-`COMPLETED` and `message` describes the first blocking error from `content_mapping.validation.errors`.

### 6.6 Upload a syllabus document
POST /gradebook/sessions/{session_id}/upload
- Request Body: `{"filename": "syllabus.pdf", "filetype": "pdf", "base64": "<base64-encoded file content>"}`
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session_id": "...", "phase": "...", "reply": "...", "proposal": Proposal}`

### 6.7 Reset a session
POST /gradebook/sessions/{session_id}/reset
- Request Body: `{"keep_extraction": true}`
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session_id": "...", "phase": "...", "proposal": Proposal, "content_mapping": ContentMapping}`

### 6.8 Get session status
GET /gradebook/sessions/{session_id}/status
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session": Session}`

### 6.9 Sync Moodle context into a session
POST /gradebook/sessions/{session_id}/sync
- Request Body: `{"course_activities": [], "confirmed_mapping": null}`
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "session_id": "...", "phase": "...", "proposal": Proposal, "content_mapping": ContentMapping}`

### 6.10 Delete a session
DELETE /gradebook/sessions/{session_id}
- Response 200 OK: `{"status": 200, "code": "SUCCESS", "message": "Gradebook session deleted.", "session_id": "...", "data": {"success": true, "existed": true, "grade_setup_cleaned": true, "grade_setup_message": ""}}`

All gradebook endpoints return 404 (`NOT_FOUND`) if `session_id` doesn't exist.

---

## 7. Model Discovery

### 7.1 List available models
GET /models/list
- Description: Lists models/providers configured on Ragflow's web interface for the configured tenant. This is the only set of models a caller should offer for bot/provider selection — models added directly in Criadex (Azure/Cohere/manual generic rows) are Ragflow-unbacked and are excluded. Master-key gated.
- Response 200 OK:
  ```json
  {
    "status": 200,
    "code": "SUCCESS",
    "message": "Successfully listed Ragflow models.",
    "models": [
      {"id": 12, "provider_type": "ragflow", "config": {"api_model": "gpt-4o", "llm_type": "chat"}}
    ]
  }
  ```

---

## 8. API Documentation & Health

### Swagger UI
GET /docs
- Serves interactive API docs.

### OpenAPI JSON
GET /openapi.json
- Returns raw OpenAPI specification.

### Health Check
GET /health_check
- Returns service health status.
- Response 200 OK:
  ```
  Pong!
  ```