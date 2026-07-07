#!/usr/bin/env python3
"""
mrc-gui backend — static topology viewer + live path-health collector.

This replaces the plain `python3 -m http.server` the GUI container used to run.
It still serves the repo root (so /gui/topology.html and /fabric_vars.json work
exactly as before), and adds a small control-plane the MRC NICs attach to:

  GET  /api/topology             peer list (names + tenant addrs) — NICs discover
                                 who to probe. Built from fabric_vars.json.
  GET  /api/mesh-plan?src=<host> the per-path probe plan for one source NIC: every
                                 (peer, spine) carrier with its fwmark + uSID, so
                                 the NIC pins one test probe to each path. Built
                                 from fabric_vars.json `paths` (no carrier re-derive).
  POST /api/mesh-health          a NIC posts its latest full-mesh sweep here every
                                 few seconds: {host, ts, peers:[...], weigher:{...}}.
  GET  /api/mesh                 the aggregate the GUI tables: every path in the
                                 fabric joined to the latest probe health reported
                                 for it. Symmetric — src->dst and dst->src are both
                                 present (each NIC probes its own outbound paths).
  GET  /api/profile/stream       SSE keepalive so an attached NIC's controller
                                 subscription stays connected (no profiles pushed).
  POST /api/metrics|/api/ev-stats|/api/probe-tx|/api/jobs/status
                                 accepted and dropped — the NIC posts these to a
                                 controller; we just don't want it to error.

Stdlib only. State is in-memory (the bind mount is read-only); a NIC re-posts its
health every sweep, so a restart self-heals within one MESH_PERIOD.

Usage:
  python3 gui/server.py [--port 8080] [--root .] [--vars fabric_vars.json]
"""

import argparse, io, json, os, tarfile, threading, time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---- live state ------------------------------------------------------------
_LOCK = threading.Lock()
_REPORTS = {}          # host -> {"ts","received_at","peers":[...],"weigher":{...}}
_BYPASS = set()        # node names an operator has drained for maintenance
_STOP = threading.Event()

# ---- fabric_vars.json (reloaded on mtime change) ---------------------------
_FAB = {"path": None, "mtime": 0.0, "data": None}

def load_fabric(path):
    """Return the parsed fabric_vars.json, reloading only when the file changes
    so a redeploy refreshes the topology without restarting the server."""
    try:
        st = os.stat(path)
    except OSError:
        return _FAB["data"]
    if _FAB["data"] is None or st.st_mtime != _FAB["mtime"] or _FAB["path"] != path:
        try:
            with open(path) as f:
                _FAB["data"] = json.load(f)
            _FAB["mtime"] = st.st_mtime
            _FAB["path"] = path
        except (OSError, ValueError) as e:
            print(f"[gui] could not load {path}: {e}")
    return _FAB["data"]

def gpu_hosts(fab):
    """[{name, addr_v6, addr_v4}] for every GPU, from fabric_vars nodes. The
    tenant addr is the GPU's plane-1 host address (its stable decap identity)."""
    hosts = []
    for name, nd in (fab.get("nodes") or {}).items():
        if nd.get("role") != "gpu":
            continue
        addr6 = None
        for itf in nd.get("interfaces", []):
            ip = (itf.get("ipv6") or "").split("/")[0]
            if ip:
                addr6 = ip
                break
        hosts.append({"name": name, "addr_v6": addr6, "addr_v4": None})
    return hosts

def decap_sid_for(fab, src):
    """g16: the host's own uSID decap block (what arriving carriers bear after
    the spine shift), served to the NIC so install_decap keys End.DT6 on it.
    Derived from the authoritative paths[] — a same-leaf usid TO this host IS
    the bare block; otherwise take the last two hextets of any inbound usid."""
    inbound = [p for p in (fab.get("paths") or []) if p.get("dst") == src]
    if not inbound:
        return None
    for p in inbound:                       # same-leaf carrier = the bare block
        if p.get("kind") == "same-leaf" and p.get("usid"):
            return p["usid"].rstrip(":") + "::/64" if not p["usid"].endswith("::") else p["usid"] + "/64"
    u = (inbound[0].get("usid") or "").rstrip(":")
    hx = [h for h in u.split(":") if h]     # e.g. fcbb bb00 1101 e002 9002 1
    if len(hx) >= 4:
        return f"{hx[0]}:{hx[1]}:{hx[-2]}:{hx[-1]}::/64"
    return None


def path_nodes(p):
    """The fabric nodes a path traverses (spine + both leaves), for maintenance
    drain: bypassing any of them drains the path."""
    return {n for n in (p.get("spine"), p.get("src_leaf"), p.get("dst_leaf")) if n}

def plan_for(fab, src):
    """The per-path probe plan for one source host: every path whose src is this
    host, shaped for the NIC's /api/mesh-plan consumer. `drained` marks a path
    that transits an operator-bypassed node — the NIC keeps probing it but steers
    data traffic off it."""
    out = []
    with _LOCK:
        bypass = set(_BYPASS)
    for p in (fab.get("paths") or []):
        if p.get("src") != src:
            continue
        out.append({
            "peer": p["dst"], "dst": p["dst_addr"],
            "spine": p.get("spine"), "path_id": p["path_id"],
            "fwmark": p["fwmark"], "probe_sid": p["usid"], "usid": p["usid"],
            "plane": p.get("plane"),   # which underlay plane this path egresses (dual-plane)
            "drained": bool(bypass & path_nodes(p)),
        })
    return out

def underlays_for(fab, src):
    """{ "<plane>": {"iface","gateway"} } for the source host's underlay NICs.

    In a multi-plane fabric each GPU homes into every plane (eth1=plane1,
    eth2=plane2, ...). A path's encapped probe must egress the plane its uSID
    traverses, so the NIC needs plane -> (iface, gateway) to install a per-path
    carrier route on the right underlay. Derived from fabric_vars nodes[src]."""
    node = (fab.get("nodes") or {}).get(src) or {}
    out = {}
    for itf in node.get("interfaces", []):
        pl = itf.get("plane")
        if pl is None or itf.get("role") != "host":
            continue
        if itf.get("name") and itf.get("gateway"):
            out[str(pl)] = {"iface": itf["name"], "gateway": itf["gateway"]}
    return out

# ---- node config (view + download) -----------------------------------------
# Node names come from fabric_vars, so a request can only ever name a real node
# (whitelist) — never an arbitrary path. Configs are read from the repo the GUI
# already serves read-only: frr/<node>/frr.conf for switches, mrc-nic/<node>.json
# for the GPU virtual NICs, with the SONiC config/<node>/ tree as a fallback.
def nodes_index(fab):
    """[{name, role, plane, has_config}] for every fabric node, for the picker."""
    out = []
    for name, nd in (fab.get("nodes") or {}).items():
        out.append({"name": name, "role": nd.get("role"),
                    "plane": nd.get("plane"), "index": nd.get("index")})
    out.sort(key=lambda n: (n.get("role") or "", n.get("plane") or 0, n.get("index") or 0))
    return out

def _config_candidates(root, name, role):
    if role == "gpu":
        return [("mrc-nic profile (json)", os.path.join(root, "mrc-nic", f"{name}.json"))]
    return [("frr.conf", os.path.join(root, "frr", name, "frr.conf")),
            ("config_db.json", os.path.join(root, "config", name, "config_db.json")),
            ("frr.conf", os.path.join(root, "config", name, "frr.conf"))]

def node_config(fab, root, name):
    """(kind, text) for a node's config, or (None, None) if the node is unknown
    (not in fabric_vars) or no config file exists."""
    nd = (fab.get("nodes") or {}).get(name)
    if nd is None:
        return None, None
    for kind, p in _config_candidates(root, name, nd.get("role")):
        try:
            with open(p) as f:
                return kind, f.read()
        except OSError:
            continue
    return None, None

def configs_tgz_bytes(root):
    """A .tar.gz of every text config the fabric ships: the frr/ and SONiC
    config/ trees, the per-GPU mrc-nic profiles, and the topology descriptors.
    The mrc-nic binary is skipped (only *.json profiles are included)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for sub in ("frr", "config"):
            d = os.path.join(root, sub)
            if os.path.isdir(d):
                tar.add(d, arcname=sub)
        nicdir = os.path.join(root, "mrc-nic")
        if os.path.isdir(nicdir):
            for fn in sorted(os.listdir(nicdir)):
                if fn.endswith(".json"):
                    tar.add(os.path.join(nicdir, fn), arcname=os.path.join("mrc-nic", fn))
        for fn in ("fabric_vars.json", "srv6lab.clab.yml", "address_plan.json",
                   "fabric_address_map.txt"):
            fp = os.path.join(root, fn)
            if os.path.isfile(fp):
                tar.add(fp, arcname=fn)
    return buf.getvalue()

# ---- health join -----------------------------------------------------------
def _sample_index():
    """{(host, peer, path_id): sample} over the latest report from every host,
    plus a per-host {(peer,path_id): denied} overlay from the weigher snapshot."""
    samples, denied, srcinfo = {}, {}, {}
    now = time.time()
    with _LOCK:
        for host, rep in _REPORTS.items():
            age = now - rep.get("received_at", now)
            up = 0; tot = 0
            for s in rep.get("peers", []):
                pid = s.get("path_id")
                if pid is None:
                    continue
                samples[(host, s.get("peer"), pid)] = s
                tot += 1; up += 1 if s.get("up") else 0
            for h in ((rep.get("weigher") or {}).get("path_health") or []):
                denied[(host, h.get("peer"), h.get("path_id"))] = bool(h.get("denied"))
            srcinfo[host] = {"age_s": round(age, 1), "peers_up": up, "peers_total": tot,
                             "ts": rep.get("ts")}
    return samples, denied, srcinfo

def mesh_view(fab):
    """Every path joined to its latest reported probe health. The full path list
    always renders (from fabric_vars); health is null until a NIC reports it."""
    samples, denied, srcinfo = _sample_index()
    with _LOCK:
        bypass = set(_BYPASS)
    now = time.time()
    paths = []
    for p in (fab.get("paths") or []):
        key = (p["src"], p["dst"], p["path_id"])
        s = samples.get(key)
        health = None
        if s is not None:
            rep_age = srcinfo.get(p["src"], {}).get("age_s")
            health = {
                "up": bool(s.get("up")),
                "loss_pct": s.get("loss_pct"),
                "rtt_min": s.get("rtt_min"), "rtt_avg": s.get("rtt_avg"),
                "rtt_max": s.get("rtt_max"),
                "jitter": s.get("rtt_mdev") if s.get("rtt_mdev") is not None else s.get("jitter"),
                "ob_avg": s.get("ob_avg"), "ib_avg": s.get("ib_avg"),
                "encap_ok": bool(s.get("encap_ok", True)),
                "denied": denied.get(key, False),
                "err": s.get("err"),
                "age_s": rep_age,
            }
        drained = bool(bypass & path_nodes(p))
        paths.append({**p, "health": health, "drained": drained})
    return {"updated": now, "name": fab.get("name"), "shape": fab.get("shape"),
            "sources": srcinfo, "n_reporting": len(srcinfo),
            "bypass": sorted(bypass), "paths": paths}

# ---- HTTP ------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    server_version = "mrc-gui/1.0"
    vars_path = "fabric_vars.json"     # set per-process below
    root_dir = "."                     # served web root (repo); set per-process below

    def log_message(self, fmt, *args):
        # quiet the per-request noise; keep it to one tidy line
        print(f"[gui] {self.address_string()} {fmt % args}")

    # -- helpers --
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _send_tgz(self):
        data = configs_tgz_bytes(self.root_dir)
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition",
                         "attachment; filename=mrc-fabric-configs.tar.gz")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _fab(self):
        return load_fabric(self.vars_path) or {}

    # -- routing --
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/api/topology":
            return self._send_json({"hosts": gpu_hosts(self._fab())})
        if path == "/api/mesh-plan":
            src = (parse_qs(u.query).get("src") or [""])[0]
            fab = self._fab()
            with _LOCK:
                bypass = sorted(_BYPASS)
            return self._send_json({"src": src, "decap_sid": decap_sid_for(fab, src),
                                    "underlays": underlays_for(fab, src),
                                    "bypass": bypass,
                                    "paths": plan_for(fab, src)})
        if path == "/api/mesh":
            return self._send_json(mesh_view(self._fab()))
        if path == "/api/nodes":
            return self._send_json({"nodes": nodes_index(self._fab())})
        if path == "/api/config":
            node = (parse_qs(u.query).get("node") or [""])[0]
            kind, text = node_config(self._fab(), self.root_dir, node)
            if text is None:
                return self._send_json({"error": "no config for node", "node": node}, code=404)
            return self._send_json({"node": node, "kind": kind, "text": text})
        if path == "/api/configs.tar.gz":
            return self._send_tgz()
        if path == "/api/bypass":
            with _LOCK:
                return self._send_json({"nodes": sorted(_BYPASS)})
        if path == "/api/reports":           # debug: raw posted health
            with _LOCK:
                return self._send_json(_REPORTS)
        if path == "/api/profile/stream":
            return self._sse()
        if path in ("/", ""):                # land on the viewer
            self.send_response(302)
            self.send_header("Location", "/gui/topology.html")
            self.end_headers()
            return
        return super().do_GET()              # static files from --root

    def do_HEAD(self):
        if self.path.startswith("/api/"):
            return self._send_json({})
        return super().do_HEAD()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if path == "/api/mesh-health":
            host = body.get("host")
            if host:
                with _LOCK:
                    _REPORTS[host] = {"ts": body.get("ts"), "received_at": time.time(),
                                      "peers": body.get("peers") or [],
                                      "weigher": body.get("weigher") or {}}
            return self._send_json({"ok": True})
        if path == "/api/bypass":
            # Maintenance drain: mark node(s) so NICs steer traffic around them.
            # Accepts {"node": "<name>", "on": true|false} to toggle one, or
            # {"nodes": [...]} to set the whole list. Only real fabric nodes are
            # accepted (validated against fabric_vars).
            valid = set((self._fab().get("nodes") or {}).keys())
            with _LOCK:
                if "nodes" in body and isinstance(body["nodes"], list):
                    _BYPASS.clear()
                    _BYPASS.update(n for n in body["nodes"] if n in valid)
                elif body.get("node") in valid:
                    if body.get("on", True):
                        _BYPASS.add(body["node"])
                    else:
                        _BYPASS.discard(body["node"])
                nodes = sorted(_BYPASS)
            return self._send_json({"ok": True, "nodes": nodes})
        # NIC also posts these to a controller; accept and drop so it doesn't error.
        if path in ("/api/metrics", "/api/ev-stats", "/api/probe-tx",
                    "/api/jobs/status"):
            return self._send_json({"ok": True})
        return self._send_json({"error": "not found"}, code=404)

    def _sse(self):
        """Keepalive-only event stream. An attached NIC subscribes here for live
        profiles; this lab pushes none, so we just hold the socket open with
        comments so its reconnect loop stays quiet."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b": mrc-gui connected\n\n")
            self.wfile.flush()
            while not _STOP.is_set():
                if _STOP.wait(5.0):
                    break
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main():
    ap = argparse.ArgumentParser(description="mrc-gui backend (static viewer + path-health API)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("GUI_PORT", "8080")))
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--root", default=os.environ.get("GUI_ROOT", "."),
                    help="directory served as the web root (the repo root)")
    ap.add_argument("--vars", default=None,
                    help="path to fabric_vars.json (default: <root>/fabric_vars.json)")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    Handler.vars_path = a.vars or os.path.join(root, "fabric_vars.json")
    Handler.root_dir = root
    fab = load_fabric(Handler.vars_path)
    npaths = len((fab or {}).get("paths") or [])
    print(f"[gui] root={root}")
    print(f"[gui] fabric_vars={Handler.vars_path}  ({npaths} paths, "
          f"{len(gpu_hosts(fab or {}))} gpus)")
    if npaths == 0:
        print("[gui] NOTE: no paths[] in fabric_vars.json — regenerate with the current "
              "gen_clab_topology.py so the paths table has data.")

    handler = partial(Handler, directory=root)
    httpd = ThreadingHTTPServer((a.bind, a.port), handler)
    httpd.daemon_threads = True
    print(f"[gui] serving http://{a.bind}:{a.port}/gui/topology.html")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _STOP.set()
        httpd.shutdown()


if __name__ == "__main__":
    main()
