#!/bin/sh
# Installs the current GitHub CLI from GitHub's apt repository (distribution packages, such as
# Ubuntu 24.04's gh 2.45, are too old for Dev Factory, which needs 2.48+). Idempotent.
#   sudo sh deploy/host/install-gh.sh
set -eu
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
KEYRING=/etc/apt/keyrings/githubcli-archive-keyring.gpg
mkdir -p -m 755 /etc/apt/keyrings
if command -v curl >/dev/null; then
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$KEYRING"
else
  wget -qO "$KEYRING" https://cli.github.com/packages/githubcli-archive-keyring.gpg
fi
chmod go+r "$KEYRING"
echo "deb [arch=$(dpkg --print-architecture) signed-by=$KEYRING] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list
apt-get update -qq
apt-get install -y -qq gh
gh --version | head -n1
