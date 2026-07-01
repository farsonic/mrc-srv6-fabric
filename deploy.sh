#!/bin/bash
# deploy.sh — bring up the mrc-srv6 fabric, push SRv6 configs onto the community
# docker-sonic-vs switches, configure the hosts, and verify the uSID SIDs are
# programmed into the kernel FIB. Ports the emulator's proven sequence; self-
# contained; independent of sonic01.
#
#   ./deploy.sh deploy     # containerlab deploy only
#   ./deploy.sh config     # push switch configs + verify
#   ./deploy.sh hosts      # configure host00/host01 (installs iproute2 first)
#   ./deploy.sh all        # deploy -> switches -> hosts  (default)
#   ./deploy.sh verify     # just re-check seg6local counts
#   ./deploy.sh destroy
set -uo pipefail
cd "$(dirname "$0")"
TOPO=topology.clab.yaml
SWITCHES="p0-spine00 p0-spine01 p0-leaf00 p0-leaf01"
HOSTS="host00 host01"
CMD="${1:-all}"

# host -> its tenant /64 (host = ::2, leaf gw = ::1)
host_pfx() { case "$1" in host00) echo "2001:db8:cccc:00";; host01) echo "2001:db8:cccc:01";; esac; }

push_switch() {
  local N="$1"
  echo "  -- switch $N --"
  printf '#!/bin/sh\nexec "$@"\n' | docker exec -i "$N" tee /usr/local/bin/sudo >/dev/null 2>&1
  docker exec "$N" chmod +x /usr/local/bin/sudo 2>/dev/null || true
  docker cp "config/$N/config_db.json" "$N:/etc/sonic/config_db.json"
  docker exec "$N" bash -c 'ip link show Loopback0 >/dev/null 2>&1 || { ip link add Loopback0 type dummy && ip link set Loopback0 up; }' 2>/dev/null || true
  docker exec "$N" bash -c 'sonic-cfggen -j /etc/sonic/config_db.json --write-to-db' 2>/dev/null || true
  docker exec "$N" bash -c 'supervisorctl restart all' >/dev/null 2>&1 || true
  docker exec "$N" bash -c '
    ip link add vrfdefault type vrf table main 2>/dev/null; ip link set vrfdefault up 2>/dev/null
    ip link add sr0 type dummy 2>/dev/null; ip link set sr0 up 2>/dev/null
    sysctl -w net.vrf.strict_mode=1 >/dev/null 2>&1
    sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1' 2>/dev/null || true
  docker exec "$N" bash -c 'for p in $(sonic-cfggen -d --var-json PORT | python3 -c "import sys,json;print(\" \".join(json.load(sys.stdin)))"); do config interface startup $p 2>/dev/null; done' 2>/dev/null || true
  for i in $(seq 1 30); do docker exec "$N" vtysh -c 'show version' >/dev/null 2>&1 && break; sleep 2; done
  local FRR_DIR; FRR_DIR="$(docker exec "$N" bash -c 'test -d /etc/sonic/frr && echo /etc/sonic/frr || echo /etc/frr' 2>/dev/null | tr -d '\r')"; FRR_DIR="${FRR_DIR:-/etc/frr}"
  docker cp "config/$N/frr.conf" "$N:$FRR_DIR/frr.conf"
  _load() {
    docker exec "$N" supervisorctl stop bgpd zebra staticd >/dev/null 2>&1 || true; sleep 2
    docker exec "$N" supervisorctl start bgpd zebra staticd >/dev/null 2>&1 || true; sleep 3
    for asn in 65000 65001 65100; do docker exec "$N" vtysh -c 'configure terminal' -c "no router bgp $asn" -c exit >/dev/null 2>&1 || true; done
    docker exec "$N" vtysh -f "$FRR_DIR/frr.conf" >/dev/null 2>&1 || true; sleep 2
  }
  local want have
  want=$(grep -cE '^[[:space:]]+sid[[:space:]]' "config/$N/frr.conf")
  _load
  have=$(docker exec "$N" ip -6 route show table all 2>/dev/null | grep -c seg6local || echo 0)
  if [ "${have:-0}" -lt "$want" ]; then echo "     retry ($have/$want)..."; _load
    have=$(docker exec "$N" ip -6 route show table all 2>/dev/null | grep -c seg6local || echo 0); fi
  echo "     seg6local programmed: $have/$want"
}

configure_host() {
  local H="$1" P; P="$(host_pfx "$H")"
  echo "  -- host $H (${P}::2) --"
  if ! docker exec "$H" bash -c 'command -v ip >/dev/null 2>&1'; then
    echo "     installing iproute2 (base ubuntu has no ip)..."
    docker exec "$H" bash -c 'apt-get update -qq >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iproute2 iputils-ping >/dev/null 2>&1' || true
  fi
  docker exec "$H" bash -c 'command -v ip >/dev/null 2>&1' || { echo "     ERROR: no ip in $H (mgmt net has no internet for apt?)"; return 1; }
  docker exec "$H" bash -c "
    ip -6 addr replace ${P}::2/64 dev eth1 2>/dev/null
    ip -6 addr replace ${P}::2/128 dev lo 2>/dev/null
    sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null 2>&1
    ip -6 route replace fc00:0000::/32 via ${P}::1 dev eth1
    ip -6 route replace 2001:db8:cccc::/48 via ${P}::1 dev eth1
    ip -6 route replace fc00:0000:d001::/48 dev eth1 encap seg6local action End.DT6 table 0
  " 2>/dev/null || true
  echo "     eth1: $(docker exec "$H" bash -c "ip -6 addr show dev eth1 2>/dev/null | awk '/inet6/{print \$2}' | tr '\n' ' '")"
}

verify() { echo "== seg6local in kernel FIB =="; for n in $SWITCHES; do
  h=$(docker exec "$n" ip -6 route show table all 2>/dev/null | grep -c seg6local || echo 0)
  echo "  $n: $h"; done; }

case "$CMD" in
  deploy)  sudo containerlab deploy -t "$TOPO" --reconfigure ;;
  config)  echo "== switches =="; for n in $SWITCHES; do push_switch "$n"; done; verify ;;
  hosts)   echo "== hosts =="; for h in $HOSTS; do configure_host "$h"; done ;;
  verify)  verify ;;
  all)     sudo containerlab deploy -t "$TOPO" --reconfigure
           echo "waiting 40s for SONiC services..."; sleep 40
           echo "== switches =="; for n in $SWITCHES; do push_switch "$n"; done
           echo "== hosts =="; for h in $HOSTS; do configure_host "$h"; done
           verify ;;
  destroy) sudo containerlab destroy -t "$TOPO" ;;
  *) echo "usage: $0 {deploy|config|hosts|verify|all|destroy}"; exit 1 ;;
esac
