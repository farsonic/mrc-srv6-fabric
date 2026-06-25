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
3. **decapsulates** (`End.DT6`) its own arriving traffic.

The switches only ever do plain IPv6 longest-prefix forwarding on `/48` / `/36`
prefixes. A leaf-nested uSID address plan keeps core routing state proportional to
the **fabric** (leaves + spines), never to the **number of GPUs**.

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
| `planes` | number of parallel fabric planes | `1` (default), `2` |
| `spec_mode` | `true` = NIC decap; `false` = leaf decap | `true` |
| `gpu_image` | GPU host container image | `debian:bookworm` |
| `switch_image` | FRR switch image | `quay.io/frrouting/frr:10.6.1` |

A larger example (8 leaves x 8 GPUs, 4 spines, spec mode):

```bash
ansible-playbook site-frr.yml \
  -e gpus_per_leaf=8 -e leaves=8 -e spines=4 \
  -e spec_mode=true -e gpu_image=debian:bookworm
```

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

Same-leaf (gpu1 -> gpu2) and cross-leaf (gpu1 -> gpu3):

```bash
docker exec clab-srv6lab-gpu1 ping6 -c3 fd00:9:1:102::2   # gpu2 (same leaf)
docker exec clab-srv6lab-gpu1 ping6 -c3 fd00:9:1:201::2   # gpu3 (cross leaf)
```

GPU host addresses follow `fd00:9:<plane>:<leaf><gpu>::2`. Use the address map
printed at generation time (or `fabric_address_map.txt`) to find any GPU.

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

### See the dumb core

```bash
docker exec clab-srv6lab-leaf1 vtysh -c "show running-config" | grep "ipv6 route"
docker exec clab-srv6lab-spine1 vtysh -c "show running-config" | grep "ipv6 route"
```

A leaf shows one `/64` per local GPU plus a single `fcbb:bb00:9000::/36`
aggregate to the spines; a spine shows one `/48` per leaf. Neither grows with the
total GPU count.

---

## Tear the lab down

```bash
cd <repo-root>
sudo containerlab destroy -t srv6lab.clab.yml --cleanup
docker network rm srv6lab-mgmt 2>/dev/null || true
```

`--cleanup` removes the lab directory and the per-node state. The
`docker network rm` clears the management network if it lingers.

To redeploy after editing the generator or NIC, destroy first, then run the
`ansible-playbook` command again — the NIC binary is bind-mounted, so a redeploy
(container recreate) is required to pick up a new build.

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

**containerlab: "Subnet ... already in use".**
Another lab is using the default management subnet. Destroy the other lab, or set
a different mgmt subnet in the generated `srv6lab.clab.yml`.

**A `spec_mode=true` deploy is slow to come up.**
Expected on first boot — `debian:bookworm` GPU hosts install iproute2/python3 on
start. A prebuilt GPU image avoids this.

---

## Repository layout

```
gen_clab_topology.py   topology + FRR + NIC-profile generator
address_plan.json      addressing scheme (block, locators, GPU uSID layout)
nic/
  mrc-nic              virtual NIC: SRv6 source, per-path carriers, End.DT6 decap
  mrc-probe            traffic generator (per-path probing)
  mrc-sink             traffic sink
ansible/
  site-frr.yml         main deploy playbook
  roles/sonic_frr/     switch (FRR) config role
  roles/gpu_host/      GPU host bootstrap (image-agnostic iproute2/python3)
  group_vars/all.yml   defaults (mrc_nic, spec_mode, ...)
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
forwarding, scaling, and state model; production per-packet spray and
congestion-reactive rebalancing are properties of smart-NIC silicon and are not
reproduced here.

## License

[MIT](LICENSE).
