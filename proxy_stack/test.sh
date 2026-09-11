#!/usr/bin/env bash
# Definition-of-done checks for the proxy. Run with it up and an Open WebUI
# reachable at LLM_HOSTNAME.
#   ./test.sh
#
# No pipefail: several checks use `grep -q`, which exits on first match and
# SIGPIPEs curl. Under pipefail that spurious failure would flap the suite red.
set -u
cd "$(dirname "$(readlink -f "$0")")"

PORT="$(grep -E '^LISTEN_PORT=' .env | cut -d= -f2-)"; PORT="${PORT:-8111}"
BASE="http://127.0.0.1:${PORT}"
KEY="$(grep -E '^WEBUI_TOKEN=' .env | cut -d= -f2-)"
UPSTREAM="$(grep -E '^LLM_HOSTNAME=' .env | cut -d= -f2-)"
pass=0; fail=0

check() {
  local name="$1"; shift
  if eval "$*" >/dev/null 2>&1; then
    printf '  \033[32mPASS\033[0m %s\n' "$name"; pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m %s\n' "$name"; fail=$((fail+1))
  fi
}

code()    { curl -s -o /dev/null -w '%{http_code}' "$@"; }
post()    { curl -s -m 300 -X POST "$BASE/v1/chat/completions" \
              -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$1"; }
content() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d['choices'][0]['message'].get('content') or '')"; }
# The first model the endpoint advertises, so the suite is not pinned to one
# model name and works against any Open WebUI.
MODEL="$(curl -s -H "Authorization: Bearer $KEY" "$BASE/v1/models" \
        | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["data"][0]["id"])' 2>/dev/null)"

ASK="{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"say OK\"}],\"stream\":false}"
TINY="{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"say OK\"}],\"stream\":false,\"max_tokens\":20}"
STREAM="{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"say OK\"}],\"stream\":true,\"max_tokens\":80}"
TOOLS="{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Weather in Paris? Use the tool.\"}],\"stream\":false,\"max_tokens\":300,\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"get_weather\",\"description\":\"Get weather\",\"parameters\":{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}}}]}"

echo "== configuration =="
check "LLM_HOSTNAME is set"       '[ -n "$UPSTREAM" ]'
check "model discovered"          '[ -n "$MODEL" ]'
check "health needs no token"     '[ "$(code $BASE/health)" = 200 ]'
check "health names the upstream" 'curl -s $BASE/health | grep -q "\"upstream\""'

echo "== auth is Open WebUI's =="
check "bogus token -> 401"        '[ "$(code -H "Authorization: Bearer not-a-real-token" $BASE/v1/models)" = 401 ]'
check "pinned token works"        '[ -z "$KEY" ] || [ "$(code -H "Authorization: Bearer $KEY" $BASE/v1/models)" = 200 ]'
check "anonymous matches config"  'strict=$(curl -s $BASE/health | grep -c "\"client_token_required\": true"); c=$(code $BASE/v1/models); if [ "$strict" = 1 ] || [ -z "$KEY" ]; then [ "$c" = 401 ]; else [ "$c" = 200 ]; fi'

echo "== OpenAI translation =="
check "models are OpenAI-shaped"  'curl -s -H "Authorization: Bearer $KEY" $BASE/v1/models | grep -q "\"object\": \"list\""'
check "owui extras stripped"      '! curl -s -H "Authorization: Bearer $KEY" $BASE/v1/models | grep -qE "connection_type|\"actions\"|\"filters\""'
check "returns content"           '[ -n "$(post "$ASK" | content)" ]'
check "reasoning_content gone"    '! post "$ASK" | grep -q "reasoning_content"'

echo "== a tiny budget must not produce an empty reply =="
check "max_tokens=20 non-empty"   '[ -n "$(post "$TINY" | content)" ]'

echo "== streaming =="
check "emits data: chunks"        'post "$STREAM" | grep -q "^data: {"'
check "terminates with [DONE]"    'post "$STREAM" | tail -3 | grep -q "\[DONE\]"'
check "stream carries content"    'post "$STREAM" | grep -q "\"content\": \"[^\"]"'
check "no reasoning in stream"    '! post "$STREAM" | grep -q "reasoning_content"'

echo "== tool calling =="
check "returns tool_calls"        'post "$TOOLS" | grep -q tool_calls'
# Open WebUI reports finish_reason "stop" on a response carrying tool_calls; an
# agent reads that as "done" and never runs the tool.
check "finish_reason corrected"   'post "$TOOLS" | python3 -c "import sys,json;c=json.load(sys.stdin)[\"choices\"][0];sys.exit(0 if c.get(\"finish_reason\")==\"tool_calls\" else 1)"'

echo "== serves the API only =="
check "/ is 404, not the UI"      '[ "$(code $BASE/)" = 404 ]'
check "404 lists the real routes" 'curl -s $BASE/ | grep -q "/v1/chat/completions"'
check "no UI html on this port"   '! curl -s $BASE/ | grep -qi "<!doctype html"'
check "owui api not proxied"      '[ "$(code $BASE/api/config)" = 404 ]'

echo "== standalone =="
# llm_stack/ still exists on disk holding 13 GB of weights for a container
# started before it was removed; what matters is that no code refers to it.
check "no llm_stack dependency"   '! grep -rqE "llm_stack|\\.\\./" proxy.py compose.yaml init.sh Dockerfile'
check "compose has no network dep" '! grep -qE "^ *external: *true" compose.yaml'
check "only LLM_HOSTNAME upstream" 'grep -q "LLM_HOSTNAME" compose.yaml && ! grep -qiE "OLLAMA_|WEBUI_HOST" compose.yaml'
check "bound to loopback"         'ss -ltn | grep -q "127.0.0.1:$PORT "'

echo
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
