#!/usr/bin/env bash
# Ethernet bring-up for the board's RTL8152B (test card stage 5). Run on the Pi with J5 cabled to a Pi USB port.
#   bash eth_check.sh                 # 5.1-5.6
#   bash eth_check.sh soak 600        # 5.7: ping for 600 s, count carrier drops
# Optional throughput (5.6): IPERF_SERVER=<host running `iperf3 -s`> bash eth_check.sh
# Output is also saved to ~/NereusData/UrchinCam/<YYYY-MM-DD>_eth/.
set -u

OUT_DIR="$HOME/NereusData/UrchinCam/$(date +%Y-%m-%d)_eth"
mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/eth_check_$(date +%H%M%S).txt"
exec > >(tee -a "$LOG") 2>&1

FAILS=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }

find_iface() {
  local d
  for d in /sys/class/net/*; do
    if [ "$(basename "$(readlink -f "$d/device/driver" 2>/dev/null)")" = "r8152" ]; then
      basename "$d"
      return 0
    fi
  done
  return 1
}

IFACE="$(find_iface || true)"

if [ "${1:-}" = "soak" ]; then
  SECS="${2:-600}"
  [ -n "$IFACE" ] || { fail "5.7 no r8152 interface"; exit 1; }
  GW="$(ip -4 route show default dev "$IFACE" | awk '{print $3; exit}')"
  [ -n "$GW" ] || { fail "5.7 no default gateway on $IFACE"; exit 1; }
  C0="$(cat "/sys/class/net/$IFACE/carrier_changes")"
  echo "soak: ping $GW on $IFACE for $SECS s"
  ping -I "$IFACE" -i 1 -w "$SECS" "$GW" | tail -3
  C1="$(cat "/sys/class/net/$IFACE/carrier_changes")"
  if [ "$C1" = "$C0" ]; then pass "5.7 no carrier changes in $SECS s"; else fail "5.7 carrier changed $((C1 - C0)) time(s)"; fi
  echo "log: $LOG"
  exit "$FAILS"
fi

echo "== 5.1 lsusb"
lsusb
if lsusb | grep -qi "0bda:8152"; then pass "5.1 Realtek 0bda:8152 on USB"; else fail "5.1 no 0bda:8152 on USB"; fi

echo "== 5.2 driver"
(dmesg 2>/dev/null || journalctl -k --no-pager 2>/dev/null) | grep -i r8152 | tail -10
if [ -n "$IFACE" ]; then pass "5.2 interface $IFACE bound to r8152"; else fail "5.2 no interface bound to r8152"; echo "log: $LOG"; exit 1; fi

echo "== 5.3 link"
for _ in $(seq 1 10); do
  [ "$(cat "/sys/class/net/$IFACE/operstate")" = "up" ] && break
  sleep 1
done
STATE="$(cat "/sys/class/net/$IFACE/operstate")"
SPEED="$(cat "/sys/class/net/$IFACE/speed" 2>/dev/null || echo "?")"
DUPLEX="$(cat "/sys/class/net/$IFACE/duplex" 2>/dev/null || echo "?")"
echo "operstate=$STATE speed=$SPEED duplex=$DUPLEX"
if [ "$STATE" = "up" ] && [ "$SPEED" = "100" ] && [ "$DUPLEX" = "full" ]; then
  pass "5.3 link up 100 Mb/s full"
else
  fail "5.3 link (want up/100/full; if down, check J2 pairs)"
fi

echo "== 5.4 DHCP"
ADDR=""
for _ in $(seq 1 30); do
  ADDR="$(ip -4 -o addr show dev "$IFACE" | awk '{print $4; exit}')"
  [ -n "$ADDR" ] && break
  sleep 1
done
if [ -n "$ADDR" ]; then pass "5.4 IPv4 $ADDR"; else fail "5.4 no IPv4 address in 30 s"; fi

echo "== 5.5 ping"
GW="$(ip -4 route show default dev "$IFACE" | awk '{print $3; exit}')"
if [ -z "$GW" ]; then
  fail "5.5 no default gateway on $IFACE"
else
  LOSS="$(ping -I "$IFACE" -c 100 -i 0.2 "$GW" | tee /dev/stderr | sed -n 's/.* \([0-9.]*\)% packet loss.*/\1/p')"
  if [ "$LOSS" = "0" ]; then pass "5.5 100 pings to $GW, 0% loss"; else fail "5.5 ${LOSS:-?}% loss to $GW"; fi
fi

echo "== 5.6 throughput"
if [ -z "${IPERF_SERVER:-}" ]; then
  echo "SKIP 5.6 set IPERF_SERVER=<host running iperf3 -s> to run it"
elif ! command -v iperf3 >/dev/null; then
  echo "SKIP 5.6 iperf3 not installed (installing it is Nick's call)"
else
  BIND="${ADDR%/*}"
  for DIR in "" "-R"; do
    BPS="$(iperf3 -c "$IPERF_SERVER" -B "$BIND" -t 30 $DIR -J | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["end"]["sum_received"]["bits_per_second"]))')"
    MBPS=$((BPS / 1000000))
    WHICH=$([ -z "$DIR" ] && echo "Pi->server" || echo "server->Pi")
    if [ "$MBPS" -ge 80 ]; then pass "5.6 $WHICH $MBPS Mb/s"; else fail "5.6 $WHICH $MBPS Mb/s (bar 80)"; fi
  done
fi

echo "fails: $FAILS"
echo "log: $LOG"
exit "$FAILS"
