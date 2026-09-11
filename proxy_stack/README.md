# proxy_stack — an OpenAI-compatible endpoint for any Open WebUI

A ~300-line standard-library Python service. Point it at an Open WebUI instance and it
exposes that instance's models as an OpenAI-compatible API, so clients like VS Code can
use it.

Standalone: one container, no dependencies, and one thing to configure.

```
proxy_stack/
├── init.sh          start here
├── proxy.py         the whole service, stdlib only
├── Dockerfile       python:3.13-alpine pinned by digest
├── compose.yaml     one service, host networking
├── test.sh          26 checks, the definition of done
├── .env             LLM_HOSTNAME + WEBUI_TOKEN (git-ignored)
└── .env.example
```

## Configure

One required setting:

```bash
# proxy_stack/.env
LLM_HOSTNAME=http://127.0.0.1:3000
```

`LLM_HOSTNAME` is the Open WebUI this proxy sits in front of. It accepts a bare host, a
host:port, or a full URL with a base path — all of these work:

| Value | Resolves to |
| --- | --- |
| `webui.example.com` | `http://webui.example.com:80` |
| `webui.example.com:3000` | `http://webui.example.com:3000` |
| `http://127.0.0.1:3000` | `http://127.0.0.1:3000` |
| `https://ai.example.com/openwebui/` | `https://ai.example.com:443/openwebui` |

A value that is not a usable host, or a scheme other than http/https, is refused at startup
with a message saying which — not a DNS error thirty seconds later.

## Run it

```bash
./init.sh            # build, up, verify
./init.sh --no-build # skip the image build
./init.sh --down     # stop
./test.sh            # 26 checks
```

`init.sh` resolves `LLM_HOSTNAME` the same way `proxy.py` does, checks the upstream is
reachable, checks the port is free, waits for health, then confirms the proxy serves the
API and lists the models it can see. Safe to re-run, and callable from any directory.

### Why host networking

The container runs with `network_mode: host`. An Open WebUI published on the host's
loopback — `127.0.0.1:3000`, the common case — is unreachable from a bridged container:
`host.docker.internal` resolves to the bridge gateway and times out against a
loopback-bound port. Measured on this host, both ways.

Host networking also means `ports:` does not apply; the proxy binds `LISTEN_HOST` itself,
which defaults to `127.0.0.1`. Set it to `0.0.0.0` to expose the endpoint on the network —
which also exposes whatever the pinned token can reach.

## What it serves

```
client ──▶ 127.0.0.1:8111 ──▶ <LLM_HOSTNAME> (Open WebUI) ──▶ its own models
```

| Route | Upstream |
| --- | --- |
| `GET /v1/models` | `GET <base>/api/models` |
| `POST /v1/chat/completions` | `POST <base>/api/chat/completions` |
| `GET /health` | answered locally, no credential |

**Everything else is 404.** This serves no interface — Open WebUI has its own, and sharing
one port between them makes it impossible to tell which service answered. `test.sh` asserts
`/` returns 404 rather than HTML.

## Authentication

The proxy has no credential of its own. A request travels upstream on **the caller's own
Open WebUI token**, so Open WebUI decides what that user may do. Both shapes it issues work:

| Credential | Where it comes from |
| --- | --- |
| API key (`sk-...`) | Settings → Account → API Keys → Create |
| Session JWT | Your browser: F12 → Console → `localStorage.token` |

Set `WEBUI_TOKEN` in `.env` and it is used when a caller sends none — so a client that
cannot hold a credential (VS Code with a placeholder key) still works. A caller's own token
always wins.

> **A pinned token means an unauthenticated caller is served.** Anything that can reach the
> port can use the model. It binds loopback by default, but if that is not the trade you
> want, set `REQUIRE_CLIENT_TOKEN=true` — the pinned token then acts purely as the upstream
> credential. `/health` reports which mode is active, and so does `init.sh`.

> Open WebUI stores **one API key per user**: creating a new one in the UI retires the
> previous one, so a key pinned in `.env` stops working the moment you press Create again.
> `test.sh` deliberately never mints a key for this reason.

## What it translates, and why

Open WebUI's responses deviate from the OpenAI schema in ways that matter to a client. Each
was measured, not assumed.

| Deviation | Fix |
| --- | --- |
| `/api/models` entries carry `tags`, `actions`, `filters`, `connection_type` | Reduced to the OpenAI list schema — `id`, `object`, `created`, `owned_by` |
| Replies carry `reasoning_content`, which is not an OpenAI field | Dropped; promoted to `content` first if the model left `content` empty, so a reply is never blank |
| **`finish_reason` is `"stop"` on a response that contains `tool_calls`** | Corrected to `"tool_calls"`. An agent reads `"stop"` as *the model is done* and so never runs the tool |
| Streaming deltas carry `reasoning_content`, sometimes with no content at all | Accumulated, not forwarded; flushed as one content chunk only if the stream produced no content. Deltas emptied by stripping are dropped rather than sent as empty tokens |

Everything else — `tools`, `tool_calls`, `usage`, model ids, the request body itself — is
forwarded unchanged. The body is parsed only to learn whether the caller asked for a
stream, so nothing the client sent is silently rewritten.

## Using it from VS Code

VS Code 1.137 bundles `copilot-chat`. Model picker → *Manage Models* → **Custom Endpoint**
(not "Ollama" or "OpenAI Compatible" — both are deprecated, and the latter is hidden on
stable builds). Paste a token as the API key, then add the model.

The config lands in `~/.config/Code/User/chatLanguageModels.json`:

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

- **`id` is sent as the model name and must match `/v1/models` exactly**, colon included.
  `name` is only a display label. Swapping them yields `400 {"detail":"Model not found"}`.
- `contextWindow` must not exceed what the upstream model actually serves; claiming more
  does not raise the limit, it makes prompts silently truncate.
- VS Code reads this file **at window startup**. After editing it, reload the window — and
  re-pick the model, since the remembered selection is stored by id.

## Troubleshooting

- **Every request 502s** — the upstream is unreachable. `curl $LLM_HOSTNAME/api/config` from this host; `init.sh` checks this before starting.
- **401** — the token was revoked, or `.env` was edited without recreating the container. Editing `.env` alone does not reach a running container; re-run `./init.sh --no-build`.
- **`/v1/models` is empty** — the token is valid but Open WebUI exposes no models to that user. Check the model list in the UI first.
- **A tool is never called** — confirm `finish_reason` comes back as `tool_calls`; `test.sh` checks exactly this.
- **A browser on this port shows `{"detail": "no such route: /"}`** — correct. The interface is at `LLM_HOSTNAME`; this port is the API only.
- **Every token stopped working after an Open WebUI restart** — that instance is regenerating `WEBUI_SECRET_KEY` on each container recreation, which invalidates every JWT and browser session. Pin it in the Open WebUI deployment.
