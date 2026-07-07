#!/usr/bin/env python3
"""plan_fabric.py — size an MRC SRv6 fabric and emit the ansible deploy command.

Answers "I want N GPUs across P planes — how many leaves/spines, and what's the
`ansible-playbook` line?" It asks for the GPU count, the physical ports per leaf,
the leaf port speed and the per-NIC throughput (breakout cables split one high-
speed leaf port into several NIC-facing links), then:

  * derives gpus_per_leaf / leaves / spines for a 2-tier CLOS per plane,
  * checks the address-plan caps (255 leaves/spines/gpus-per-leaf per plane,
    <=9 planes cleanly, mgmt IPv4 /22 hard-fails past ~256 GPUs),
  * prints the fabric shape + the ready-to-run ansible command,
  * optional --dry-run: actually generates the FRR/clab configs (small builds
    real, huge builds extrapolated from two reference builds) and reports the
    on-disk config size so you can sanity-check a massive deployment.

Interactive:   python3 plan_fabric.py
Scripted:      python3 plan_fabric.py --gpus 10000 --ports-per-leaf 64 \
                 --port-speed 800 --nic-speed 200 --planes 4 --dry-run
"""
import argparse, math, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SWITCH_IMAGE = "quay.io/frrouting/frr:10.6.1"
GPU_IMAGE = "debian:bookworm"

# address-plan caps (see gen_clab_topology.py / address_plan.json)
CAP_INDEX = 255      # leaves / spines / gpus-per-leaf per plane (RPII index = 2 hex nibbles)
CAP_PLANES_CLEAN = 9  # plane is 1 hex nibble; 1..9 read cleanly, 10..15 still work
CAP_PLANES_HARD = 15
MGMT_DEFAULT_GPUS = 256   # 172.20.20.0/22 with gpu base 256 hard-fails past ~this


def ask(prompt, default, cast):
    """Prompt with a default; blank keeps the default. Falls back to default on EOF."""
    try:
        raw = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        print(f"  ! not a valid value, using {default}")
        return default


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def plan(gpus, ports_per_leaf, port_speed, nic_speed, planes, oversub, sparse=None, spray=None):
    """Return the computed fabric shape + a list of warnings."""
    warn = []
    breakout = port_speed / nic_speed
    if breakout < 1:
        warn.append(f"NIC speed {nic_speed}G > leaf port speed {port_speed}G — no breakout possible; "
                    f"treating breakout as 1 (one NIC per leaf port).")
        breakout = 1
    breakout_i = int(breakout)
    if breakout_i != breakout:
        warn.append(f"port {port_speed}G / NIC {nic_speed}G = {breakout:.2f} is not an integer breakout; "
                    f"flooring to {breakout_i}x (leftover lanes unused).")
    breakout = max(1, breakout_i)

    # split the leaf's physical ports into downlinks (to NICs) and uplinks (to spines)
    # by the oversubscription ratio (down:up). r=1 -> half/half (non-blocking).
    r = max(0.1, oversub)
    down = max(1, round(ports_per_leaf * r / (r + 1.0)))
    up = max(1, ports_per_leaf - down)
    if down + up > ports_per_leaf:      # rounding guard
        down = ports_per_leaf - up

    gpus_per_leaf = down * breakout               # NICs facing one leaf (per plane)
    spines = up                                   # one uplink port per spine (non-blocking within plane)
    leaves = math.ceil(gpus / gpus_per_leaf)

    if spines > CAP_INDEX:
        warn.append(f"{spines} spines/plane exceeds the {CAP_INDEX} cap — capping to {CAP_INDEX} "
                    f"(fabric becomes oversubscribed).")
        spines = CAP_INDEX
    if gpus_per_leaf > CAP_INDEX:
        warn.append(f"{gpus_per_leaf} GPUs/leaf exceeds the {CAP_INDEX} cap — widen the RPII index "
                    f"in gen_clab_topology.py or use fewer breakout lanes.")
    if leaves > CAP_INDEX:
        warn.append(f"{leaves} leaves/plane exceeds the {CAP_INDEX} cap — this needs a 3-tier fabric "
                    f"(super-spines) or a wider index nibble; the current generator is 2-tier only.")
    if planes > CAP_PLANES_HARD:
        warn.append(f"{planes} planes exceeds the hard cap of {CAP_PLANES_HARD}.")
    elif planes > CAP_PLANES_CLEAN:
        warn.append(f"{planes} planes works but 10..15 no longer read cleanly in the position-encoding.")

    actual_gpus = leaves * gpus_per_leaf
    switches = (leaves + spines) * planes
    containers = actual_gpus + switches

    mgmt_subnet = None
    if actual_gpus >= MGMT_DEFAULT_GPUS:
        # widen mgmt so the generator doesn't hard-fail past ~256 GPUs
        mgmt_subnet = "172.16.0.0/12"
        warn.append(f"{actual_gpus} GPUs needs a wider mgmt subnet than the default /22 — "
                    f"the command below adds -e mgmt_subnet={mgmt_subnet}.")
    # the default address plan strides mgmt IPv4 by 20 per plane, so multi-plane
    # builds collide once a role exceeds ~20 switches/plane — a real wall that a
    # wider subnet alone does NOT fix (the per-plane stride must grow too).
    if planes > 1 and max(leaves, spines) > 20:
        warn.append(f"{max(leaves, spines)} switches/plane with {planes} planes overruns the "
                    f"default mgmt per-plane stride (20) — leaf/spine mgmt IPs collide across "
                    f"planes. Widen mgmt.per_plane_stride (and mgmt.subnet) in address_plan.json.")

    # EV state held at the edge: full mesh vs a sparse (connection-oriented) set.
    ev_full = ev_count(actual_gpus, gpus_per_leaf, planes, spines)
    peers = None if sparse is None else min(sparse, actual_gpus - 1)
    ev_sparse = None
    if sparse is not None or spray is not None:
        ev_sparse = ev_count(actual_gpus, gpus_per_leaf, planes, spines, peers=peers, spray=spray)
        if peers is not None and peers > actual_gpus - 1:
            warn.append(f"--sparse {sparse} exceeds the {actual_gpus-1} available peers; using full mesh.")

    return dict(
        gpus=gpus, actual_gpus=actual_gpus, planes=planes, breakout=breakout,
        down=down, up=up, ports_per_leaf=ports_per_leaf, port_speed=port_speed, nic_speed=nic_speed,
        gpus_per_leaf=gpus_per_leaf, leaves=leaves, spines=spines,
        switches=switches, containers=containers, mgmt_subnet=mgmt_subnet,
        sparse=sparse, spray=spray, peers=peers, ev_full=ev_full, ev_sparse=ev_sparse,
        # bandwidth (both directions counted once): all NIC downlinks across all planes
        total_nic_bw_tbps=actual_gpus * nic_speed * planes / 1000.0,
    ), warn


def ansible_cmd(p, extra_gui=True):
    parts = ["ansible-playbook site-frr.yml",
             f"  -e planes={p['planes']} -e leaves={p['leaves']} "
             f"-e spines={p['spines']} -e gpus_per_leaf={p['gpus_per_leaf']}"]
    if p["mgmt_subnet"]:
        parts.append(f"  -e mgmt_subnet={p['mgmt_subnet']}")
    parts.append("  -e spec_mode=true -e gpu_image=debian:bookworm")
    if extra_gui:
        parts.append("  -e do_gui=true")
    return " \\\n".join(parts)


def print_plan(p, warn):
    print("\n" + "=" * 66)
    print(f"  MRC SRv6 fabric — {p['actual_gpus']} GPUs across {p['planes']} planes")
    print("=" * 66)
    print(f"  breakout            {p['port_speed']}G leaf port / {p['nic_speed']}G NIC "
          f"= {p['breakout']}x per port")
    print(f"  leaf ports          {p['ports_per_leaf']}  ->  {p['down']} downlink + {p['up']} uplink")
    print(f"  gpus_per_leaf (K)   {p['gpus_per_leaf']}   ({p['down']} downlink ports x {p['breakout']} breakout)")
    print(f"  leaves  / plane     {p['leaves']}")
    print(f"  spines  / plane     {p['spines']}")
    print(f"  planes              {p['planes']}")
    print("  " + "-" * 62)
    print(f"  switches (total)    {p['switches']:>8}   = ({p['leaves']}+{p['spines']}) x {p['planes']}")
    print(f"  GPU nodes           {p['actual_gpus']:>8}")
    print(f"  containers (total)  {p['containers']:>8}   (1 per node in containerlab)")
    print(f"  fabric NIC BW       {p['total_nic_bw_tbps']:>8.1f} Tbps  ({p['actual_gpus']} GPUs "
          f"x {p['nic_speed']}G x {p['planes']} planes)")
    if p["actual_gpus"] != p["gpus"]:
        print(f"  note: rounded up to {p['actual_gpus']} GPUs to fill {p['leaves']} even leaves "
              f"(asked for {p['gpus']}).")
    print("  " + "-" * 62)
    print(f"  EV records (full mesh) {p['ev_full']:>13,}   = GPUs² · planes · spines (all-to-all)")
    if p["ev_sparse"] is not None:
        levers = []
        if p["sparse"] is not None:
            levers.append(f"{p['peers']} peers/GPU")
        if p["spray"] is not None:
            levers.append(f"{min(p['spray'], p['spines'])} paths/flow")
        red = p["ev_full"] / p["ev_sparse"] if p["ev_sparse"] else float("inf")
        print(f"  EV records (sparse)    {p['ev_sparse']:>13,}   = {', '.join(levers)}  "
              f"→ {red:,.0f}× smaller")
        print("  sparse = the connection-oriented MRC model: EVs only for a GPU's live")
        print("  training-run peers, bounded path fan-out — LINEAR in GPUs, not quadratic.")
    if warn:
        print("\n  ⚠ notes:")
        for w in warn:
            print(f"    - {w}")
    feasible = p["containers"] <= 800
    print("\n  deploy (from ansible/):\n")
    for line in ("cd ansible\n" + ansible_cmd(p)).splitlines():
        print("    " + line)
    if not feasible:
        print(f"\n  ⚠ {p['containers']} containers will NOT fit on one host — containerlab runs one")
        print("    container per node. Treat this as an addressing/config-plan target (use")
        print("    --dry-run to size the configs), or split across hosts with multi-node clab.")
    print()


# ---------------- dry-run: generate configs and report their size ----------------
def _gen(tmp, leaves, spines, gpus_per_leaf, planes):
    """Run gen_clab_topology.py into tmp (cwd); return byte buckets + gpu count.

    Buckets matter because the terms scale differently:
      prof  = per-GPU EV profiles (mrc-nic/*.json)  -> O(GPUs^2 * planes)
      mesh  = fabric_vars.json (the probe mesh)       -> O(GPUs^2 * planes)
      clab  = the .clab.yml containerlab parses        -> ~linear in nodes+links
      other = per-switch FRR configs, hosts, scripts   -> ~linear in switch nodes
    """
    cmd = [sys.executable, os.path.join(HERE, "gen_clab_topology.py"),
           "--image", SWITCH_IMAGE, "--gpu-image", GPU_IMAGE,
           "--gpus-per-leaf", str(gpus_per_leaf), "--leaves", str(leaves),
           "--spines", str(spines), "--planes", str(planes),
           "--name", "sizing", "--plan", os.path.join(HERE, "address_plan.json"),
           "--out", "fabric", "--frr-linux", "--seg6", "frr",
           "--mrc-nic", "--spec-mode", "--mgmt-subnet", "10.128.0.0/9"]
    r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"generator failed:\n{r.stderr.strip() or r.stdout.strip()}")
    skip = {"mrc-nic", "mrc-probe"}       # staged NIC binaries, not configs
    b = dict(prof=0, mesh=0, clab=0, other=0)
    gpu = 0
    for root, _dirs, files in os.walk(tmp):
        in_nic = os.path.basename(root) == "mrc-nic"
        for fn in files:
            if fn in skip:
                continue
            try:
                sz = os.path.getsize(os.path.join(root, fn))
            except OSError:
                continue
            if in_nic and fn.endswith(".json"):
                b["prof"] += sz; gpu += 1
            elif fn.endswith("_vars.json"):
                b["mesh"] += sz
            elif fn.endswith(".clab.yml"):
                b["clab"] += sz
            else:
                b["other"] += sz
    return b, gpu


def ev_count(gpus, gpus_per_leaf, planes, spines, peers=None, spray=None):
    """Total EV (uSID carrier) records the edge holds.

    FULL MESH (peers=None, spray=None): every source GPU stores one EV per
    destination GPU, per plane, and — for destinations on a DIFFERENT leaf — per
    spine (a distinct parallel path):
        same-leaf dsts  (K-1)  -> planes EVs each          (spine=null)
        cross-leaf dsts (G-K)  -> planes*spines EVs each
    => ~ G^2 * planes * spines. Verified real: 2x GPUs -> ~4x, 2x spines -> ~2x.

    SPARSE (the connection-oriented MRC model): cap each GPU to `peers` live
    connections (a training-run hot set — ring/TP/PP/EP partners, not all N) and
    cap cross-leaf paths-per-flow to `spray` (keep a bounded fan-out instead of
    one EV per spine). Peers are split same/cross-leaf in the fabric's ratio.
    => ~ G * peers * planes * spray  (LINEAR in GPUs)."""
    G, K, P, S = gpus, gpus_per_leaf, planes, spines
    fan = S if spray is None else max(1, min(spray, S))     # cross-leaf paths per flow per plane
    total_peers = max(0, G - 1)
    npeer = total_peers if peers is None else max(0, min(peers, total_peers))
    frac_same = ((K - 1) / total_peers) if total_peers else 0.0   # same-leaf share of the peer set
    same = round(npeer * frac_same)
    cross = npeer - same
    return G * (same * P + cross * P * fan)


def _estimate(p, tmp):
    """Project config size from ONE tiny reference build + the exact EV-count law.

    We build a small fabric just to MEASURE the per-EV byte cost of the profile
    and mesh JSON, then scale by the ratio of analytically-computed EV counts
    (which captures GPUs^2, planes AND spines exactly). Tiny reference => fast;
    the closed-form ratio => accurate regardless of spine/plane count."""
    rl, rs = 4, min(p["spines"], 4)
    rk = min(p["gpus_per_leaf"], 8)               # keep the reference ~32 GPUs = fast
    b, gpu_ref = _gen(tmp, rl, rs, rk, 1)         # planes=1 dodges the mgmt-stride collision
    if gpu_ref < 1:
        raise RuntimeError("reference build produced no GPU profiles")
    ev_ref = ev_count(gpu_ref, rk, 1, rs)
    ev_tgt = ev_count(p["actual_gpus"], p["gpus_per_leaf"], p["planes"], p["spines"])
    ratio = (ev_tgt / ev_ref) if ev_ref else 0.0
    sw_ref = (rl + rs) * 1
    sw_tgt = (p["leaves"] + p["spines"]) * p["planes"]
    prof = b["prof"] * ratio                          # scales with EV count
    mesh = b["mesh"] * ratio                          # scales with EV count
    clab = b["clab"] * (sw_tgt + p["actual_gpus"]) / (sw_ref + gpu_ref)  # ~linear in nodes+links
    other = b["other"] * sw_tgt / sw_ref              # ~linear in switch nodes
    return dict(total=prof + mesh + clab + other, prof=prof, mesh=mesh, clab=clab,
                other=other, evs=ev_tgt)


def dry_run(p):
    print("  dry-run: measuring a tiny reference build and projecting …")
    with tempfile.TemporaryDirectory(prefix="mrc-sizing-") as tmp:
        exact = False
        try:
            if p["containers"] <= 400:
                try:
                    b, _g = _gen(tmp, p["leaves"], p["spines"], p["gpus_per_leaf"], p["planes"])
                    est = dict(total=sum(b.values()), prof=b["prof"], mesh=b["mesh"],
                               clab=b["clab"], other=b["other"],
                               evs=ev_count(p["actual_gpus"], p["gpus_per_leaf"],
                                            p["planes"], p["spines"]))
                    exact = True
                except RuntimeError:      # multi-plane mgmt-stride collision at this scale
                    shutil.rmtree(tmp, ignore_errors=True); os.mkdir(tmp)
                    est = _estimate(p, tmp)
            else:
                est = _estimate(p, tmp)
        except RuntimeError as e:
            print(f"  ! dry-run failed: {e}")
            return
    tag = "measured" if exact else "estimated from a ~32-GPU reference + EV-count law"
    print(f"\n  config size — FULL MESH ({tag}):")
    print(f"    total on-disk configs    {human(est['total'])}")
    print(f"    ├ per-GPU EV profiles    {human(est['prof'])}   (mrc-nic/*.json)")
    print(f"    ├ probe mesh (vars.json) {human(est['mesh'])}   (GUI/mesh inventory)")
    print(f"    ├ clab topology (.yml)   {human(est['clab'])}   (single YAML containerlab parses)")
    print(f"    └ per-switch FRR configs {human(est['other'])}")
    print(f"    EV carrier records       {est['evs']:,}   (= GPUs² · planes · spines, all-to-all)")
    print(f"    ~ per node               {human(est['total'] / max(1, p['containers']))}")

    if p["ev_sparse"] is not None and est["evs"]:
        # the EV-scaling buckets (profiles + mesh) shrink by the EV ratio; the
        # switch configs (clab.yml, FRR) are unaffected by connection sparsity.
        f = p["ev_sparse"] / est["evs"]
        sprof, smesh = est["prof"] * f, est["mesh"] * f
        stotal = sprof + smesh + est["clab"] + est["other"]
        red = est["total"] / stotal if stotal else float("inf")
        levers = []
        if p["sparse"] is not None:
            levers.append(f"{p['peers']} peers/GPU")
        if p["spray"] is not None:
            levers.append(f"{min(p['spray'], p['spines'])} paths/flow")
        print(f"\n  config size — SPARSE ({', '.join(levers)}):")
        print(f"    total on-disk configs    {human(stotal)}   ({red:,.0f}× smaller)")
        print(f"    ├ per-GPU EV profiles    {human(sprof)}")
        print(f"    ├ probe mesh (vars.json) {human(smesh)}")
        print(f"    └ switch configs (unchanged) {human(est['clab'] + est['other'])}")
        print(f"    EV carrier records       {p['ev_sparse']:,}   (= GPUs · peers · planes · paths)")
        print("    this is the connection-oriented MRC edge: EVs only for a GPU's live")
        print("    training-run peers with a bounded path fan-out — LINEAR in GPU count.")
    else:
        print("    the profile + mesh JSON enumerate one uSID carrier per (src GPU, dst GPU,")
        print("    plane, spine) — an all-to-all × all-paths set. Add --sparse K [--spray N]")
        print("    to model a real training run's connection set (linear, not quadratic).")
    print()


def main():
    ap = argparse.ArgumentParser(description="Size an MRC SRv6 fabric and emit the ansible command.")
    ap.add_argument("--gpus", type=int)
    ap.add_argument("--ports-per-leaf", type=int)
    ap.add_argument("--port-speed", type=float, help="leaf switch port speed in Gbps (e.g. 800)")
    ap.add_argument("--nic-speed", type=float, help="per-NIC throughput in Gbps (e.g. 200)")
    ap.add_argument("--planes", type=int, default=4)
    ap.add_argument("--oversub", type=float, default=1.0,
                    help="downlink:uplink ratio (1.0 = non-blocking, 3.0 = 3:1)")
    ap.add_argument("--sparse", type=int, default=None,
                    help="connections (peers) per GPU — a real training-run hot set "
                         "instead of the all-to-all mesh (e.g. 32). Makes EV state linear.")
    ap.add_argument("--spray", type=int, default=None,
                    help="paths (EVs) per cross-leaf flow per plane; caps the per-spine "
                         "fan-out (e.g. 4). Default = all spines (full spray).")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate the configs and report their on-disk size")
    a = ap.parse_args()

    interactive = a.gpus is None
    if interactive:
        print("MRC SRv6 fabric planner — answer a few questions (Enter accepts the default).\n")
    gpus = a.gpus if a.gpus is not None else ask("Total GPUs", 1024, int)
    ports = a.ports_per_leaf if a.ports_per_leaf is not None else ask("Physical ports per leaf switch", 64, int)
    pspeed = a.port_speed if a.port_speed is not None else ask("Leaf port speed (Gbps)", 800, float)
    nspeed = a.nic_speed if a.nic_speed is not None else ask("Per-NIC throughput (Gbps, via breakout)", 200, float)
    planes = a.planes if not interactive else ask("Planes", a.planes, int)
    oversub = a.oversub if not interactive else ask("Oversubscription (down:up, 1=non-blocking)", a.oversub, float)
    sparse, spray = a.sparse, a.spray
    if interactive:
        s = ask("Sparse: connections per GPU (blank = full mesh)", "", str)
        sparse = int(s) if s.strip().isdigit() else None
        if sparse is not None:
            s = ask("  paths per cross-leaf flow (blank = all spines)", "", str)
            spray = int(s) if s.strip().isdigit() else None

    if gpus < 1 or ports < 2 or pspeed <= 0 or nspeed <= 0 or planes < 1:
        sys.exit("error: GPUs>=1, ports>=2, speeds>0, planes>=1 required.")
    if sparse is not None and sparse < 1:
        sys.exit("error: --sparse must be >= 1.")

    p, warn = plan(gpus, ports, pspeed, nspeed, planes, oversub, sparse=sparse, spray=spray)
    print_plan(p, warn)

    do_dry = a.dry_run
    if interactive and not do_dry:
        do_dry = ask("Run a config-size dry-run now? (y/n)", "n", str).lower().startswith("y")
    if do_dry:
        dry_run(p)


if __name__ == "__main__":
    main()
