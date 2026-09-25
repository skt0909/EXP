#!/bin/sh
# 2 GB swap file: a safety net for pip install, the daily prediction
# backfill's ~270 MB peak, and any spike on a 1 GB box.
# Run once as root: sudo sh deploy/scripts/setup_swap.sh
set -e
if swapon --show | grep -q /swapfile; then
    echo "swap already on"; exit 0
fi
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
# Prefer RAM; swap only under real pressure.
sysctl vm.swappiness=10
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
swapon --show
