# Quickstart — SRv6 uSID fabric

Extract everything into your project dir (do NOT extract single files):

    cd ~/clab/srv6
    tar xzf mrc-fabric-ansible.tgz        # extracts gen_clab_topology.py, ansible/, address_plan.json, ...

Set the shape in `ansible/group_vars/all.yml` (defaults: single plane, 2 leaf x 2 spine,
4 GPU homed 2/leaf, name `srv6lab`). Then one command does generate -> deploy -> verify -> GPUs:

    cd ~/clab/srv6/ansible && ansible-playbook site-frr.yml

The playbook self-cleans first: it destroys any lab whose name != `fabric_name` and removes
stale `*.clab.yml` / `fabric_vars.json` / `srv6-*.sh` before generating. A rename or reshape
is therefore a one-variable edit — no manual teardown, no ghost hosts, no stale scripts.

Test the SRv6 dataplane (both scripts are regenerated every run, named to match the fabric):

    cd ~/clab/srv6 && ./srv6-test.sh      # PASS/CHECK gate (encap -> spine End.X DA-shift)
    cd ~/clab/srv6 && ./srv6-walk.sh      # per-hop DA-shift trace (carrier -> shift -> decap)

Change shape without touching files, e.g.:

    ansible-playbook site-frr.yml -e planes=2 -e fabric_name=twoplane
    ansible-playbook site-frr.yml -e seg6_mode=kernel
    ansible-playbook site-frr.yml -e gpus_per_leaf=0      # legacy full mesh

Tear down:

    cd ~/clab/srv6 && sudo containerlab destroy -t $(grep '^fabric_name:' ansible/group_vars/all.yml | awk '{print $2}').clab.yml --cleanup

## Packet capture (Edgeshark)

`do_edgeshark: true` (default) brings up the Edgeshark web UI alongside the lab — a
single GUI to capture ANY interface on ANY node (leaf/spine/gpu), piped to your local
Wireshark. It's a host service (introspects all container namespaces), not part of the
.clab.yml topology.

    # web UI (after the playbook runs):
    http://<containerlab-host>:5001

    # one-time on your laptop: install the cshargextcap plugin + packetflix:// handler
    # https://containerlab.dev/manual/wireshark/#edgeshark-integration

    # turn it off / tear it down:
    ansible-playbook site-frr.yml -e do_edgeshark=false
    # or directly:
    docker compose -p edgeshark down
