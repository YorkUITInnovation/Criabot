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
- Description: Register a new bot configuration.
- Path Parameters:
  - `bot_name` (string, required): The unique name for the bot.
- Request Body (application/json):
  ```json
  {
    "llm_model_id": 1,
    "embedding_model_id": 2,
    "rerank_model_id": 3
  }
  ```
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
- Description: Modify existing bot parameters.
- Path Parameters:
  - `bot_name` (string, required): The name of the bot.
- Request Body (application/json):
  ```json
  {
    "llm_model_id": 4,
    "embedding_model_id": 5,
    "rerank_model_id": 6
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
- Description: Send a query to an existing chat session; returns bot reply.
- Path Parameters:
  - `chat_id` (string, required): The ID of the chat session.
- Request Body (application/json):
  ```json
  {
    "prompt": "What is the capital of France?",
    "bot_name": "my-new-bot-name",
    "extra_bots": []
  }
  ```
- Response 200 OK:
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
- Description: Send a user message to an existing chat session; returns bot reply.
- Path Parameters:
  - `chat_id` (string, required): The ID of the chat session.
- Request Body (application/json):
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

## 5. API Documentation & Health

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