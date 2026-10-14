# AI Tutor Chat

A small Korean-language web chat for OpenAI models. It provides persistent chat sessions, four fixed system prompts, file attachments, and exactly two tools: a session-scoped Python interpreter and Google search through Serper.

## Run with Docker Compose

Requirements: Docker with Compose. An API key is required by OpenAI, but may be omitted when the configured compatible server does not require one. A Serper key is optional; without it, only the Google search tool returns a configuration error.

```bash
cp .env.example .env
# Edit .env and set OPENAI_API_KEY.
docker compose up --build
```

Open <http://localhost:8000>.

The Compose stack includes a privileged Docker-in-Docker service. The app asks that isolated daemon to run one offline, read-only Python container per active chat session. No API keys or host paths are mounted into Python containers.

## Configuration

- `OPENAI_API_KEY` — required by OpenAI; optional for compatible servers that accept unauthenticated requests.
- `OPENAI_BASE_URL` — OpenAI-compatible API root; defaults to `https://api.openai.com/v1`.
- `SERPER_API_KEY` — optional, required for `google_search`.
- `OPENAI_MODELS` — comma-separated model IDs shown in the UI; defaults to `gpt-5-mini`.

Chat metadata and messages are stored in the `app-data` Docker volume. UTF-8 text attachments are stored locally and included inline in the user message using `<attachment>` blocks. Each file is limited to 1 MB and each request to 4 MB in total.

Changing the system prompt keeps old messages visible but starts a new API context at that point. Changing only the model keeps the current context.

The compatible endpoint only needs to implement the Responses API (`POST /responses`); the Files API is not used. Set `OPENAI_BASE_URL` to the API root including its version prefix, for example `http://host.docker.internal:8080/v1`.
