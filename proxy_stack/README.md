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
├── test.sh          33 checks, the definition of done
├── tls_101.md       trusting an https upstream, and the gateway in front of it
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

### An https upstream with a certificate the container does not trust

`./init.sh --autocert` does the whole thing in one command — finds the certificate the
upstream needs, keeps it in `proxy_stack_certs/`, wires it into `.env` and re-checks. Short
version below; **[tls_101.md](tls_101.md)** is the whole walkthrough — how verification
works, how to fetch the certificate from your PKI, and what to do when it still fails.

The image carries the public root store, so a certificate from a public CA just works.
Anything else fails every request with one message:

```
CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate
```

That wording covers **two different faults with different fixes**, and names neither:

| What is wrong | What fixes it |
| --- | --- |
| The certificate is signed by a CA the container does not have (company or self-signed) | Trust that CA |
| The server sends only its own certificate and not the intermediate above it | Trust the **intermediate** — the root alone will not bridge the gap |

The second is the common one behind a reverse proxy, and it is the reason "point it at your
root CA" can leave the error byte-for-byte unchanged. So don't guess — ask:

```bash
./init.sh --tls
```

It prints the chain the server actually sends and names the certificate that would complete
it, with the URL it is published at when the certificate carries one:

```
https://ai.example.com:443 sends 1 certificate(s):
  0. ai.example.com
     issued by Example Issuing CA

NOT trusted: unable to get local issuer certificate
the server sent only its own certificate, so nothing links it to a root.
the chain stops at 'ai.example.com', and its issuer 'Example Issuing CA' is not in this trust store.
get the certificate for 'Example Issuing CA' -- and any above it, up to the root --
  it is usually published at http://pki.example.com/issuing-ca.crt
put them all in one PEM file and set UPSTREAM_CA_FILE to it.
```

It runs **inside the container** whenever one is up, because the trust store that decides is
the container's, not your shell's. When the certificate is untrusted it then searches **this
host** for the one that is missing — an internal CA is usually installed on the machines
that need it and nowhere else, so the file that fixes the container is often already here:

```
looking for that certificate on this host:
  this host already trusts 'Example Issuing CA'. It is in:
    /usr/local/share/ca-certificates/example-issuing.crt
    /etc/ssl/certs/ca-certificates.crt

  verified: 2 of them complete the chain to https://ai.example.com:443:
    /usr/local/share/ca-certificates/example-issuing.crt
    /etc/ssl/certs/ca-certificates.crt
  the container does not have it, so hand it one of those -- in .env:
    UPSTREAM_CA_FILE=/usr/local/share/ca-certificates/example-issuing.crt
```

Each candidate is **tried**, not guessed at: a file holding the right name still fails if
the certificate above it is missing too, and the check says so instead of recommending it.
If this host does not have it either, you need it from whoever runs the CA — or exported
from a browser that trusts the site. Then:

```bash
# proxy_stack/.env
UPSTREAM_CA_FILE=/etc/ssl/certs/company-chain.pem   # a path on THIS host
```

Everything in that file is trusted **in addition to** the public roots, so a mixed estate
keeps working — concatenate as many certificates as the chain needs. `init.sh` refuses to
start if the path does not exist, and the proxy exits at startup, rather than 502ing on
every request, if the file is not readable PEM.

If the cause is the missing intermediate, the real fix is in that server's chain; trusting
it here is the workaround that does not disable verification.

```bash
UPSTREAM_TLS_VERIFY=false    # last resort: accept any certificate
```

This drops the only check that the host answering is the one you named — anything able to
intercept that connection sees the token and the conversation. `/health` reports which is
in force as `upstream_tls_verified`, `init.sh` warns on every start, `--tls` still reports
what verification *would* find, and `test.sh` asserts the posture matches the scheme so an
https upstream cannot be silently downgraded.

## Run it

```bash
./init.sh            # build, up, verify
./init.sh --no-build # skip the image build
./init.sh --tls      # why an https upstream is or is not trusted
./init.sh --autocert # fetch the CA an https upstream needs, keep it, wire it in
./init.sh --down     # stop
./test.sh            # 33 checks
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

### Behind an SSO gateway

Some deployments put a gateway in front of Open WebUI that authenticates by **session
cookie**, not by bearer token. A request carrying only a token is anonymous to it, and it
refuses before Open WebUI ever sees it:

```json
{"detail": "no session ID found"}
```

The 401 comes from the gateway, so no Open WebUI token will fix it. Give the proxy the
cookie instead — from a browser that is logged in, F12 → Application → Cookies:

```bash
# proxy_stack/.env
UPSTREAM_COOKIE=session=a1b2c3...
```

It is sent with every upstream request, and a caller's own `Cookie` header wins over it,
exactly as with the token. `/health` reports `pinned_cookie_configured`, and when the
upstream answers 401 or 403 with no cookie in play the proxy says so in its log.

> A session cookie carries the same weight as a password and **expires** — expect to refresh
> it. If the gateway can issue a long-lived API key or a bypass for service traffic, that is
> the better credential.

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
| Streaming deltas carry `reasoning_content`, sometimes with no content at all | Accumulated, not forwarded; flushed as one content chunk only if the stream produced neither content nor a tool call. Deltas emptied by stripping are dropped rather than sent as empty tokens |
| **A streamed tool call ends at `[DONE]` with `finish_reason` null on every chunk** | A finishing chunk is always emitted — `"tool_calls"` if any were sent, else `"stop"`. Without it VS Code reports *Response contained no choices* and discards the reply |
| The reply shape follows Open WebUI's mood, not the request: a stream for a caller that wanted JSON, or one JSON body for a caller that wanted a stream | Decided by the upstream `Content-Type`, not the request. A stream is folded into one `chat.completion` (tool-call fragments merged by index); a single body is wrapped as one chunk plus `[DONE]`. The caller always gets what it asked for |

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
  "contextWindow": 32768,
  "streaming": true
}
```

- **`id` is sent as the model name and must match `/v1/models` exactly**, colon included.
  `name` is only a display label. Swapping them yields `400 {"detail":"Model not found"}`.
- `contextWindow` must not exceed what the upstream model actually serves; claiming more
  does not raise the limit, it makes prompts silently truncate. Claiming *less* is the
  other failure: Copilot counts the prompt before sending and refuses with **"Message
  exceeds token limit"** once its agent-mode system prompt and tool definitions outgrow
  `contextWindow - maxOutputTokens`. 16384 is too small for that; `../llm_stack` serves
  32768, the most that stays fully on a 16 GB GPU with gpt-oss:20b.
- VS Code reads this file **at window startup**. After editing it, reload the window — and
  re-pick the model, since the remembered selection is stored by id.

## Troubleshooting

- **VS Code: "Sorry, your request failed ... Message exceeds token limit"** — raised by Copilot before any request reaches this proxy: the prompt is larger than `contextWindow - maxOutputTokens` in `chatLanguageModels.json`. Raise `contextWindow` to what the upstream really serves (`OLLAMA_CONTEXT_LENGTH` in `../llm_stack/compose.yaml`), reload the window, and re-pick the model.
- **Every request 502s** — the upstream is unreachable. `curl $LLM_HOSTNAME/api/config` from this host; `init.sh` checks this before starting.
- **502 `CERTIFICATE_VERIFY_FAILED` / `unable to get local issuer certificate`** — run `./init.sh --tls`: it names the certificate that is missing, which is not always the root. See [above](#an-https-upstream-with-a-certificate-the-container-does-not-trust).
- **Trusting the root CA did not help** — the server is probably not sending its intermediate; that intermediate has to be in `UPSTREAM_CA_FILE` too. `./init.sh --tls` says so explicitly.
- **An internal CA that is on the host but not in the container** — `./init.sh --tls` finds the file and prints the `UPSTREAM_CA_FILE=` line to paste. The container is deliberately not given the host's store wholesale; it gets the one file you name.
- **`{"detail": "no session ID found"}` or a 401 that mentions a session** — that is a gateway in front of Open WebUI, not Open WebUI. Set `UPSTREAM_COOKIE`; see [Behind an SSO gateway](#behind-an-sso-gateway).
- **401** — the token was revoked, or `.env` was edited without recreating the container. Editing `.env` alone does not reach a running container; re-run `./init.sh --no-build`.
- **`/v1/models` is empty** — the token is valid but Open WebUI exposes no models to that user. Check the model list in the UI first.
- **A tool is never called** — confirm `finish_reason` comes back as `tool_calls`; `test.sh` checks exactly this.
- **A browser on this port shows `{"detail": "no such route: /"}`** — correct. The interface is at `LLM_HOSTNAME`; this port is the API only.
- **Every token stopped working after an Open WebUI restart** — that instance is regenerating `WEBUI_SECRET_KEY` on each container recreation, which invalidates every JWT and browser session. Pin it in the Open WebUI deployment.
