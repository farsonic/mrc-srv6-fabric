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

import argparse, base64, hashlib, http.client, io, json, os, select, socket, \
       struct, tarfile, threading, time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---- optional Docker access (console feature, opt-in) ----------------------
# The GUI reaches the node containers through the Docker Engine API over the
# mounted socket — no docker CLI needed in the image. Off unless the socket is
# present (operator mounts it explicitly), so the default GUI stays unprivileged.
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
# Versionless Docker Engine API path prefix: the daemon negotiates its own
# version. A hardcoded version (e.g. /v1.43) 400s ("client version too new") on
# any daemon whose max API version is older, which broke the console on hosts
# with an older Docker. Empty = version-agnostic. Override with MRC_DOCKER_API.
_DOCKER_API = os.environ.get("MRC_DOCKER_API", "").strip("/")
def _api(path):
    return (f"/{_DOCKER_API}{path}" if _DOCKER_API else path)
_CTR_CACHE = {}        # node -> container name (resolved once)
_CTR_LOCK = threading.Lock()

def docker_available():
    try:
        return os.path.exists(DOCKER_SOCK)
    except OSError:
        return False

class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client speaking to the Docker Engine API over its unix socket."""
    def __init__(self, timeout=15):
        super().__init__("localhost", timeout=timeout)
    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(DOCKER_SOCK)
        self.sock = s

def _docker_json(method, path, body=None, timeout=15):
    c = _UnixHTTPConnection(timeout=timeout)
    data = json.dumps(body).encode() if body is not None else None
    c.request(method, _api(path), body=data,
              headers={"Content-Type": "application/json", "Host": "docker"})
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, (json.loads(raw) if raw else None)

def container_for(fab, node):
    """The running container name for a fabric node (clab names it
    clab-<lab>-<node>). Only nodes present in fabric_vars are resolvable."""
    if node not in (fab.get("nodes") or {}):
        return None
    with _CTR_LOCK:
        if node in _CTR_CACHE:
            return _CTR_CACHE[node]
    st, arr = _docker_json("GET", "/containers/json?all=1")   # raises on socket error
    if st != 200:
        raise RuntimeError(f"GET /containers/json -> HTTP {st}")
    if not arr:
        return None
    hit = None
    for ctr in arr:
        for nm in (ctr.get("Names") or []):
            nm = nm.lstrip("/")
            if nm == node or nm.endswith("-" + node):
                hit = nm
                break
        if hit:
            break
    if hit:
        with _CTR_LOCK:
            _CTR_CACHE[node] = hit
    return hit

def _demux(raw):
    """Flatten Docker's multiplexed (non-TTY) exec stream: repeated frames of an
    8-byte header [stream, 0,0,0, size(4, big-endian)] followed by `size` bytes."""
    out = []
    i, n = 0, len(raw)
    while i + 8 <= n:
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if i < n:                      # tolerate a non-framed tail (older daemons)
        out.append(raw[i:])
    return b"".join(out)

def docker_exec(container, argv, timeout=20):
    """Run argv in a container, non-interactively; return (rc, text)."""
    st, ex = _docker_json("POST", f"/containers/{container}/exec",
                          {"AttachStdout": True, "AttachStderr": True,
                           "Tty": False, "Cmd": argv}, timeout=timeout)
    if st not in (200, 201) or not ex or "Id" not in ex:
        return 1, f"exec create failed (HTTP {st})"
    eid = ex["Id"]
    c = _UnixHTTPConnection(timeout=timeout)
    c.request("POST", _api(f"/exec/{eid}/start"),
              body=json.dumps({"Detach": False, "Tty": False}).encode(),
              headers={"Content-Type": "application/json", "Host": "docker"})
    r = c.getresponse()
    raw = r.read()
    c.close()
    text = _demux(raw).decode("utf-8", "replace")
    rc = None
    try:
        _, info = _docker_json("GET", f"/exec/{eid}/json")
        rc = (info or {}).get("ExitCode")
    except Exception:
        pass
    return (rc if rc is not None else 0), text

def docker_exec_tty(container):
    """Create an interactive TTY exec (a login shell) and hijack its stream.
    Returns (exec_id, raw_socket, initial_bytes) or (None, None, b'')."""
    st, ex = _docker_json("POST", f"/containers/{container}/exec",
                          {"AttachStdin": True, "AttachStdout": True,
                           "AttachStderr": True, "Tty": True,
                           "Cmd": ["/bin/sh", "-lc",
                                   "if command -v bash >/dev/null 2>&1; then exec bash; "
                                   "else exec sh; fi"]})
    if st not in (200, 201) or not ex or "Id" not in ex:
        return None, None, b""
    eid = ex["Id"]
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(DOCKER_SOCK)
    body = json.dumps({"Detach": False, "Tty": True}).encode()
    req = (f"POST {_api('/exec/'+eid+'/start')} HTTP/1.1\r\nHost: docker\r\n"
           "Content-Type: application/json\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n"
           f"Content-Length: {len(body)}\r\n\r\n").encode() + body
    s.sendall(req)
    buf = b""
    while b"\r\n\r\n" not in buf:            # consume the 101 response headers
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    _, _, rest = buf.partition(b"\r\n\r\n")   # any TTY bytes already delivered
    return eid, s, rest

# ---- minimal WebSocket (RFC6455) over the stdlib http.server socket --------
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def ws_accept_key(key):
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()

def ws_send(sock, data, opcode=0x2):
    if isinstance(data, str):
        data = data.encode()
    hdr = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126); hdr += struct.pack(">H", n)
    else:
        hdr.append(127); hdr += struct.pack(">Q", n)
    try:
        sock.sendall(bytes(hdr) + data)
    except OSError:
        pass

class _WSReader:
    """Accumulates bytes and yields (opcode, payload) for each complete client
    frame (client->server frames are always masked)."""
    def __init__(self):
        self.buf = bytearray()
    def feed(self, data):
        self.buf += data
    def frames(self):
        while True:
            if len(self.buf) < 2:
                return
            b0, b1 = self.buf[0], self.buf[1]
            op = b0 & 0x0f
            masked = b1 & 0x80
            ln = b1 & 0x7f
            idx = 2
            if ln == 126:
                if len(self.buf) < 4:
                    return
                ln = struct.unpack(">H", self.buf[2:4])[0]; idx = 4
            elif ln == 127:
                if len(self.buf) < 10:
                    return
                ln = struct.unpack(">Q", self.buf[2:10])[0]; idx = 10
            if masked:
                if len(self.buf) < idx + 4:
                    return
                mask = self.buf[idx:idx + 4]; idx += 4
            if len(self.buf) < idx + ln:
                return
            payload = bytes(self.buf[idx:idx + ln])
            if masked:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(ln))
            del self.buf[:idx + ln]
            yield op, payload

# ---- live state ------------------------------------------------------------
_LOCK = threading.Lock()
_REPORTS = {}          # host -> {"ts","received_at","peers":[...],"weigher":{...}}
_BYPASS = set()        # node names an operator has drained for maintenance
_BYPASS_PLANES = set() # whole planes (ints) an operator has drained for maintenance
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

def _bypass_now():
    """A snapshot of the current drain state: (bypassed node names, bypassed
    plane ids). Read under the lock so a concurrent toggle is atomic."""
    with _LOCK:
        return set(_BYPASS), set(_BYPASS_PLANES)

def is_drained(p, bypass_nodes, bypass_planes):
    """A path is drained if its plane is bypassed OR any node it transits is."""
    return (p.get("plane") in bypass_planes) or bool(bypass_nodes & path_nodes(p))

def plan_for(fab, src):
    """The per-path probe plan for one source host: every path whose src is this
    host, shaped for the NIC's /api/mesh-plan consumer. `drained` marks a path
    that transits an operator-bypassed node OR plane — the NIC keeps probing it
    but steers data traffic off it."""
    out = []
    bn, bp = _bypass_now()
    for p in (fab.get("paths") or []):
        if p.get("src") != src:
            continue
        out.append({
            "peer": p["dst"], "dst": p["dst_addr"],
            "spine": p.get("spine"), "path_id": p["path_id"],
            "fwmark": p["fwmark"], "probe_sid": p["usid"], "usid": p["usid"],
            "plane": p.get("plane"),   # which underlay plane this path egresses (dual-plane)
            "drained": is_drained(p, bn, bp),
        })
    return out

def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def fabric_planes(fab):
    """The set of plane ids present in the fabric (from nodes), so a drain-plane
    request can be validated — works for a single-plane deploy too."""
    planes = set()
    for nd in (fab.get("nodes") or {}).values():
        pl = nd.get("plane")
        if isinstance(pl, int) and pl > 0:
            planes.add(pl)
    for p in (fab.get("paths") or []):
        pl = p.get("plane")
        if isinstance(pl, int) and pl > 0:
            planes.add(pl)
    return planes

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
    bn, bp = _bypass_now()
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
                "reorder": s.get("reorder"), "reorder_pct": s.get("reorder_pct"),
                "encap_ok": bool(s.get("encap_ok", True)),
                "denied": denied.get(key, False),
                "err": s.get("err"),
                "age_s": rep_age,
            }
        paths.append({**p, "health": health, "drained": is_drained(p, bn, bp)})
    return {"updated": now, "name": fab.get("name"), "shape": fab.get("shape"),
            "sources": srcinfo, "n_reporting": len(srcinfo),
            "bypass": sorted(bn), "bypass_planes": sorted(bp), "paths": paths}

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
            bn, bp = _bypass_now()
            return self._send_json({"src": src, "decap_sid": decap_sid_for(fab, src),
                                    "underlays": underlays_for(fab, src),
                                    "bypass": sorted(bn), "bypass_planes": sorted(bp),
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
                return self._send_json({"nodes": sorted(_BYPASS),
                                        "planes": sorted(_BYPASS_PLANES)})
        if path == "/api/console-info":
            return self._send_json({"enabled": docker_available()})
        if path == "/api/console" and \
                self.headers.get("Upgrade", "").lower() == "websocket":
            node = (parse_qs(u.query).get("node") or [""])[0]
            return self._console_ws(node)
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
            # Maintenance drain: mark node(s) or whole plane(s) so NICs steer
            # traffic around them (probes keep running). Accepts:
            #   {"node": "<name>", "on": bool}   toggle one node
            #   {"nodes": [...]}                 set the node list
            #   {"plane": <int>, "on": bool}     toggle a whole plane
            #   {"planes": [...]}                set the plane list
            # Only real fabric nodes / planes are accepted (validated vs fabric_vars).
            fab = self._fab()
            valid = set((fab.get("nodes") or {}).keys())
            vplanes = fabric_planes(fab)
            with _LOCK:
                if "nodes" in body and isinstance(body["nodes"], list):
                    _BYPASS.clear()
                    _BYPASS.update(n for n in body["nodes"] if n in valid)
                elif body.get("node") in valid:
                    if body.get("on", True):
                        _BYPASS.add(body["node"])
                    else:
                        _BYPASS.discard(body["node"])
                if "planes" in body and isinstance(body["planes"], list):
                    _BYPASS_PLANES.clear()
                    _BYPASS_PLANES.update(int(p) for p in body["planes"]
                                          if _as_int(p) in vplanes)
                elif body.get("plane") is not None and _as_int(body["plane"]) in vplanes:
                    pl = _as_int(body["plane"])
                    if body.get("on", True):
                        _BYPASS_PLANES.add(pl)
                    else:
                        _BYPASS_PLANES.discard(pl)
                nodes = sorted(_BYPASS); planes = sorted(_BYPASS_PLANES)
            return self._send_json({"ok": True, "nodes": nodes, "planes": planes})
        if path == "/api/exec":
            if not docker_available():
                return self._send_json({"error": "console disabled (mrc-gui has no "
                                        "docker socket)"}, code=503)
            fab = self._fab()
            node = body.get("node") or ""
            cmd = (body.get("cmd") or "").strip()
            try:
                ctr = container_for(fab, node)
            except Exception as e:
                return self._send_json({"error": f"docker API error: {e}"}, code=502)
            if not ctr:
                return self._send_json({"error": "unknown node", "node": node}, code=404)
            if not cmd:
                return self._send_json({"error": "empty command"}, code=400)
            try:
                rc, out = docker_exec(ctr, ["sh", "-lc", cmd])
            except Exception as e:
                return self._send_json({"error": f"exec failed: {e}"}, code=502)
            if len(out) > 200_000:            # cap runaway output
                out = out[:200_000] + "\n… (truncated)"
            return self._send_json({"node": node, "container": ctr, "rc": rc, "out": out})
        # NIC also posts these to a controller; accept and drop so it doesn't error.
        if path in ("/api/metrics", "/api/ev-stats", "/api/probe-tx",
                    "/api/jobs/status"):
            return self._send_json({"ok": True})
        return self._send_json({"error": "not found"}, code=404)

    def _console_ws(self, node):
        """Bridge a browser WebSocket to an interactive `docker exec` TTY on the
        node's container. The GUI (xterm.js) sends binary keystroke frames and
        JSON text frames for {type:'resize',cols,rows}; we stream the TTY output
        back as binary frames. Requires the docker socket (console opt-in)."""
        self.close_connection = True
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._send_json({"error": "bad websocket handshake"}, code=400)
        # Upgrade FIRST, then report any problem AS TEXT over the socket, so the
        # user sees the actual reason in the terminal instead of a silent close.
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept_key(key))
        self.end_headers()
        cli = self.connection
        def fail(msg):
            ws_send(cli, ("\r\n[console] " + msg + "\r\n").encode(), 0x1)
        if not docker_available():
            return fail("disabled — mrc-gui has no docker socket "
                        "(redeploy with -e gui_console=true)")
        try:
            ctr = container_for(self._fab(), node)
        except Exception as e:
            return fail(f"docker API error: {e}")
        if not ctr:
            return fail(f"no running container found for node '{node}'")
        try:
            eid, dsock, initial = docker_exec_tty(ctr)
        except Exception as e:
            return fail(f"exec into {ctr} failed: {e}")
        if not dsock:
            return fail(f"could not open a shell on {ctr}")
        if initial:
            ws_send(cli, initial, 0x2)
        self._bridge(cli, dsock, eid)

    def _bridge(self, cli, dsock, eid):
        reader = _WSReader()
        try:
            while True:
                r, _, _ = select.select([cli, dsock], [], [], 45)
                if not r:
                    ws_send(cli, b"", 0x9)          # idle ping keepalive
                    continue
                if dsock in r:
                    out = dsock.recv(65536)
                    if not out:
                        break
                    ws_send(cli, out, 0x2)
                if cli in r:
                    data = cli.recv(65536)
                    if not data:
                        break
                    reader.feed(data)
                    for op, payload in reader.frames():
                        if op == 0x8:              # client close
                            raise ConnectionError
                        elif op == 0x9:            # ping -> pong
                            ws_send(cli, payload, 0xA)
                        elif op == 0x1:            # text control (resize)
                            try:
                                m = json.loads(payload or b"{}")
                                if m.get("type") == "resize":
                                    _docker_json("POST", f"/exec/{eid}/resize"
                                                 f"?h={int(m['rows'])}&w={int(m['cols'])}")
                            except Exception:
                                pass
                        elif op == 0x2:            # keystrokes -> TTY
                            dsock.sendall(payload)
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            for s in (dsock, cli):
                try:
                    s.close()
                except OSError:
                    pass

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
