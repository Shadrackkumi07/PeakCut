#!/usr/bin/env bash
# Prepares a fresh Oracle Cloud Ubuntu machine (Always Free ARM works) for PeakCut, then starts it.
# Run on the server, inside the PeakCut folder, after copying the folder and its .env there:
#     sudo bash deploy/oracle-setup.sh
# Safe to run again: each step checks whether it is already done.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Put the server's .env in $(pwd) first."; exit 1; }

echo "== Docker"
if ! command -v docker >/dev/null; then
  # a new machine runs its own updates for the first few minutes: wait for them instead of failing
  while fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock >/dev/null 2>&1; do sleep 10; done
  apt-get -o DPkg::Lock::Timeout=900 update -q
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=900 install -yq docker.io docker-compose-v2 netfilter-persistent iptables-persistent
  systemctl enable --now docker
fi
id -nG "${SUDO_USER:-ubuntu}" | grep -qw docker || usermod -aG docker "${SUDO_USER:-ubuntu}"

echo "== Firewall: web ports 80 and 443 (Oracle's Ubuntu image blocks everything but SSH)"
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null \
    || iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
netfilter-persistent save >/dev/null

echo "== Start PeakCut"
mkdir -p secrets
chmod 700 secrets
chmod 600 .env
docker compose up -d --build
echo "Done. Check with: docker compose ps"
