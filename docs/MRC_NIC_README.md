# MRC virtual-NIC layer — quickstart

This bundle adds a standalone virtual MRC NIC (Pollara/Thor-class emulation) to every GPU,
on top of the proven SRv6 fabric. It is ADDITIVE and gated by the `--mrc-nic` generator flag.

## What you get
- `nic/mrc-nic`   — standalone virtual MRC NIC (controller-optional; reads a local profile.json)
- `nic/mrc-probe` — per-EV SO_MARK traffic generator (sprays across the pinned EV set)
- `nic/mrc-sink`  — receiver with per-EV SACK/NAK/OOO reporting
- generator `--mrc-nic` flag: emits `mrc-nic/<gpu>.json` (full EV-set of uSID carriers,
  one EV per dest GPU per plane per spine path) and bind-mounts + launches mrc-nic on each GPU.

## Generate (small first)
    cd ~/clab/srv6
    python3 gen_clab_topology.py --image quay.io/frrouting/frr:10.6.1 \
      --gpu-image ghcr.io/hellt/network-multitool:latest \
      --gpus-per-leaf 2 --leaves 2 --spines 2 --planes 1 --name srv6lab \
      --plan address_plan.json --out fabric --frr-linux --seg6 frr --mrc-nic

Then deploy as usual (containerlab or the Ansible site-frr.yml). Each GPU boots, reads its
profile, and programs: per-EV fwmark-pinned seg6 encap to every peer + its own End.DT6 decap.

## Inspect on a GPU
    docker exec clab-srv6lab-gpu1 python3 /usr/local/bin/mrc-nic status
    docker exec clab-srv6lab-gpu1 ip -6 route show | grep -E 'seg6|encap'
    docker exec clab-srv6lab-gpu1 ip -6 rule show | grep 4900

## Drive traffic (per-EV pinned spray)
    docker exec clab-srv6lab-gpu3 python3 /usr/local/bin/mrc-sink &
    docker exec clab-srv6lab-gpu1 python3 /usr/local/bin/mrc-probe --dst fd00:9:1:201::2 --entropy-ports 4

## IMPORTANT — what this is and isn't
- IS: the virtual NIC running as a standing SR source on the PROVEN fc00:0 switch dataplane.
  Carriers name the real spine End.X -> dst-leaf End.DT6 path, multi-path across spines.
- IS NOT (yet): the full spec relabel to fcbb:bb00 / T0-T1 / NIC-only decap from MRC_SRV6_DESIGN.md.
  That cutover comes after validating the seg6local kernel actions on a 2-node lab, so we don't
  bake an unproven forwarding action into a large fabric. The NIC already installs its own
  End.DT6 decap, so the host-decap half is in place to validate.

## Standalone NIC usage (no controller)
    mrc-nic run --tenant <addr> --underlay eth1 --profile /etc/mrc-nic/profile.json

## IMPORTANT: GPU image needs python3
mrc-nic/probe/sink are Python. The default network-multitool image is Alpine with NO python3.
The generator now installs it at boot (`apk add python3`) before launching mrc-nic — this needs
the GPU netns to reach an Alpine mirror. For LARGE fabrics, bake python3 into a custom gpu_image
instead of paying the per-GPU apk (set gpu_image in group_vars/all.yml).

If a GPU shows `nohup: can't execute 'python3'` in /var/log/mrc-nic.log, python3 didn't install
(no internet from the netns). Manual fix on a running GPU:
    docker exec clab-srv6lab-gpuN apk add --no-cache python3
    docker exec -d clab-srv6lab-gpuN sh -c 'python3 /usr/local/bin/mrc-nic run --underlay eth1 --profile /etc/mrc-nic/profile.json >/var/log/mrc-nic.log 2>&1'
(--tenant is optional now; mrc-nic reads it from the profile's top-level "tenant" field.)

## SPEC MODE (--spec-mode / -e spec_mode=true) — the MRC cutover

The DEFAULT fabric is the proven fc00:0 path where the LEAF decaps (End.DT6 on leaves).
Spec mode is the MRC-faithful rebuild:
  - block fcbb:bb00 /32 (F3216, matches the paper/Cisco)
  - leaves carry NO End.DT6 — the dumb core only uN-forwards + plain-forwards GPU decap SIDs
  - each GPU has a decap uSID (fcbb:bb00:900N::) and decaps it itself (End.DT6 on the NIC)
  - carriers name the full path + the dst GPU: block:<spine>:<spineEndX>:<leaf>:<gpu>::

### Deploy spec mode
    cd ~/clab/srv6 && sudo containerlab destroy -t srv6lab.clab.yml --cleanup; docker network rm srv6lab-mgmt 2>/dev/null
    cd ~/clab/srv6/ansible && ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e spec_mode=true

### Verify (build stamp first!)
    docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic version    # must be 2026-06-25-g8-spec-decap or later
    docker exec clab-srv6lab-gpu1 cat /var/log/mrc-nic.log
      # expect TWO decap lines: tenant fd00:9:1:101::2 AND fcbb:bb00:9001::

### The wire test that matters (does the NIC decap on this kernel?)
    # capture on the DESTINATION gpu3 while pinging from gpu1:
    docker exec -d clab-srv6lab-gpu3 sh -c 'timeout 8 tcpdump -nni eth1 -vv ip6 > /tmp/gpu3.txt 2>&1'
    docker exec clab-srv6lab-gpu1 ping6 -c3 fd00:9:1:201::2
    sleep 2; docker exec clab-srv6lab-gpu3 cat /tmp/gpu3.txt

  - If gpu3 sees the packet arrive WITH an SRH (outer DA fcbb:bb00:...:9003::) and ALSO a
    decapped inner packet delivered -> NIC-side End.DT6 WORKS on this kernel. Spec cutover validated.
  - If gpu3 sees the SRH arrive but NO decapped delivery (ping fails) -> the kernel doesn't
    decap End.DT6 at the NIC. Fall back: -e spec_mode=false (the proven leaf-decap fabric).

### Rollback (instant)
    ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e spec_mode=false

## SPEC MODE REQUIRES A MODERN GPU IMAGE (iproute2 >= 6.1)

NIC-side uSID decap needs iproute2 >= 6.1 (NEXT-C-SID) AND kernel >= 6.1. The default
network-multitool image ships iproute2 5.6 (2020) and will SILENTLY DROP decapped packets.

### Deploy spec mode with a capable image
    cd ~/clab/srv6 && tar xzf mrc-fabric-ansible.tgz && cp nic/mrc-nic mrc-nic/mrc-nic
    cd ~/clab/srv6 && sudo containerlab destroy -t srv6lab.clab.yml --cleanup; docker network rm srv6lab-mgmt 2>/dev/null
    cd ~/clab/srv6/ansible && ansible-playbook site-frr.yml \
        -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e spec_mode=true \
        -e gpu_image=debian:bookworm

### Verify capability (NEW: doctor + startup warning)
    docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic version    # 2026-06-25-g9-capability-doctor
    docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic doctor     # iproute2 ver, seg6, decap routes, verdict
    docker exec clab-srv6lab-gpu1 cat /var/log/mrc-nic.log | grep -i capability

  If doctor reports iproute2 OK and seg6_enabled=1 on eth1, the cross-leaf ping should decap:
    docker exec clab-srv6lab-gpu1 ping6 -c3 fd00:9:1:201::2

### If the image still lacks a new enough iproute2
  Fall back to the proven leaf-decap fabric (works on any image, validated on the wire):
    ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e spec_mode=false
