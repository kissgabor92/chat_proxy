#!/usr/bin/env bash
#
# Bring up the isolated OpenAI endpoint that VS Code talks to.
#
# Safe to re-run: it never overwrites an existing .env and never recreates a
# container that is already healthy.
#
#   ./init.sh              # build if needed, up, verify
#   ./init.sh --no-build   # skip the image build
#   ./init.sh --down       # stop it

set -euo pipefail

HERE="$(dirname "$(readlink -f "$0")")"
cd "$HERE"

PORT=8111
UI_PORT=3000
NETWORK=chat-proxy
BUILD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --no-build)  BUILD=0 ;;
    --down)      exec docker compose down ;;
    -h|--help)   sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '    \033[31mfail\033[0m %s\n' "$*" >&2; exit 1; }

say "Checking prerequisites"

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose v2 plugin is missing"
docker info >/dev/null 2>&1 || die "the docker daemon is not reachable"
ok "docker $(docker --version | awk '{print $3}' | tr -d ,), compose $(docker compose version --short)"

# The network is declared external: it belongs to the llm_stack project. Without
# it `up` fails with a bare "network not found", which does not say what to do.
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  die "network '$NETWORK' does not exist — start the model stack first: (cd ../llm_stack && ./init.sh)"
fi
ok "network '$NETWORK' exists"

# This service resolves webui by name over that network. If they are absent it
# still starts happily and every request 502s instead, so say so up front.
members="$(docker network inspect "$NETWORK" --format '{{range .Containers}}{{.Name}} {{end}}')"
missing=""
for c in chat-proxy-ollama chat-proxy-webui; do
  grep -q "$c" <<<"$members" || missing="$missing $c"
done
if [ -n "$missing" ]; then
  warn "not on '$NETWORK':$missing — every request will 502"
  warn "start the model stack with: (cd ../llm_stack && ./init.sh)"
else
  ok "upstreams present: chat-proxy-ollama, chat-proxy-webui"
fi

# There is no credential to generate: authentication is an Open WebUI token, and
# Open WebUI issues it. An empty WEBUI_TOKEN is valid -- it just means every
# caller must present a live session token instead of a pinned one.
if [ ! -f .env ]; then
  { echo "# An Open WebUI token. Get one from Settings -> Account -> API Keys,"
    echo "# or the \`token\` field of GET /api/v1/auths/."
    echo "WEBUI_TOKEN="; } > .env
  chmod 600 .env
  warn "created .env with an empty WEBUI_TOKEN — set one for VS Code (see below)"
elif grep -q '^WEBUI_TOKEN=.\+' .env; then
  ok ".env present with a pinned Open WebUI token"
else
  warn "WEBUI_TOKEN is empty in .env — clients must send a live session token"
fi

# Only a conflict we did not create ourselves matters.
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:${PORT} " \
&& ! docker compose ps --status running --services 2>/dev/null | grep -q proxy; then
  die "port ${PORT} is already in use by something else — change the ports: mapping"
fi
ok "port ${PORT} available"

say "Starting the OpenAI endpoint"
if [ "$BUILD" = "1" ]; then
  docker compose up -d --build --wait
else
  docker compose up -d --wait
fi
docker compose ps --format '    {{.Name}}  {{.Status}}'

say "Verifying"

health="$(curl -s -m 5 "http://127.0.0.1:${PORT}/health" || true)"
if echo "$health" | grep -q '"status": *"ok"'; then
  mode="$(echo "$health" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if d["client_token_required"]:
    print("callers must send their own token")
elif d["pinned_token_configured"]:
    print("anonymous callers served on the pinned token")
else:
    print("callers must send their own token (none pinned)")' 2>/dev/null || echo 'unknown auth mode')"
  ok "endpoint healthy — $mode"
else
  die "endpoint did not answer /health — see: docker compose logs proxy"
fi

# This port must NOT serve the interface. A 200 here would mean it is
# proxying Open WebUI again, and the port would stop telling you which
# service answered.
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)"
[ "$code" = "404" ] && ok "serves the API only — / is 404, not the UI" \
                    || warn "/ returned HTTP ${code:-no response}, expected 404"

# A 200 on the UI only proves the pass-through works. The model list travels a
# different path -- through Open WebUI's /api/models -- so check it separately.
TOKEN="$(grep -E '^WEBUI_TOKEN=' .env | cut -d= -f2-)"
models="$(curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:${PORT}/v1/models" || true)"
count="$(echo "$models" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("data",[])))' 2>/dev/null || echo 0)"
if [ "${count:-0}" -gt 0 ]; then
  ok "model list reachable through Open WebUI ($count model(s))"
else
  warn "/v1/models returned no models — is WEBUI_TOKEN valid, and are models enabled in the UI?"
fi

# Keys come from Open WebUI's own account page, which is on its own port. With
# the flag off the UI hides that section and there is no way to obtain one.
if curl -s -m 5 "http://127.0.0.1:${UI_PORT}/api/config" | grep -q '"enable_api_key": *true'; then
  ok "Open WebUI reachable on ${UI_PORT} with its API key page enabled"
else
  warn "Open WebUI on ${UI_PORT} is down, or its API Keys section is disabled"
fi


say "Ready — http://127.0.0.1:${PORT}"
cat <<NOTE
    This port serves the OpenAI API only -- no interface. Open WebUI is on
    http://127.0.0.1:${UI_PORT}. Requests go through it, never straight to Ollama.

    token           any Open WebUI token works: the JWT from your browser
                    session, or an sk- key from Settings -> Account -> API Keys
    pin it          put it in $HERE/.env as WEBUI_TOKEN, then re-run this script
    vs code         http://127.0.0.1:${PORT}/v1/chat/completions
    run the checks  $HERE/test.sh
    stop            $HERE/init.sh --down
NOTE
