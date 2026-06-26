# Topology viewer

A single-file, dependency-free GUI that renders the fabric from
`fabric_vars.json` — planes as stacked CLOS layers (spines over leaves), with
GPUs in a shared row connecting up into every plane (one NIC port per plane).

## Run it as a container (with the lab)

Bring the GUI up alongside the fabric with `-e do_gui=true`:

```bash
cd ansible
ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e do_gui=true
```

This starts a small `mrc-gui` container (default `python:3.12-slim`) that serves
the repo root over HTTP and prints the URL:

```
http://<this-host>:8080/gui/topology.html
```

It read-only bind-mounts the repo, so it always serves the current
`fabric_vars.json`. Re-run a deploy to refresh the topology; reload the page.

Tear it down with the lab, or on its own:

```bash
ansible-playbook site-frr.yml ... -e do_gui=false   # stops mrc-gui
# or:
docker rm -f mrc-gui
```

Knobs (in `ansible/group_vars/all.yml`, override with `-e`):
`do_gui` (default false), `gui_port` (8080), `gui_image` (python:3.12-slim).

## Run it without the container

Serve the repo root yourself and open the page:

```bash
# from the repo root
python3 -m http.server 8080
# then open http://localhost:8080/gui/topology.html
```

Or open `gui/topology.html` directly and use the **load fabric_vars.json** link
in the header (browsers block local `fetch()` on `file://`, so the picker is the
offline path).

## What it shows

- **Plane bands** — each plane is a tinted CLOS layer (spine row over leaf row).
- **Shared GPU row** — every GPU connects to its home leaf *in each plane*, so a
  GPU's links fan up into all planes.
- **Hover** a node to highlight its links and dim the rest; **click** to pin its
  details (role, plane, mgmt, loopback, locator) and full link list.

No build step. The container is just a static file server today — a deliberate
seam for adding a live backend (status polling, carrier path overlay) later.
