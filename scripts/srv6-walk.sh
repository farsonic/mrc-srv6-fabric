#!/usr/bin/env bash
# SRv6 uSID per-hop walkthrough for srv6lab:
#   gpu1(leaf1) --encap--> spine1 --End.X DA-shift--> leaf2 --End.DT6 decap--> gpu3
# carrier fc00:0:1101:e002:2102:fff6::  =  [1101:e002 spine1 End.X->leaf2] [2102:fff6 leaf2 End.DT6]
set -u
N=srv6lab
SRCLEAF=clab-$N-leaf1; SPINE=clab-$N-spine1; DSTLEAF=clab-$N-leaf2
SRCGPU=clab-$N-gpu1; DSTADDR=fd00:9:1:201::2; DSTPFX=fd00:9:1:0201::/64
CARRIER=fc00:0:1101:e002:2102:fff6::; SHIFTED=fc00:0:2102:fff6::
SRCDEV=eth1
SPINE_IN=eth1
SPINE_OUT=eth2
DSTGPU_PORT=eth3

cap(){ docker exec -d $1 sh -c "timeout 7 tcpdump -nni $2 -vv ip6 > /tmp/walk_$3.txt 2>&1"; }
show(){ docker exec $1 sh -c "grep -m1 'echo request' /tmp/walk_$2.txt 2>/dev/null" | sed 's/^[ \t]*//'; }
ensure_tcpdump(){ docker exec $1 sh -c 'command -v tcpdump >/dev/null 2>&1 || apk add --no-cache tcpdump >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq tcpdump >/dev/null 2>&1)'; }

echo "========================================================================"
echo " SRv6 uSID walkthrough: gpu1 -> gpu3  (carrier $CARRIER)"
echo "========================================================================"

echo ">> ensuring tcpdump on the 3 hop nodes (first run may take a moment)..."
ensure_tcpdump $SRCLEAF; ensure_tcpdump $SPINE; ensure_tcpdump $DSTLEAF
echo ">> arming encap on $SRCLEAF and starting per-hop captures..."
docker exec $SRCLEAF ip -6 route replace $DSTPFX encap seg6 mode encap segs $CARRIER dev $SRCDEV
cap $SRCLEAF $SRCDEV   srcleaf_out
cap $SPINE   $SPINE_IN  spine_in
cap $SPINE   $SPINE_OUT spine_out
cap $DSTLEAF $DSTGPU_PORT dstleaf_out
sleep 2
echo ">> sending 3 pings gpu1 -> $DSTADDR"
docker exec $SRCGPU ping6 -c3 $DSTADDR >/dev/null 2>&1 || true
sleep 2

echo; echo "STEP 1  $SRCLEAF egress $SRCDEV  (just encapsulated, heading to spine)"
echo "        expect outer DA = CARRIER, active uSID = 1101:e002 (=spine1 End.X)"
echo -n "   "; show $SRCLEAF srcleaf_out

echo; echo "STEP 2  $SPINE ingress $SPINE_IN  (arriving at spine, pre-shift)"
echo "        outer DA still = CARRIER ($CARRIER)"
echo -n "   "; show $SPINE spine_in

echo; echo "STEP 3  $SPINE egress $SPINE_OUT  (AFTER End.X DA-shift, heading to dst leaf)"
echo "        expect outer DA = $SHIFTED  --  1101:e002 consumed, now active uSID = 2102:fff6 (=leaf2 End.DT6)"
echo -n "   "; show $SPINE spine_out

echo; echo "STEP 4  $DSTLEAF egress $DSTGPU_PORT  (AFTER End.DT6 decap, delivered to gpu)"
echo "        expect NO SRH: plain inner packet, DA = $DSTADDR"
echo -n "   "; show $DSTLEAF dstleaf_out

echo; echo "------------------------------------------------------------------------"
echo " DA progression:  CARRIER ($CARRIER)"
echo "          -> spine End.X shift -> $SHIFTED"
echo "          -> leaf End.DT6 decap -> inner $DSTADDR (no SRH)"
echo "------------------------------------------------------------------------"

echo ">> cleanup"
docker exec $SRCLEAF ip -6 route del $DSTPFX encap seg6 mode encap segs $CARRIER dev $SRCDEV 2>/dev/null || true
