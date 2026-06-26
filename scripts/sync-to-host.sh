#!/usr/bin/env bash
# Push this working tree to a remote lab host over SSH (rsync).
#
# Honours .gitignore, so generated/running-lab files are NEVER transferred and
# NEVER deleted on the remote: clab-*/, fabric_vars.json, frr/, mrc-nic/ profile
# dir, *.clab.yml, __pycache__/ stay as the remote left them. The NIC binary
# nic/mrc-nic and its +x bit ARE carried (it's a file, not the ignored dir).
#
# Usage:
#   scripts/sync-to-host.sh user@host [remote-dir]
#   scripts/sync-to-host.sh fprowse@containerlab                 # -> ~/mrc-srv6-fabric
#   scripts/sync-to-host.sh fprowse@192.168.0.106 ~/lab/mrc-srv6-fabric
#
# By default it only ADDS/UPDATES files. To make the remote mirror local exactly
# (delete remote files that no longer exist locally — still preserving the
# gitignored running-lab files), set MIRROR=1:
#   MIRROR=1 scripts/sync-to-host.sh fprowse@containerlab
set -euo pipefail

dest="${1:?usage: sync-to-host.sh user@host [remote-dir]}"
remote_dir="${2:-mrc-srv6-fabric}"                 # relative to the remote $HOME
here="$(cd "$(dirname "$0")/.." && pwd)"

extra=()
[ "${MIRROR:-0}" = "1" ] && extra+=(--delete)      # excluded files are kept, not deleted

echo "rsync  $here/  ->  $dest:$remote_dir/   ${MIRROR:+(mirror/--delete)}"
rsync -avz ${extra[@]+"${extra[@]}"} \
  --filter=':- .gitignore' \
  --exclude='.git/' \
  --exclude='.claude/' \
  --exclude='.DS_Store' \
  "$here/" "$dest:$remote_dir/"

cat <<EOF

Done. Next, on the remote:
  ssh $dest
  cd $remote_dir/ansible
  ansible-playbook site-frr.yml -e gpus_per_leaf=2 -e leaves=2 -e spines=2 -e planes=2 \\
    -e spec_mode=true -e gpu_image=debian:bookworm -e mgmt_subnet=172.30.0.0/22 \\
    -e do_gui=true -e mesh_controller_url=http://172.30.0.1:8080
EOF
