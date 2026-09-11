"""
An isolated OpenAI-compatible endpoint for VS Code, in front of Open WebUI.

This serves one thing and nothing else:

  GET  /v1/models            -> Open WebUI GET  /api/models
  POST /v1/chat/completions  -> Open WebUI POST /api/chat/completions
  GET  /health               -> liveness, no credential

Any other path is 404. The browser UI is Open WebUI's own port, not this one --
mixing the two behind a single port made it impossible to tell which service was
answering.

Ollama sits behind Open WebUI and is never addressed here, so Open WebUI's model
permissions always apply and this process cannot become a way around them.

Standard library only. It exists to translate, not to decide:

1. Open WebUI answers on /api/... paths, not the /v1/... an OpenAI-compatible
   client expects, and its model list carries fields no such client reads.

2. Its replies deviate from the OpenAI schema in two ways that matter to VS
   Code -- a non-standard `reasoning_content` field, and `finish_reason` left as
   "stop" on a response that actually carries tool_calls, which an agent reads
   as "the model is done" and so never runs the tool.
"""

import http.client
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8111"))
WEBUI_HOST = os.environ.get("WEBUI_HOST", "webui")
WEBUI_PORT = int(os.environ.get("WEBUI_PORT", "8080"))
# Inference can take minutes on a cold model load; a short timeout here shows up
# as a truncated reply in the editor rather than as an error.
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "600"))

# The Open WebUI token requests travel upstream with when the caller supplies
# none -- the "provide it beforehand" case, so VS Code can be configured with
# any placeholder key. A caller's own token always wins, so per-user model
# permissions still apply.
WEBUI_TOKEN = os.environ.get("WEBUI_TOKEN", "").strip()

# With a pinned token, a request carrying no credential is answered using it.
# Convenient, but it means anything that can reach this port can use the model.
# Set true to demand a bearer token anyway.
REQUIRE_CLIENT_TOKEN = os.environ.get("REQUIRE_CLIENT_TOKEN", "false").lower() == "true"


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def normalise(choice):
    """Bring one Open WebUI choice back to the OpenAI schema.

    `reasoning_content` is not an OpenAI field. It is dropped, but promoted to
    `content` first if the model spent its whole budget thinking and left
    `content` empty -- otherwise the editor renders a blank reply.

    `finish_reason` comes back as "stop" even when the message carries
    tool_calls. An agent reads "stop" as "the model is done" and never runs the
    tool, so such a response is corrected to "tool_calls".
    """
    if not isinstance(choice, dict):
        return choice
    message = choice.get("message") or {}
    reasoning = message.pop("reasoning_content", None)
    if not message.get("content") and reasoning and not message.get("tool_calls"):
        message["content"] = reasoning
    if message.get("tool_calls") and choice.get("finish_reason") == "stop":
        choice["finish_reason"] = "tool_calls"
    return choice


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "webui-bridge/1.0"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    # ---------- helpers ----------

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_detail(self, status, detail):
        """FastAPI's error shape, so clients written against Open WebUI parse it."""
        self.send_json(status, {"detail": detail})

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def bearer(self):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return self.headers.get("api-key", "").strip()

    def webui(self, method, path, body=None):
        token = self.bearer() or WEBUI_TOKEN
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        conn = http.client.HTTPConnection(WEBUI_HOST, WEBUI_PORT, timeout=UPSTREAM_TIMEOUT)
        conn.request(method, path, body=body, headers=headers)
        return conn.getresponse()

    # ---------- routing ----------

    def route(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if path == "/health":
            return self.send_json(200, {
                "status": "ok",
                "webui": "%s:%d" % (WEBUI_HOST, WEBUI_PORT),
                "pinned_token_configured": bool(WEBUI_TOKEN),
                "client_token_required": REQUIRE_CLIENT_TOKEN,
            })

        if path not in ("/v1/models", "/v1/chat/completions"):
            # Deliberately not a pass-through. This port is the model endpoint
            # only; the Open WebUI interface lives on its own port.
            return self.send_json(404, {
                "detail": "no such route: %s" % self.path,
                "routes": ["GET /v1/models", "POST /v1/chat/completions", "GET /health"],
            })

        if REQUIRE_CLIENT_TOKEN and not self.bearer():
            return self.send_detail(401, "Not authenticated")
        if not (self.bearer() or WEBUI_TOKEN):
            return self.send_detail(401, "Not authenticated")

        if path == "/v1/models":
            if self.command != "GET":
                return self.send_detail(405, "use GET on /v1/models")
            return self.handle_models()

        if self.command != "POST":
            return self.send_detail(405, "use POST on /v1/chat/completions")
        return self.handle_chat()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = route

    # ---------- the two real routes ----------

    def handle_models(self):
        """Open WebUI's /api/models, reduced to the OpenAI list schema.

        Its entries carry extra keys (tags, actions, filters, connection_type)
        that no OpenAI client reads; passing them through invites a client to
        choke on a shape it did not expect.
        """
        try:
            resp = self.webui("GET", "/api/models")
            raw = resp.read()
        except OSError as exc:
            return self.send_detail(502, "Open WebUI unreachable: %s" % exc)
        if resp.status != 200:
            return self.send_detail(resp.status, raw.decode(errors="replace")[:500])

        try:
            items = json.loads(raw).get("data", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            return self.send_detail(502, "unreadable model list: %s" % exc)

        return self.send_json(200, {
            "object": "list",
            "data": [{
                "id": m.get("id"),
                "object": "model",
                "created": m.get("created", 0),
                "owned_by": m.get("owned_by", "open-webui"),
            } for m in items if m.get("id")],
        })

    def handle_chat(self):
        raw = self.read_body() or b"{}"
        try:
            # Parsed only to learn whether the caller asked for a stream; the
            # original bytes are forwarded, so nothing the client sent is
            # silently rewritten on the way through.
            wants_stream = bool(json.loads(raw).get("stream"))
        except (json.JSONDecodeError, AttributeError) as exc:
            return self.send_detail(400, "malformed JSON body: %s" % exc)

        try:
            resp = self.webui("POST", "/api/chat/completions", body=raw)
        except OSError as exc:
            return self.send_detail(502, "Open WebUI unreachable: %s" % exc)

        if resp.status >= 400:
            return self.send_detail(resp.status, resp.read().decode(errors="replace")[:500])

        if wants_stream:
            return self.relay_stream(resp)

        try:
            data = json.loads(resp.read())
        except json.JSONDecodeError as exc:
            return self.send_detail(502, "unreadable completion: %s" % exc)
        for choice in data.get("choices", []):
            normalise(choice)
        return self.send_json(200, data)

    def relay_stream(self, response):
        """Re-emit Open WebUI's SSE as OpenAI-shaped chunks.

        `reasoning_content` deltas are accumulated rather than forwarded: they
        are not an OpenAI field, and a chunk carrying only reasoning has no
        content to render. If the stream ends having produced no content at all,
        the accumulation is flushed as one content chunk so the reply is never
        blank.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        saw_content = False
        reasoning_parts = []
        last_chunk = None

        def emit(blob):
            self.wfile.write(blob)
            self.wfile.flush()

        try:
            for line in response:
                if not line.strip():
                    continue
                if not line.startswith(b"data: "):
                    emit(line)
                    continue
                data = line[6:].strip()
                if data == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                last_chunk = chunk
                keep = False
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    reasoning = delta.pop("reasoning_content", None)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if delta.get("content"):
                        saw_content = True
                    if delta.get("tool_calls") and choice.get("finish_reason") == "stop":
                        choice["finish_reason"] = "tool_calls"
                    # A delta emptied by stripping carries no information, and
                    # forwarding it makes a client count empty tokens.
                    if delta or choice.get("finish_reason"):
                        keep = True
                if keep:
                    emit(b"data: " + json.dumps(chunk).encode() + b"\n\n")

            if not saw_content and reasoning_parts:
                emit(b"data: " + json.dumps({
                    "id": (last_chunk or {}).get("id", "chatcmpl-fallback"),
                    "object": "chat.completion.chunk",
                    "created": (last_chunk or {}).get("created", 0),
                    "model": (last_chunk or {}).get("model", ""),
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant",
                                           "content": "".join(reasoning_parts)},
                                 "finish_reason": None}],
                }).encode() + b"\n\n")

            emit(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-stream")


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    log("openai endpoint on %s:%d -> open webui %s:%d (pinned token: %s)"
        % (LISTEN_HOST, LISTEN_PORT, WEBUI_HOST, WEBUI_PORT, "yes" if WEBUI_TOKEN else "no"))
    server.serve_forever()


if __name__ == "__main__":
    main()
