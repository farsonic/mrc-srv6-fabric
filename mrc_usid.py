#!/usr/bin/env python3
"""mrc_usid.py — compute MRC uSID carriers from a compact fabric descriptor.

This is the "formula, not table" core. Because every address in the fabric is
position-encoded, a GPU's entire EV set (one uSID carrier per destination GPU,
per plane, per spine) is DERIVABLE from a handful of integers — the fabric shape
plus this GPU's index — instead of being enumerated on disk.

    carrier (spec, cross-leaf) = block : <spine RPII> : e000+leaf : 9LLL : GGGG : 0 : 0
        RPII(role,plane,idx) = role·0x1000 + plane·0x100 + idx   (spine role=1, leaf=2)
        9LLL = 0x9000 + dst_leaf   GGGG = dst_local_gpu_index

`expand()` turns the descriptor into the exact same `profiles` blocks the
generator used to write out in full — so the NIC's runtime behaviour (per-EV
encap, per-plane/per-spine drain, probing) is byte-for-byte unchanged. The only
thing that shrinks is the on-disk config: O(1) formula vs O(GPUs²·planes·spines)
JSON. Shared by gen_clab_topology.py (emit + self-verify) and mrc-nic (load).
"""
import ipaddress


def _addr(hexts):
    return ipaddress.IPv6Address(":".join(hexts)).compressed


def _peer_set(sg, G, K, sparse):
    """Destination GPUs this source connects to. Full mesh = everyone else.
    Sparse = a deterministic 'closest K' hot set: same-leaf peers first (cheap,
    no spine), then nearest GPU indices — a stand-in for a real training run's
    ring/TP/PP/EP partners."""
    others = [g for g in range(1, G + 1) if g != sg]
    if not sparse or sparse >= len(others):
        return others
    sl = (sg - 1) // K
    others.sort(key=lambda g: (0 if (g - 1) // K == sl else 1, abs(g - sg)))
    return sorted(others[:sparse])


def expand(desc, sparse=None, spray=None):
    """Reconstruct the full `profiles` list from a compact fabric descriptor.

    desc["fabric"] carries: mode, block, endx_base, dt6, gpu_nibble, planes,
    spines, gpus_per_leaf, gpus, gpu_base, gpu_host, src_gpu, ev_base,
    role_spine, role_leaf, multi, hp{spine,leaf,gpu,plane_tag}.
    Optional sparse (peers/GPU) and spray (paths/flow) trim the set exactly like
    the planner models — omit for the true full mesh."""
    f = desc["fabric"]
    spec = f["mode"] == "spec"
    blk = f["block"].split(":")                       # ["fcbb","bb00"] or ["fc00","0"]
    endx = int(f["endx_base"], 16)                    # 0xe000
    dt6 = f["dt6"]                                     # "fff6"
    nib = f["gpu_nibble"]                              # 9
    P, S, K, G = f["planes"], f["spines"], f["gpus_per_leaf"], f["gpus"]
    gpu_base, gpu_host = f["gpu_base"], f["gpu_host"]
    r_sp, r_lf = f.get("role_spine", 1), f.get("role_leaf", 2)
    multi = f.get("multi", P > 1)
    hp = f.get("hp", {"spine": "spine", "leaf": "leaf", "gpu": "gpu", "plane_tag": "p"})
    sg = f["src_gpu"]
    ev = f.get("ev_base", 49001)
    fan = S if spray is None else max(1, min(int(spray), S))

    def home_leaf(g): return (g - 1) // K + 1
    def local_idx(g): return (g - 1) % K + 1
    def gname(g): return f"{hp['gpu']}{g}"
    def sname(s, p): return f"{hp['spine']}{s}-{hp['plane_tag']}{p}" if multi else f"{hp['spine']}{s}"
    def lname(l, p): return f"{hp['leaf']}{l}-{hp['plane_tag']}{p}" if multi else f"{hp['leaf']}{l}"
    def rpii(role, p, idx): return f"{role:x}{p:x}{idx:02x}"
    def gpu_addr(g):
        l, loc = home_leaf(g), local_idx(g)
        return _addr([gpu_base.split(":")[0], gpu_base.split(":")[1], "1",
                      f"{l:02x}{loc:02x}", "", f"{gpu_host:x}"])  # base:1:LLGG::host

    sl = home_leaf(sg)
    sgn = gname(sg)
    profiles = []
    for dg in _peer_set(sg, G, K, sparse):
        dl, dloc = home_leaf(dg), local_idx(dg)
        dgn = gname(dg)
        evs = []
        for p in range(1, P + 1):
            if dl == sl:
                # same-leaf: no spine transit (one path per plane)
                if spec:
                    usid = _addr(blk + [f"{(nib << 12) + dl:x}", f"{dloc:x}", "", ""])
                    path = f"{sgn}->{dgn} same-leaf -> {dgn} decap uSID"
                else:
                    usid = _addr(blk + [rpii(r_lf, p, dl), dt6, "0", "0", "0", "0"])
                    path = f"{sgn}->{dgn} same-leaf {lname(dl, p)} End.DT6"
                evs.append(dict(entropy=ev, usid=usid, plane=p, spine=None, path=path))
                ev += 1
            else:
                # cross-leaf: one EV per spine path (bounded by `fan`)
                for s in range(1, fan + 1):
                    spn = sname(s, p)
                    if spec:
                        usid = _addr(blk + [rpii(r_sp, p, s), f"{endx + dl:x}",
                                            f"{(nib << 12) + dl:x}", f"{dloc:x}", "0", "0"])
                        path = f"{sgn}->{dgn} via {spn} -> {lname(dl, p)} -> {dgn} decap (NIC)"
                    else:
                        usid = _addr(blk + [rpii(r_sp, p, s), f"{endx + dl:x}",
                                            rpii(r_lf, p, dl), dt6, "0", "0"])
                        path = f"{sgn}->{dgn} via {spn} -> {lname(dl, p)}"
                    evs.append(dict(entropy=ev, usid=usid, plane=p, spine=spn, path=path))
                    ev += 1
        profiles.append(dict(mode="srv6", flow=dict(src=sgn, dst=dgn),
                             active_dst=gpu_addr(dg), active_evs=evs))
    return profiles
