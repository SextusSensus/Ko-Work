#!/bin/bash
echo "=== IPv4 interfaces (name / addr / dynamic|static) ==="
ip -o -4 addr show | while read -r line; do
  IF=$(echo "$line" | awk '{print $2}')
  IP=$(echo "$line" | awk '{print $4}')
  DYN=$(echo "$line" | grep -o 'dynamic' || echo 'static')
  echo "  $IF  $IP  $DYN"
done
echo "=== sshd listening on all interfaces? ==="
ss -tlnp 2>/dev/null | grep ':22 ' | head -3
echo "=== link/carrier per wired iface (UP = cable present) ==="
for d in /sys/class/net/*/carrier; do
  n=$(basename "$(dirname "$d")")
  c=$(cat "$d" 2>/dev/null)
  [ "$n" != "lo" ] && echo "  $n carrier=$c operstate=$(cat /sys/class/net/$n/operstate 2>/dev/null)"
done
