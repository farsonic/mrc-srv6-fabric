# mrc multiplane spine/leaf fabric — Ansible

100% Ansible-driven: `ansible-playbook site.yml` generates the topology, deploys
the containerlab lab, and configures it. See **DEPLOY.md** for the full guide.

- **SONiC switches** (spines+leaves): configured entirely through **FRR** — base
  config (loopback, interfaces, static IPv6 routes, SRv6 locator) plus **SRv6
  static-sids** (uN, uA/End.X per neighbour, uDT6/End.DT6 on leaves), piped into
  `vtysh`. Source of truth is `fabric_vars.json`.
- **GPU hosts**: plain Linux `iproute2` via `raw` (no Python needed).

Layout:
```
gen_clab_topology.py        # topology + position-encoded addressing
address_plan.json           # editable address ranges
ansible/
  site.yml                  # generate -> deploy -> configure
  group_vars/all.yml        # fabric shape + toggles
  inventory_clab.py         # dynamic inventory from fabric_vars.json
  roles/sonic_frr/          # FRR config (base + SRv6 static-sids)
  roles/gpu_host/           # GPU iproute2 config
```
