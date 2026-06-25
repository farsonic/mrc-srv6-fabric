#!/usr/bin/env python3
"""Dynamic Ansible inventory from gen_clab_topology.py's fabric_vars.json.

Groups: switches (spines+leaves), spines, leaves, gpus. Each host gets
ansible_host = clab-<lab>-<node> over the docker connection, plus its full
per-node record (addresses, routes, srv6) as the `node` hostvar.

Path: CLAB_FABRIC_VARS env, else ../fabric_vars.json next to this script.
If the file is missing (first run, before generation), returns empty groups so
the playbook's localhost play can generate it, then `meta: refresh_inventory`.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
VARS = os.environ.get("CLAB_FABRIC_VARS", os.path.join(HERE, "..", "fabric_vars.json"))

EMPTY = {
    "_meta": {"hostvars": {}},
    "all": {"children": ["switches", "gpus"]},
    "switches": {"children": ["spines", "leaves"]},
    "spines": {"hosts": []}, "leaves": {"hosts": []}, "gpus": {"hosts": []},
}


def build():
    try:
        with open(VARS) as f:
            V = json.load(f)
    except (FileNotFoundError, ValueError):
        return EMPTY
    lab, nodes = V["name"], V["nodes"]
    inv = json.loads(json.dumps(EMPTY))  # deep copy
    grpmap = {"spine": "spines", "leaf": "leaves", "gpu": "gpus"}
    for name, r in nodes.items():
        inv[grpmap[r["role"]]]["hosts"].append(name)
        hv = {
            "ansible_connection": "community.docker.docker",
            "ansible_host": f"clab-{lab}-{name}",
            "node": r,
        }
        if r["role"] in ("spine", "leaf"):
            hv["ansible_python_interpreter"] = "/usr/bin/python3"
        inv["_meta"]["hostvars"][name] = hv
    return inv


if __name__ == "__main__":
    print(json.dumps({} if "--host" in sys.argv else build(), indent=2))
