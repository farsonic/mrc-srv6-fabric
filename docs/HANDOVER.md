# HANDOVER — MRC-style SRv6 uSID Fabric

**Date:** 2026-06-26
**Owner:** François Prowse (GitHub/DockerHub: `farsonic`), Brisbane
**Repo:** https://github.com/farsonic/mrc-srv6-fabric  (private)
**Current HEAD:** `4ae86c7` — "feat: containerized topology GUI as an optional service"
**NIC build stamp:** `2026-06-25-g13-aggregated-routes`

This document is a complete state-of-play for a new session. It assumes the
reader knows networking but has no prior context on this project. Read the
"Mental model" and "Gotchas" sections first — they prevent the mistakes that cost
hours.

---

## 1. What this project is

An **all-Linux reference implementation of an MRC-style AI/GPU fabric**. The
defining property: the **NIC chooses the entire path** and writes it into the
IPv6 destination address as a stack of SRv6 micro-SIDs (uSID); the **core (spines
+ leaves) does nothing but plain longest-prefix IPv6 forwarding**. No MPLS, no
EVPN, no per-endpoint state in the core. The core's routing table is a function
of the fabric's *shape* (leaves + spines), never the *number of GPUs*.

It runs as a **containerlab** topology of FRR switches and Linux "GPU host"
containers, deployed via **Ansible**. A software **virtual NIC** (`nic/mrc-nic`,
Python) stands in for smart-NIC silicon: it sources the SRv6 encapsulation, pins
one carrier per path, and performs terminal `End.DT6` decapsulation.

Everything has been **validated end-to-end on the wire** on Linux kernel 6.8 /
FRR 10.6 / iproute2 6.1.

---

## 2. Mental model (read this)

### The address IS the path
A uSID carrier is `block : uSID : uSID : … :: `. This project uses block
`fcbb:bb00::/32` (F3216 format: 32-bit block, 16-bit uSIDs). After the block,
each 16-bit hextet is one instruction. A switch matches the **first** uSID
against its own locator; if it matches it performs the behaviour and **shifts**
the address left (next uSID becomes active), then forwards. If it doesn't match,
it just longest-prefix-forwards. Behaviours used:
- `uN`  — transit waypoint (spine locator); shift + forward.
- `uA`/`End.X` — shift + send out a specific link toward a neighbour (spine →
  destination leaf). The locator+adjacency are a pair; the shift pops both.
- `End.DT6` — final SID; strip outer header, deliver inner. **Only the
  destination NIC does this.** No switch decapsulates.

### Address plan (leaf-nested, this is what makes it scale)
- Switch locator hextet `RPII`: R=role(1=spine,2=leaf), P=**plane**, II=index.
  e.g. spine1 plane1 = `1101`; spine1 plane2 = `1201`; leaf1 plane1 = `2101`.
  **The 2nd nibble is the plane** — visible directly in any carrier.
- GPU decap SID: `fcbb:bb00:9LLL:GGGG::/64` — nibble `9` = GPU block, `LLL` =
  home-leaf index, `GGGG` = local GPU index. So every leaf's GPUs share
  `fcbb:bb00:9LLL::/48`.
- Underlay addressing: switch loopback `fd00:0:0:RPII::1/128`; p2p
  `fd00:1:plane:LLSS::/127`; GPU `fd00:9:plane:LLGG::/64` (GPU = `::2`, leaf gw
  = `::1`). GPU tenant address e.g. gpu1 = `fd00:9:1:101::2`.

### Why the core stays small (the scaling claim, exactly)
- **Spine**: one `/48` per leaf (`fcbb:bb00:9LLL::/48` → that leaf). O(leaves).
- **Leaf**: one specific `/64` per LOCAL gpu → access port, PLUS one `/36`
  aggregate (`fcbb:bb00:9000::/36` → spines, ECMP) for ALL remote GPUs.
  O(local gpus + 1).
- LPM ties it together: remote-GPU packet hits the leaf `/36` → spine; spine's
  `/48` → home leaf; home leaf's `/64` → GPU port; GPU End.DT6-decaps.
- Measured at 64 GPUs (8 leaves×8): a leaf holds **9** GPU routes (not 64), a
  spine holds **8**. At 4096 GPUs those counts are unchanged.

### Life of a packet (cross-leaf, gpu1→gpu3)
1. gpu1 NIC encaps, outer DA = `fcbb:bb00:1102:e002:9002:1::` (via spine2).
2. leaf1 plain-forwards on `1102::/48` → spine2.
3. spine2 End.X matches `1102:e002`, **shifts both out** → DA = `fcbb:bb00:9002:1::`.
4. leaf2 (home of gpu3) plain-forwards local `/64` → gpu3 port.
5. gpu3 NIC: DA is its own decap SID → End.DT6 → deliver inner.
Leaf never touches the SRH. Spine does one shift. NIC decaps. No switch holds a
GPU route.

---

## 3. THE solved problem (most important technical result)

**NIC-side `End.DT6` decap of a transit-delivered uSID packet works on kernel
6.8**, but only with the right recipe. This cost a long debugging session; do not
re-derive it. A bare `End.DT6 table main dev mrc0` route **does not fire** — the
arriving packet has a non-local DA, so the kernel sends it down the FORWARD path
and never hands it to seg6local (counter stays 0, `Ip6InDiscards` climbs).

**The recipe (now baked into `mrc-nic install_decap`):**
1. **prio-50 FIB rule** steering the SID prefix into a dedicated localsid table:
   `ip -6 rule add prio 50 to <sid>/64 lookup 100`. THIS is the piece that gets
   the packet into seg6local input. Without it → discarded at IPv6 input.
2. **End.DT6 in table 100 with inner lookup in table 255 (local)**:
   `ip -6 route add <sid>/64 table 100 encap seg6local action End.DT6 count table 255 dev eth1`.
   `table 255` delivers to the local stack (ICMP responds); `table main` errors.
3. **Carrier-block route** `fcbb:bb00::/32 via <leaf-gw>` so the GPU's own
   encapped replies can egress (else "Network is unreachable" on send).

Diagnostic instrument: the seg6local `packets/errors` counter
(`ip -s -6 route show table 100`). Requires kernel ≥6.1 AND iproute2 ≥6.1.
`mrc-nic doctor` dumps all three pieces + the capability verdict in one shot.

Full forensic write-up: `docs/MRC_SRV6_DESIGN.md` (verdict = SOLVED).

---

## 4. Repository contents

```
gen_clab_topology.py   Generator: emits the containerlab topology + per-node
                       frr.conf + per-GPU NIC profiles from address_plan.json.
                       Has --mgmt-subnet override, --spec-mode, --planes, etc.
address_plan.json      The addressing scheme (block, locators, GPU uSID layout,
                       mgmt subnet 172.20.20.0/22 by default).
nic/
  mrc-nic              Virtual NIC (Python). Build g13. Subcommands:
                       run, status, paths [--decode], version, doctor, test,
                       monitor, mesh-test.
  mrc-probe            Per-path probe/traffic generator.
  mrc-sink             Traffic sink.
ansible/
  site-frr.yml         MAIN deploy playbook (generate → deploy → start NIC →
                       optional Edgeshark → optional GUI).
  group_vars/all.yml   Defaults: spec_mode, mrc_nic, do_edgeshark(false),
                       do_gui(false), gui_port(8080), gui_image, etc.
  roles/sonic_frr/     Switch (FRR) config role + templates.
  roles/gpu_host/      GPU host bootstrap (image-agnostic iproute2/python3).
gui/
  topology.html        Topology viewer (no deps) + a paths table beneath it that
                       shows every (src,dst,plane,spine) carrier with live
                       test-probe health (RTT/jitter/loss), symmetric fwd/return,
                       hover to overlay the carrier on the topology.
  server.py            Backend (stdlib): serves the repo + the control-plane the
                       NICs attach to (/api/topology, /api/mesh-plan,
                       /api/mesh-health collect, /api/mesh aggregate for the page).
  README.md            GUI usage (container + standalone) + the live-data flow.
scripts/
  srv6-test.sh         End-to-end connectivity test (generated copy also lands
  srv6-walk.sh         hop-by-hop SRv6 walk            at repo root on deploy).
docs/
  MRC_SRV6_DESIGN.md   Engineering design notes + the SOLVED decap recipe + the
                       route-aggregation scheme. The canonical technical doc.
  MRC_NIC_README.md    mrc-nic reference.
  QUICKSTART.md        Condensed quick start.
  SRv6_Stateless_Core_Whitepaper.docx   Network-engineer whitepaper (see below).
examples/              Example NIC profiles.
```

### Git history (6 commits)
```
4ae86c7 feat: containerized topology GUI as an optional service (-e do_gui=true)
3ad785d feat: simple topology viewer GUI (gui/topology.html)
3964452 fix: Edgeshark optional & compose-flavour tolerant
19d3869 feat: --mgmt-subnet override to avoid mgmt network collisions
e1cc2f7 docs: comprehensive operational README (bring-up, use, tear-down)
e729fdd Initial commit
```
NOTE: GitHub was force-pushed to this exact history. Both the lab host clone and
the Mac clone were reset to it. If a clone shows different hashes, it diverged —
`git fetch && git reset --hard origin/master`.

---

## 5. How to deploy / use / tear down

Run from `ansible/`. The playbook generates everything, deploys containerlab,
starts the NIC on each GPU. Two modes: leaf-decap (`spec_mode=false`, default,
simplest) and NIC-decap (`spec_mode=true`, full MRC, needs kernel/iproute2 ≥6.1).

**Deploy (current target shape — 2 planes, 2 spines, 2 leaves, 2 gpu/leaf):**
```bash
cd ansible
ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e planes=2 \
  -e spec_mode=true -e gpu_image=debian:bookworm \
  -e do_gui=true -e mgmt_subnet=172.30.0.0/22
```

**Deploy params:** `gpus_per_leaf`, `leaves`, `spines`, `planes` (default 1),
`spec_mode`, `gpu_image` (use `debian:bookworm` for spec_mode — ships iproute2
6.1), `switch_image`, `mgmt_subnet` (override to dodge collisions),
`do_gui` (false), `do_edgeshark` (false).

**Tear down:**
```bash
cd ~/mrc-srv6-fabric
sudo containerlab destroy -t srv6lab.clab.yml --cleanup
docker rm -f mrc-gui 2>/dev/null || true
docker network rm srv6lab-mgmt 2>/dev/null || true
```

**Verify:**
```bash
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic version   # expect g13
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic doctor
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic paths --decode
docker exec clab-srv6lab-gpu1 ping6 -c3 fd00:9:1:201::2        # cross-leaf gpu1->gpu3
```

**GPU address map:** `fd00:9:<plane>:<leaf><gpu>::2`. gpu1=`fd00:9:1:101::2`,
gpu3=`fd00:9:1:201::2`, gpu5=`fd00:9:1:301::2`, etc. Home-leaf = `(g-1)//gpus_per_leaf + 1`.

**Topology GUI:** with `-e do_gui=true`, an `mrc-gui` container (python:3.12-slim)
serves the repo root. Open `http://<host>:8080/gui/topology.html`. It read-only
bind-mounts the repo so it always shows the current `fabric_vars.json`. Renders
each plane as a CLOS band; GPUs thread up into every plane; hover highlights a
node's links, click pins its details. SSH tunnel if needed:
`ssh -L 8080:localhost:8080 fprowse@containerlab`.

---

## 6. GOTCHAS (these cost time — read before deploying)

1. **mgmt subnet collisions.** Default mgmt is `172.20.20.0/22`. Other
   containerlab labs (e.g. `twoplane-mgmt` 172.20.20.0/24) sit inside it and
   cause "overlap an existing Docker network". Fix: remove the other network
   (`docker network rm <name>`) or `-e mgmt_subnet=172.30.0.0/22`.

2. **`spec_mode=true` needs a modern GPU image.** Use
   `-e gpu_image=debian:bookworm` (iproute2 6.1). The old Alpine
   network-multitool ships iproute2 5.6 which **silently fails** uSID decap.
   First boot is slow — each GPU apt-installs iproute2/python3.

3. **NIC binary is bind-mounted → redeploy to refresh.** Editing `nic/mrc-nic`
   does NOT update a running container; you must destroy + redeploy. Always
   verify the build stamp first: `mrc-nic version` → expect `g13`.

4. **Edgeshark needs `docker compose` v2.** The repo defaults `do_edgeshark=false`
   and is now tolerant of v1/v2, but if you enable it on a host with only classic
   `docker-compose`, install `docker-compose-plugin`.

5. **Generated artifacts are NOT in git** and clutter the working dir
   (`fabric_vars.json`, `frr/`, `clab-srv6lab/`, `mrc-nic/` profile dir,
   `srv6-*.sh`, `*.clab.yml`). They're gitignored/regenerated. Don't commit them.
   The GUI needs `fabric_vars.json` at the repo root — present after any deploy,
   or regenerate with the generator directly.

6. **Multiple stale clones bite.** During handover there were 3 Mac copies in
   different folders, only one current. Keep ONE clone per machine; if unsure,
   `git fetch && git reset --hard origin/master`.

7. **Switches lack tcpdump by default** (`apk add --no-cache tcpdump`); `any`
   device fails promiscuous in these containers. For on-wire capture prefer the
   Edgeshark GUI or per-interface tcpdump (`-nni ethN ... ip6`, grep `srcrt`).

---

## 7. Current state / where we are

- **6-commit repo on GitHub**, lab host + Mac clones aligned to `4ae86c7`.
- **NIC build g13** with route aggregation + the solved localsid decap recipe.
- **Both fabrics proven on the wire** (single plane): leaf-decap and NIC-decap,
  cross-leaf + same-leaf, 0% loss.
- **Route aggregation proven** on François's hardware (leaf = local /64s + one
  /36; spine = one /48 per leaf), forwarding through the /36→/48→/64 LPM chain.
- **Topology GUI** built and containerized (`-e do_gui=true`), now with a live
  **paths table** (`gui/server.py` backend): every (src,dst,plane,spine) carrier
  with per-path test-probe health (RTT/jitter/loss), shown symmetrically, hover
  to overlay the carrier. Generator emits the carrier list as `paths[]` in
  `fabric_vars.json`; NICs attach with `-e mesh_controller_url=...` and POST
  health to `/api/mesh-health`. Per-path probe uses TWAMP when `mrc-twamp` is
  present, else the NIC's built-in `SO_MARK`-pinned raw ICMPv6 (no extra binary).
- **Whitepaper** (`docs/…docx`) for a traditional network engineer: stateless
  core, scaling, multi-plane (with concrete plane-distinguished carriers),
  per-packet spray, probe-based path health. Framed against routed-CLOS+ECMP
  (not MPLS — nobody runs MPLS in the DC) and VXLAN-EVPN only as the
  "if multi-tenant" option.
- **Last action in progress:** scaling the lab to **2 planes × (2 spines, 2
  leaves, 2 gpu/leaf)**. Shape generates correctly (4 spine, 4 leaf, 4 gpu, 16
  links; each GPU homes on its leaf in BOTH planes). Deploy command is in §5.
  NOTE: cross-leaf NIC-decap was wire-proven single-plane; two-plane carriers are
  structurally identical (plane only changes the spine-locator nibble) so should
  forward the same, but the **2-plane cross-leaf forward is not yet wire-confirmed**
  — verify with the §5 ping; if it fails, `mrc-nic doctor` on both ends shows
  which decap piece is missing.

---

## 8. Open / deferred items (offered, not built)

- **Prebuilt slim GPU image** (iproute2 6.1 + python3 + tcpdump baked in) to kill
  the per-boot apt cost before scaling toward many GPUs. Short Dockerfile + a
  `-e gpu_image=` swap. Becomes worthwhile past ~8 GPUs in spec mode.
- **GUI backend — BUILT (`gui/server.py`).** No longer a static-only seam: it
  serves the repo AND the NIC control-plane (`/api/topology`, `/api/mesh-plan`,
  `/api/mesh-health`, `/api/mesh`), and the page shows the live per-path test-probe
  table with the carrier overlay. Still open on this seam:
  (a) `/api/status` polling docker/containerlab for live node up/down → colour the
  topology nodes green/red (the paths table already colours per-path health, but
  the node boxes don't yet reflect container state);
  (b) wire-validate the live pipeline end-to-end on the lab — the backend +
  generator + NIC contract are unit-tested locally (synthetic POSTs render the
  table correctly) but have NOT yet been run against real attached NICs. Deploy
  with `-e mesh_controller_url=http://<mgmt-gw>:8080`, confirm `/api/reports`
  fills, and confirm the table goes live. If a NIC can't reach the URL, check the
  mgmt-net gateway IP the GPU containers actually see.
- **GUI viewer polish:** pan/zoom for large fabrics (gets cramped past ~64 GPUs),
  per-plane collapse toggle.
- **Traffic-level validation** with iperf3/mrc-probe (exercise per-EV fwmark
  spray under load; ping only tests unmarked main-table encap).
- **2-plane cross-leaf wire validation** (see §7).
- **End.X decode label** currently shows the raw neighbour index ("toward
  neighbor 2") not the resolved leaf name. Could stamp the resolved leaf name
  into EV path metadata. Cosmetic.

---

## 9. Standing preferences (the owner's working style)

- Commands shown **inline, one per line, ready to paste** — never download-only.
- Get **on-box ground truth** (counters, captures, `show run`) before deciding.
- **Avoid shell-inlined python/awk with nested quotes**; use heredocs/argv.
- Verify build stamps before testing (`mrc-nic version`).
- Self-asserting patches; explain the reasoning, not just the change.

---

## 10. Environment quick facts

- Lab host: `containerlab` (user `fprowse`), Ubuntu, kernel 6.8.0-x-generic,
  containerlab 0.76.0. Repo clone at `~/mrc-srv6-fabric`.
- Mac: clone under `~/Documents/mrc-srv6-fabric` (keep ONE; others were stale).
- Images present: `quay.io/frrouting/frr:10.6.1`, `debian:bookworm`,
  `python:3.12-slim`, plus various SONiC VS / Enterprise images (separate
  two-box SONiC lab experiments — not part of this fabric).
- Container naming: `clab-srv6lab-<node>` (e.g. `clab-srv6lab-gpu1`,
  `clab-srv6lab-leaf1`, `clab-srv6lab-spine1`). GUI container: `mrc-gui`.
- Switch image is FRR (the project moved to all-Linux FRR; the `sonic_frr` role
  name is historical — it configures FRR).
