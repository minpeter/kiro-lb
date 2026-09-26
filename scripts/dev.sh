#!/usr/bin/env bash
# Local development stack for one kiro-lb git worktree (AGENTS.md, LOCAL DEVELOPMENT).
#
#   scripts/dev.sh init [--no-deps]   dev-only .env with a port slot, then .venv + frontend deps
#   scripts/dev.sh api                backend on $DEV_HOST:$DEV_API_PORT (foreground)
#   scripts/dev.sh web                Vite dashboard with HMR on $DEV_HOST:$DEV_WEB_PORT (foreground)
#   scripts/dev.sh status             this worktree's URLs and every worktree's slot
#
# DEV_HOST defaults to the IPv4 address on the default-route interface, i.e. the
# LAN. Export DEV_HOST to override. No address is hardcoded here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
# Slot N: backend API_BASE+SLOT_STEP*N, dashboard WEB_BASE+SLOT_STEP*N. Slot 0
# stays clear of the production edge (:8000) and both blue/green slots.
API_BASE=8100
WEB_BASE=5174
SLOT_STEP=10
MAX_SLOTS=50

die() { printf 'dev.sh: %s\n' "$*" >&2; exit 1; }
log() { printf '==> %s\n' "$*" >&2; }

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# Value of KEY="value" in an env file. Reads single lines only: sourcing the
# file would export the dev secrets into the caller's shell.
env_value() {
  sed -n "s/^$1=\"\\([^\"]*\\)\"\$/\\1/p" "$2"
}

guard_not_deploy_root() {
  # deploy.sh writes this gitignored per-host file into the checkout it deploys
  # from; that checkout's data/ and .env belong to the live slot.
  if [[ -e "$ROOT/deploy/bluegreen/active_slot" ]]; then
    die "this checkout is the deploy root (deploy/bluegreen/active_slot exists); develop in a worktree, see AGENTS.md"
  fi
}

# Loads DEV_API_PORT / DEV_WEB_PORT from this worktree's .env, refusing any
# .env that init did not write (a production .env has no DEV_ lines).
load_ports() {
  [[ -f "$ENV_FILE" ]] || die "no .env; run: scripts/dev.sh init"
  DEV_API_PORT="$(env_value DEV_API_PORT "$ENV_FILE")"
  DEV_WEB_PORT="$(env_value DEV_WEB_PORT "$ENV_FILE")"
  [[ "$DEV_API_PORT" =~ ^[0-9]+$ && "$DEV_WEB_PORT" =~ ^[0-9]+$ ]] \
    || die ".env has no DEV_API_PORT/DEV_WEB_PORT, so it is not a dev .env; refusing to use it"
}

dev_host() {
  if [[ -n "${DEV_HOST:-}" ]]; then
    printf '%s\n' "$DEV_HOST"
    return
  fi
  command -v ip >/dev/null || die "ip(8) not found; export DEV_HOST"
  local dev addr
  dev="$(ip -4 route show default | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit }}')"
  [[ -n "$dev" ]] || die "no IPv4 default route; export DEV_HOST"
  addr="$(ip -4 -o addr show dev "$dev" | awk '{ split($4, a, "/"); print a[1]; exit }')"
  [[ -n "$addr" ]] || die "no IPv4 address on $dev; export DEV_HOST"
  printf '%s\n' "$addr"
}

port_listening() {
  command -v ss >/dev/null || return 1
  ss -ltnH "( sport = :$1 )" | grep -q .
}

worktree_paths() {
  git -C "$ROOT" worktree list --porcelain | sed -n 's/^worktree //p'
}

# Backend ports recorded in any worktree's .env, one per line.
claimed_api_ports() {
  local wt
  while IFS= read -r wt; do
    if [[ -f "$wt/.env" ]]; then
      env_value DEV_API_PORT "$wt/.env"
    fi
  done < <(worktree_paths)
}

# Prints "API WEB" for the lowest slot that no worktree claims and nothing
# listens on. Claimed-but-idle slots stay reserved so their URLs stay stable.
allocate_slot() {
  local claimed n api web
  claimed="$(claimed_api_ports)"
  for ((n = 0; n < MAX_SLOTS; n++)); do
    api=$((API_BASE + SLOT_STEP * n))
    web=$((WEB_BASE + SLOT_STEP * n))
    if grep -qx "$api" <<<"$claimed" || port_listening "$api" || port_listening "$web"; then
      continue
    fi
    printf '%s %s\n' "$api" "$web"
    return
  done
  die "no free port slot among the first $MAX_SLOTS"
}

init_env() {
  if [[ -f "$ENV_FILE" ]]; then
    load_ports
    log ".env exists; keeping its secrets and slot (api $DEV_API_PORT, web $DEV_WEB_PORT)"
    return
  fi

  # Serialize allocation across worktrees: two inits racing would otherwise
  # read the same claims and pick the same slot.
  local lock slot api web
  if command -v flock >/dev/null; then
    lock="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/kiro-lb-dev-ports.lock"
    exec 9>"$lock"
    flock 9
  fi

  slot="$(allocate_slot)"
  api="${slot% *}"
  web="${slot#* }"
  # noclobber: never replace an existing file; umask: 0600 before any secret lands.
  (
    set -C
    umask 077
    {
      printf '# Local development only (scripts/dev.sh). Never reuse production values.\n'
      printf 'PROXY_API_KEY="dev-%s"\n' "$(openssl rand -hex 16)"
      printf 'DASHBOARD_PASSWORD="dev-%s"\n' "$(openssl rand -hex 8)"
      printf 'DASHBOARD_DATA_DIR="data"\n'
      printf 'DASHBOARD_SECURE_COOKIE="false"\n'
      printf 'LOG_LEVEL="DEBUG"\n'
      printf 'DEV_API_PORT="%s"\n' "$api"
      printf 'DEV_WEB_PORT="%s"\n' "$web"
    } >"$ENV_FILE"
  )

  if [[ -n "${lock:-}" ]]; then
    exec 9>&-
  fi
  log "wrote .env (0600): api $api, web $web"
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
  load_ports
  [[ -x .venv/bin/python ]] || die "no .venv; run: scripts/dev.sh init"
  local host
  host="$(dev_host)"
  log "api  http://$host:$DEV_API_PORT"
  # env -i: load_dotenv() never overrides an exported variable, so a shell that
  # exports the production PROXY_API_KEY would otherwise win over this .env.
  exec env -i HOME="$HOME" PATH="$PATH" "$ROOT/.venv/bin/python" main.py \
    --host "$host" --port "$DEV_API_PORT"
}

cmd_web() {
  guard_not_deploy_root
  load_ports
  [[ -d frontend/node_modules ]] || die "no frontend/node_modules; run: scripts/dev.sh init"
  local host
  host="$(dev_host)"
  log "web  http://$host:$DEV_WEB_PORT  (proxying to http://$host:$DEV_API_PORT)"
  cd frontend
  # --strictPort: fail instead of drifting off the slot's recorded URL.
  exec env API_PROXY_TARGET="http://$host:$DEV_API_PORT" \
    bun run dev --host "$host" --port "$DEV_WEB_PORT" --strictPort
}

cmd_status() {
  local host wt api web state
  host="$(dev_host)"
  printf '%-8s %-8s %-10s %s\n' API WEB STATE WORKTREE
  while IFS= read -r wt; do
    [[ -f "$wt/.env" ]] || continue
    api="$(env_value DEV_API_PORT "$wt/.env")"
    web="$(env_value DEV_WEB_PORT "$wt/.env")"
    [[ -n "$api" ]] || continue
    state=stopped
    if port_listening "$api" || port_listening "$web"; then
      state=running
    fi
    printf '%-8s %-8s %-10s %s\n' "$api" "$web" "$state" "$wt"
  done < <(worktree_paths)
  if [[ -f "$ENV_FILE" ]] && [[ -n "$(env_value DEV_API_PORT "$ENV_FILE")" ]]; then
    load_ports
    printf '\nthis worktree: dashboard http://%s:%s  api http://%s:%s\n' \
      "$host" "$DEV_WEB_PORT" "$host" "$DEV_API_PORT"
  fi
}

main() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift
  case "$cmd" in
    init) cmd_init "$@" ;;
    api) cmd_api ;;
    web) cmd_web ;;
    status) cmd_status ;;
    -h | --help | help | "") usage ;;
    *) die "unknown command: $cmd (see scripts/dev.sh --help)" ;;
  esac
}

main "$@"
