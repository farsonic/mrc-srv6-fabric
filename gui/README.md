# Topology viewer + path-health backend

`topology.html` renders the fabric from `fabric_vars.json` — planes as stacked
CLOS layers (spines over leaves), with GPUs in a shared row connecting up into
every plane (one NIC port per plane). Beneath it, a **paths table** lists every
GPU-to-GPU carrier the fabric can take — one row per `(src, dst, plane, spine)` —
and joins **live test-probe health** (RTT, jitter, loss, up/denied) to each.
Every path is shown **symmetrically**: forward (`A→B`) and return (`B→A`) side by
side, so an asymmetric path is obvious at a glance. Hovering a row lights its
exact carrier through the topology (gpu → leaf → spine → leaf → gpu).

`server.py` is the backend that feeds it: it serves the repo root (so the page
loads as before) **and** runs a small control-plane the NICs attach to —
`/api/topology` + `/api/mesh-plan` hand each NIC its per-path probe plan, the
NICs POST results to `/api/mesh-health`, and the page polls `/api/mesh` for the
aggregate. Stdlib only, no build step.

## Run it as a container (with the lab)

Bring the GUI up alongside the fabric with `-e do_gui=true`, and point the NICs
at it with `-e mesh_controller_url=...` so the paths table gets **live** health:

```bash
cd ansible
ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 \
  -e do_gui=true -e mesh_controller_url=http://172.20.20.1:8080
```

`mesh_controller_url` is this host's URL **as the GPU containers see it** — the
mgmt-net gateway (e.g. `172.20.20.1`, the `.1` of your `mgmt_subnet`) on
`gui_port`. The `mrc-gui` container (default `python:3.12-slim`) serves:

```
http://<this-host>:8080/gui/topology.html
```

It read-only bind-mounts the repo, so it always serves the current
`fabric_vars.json`. Re-run a deploy to refresh the topology; reload the page.
Live health is **in-memory** — each NIC re-posts every few seconds, so a GUI
restart self-heals within one sweep.

Without `mesh_controller_url` the NICs run standalone (no mesh) and the table
shows the full path **inventory** with status “no data”. Tear down:

```bash
ansible-playbook site-frr.yml ... -e do_gui=false   # stops mrc-gui
# or:
docker rm -f mrc-gui
```

Knobs (in `ansible/group_vars/all.yml`, override with `-e`):
`do_gui` (false), `gui_port` (8080), `gui_image` (python:3.12-slim),
`mesh_controller_url` ("" = standalone NICs).

## Run it without the container

```bash
# from the repo root — serves static files AND the /api endpoints
python3 gui/server.py --port 8080 --root .
# then open http://localhost:8080/gui/topology.html
```

`python3 -m http.server 8080` still works for the topology alone, but it has no
`/api/*`, so the paths table falls back to the inventory-only view. Opening
`gui/topology.html` directly (`file://`) also works via the **load
fabric_vars.json** picker in the header (browsers block local `fetch()`).

## The paths table

- **Every path, both ways.** One row per `(src, dst, plane, spine)` carrier. With
  *symmetric pairs* on (default), `A↔B` shows forward and return RTT/loss in
  adjacent columns; toggle it off for a flat directed list with jitter + age.
- **Live health** comes from each NIC's always-on full-mesh test probe (TWAMP if
  `mrc-twamp` is deployed, else the NIC's built-in `SO_MARK`-pinned raw ICMPv6 —
  no extra binary). Status dot: green up · amber degraded (loss) · red down ·
  purple auto-denied · grey no data.
- **Carrier overlay.** Hover a row to light its exact hops through the fabric;
  click to pin. The carrier uSID for the path is shown under the spine name.

## How the live data flows

```
each GPU's mrc-nic                          gui/server.py                 browser
  GET  /api/topology   ── discover peers ──────►  (from fabric_vars.json)
  GET  /api/mesh-plan  ── per-path probe plan ─►  (from fabric_vars `paths`)
  …probe every path (TWAMP / raw ICMPv6, fwmark-pinned per carrier)…
  POST /api/mesh-health ── results ────────────►  stored in memory
                                                  GET /api/mesh ◄── poll ── topology.html
```

The path list itself is generated, not guessed: `gen_clab_topology.py` emits a
`paths[]` array into `fabric_vars.json` (every src/dst/plane/spine carrier uSID,
each with a unique fwmark), so the backend never re-derives carriers and the
table matches exactly what the NICs program.

## What the topology shows

- **Plane bands** — each plane is a tinted CLOS layer (spine row over leaf row).
- **Shared GPU row** — every GPU connects to its home leaf *in each plane*, so a
  GPU's links fan up into all planes.
- **Hover** a node to highlight its links and dim the rest; **click** to pin its
  details (role, plane, mgmt, loopback, locator) and full link list.
