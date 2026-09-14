#!/usr/bin/env bash
#
# Bring up the Open WebUI + Ollama stack and leave it ready to chat.
#
# Safe to re-run: it never re-pulls a model that is already present and never
# recreates containers that are already healthy.
#
#   ./init.sh                  # up + pull the default model if nothing is there
#   MODEL=llama3.2:3b ./init.sh
#   ./init.sh --no-pull        # just bring the stack up
#   ./init.sh --down           # stop the stack (weights and history survive)

set -euo pipefail

HERE="$(dirname "$(readlink -f "$0")")"
cd "$HERE"

MODEL="${MODEL:-gpt-oss:20b}"
UI_PORT=3000
PULL=1

for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    --down)    exec docker compose down ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# The compose file pins the ollama service to uid 1000 so bind-mounted weights
# are not root-owned. On a host where that is not the current user, the files
# land unwritable instead.
host_uid="$(id -u)"
if [ "$host_uid" != "1000" ]; then
  warn "you are uid $host_uid but compose.yaml pins ollama to 1000:1000"
  warn "edit the 'user:' line, or ./ollama will be owned by the wrong user"
else
  ok "uid 1000 matches the user: pin in compose.yaml"
fi

# GPU is optional -- Ollama runs on CPU without it, just far slower -- but a
# missing device node aborts 'up' outright, and a missing driver library is
# worse: the stack starts fine and silently serves from the CPU.
missing_dev=""
for d in /dev/nvidia0 /dev/nvidiactl /dev/nvidia-uvm /dev/nvidia-uvm-tools; do
  [ -e "$d" ] || missing_dev="$missing_dev $d"
done
missing_lib=""
for l in /usr/lib/x86_64-linux-gnu/libcuda.so.1 \
         /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1 \
         /usr/lib/x86_64-linux-gnu/libnvidia-ptxjitcompiler.so.1 \
         /usr/lib/x86_64-linux-gnu/libnvidia-nvvm.so.4; do
  [ -e "$l" ] || missing_lib="$missing_lib $l"
done

if [ -n "$missing_dev" ]; then
  die "missing device node(s):$missing_dev — is the nvidia driver loaded? (nvidia-smi)"
elif [ -n "$missing_lib" ]; then
  warn "missing driver librar(ies):$missing_lib"
  warn "compose.yaml mounts these by path; 'up' will fail until they exist"
else
  ok "nvidia device nodes and driver libraries present"
fi

# Open WebUI signs its JWTs with this. Its own start.sh would persist a
# generated key to /app/backend/.webui_secret_key -- inside the image, not the
# data volume -- so without pinning it here every container recreation silently
# invalidates every API token and every browser session.
if [ ! -f .env ]; then
  { echo "# Open WebUI's JWT signing key. Changing it invalidates every API"
    echo "# token and every browser session, so keep it stable."
    echo "WEBUI_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"; } > .env
  chmod 600 .env
  ok "generated .env with a fresh WEBUI_SECRET_KEY"
elif grep -q '^WEBUI_SECRET_KEY=.\+' .env; then
  ok ".env present with a pinned WEBUI_SECRET_KEY"
else
  die ".env exists but WEBUI_SECRET_KEY is empty — set it, or every login breaks on restart"
fi

# The gateway in front of Open WebUI accepts exactly one `session` cookie
# value. Generated once and kept: ../proxy_stack/.env pins the same value as
# UPSTREAM_COOKIE, and changing it here breaks that pairing.
if ! grep -q '^GATEWAY_SESSION=.\+' .env; then
  { echo
    echo "# The one 'session' cookie value the gateway lets through to Open WebUI."
    echo "# ../proxy_stack/.env must carry it as UPSTREAM_COOKIE=session=<this>."
    echo "GATEWAY_SESSION=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"; } >> .env
  ok "generated a GATEWAY_SESSION into .env"
else
  ok "GATEWAY_SESSION pinned in .env"
fi
GATEWAY_SESSION="$(grep '^GATEWAY_SESSION=' .env | cut -d= -f2-)"

# Only a conflict we did not create ourselves matters.
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:${UI_PORT} " \
&& ! docker compose ps --status running --services 2>/dev/null | grep -q gateway; then
  die "port ${UI_PORT} is already in use by something else — change the ports: mapping"
fi
ok "port ${UI_PORT} available"

mkdir -p ollama

say "Starting the stack"
docker compose up -d --wait
docker compose ps --format '    {{.Name}}  {{.Status}}'

say "Models"
if [ "$PULL" = "1" ]; then
  if docker compose exec -T ollama ollama list 2>/dev/null | grep -q "^${MODEL%%:*}"; then
    ok "$MODEL already present, skipping pull"
  else
    warn "pulling $MODEL — this is a multi-GB download and will take a while"
    docker compose exec -T ollama ollama pull "$MODEL"
    ok "$MODEL pulled"
  fi
else
  ok "--no-pull given, skipping"
fi
docker compose exec -T ollama ollama list 2>/dev/null | sed 's/^/    /'

say "Verifying"

# Ollama falls back to CPU silently and still answers correctly, so the device
# has to be checked directly rather than inferred from a working reply.
if docker compose exec -T ollama ls /dev/nvidia0 >/dev/null 2>&1; then
  gpu_name="$(docker compose logs ollama 2>/dev/null \
    | grep 'inference compute' | grep -v 'library=cpu' \
    | tail -1 | sed -n 's/.*name="\([^"]*\)".*/\1/p')"
  if [ -n "$gpu_name" ]; then
    ok "GPU in use: $gpu_name"
  else
    warn "device node present but Ollama reports no CUDA device — check 'docker compose logs ollama'"
  fi
else
  warn "no /dev/nvidia0 in the container — inference will run on CPU"
fi

# The gateway must refuse without the cookie and pass with it -- both halves,
# or it is not reproducing the deployment it stands in for.
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${UI_PORT}/api/config" || true)"
[ "$code" = "401" ] && ok "gateway refuses without a session cookie (401)" \
                    || warn "gateway answered HTTP ${code:-no response} without a cookie, expected 401"
code="$(curl -s -o /dev/null -w '%{http_code}' -b "session=${GATEWAY_SESSION}" \
        "http://127.0.0.1:${UI_PORT}/api/config" || true)"
[ "$code" = "200" ] && ok "Open WebUI responding through the gateway with the cookie" \
                    || warn "UI returned HTTP ${code:-no response} with the cookie"

say "Ready — http://127.0.0.1:${UI_PORT}/gateway/login"
cat <<NOTE
    Everything on ${UI_PORT} is behind a session-cookie gateway. In a browser,
    open /gateway/login once: it sets the cookie and forwards you to the UI.
    First visit then asks you to create a local admin account; it stays in
    the chat-proxy-openwebui-data volume on this machine.

    VS Code talks to a separate service on 8111 -- see ../proxy_stack. That
    proxy needs the cookie too; in ../proxy_stack/.env:
        UPSTREAM_COOKIE=session=${GATEWAY_SESSION}

    ollama ps        docker compose -f $HERE/compose.yaml exec ollama ollama ps
    stop             $HERE/init.sh --down
NOTE
