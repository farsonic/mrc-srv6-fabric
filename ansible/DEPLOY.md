# Deploying the multiplane spine/leaf fabric — 100% Ansible

One playbook generates the topology, deploys the containerlab lab, and configures
everything (SONiC switches via FRR incl. SRv6 static-sids; GPU hosts via iproute2).
Run it on the containerlab host.

## 0. One-time prerequisites
```
pip install ansible
ansible-galaxy collection install community.docker
```
`docker` and `containerlab` must be on PATH. Switches use SONiC's own python3;
GPU hosts need nothing (configured via `raw`).

## 1. Set the fabric shape
Edit `ansible/group_vars/all.yml`:
```
fabric_name: twoplane
sonic_image: docker-sonic-vs:latest
gpus: 4
leaves: 2
spines: 2
planes: 2
```

## 2. Run it
```
cd ansible
ansible-playbook site.yml
```
That single command:
1. generates `../twoplane.clab.yml` + `../fabric_vars.json` (the source of truth),
2. `containerlab deploy --reconfigure`,
3. refreshes inventory from the generated vars,
4. waits for each SONiC node to create ports,
5. configures switches (FRR: loopback, interfaces, static routes, SRv6 locator + static-sids),
6. configures GPU hosts (IPv6 addresses + ECMP static routes).

`containerlab deploy` needs root — if sudo prompts for a password, add `-K`:
```
ansible-playbook site.yml -K
```

## 3. Re-run behavior (toggles in group_vars/all.yml)
- Re-push config only, skip regen/redeploy:
  ```
  ansible-playbook site.yml -e do_generate=false -e do_deploy=false
  ```
- Only the switches, or only the GPUs:
  ```
  ansible-playbook site.yml -e do_generate=false -e do_deploy=false --limit switches
  ansible-playbook site.yml -e do_generate=false -e do_deploy=false --limit gpus
  ```

## 4. Verify
The switch play prints `show ipv6 route static` and `show segment-routing srv6
static-sids` for each switch. Then test the dataplane:
```
docker exec clab-twoplane-gpu1 ping6 -c3 fd00:9:1:102::2     # gpu1 -> gpu2, plane 1
docker exec clab-twoplane-gpu1 ping6 -c3 fd00:9:2:102::2     # gpu1 -> gpu2, plane 2
```

## 5. Teardown
```
cd .. && sudo containerlab destroy -t twoplane.clab.yml --cleanup
```

## Knobs (group_vars/all.yml or -e)
- `sonic_port_step` (4): clab ethN -> SONiC Ethernet{(N-1)*step}; set 1 for 1:1 images.
- `configure_srv6_locator` (true), `configure_srv6_sids` (true): toggle SRv6 config.
- `do_generate` / `do_deploy` (true): lifecycle toggles described above.

## Notes
- FRR is reached with `vtysh` directly inside each switch (FRR 10.5 on this image).
- SRv6 static-sids are applied in an isolated step that reports errors without
  failing the run, so the base routing always lands even if a SID line needs a tweak.
