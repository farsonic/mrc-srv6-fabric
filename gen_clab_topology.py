#!/usr/bin/env python3
"""gen_clab_topology.py — containerlab multiplane spine/leaf fabric + position-encoded addressing.

Model (matches the "Two Plane, 2 Levels" diagram):
  * P fully ISOLATED planes. A plane = its own leaf-spine CLOS (L leaves, S
    spines, full leaf-spine mesh). Planes share NO links whatsoever.
  * Every GPU has a NIC into EVERY leaf — all P*L leaves across all planes.
    GPU NIC eth{(p-1)*L + l} -> leaf l of plane p.
  * Because every GPU sits on every leaf, every plane independently provides
    full GPU<->GPU routing.

Host/switch config is applied later by Ansible from the emitted *_vars.json.

Addressing is POSITION-ENCODED (driven by address_plan.json) so any address
decodes back to its place in the fabric:
  loopback   {base}:{RPII}::h/128          RPII = role*0x1000 + plane*0x100 + index
  p2p /127   {base}:{plane}:{LLSS}::/127   LLSS = leaf*0x100 + spine
  gpu /64    {base}:{plane}:{LLGG}::/64    LLGG = leaf*0x100 + gpu  (so plane+leaf+gpu readable)
  srv6 loc   {block}:{RPII}::/48           + End.DT6 / End.X functions

Outputs (prefix from --out, default 'fabric'):
  <name>.clab.yml          containerlab topology (static mgmt IPs, labels)
  <out>_vars.json          per-node addresses + SRv6 SIDs + static routes (Ansible)
  <out>_address_map.txt    human-readable position -> address listing

Usage:
  ./gen_clab_topology.py --image quay.io/frrouting/frr:10.6.1 --gpus 4 --leaves 2 --spines 2 --planes 2
"""
import argparse
import ipaddress
import json
import os
import sys


def hx(base, *more):
    parts = [p for p in (base.split(":") + [str(m) for m in more]) if p != ""]
    return ":".join(parts)


def comp(addr):
    return str(ipaddress.IPv6Address(addr))


def main():
    ap = argparse.ArgumentParser(
        description="Generate an isolated-plane multiplane spine/leaf containerlab topology "
                    "(every GPU NIC into every leaf) with position-encoded IPv6/SRv6 "
                    "addressing from a JSON plan.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--image", required=True, help="switch docker image (spines+leaves)")
    ap.add_argument("--gpu-image", default="ghcr.io/hellt/network-multitool:latest")
    ap.add_argument("--gpus", type=int, default=4,
                    help="(legacy mesh mode) total GPUs, each NIC'd into EVERY leaf")
    ap.add_argument("--gpus-per-leaf", type=int, default=None,
                    help="HOMED mode: attach K GPUs to a SINGLE leaf each (one NIC per plane into "
                         "their home leaf, rail-aligned). Total GPUs = leaves*K. Cross-leaf GPU "
                         "traffic must transit a spine, so this exercises the spine layer. "
                         "Overrides --gpus.")
    ap.add_argument("--leaves", type=int, default=2, help="leaves per plane")
    ap.add_argument("--spines", type=int, default=2, help="spines per plane")
    ap.add_argument("--planes", type=int, default=2, help="isolated planes")
    ap.add_argument("--kind", default="sonic-vs", help="clab kind for switches")
    ap.add_argument("--gpu-kind", default="linux", help="clab kind for GPUs")
    ap.add_argument("--name", default="clos-fabric", help="lab name")
    ap.add_argument("--plan", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "address_plan.json"))
    ap.add_argument("--out", default="fabric", help="output filename prefix for vars/map")
    ap.add_argument("--mgmt-subnet", default=None,
                    help="override the management subnet from the address plan (e.g. "
                         "172.30.0.0/22) to avoid colliding with other containerlab labs "
                         "already using the planned subnet")
    ap.add_argument("--max-leaf-ports", type=int, default=128)
    ap.add_argument("--mrc-nic", action="store_true",
                    help="emit a per-GPU MRC virtual-NIC profile.json (full EV-set of uSID "
                         "carriers, one per dest GPU per plane) and bind-mount it so each GPU "
                         "runs the standalone mrc-nic as a standing SR source. Additive: the "
                         "switch/forwarding generation is unchanged.")
    ap.add_argument("--spec-mode", action="store_true",
                    help="MRC-FAITHFUL rebuild: fcbb:bb00 /32 block (F3216), roles relabel to "
                         "T0/T1 conceptually, decap moves to the destination NIC (leaves carry NO "
                         "End.DT6; each GPU has a decap uSID, its leaf plain-forwards that uSID to "
                         "the GPU port). Carriers name block:T1:T0:gpu. Implies --mrc-nic. The "
                         "proven fc00:0 leaf-decap path is the DEFAULT; this is the spec cutover.")
    ap.add_argument("--frr-linux", action="store_true",
                    help="leaf/spine = plain Linux + FRR with bind-mounted frr.conf "
                         "(eth1.. naming, kernel seg6 dataplane; no SONiC/vtysh push). "
                         "Use --image frrouting/frr:latest with this.")
    ap.add_argument("--seg6", choices=["kernel", "frr"], default="kernel",
                    help="how to program SRv6 SIDs in --frr-linux mode: 'kernel' = direct "
                         "ip seg6local routes via a bind-mounted seg6.sh (works on ANY FRR, "
                         "incl. 8.4); 'frr' = FRR static-sids in frr.conf (needs FRR >= 9.1).")
    ap.add_argument("--controller", default=None,
                    help="controller/GUI-backend base URL (e.g. http://172.20.20.1:8080). When "
                         "set, each GPU's mrc-nic is launched with --controller/--host so it "
                         "attaches, runs the full-mesh per-path test probe, and POSTs path health "
                         "to the backend's /api/mesh-health (feeds the GUI paths table). Default "
                         "off = standalone NICs, byte-identical to before.")
    a = ap.parse_args()
    if a.frr_linux:
        a.kind = "linux"

    L, S, P = a.leaves, a.spines, a.planes
    for nm, v, lo in [("leaves", L, 1), ("spines", S, 1), ("planes", P, 1)]:
        if v < lo:
            ap.error(f"--{nm} must be >= {lo}")
    homed = a.gpus_per_leaf is not None
    if homed:
        if a.gpus_per_leaf < 1:
            ap.error("--gpus-per-leaf must be >= 1")
        K = a.gpus_per_leaf
        G = L * K
    else:
        G = a.gpus
        if G < 0:
            ap.error("--gpus must be >= 0")
    with open(a.plan) as f:
        plan = json.load(f)
    if a.mgmt_subnet:
        plan.setdefault("mgmt", {})["subnet"] = a.mgmt_subnet

    HP, RID = plan["hostname_prefixes"], plan["role_ids"]
    lp, pp, gp_, sv, mg = plan["loopback"], plan["p2p"], plan["gpu"], plan["srv6"], plan["mgmt"]
    # --spec-mode: MRC-faithful rebuild. Swap the SRv6 section to the fcbb:bb00 spec block,
    # imply --mrc-nic (the NIC is mandatory — it's the only decap point), and flip behaviour
    # flags consumed below (no leaf End.DT6; per-GPU decap SID; block:T1:T0:gpu carriers).
    spec = bool(getattr(a, "spec_mode", False))
    if spec:
        sv = plan["srv6_spec"]
        a.mrc_nic = True
        sv_spec = plan["srv6_spec"]
    multi = P > 1

    def sname(s, p): return f"{HP['spine']}{s}-{HP['plane_tag']}{p}" if multi else f"{HP['spine']}{s}"
    def lname(l, p): return f"{HP['leaf']}{l}-{HP['plane_tag']}{p}" if multi else f"{HP['leaf']}{l}"
    def gname(g):    return f"{HP['gpu']}{g}"
    def rpii(role, plane, index): return f"{RID[role]:x}{plane:x}{index:02x}"

    # GPU NIC index for (plane p, leaf l): planes-major, 1-based -> eth{(p-1)*L + l}
    def gpu_nic(p, l): return (p - 1) * L + l
    # leaf host-facing port for GPU g: after the S spine uplinks
    def leaf_host_port(g): return S + g

    # ---- position-encoded address builders ----
    def loopback(role, plane, index): return comp(hx(lp["base"], rpii(role, plane, index)) + f"::{lp['host']:x}")
    def srv6_loc(role, plane, index): return hx(sv["block"], rpii(role, plane, index)) + "::"
    def srv6_endx(loc, n): return comp(loc.rstrip(":") + f":{int(sv['func_endx_base'],16)+n:x}::")
    def srv6_dt6(loc):     return comp(loc.rstrip(":") + f":{sv['func_end_dt6']}::")
    def p2p_net(p, l, s):  return hx(pp["base"], str(p), f"{l:02x}{s:02x}")
    def p2p_leaf(p, l, s): return comp(p2p_net(p, l, s) + f"::{pp['leaf_host']:x}")
    def p2p_spine(p, l, s):return comp(p2p_net(p, l, s) + f"::{pp['spine_host']:x}")
    def gpu_net(p, l, g):  return hx(gp_["base"], str(p), f"{l:02x}{g:02x}")     # plane:leaf:gpu
    def gpu_addr(p, l, g): return comp(gpu_net(p, l, g) + f"::{gp_['gpu_host']:x}")
    def gpu_gw(p, l, g):   return comp(gpu_net(p, l, g) + f"::{gp_['leaf_host']:x}")
    gpu_base_hextets = gp_["base"].count(":") + 1
    def gpu_plane_agg(p):  return f"{hx(gp_['base'], str(p))}::/{(gpu_base_hextets + 1) * 16}"

    # ---- homed-mode GPU helpers: GPU g lives on one leaf; plane:homeleaf:localidx ----
    def home_leaf(g):     return (g - 1) // K + 1
    def local_idx(g):     return (g - 1) % K + 1
    def gpu_net_h(p, g):  return hx(gp_["base"], str(p), f"{home_leaf(g):02x}{local_idx(g):02x}")
    def gpu_addr_h(p, g): return comp(gpu_net_h(p, g) + f"::{gp_['gpu_host']:x}")
    def gpu_gw_h(p, g):   return comp(gpu_net_h(p, g) + f"::{gp_['leaf_host']:x}")
    # all of leaf l's GPU /64s in plane p collapse to one /56 (high byte = leaf, low byte = gpu)
    def leaf_gpu_agg(p, l): return f"{hx(gp_['base'], str(p), f'{l:02x}00')}::/56"

    # ---- spec-mode: per-GPU decap uSID + decap SID (NIC owns decap) ----
    # uSID layout (position-encoded for clean aggregation as the fabric grows):
    #   3rd hextet = 0x9LLL   (nibble 9 marks the GPU block; LLL = home-leaf index, up to 4095)
    #   4th hextet = 0xGGGG   (local gpu index on that leaf, up to 65535)
    #   -> decap SID = block:9LLL:GGGG::/64 ; e.g. leaf1 gpu1 = fcbb:bb00:9001:0001::
    # This nests cleanly so both leaf and spine aggregate, invariant to GPU count:
    #   - a SPINE needs ONE route per leaf:  fcbb:bb00:9LLL::/48 -> that leaf   (O(leaves))
    #   - a LEAF needs ONE aggregate:         fcbb:bb00:9000::/36 -> spines      (O(1))
    #     plus a specific /64 per LOCAL gpu -> its access port                  (O(local gpus))
    # Caps: 4095 leaves x 65535 gpus/leaf — far beyond any real fabric.
    GPU_BLOCK_NIBBLE = 0x9
    def gpu_usid_parts(g):
        l = home_leaf(g)                 # 1-based leaf
        gg = (g - 1) % K + 1 if homed else g   # 1-based local index
        leaf_hextet = (GPU_BLOCK_NIBBLE << 12) | (l & 0x0fff)   # 9LLL
        return leaf_hextet, (gg & 0xffff)
    def gpu_decap_sid(g):
        lh, gg = gpu_usid_parts(g)
        return comp(f"{sv['block']}:{lh:x}:{gg:x}::")
    def gpu_decap_prefixlen():
        return 64
    def leaf_gpu_block(l):
        # per-leaf GPU uSID aggregate: fcbb:bb00:9LLL::/48
        lh = (GPU_BLOCK_NIBBLE << 12) | (l & 0x0fff)
        return f"{sv['block']}:{lh:x}::/48"
    def gpu_block_all():
        # whole GPU uSID space across all leaves: fcbb:bb00:9000::/36
        return f"{sv['block']}:{(GPU_BLOCK_NIBBLE << 12):x}::/36"

    # ---- MRC virtual-NIC carrier builder (F3216 uSID, fcbb:bb00 block via the plan) ----
    # A carrier names the full transit: <block> <spine End.X toward dst-leaf> <dst-leaf End.DT6>.
    # Both micro-SIDs are the SAME SIDs the switch dataplane programs, so host-source encap and
    # the dumb-core forwarding are guaranteed consistent. entropy = a unique per-(src,dst,plane,spine)
    # value in the 49000+ space so mrc-probe's SO_MARK pins each EV to its physical path.
    def _csid_pair(sid):
        # the two 16-bit hextets after the 32-bit block (positions 2,3 of the exploded addr)
        return ipaddress.IPv6Address(sid).exploded.split(":")[2:4]

    def mrc_carrier(p, dst_leaf_name, spine_name):
        """uSID carrier from any GPU to a GPU homed on dst_leaf, transiting spine (plane p)."""
        spine_endx = nodes[spine_name]["srv6"]["endx"][dst_leaf_name]["sid"]  # spine End.X -> dst leaf
        dst_dt6 = nodes[dst_leaf_name]["srv6"]["end_dt6"]                     # dst leaf End.DT6 decap
        block = ipaddress.IPv6Address(srv6_loc("leaf", p, 1)).exploded.split(":")[0:2]
        full = ":".join(block + _csid_pair(spine_endx) + _csid_pair(dst_dt6) + ["0", "0"])
        return ipaddress.IPv6Address(full).compressed

    def mrc_carrier_spec(p, dst_leaf_name, spine_name, dst_gpu):
        """SPEC-mode carrier (NIC decaps; dst leaf does PLAIN forward, no shift).

        Wire-validated structure: the spine's End.X next-csid shift removes the spine's
        WHOLE locator+func pair (e.g. 1102:e002), so we do NOT name the dst leaf as a uN
        hop. If we did, the leaf would have to uN-shift-then-plain-forward, which this 6.8
        kernel DROPS (confirmed: leaf Ip6InReceives>0 but Ip6OutForwDatagrams=0). Instead:

          carrier = block : <spine loc uSID> : <spine End.X uSID> : <gpu leaf-hextet> : <gpu index> : 0

        Hop trace (validated against the spine IN/OUT capture):
          src leaf : plain-fwd block:<spine>::/48 -> spine (unchanged)
          spine    : End.X (matches /64 locator+func) shifts BOTH out -> DA = block:9LLL:GGGG::
          dst leaf : plain-fwd block:9LLL:GGGG::/64 -> GPU port (NO seg6local, UNCHANGED)
          dst GPU  : DA = block:9LLL:GGGG:: = its own decap SID -> terminal End.DT6 -> inner

        Leaf stays a pure IPv6 forwarder; ALL SRv6 endpoint processing — including the
        terminal decap — lives in the virtual NIC. Same-leaf peers (no spine) name the gpu
        uSID directly (handled in the profile emitter)."""
        spine_endx = nodes[spine_name]["srv6"]["endx"][dst_leaf_name]["sid"]  # spine End.X -> dst leaf
        spine_usids = _csid_pair(spine_endx)                  # [spine-locator uSID, End.X uSID]
        lh, gg = gpu_usid_parts(dst_gpu)                      # GPU uSID = 9LLL:GGGG (two hextets)
        block = ipaddress.IPv6Address(srv6_loc("leaf", p, 1)).exploded.split(":")[0:2]
        full = ":".join(block + spine_usids + [f"{lh:x}", f"{gg:x}", "0", "0"])
        return ipaddress.IPv6Address(full).compressed

    def mgmt_ip(role, plane, index):
        net = ipaddress.ip_network(mg["subnet"], strict=False)
        host = mg["role_base"][role] + (plane - 1) * mg["per_plane_stride"][role] + index
        return str(net.network_address + host)

    # ================= nodes =================
    nodes = {}
    links = []

    def add_node(name, role, plane, index, kind, image):
        rec = dict(role=role, plane=plane, index=index, kind=kind, image=image,
                   mgmt=mgmt_ip(role, plane if plane else 1, index), interfaces=[], routes=[])
        if role in ("spine", "leaf"):
            loc = srv6_loc(role, plane, index)
            rec["loopback"] = loopback(role, plane, index)
            rec["srv6"] = {"locator": f"{loc}/{sv['locator_prefixlen']}", "uN_end": loc,
                           "endx": {}, "end_dt6": None}
        nodes[name] = rec

    for p in range(1, P + 1):
        for s in range(1, S + 1):
            add_node(sname(s, p), "spine", p, s, a.kind, a.image)
        for l in range(1, L + 1):
            add_node(lname(l, p), "leaf", p, l, a.kind, a.image)
    for g in range(1, G + 1):
        add_node(gname(g), "gpu", 0, g, a.gpu_kind, a.gpu_image)

    # mgmt sanity: unique + inside subnet + not the .1 bridge gateway
    mnet = ipaddress.ip_network(mg["subnet"], strict=False)
    gateway = mnet.network_address + 1
    seen_mgmt = {}
    for name, r in nodes.items():
        ip = ipaddress.ip_address(r["mgmt"])
        if ip == gateway:
            ap.error(f"mgmt {ip} for {name} is the .1 bridge gateway clab assigns to the mgmt "
                     f"network — raise the role_base in the plan so no node lands on it.")
        if ip not in mnet or ip in (mnet.network_address, mnet.broadcast_address):
            ap.error(f"mgmt {ip} for {name} is outside usable {mg['subnet']} — widen mgmt.subnet "
                     f"or lower role_base/stride in the plan.")
        if r["mgmt"] in seen_mgmt:
            ap.error(f"mgmt collision: {name} and {seen_mgmt[r['mgmt']]} both get {r['mgmt']} — "
                     f"mgmt blocks overlap at this scale; widen the subnet or spacing.")
        seen_mgmt[r["mgmt"]] = name

    # ================= leaf<->spine links (per plane, isolated) =================
    for p in range(1, P + 1):
        for l in range(1, L + 1):
            for s in range(1, S + 1):
                ln, sn = lname(l, p), sname(s, p)
                links.append((f"{ln}:eth{s}", f"{sn}:eth{l}"))
                la, sa = p2p_leaf(p, l, s), p2p_spine(p, l, s)
                net = f"{p2p_net(p, l, s)}::/{pp['prefixlen']}"
                nodes[ln]["interfaces"].append(dict(name=f"eth{s}", peer=sn, role="fabric",
                                                    ipv6=f"{la}/{pp['prefixlen']}", subnet=net))
                nodes[sn]["interfaces"].append(dict(name=f"eth{l}", peer=ln, role="fabric",
                                                    ipv6=f"{sa}/{pp['prefixlen']}", subnet=net))
                nodes[ln]["srv6"]["endx"][sn] = dict(sid=srv6_endx(nodes[ln]["srv6"]["uN_end"], s),
                                                     via=sa, dev=f"eth{s}")
                nodes[sn]["srv6"]["endx"][ln] = dict(sid=srv6_endx(nodes[sn]["srv6"]["uN_end"], l),
                                                     via=la, dev=f"eth{l}")

    # ================= gpu<->leaf links =================
    if homed:
        # HOMED: each GPU attaches to ONE leaf (its home leaf), one NIC per plane
        # (rail-aligned: same leaf index across planes). Cross-leaf GPU traffic
        # must cross a spine.
        for g in range(1, G + 1):
            gn, l = gname(g), home_leaf(g)
            for p in range(1, P + 1):
                ln = lname(l, p)
                gport, lport = f"eth{p}", f"eth{S + local_idx(g)}"
                links.append((f"{gn}:{gport}", f"{ln}:{lport}"))
                ga, gw = gpu_addr_h(p, g), gpu_gw_h(p, g)
                net = f"{gpu_net_h(p, g)}::/{gp_['prefixlen']}"
                nodes[gn]["interfaces"].append(dict(name=gport, peer=ln, role="host", plane=p, leaf=l,
                                                    ipv6=f"{ga}/{gp_['prefixlen']}", gateway=gw, subnet=net))
                nodes[ln]["interfaces"].append(dict(name=lport, peer=gn, role="host",
                                                    ipv6=f"{gw}/{gp_['prefixlen']}", subnet=net))
        for p in range(1, P + 1):
            for l in range(1, L + 1):
                ln = lname(l, p)
                # spec-mode: leaves carry NO End.DT6 — decap is the NIC's job.
                if not spec and nodes[ln]["srv6"]["end_dt6"] is None:
                    nodes[ln]["srv6"]["end_dt6"] = srv6_dt6(nodes[ln]["srv6"]["uN_end"])
    else:
        # MESH (legacy): EVERY gpu into EVERY leaf
        for p in range(1, P + 1):
            for l in range(1, L + 1):
                ln = lname(l, p)
                for g in range(1, G + 1):
                    gn = gname(g)
                    gport, lport = f"eth{gpu_nic(p, l)}", f"eth{leaf_host_port(g)}"
                    links.append((f"{gn}:{gport}", f"{ln}:{lport}"))
                    ga, gw = gpu_addr(p, l, g), gpu_gw(p, l, g)
                    net = f"{gpu_net(p, l, g)}::/{gp_['prefixlen']}"
                    nodes[gn]["interfaces"].append(dict(name=gport, peer=ln, role="host", plane=p, leaf=l,
                                                        ipv6=f"{ga}/{gp_['prefixlen']}", gateway=gw, subnet=net))
                    nodes[ln]["interfaces"].append(dict(name=lport, peer=gn, role="host",
                                                        ipv6=f"{gw}/{gp_['prefixlen']}", subnet=net))
                if not spec and nodes[ln]["srv6"]["end_dt6"] is None:
                    nodes[ln]["srv6"]["end_dt6"] = srv6_dt6(nodes[ln]["srv6"]["uN_end"])

    # ================= static underlay routes (per plane, isolated) =================
    def add_route(node, dest, nh, comment):
        nodes[node]["routes"].append(dict(dest=dest, nexthops=list(nh), comment=comment))

    for p in range(1, P + 1):
        for l in range(1, L + 1):
            ln = lname(l, p)
            for s in range(1, S + 1):
                sn, nh = sname(s, p), p2p_spine(p, l, s)
                add_route(ln, nodes[sn]["srv6"]["locator"], [nh], f"-> {sn} locator")
                add_route(ln, f"{nodes[sn]['loopback']}/128", [nh], f"-> {sn} loopback")
            ecmp = [p2p_spine(p, l, s) for s in range(1, S + 1)]
            for l2 in range(1, L + 1):
                if l2 == l:
                    continue
                rn = lname(l2, p)
                add_route(ln, nodes[rn]["srv6"]["locator"], ecmp, f"-> {rn} locator (ECMP)")
                add_route(ln, f"{nodes[rn]['loopback']}/128", ecmp, f"-> {rn} loopback (ECMP)")
                if homed:
                    add_route(ln, leaf_gpu_agg(p, l2), ecmp, f"-> {rn} GPUs via spine (ECMP)")
                else:
                    for g in range(1, G + 1):
                        add_route(ln, f"{gpu_net(p, l2, g)}::/{gp_['prefixlen']}", ecmp,
                                  f"-> {gname(g)} via {rn} (ECMP)")
        for s in range(1, S + 1):
            sn = sname(s, p)
            for l in range(1, L + 1):
                ln, nh = lname(l, p), p2p_leaf(p, l, s)
                add_route(sn, nodes[ln]["srv6"]["locator"], [nh], f"-> {ln} locator")
                add_route(sn, f"{nodes[ln]['loopback']}/128", [nh], f"-> {ln} loopback")
                if homed:
                    add_route(sn, leaf_gpu_agg(p, l), [nh], f"-> {ln} GPUs")
                else:
                    for g in range(1, G + 1):
                        add_route(sn, f"{gpu_net(p, l, g)}::/{gp_['prefixlen']}", [nh],
                                  f"-> {gname(g)} via {ln}")
    # gpu routes: reach the rest of each plane's GPU space via the leaf gateway(s)
    for g in range(1, G + 1):
        for p in range(1, P + 1):
            if homed:
                gw = gpu_gw_h(p, g)
                add_route(gname(g), gpu_plane_agg(p), [gw],
                          f"plane {p}: GPU<->GPU via home leaf {home_leaf(g)} (own /64 connected)")
            else:
                gws = [gpu_gw(p, l, g) for l in range(1, L + 1)]
                add_route(gname(g), gpu_plane_agg(p), gws,
                          f"plane {p}: GPU<->GPU via {L} leaf gw(s) (local /64s stay connected)")

    # ---- spec-mode: route GPU decap uSIDs, AGGREGATED so config is invariant to GPU count ----
    # uSID layout 9LLL:GGGG (leaf-nested) lets us aggregate at every tier:
    #   - home leaf : SPECIFIC /64 per LOCAL gpu -> its access port (LPM beats the aggregate).
    #   - spine     : ONE /48 per leaf  (9LLL::/48 -> that leaf).            O(leaves), not O(gpus)
    #   - other leaf: ONE /36 aggregate (9000::/36 -> spines, ECMP).         O(1), not O(remote gpus)
    # A remote-GPU packet on a leaf hits the /36 -> spine; the spine's /48 sends it to the home
    # leaf; the home leaf's specific /64 delivers it to the GPU port. The GPU End.DT6-decaps.
    if spec and homed:
        for p in range(1, P + 1):
            # home leaf: one specific /64 per local GPU -> access port
            for g in range(1, G + 1):
                hl = home_leaf(g)
                sid = f"{gpu_decap_sid(g)}/{gpu_decap_prefixlen()}"
                add_route(lname(hl, p), sid, [gpu_addr_h(p, g)],
                          f"-> {gname(g)} decap uSID out access port (NIC decaps)")
            # spine: one /48 per leaf -> that leaf (O(leaves)); other leaf: one /36 -> spines (O(1))
            for l in range(1, L + 1):
                blk = leaf_gpu_block(l)
                for s in range(1, S + 1):
                    add_route(sname(s, p), blk, [p2p_leaf(p, l, s)],
                              f"-> {lname(l,p)} GPU block (aggregate, all its GPUs)")
            for l_other in range(1, L + 1):
                ecmp_up = [p2p_spine(p, l_other, s) for s in range(1, S + 1)]
                add_route(lname(l_other, p), gpu_block_all(), ecmp_up,
                          f"-> all remote GPUs via spines (aggregate /36, ECMP)")

    # ================= emit per-node FRR configs (bind-mount mode) =================
    if a.frr_linux:
        frr_dir = "frr"
        os.makedirs(frr_dir, exist_ok=True)
        daemons = ["zebra=yes", "bgpd=no", "ospfd=no", "ospf6d=no", "ripd=no", "ripngd=no",
                   "isisd=no", "pimd=no", "pim6d=no", "ldpd=no", "nhrpd=no", "eigrpd=no",
                   "babeld=no", "sharpd=no", "pbrd=no", "bfdd=no", "fabricd=no", "vrrpd=no",
                   "pathd=no", "staticd=yes", "",
                   "vtysh_enable=yes",
                   'zebra_options="  -A 127.0.0.1 -s 90000000"',
                   'mgmtd_options="  -A 127.0.0.1"',
                   'staticd_options="-A 127.0.0.1"', ""]
        with open(os.path.join(frr_dir, "daemons"), "w") as f:
            f.write("\n".join(daemons) + "\n")

        def build_frr_conf(name, r):
            o = ["frr defaults datacenter", f"hostname {name}",
                 "log syslog informational", "service integrated-vtysh-config", "!",
                 "interface lo", f" ipv6 address {r['loopback']}/128", "exit", "!"]
            for i in r["interfaces"]:
                o += [f"interface {i['name']}", f" description to-{i['peer']}",
                      f" ipv6 address {i['ipv6']}", " no shutdown", "exit", "!"]
            for rt in r["routes"]:
                for nh in rt["nexthops"]:
                    o.append(f"ipv6 route {rt['dest']} {nh}")
            o.append("!")
            # FRR static-sids only when asked (needs FRR >= 9.1). In kernel mode the
            # SIDs are programmed by seg6.sh instead, so this block is omitted to avoid
            # a double-install on newer FRR and silent-drop noise on 8.4.
            if a.seg6 == "frr":
                s6 = r["srv6"]
                o += ["segment-routing", " srv6", "  locators", "   locator MAIN",
                      f"    prefix {s6['locator']} block-len 32 node-len 16 func-bits 16",
                      "   exit", "  exit", "  static-sids",
                      f"   sid {s6['locator']} locator MAIN behavior uN"]
                for _peer, x in s6["endx"].items():
                    o.append(f"   sid {x['sid']}/64 locator MAIN behavior uA "
                             f"interface {x['dev']} nexthop {x['via']}")
                if s6["end_dt6"]:
                    o.append(f"   sid {s6['end_dt6']}/64 locator MAIN behavior uDT6 vrf default")
                o += ["  exit", " exit", "exit", "!"]
            return "\n".join(o) + "\n"

        FLV = "flavors next-csid lblen 32 nflen 32"

        def build_seg6_sh(name, r):
            # Full kernel seg6local programming (--seg6 kernel): End + End.X + End.DT6,
            # all with the next-csid uSID flavor so the DA-shift matches FRR's behaviour.
            s6 = r["srv6"]
            fabric_ifs = [i["name"] for i in r["interfaces"] if i.get("role") == "fabric"]
            up = fabric_ifs[0] if fabric_ifs else "lo"
            o = ["#!/bin/sh",
                 f"# SRv6 uSID seg6local endpoints for {name} (kernel dataplane; any FRR version).",
                 "# idempotent: 'route replace' so re-running is safe.",
                 f"ip -6 route replace {s6['locator']} encap seg6local action End {FLV} dev {up}"]
            for _peer, x in s6["endx"].items():
                o.append(f"ip -6 route replace {x['sid']}/64 encap seg6local action End.X "
                         f"nh6 {x['via']} {FLV} dev {x['dev']}")
            if s6["end_dt6"]:
                o.append(f"ip -6 route replace {s6['end_dt6']}/64 encap seg6local "
                         f"action End.DT6 table 254 dev {up}")
            return "\n".join(o) + "\n"

        def build_seg6_backfill(name, r):
            # --seg6 frr: FRR 10.6.1 programs End.X to the kernel but NOT uN (End) or
            # uDT6 (End.DT6) — they sit in FRR's SID table but never reach the FIB. This
            # backfills exactly those two (with matching next-csid flavor) so the uSID
            # path forwards end-to-end. End.X is left to FRR.
            s6 = r["srv6"]
            fabric_ifs = [i["name"] for i in r["interfaces"] if i.get("role") == "fabric"]
            up = fabric_ifs[0] if fabric_ifs else "lo"
            o = ["#!/bin/sh",
                 f"# uN + uDT6 backfill for {name} (FRR installs End.X; these it drops).",
                 f"ip -6 route replace {s6['locator']} encap seg6local action End {FLV} dev {up}"]
            if s6["end_dt6"]:
                o.append(f"ip -6 route replace {s6['end_dt6']}/64 encap seg6local "
                         f"action End.DT6 table 254 dev {up}")
            return "\n".join(o) + "\n"

        for nm, r in nodes.items():
            if r["role"] in ("spine", "leaf"):
                d = os.path.join(frr_dir, nm)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "frr.conf"), "w") as f:
                    f.write(build_frr_conf(nm, r))
                if a.seg6 == "kernel":
                    with open(os.path.join(d, "seg6.sh"), "w") as f:
                        f.write(build_seg6_sh(nm, r))
                else:  # frr: backfill uN + uDT6 that FRR drops
                    with open(os.path.join(d, "seg6.sh"), "w") as f:
                        f.write(build_seg6_backfill(nm, r))

        # ---- emit srv6-test.sh: uSID DA-shift proof over a spine (homed only) ----
        if homed and L >= 2 and not spec:
            pt = 1
            sl, dl, sp = lname(1, pt), lname(L, pt), sname(1, pt)
            src_g, dst_g = 1, (L - 1) * K + 1
            spine_endx = nodes[sp]["srv6"]["endx"][dl]["sid"]   # spine End.X toward dst leaf
            dst_dt6 = nodes[dl]["srv6"]["end_dt6"]              # dst leaf End.DT6 (decap)

            def _csid(sid): return ipaddress.IPv6Address(sid).exploded.split(":")[2:4]
            block = ipaddress.IPv6Address(srv6_loc("leaf", pt, 1)).exploded.split(":")[0:2]
            carrier = ipaddress.IPv6Address(
                ":".join(block + _csid(spine_endx) + _csid(dst_dt6) + ["0", "0"])).compressed
            # src-leaf uplink toward the spine; spine iface toward dst leaf
            src_dev = next(i["name"] for i in nodes[sl]["interfaces"]
                           if i.get("role") == "fabric" and i["peer"] == sp)
            spine_cap = next(i["name"] for i in nodes[sp]["interfaces"]
                             if i.get("role") == "fabric" and i["peer"] == dl)
            dst_pfx = f"{gpu_net_h(pt, dst_g)}::/{gp_['prefixlen']}"
            dst_addr = gpu_addr_h(pt, dst_g)

            t = [
                "#!/usr/bin/env bash",
                f"# SRv6 uSID DA-shift proof for {a.name}: gpu{src_g}({sl}) -> gpu{dst_g}({dl}) via {sp}.",
                f"# uSID carrier {carrier} = block + [{':'.join(_csid(spine_endx))} {sp} End.X->{dl}]"
                f" + [{':'.join(_csid(dst_dt6))} {dl} End.DT6].",
                "N=" + a.name,
                f"SPINE=clab-$N-{sp}; SRCLEAF=clab-$N-{sl}; DSTLEAF=clab-$N-{dl}",
                f"SRCGPU=clab-$N-gpu{src_g}; DSTADDR={dst_addr}",
                f"DSTPFX={dst_pfx}; CARRIER={carrier}",
                f"SRCDEV={src_dev}; SPINECAP={spine_cap}",
                f"DSTDT6={dst_dt6}/64",
                "",
                'echo ">> 1. install tcpdump on $SPINE (if missing)"',
                "docker exec $SPINE sh -c 'command -v tcpdump >/dev/null 2>&1 || "
                "apk add --no-cache tcpdump >/dev/null 2>&1 || "
                "(apt-get update -qq && apt-get install -y -qq tcpdump >/dev/null 2>&1)' || true",
                "",
                'echo ">> 2. encap $DSTPFX into the uSID carrier on $SRCLEAF"',
                "docker exec $SRCLEAF ip -6 route replace $DSTPFX encap seg6 mode encap "
                "segs $CARRIER dev $SRCDEV",
                'echo "   route get (must show encap seg6):"',
                "docker exec $SRCLEAF ip -6 route get $DSTADDR | sed \"s/^/   /\"",
                'echo "   carrier first-uSID resolves toward the spine:"',
                "docker exec $SRCLEAF ip -6 route get $CARRIER | sed \"s/^/   /\"",
                "",
                'echo ">> 3. start SRH capture on $SPINE $SPINECAP (interface first, then filter)"',
                "docker exec -d $SPINE sh -c \"timeout 8 tcpdump -nni $SPINECAP "
                "'ip6 and ip6[6]==43' > /tmp/srh.txt 2>&1\"",
                "sleep 1",
                'echo ">> 4. ping (encapsulated) $SRCGPU -> $DSTADDR"',
                "docker exec $SRCGPU ping6 -c5 $DSTADDR || true",
                "sleep 1",
                'echo ">> 5. SRH packets captured on spine egress (the DA-shift, on the wire):"',
                "docker exec $SPINE sh -c 'grep -E \"RT6|srcrt|routing\" /tmp/srh.txt 2>/dev/null | sed \"s/^/   /\"'",
                "SRH=$(docker exec $SPINE sh -c 'grep -cE \"RT6|srcrt|routing\" /tmp/srh.txt 2>/dev/null' "
                "| tr -dc 0-9); SRH=${SRH:-0}",
                'echo "   SRH packet count: $SRH"',
                "",
                'echo ">> 6. cleanup: remove encap (restore plain forwarding)"',
                "docker exec $SRCLEAF ip -6 route del $DSTPFX encap seg6 mode encap "
                "segs $CARRIER dev $SRCDEV 2>/dev/null || true",
                "if [ \"$SRH\" -gt 0 ]; then "
                "echo \">> PASS: spine forwarded $SRH SRH packet(s) with the DA shifted to the next uSID.\"; "
                "else echo \">> CHECK: no SRH seen on $SPINE $SPINECAP — verify encap (route get above) and seg6.\"; fi",
                "",
            ]
            with open("srv6-test.sh", "w") as f:
                f.write("\n".join(t))
            os.chmod("srv6-test.sh", 0o755)

            # ---- srv6-walk.sh: capture at EVERY hop and show the DA mutate per step ----
            dst_gpu_port = f"eth{S + local_idx(dst_g)}"   # dst leaf's port toward dst gpu
            shifted_da = dst_dt6                          # after spine End.X shift, DA = dst-leaf DT6
            endx_csid = ":".join(_csid(spine_endx))
            dt6_csid = ":".join(_csid(dst_dt6))
            w = [
                "#!/usr/bin/env bash",
                f"# SRv6 uSID per-hop walkthrough for {a.name}:",
                f"#   gpu{src_g}({sl}) --encap--> {sp} --End.X DA-shift--> {dl} --End.DT6 decap--> gpu{dst_g}",
                f"# carrier {carrier}  =  [{endx_csid} {sp} End.X->{dl}] [{dt6_csid} {dl} End.DT6]",
                "set -u",
                "N=" + a.name,
                f"SRCLEAF=clab-$N-{sl}; SPINE=clab-$N-{sp}; DSTLEAF=clab-$N-{dl}",
                f"SRCGPU=clab-$N-gpu{src_g}; DSTADDR={dst_addr}; DSTPFX={dst_pfx}",
                f"CARRIER={carrier}; SHIFTED={shifted_da}",
                f"SRCDEV={src_dev}",                       # src-leaf egress toward spine
                f"SPINE_IN={nodes[sp]['interfaces'][[i['peer'] for i in nodes[sp]['interfaces']].index(sl)]['name']}",
                f"SPINE_OUT={spine_cap}",                  # spine egress toward dst leaf
                f"DSTGPU_PORT={dst_gpu_port}",             # dst-leaf egress toward dst gpu
                "",
                "cap(){ docker exec -d $1 sh -c \"timeout 7 tcpdump -nni $2 -vv ip6 "
                "> /tmp/walk_$3.txt 2>&1\"; }",
                "show(){ docker exec $1 sh -c \"grep -m1 'echo request' /tmp/walk_$2.txt 2>/dev/null\" "
                "| sed 's/^[ \\t]*//'; }",
                "ensure_tcpdump(){ docker exec $1 sh -c 'command -v tcpdump >/dev/null 2>&1 || "
                "apk add --no-cache tcpdump >/dev/null 2>&1 || "
                "(apt-get update -qq && apt-get install -y -qq tcpdump >/dev/null 2>&1)'; }",
                "",
                'echo \"========================================================================\"',
                'echo \" SRv6 uSID walkthrough: gpu' + str(src_g) + ' -> gpu' + str(dst_g) + '  (carrier $CARRIER)\"',
                'echo \"========================================================================\"',
                "",
                'echo \">> ensuring tcpdump on the 3 hop nodes (first run may take a moment)...\"',
                "ensure_tcpdump $SRCLEAF; ensure_tcpdump $SPINE; ensure_tcpdump $DSTLEAF",
                'echo \">> arming encap on $SRCLEAF and starting per-hop captures...\"',
                "docker exec $SRCLEAF ip -6 route replace $DSTPFX encap seg6 mode encap "
                "segs $CARRIER dev $SRCDEV",
                "cap $SRCLEAF $SRCDEV   srcleaf_out",
                "cap $SPINE   $SPINE_IN  spine_in",
                "cap $SPINE   $SPINE_OUT spine_out",
                "cap $DSTLEAF $DSTGPU_PORT dstleaf_out",
                "sleep 2",
                'echo \">> sending 3 pings gpu' + str(src_g) + ' -> $DSTADDR\"',
                "docker exec $SRCGPU ping6 -c3 $DSTADDR >/dev/null 2>&1 || true",
                "sleep 2",
                "",
                'echo; echo \"STEP 1  $SRCLEAF egress $SRCDEV  (just encapsulated, heading to spine)\"',
                'echo \"        expect outer DA = CARRIER, active uSID = ' + endx_csid + ' (=' + sp + ' End.X)\"',
                'echo -n \"   \"; show $SRCLEAF srcleaf_out',
                "",
                'echo; echo \"STEP 2  $SPINE ingress $SPINE_IN  (arriving at spine, pre-shift)\"',
                'echo \"        outer DA still = CARRIER ($CARRIER)\"',
                'echo -n \"   \"; show $SPINE spine_in',
                "",
                'echo; echo \"STEP 3  $SPINE egress $SPINE_OUT  (AFTER End.X DA-shift, heading to dst leaf)\"',
                'echo \"        expect outer DA = $SHIFTED  --  ' + endx_csid + ' consumed, now active uSID = ' + dt6_csid + ' (=' + dl + ' End.DT6)\"',
                'echo -n \"   \"; show $SPINE spine_out',
                "",
                'echo; echo \"STEP 4  $DSTLEAF egress $DSTGPU_PORT  (AFTER End.DT6 decap, delivered to gpu)\"',
                'echo \"        expect NO SRH: plain inner packet, DA = $DSTADDR\"',
                'echo -n \"   \"; show $DSTLEAF dstleaf_out',
                "",
                'echo; echo \"------------------------------------------------------------------------\"',
                'echo \" DA progression:  CARRIER ($CARRIER)\"',
                'echo \"          -> spine End.X shift -> $SHIFTED\"',
                'echo \"          -> leaf End.DT6 decap -> inner $DSTADDR (no SRH)\"',
                'echo \"------------------------------------------------------------------------\"',
                "",
                'echo \">> cleanup\"',
                "docker exec $SRCLEAF ip -6 route del $DSTPFX encap seg6 mode encap "
                "segs $CARRIER dev $SRCDEV 2>/dev/null || true",
                "",
            ]
            with open("srv6-walk.sh", "w") as f:
                f.write("\n".join(w))
            os.chmod("srv6-walk.sh", 0o755)

    # ================= emit per-GPU MRC virtual-NIC profiles =================
    # Additive: only when --mrc-nic. Requires homed mode (cross-leaf transits a spine, which is
    # what makes a source-routed carrier meaningful). Each GPU gets profile.json = the full EV-set
    # of uSID carriers to every OTHER GPU, one EV per plane per spine path. mrc-nic reads this and
    # programs per-EV fwmark-pinned seg6 encap + its own End.DT6 decap.
    if a.mrc_nic:
        if not homed:
            ap.error("--mrc-nic requires --gpus-per-leaf (homed mode): a source route only has "
                     "meaning when cross-leaf traffic transits a spine.")
        nic_dir = "mrc-nic"
        os.makedirs(nic_dir, exist_ok=True)
        ev_base = 49001
        nic_profiles = {}
        for sg in range(1, G + 1):
            sgn = gname(sg)
            sl = home_leaf(sg)
            # this GPU's tenant address = its plane-1 GPU address (stable identity for decap)
            tenant = gpu_addr_h(1, sg)
            profiles = []
            ev = ev_base   # monotonic across ALL peers in this GPU's profile: each EV needs a
                           # globally-unique fwmark/table or programming peer N+1 wipes peer N's rules.
            for dg in range(1, G + 1):
                if dg == sg:
                    continue
                dgn = gname(dg)
                dl = home_leaf(dg)
                dst_addr = gpu_addr_h(1, dg)
                evs = []
                for p in range(1, P + 1):
                    if dl == sl:
                        # same-leaf peer: no spine transit.
                        if spec:
                            # spec: name the dst GPU's decap uSID directly. dst leaf plain-forwards
                            # block:gpu_usid::/48 to the GPU port; the GPU End.DT6-decaps.
                            carrier = gpu_decap_sid(dg)
                            evs.append(dict(entropy=ev, usid=carrier, plane=p, spine=None,
                                            path=f"{sgn}->{dgn} same-leaf -> {dgn} decap uSID"))
                        else:
                            # proven: deliver via the dst-leaf End.DT6 (leaf decaps).
                            dst_dt6 = nodes[lname(dl, p)]["srv6"]["end_dt6"]
                            block = ipaddress.IPv6Address(srv6_loc("leaf", p, 1)).exploded.split(":")[0:2]
                            carrier = ipaddress.IPv6Address(
                                ":".join(block + _csid_pair(dst_dt6) + ["0", "0", "0", "0"])).compressed
                            evs.append(dict(entropy=ev, usid=carrier, plane=p, spine=None,
                                            path=f"{sgn}->{dgn} same-leaf {lname(dl,p)} End.DT6"))
                        ev += 1
                    else:
                        # cross-leaf: one EV per spine path (the EV-set / multi-path spray analog)
                        for s in range(1, S + 1):
                            sp = sname(s, p)
                            if spec:
                                carrier = mrc_carrier_spec(p, lname(dl, p), sp, dg)
                                pathdesc = f"{sgn}->{dgn} via {sp} -> {lname(dl,p)} -> {dgn} decap (NIC)"
                            else:
                                carrier = mrc_carrier(p, lname(dl, p), sp)
                                pathdesc = f"{sgn}->{dgn} via {sp} -> {lname(dl,p)}"
                            evs.append(dict(entropy=ev, usid=carrier, plane=p, spine=sp,
                                            path=pathdesc))
                            ev += 1
                profiles.append(dict(
                    mode="srv6",
                    flow=dict(src=sgn, dst=dgn),
                    active_dst=dst_addr,
                    active_evs=evs,
                ))
            doc = dict(
                _comment=f"MRC virtual-NIC profile for {sgn} (tenant {tenant}). "
                         f"Generated; one block per peer GPU, EVs = pinned uSID carriers."
                         + (" SPEC-mode: NIC decaps its own decap_sid." if spec else ""),
                tenant=tenant,
                underlay="eth1",
                gateway=gpu_gw_h(1, sg),   # this GPU's plane-1 leaf gateway — the deterministic
                                           # nexthop for carrier-block + multipath encap routes.
                spec_mode=spec,
                # spec: the GPU decaps packets whose outer DA = its decap SID (block:gpu_usid::),
                # not its tenant /128. The leaf plain-forwards that SID to the GPU. In proven mode
                # this is None and the NIC decaps its tenant (and the leaf also has End.DT6).
                decap_sid=(gpu_decap_sid(sg) if spec else None),
                profiles=profiles,
            )
            with open(os.path.join(nic_dir, f"{sgn}.json"), "w") as f:
                json.dump(doc, f, indent=2)
            nic_profiles[sgn] = dict(tenant=tenant, gateway=gpu_gw_h(1, sg),
                                     n_peers=len(profiles),
                                     n_evs=sum(len(x["active_evs"]) for x in profiles))

        # stage the mrc-nic binary alongside the profiles so the bind-mount resolves.
        # look next to this generator (nic/mrc-nic) or in the bundle root.
        here = os.path.dirname(os.path.abspath(__file__))
        nic_src = None
        for cand in [os.path.join(here, "nic", "mrc-nic"),
                     os.path.join(here, "mrc-nic.bin"),
                     os.path.join(here, "..", "nic", "mrc-nic")]:
            if os.path.isfile(cand):
                nic_src = cand
                break
        if nic_src:
            import shutil as _sh
            _sh.copyfile(nic_src, os.path.join(nic_dir, "mrc-nic"))
            os.chmod(os.path.join(nic_dir, "mrc-nic"), 0o755)
        else:
            print("WARNING: mrc-nic binary not found to stage into ./mrc-nic/ — "
                  "place it there before deploy (bind-mount expects mrc-nic/mrc-nic).",
                  file=sys.stderr)

        # /etc/hosts fabric block: resolve every node name to its FABRIC IPv6 (a
        # GPU's plane-1 tenant, a switch's loopback) so `ping <name>` between GPUs
        # rides the SRv6 underlay instead of the clab mgmt network. Bind-mounted
        # read-only and merged into each GPU's /etc/hosts at boot (keeping the
        # localhost/ip6 specials). Cross-leaf pings are SRv6-encapped; same-leaf go
        # native over the fabric.
        with open(os.path.join(nic_dir, "hosts.fabric"), "w") as f:
            f.write("# --- MRC fabric (IPv6) — gpu<->gpu traffic routes over the SRv6 underlay ---\n")
            for nm, nd in nodes.items():
                if nd.get("role") == "gpu":
                    addr = next((i["ipv6"].split("/")[0] for i in nd.get("interfaces", [])
                                 if i.get("plane") == 1 and i.get("ipv6")), None)
                elif nd.get("role") in ("leaf", "spine"):
                    addr = nd.get("loopback")
                else:
                    addr = None
                if addr:
                    f.write(f"{addr}\t{nm}\n")

    # ================= emit clab topology =================
    out = ["# Generated by gen_clab_topology.py — isolated multiplane spine/leaf fabric.",
           f"# {G} GPU(s) x every leaf; {L} leaf x {S} spine x {P} isolated plane(s); 1-based names.",
           "# Data-plane addressing is in the *_vars.json (applied by Ansible).",
           f"name: {a.name}", "", "mgmt:", f"  network: {a.name}-mgmt",
           f"  ipv4-subnet: {mg['subnet']}", "", "topology:", "  nodes:"]
    for name, r in nodes.items():
        out.append(f"    {name}:")
        out.append(f"      kind: {r['kind']}")
        out.append(f"      image: {r['image']}")
        out.append(f"      mgmt-ipv4: {r['mgmt']}")
        if a.frr_linux and r["role"] in ("spine", "leaf"):
            out.append("      binds:")
            out.append(f"        - frr/{name}/frr.conf:/etc/frr/frr.conf")
            out.append("        - frr/daemons:/etc/frr/daemons")
            out.append(f"        - frr/{name}/seg6.sh:/seg6.sh")
            out.append("      exec:")
            out.append("        - sh -c 'echo 1 > /proc/sys/net/ipv6/conf/all/forwarding'")
            out.append("        - sh -c 'for d in /proc/sys/net/ipv6/conf/*/seg6_enabled; do echo 1 > \"$d\"; done'")
            out.append("        - sh -c 'touch /etc/frr/vtysh.conf'")
            # run after FRR has had a moment to load frr.conf (frr mode backfills uN/uDT6;
            # kernel mode programs all SIDs). retry briefly in case FRR is still starting.
            out.append("        - sh -c 'for i in 1 2 3 4 5; do sh /seg6.sh && break; sleep 2; done'")
        if a.mrc_nic and r["role"] == "gpu":
            tenant = nic_profiles[name]["tenant"]
            gw = nic_profiles[name]["gateway"]
            out.append("      binds:")
            out.append(f"        - mrc-nic/{name}.json:/etc/mrc-nic/profile.json")
            # Stage the binary at a SEPARATE read-only path, NOT the exec path.
            # Bind-mounting straight onto /usr/local/bin/mrc-nic makes that path
            # un-replaceable (docker cp -> "device or resource busy") and pins the
            # container to the mount's inode, so host-side updates never propagate.
            # Instead we mount to /opt and copy to a real file at startup, so the
            # running process always loads the latest staged binary.
            out.append("        - mrc-nic/mrc-nic:/opt/mrc-nic.bin:ro")
            out.append("        - mrc-nic/hosts.fabric:/etc/hosts.fabric:ro")
            out.append("      exec:")
            # resolve fabric node names to their IPv6 (SRv6 underlay) instead of the
            # mgmt net — /etc/hosts is a bind mount, so rewrite it in place (keep the
            # localhost/ip6 specials, append the fabric block).
            out.append("        - sh -c 'keep=$(grep -E \"localhost|ip6-|^ff0|^fe00\" /etc/hosts); "
                       "{ printf \"%s\\n\" \"$keep\"; cat /etc/hosts.fabric; } > /etc/hosts'")
            # underlay loopback routes: reach each plane's switch loopbacks via THAT
            # plane's gateway so `mtr <switch>` traces the native underlay (the SRv6
            # tunnel to a GPU hides transit hops; loopbacks are natively routed). The
            # planes are isolated, so each plane's /56 loopback block routes out its
            # own interface. GPUs otherwise have no route to the loopbacks.
            import ipaddress as _ipa
            _gifs = {i["plane"]: i for i in nodes[name].get("interfaces", [])
                     if i.get("plane") and i.get("gateway")}
            _lbr = {}
            for _sw, _swnd in nodes.items():
                if _swnd.get("role") not in ("leaf", "spine"):
                    continue
                _lo, _pl = _swnd.get("loopback"), _swnd.get("plane")
                if not _lo or _pl not in _gifs:
                    continue
                _net = str(_ipa.ip_network(_lo + "/56", strict=False))
                _lbr[_net] = (_gifs[_pl]["name"], _gifs[_pl]["gateway"])
            for _net, (_ifc, _gw) in sorted(_lbr.items()):
                out.append(f"        - sh -c 'ip -6 route replace {_net} via {_gw} dev {_ifc}'")
            out.append("        - sh -c 'echo 1 > /proc/sys/net/ipv6/conf/all/forwarding'")
            out.append("        - sh -c 'for d in /proc/sys/net/ipv6/conf/*/seg6_enabled; do echo 1 > \"$d\"; done'")
            # copy staged binary -> real in-container file (NOT a mount, so re-readable)
            out.append("        - sh -c 'install -m 0755 /opt/mrc-nic.bin /usr/local/bin/mrc-nic'")
            # the network-multitool (Alpine) image ships no python3, and minimal Debian
            # images ship neither python3 NOR iproute2. Install both at boot if missing,
            # image-agnostic (apt then apk). For large fabrics, bake these into a custom
            # gpu_image instead of paying this per GPU (see MRC_NIC_README.md).
            # SPEC NOTE: iproute2 >= 6.1 (bookworm=6.1) is REQUIRED for NIC uSID decap.
            out.append("        - sh -c 'if command -v python3 >/dev/null 2>&1 && command -v ip "
                       ">/dev/null 2>&1; then :; elif command -v apt-get >/dev/null 2>&1; then "
                       "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                       "python3 iproute2 iputils-ping tcpdump >/dev/null 2>&1; elif command -v apk "
                       ">/dev/null 2>&1; then apk add --no-cache python3 iproute2 iputils tcpdump "
                       ">/dev/null 2>&1; fi'")
            # start the standalone virtual NIC: reads profile.json, programs per-EV pinned encap
            # + its own End.DT6 decap. tenant + gateway passed explicitly (deterministic).
            # When --controller is set, also attach to the backend (--controller/--host) so the
            # NIC runs the always-on full-mesh per-path test probe and POSTs path health for the
            # GUI paths table. Without it the NIC stays standalone (no mesh), exactly as before.
            ctrl = (f"--controller {a.controller} --host {name} " if a.controller else "")
            out.append(f"        - sh -c 'nohup python3 /usr/local/bin/mrc-nic run --tenant {tenant} "
                       f"--gateway {gw} --underlay eth1 --profile /etc/mrc-nic/profile.json "
                       f"{ctrl}>/var/log/mrc-nic.log 2>&1 &'")
        lbl = f'role: "{r["role"]}"'
        if r["plane"]:
            lbl += f', plane: "{r["plane"]}"'
        lbl += f', index: "{r["index"]}"'
        out.append(f"      labels: {{{lbl}}}")
    out.append("  links:")
    for ea, eb in links:
        out.append(f'    - endpoints: ["{ea}", "{eb}"]')
    clab_path = f"{a.name}.clab.yml"
    with open(clab_path, "w") as f:
        f.write("\n".join(out) + "\n")

    # ================= build the symmetric path inventory =================
    # Every GPU-to-GPU carrier the fabric can take: one record per
    # (src gpu, dst gpu, plane, spine). This is the AUTHORITATIVE list of paths
    # the mesh test-probe sweeps — emitted into fabric_vars.json so the GUI can
    # table every path (and join live probe health to it via the backend) and the
    # controller can hand each NIC its per-path probe plan WITHOUT re-deriving the
    # uSID carriers. Same carrier math as the per-GPU NIC profiles above, but as a
    # flat, source-agnostic list. "Symmetric" = both directions are present: a
    # path src->dst via a spine and its reverse dst->src via that same spine are
    # two records (each NIC probes its own outbound direction).
    #
    # fwmark is globally unique (monotonic) so a single value names exactly one
    # path; the NIC installs one fwmark->table->seg6 route per path to pin the
    # probe to that carrier. path_id is unique within a (src,dst) pair (the spine
    # node name for cross-leaf, "direct-<leaf>" for same-leaf) — the key the NIC
    # reports health under. Only homed mode has spine-transited carriers.
    paths = []
    if homed:
        fw = 49001
        for sg in range(1, G + 1):
            sgn, sl = gname(sg), home_leaf(sg)
            for dg in range(1, G + 1):
                if dg == sg:
                    continue
                dgn, dl = gname(dg), home_leaf(dg)
                for p in range(1, P + 1):
                    dleaf = lname(dl, p)
                    if dl == sl:
                        # same-leaf: no spine transit (leaf plain-forwards the /64).
                        if spec:
                            carrier = gpu_decap_sid(dg)
                        else:
                            dst_dt6 = nodes[dleaf]["srv6"]["end_dt6"]
                            block = ipaddress.IPv6Address(srv6_loc("leaf", p, 1)).exploded.split(":")[0:2]
                            carrier = ipaddress.IPv6Address(
                                ":".join(block + _csid_pair(dst_dt6) + ["0", "0", "0", "0"])).compressed
                        paths.append(dict(
                            src=sgn, dst=dgn, src_addr=gpu_addr_h(1, sg), dst_addr=gpu_addr_h(1, dg),
                            plane=p, spine=None, src_leaf=lname(sl, p), dst_leaf=dleaf,
                            kind="same-leaf", fwmark=fw, path_id=f"direct-{dleaf}", usid=carrier))
                        fw += 1
                    else:
                        # cross-leaf: one path per spine (the per-path spray analog).
                        for s in range(1, S + 1):
                            sp = sname(s, p)
                            carrier = (mrc_carrier_spec(p, dleaf, sp, dg) if spec
                                       else mrc_carrier(p, dleaf, sp))
                            paths.append(dict(
                                src=sgn, dst=dgn, src_addr=gpu_addr_h(1, sg), dst_addr=gpu_addr_h(1, dg),
                                plane=p, spine=sp, src_leaf=lname(sl, p), dst_leaf=dleaf,
                                kind="cross-leaf", fwmark=fw, path_id=sp, usid=carrier))
                            fw += 1

    # ================= emit fabric_vars.json =================
    vars_path = f"{a.out}_vars.json"
    with open(vars_path, "w") as f:
        json.dump(dict(name=a.name, shape=dict(gpus=G, leaves=L, spines=S, planes=P),
                       plan=plan, nodes=nodes, links=[dict(a=x, b=y) for x, y in links],
                       paths=paths),
                  f, indent=2)

    # ================= emit human-readable address map =================
    amap = [f"# address map for {a.name}  ({G} gpu x every leaf, {L} leaf x {S} spine x {P} plane)",
            "# every address decodes to its position. RPII=role(1)plane(1)index(2), gpu=plane:leaf:gpu.", ""]
    for name, r in nodes.items():
        pos = f"{r['role']} {r['index']}" + (f" plane {r['plane']}" if r['plane'] else " (all planes/leaves)")
        amap.append(f"{name}   [{pos}]")
        amap.append(f"    mgmt      {r['mgmt']}")
        if "loopback" in r:
            amap.append(f"    loopback  {r['loopback']}")
            amap.append(f"    srv6 loc  {r['srv6']['locator']}")
            if r["srv6"]["end_dt6"]:
                amap.append(f"    end.dt6   {r['srv6']['end_dt6']}")
        for i in r["interfaces"]:
            tag = ""
            if i.get("plane"):
                tag = f" (plane {i['plane']} leaf {i['leaf']})"
            amap.append(f"    {i['name']:<7} {i['ipv6']:<30} -> {i['peer']}{tag}")
        amap.append("")
    amap_path = f"{a.out}_address_map.txt"
    with open(amap_path, "w") as f:
        f.write("\n".join(amap) + "\n")

    # ================= summary =================
    def info(m): print(m, file=sys.stderr)
    if homed:
        leaf_ports = S + K
        gpu_nics = P
        gpu_leaf_links = P * G
        info(f"== {a.name} :: {P} plane(s), {L} leaf x {S} spine each, HOMED {K} GPU(s)/leaf = {G} GPU(s) ==")
        info(f"  switches: {P*(S+L)}   gpus: {G}  (each GPU homed to 1 leaf, {gpu_nics} NIC(s) -> one per plane)")
        info(f"  cross-leaf GPU traffic transits a spine (this is the point)")
    else:
        leaf_ports = S + G
        gpu_nics = P * L
        gpu_leaf_links = P * L * G
        info(f"== {a.name} :: {P} isolated plane(s), {L} leaf x {S} spine each, {G} GPU(s) (MESH) ==")
        info(f"  switches: {P*(S+L)}   gpus: {G}  (each GPU has {gpu_nics} NICs -> every leaf)")
    info(f"  links: {len(links)}   ({P*L*S} leaf-spine + {gpu_leaf_links} gpu-leaf)")
    info(f"  leaf ports: eth1..eth{S} uplinks + eth{S+1}..eth{leaf_ports} GPUs = {leaf_ports}")
    if leaf_ports > a.max_leaf_ports:
        info(f"  WARNING: leaf needs {leaf_ports} > {a.max_leaf_ports} ports — add leaves/planes or cut GPUs.")
    info(f"  wrote: {clab_path}")
    info(f"  wrote: {vars_path}   (addresses + SRv6 SIDs + static routes for Ansible)")
    info(f"  wrote: {amap_path}   (human-readable position -> address)")
    if a.mrc_nic:
        tot_ev = sum(v["n_evs"] for v in nic_profiles.values())
        info(f"  wrote: mrc-nic/   ({len(nic_profiles)} GPU profiles, {tot_ev} total EVs, "
             f"+ staged mrc-nic binary) — each GPU runs the standalone virtual MRC NIC")
        if spec:
            info("")
            info("  *** SPEC MODE — GPU IMAGE REQUIREMENT ***")
            info("  NIC-side uSID decap (End.DT6 on the GPU) needs iproute2 >= 6.1 AND kernel")
            info("  >= 6.1 (NEXT-C-SID). The default network-multitool image ships iproute2 5.6")
            info("  (2020) and will SILENTLY DROP decapped packets. Use a current image, e.g.:")
            info("     --gpu-image debian:bookworm   (iproute2 6.1)")
            info("     --gpu-image debian:trixie     (iproute2 6.15)")
            info("  The NIC prints a capability WARNING at startup and `mrc-nic doctor` reports")
            info("  the iproute2 version. Proven leaf-decap fabric (spec_mode=false) works on any image.")
    info(f"  deploy: sudo containerlab deploy -t {clab_path}")


if __name__ == "__main__":
    main()
