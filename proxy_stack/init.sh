#!/usr/bin/env bash
#
# Bring up the OpenAI-compatible proxy in front of an Open WebUI instance.
#
# Safe to re-run: it never overwrites an existing .env and never recreates a
# container that is already healthy.
#
#   ./init.sh              # build if needed, up, verify
#   ./init.sh --no-build   # skip the image build
#   ./init.sh --tls        # why an https upstream is or is not trusted
#   ./init.sh --autocert   # fetch the CA it needs, keep it, wire it in
#   ./init.sh --down       # stop it

set -euo pipefail

HERE="$(dirname "$(readlink -f "$0")")"
cd "$HERE"

BUILD=1
TLS_ONLY=0
AUTOCERT=0
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=0 ;;
    --tls)      TLS_ONLY=1 ;;
    --autocert) AUTOCERT=1 ;;
    --down)     exec docker compose down ;;
    -h|--help)  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
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

# There is nothing to generate: this proxy has no credential of its own, and
# LLM_HOSTNAME is the one thing only the operator knows.
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  die "created .env from the example — set LLM_HOSTNAME in it, then re-run"
fi
ok ".env present"

LLM_HOSTNAME="$(grep -E '^LLM_HOSTNAME=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")"
[ -n "$LLM_HOSTNAME" ] || die "LLM_HOSTNAME is empty in .env — point it at an Open WebUI instance"
PORT="$(grep -E '^LISTEN_PORT=' .env | cut -d= -f2-)"; PORT="${PORT:-8111}"

# Resolve exactly the way proxy.py does, so a bad value is caught here with an
# explanation rather than as a crash loop after `up`.
read -r SCHEME HOST UPORT BASE <<EOF
$(python3 - "$LLM_HOSTNAME" <<'PY'
import sys, urllib.parse
v = sys.argv[1].strip()
if "://" not in v:
    v = "http://" + v
p = urllib.parse.urlsplit(v)
if p.scheme not in ("http", "https") or not p.hostname:
    sys.exit(1)
print(p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80), p.path.rstrip("/") or "-")
PY
)
EOF
[ -n "${HOST:-}" ] || die "LLM_HOSTNAME is not a usable host or URL: $LLM_HOSTNAME"
[ "$BASE" = "-" ] && BASE=""
ok "upstream: ${SCHEME}://${HOST}:${UPORT}${BASE}"

# Both keys are optional, and `set -e` would treat a .env without them as fatal.
CA_FILE="$(grep -E '^UPSTREAM_CA_FILE=' .env | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
TLS_VERIFY="$(grep -E '^UPSTREAM_TLS_VERIFY=' .env | cut -d= -f2- || true)"; TLS_VERIFY="${TLS_VERIFY:-true}"
[ -z "$CA_FILE" ] || [ -f "$CA_FILE" ] || die "UPSTREAM_CA_FILE is set but there is no file at $CA_FILE"

# An unreachable upstream makes every request 502; say so before starting.
if python3 -c "
import socket,sys
try:
    socket.create_connection(('$HOST', $UPORT), timeout=5).close()
except OSError as e:
    sys.exit(str(e))
" 2>/dev/null; then
  ok "upstream reachable"
else
  warn "cannot reach ${HOST}:${UPORT} from this shell — every request will 502"
  warn "note the container uses host networking, so it resolves names the same way"
fi

# A certificate the proxy will not trust is the other way every request 502s,
# and OpenSSL's wording for it names neither cause. proxy.py --tls-check prints
# the chain the server sends and which certificate would complete it; run inside
# the container when there is one, because the trust store that decides is the
# container's, not this shell's.
tls_check() {
  if docker compose ps --status running --services 2>/dev/null | grep -q '^proxy$'; then
    docker compose exec -T proxy python /app/proxy.py --tls-check 2>&1
  else
    env LLM_HOSTNAME="$LLM_HOSTNAME" UPSTREAM_CA_BUNDLE="$CA_FILE" \
        UPSTREAM_TLS_VERIFY="$TLS_VERIFY" python3 proxy.py --tls-check 2>&1
  fi
}

report_tls() {  # $1 = "always" to print the chain even when it verifies
  local out rc
  if [ "$SCHEME" != "https" ]; then
    ok "${LLM_HOSTNAME} is plain http — no certificate involved"
    return 0
  fi
  # `set -e` would take a failing check as a reason to stop; it is a finding.
  out="$(tls_check)" && rc=0 || rc=$?
  if [ "$rc" = 0 ]; then
    ok "upstream certificate trusted${CA_FILE:+ (via $CA_FILE)}"
    if [ "${1:-}" = always ]; then printf '%s\n' "$out" | sed 's/^/         /'; fi
  else
    warn "upstream certificate not trusted — every request will 502:"
    printf '%s\n' "$out" | sed 's/^/         /'
    # The container's store is the one missing it; this host's may not be, and
    # an internal CA is usually installed exactly here.
    warn "looking for that certificate on this host:"
    env LLM_HOSTNAME="$LLM_HOSTNAME" python3 proxy.py --find-ca 2>&1 | sed 's/^/         /' || true
    warn "or set UPSTREAM_TLS_VERIFY=false to stop checking (unauthenticated)"
  fi
  return $rc
}

if [ "$TLS_ONLY" = 1 ]; then
  say "Checking the upstream certificate"
  report_tls always
  exit $?
fi

# Certificates live outside proxy_stack: they are host state, not part of the
# image, and nothing in the build context should carry them.
CERTS_DIR="$(cd "$HERE/.." && pwd)/proxy_stack_certs"

set_env_key() {  # key value -- add or replace it in .env, leaving the rest alone
  if grep -qE "^$1=" .env; then
    sed -i "s#^$1=.*#$1=$2#" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

if [ "$AUTOCERT" = 1 ]; then
  say "Fetching the certificate this upstream needs"
  if [ "$SCHEME" != "https" ]; then
    ok "${LLM_HOSTNAME} is plain http — no certificate needed"
    exit 0
  fi
  mkdir -p "$CERTS_DIR"
  # Run on the host: the container is the one that lacks the certificate, and
  # this host is where it is most likely to already be.
  out="$(env LLM_HOSTNAME="$LLM_HOSTNAME" UPSTREAM_CA_BUNDLE="$CA_FILE" \
             python3 proxy.py --autocert "$CERTS_DIR" 2>&1)" && rc=0 || rc=$?
  printf '%s\n' "$out" | sed 's/^/    /'
  [ "$rc" = 0 ] || die "could not obtain it — proxy_stack/tls_101.md covers what to ask for"

  pem="$(printf '%s\n' "$out" | sed -n 's/^UPSTREAM_CA_FILE=//p' | tail -1)"
  if [ -z "$pem" ]; then
    ok "nothing to change"
    exit 0
  fi
  set_env_key UPSTREAM_CA_FILE "$pem"
  ok "UPSTREAM_CA_FILE written to .env"
  if [ "$TLS_VERIFY" = "false" ]; then
    set_env_key UPSTREAM_TLS_VERIFY true
    ok "UPSTREAM_TLS_VERIFY back to true — the escape hatch is not needed now"
  fi
  CA_FILE="$pem"; TLS_VERIFY=true

  say "Applying it"
  docker compose up -d --wait
  docker compose ps --format '    {{.Name}}  {{.Status}}'
  report_tls always
  exit $?
fi

if [ "$SCHEME" = "https" ]; then
  if [ "$TLS_VERIFY" = "false" ]; then
    warn "UPSTREAM_TLS_VERIFY=false — the upstream certificate will not be checked"
  else
    report_tls || true
  fi
fi

# Only a conflict we did not create ourselves matters.
if ss -ltn 2>/dev/null | grep -qE "127\.0\.0\.1:${PORT} |0\.0\.0\.0:${PORT} " \
&& ! docker compose ps --status running --services 2>/dev/null | grep -q proxy; then
  die "port ${PORT} is already in use by something else — change LISTEN_PORT in .env"
fi
ok "port ${PORT} available"

say "Starting the proxy"
if [ "$BUILD" = "1" ]; then docker compose up -d --build --wait; else docker compose up -d --wait; fi
docker compose ps --format '    {{.Name}}  {{.Status}}'

say "Verifying"

health="$(curl -s -m 5 "http://127.0.0.1:${PORT}/health" || true)"
echo "$health" | grep -q '"status": *"ok"' \
  || die "proxy did not answer /health — see: docker compose logs proxy"
mode="$(echo "$health" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if d["client_token_required"]:
    print("callers must send their own token")
elif d["pinned_token_configured"]:
    print("anonymous callers served on the pinned token")
else:
    print("callers must send their own token (none pinned)")' 2>/dev/null || echo "unknown auth mode")"
ok "proxy healthy — $mode"

# This port must serve no interface. A 200 here would mean it is proxying the
# UI again, and the port would stop telling you which service answered.
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)"
[ "$code" = "404" ] && ok "serves the API only — / is 404, not the UI" \
                    || warn "/ returned HTTP ${code:-no response}, expected 404"

TOKEN="$(grep -E '^WEBUI_TOKEN=' .env | cut -d= -f2-)"
models="$(curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:${PORT}/v1/models" || true)"
count="$(echo "$models" | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("data",[])))' 2>/dev/null || echo 0)"
if [ "${count:-0}" -gt 0 ]; then
  ok "model list reachable ($count model(s))"
  echo "$models" | python3 -c 'import sys,json;[print("         -",m["id"]) for m in json.load(sys.stdin)["data"]]'
else
  warn "/v1/models returned no models — is WEBUI_TOKEN valid for ${HOST}?"
fi

say "Ready — http://127.0.0.1:${PORT}"
cat <<NOTE
    This port serves the OpenAI API only. Open WebUI itself is at
    ${SCHEME}://${HOST}:${UPORT}${BASE}

    token           any Open WebUI token: the JWT from your browser session
                    (F12 -> localStorage.token), or Settings -> Account -> API Keys
    vs code         http://127.0.0.1:${PORT}/v1/chat/completions
    run the checks  $HERE/test.sh
    stop            $HERE/init.sh --down
NOTE
