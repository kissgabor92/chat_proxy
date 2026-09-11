#!/usr/bin/env bash
# Definition-of-done checks for the bridge. Run with the llm_stack stack up.
#   ./test.sh
#
# No pipefail: several checks use `grep -q`, which exits on first match and
# SIGPIPEs curl. Under pipefail that spurious failure would flap the suite red.
set -u
cd "$(dirname "$(readlink -f "$0")")"

PORT="${PORT:-8111}"
BASE="http://127.0.0.1:${PORT}"
KEY="$(grep -E '^WEBUI_TOKEN=' .env 2>/dev/null | cut -d= -f2-)"
pass=0; fail=0

# Evaluated in this shell so the helpers below stay in scope.
check() {
  local name="$1"; shift
  if eval "$*" >/dev/null 2>&1; then
    printf '  \033[32mPASS\033[0m %s\n' "$name"; pass=$((pass+1))
  else
    printf '  \033[31mFAIL\033[0m %s\n' "$name"; fail=$((fail+1))
  fi
}

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# A real Open WebUI session, minted with Open WebUI's own signing code. Cached
# because it costs a container exec.
owui_token() {
  [ -n "${_OWUI_TOKEN:-}" ] || _OWUI_TOKEN="$(docker compose -f ../llm_stack/compose.yaml exec -T webui sh -c \
    'export $(tr "\0" "\n" < /proc/1/environ | grep -E "^WEBUI_SECRET_KEY=" | head -1); cd /app/backend && python -c "
from open_webui.models.users import Users
from open_webui.utils.auth import create_token
import datetime
us=Users.get_users(); users=us[\"users\"] if isinstance(us,dict) else us
print(create_token(data={\"id\":users[0].id}, expires_delta=datetime.timedelta(minutes=30)))"' 2>/dev/null | tr -d "\r")"
  printf '%s' "$_OWUI_TOKEN"
}
# Exactly what the account page's Create button does.
owui_create() {
  curl -s -X POST -H "Authorization: Bearer $(owui_token)" "http://127.0.0.1:3000/api/v1/auths/api_key" \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('api_key',''))"
}

AUTH="Authorization: Bearer $(owui_token)"
post()    { curl -s -m 300 -X POST "$BASE/v1/chat/completions" \
              -H "$AUTH" -H 'Content-Type: application/json' -d "$1"; }
content() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d['choices'][0]['message'].get('content') or '')"; }

ASK='{"model":"gpt-oss:20b","messages":[{"role":"user","content":"say OK"}],"stream":false}'
TINY='{"model":"gpt-oss:20b","messages":[{"role":"user","content":"say OK"}],"stream":false,"max_tokens":20}'
STREAM='{"model":"gpt-oss:20b","messages":[{"role":"user","content":"say OK"}],"stream":true,"max_tokens":80}'
TOOLS='{"model":"gpt-oss:20b","messages":[{"role":"user","content":"Weather in Paris? Use the tool."}],"stream":false,"max_tokens":300,"tools":[{"type":"function","function":{"name":"get_weather","description":"Get weather","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'

echo "== auth is Open WebUI's =="
# With a pinned token and REQUIRE_CLIENT_TOKEN=false, an anonymous caller is
# served on the pinned credential. Assert whichever mode is actually configured.
check "anonymous matches config"  'strict=$(curl -s $BASE/health | grep -c "\"client_token_required\": true"); c=$(code $BASE/v1/models); if [ "$strict" = 1 ] || [ -z "$KEY" ]; then [ "$c" = 401 ]; else [ "$c" = 200 ]; fi'
check "bogus token -> 401"        '[ "$(code -H "Authorization: Bearer not-a-real-token" $BASE/v1/models)" = 401 ]'
check "session token -> 200"      '[ "$(code -H "$AUTH" $BASE/v1/models)" = 200 ]'
check "pinned WEBUI_TOKEN works"  '[ -z "$KEY" ] || [ "$(code -H "Authorization: Bearer $KEY" $BASE/v1/models)" = 200 ]'
check "model listed"              'curl -s -H "$AUTH" $BASE/v1/models | grep -q "gpt-oss:20b"'
check "models are OpenAI-shaped"  'curl -s -H "$AUTH" $BASE/v1/models | grep -q "\"object\": \"list\""'
check "owui extras stripped"      '! curl -s -H "$AUTH" $BASE/v1/models | grep -qE "connection_type|\"actions\"|\"filters\""'
check "health needs no token"     '[ "$(code $BASE/health)" = 200 ]'

echo "== non-streaming =="
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
# Open WebUI reports finish_reason "stop" on a response that carries tool_calls;
# an agent reads that as "done" and never runs the tool.
check "finish_reason corrected"   'post "$TOOLS" | python3 -c "import sys,json;c=json.load(sys.stdin)[\"choices\"][0];sys.exit(0 if c.get(\"finish_reason\")==\"tool_calls\" else 1)"'

echo "== the proxy serves ONLY the model endpoint =="
# A UI request here must 404. If it ever proxies the interface again there is
# no way to tell from the port which service answered.
check "/ is 404, not the UI"      '[ "$(code $BASE/)" = 404 ]'
check "404 lists the real routes" 'curl -s $BASE/ | grep -q "/v1/chat/completions"'
check "no UI html on this port"   '! curl -s $BASE/ | grep -qi "<!doctype html"'
check "static assets 404"         '[ "$(code $BASE/static/favicon.png)" = 404 ]'
check "owui api not proxied"      '[ "$(code $BASE/api/config)" = 404 ]'

echo "== Open WebUI is reachable on its own port =="
check "UI html on 3000"           'curl -s http://127.0.0.1:3000/ | grep -qi "<!doctype html"'
check "owui api on 3000"          'curl -s http://127.0.0.1:3000/api/config | grep -q "\"version\""'
check "token survives recreate"   '[ "$(code -H "Authorization: Bearer $KEY" http://127.0.0.1:3000/api/models)" = 200 ]'

echo "== isolation =="
check "11434 not published"       '! ss -ltn | grep -q ":11434 "'
# 3000 is Open WebUI's own port and is meant to be published: the proxy serves
# no interface, so the UI needs a door of its own.
check "webui 3000 published"      'ss -ltn | grep -q "127.0.0.1:3000 "'
check "exactly two host ports"    '[ "$(docker ps --filter name=chat-proxy --format "{{.Ports}}" | grep -c "0.0.0.0\|127.0.0.1")" = 2 ]'
check "ui port is webui, not us"  'docker ps --filter name=chat-proxy-webui --format "{{.Ports}}" | grep -q "3000->8080"'
check "bound to loopback"         'ss -ltn | grep -q "127.0.0.1:$PORT "'
check "joined chat-proxy net"     'docker network inspect chat-proxy --format "{{range .Containers}}{{.Name}} {{end}}" | grep -q chat-proxy-vscode'
check "ollama still 100% GPU"     'docker compose -f ../llm_stack/compose.yaml exec -T ollama ollama ps | grep -q "100% GPU"'
# The bridge must reach the model only through Open WebUI; a direct route to
# Ollama would skip Open WebUI's model permissions entirely.
check "bridge never calls ollama" '! grep -qi "ollama_host\|11434" proxy.py'

echo
printf 'passed %d, failed %d\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
