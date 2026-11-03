#!/bin/sh
# One-time host hardening for running Dev Factory next to other services. Idempotent.
#   sudo sh deploy/host/setup-host.sh [swap size, default 2G]
# - Adds a swapfile (a safety net against memory spikes; factory containers never use it).
# - Prefers RAM over swap (vm.swappiness=10).
# - Installs dev-factory.slice, which caps all factory work together.
set -eu
SWAP_SIZE="${1:-2G}"
SWAPFILE=/swapfile
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }

if swapon --noheadings --show=NAME | grep -q .; then
  echo "swap already active:"; swapon --show
else
  [ -e "$SWAPFILE" ] || fallocate -l "$SWAP_SIZE" "$SWAPFILE"
  chmod 600 "$SWAPFILE"
  mkswap "$SWAPFILE" >/dev/null
  swapon "$SWAPFILE"
  grep -q "^$SWAPFILE " /etc/fstab || echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
  echo "swap enabled: $SWAP_SIZE at $SWAPFILE"
fi

echo "vm.swappiness=10" > /etc/sysctl.d/99-dev-factory.conf
sysctl -q -p /etc/sysctl.d/99-dev-factory.conf
echo "vm.swappiness=$(cat /proc/sys/vm/swappiness)"

install -m 644 "$HERE/../systemd/dev-factory.slice" /etc/systemd/system/dev-factory.slice
systemctl daemon-reload
systemctl start dev-factory.slice
echo "dev-factory.slice MemoryMax=$(systemctl show dev-factory.slice -p MemoryMax --value)"
