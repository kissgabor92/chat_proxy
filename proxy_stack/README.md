# proxy_stack — an isolated OpenAI endpoint for VS Code

A ~300-line standard-library Python service that exposes the local model to VS Code as an
OpenAI-compatible API, by talking to Open WebUI.

Verified end to end on this host on 2026-09-11.

```
proxy_stack/
├── init.sh          start here
├── proxy.py         the whole service, stdlib only, no dependencies
├── Dockerfile       python:3.13-alpine pinned by digest
├── compose.yaml     joins the existing chat-proxy network
├── test.sh          33 checks, the definition of done
├── .env             WEBUI_TOKEN (git-ignored)
└── .env.example
```

## Two ports, two services

**This port serves no interface.** The Open WebUI UI is on its own port, so which port you
are talking to always tells you which service answered.

```
browser ──▶ 127.0.0.1:3000   Open WebUI (the interface)
                    │
VS Code ──▶ 127.0.0.1:8111   this service ──▶ webui:8080 ──▶ ollama:11434
                    (OpenAI API only)                        (behind Open WebUI)
```

| Port | Service | Serves |
| --- | --- | --- |
| `3000` | `chat-proxy-webui` | The Open WebUI interface. Open it in a browser |
| `8111` | `chat-proxy-vscode` | `GET /v1/models`, `POST /v1/chat/completions`, `GET /health`. **Everything else is 404** |

Ollama publishes nothing and is never addressed by this service — every model request goes
through Open WebUI, so its model access rules always apply. `test.sh` asserts `proxy.py`
contains no route to Ollama, and that `/` on 8111 returns 404 rather than the UI.

| This service | Open WebUI endpoint it calls |
| --- | --- |
| `GET /v1/models` | `GET /api/models` |
| `POST /v1/chat/completions` | `POST /api/chat/completions` |

## Run it

Both scripts resolve their own directory, so run them by path from anywhere — these work
from the repository root:

```bash
./llm_stack/init.sh      # Ollama + Open WebUI  (UI on :3000)
./proxy_stack/init.sh    # this service          (API on :8111)
./proxy_stack/test.sh    # 33 checks
```

Order matters: `proxy_stack` joins a network that `llm_stack` creates, and refuses to start
with a clear message if it is missing.

| | |
| --- | --- |
| `./proxy_stack/init.sh` | Build, up, verify |
| `./proxy_stack/init.sh --no-build` | Skip the image build |
| `./proxy_stack/init.sh --down` | Stop |

## Authentication

The bridge has no credential of its own. A request travels upstream on **the caller's own
Open WebUI token**, so Open WebUI decides what that user may do. Both credential shapes it
issues work, and the bridge never has to tell them apart:

| Credential | Where it comes from |
| --- | --- |
| API key (`sk-...`) | The UI: **Settings → Account → API Keys → Create** |
| Session JWT | The `token` field of `GET /api/v1/auths/`, which returns `token_type: "Bearer"` |

### The pinned token

VS Code has no browser session, so put a token in `.env`:

```bash
# proxy_stack/.env
WEBUI_TOKEN=sk-...
```

That token is used upstream whenever a caller supplies none — the "provide it beforehand"
case, which lets VS Code be configured with any placeholder key. A caller's own token
always wins.

> **A pinned token means an unauthenticated caller is served.** Anything that can reach
> port 8111 can then use the model. The port is loopback-only, but if that is not the trade
> you want, set `REQUIRE_CLIENT_TOKEN=true` — the pinned token then acts purely as the
> upstream credential and a caller must still present its own. `/health` reports which mode
> is active, and so does `init.sh`.

Get one from the UI on <http://127.0.0.1:3000> — **Settings → Account → API Keys → Create**.

> Open WebUI stores **one API key per user**. Creating a new one in the UI retires the
> previous one, so a key pinned in `.env` stops working the moment you press Create again.
> `test.sh` deliberately never mints a key for this reason.

> **Prefer an `sk-` key over a session JWT.** A JWT is signed with Open WebUI's
> `WEBUI_SECRET_KEY`; an `sk-` key lives in the database and survives a key rotation.

## What it translates, and why

Open WebUI's responses deviate from the OpenAI schema in ways that matter to a client.
Each of these was measured against this stack, not assumed.

| Deviation | Fix |
| --- | --- |
| `/api/models` entries carry `tags`, `actions`, `filters`, `connection_type` | Reduced to the OpenAI list schema — `id`, `object`, `created`, `owned_by` |
| Replies carry `reasoning_content`, which is not an OpenAI field | Dropped; promoted to `content` first if the model left `content` empty, so a reply is never blank |
| **`finish_reason` is `"stop"` on a response that contains `tool_calls`** | Corrected to `"tool_calls"`. An agent reads `"stop"` as *the model is done* and so never runs the tool |
| Streaming deltas carry `reasoning_content`, sometimes with no content at all | Accumulated, not forwarded; flushed as one content chunk only if the stream produced no content. Deltas emptied by stripping are dropped rather than sent as empty tokens |

Everything else — `tools`, `tool_calls`, `usage`, model ids, the request body itself — is
forwarded byte for byte. The body is parsed only to learn whether the caller asked for a
stream, so nothing the client sent is silently rewritten.

## Using it from VS Code

VS Code 1.137.0 bundles `copilot-chat` 0.65.0. Use the **Custom Endpoint** provider via the
model picker → *Manage Models*. Paste the token from `.env` as the API key, then:

```json
{
  "id": "gpt-oss:20b",
  "name": "gpt-oss 20B (local)",
  "url": "http://127.0.0.1:8111/v1/chat/completions",
  "toolCalling": true,
  "vision": false,
  "maxOutputTokens": 4096,
  "contextWindow": 16384,
  "streaming": true
}
```

`id` must match what `/v1/models` returns exactly, colon included — VS Code sends back
whatever id it was given. `contextWindow` must not exceed `OLLAMA_CONTEXT_LENGTH` in
`llm_stack/compose.yaml` (currently 16384); claiming more does not raise the limit, it just
makes VS Code send prompts that are silently truncated.

## Tests

`./test.sh` is the definition of done — 33 checks covering authentication, the OpenAI
translation, tool-call correction, streaming, the isolation of this port from the UI, and
the host port map. There was no test command anywhere in this repository
before this directory; it brings its own.

```
passed 33, failed 0
```

## Troubleshooting

- **`webui` does not resolve** — this service joined a different network. `docker network inspect chat-proxy --format '{{range .Containers}}{{.Name}} {{end}}'` must list `chat-proxy-vscode` alongside `chat-proxy-webui`. The network is declared `external: true` precisely to prevent this.
- **401 from VS Code** — the token was revoked in the UI, or `.env` was edited without recreating the container. Editing `.env` alone does not reach a running container; re-run `./init.sh --no-build`.
- **`/v1/models` is empty** — the token is valid but Open WebUI is exposing no models to that user. Check the model list in the UI first.
- **A tool is never called** — confirm `finish_reason` comes back as `tool_calls`; `./test.sh` has a check for exactly this.
- **A browser on 8111 shows `{"detail": "no such route: /"}`** — that is correct. The interface is on <http://127.0.0.1:3000>; 8111 is the API only.
- **Tokens all stopped working after a restart** — `WEBUI_SECRET_KEY` must be pinned in `llm_stack/.env`, or Open WebUI regenerates it on every container recreation and invalidates every JWT and browser session.
