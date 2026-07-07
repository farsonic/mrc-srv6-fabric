# MRC-style SRv6 uSID Fabric (all-Linux reference)

An all-Linux reference implementation of an **MRC-style AI/GPU fabric**: SRv6
micro-SID (uSID) source routing where the **NIC chooses the path** and writes it
into the IPv6 destination address, and the **core (spines + leaves) does nothing
but plain longest-prefix IPv6 forwarding**. No MPLS, no EVPN, no per-endpoint
state in the core.

The design and scaling rationale — written for a traditional network engineer,
including multi-plane and per-packet spray — is in
[`docs/SRv6_Stateless_Core_Whitepaper.docx`](docs/SRv6_Stateless_Core_Whitepaper.docx).
Full engineering notes: [`docs/MRC_SRV6_DESIGN.md`](docs/MRC_SRV6_DESIGN.md).

---

## Contents

- [How it works (one minute)](#how-it-works-one-minute)
- [Prerequisites](#prerequisites)
- [Bring the lab up](#bring-the-lab-up)
- [Use the fabric](#use-the-fabric)
- [Inspect what the NIC is doing](#inspect-what-the-nic-is-doing)
- [Topology GUI](#topology-gui)
- [Maintenance drain](#maintenance-drain)
- [Live packet capture (Edgeshark)](#live-packet-capture-edgeshark)
- [Tear the lab down](#tear-the-lab-down)
- [How it scales](#how-it-scales)
- [Troubleshooting](#troubleshooting)
- [Repository layout](#repository-layout)

---

## How it works (one minute)

A CLOS fabric of FRR switches (spines, leaves) and GPU hosts, all Linux. Each GPU
host runs a software **virtual NIC** (`mrc-nic`) that:

1. builds an SRv6 uSID **carrier** for every destination — one carrier per path,
2. **encapsulates** outbound traffic into that carrier,
3. **decapsulates** (`End.DT6`) its own arriving traffic,
4. **probes every path continuously** (an SO_MARK-pinned test probe per uSID
   carrier) and reports per-path RTT / loss / jitter so path health is always live.

The switches only ever do plain IPv6 longest-prefix forwarding on `/48` / `/36`
prefixes. A leaf-nested uSID address plan keeps core routing state proportional to
the **fabric** (leaves + spines), never to the **number of GPUs**.

An optional [**topology GUI**](#topology-gui) turns the probe stream into a live
dashboard (per-path health, per-node config/console, maintenance drain), and
[**Edgeshark**](#live-packet-capture-edgeshark) adds in-browser Wireshark capture
on any fabric link.

Two modes, selected at deploy time:

| Mode | Flag | Decap location | Requirements |
|------|------|----------------|--------------|
| **Leaf-decap** (default, proven) | `spec_mode=false` | leaf | none special |
| **NIC-decap** (full MRC spec) | `spec_mode=true` | destination NIC | kernel >= 6.1, iproute2 >= 6.1 on the GPU image |

---

## Prerequisites

On the lab host (a Linux box):

- **Docker**
- **[containerlab](https://containerlab.dev/install/)**  (`bash -c "$(curl -sL https://get.containerlab.dev)"`)
- **Ansible**  (`pip install ansible` or distro package)
- The FRR switch image and a GPU host image, pullable by Docker:
  - switches: `quay.io/frrouting/frr:10.6.1` (or any recent FRR)
  - GPU hosts: `debian:bookworm` **for `spec_mode=true`** (ships iproute2 6.1);
    `ghcr.io/hellt/network-multitool:latest` is fine for the default mode.

```bash
docker pull quay.io/frrouting/frr:10.6.1
docker pull debian:bookworm
```

Clone:

```bash
git clone https://github.com/farsonic/mrc-srv6-fabric.git
cd mrc-srv6-fabric
```

---

## Bring the lab up

All deploys run from the `ansible/` directory. The playbook generates the
containerlab topology + FRR configs + per-GPU NIC profiles, deploys the lab, and
starts the NIC on each GPU host.

### Default fabric (leaf-decap) — 2 leaves, 2 spines, 2 GPUs/leaf

```bash
cd ansible
ansible-playbook site-frr.yml \
  -e gpus_per_leaf=2 -e leaves=2 -e spines=2
```

### Full MRC spec fabric (NIC-side decap)

```bash
cd ansible
ansible-playbook site-frr.yml \
  -e gpus_per_leaf=2 -e leaves=2 -e spines=2 \
  -e spec_mode=true -e gpu_image=debian:bookworm
```

### Deploy parameters

| Variable | Meaning | Example |
|----------|---------|---------|
| `gpus_per_leaf` | GPUs attached to each leaf | `2`, `8` |
| `leaves` | number of leaf switches | `2` |
| `spines` | number of spine switches | `2` |
| `planes` | number of parallel (isolated) fabric planes | `1` (default), `2`, `4` |
| `spec_mode` | `true` = NIC decap; `false` = leaf decap | `true` |
| `gpu_image` | GPU host container image | `debian:bookworm` |
| `switch_image` | FRR switch image | `quay.io/frrouting/frr:10.6.1` |
| `do_gui` | start the [topology GUI](#topology-gui) + probe dashboard on `:8080` | `true` |
| `gui_console` | let the GUI open an in-browser shell / run commands on nodes (mounts the host Docker socket — **lab-only**) | `true` |
| `do_edgeshark` | deploy [Edgeshark](#live-packet-capture-edgeshark) for live per-link capture on `:5001` (**lab-only**) | `true` |

When `do_gui=true`, each NIC attaches to the GUI backend and streams its live
per-path probe health into the dashboard. A full-featured deploy:

```bash
ansible-playbook site-frr.yml \
  -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e planes=2 \
  -e spec_mode=true -e gpu_image=debian:bookworm \
  -e do_gui=true -e gui_console=true -e do_edgeshark=true
```

A larger example (8 leaves x 8 GPUs, 4 spines, spec mode):

```bash
ansible-playbook site-frr.yml \
  -e gpus_per_leaf=8 -e leaves=8 -e spines=4 \
  -e spec_mode=true -e gpu_image=debian:bookworm
```

### Single-, dual-, and four-plane fabrics

The number of **isolated planes** is just `-e planes=N` — each GPU gets one NIC
per plane, and each plane is its own leaf-spine CLOS. Everything else identical;
`spec_mode=true` + `debian:bookworm` gives the full NIC-decap / dual-plane path
set, GUI, console, and Edgeshark. Run each from `ansible/`, one topology at a
time (`containerlab destroy` before switching plane count — see
[Tear the lab down](#tear-the-lab-down)).

**Single plane** — 4 GPUs (1 NIC each), 2 leaves + 2 spines, 20 paths:

```bash
ansible-playbook site-frr.yml \
  -e planes=1 -e leaves=2 -e spines=2 -e gpus_per_leaf=2 \
  -e spec_mode=true -e gpu_image=debian:bookworm \
  -e do_gui=true -e gui_console=true -e do_edgeshark=true
```

**Two planes** — 4 GPUs (2 NICs each), 4 leaves + 4 spines, 40 paths:

```bash
ansible-playbook site-frr.yml \
  -e planes=2 -e leaves=2 -e spines=2 -e gpus_per_leaf=2 \
  -e spec_mode=true -e gpu_image=debian:bookworm \
  -e do_gui=true -e gui_console=true -e do_edgeshark=true
```

**Four planes** — 4 GPUs (4 NICs each), 8 leaves + 8 spines, 80 paths:

```bash
ansible-playbook site-frr.yml \
  -e planes=4 -e leaves=2 -e spines=2 -e gpus_per_leaf=2 \
  -e spec_mode=true -e gpu_image=debian:bookworm \
  -e do_gui=true -e gui_console=true -e do_edgeshark=true
```

| planes | GPUs | NICs/GPU | switches (leaf+spine) | test-probe paths |
|--------|------|----------|-----------------------|------------------|
| 1 | 4 | 1 | 4 | 20 |
| 2 | 4 | 2 | 8 | 40 |
| 4 | 4 | 4 | 16 | 80 |

(Total GPUs = `leaves × gpus_per_leaf`; NICs per GPU = `planes`; switches =
`(leaves + spines) × planes`.) After it comes up: GUI at
`http://<host>:8080/gui/topology.html`, Edgeshark at `http://<host>:5001`. First
boot on `debian:bookworm` installs tooling per GPU, so give the paths table a
minute or two to fill.

> **First boot on `debian:bookworm` is slower** — each GPU host installs
> `iproute2`/`python3` on first start. Subsequent operations are fast.

---

## Use the fabric

Node names follow `clab-srv6lab-<node>` (e.g. `clab-srv6lab-gpu1`,
`clab-srv6lab-leaf1`, `clab-srv6lab-spine1`).

### Confirm the NIC build and health

```bash
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic version
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic doctor
```

`doctor` prints the iproute2/kernel capability verdict, `seg6_enabled` per
interface, the installed decap routes, the localsid steering rules, and the
carrier-block route — everything needed to diagnose a decap problem in one shot.

### Test connectivity

Each GPU's `/etc/hosts` resolves fabric node names to their **fabric IPv6** (a
GPU's tenant address, a switch's loopback) rather than the mgmt network, so a
ping between GPUs **rides the SRv6 underlay**:

```bash
docker exec clab-srv6lab-gpu1 ping -c3 gpu2   # same leaf  -> native over the fabric
docker exec clab-srv6lab-gpu1 ping -c3 gpu3   # cross leaf -> SRv6-encapped (spine transit)
```

GPU host addresses follow `fd00:9:<plane>:<leaf><gpu>::2`. Use the address map
printed at generation time (or `fabric_address_map.txt`) to find any GPU.

### Trace the underlay

A traceroute to a GPU shows only the endpoint — the SRv6 tunnel hides the
transit hops (the core only decrements the *outer* header's hop-limit). To see
the real underlay path, trace a **switch loopback** (each GPU has routes to
them), which is natively routed:

```bash
docker exec clab-srv6lab-gpu1 mtr -6 -r -c3 leaf2-p1   # gpu1 -> leaf1 -> spine -> leaf2
```

---

## Inspect what the NIC is doing

### Show the path mesh

```bash
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic paths
```

```
mode=SRV6  peers=3  total_evs=5
  -> gpu2 (fd00:9:1:102::2)  [1 path]
       via same-leaf  fcbb:bb00:9001:2::
  -> gpu3 (fd00:9:1:201::2)  [2 paths]
       via spine1     fcbb:bb00:1101:e002:9002:1::
       via spine2     fcbb:bb00:1102:e002:9002:1::
  ...
```

### Decode each carrier hop-by-hop

```bash
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic paths --decode
```

Breaks every carrier into its component uSIDs and labels what each one does and
which node processes it (spine locator / End.X shift / GPU decap block).

### Live path health (mrc-probe)

Every NIC runs an always-on **`mrc-probe`** per path. Following the MRC
whitepaper, each burst is **source-routed** — `SO_MARK`-pinned to one SRv6 uSID
carrier, the exact path the data plane takes — so there is "no ambiguity about
which path a probe packet takes" (ground truth about the forwarding plane).
Against a small reflector on every peer (the deploy starts one) it measures the
per-path signals MRC reacts to:

| Signal | Field(s) | MRC use |
|--------|----------|---------|
| loss / liveness | `loss_pct`, up/down | bad path → EV removed; probes resurrect it |
| latency | `rtt_min/avg/max` | tail-latency / path cost |
| one-way delay | `ob` (outbound, the pinned path), `ib` (return) | localize direction |
| jitter | `rtt_mdev` | path quality |
| out-of-order | `reorder` | MRC expects & tolerates reordering |

If the reflector is absent it falls back to a built-in `SO_MARK`-pinned ICMPv6
probe (round-trip only). `status` shows the controller link, programmed EVs,
kernel route, and a full `MESH HEALTH` block, with any
[drained](#maintenance-drain) path marked:

```bash
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic status
```

```
  MESH HEALTH  10/10 paths up · swept now
    [✓] gpu2                     loss   0.0%   rtt 0.21 · jit 0.05 · 1-way 0.12/0.10 ms
    [✓] gpu3     via spine1-p1   loss   0.0%   rtt 0.23 · jit 0.06 · 1-way 0.12/0.11 ms
    [~] gpu4     via spine1-p1   DRAINED  loss   0.0%   rtt 0.25 · jit 0.06 · 1-way 0.13/0.12 ms
    ...
```

The GUI paths table surfaces the same columns (rtt / loss / one-way / jitter /
reorder). Loss feeds the NIC's adaptive-spray and auto-deny/restore loop — the
software analogue of MRC removing and resurrecting EVs. *Not* reproduced here
are the transport/silicon parts of MRC (ECN-based congestion control, SACK/NACK
loss recovery, per-packet spray, port-state bitmaps) and full Clustermapper-style
switch self-probing for T0/T1 fault localization.

### See the dumb core

```bash
docker exec clab-srv6lab-leaf1 vtysh -c "show running-config" | grep "ipv6 route"
docker exec clab-srv6lab-spine1 vtysh -c "show running-config" | grep "ipv6 route"
```

A leaf shows one `/64` per local GPU plus a single `fcbb:bb00:9000::/36`
aggregate to the spines; a spine shows one `/48` per leaf. Neither grows with the
total GPU count.

---

## Topology GUI

Bring up a browser-based topology viewer + live operations dashboard alongside
the lab:

```bash
cd ansible
ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e do_gui=true
```

Then open `http://<this-host>:8080/gui/topology.html`. It renders each plane as a
CLOS layer with the GPUs threading up into every plane, and provides:

- **Live test-probe health** — the paths table shows every path's forward/return
  RTT and loss, updated as each NIC reports (`up` / `down` / `drained`).
- **Path & link inspection** — hover a node or a path to pop a detail table
  showing each link's interface names + IPv6, plus (for a path) an **SRv6 carrier
  progression** box tracing how the outer uSID shifts hop-by-hop (encap → transit
  → `End.X` shift at the spine → `End.DT6` decap).
- **Per-node config** — click a node to view its `frr.conf` / NIC profile; the
  header has a one-click **download of all configs** as a `.tar.gz`.
- **Node console** (`-e gui_console=true`) — an in-browser terminal (xterm.js over
  a websocket to `docker exec`) plus a quick command runner per node.
- **Maintenance drain** — see [below](#maintenance-drain).
- **Edgeshark link** — a header button opens [Edgeshark](#live-packet-capture-edgeshark).
- Resizable inspector / paths panels.

See [`gui/README.md`](gui/README.md).

> **`gui_console` mounts the host Docker socket** into the GUI container so the
> web UI can exec into nodes — it grants that container control of the host's
> Docker, so only enable it on a trusted lab host.

---

## Maintenance drain

Select a **node** (click it → **Bypass**) or a whole **plane** (click the plane
label) in the GUI to drain it for maintenance: every NIC steers its data traffic
(the weighted SRv6 spray) off any path transiting the drained node/plane within
~2 s, **while still probing those paths** so you can see when it is healthy and
safe to restore. Drained paths are marked `drained` in the GUI and `[~] DRAINED`
in `mrc-nic status` / `mrc-nic paths`. A last-path guard prevents a full drain
(e.g. draining the only plane) from black-holing a flow.

The same works over the API:

```bash
curl -X POST http://<host>:8080/api/bypass -d '{"node":"spine1-p1","on":true}'
curl -X POST http://<host>:8080/api/bypass -d '{"plane":2,"on":true}'
curl -X POST http://<host>:8080/api/bypass -d '{"planes":[]}'          # restore all
```

---

## Live packet capture (Edgeshark)

Deploy [Siemens Edgeshark](https://github.com/siemens/edgeshark) for live
per-link packet capture across every container in the fabric. It is a built-in
option of the playbook (compose stack in `ansible/files/edgeshark-compose.yml`,
auto-detecting `docker compose` v2 or the classic `docker-compose`):

```bash
ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 \
  -e do_gui=true -e do_edgeshark=true          # -e edgeshark_port=5001 to change the port
```

Open `http://<this-host>:5001` (the GUI header also links to it). Edgeshark
discovers every clab node's interfaces and hands a chosen link off to Wireshark
in the browser — install the one-time **Edgeshark browser extension**
(`cshargextcap`) for the Wireshark handoff; the in-browser topology/interface
view works without it.

You can also launch a capture straight from the topology GUI: hover a node or a
path, and in the link-detail table click the **🦈** on either end of a link to
open Wireshark on that node's interface (needs `-e gui_console=true` so the GUI
can read the container netns).

> Edgeshark runs privileged host-level containers (`pid:host` + scoped caps), so
> it is **lab-only** and off by default.

---

## Tear the lab down

`containerlab destroy` removes the fabric nodes (gpu/leaf/spine); the GUI and
Edgeshark are separate **host** containers, so tear those down too:

```bash
cd <repo-root>
sudo containerlab destroy -t srv6lab.clab.yml --cleanup   # fabric nodes + lab dir
docker rm -f mrc-gui 2>/dev/null || true                  # topology GUI (if do_gui)
docker compose -p edgeshark down 2>/dev/null || true      # Edgeshark (if do_edgeshark)
docker network rm srv6lab-mgmt 2>/dev/null || true        # mgmt net, if it lingers
```

`--cleanup` removes the lab directory and per-node state. Alternatively, let the
playbook tear the extras down — re-run it with them disabled:

```bash
cd ansible
ansible-playbook site-frr.yml -e do_gui=false -e do_edgeshark=false
```

**Switching plane count / redeploying:** destroy first, then run the
`ansible-playbook` command again — only one topology (`srv6lab.clab.yml`) exists
at a time, and the NIC binary is bind-mounted, so a container recreate is
required to pick up a new build.

---

## How it scales

The leaf-nested uSID layout `fcbb:bb00:9LLL:GGGG::/64` (where `LLL` = home-leaf
index, `GGGG` = local GPU index) lets every tier aggregate:

- **Spine**: one `/48` per leaf — `O(leaves)`.
- **Leaf**: one specific `/64` per *local* GPU + one `/36` aggregate for *all*
  remote GPUs — `O(local GPUs + 1)`.

Measured on a 64-GPU fabric (8 leaves x 8 GPUs):

| Node | Naive (per-GPU) | This design |
|------|-----------------|-------------|
| a leaf's GPU routes | 64 | **9** |
| a spine's GPU routes | 64 | **8** |

At 4096 GPUs those counts are unchanged. Adding GPUs to other leaves changes
nothing on a given leaf or any spine.

### Plan a fabric — `plan_fabric.py`

Don't work out `leaves`/`spines`/`gpus_per_leaf` by hand. Tell the planner your
GPU count, ports per leaf, port speed and per-NIC throughput (breakout cables
split one high-speed leaf port into several NIC-facing links), and it derives the
CLOS shape, checks the address-plan caps, and prints the exact `ansible-playbook`
command:

```bash
python3 plan_fabric.py                       # interactive
python3 plan_fabric.py --gpus 10000 --ports-per-leaf 64 \
  --port-speed 800 --nic-speed 200 --planes 4 --dry-run
```

`--dry-run` generates the configs (small fabrics for real; huge ones projected
from a tiny reference build) and reports the on-disk config size. It surfaces the
real scaling walls: mgmt IPv4 `/22` (hard-fails past ~256 GPUs), the per-plane
mgmt stride (leaf/spine collide past ~20 switches/plane when multi-plane), the
255-per-plane index cap, and — the big one — the **all-to-all EV set**: the
generator materialises one uSID carrier per **(src GPU × dst GPU × plane ×
spine)**, so profile + mesh configs grow **O(GPUs²·planes·spines)** (≈ 8 TB at
10,000 GPUs, while the switch configs stay a few MB). That product, not the
switches, is what dominates at scale.

That all-to-all dump is a lab convenience (so the GUI can draw every path), not
how a connection-oriented MRC edge actually holds state. Model a real training
run instead with:

- `--sparse K` — cap each GPU to `K` live connections (its ring / TP / PP / EP
  peers), not all N. EV state becomes **linear** in GPU count.
- `--spray N` — cap cross-leaf paths-per-flow to `N` EVs (a bounded fan-out
  instead of one per spine).

```bash
python3 plan_fabric.py --gpus 10000 --ports-per-leaf 64 \
  --port-speed 800 --nic-speed 200 --planes 4 --sparse 32 --spray 4 --dry-run
```

The planner prints full-mesh vs sparse side by side. Examples: 10,000 GPUs
`--sparse 32 --spray 4` ≈ **2,500× smaller**; 1,000 GPUs `--sparse 16 --spray 2`
drops ~38 GB → ~80 MB. That’s the difference between the theoretical mesh and the
per-connection EV set MRC actually keeps.

### Computed carriers — `--compute-carriers` (formula, not table)

The planner *reports* the reduction; `--compute-carriers` on the generator
*builds* it. Because every address is position-encoded, a GPU's whole EV set is
**derivable** from a formula rather than enumerated:

```
carrier (spec, cross-leaf) = block : <spine RPII> : e000+leaf : 9LLL : GGGG : 0 : 0
    RPII(role,plane,idx) = role·0x1000 + plane·0x100 + idx   (spine=1, leaf=2)
    9LLL = 0x9000 + dst_leaf     GGGG = dst_local_gpu_index
```

In this mode each `mrc-nic/<gpu>.json` is a **compact fabric descriptor** (~800
bytes: block + shape + this GPU's index) instead of the fully-enumerated table.
The shared [`mrc_usid.py`](mrc_usid.py) `expand()` reconstructs the *identical*
carriers at NIC load — verified byte-for-byte against the materialised output — so
runtime behaviour is unchanged: multi-plane transit, per-spine paths, live
probing, and maintenance **drain** all still work (the computed EVs carry the same
`spine`/`plane` labels drain matches on). On disk it's **O(1) per GPU** instead of
O(GPUs²·planes·spines) — 10,000-GPU NIC state drops from ~TB to a few MB.

```bash
ansible-playbook site-frr.yml -e planes=4 -e leaves=... -e spec_mode=true \
  -e gpu_image=debian:bookworm -e compute_carriers=true      # optional: -e sparse=32 -e spray=4
```

This is the connection-oriented model from the MRC whitepaper: the edge holds a
formula + fabric map and computes carriers, rather than storing an all-to-all
table. `--sparse`/`--spray` compose with it to emit only a training-run's live
peer set.

---

## Troubleshooting

**`mrc-nic doctor` says iproute2 too old / decap won't fire.**
You deployed `spec_mode=true` with a GPU image older than iproute2 6.1. Use
`-e gpu_image=debian:bookworm` (or newer). The default leaf-decap mode has no
such requirement.

**Cross-leaf ping fails but same-leaf works (spec mode).**
Check `mrc-nic doctor` on both endpoints for the three required pieces: the
prio-50 localsid rule, the `End.DT6` route in table 100, and the
`fcbb:bb00::/32` carrier-block route. The NIC installs all three; if one is
missing, `doctor` shows which.

**containerlab: "Subnet ... already in use" / "overlap an existing Docker network".**
Another containerlab lab is holding a subnet inside the fabric's `172.20.20.0/22`
management range. Either remove the other lab's network
(`docker network rm <name>`), or deploy the fabric on a different mgmt subnet:

```bash
ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 \
  -e mgmt_subnet=172.30.0.0/22
```

**A `spec_mode=true` deploy is slow to come up.**
Expected on first boot — `debian:bookworm` GPU hosts install iproute2/python3 on
start. A prebuilt GPU image avoids this.

---

## Repository layout

```
gen_clab_topology.py   topology + FRR + NIC-profile generator (also emits the
                       IPv6 /etc/hosts, underlay loopback routes, per-GPU config)
address_plan.json      addressing scheme (block, locators, GPU uSID layout)
nic/
  mrc-nic              virtual NIC: SRv6 source, per-path carriers, End.DT6 decap,
                       always-on test probe + weighted spray + maintenance drain
  mrc-probe            per-path health probe: source-routed reflector + client
                       (loss, RTT, one-way delay, jitter, out-of-order)
  mrc-meshprobe        standalone full-mesh probe helper
  mrc-traffic          MRC-aware synthetic UDP traffic generator (+ mrc-sink)
  mrc-sink             traffic sink
gui/
  server.py            GUI backend: probe-health aggregate + config / bypass /
                       exec / websocket-console API (stdlib only)
  topology.html        the dashboard (topology, live paths, inspector, console)
  vendor/              xterm.js (vendored, MIT) for the in-browser console
ansible/
  site-frr.yml         main deploy playbook (fabric + GUI + Edgeshark)
  files/edgeshark-compose.yml  Siemens Edgeshark stack for live per-link capture
  roles/sonic_frr/     switch (FRR) config role
  roles/gpu_host/      GPU host bootstrap (image-agnostic iproute2/python3)
  group_vars/all.yml   defaults (spec_mode, do_gui, gui_console, do_edgeshark, ...)
scripts/
  srv6-test.sh         end-to-end connectivity test
  srv6-walk.sh         hop-by-hop SRv6 walk
examples/              example NIC profiles
docs/
  MRC_SRV6_DESIGN.md   engineering design notes + the solved decap recipe
  MRC_NIC_README.md    mrc-nic reference
  QUICKSTART.md        condensed quick start
  SRv6_Stateless_Core_Whitepaper.docx   the network-engineer whitepaper
```

---

## Status

Reference / research design, validated end-to-end on an all-Linux fabric (Linux
kernel 6.8, FRR 10.6, iproute2 6.1). The virtual NIC demonstrates the addressing,
forwarding, scaling, and state model, plus a software emulation of the control
loop — always-on per-path probing, adaptive weighted spray, and maintenance
drain — surfaced through the GUI. True per-packet spray and hardware-timescale
congestion-reactive rebalancing are properties of smart-NIC silicon and are
approximated, not reproduced, here.

## License

[MIT](LICENSE).
