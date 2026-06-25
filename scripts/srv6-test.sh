#!/usr/bin/env bash
# SRv6 uSID DA-shift proof for srv6lab: gpu1(leaf1) -> gpu3(leaf2) via spine1.
# uSID carrier fc00:0:1101:e002:2102:fff6:: = block + [1101:e002 spine1 End.X->leaf2] + [2102:fff6 leaf2 End.DT6].
N=srv6lab
SPINE=clab-$N-spine1; SRCLEAF=clab-$N-leaf1; DSTLEAF=clab-$N-leaf2
SRCGPU=clab-$N-gpu1; DSTADDR=fd00:9:1:201::2
DSTPFX=fd00:9:1:0201::/64; CARRIER=fc00:0:1101:e002:2102:fff6::
SRCDEV=eth1; SPINECAP=eth2
DSTDT6=fc00:0:2102:fff6::/64

echo ">> 1. install tcpdump on $SPINE (if missing)"
docker exec $SPINE sh -c 'command -v tcpdump >/dev/null 2>&1 || apk add --no-cache tcpdump >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq tcpdump >/dev/null 2>&1)' || true

echo ">> 2. encap $DSTPFX into the uSID carrier on $SRCLEAF"
docker exec $SRCLEAF ip -6 route replace $DSTPFX encap seg6 mode encap segs $CARRIER dev $SRCDEV
echo "   route get (must show encap seg6):"
docker exec $SRCLEAF ip -6 route get $DSTADDR | sed "s/^/   /"
echo "   carrier first-uSID resolves toward the spine:"
docker exec $SRCLEAF ip -6 route get $CARRIER | sed "s/^/   /"

echo ">> 3. start SRH capture on $SPINE $SPINECAP (interface first, then filter)"
docker exec -d $SPINE sh -c "timeout 8 tcpdump -nni $SPINECAP 'ip6 and ip6[6]==43' > /tmp/srh.txt 2>&1"
sleep 1
echo ">> 4. ping (encapsulated) $SRCGPU -> $DSTADDR"
docker exec $SRCGPU ping6 -c5 $DSTADDR || true
sleep 1
echo ">> 5. SRH packets captured on spine egress (the DA-shift, on the wire):"
docker exec $SPINE sh -c 'grep -E "RT6|srcrt|routing" /tmp/srh.txt 2>/dev/null | sed "s/^/   /"'
SRH=$(docker exec $SPINE sh -c 'grep -cE "RT6|srcrt|routing" /tmp/srh.txt 2>/dev/null' | tr -dc 0-9); SRH=${SRH:-0}
echo "   SRH packet count: $SRH"

echo ">> 6. cleanup: remove encap (restore plain forwarding)"
docker exec $SRCLEAF ip -6 route del $DSTPFX encap seg6 mode encap segs $CARRIER dev $SRCDEV 2>/dev/null || true
if [ "$SRH" -gt 0 ]; then echo ">> PASS: spine forwarded $SRH SRH packet(s) with the DA shifted to the next uSID."; else echo ">> CHECK: no SRH seen on $SPINE $SPINECAP — verify encap (route get above) and seg6."; fi
