#!/usr/bin/env bash
# One entry point for Linux/fnOS deployments. Never sources .env as shell code.
set -Eeuo pipefail
umask 077

usage() {
  cat <<'HELP'
Usage: bash scripts/deploy.sh <init|init-account|up|status|urls|backup> [options]
  --host ADDRESS       LAN IPv4 address for a fresh installation (default 127.0.0.1)
  --username NAME      First administrator (default admin; password read from terminal)
  --port PORT          Application port (8000, or 18081 with --fnos)
  --fnos               Use docker-compose.fnos.yml, with an existing local app image
  --file FILE          Compose file; repeat to include existing overlays
  --dir DIRECTORY      Deployment directory (default current directory)

init creates .env and the first account; it refuses to replace an existing .env.
init-account resumes first account creation after an interrupted init.
up starts the configured stack; status/urls display public addresses without tokens.
backup briefly stops data writers, copies app/QQ/vector data, then restarts only
services that were running. No command deletes volumes or resets an account.
HELP
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
command_name=${1:-help}
[[ $# == 0 ]] || shift
host=127.0.0.1 username=admin port= fnos=false directory=$PWD
files=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host|--username|--port|--dir|--file)
      [[ $# -ge 2 ]] || die "Missing value for $1"
      case "$1" in
        --host) host=$2;; --username) username=$2;; --port) port=$2;;
        --dir) directory=$2;; --file) files+=("$2");;
      esac
      shift 2;;
    --fnos) fnos=true; shift;;
    --help|-h) usage; exit 0;;
    *) die "Unknown option: $1";;
  esac
done
case "$command_name" in init|init-account|up|status|urls|backup) ;; help|--help|-h) usage; exit 0;; *) usage; exit 1;; esac
cd -- "$directory"
if [[ ${#files[@]} == 0 ]]; then
  if [[ -f compose.yml ]]; then files=(compose.yml)
  elif $fnos; then files=(docker-compose.fnos.yml)
  else files=(docker-compose.yml); fi
fi
compose_args=()
for file in "${files[@]}"; do
  [[ -f "$file" ]] || die "Missing Compose file: $file"
  compose_args+=(-f "$file")
done
dc() { docker compose --ansi never "${compose_args[@]}" "$@"; }
command -v docker >/dev/null || die 'Docker is required'
docker compose version >/dev/null

# Only read literal values. No eval, command substitution or shell sourcing of .env.
read_env() {
  local key=$1 value
  value=$(sed -n "s/^${key}=//p" .env 2>/dev/null | tail -n 1) || true
  value=${value%$'\r'}
  if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then value=${value:1:${#value}-2}; fi
  printf '%s' "$value"
}
random_key() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }
read_password() {
  if [[ -z ${LINGXI_ADMIN_PASSWORD:-} ]]; then
    [[ -t 0 ]] || die 'Set LINGXI_ADMIN_PASSWORD or run interactively to initialize the account.'
    read -r -s -p 'Administrator password (12+ characters): ' LINGXI_ADMIN_PASSWORD
    printf '\n'
  fi
  [[ ${#LINGXI_ADMIN_PASSWORD} -ge 12 ]] || die 'Administrator password must contain at least 12 characters.'
  export LINGXI_ADMIN_PASSWORD
}
init_account() {
  read_password
  dc run --rm --no-deps -e LINGXI_ADMIN_PASSWORD app init --username "$username"
  unset LINGXI_ADMIN_PASSWORD
}
urls() {
  local public_url napcat_url
  public_url=${LINGXI_PUBLIC_URL:-$(read_env LINGXI_PUBLIC_URL)}
  napcat_url=${LINGXI_NAPCAT_WEBUI_URL:-$(read_env LINGXI_NAPCAT_WEBUI_URL)}
  [[ -z "$public_url" ]] || printf 'Lingxi: %s/\n' "${public_url%/}"
  [[ -z "$napcat_url" ]] || printf 'NapCat: %s\n' "$napcat_url"
  [[ -n "$public_url$napcat_url" ]] || printf 'Set LINGXI_PUBLIC_URL / LINGXI_NAPCAT_WEBUI_URL in .env to display access addresses.\n'
}

if [[ "$command_name" == init ]]; then
  [[ ! -e .env ]] || die '.env already exists and was preserved. Use up/status for an existing installation.'
  [[ ! -e data/lingxi.sqlite3 && ! -e data/master.key ]] || die 'Existing app data found. Configure .env for the existing deployment; init never replaces it.'
  [[ ! -d napcat || -z $(find napcat -mindepth 1 -print -quit) ]] || die 'Existing NapCat data found. Reuse its configuration/token through .env; init never replaces it.'
  [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die '--host must be an IPv4 address; configure public DNS URLs in .env after initialization'
  IFS=. read -r -a octets <<< "$host"
  for octet in "${octets[@]}"; do (( 10#$octet <= 255 )) || die 'Invalid IPv4 address'; done
  [[ "$host" != 0.0.0.0 ]] || die 'Use a reachable LAN address, not 0.0.0.0'
  if $fnos; then
    [[ "$host" != 127.0.0.1 ]] || die '--fnos requires --host with the NAS LAN address'
    docker image inspect lingxi:2.0.0 >/dev/null 2>&1 || die 'Import/build the lingxi:2.0.0 image before init.'
  fi
  [[ $(id -u) == 0 || $(id -u) == 1000 ]] || die 'Run init as UID 1000 or root so container data permissions can be set correctly.'
  [[ -n "$port" ]] || { if $fnos; then port=18081; else port=8000; fi; }
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port >= 1 && 10#$port <= 65535 )) || die 'Invalid port'
  read_password
  napcat_token=$(random_key)
  vector_token=$(random_key)
  cat > .env <<EOF
COMPOSE_PROFILES=napcat
LINGXI_IMAGE=lingxi:2.0.0
LINGXI_NAS_IP=$host
LINGXI_BIND_HOST=$host
LINGXI_PORT=$port
LINGXI_PUBLIC_URL=http://$host:$port
LINGXI_ALLOW_PRIVATE_NETWORK=true
LINGXI_FEISHU_EMBEDDED=true
LINGXI_QDRANT_API_KEY=$vector_token
LINGXI_NAPCAT_MANAGED=true
LINGXI_NAPCAT_DATA_DIR=./napcat
LINGXI_NAPCAT_WEBUI_URL=http://$host:6099/webui/
LINGXI_NAPCAT_HTTP_URL=http://napcat:3000
LINGXI_NAPCAT_HTTP_TOKEN=$napcat_token
NAPCAT_BIND_IP=$host
NAPCAT_WEBUI_PORT=6099
EOF
  mkdir -p data napcat/config napcat/plugins napcat/qq backups
  # Never touch a previously configured QQ instance, even when .env is new.
  if [[ ! -e napcat/config/onebot11.json ]]; then
    cat > napcat/config/onebot11.json <<EOF
{"network":{"httpServers":[{"name":"lingxi-http","enable":true,"host":"0.0.0.0","port":3000,"messagePostFormat":"array","token":"$napcat_token"}],"httpClients":[],"httpSseServers":[],"websocketServers":[],"websocketClients":[],"plugins":[]}}
EOF
  fi
  chmod 600 .env
  if [[ $(id -u) == 0 ]]; then
    chown 1000:1000 data napcat napcat/config napcat/config/onebot11.json napcat/plugins napcat/qq
  fi
  dc config --quiet
  if ! docker image inspect "$(read_env LINGXI_IMAGE)" >/dev/null 2>&1; then
    if $fnos; then die 'Import/build the app image first. .env is ready; initialize the account with docker compose run --rm -e LINGXI_ADMIN_PASSWORD app init, then use up.'
    else dc build app; fi
  fi
  init_account
  unset napcat_token vector_token
  printf 'Initialized. Run the same command with "up" to start the stack.\n'
  urls
  exit 0
fi

[[ -f .env ]] || die 'No .env found. Run init first, or copy and configure .env.docker.example.'
dc config --quiet
case "$command_name" in
  init-account)
    init_account
    printf 'Administrator initialized; use up to start the stack.\n';;
  up)
    dc up -d --wait
    urls;;
  status)
    dc ps
    urls;;
  urls) urls;;
  backup)
    [[ -n $(dc ps -a -q app) ]] || die 'No app container exists to back up. Start the deployment first.'
    running_services=$(dc ps --status running --services)
    configured_services=$(dc config --services)
    backup_dir="$PWD/backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    mkdir -p "$backup_dir"
    chmod 700 "$backup_dir"
    touch "$backup_dir/INCOMPLETE"
    cp -- .env "$backup_dir/env"
    index=0
    for file in "${files[@]}"; do
      cp -- "$file" "$backup_dir/compose-$index.yml"
      printf '%s\n' "$file" >> "$backup_dir/compose-files.txt"
      index=$((index + 1))
    done
    dc config --images > "$backup_dir/images.txt"
    mapfile -t active < <(printf '%s\n' "$running_services" | sed '/^$/d')
    # start, rather than up, preserves the exact stopped containers/configuration.
    restart_previous() {
      local result=$?
      trap - EXIT
      if [[ ${#active[@]} -gt 0 ]]; then dc start "${active[@]}" || result=1; fi
      exit "$result"
    }
    trap restart_previous EXIT
    if [[ ${#active[@]} -gt 0 ]]; then dc stop "${active[@]}"; fi
    for service in app worker qdrant napcat feishu-bridge; do
      [[ $'\n'"$configured_services"$'\n' == *$'\n'"$service"$'\n'* ]] || continue
      container_id=$(dc ps -a -q "$service")
      [[ -n "$container_id" ]] || continue
      case "$service" in
        app) dc cp app:/data "$backup_dir/app-data";;
        worker) [[ -d "$backup_dir/app-data" ]] || dc cp worker:/data "$backup_dir/app-data";;
        qdrant) dc cp qdrant:/qdrant/storage "$backup_dir/qdrant-storage";;
        napcat)
          mkdir "$backup_dir/napcat"
          dc cp napcat:/app/napcat/config "$backup_dir/napcat/config"
          dc cp napcat:/app/napcat/plugins "$backup_dir/napcat/plugins"
          dc cp napcat:/app/.config/QQ "$backup_dir/napcat/qq";;
        feishu-bridge) dc cp feishu-bridge:/data "$backup_dir/feishu-bridge-data";;
      esac
    done
    rm -- "$backup_dir/INCOMPLETE"
    printf 'Backup: %s\n' "$backup_dir"
    printf 'Contains private keys, account data and QQ login state. Keep this directory private.\n';;
esac
