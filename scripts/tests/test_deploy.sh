#!/usr/bin/env bash
# Exercise deployment decisions without accessing a Docker daemon or live data.
set -Eeuo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
mkdir "$temporary/bin"
cat > "$temporary/bin/docker" <<'MOCK'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$MOCK_LOG"
if [[ $1 == image ]]; then exit 0; fi
[[ $1 == compose ]] || exit 91
shift
while [[ $# -gt 0 ]]; do
  case "$1" in --ansi|-f) shift 2;; *) break;; esac
done
case "$1" in
  version|up|stop|start|build) exit 0;;
  config)
    case "${2:-}" in
      --services) printf 'app\nqdrant\nnapcat\n';;
      --images) printf 'lingxi:2.0.0\nqdrant/qdrant:v1.19.0\n';;
      '') printf 'services:\n  included-service:\n    image: test-included-image\n    environment:\n      PRIVATE_TOKEN: synthetic-resolved-secret\n';;
    esac;;
  run) [[ ${MOCK_INIT_FAIL:-0} != 1 ]] || exit 37;;
  ps)
    if [[ "$*" == *'--status running'* ]]; then printf 'app\nnapcat\n'
    elif [[ "$*" == *'-q'* ]]; then printf 'test-container\n'; fi;;
  cp)
    [[ ${MOCK_COPY_FAIL:-0} != 1 ]] || exit 42
    mkdir -p -- "$3"
    touch "$3/copied-data";;
  *) exit 92;;
esac
MOCK
chmod +x "$temporary/bin/docker"
# Model a normal UID-1000 deployment user, regardless of the CI runner UID.
printf '#!/usr/bin/env bash\nif [[ ${1:-} == -u ]]; then printf "1000\\n"; else /usr/bin/id "$@"; fi\n' > "$temporary/bin/id"
chmod +x "$temporary/bin/id"
export PATH="$temporary/bin:$PATH"
export MOCK_LOG="$temporary/docker.log"
export LINGXI_ADMIN_PASSWORD='synthetic-test-password'
new_case() {
  mkdir "$temporary/$1"
  cd "$temporary/$1"
  printf 'services: {}\n' > docker-compose.yml
  : > "$MOCK_LOG"
}
expect_fail() { if "$@" >output 2>&1; then printf 'Unexpected success: %s\n' "$*" >&2; exit 1; fi; }
deploy() { bash "$root/scripts/deploy.sh" "$@"; }

new_case short-password
expect_fail env LINGXI_ADMIN_PASSWORD=short bash "$root/scripts/deploy.sh" init
[[ ! -e .env ]]
new_case invalid-address
expect_fail deploy init --host nas.example.com
[[ ! -e .env ]]
expect_fail deploy init --host 192.168.1.999
[[ ! -e .env ]]
new_case existing-napcat
mkdir -p napcat/config
printf 'existing-onebot-token' > napcat/config/onebot11.json
expect_fail deploy init
[[ ! -e .env && $(cat napcat/config/onebot11.json) == existing-onebot-token ]]
new_case existing-app
mkdir data
touch data/lingxi.sqlite3
expect_fail deploy init
[[ ! -e .env ]]

new_case fresh
deploy init --host 192.0.2.10 --username king >output
[[ $(stat -c %a .env) == 600 ]]
token=$(sed -n 's/^LINGXI_NAPCAT_HTTP_TOKEN=//p' .env)
[[ "$token" =~ ^[0-9a-f]{64}$ ]]
node -e 'const fs=require("node:fs"); const env=fs.readFileSync(".env","utf8"); const token=env.match(/^LINGXI_NAPCAT_HTTP_TOKEN=(.+)$/m)[1]; const config=JSON.parse(fs.readFileSync("napcat/config/onebot11.json","utf8")); if(config.network.httpServers[0].token!==token || config.network.httpClients.length)process.exit(1);'
before=$(sha256sum .env)
expect_fail deploy init
[[ $(sha256sum .env) == "$before" ]]
deploy urls >output
grep -q 'http://192.0.2.10:6099/webui/' output
if grep -q "$token" output; then printf 'Secret leaked in URLs\n' >&2; exit 1; fi

new_case interrupted
expect_fail env MOCK_INIT_FAIL=1 bash "$root/scripts/deploy.sh" init
[[ -f .env ]]
before=$(sha256sum .env)
deploy init-account --username king >output
[[ $(sha256sum .env) == "$before" ]]
deploy backup >output
[[ $(find backups -name INCOMPLETE | wc -l) == 0 ]]
[[ $(find backups -path '*/napcat/qq/copied-data' | wc -l) == 1 ]]
resolved=$(find backups -name compose.resolved.yml)
[[ $(stat -c %a "$resolved") == 600 ]]
grep -q included-service "$resolved"
grep -q synthetic-resolved-secret "$resolved"
if grep -q synthetic-resolved-secret output; then printf 'Resolved Compose secret leaked to console\n' >&2; exit 1; fi
grep -q 'start app napcat$' "$MOCK_LOG"
if grep -q 'start .*qdrant' "$MOCK_LOG"; then printf 'Backup started a previously stopped service\n' >&2; exit 1; fi
: > "$MOCK_LOG"
expect_fail env MOCK_COPY_FAIL=1 bash "$root/scripts/deploy.sh" backup
[[ $(find backups -name INCOMPLETE | wc -l) == 1 ]]
grep -q 'start app napcat$' "$MOCK_LOG"
printf 'Deployment helper tests passed (fresh init, preservation, resume, URL redaction, backup recovery).\n'
