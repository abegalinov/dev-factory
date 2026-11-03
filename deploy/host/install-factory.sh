#!/bin/sh
# Installs or updates Dev Factory as a systemd timer. Idempotent: run it again to update.
#   sudo sh deploy/host/install-factory.sh --project FILE [--project FILE ...] \
#     --gh-user LOGIN --agent-env FILE [--mutate] [--no-start]
#
#   --project FILE    a host registration (config/projects/<id>.yaml) to install, enabled
#   --gh-user LOGIN   the factory's GitHub account; its token is taken from the invoking
#                     user's `gh` login (sudo's SUDO_USER). Skip once devfactory is logged in
#   --agent-env FILE  KEY=VALUE file with CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`)
#   --mutate          set runtime.observe_only: false. Without it the timer only observes
#   --no-start        install everything but leave the timer off
#   --repo URL        default https://github.com/abegalinov/dev-factory.git
#
# Result:
#   /opt/dev-factory          the clone (updated with git pull), owned by devfactory
#   /opt/dev-factory/config   host config; local edits survive updates
#   /var/lib/dev-factory      devfactory's HOME: gh login, agent.env, the poll lock
# Nothing under /etc/dev-factory (the deployer's) is touched. Requires git, gh, python3 and
# docker; uses uv when python3 has no ensurepip. See docs/operations.md.
set -eu

APP=/opt/dev-factory
DF_HOME=/var/lib/dev-factory
REPO=https://github.com/abegalinov/dev-factory.git
PROJECTS="" GH_USER="" AGENT_ENV="" MUTATE=0 START=1

die() { echo "error: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECTS="$PROJECTS
$2"; shift 2 ;;
    --gh-user) GH_USER="$2"; shift 2 ;;
    --agent-env) AGENT_ENV="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --mutate) MUTATE=1; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
for cmd in git gh python3 docker systemctl; do
  command -v "$cmd" >/dev/null || die "$cmd not found"
done
GH_VERSION=$(gh --version | sed -n 's/^gh version \([0-9.]*\).*/\1/p')
if [ "$(printf '2.48\n%s\n' "$GH_VERSION" | sort -V | head -n1)" != "2.48" ]; then
  die "gh $GH_VERSION is too old (2.48+ needed): install it from https://cli.github.com"
fi
for file in $PROJECTS $AGENT_ENV; do
  [ -f "$file" ] || die "$file not found"
done

as_df() { (cd "$APP" && runuser -u devfactory -- env HOME="$DF_HOME" "$@"); }

echo "== devfactory user (docker group: it starts the agent containers)"
if ! id devfactory >/dev/null 2>&1; then
  useradd --system --home-dir "$DF_HOME" --create-home --shell /usr/sbin/nologin \
    --groups docker devfactory
fi
chmod 750 "$DF_HOME"

echo "== code in $APP"
if [ -d "$APP/.git" ]; then
  as_df git pull -q --ff-only --autostash
else
  git clone -q "$REPO" "$APP"
fi
for file in $PROJECTS; do
  dest="$APP/config/projects/$(basename "$file")"
  install -m 640 "$file" "$dest"
  sed -i 's/^enabled: false/enabled: true/' "$dest"
done
if [ "$MUTATE" -eq 1 ]; then
  sed -i 's/^  observe_only: true/  observe_only: false/' "$APP/config/factory.yaml"
fi
mkdir -p "$APP/.runtime"
chown -R devfactory:devfactory "$APP"

echo "== virtualenv"
if python3 -c "import ensurepip" 2>/dev/null; then
  as_df python3 -m venv .venv
  as_df .venv/bin/pip install -q -e .
else
  UV=$(command -v uv || true)
  if [ -z "$UV" ] && [ -n "${SUDO_USER:-}" ]; then
    UV=$(eval echo "~$SUDO_USER")/.local/bin/uv
  fi
  [ -x "$UV" ] || die "python3 has no ensurepip and uv was not found"
  install -m 755 "$UV" /usr/local/bin/uv
  as_df /usr/local/bin/uv venv -q --allow-existing --python "$(command -v python3)" .venv
  as_df /usr/local/bin/uv pip install -q -p .venv -e .
fi

echo "== GitHub login"
if [ -n "$GH_USER" ]; then
  [ -n "${SUDO_USER:-}" ] || die "--gh-user reads the invoking user's gh login; run via sudo"
  runuser -u "$SUDO_USER" -- gh auth token --user "$GH_USER" \
    | as_df gh auth login --hostname github.com --with-token
fi
as_df gh auth status >/dev/null 2>&1 || die "devfactory has no gh login; pass --gh-user"

echo "== agent token"
if [ -n "$AGENT_ENV" ]; then
  install -d -m 700 -o devfactory -g devfactory "$DF_HOME/.config" "$DF_HOME/.config/dev-factory"
  install -m 600 -o devfactory -g devfactory "$AGENT_ENV" "$DF_HOME/.config/dev-factory/agent.env"
fi

echo "== checks"
as_df .venv/bin/dev-factory --config-dir "$APP/config" validate
for file in $PROJECTS; do
  as_df .venv/bin/dev-factory --config-dir "$APP/config" doctor \
    --project "$(sed -n "s/^project_id: *//p" "$file")" || true
done

echo "== systemd"
install -m 644 "$APP/deploy/systemd/dev-factory.slice" \
  "$APP/deploy/systemd/dev-factory-poll.service" "$APP/deploy/systemd/dev-factory-poll.timer" \
  /etc/systemd/system/
systemctl daemon-reload
if [ "$START" -eq 1 ]; then
  systemctl enable --now dev-factory-poll.timer
  echo "first poll:"
  systemctl start dev-factory-poll.service || true
  journalctl -u dev-factory-poll -n 20 --no-pager
fi
grep -n "observe_only:" "$APP/config/factory.yaml"
systemctl list-timers dev-factory-poll.timer --no-pager
