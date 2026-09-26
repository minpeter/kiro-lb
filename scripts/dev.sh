#!/usr/bin/env bash
# Local development stack for one kiro-lb git worktree (AGENTS.md, LOCAL DEVELOPMENT).
#
#   scripts/dev.sh init [--no-deps]   dev-only .env, then .venv + frontend deps (incl. portless)
#   scripts/dev.sh api                backend behind portless (foreground)
#   scripts/dev.sh web                Vite dashboard with HMR behind portless (foreground)
#   scripts/dev.sh status             proxy routes and this worktree's URLs
#   scripts/dev.sh proxy-stop         stop the shared LAN proxy
#
# Apps listen on loopback ports portless assigns; one LAN-mode portless proxy
# publishes them as http://<branch>.kiro-lb.local:<proxy port> over mDNS.
# DEV_HOST (the address advertised over mDNS) defaults to the IPv4 address on
# the default-route interface, i.e. the LAN. Export DEV_HOST to override. No
# address is hardcoded here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
# The marker that makes a .env a dev .env. A production .env never has it.
DEV_MARKER='DEV_ENV="1"'
WEB_NAME=kiro-lb
API_NAME=api.kiro-lb
PORTLESS_BIN="${DEV_PORTLESS_BIN:-$ROOT/frontend/node_modules/.bin/portless}"
# A state dir and port of its own, so a portless setup other projects use
# (~/.portless, :1355) is never switched into LAN mode by this script.
export PORTLESS_STATE_DIR="${DEV_PORTLESS_STATE_DIR:-$HOME/.portless-kiro-lb}"
export PORTLESS_PORT="${DEV_PROXY_PORT:-1356}"
# Never edit /etc/hosts; names are served over mDNS.
export PORTLESS_SYNC_HOSTS=0

die() { printf 'dev.sh: %s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*" >&2; }

usage() { sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

guard_not_deploy_root() {
  # deploy.sh writes this gitignored per-host file into the checkout it deploys
  # from; that checkout's data/ and .env belong to the live slot.
  if [[ -e "$ROOT/deploy/bluegreen/active_slot" ]]; then
    die "this checkout is the deploy root (deploy/bluegreen/active_slot exists); develop in a worktree, see AGENTS.md"
  fi
}

require_dev_env() {
  [[ -f "$ENV_FILE" ]] || die "no .env; run: scripts/dev.sh init"
  grep -qx "$DEV_MARKER" "$ENV_FILE" \
    || die ".env has no $DEV_MARKER line, so it is not a dev .env; refusing to use it"
}

require_portless() {
  [[ -x "$PORTLESS_BIN" ]] || die "portless not installed; run: scripts/dev.sh init"
}

portless() { "$PORTLESS_BIN" "$@"; }

dev_host() {
  if [[ -n "${DEV_HOST:-}" ]]; then
    printf '%s\n' "$DEV_HOST"
    return
  fi
  command -v ip >/dev/null || die "ip(8) not found; export DEV_HOST"
  local dev addr
  # The main table's default route, not `ip route get`: a VPN policy table can
  # win that lookup and would advertise the VPN address instead of the LAN.
  dev="$(ip -4 route show default | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit }}')"
  [[ -n "$dev" ]] || die "no IPv4 default route; export DEV_HOST"
  addr="$(ip -4 -o addr show dev "$dev" | awk '{ split($4, a, "/"); print a[1]; exit }')"
  [[ -n "$addr" ]] || die "no IPv4 address on $dev; export DEV_HOST"
  printf '%s\n' "$addr"
}

# Starts the shared LAN proxy if it is not running; a no-op otherwise.
ensure_proxy() {
  local host
  host="$(dev_host)"
  command -v avahi-publish-address >/dev/null || [[ -n "${DEV_PORTLESS_BIN:-}" ]] \
    || die "LAN mode needs avahi-publish-address; install avahi-utils"
  # --ip pins the advertised address; auto-detection can pick a VPN interface.
  portless proxy start -p "$PORTLESS_PORT" --no-tls --lan --ip "$host" >/dev/null
}

# host:port of a route, as the proxy matches it in the Host header.
route_authority() {
  local url
  url="$(portless get "$1")"
  url="${url#*://}"
  printf '%s\n' "${url%%/*}"
}

init_env() {
  if [[ -f "$ENV_FILE" ]]; then
    require_dev_env
    log ".env exists; keeping its secrets"
    return
  fi
  # Secrets are generated here, never written as literals in this file.
  local api_key dashboard_secret
  api_key="dev-$(openssl rand -hex 16)"
  dashboard_secret="dev-$(openssl rand -hex 8)"
  # noclobber: never replace an existing file; umask: 0600 before any secret lands.
  (
    set -C
    umask 077
    {
      printf '# Local development only (scripts/dev.sh). Never reuse production values.\n'
      printf '%s\n' "$DEV_MARKER"
      printf '%s="%s"\n' PROXY_API_KEY "$api_key"
      printf '%s="%s"\n' DASHBOARD_PASSWORD "$dashboard_secret"
      printf 'DASHBOARD_DATA_DIR="data"\n'
      printf 'DASHBOARD_SECURE_COOKIE="false"\n'
      printf 'LOG_LEVEL="DEBUG"\n'
    } >"$ENV_FILE"
  )
  log "wrote .env (0600)"
}

init_deps() {
  command -v uv >/dev/null || die "uv not found"
  command -v bun >/dev/null || die "bun not found"
  # A moved worktree keeps a .venv whose scripts point at the old path.
  if [[ -f .venv/bin/pytest && "$(head -n 1 .venv/bin/pytest)" != "#!$ROOT/.venv/bin/python" ]]; then
    log ".venv was built for another path; recreating"
    rm -rf .venv
  fi
  if [[ ! -x .venv/bin/python ]]; then
    uv venv --python 3.12 .venv
  fi
  uv pip install --python .venv/bin/python \
    -r requirements.txt -r requirements-test.txt -r requirements-dev.txt
  (cd frontend && bun install --frozen-lockfile)
}

cmd_init() {
  local deps=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-deps) deps=0 ;;
      *) die "unknown init option: $1" ;;
    esac
    shift
  done
  guard_not_deploy_root
  command -v openssl >/dev/null || die "openssl not found"
  init_env
  if [[ "$deps" -eq 1 ]]; then
    init_deps
  fi
  log "next: scripts/dev.sh api   (and scripts/dev.sh web in a second terminal)"
}

cmd_api() {
  guard_not_deploy_root
  require_dev_env
  require_portless
  [[ -x .venv/bin/python ]] || die "no .venv; run: scripts/dev.sh init"
  ensure_proxy
  log "api  $(portless get "$API_NAME")"
  # portless sets HOST (loopback) and PORT. env -i: load_dotenv() never
  # overrides an exported variable, so a shell that exports the production
  # PROXY_API_KEY would otherwise win over this .env.
  # shellcheck disable=SC2016  # expanded by the inner sh, after portless sets them
  exec "$PORTLESS_BIN" run --name "$API_NAME" \
    sh -c 'exec env -i HOME="$HOME" PATH="$PATH" "$0" main.py --host "$HOST" --port "$PORT"' \
    "$ROOT/.venv/bin/python"
}

cmd_web() {
  guard_not_deploy_root
  require_dev_env
  require_portless
  [[ -d frontend/node_modules ]] || die "no frontend/node_modules; run: scripts/dev.sh init"
  ensure_proxy
  local api
  api="$(route_authority "$API_NAME")"
  log "web  $(portless get "$WEB_NAME")  (api via $api)"
  # Vite proxies to the portless proxy on loopback and names the api route in
  # the Host header (vite.config.ts): the api's own port changes on restart,
  # and this host cannot resolve multi-label .local names itself.
  cd frontend
  exec env API_PROXY_TARGET="http://localhost:$PORTLESS_PORT" API_PROXY_HOST="$api" \
    "$PORTLESS_BIN" run --name "$WEB_NAME" bun run dev
}

cmd_status() {
  require_portless
  portless list || true
  if [[ -f "$ENV_FILE" ]] && grep -qx "$DEV_MARKER" "$ENV_FILE"; then
    printf '\nthis worktree: dashboard %s  api %s\n' \
      "$(portless get "$WEB_NAME")" "$(portless get "$API_NAME")"
  fi
}

cmd_proxy_stop() {
  require_portless
  portless proxy stop -p "$PORTLESS_PORT"
}

main() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "$cmd" in
    init) cmd_init "$@" ;;
    api) cmd_api ;;
    web) cmd_web ;;
    status) cmd_status ;;
    proxy-stop) cmd_proxy_stop ;;
    -h | --help | help | "") usage ;;
    *) die "unknown command: $cmd (see scripts/dev.sh --help)" ;;
  esac
}

main "$@"
