# MRC-style SRv6 uSID Fabric (all-Linux reference)

A reference implementation of an **MRC-style AI/GPU fabric** built entirely on
Linux: SRv6 micro-SID (uSID) source routing where the **NIC chooses the path**
and writes it into the IPv6 destination address, and the **core does nothing but
plain longest-prefix IPv6 forwarding**. No MPLS, no EVPN, no per-endpoint state
in the spine or leaf.

The design, the scaling argument, and the multi-plane / per-packet-spray
discussion are written up for a traditional network engineer in
[`docs/SRv6_Stateless_Core_Whitepaper.docx`](docs/SRv6_Stateless_Core_Whitepaper.docx).
The full engineering design notes are in
[`docs/MRC_SRV6_DESIGN.md`](docs/MRC_SRV6_DESIGN.md).

## What this is

- An **all-Linux FRR + containerlab** fabric (spines, leaves, GPU hosts) that
  forwards SRv6 uSID traffic end to end.
- A software **virtual NIC** (`nic/mrc-nic`) that sources the SRv6 encapsulation,
  pins one carrier per path, and performs terminal `End.DT6` decapsulation on the
  destination host — standing in for smart-NIC silicon.
- A topology **generator** (`gen_clab_topology.py`) that emits the containerlab
  topology, the FRR configs, and the per-GPU NIC profiles from a single address
  plan, with route **aggregation** so core state is invariant to GPU count.

## Two forwarding modes

| Mode | Flag | Decap location | Notes |
|------|------|----------------|-------|
| Leaf-decap (proven) | `spec_mode=false` (default) | leaf (`End.DT6`) | Simplest; host-sources SRv6, leaf decaps. |
| NIC-decap (spec) | `spec_mode=true` | destination NIC | Full MRC model. Needs kernel ≥ 6.1 and iproute2 ≥ 6.1. |

Both are validated on the wire on Linux kernel 6.8 / FRR 10.6 / iproute2 6.1.

## Scaling: the core stays dumb

A leaf-nested uSID address plan (`fcbb:bb00:9LLL:GGGG::/64`) lets every tier
aggregate, so routing state is a function of fabric **shape**, not GPU **count**:

- **Spine**: one `/48` per leaf (`O(leaves)`).
- **Leaf**: one specific `/64` per *local* GPU + one `/36` aggregate for all
  remote GPUs (`O(local GPUs + 1)`).

Measured at 64 GPUs (8 leaves × 8): a leaf holds 9 GPU routes (not 64), a spine
holds 8. At 4096 GPUs those counts are unchanged.

## Quick start

Requires: a Linux host with Docker + [containerlab](https://containerlab.dev),
plus `ansible`. See [`docs/QUICKSTART.md`](docs/QUICKSTART.md) for detail.

```bash
# generate + deploy the proven (leaf-decap) fabric: 2 leaves, 2 spines, 2 GPUs/leaf
cd ansible
ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2

# or the full MRC spec fabric (NIC-side decap)
ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 \
  -e spec_mode=true -e gpu_image=debian:bookworm
```

Verify and test:

```bash
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic version
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic doctor
docker exec clab-srv6lab-gpu1 /usr/local/bin/mrc-nic paths --decode
docker exec clab-srv6lab-gpu1 ping6 -c3 fd00:9:1:201::2
```

Tear down:

```bash
sudo containerlab destroy -t srv6lab.clab.yml --cleanup
```

## The NIC (`mrc-nic`)

```
mrc-nic run      --profile <gpu.json>   # source SRv6, install per-path carriers + decap
mrc-nic paths    [--decode]             # show the carriers per peer; --decode walks each uSID
mrc-nic doctor                          # dump everything relevant to SRv6 decap in one shot
mrc-nic version
```

`doctor` reports the iproute2/kernel capability, `seg6_enabled` per interface, the
installed decap routes, the localsid steering rules, and the carrier-block route —
so a misconfiguration is diagnosable in one command.

## Repository layout

```
gen_clab_topology.py   topology + FRR + NIC-profile generator
address_plan.json      the addressing scheme (block, locators, GPU uSID layout)
nic/                   mrc-nic (virtual NIC), mrc-probe, mrc-sink
ansible/               site playbooks + roles (sonic_frr, gpu_host)
scripts/               srv6-test.sh, srv6-walk.sh helpers
examples/              example NIC profiles
docs/                  design notes, NIC readme, quickstart, whitepaper
```

## Status

Reference / research design. Validated end-to-end on an all-Linux fabric. The
virtual NIC demonstrates the addressing, forwarding, scaling, and state model;
production per-packet spray and congestion-reactive rebalancing are properties of
smart-NIC silicon, not reproduced here.

## License

See [LICENSE](LICENSE).
