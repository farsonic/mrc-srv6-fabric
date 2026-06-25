# Topology viewer

A single-file, dependency-free GUI that renders the fabric from
`fabric_vars.json` — planes as stacked CLOS layers (spines over leaves), with
GPUs in a shared row connecting up into every plane.

## Use it

The generator writes `fabric_vars.json` to the repo root on every deploy. Serve
the repo root (the page looks for `fabric_vars.json` next to it or one dir up):

```bash
# from the repo root
python3 -m http.server 8080
# then open http://localhost:8080/gui/topology.html
```

Or just open `gui/topology.html` in a browser and use the **load
fabric_vars.json** link in the header to pick the file (browsers block local
`fetch()` on `file://`, so the picker is the offline path).

## What it shows

- **Plane bands** — each plane is a tinted CLOS layer (spine row over leaf row).
- **Shared GPU row** — every GPU connects to its home leaf *in each plane*
  (one NIC port per plane), so a GPU's links fan up into all planes.
- **Hover** a node to highlight its links and dim the rest; **click** to pin its
  details (role, plane, mgmt, loopback, locator) and full link list in the
  inspector.

No build step, no server framework — just HTML/SVG/JS.
