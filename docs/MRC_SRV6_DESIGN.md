# MRC-Faithful SRv6 Fabric — Design of Record

Target: rebuild the all-Linux FRR fabric so its SRv6 data plane is **identical to the
OpenAI/Microsoft MRC paper** ("Resilient AI Supercomputer Networking using MRC and SRv6",
OCP MRC 1.0). Host is the SR source; the core is a dumb static uN-shift forwarder.

This document is the agreed contract. Code follows from it. Nothing here is hand-typed at
runtime — the generator computes every address, uSID, carrier, and route from the position plan.

---

## 1. Decisions locked (from the spec + lab-scope answers)

| # | Decision | Choice | Source |
|---|----------|--------|--------|
| 1 | SR source | **The NIC/GPU (host)** imposes the full source route | paper §2.2 |
| 2 | Core intelligence | **None** — T0/T1 are static uN-shift forwarders, config written once, never changed | paper §2.2 |
| 3 | Decap location | **Destination NIC** strips IPv6-in-IPv6 (inner DA = NIC's own addr) | paper §2.2 |
| 4 | Path naming | **Full** T0→T1→T0→dst — every switch named in the carrier | user choice + paper Fig.3 |
| 5 | uSID flavour | **Classic uN (RFC 8986 / RFC 9800)**, locator-table style | user choice |
| 6 | uSID block | **fcbb:bb00::/32** (spec/Cisco style, F3216) | user choice |
| 7 | Carrier format | **F3216**: 32-bit block + 16-bit uSIDs | paper §2.2, RFC 9800, Cisco |
| 8 | Planes | **Configurable**: single-plane and N-plane both supported, scale-aware | user choice |
| 9 | GPU routes | **Ansible installs at deploy** — every GPU a standing SR source | user choice |
| 10 | Roles | leaf→**T0**, spine→**T1**, gpu→**NIC/host** (2-tier multi-plane Clos) | paper §2 |

---

## 2. uSID carrier format (F3216)

```
128-bit uSID carrier (outer IPv6 destination address)
┌────────────┬────────┬────────┬────────┬────────┬─────────────┐
│  block /32 │ uSID#1 │ uSID#2 │ uSID#3 │  ...   │ end-of-carr │
│  fcbb:bb00 │ 16-bit │ 16-bit │ 16-bit │        │   0x0000    │
└────────────┴────────┴────────┴────────┴────────┴─────────────┘
   bits 0-31   32-47    48-63    64-79              trailing zeros
```

- **Active uSID** = the first uSID after the block (bits 32-47). It sits in the /48-significant
  region the switch matches on.
- **uN behavior at each switch**: match first 48 bits (block+active uSID) against my locator;
  if match, **left-shift the uSID field by 16 bits** (next uSID becomes active), then FIB-lookup
  the new DA and forward. Trailing zero = end of carrier reached.
- This is exactly the paper's Fig.2 and your spine1 capture, now with a 32-bit block instead of
  your old 48-bit locator.

### uSID allocation (position-encoded, per node)

Each switch gets a **globally-unique 16-bit uSID**, structured so you can read role+plane+index:

```
uSID = (tier_nibble << 12) | (plane << 8) | index
        tier: T1=0x1, T0=0x2      (4 bits)
        plane: 1..N                (4 bits)
        index: 1..255              (8 bits)
e.g.  T1 #3 plane1  -> 0x1103
      T0 #2 plane1  -> 0x2102
```

Each switch's **locator** (what it matches + advertises into the static table) is:
```
locator = fcbb:bb00:<uSID>::/48      e.g. T0#2p1 -> fcbb:bb00:2102::/48
```
All switches share block `fcbb:bb00::/32`; the /48 = block(32) + uSID(16).

---

## 3. The path: NIC → T0 → T1 → T0 → NIC

For a packet from a source GPU on T0-a (plane p) to a destination GPU on T0-b via T1-x:

```
Source GPU builds carrier naming every transit switch:
  fcbb:bb00 : <T1-x uSID> : <T0-b uSID> : 0 ...

  (the source's own T0-a is NOT named — packet already egresses there;
   the first named switch is the T1, then the destination T0.)
```

Step by step on the wire (DA = outer destination):

| Hop | Node | Action | Outer DA after the hop |
|-----|------|--------|------------------------|
| 0 | src GPU | encap: push outer IPv6 + carrier; inner DA = dst GPU | `fcbb:bb00:<T1x>:<T0b>::` |
| 1 | T0-a | uN: active uSID is T1x → shift → forward up to T1-x | `fcbb:bb00:<T0b>::` |
| 2 | T1-x | uN: active uSID is T0b → shift → forward down to T0-b | `fcbb:bb00::` *(carrier exhausted; DA now = the inner? no — see note)* |
| 3 | T0-b | plain IPv6 forward toward dst GPU's subnet (no SID left) | unchanged |
| 4 | dst GPU | recognise inner DA = mine → **decap** → deliver native | (inner) `<dst GPU>::2` |

**NOTE on the last shift / delivery.** Two valid encodings, we pick the one that keeps the core
dumbest:

- **(A) Name dst-GPU's prefix as the final step.** The carrier's last uSID after T0b is omitted;
  instead the source sets the *post-exhaustion* DA to the **destination GPU's real /64**. After
  T1-x shifts T0b in, the DA exposes T0b's locator; T0-b matches, shifts, and the now-exhausted
  carrier reveals the inner destination prefix which T0-b plain-routes to the GPU subnet. The GPU
  decaps. This keeps T0-b doing only uN + plain IPv6 — no per-GPU SID on the switch. **CHOSEN.**

  Concretely the source encodes the **GPU's own global address as the inner DA** and the carrier
  as `block:<T1x>:<T0b>::`. After T0-b consumes T0b, DA = `fcbb:bb00::` (carrier end). T0-b has a
  static route for the GPU /64s pointing out the access port (this is plain IPv6 reachability, not
  a SID), so the *inner* packet — once decapped — is delivered. But decap happens at the GPU, so
  T0-b must forward the *encapsulated* packet to the GPU. Therefore:

  **The destination GPU's access subnet must be reachable as plain IPv6 from T0-b, and the outer
  DA after carrier-exhaustion must route to that GPU.** We achieve this by making the **last uSID =
  the destination GPU's downlink identifier** so the exhausted-carrier DA longest-prefix-matches a
  static T0-b route to the GPU port. The GPU sees outer DA = (its delivery SID / its own addr) and
  decaps.

This is the one genuinely fiddly part and I'll validate it on the wire before declaring done.

### RESOLVED (validated path, post-wire-capture 2026-06-25)

Wire capture proved: in the `fc00:0` proven fabric, decap happens at the **leaf** (leaf End.DT6),
because the carrier's last uSID names the *leaf's* End.DT6 SID. To move decap to the NIC, the
carrier must name a **per-GPU decap uSID** and the destination leaf must uN-forward it to the GPU
port (NOT decap it). Locked scheme:

- Each GPU gets a decap uSID `gpu_usid` (16-bit, in the block). Its decap SID = `fcbb:bb00:<gpu_usid>::`,
  programmed on the GPU as `seg6local action End.DT6` (matches its own SID, strips, delivers inner).
- The GPU's home leaf (T0-b) holds ONE static route per local GPU:
  `fcbb:bb00:<gpu_usid>::/48 → out the GPU access port` (plain forward, no SID action on the leaf).
- Carrier = `block : <T1x> : <T0b> : <gpu_usid> : 0`. Inner DA = dst GPU's real /64 address.

Wire walk (cross-leaf, plane p):
| Hop | Node | active uSID | action | outer DA after |
|-----|------|-------------|--------|----------------|
| 0 | src GPU | — | encap, inner DA = dst GPU real addr | `fcbb:bb00:T1x:T0b:gpu::` |
| 1 | T0-a | T1x | uN shift → up to T1x | `fcbb:bb00:T0b:gpu::` |
| 2 | T1x | T0b | uN shift → down to T0-b | `fcbb:bb00:gpu::` |
| 3 | T0-b | gpu | **uN shift** → static route to GPU port | `fcbb:bb00::` *(carrier end)*, fwd out GPU port |
| 4 | dst GPU | — | outer DA `fcbb:bb00:gpu::` = my decap SID → **End.DT6 strip** → deliver inner | (inner) dst real addr |

Wait — at hop 3 after uN-shift the DA is `fcbb:bb00::` (exhausted), which won't reach the GPU.
CORRECTION: T0-b does NOT uN-shift the gpu uSID. Instead T0-b's static route matches
`fcbb:bb00:<gpu_usid>::/48` and forwards the packet **unchanged** out the GPU port (plain IPv6
longest-prefix forward on the still-active-uSID DA). The GPU receives outer DA
`fcbb:bb00:<gpu_usid>::` = its own decap SID, and End.DT6-strips. So T0-b holds the gpu uSID as a
PLAIN /48 forward route (not a uN End SID) — the leaf never has a SID action for GPU traffic, only
a forwarding entry. This is the "keeps T0 dumb" property: T0 has uN for its own locator + plain
static forwards for its local GPU decap-SIDs. CHOSEN + LOCKED.

---

## 4. What each device holds

**T1 (spine) — pure uN forwarder**
- One locator `fcbb:bb00:<uSID>::/48`, behavior uN.
- Static `/48` routes: for every *other* switch uSID, "to reach `fcbb:bb00:<thatuSID>::/48`,
  send out port X." Configured once. No dynamic protocol.
- No host routes, no decap, no per-GPU state.

**T0 (leaf) — uN forwarder + plain IPv6 reachability to its own GPUs**
- One locator `fcbb:bb00:<uSID>::/48`, behavior uN.
- Static `/48` routes to reach every other switch uSID (via its T1 uplinks — ECMP set).
- Plain IPv6 connected routes for its own GPU access /64s (so it can deliver the
  encapsulated packet to the destination GPU, which then decaps).
- No End.DT6, no per-GPU SID.

**NIC (GPU) — the only SRv6-smart device**
- **Encap routes** (installed by Ansible): for every destination GPU /64 (or per-T0 aggregate),
  an seg6 encap route whose carrier = `block:<T1x>:<T0b-delivery>::`, inner dst = dst GPU.
  Full path named. One per (destination, plane[, path]) — the EV-set analog.
- **Decap rule**: a local seg6 / IPv6-in-IPv6 decapsulation entry matching its own address,
  so inbound encapsulated packets are stripped and delivered to the host stack.
- Reachability for the block `fcbb:bb00::/32` via its T0 gateway (so the carrier is routable).

---

## 5. Multi-plane (configurable, scale-aware)

- `planes: 1` → single plane, bare names (T0-1, T1-1, gpu1). Prove the model here first.
- `planes: N` → N parallel Clos planes; each GPU has one NIC per plane into the plane's T0.
  uSID plane-nibble distinguishes `fcbb:bb00:2101::` (p1) from `fcbb:bb00:2201::` (p2) etc.
- GPU encap route set spans planes: the source installs ≥1 carrier per plane per destination
  (the paper's "equal EVs per plane"). With one waypoint-path per plane that's `planes` routes
  per destination; with K paths/plane it's `planes*K` (the EV-set). Default: one path/plane,
  knob to raise.
- Addressing auto-scales (position-encoded) exactly as today.

---

## 6. What we CAN and CANNOT reproduce vs the paper (honesty)

**Reproduce faithfully (data plane):**
- uN/uSID F3216 forwarding, full-path source routing, host encap + host decap, dumb static core,
  multi-plane addressing, per-plane path sets, the DA-shift on the wire.

**Cannot reproduce (transport / NIC firmware):**
- MRC itself: per-packet EV spraying, SACK/NACK selective retransmit, packet trimming,
  ECN-based EV rebalancing, RDMA. These live in CX-8/Pollara/Thor-Ultra silicon.
- So "EVs" in the lab = a static set of pre-installed carriers per destination; we can demo
  "host picks among N pre-computed physical paths," not live per-packet entropy rotation.

The deliverable is an **identical SRv6 addressing + forwarding plane** with hosts as SR
head-ends and a stateless core — the routing architecture of the paper, minus the NIC transport.

---

## 7. Build plan (generator + ansible changes)

1. **address_plan.json**: add `usid` section (block `fcbb:bb00`, 32/16 split, tier/plane/index
   encoding). Keep `fd00:9` GPU host plan + `fd00:1` p2p. Retire the old `fc00:0 /48` srv6 section.
2. **gen_clab_topology.py**:
   - emit per-switch locator + static `/48` uN routes (kernel `seg6local action End flavor next-csid`
     — wait, uN classic: use `seg6local action End` with the uSID/next-csid flavor appropriate to
     RFC 9800; validate which the 6.8 kernel programs) and static `/48` forwarding to neighbors.
   - drop End.X/End.DT6 generation for the core.
   - compute per-(src,dst,plane) carriers `block:<T1x>:<T0b>::` and write them into
     `fabric_vars.json` for Ansible.
3. **roles/gpu_host**: install encap routes (one per dst per plane) + a decap rule on every GPU.
4. **srv6-test.sh / srv6-walk.sh**: retarget to NIC-source + NIC-decap; assert DA-shift at T0 and
   T1 and native decap at the destination GPU (not the leaf).
5. Validate on the wire (single plane, 2×T0 / 2×T1 / 4 GPU) before scaling.

Open kernel question to settle empirically before mass-generating: which seg6local incantation on
the 6.8 kernel gives true uN next-csid shift (vs the End.X workaround you used before). I'll probe
this on a 2-node sanity lab first so we don't bake a wrong action into every switch.
```

---

## 8. Virtual MRC NIC integration (mrc-agent → standalone mrc-nic)

Reuse the existing `mrc-agent` (1886 lines, from mrc-fabric-v2) as the per-GPU virtual
Pollara/Thor NIC. It already implements the real dataplane; we sever the controller and feed
it from a generator-produced profile.

**Keep verbatim (the virtual NIC core, `Dataplane` class):**
- `mrc0` tenant interface lifecycle; tenant IPv6 lives on mrc0 (decoupled from underlay).
- `program_srv6(peer_dst, ev_stacks)`: per-EV seg6 encap with **fwmark path pinning** —
  one `ip -6 route … table <EV>` + `ip -6 rule fwmark <EV> table <EV>` per EV, plus an
  ECMP main-table fallback for unmarked sockets.
- The SO_MARK contract: `mrc-probe` stamps `SO_MARK=<entropy>` per stream → kernel hits the
  fwmark rule → single-nexthop encap → deterministic per-EV physical path. This is the EV→path
  mechanism and it is already correct; the generator just has to emit matching {entropy, usid}.

**Sever (controller coupling — make dormant, not deleted):**
- `--controller` becomes OPTIONAL. Absent → standalone. Present → today's behaviour.
- New `FileProfileSource`: reads `/etc/mrc-nic/profile.json`, calls the SAME `on_profile(p)`
  seam the SSE path calls. (on_profile already gates on flow.src == this host, handles
  mode=srv6, clears stale encap, etc. — no change.)
- When standalone: skip SSE subscribe, MetricsPoster, MeshHealth (all controller-POSTing).
  State file + `status`/`paths`/`monitor` still work (local).

**Add (NIC owns decap now — the one new behaviour):**
- At `bring_up()`, install an inbound decap rule matching the tenant address, so encapsulated
  packets arriving for this NIC are stripped (per MRC spec: destination NIC decaps).
  Exact seg6local action validated on the 2-node kernel probe before baking in.

**Profile schema (generator emits this per GPU; identical to what the controller used to push):**
```json
{
  "mode": "srv6",
  "flow": { "src": "<this-gpu-host>", "dst": "<peer-gpu-host>" },
  "active_dst": "<peer tenant addr>",
  "active_evs": [
    { "entropy": 49001, "usid": "fcbb:bb00:<T1x>:<T0b>::", "plane": 1 },
    { "entropy": 49002, "usid": "fcbb:bb00:<T1y>:<T0b>::", "plane": 2 },
    ...
  ]
}
```
- **Full EV-set precomputed by the generator** (user choice): every pinned carrier per
  destination per plane, one entropy value each, written into the GPU's profile at deploy.
- Multi-dest: profile is a list of these blocks (one per peer GPU), or a map keyed by dst.

**Traffic generation (user choice): reuse mrc-probe / mrc-sink.**
- They already do per-EV SO_MARK spray + SACK/NAK/OOO reporting. No rewrite.
- `mrc-probe --dst <peer> --entropy-ports N` sprays across the installed EV set; mrc-sink
  reports per-EV. Validates the pinned paths end to end.

**Deploy flow (Ansible, no controller):**
1. generator computes per-GPU profile.json (peers × planes × pinned carriers).
2. gpu_host role drops profile.json + installs mrc-nic, mrc-probe, mrc-sink on each GPU.
3. `mrc-nic run --tenant <addr> --underlay eth1 --profile /etc/mrc-nic/profile.json` (no --controller).
4. NIC programs encap (per-EV pinned) + decap (own tenant) at startup; standing SR source.

**Cannot reproduce (unchanged from §6):** live per-packet EV rotation, hardware SACK/trimming,
ECN rebalancing — these are NIC silicon. The lab demonstrates pinned-path source routing with
a static EV-set and software probe/sink spray+report.
```

---

## WIRE FINDINGS — NIC-side uSID decap (2026-06-25, kernel 6.8)

The full spec data path was validated hop-by-hop on the all-Linux fabric. RESULT: every
hop works EXCEPT the terminal NIC decap, and the blocker is **userspace tooling**, not the
kernel or the design.

Proven on the wire (captures):
- src GPU host-NIC encap: outer SA = GPU tenant, outer DA = `fcbb:bb00:<spine>:e002:<gpu>::` carrier, SRH present.
- src leaf: plain-forwards the carrier to the spine (no shift).
- spine: End.X next-csid shift removes the spine's WHOLE locator+func pair
  (IN `fcbb:bb00:1102:e002:2102:9003::` -> OUT `fcbb:bb00:2102:9003::`, and with the leaf hop
  dropped: -> `fcbb:bb00:9003::`). Confirmed by spine eth1-in / eth2-out capture.
- dst leaf: plain-forwards `fcbb:bb00:9003::/48` to the GPU port UNCHANGED (no seg6local).
- dst GPU: packet ARRIVES on eth1 as `fcbb:bb00:9003::` + SRH (capture confirmed), seg6_enabled=1,
  decap route installed, inner DA `fd00:9:1:201::2` is local. **But decap does not fire — 100% loss.**

Root cause (definitive): the `ghcr.io/hellt/network-multitool` GPU image ships **iproute2 5.6.0
(ss200330, 2020)** — the newest in that Alpine release. NEXT-C-SID/uSID support in iproute2
landed in **6.1** (Dec 2022). The old tool installs the End.DT6 route, but the netlink message
it builds doesn't drive the modern kernel's uSID terminal-decap path, so the packet is silently
consumed (cf. netdev 0x19 "Uneven Routing Error Handling in SRv6 End": IPv6 routing failures in
End* are consumed, returning 0, no error counter). Proven by a fabric-free self-decap test on the
GPU (encap to its own decap SID + loop through its own End.DT6) ALSO failing 100%.

FIX (locked): spec mode requires a GPU image with **iproute2 >= 6.1 AND kernel >= 6.1**.
Recommended `--gpu-image debian:bookworm` (iproute2 6.1) or `debian:trixie` (6.15). The NIC now:
- prints a CAPABILITY WARNING at startup if iproute2 is too old (parses `ip -V`);
- ships a `doctor` subcommand dumping iproute2 version, kernel, seg6_enabled per iface, installed
  seg6local routes, and a capability verdict — so this failure is one command to diagnose, not hours.

The proven LEAF-decap fabric (`spec_mode=false`, fc00:0 block, End.DT6 on the leaf) works on the
wire on ANY image including the stale one — it is the validated default and the fallback.

---

## FINAL VERDICT — NIC-side decap on kernel 6.8.0-111-generic (2026-06-25)

After exhaustive on-box forensics, the spec cutover's terminal NIC decap is blocked by the
**kernel**, not the design or tooling. Full chain of evidence:

1. iproute2 confirmed 6.1.0 (NEXT-C-SID capable), kernel 6.8, seg6_enabled=1 on all ifaces,
   forwarding=1, decap route correctly installed, `ip route get` resolves the arriving DA to the
   End.DT6 route. ALL preconditions satisfied.
2. Packet ARRIVES at the destination GPU (tcpdump confirmed) as either SRH-form
   (`fcbb:bb00:9003::` + SRH segleft=0) or reduced-encap IPv6-in-IPv6 (next-header 41). Inner DA
   = the GPU's own address, confirmed local (`dev lo table local`).
3. seg6local behavior COUNTER stays `packets 0 errors 0` — the action never runs.
4. `Ip6InDiscards` increments EXACTLY per received packet — the kernel discards the packet at
   IPv6 input, BEFORE seg6local processing. Not InNoRoutes, not InAddrErrors.
5. Reproduced identically for: classic 128-bit SID End.DT6, uSID End.DT6, End.DX6, with/without
   the carrier `/32`, decap on mrc0 AND on eth1, SRH-form AND reduced-encap.

CONCLUSION: on this kernel build, seg6local End.DT6/DX6 decap of a TRANSIT-delivered packet
(outer DA = a non-local seg6local SID arriving on a forwarding interface) is discarded at IPv6
input and never reaches the local-SID action. Every hop UPSTREAM works on the wire: host-NIC
uSID encap, dumb-core plain forwarding, spine End.X next-csid shift (capture-verified
`1102:e002:...` -> shifted correctly), dst-leaf plain forward to the GPU port.

### Shipped resolution
- DEFAULT = proven LEAF-decap fabric (`spec_mode=false`, fc00:0 block, End.DT6 on the leaf).
  This forwards AND decaps correctly on the wire today, host-sourcing SRv6 from the NIC. It
  demonstrates the MRC model end-to-end; the only spec deviation is decap location (leaf vs NIC).
- SPEC path (`spec_mode=true`, fcbb:bb00, NIC decap, T0/T1) is fully built, correct, and
  flag-gated. It is ready for a kernel where seg6local input decap fires. Given everything
  upstream works, this is likely a newer kernel point-release or a non-default sysctl in a
  hardened Ubuntu build (candidates to try later: net.ipv6.conf.*.disable_policy,
  accept_local, seg6 input via a VRF strict-mode table per the netdev 0x19 End.DT* guidance).
- The NIC ships `mrc-nic doctor` + a startup capability check so the iproute2 axis is never a
  silent failure again.

---

## *** SOLVED *** — NIC-side uSID decap WORKS on kernel 6.8 (2026-06-25)

The earlier "kernel limitation" verdict was WRONG. NIC-side End.DT6 decap of a transit-delivered
uSID packet works on 6.8.0-111-generic + iproute2 6.1.0. Full cross-leaf round trip validated:
`gpu1 ping6 gpu3 -> 3 packets, 0% loss`, host-NIC-sourced SRv6 both directions, decap at the
destination NIC. The blocker was never the kernel — it was that a bare `End.DT6 table main dev
mrc0` route does not enter the seg6local input path. The fix is the canonical Linux MyLocalSID
pattern, which we reverse-engineered on the wire:

### The proven recipe (now in mrc-nic install_decap, build g10+)
1. **FIB rule** steering the SID prefix into a dedicated localsid table:
   `ip -6 rule add prio 50 to <block:gpu_usid>/48 lookup 100`
   prio 50 places it BEFORE the local table (prio 0) so the SID is classified as a local-SID
   lookup, not a transit forward. WITHOUT this rule the packet is discarded at IPv6 input
   (Ip6InDiscards++) and seg6local never runs (counter stays 0). THIS was the missing piece.
2. **End.DT6 in the localsid table, inner lookup in the LOCAL table (255)**:
   `ip -6 route add <sid>/48 table 100 encap seg6local action End.DT6 count table 255 dev eth1`
   `table 255` (local) delivers the decapped inner packet to the host stack and ICMP responds
   (verified: Icmp6InEchos and Icmp6OutEchoReplies both increment). `table main` instead gives
   `errors N` — the action fires but the inner lookup fails.
3. **Carrier-block route** `fcbb:bb00::/32 via <leaf-gw>` present so the GPU's OWN encapped
   replies can egress (else replies are generated but "Network is unreachable" on send).

### Debugging path (counters were the oscilloscope)
- `ip -s -6 route show` seg6local counter `packets/errors` (needs iproute2 >= 5.13) distinguishes
  "action never ran" (0/0 + Ip6InDiscards) from "ran but inner lookup failed" (N/errors N) from
  "fully worked" (N/0).
- The breakthrough was observing the counter go from `packets 0` (discarded pre-action) to
  `errors 3` (action firing) the instant the prio-50 localsid rule was added — proving the rule,
  not the SID form or the kernel, was the gate.

### Status
Spec mode (`spec_mode=true`, fcbb:bb00, NIC decap) is now FULLY WORKING on the wire. The NIC
installs the localsid rule + table 100 + End.DT6 table 255 automatically. `mrc-nic doctor` shows
the rule, table, and carrier-block route. Both leaf-decap (default) and NIC-decap (spec) fabrics
forward correctly on kernel 6.8.

---

## Route aggregation for scale (2026-06-25, build g13)

The first spec implementation emitted one /48 route PER GPU on every spine and every remote
leaf — O(total GPUs) state on every node, defeating the "dumb core" goal. Fixed with a
leaf-nested uSID layout that aggregates at every tier.

### uSID layout: block:9LLL:GGGG::/64
3rd hextet = 0x9LLL (nibble 9 = GPU block, LLL = home-leaf index, up to 4095 leaves).
4th hextet = GGGG (local GPU index on that leaf, up to 65535). A GPU's decap SID is its own
/64 (block:9LLL:GGGG::). Caps far exceed any real fabric.

### Resulting per-node state (invariant to GPUs elsewhere)
- **Spine**: ONE /48 per leaf -> that leaf  (fcbb:bb00:9LLL::/48). O(leaves).
- **Leaf** : ONE /36 aggregate -> spines (fcbb:bb00:9000::/36, ECMP) for ALL remote GPUs,
             plus a specific /64 per LOCAL GPU -> its access port. O(local GPUs + 1).
- LPM does the rest: a remote-GPU packet hits the leaf /36 -> spine; the spine /48 -> home
  leaf; the home leaf's specific /64 -> the GPU port; the GPU End.DT6-decaps.

Measured at 64 GPUs (8 leaves x 8): leaf1 = 9 GPU routes (was 64), spine1 = 8 (one per leaf).
At 256 GPUs a leaf still carries only (local GPUs + 1) GPU routes; adding GPUs on other leaves
changes NOTHING on this leaf or any spine. The carrier gains one hextet (now
block:spine:End.X:9LLL:GGGG::) and the spine End.X shift still strips its locator+func pair,
leaving block:9LLL:GGGG:: for the home leaf to plain-forward. Validated end-to-end in the
generator trace; proven (leaf-decap) mode is unaffected.
