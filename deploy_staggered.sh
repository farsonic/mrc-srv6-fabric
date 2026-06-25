#!/usr/bin/env bash
# Staggered containerlab deploy for docker-sonic-vs fabrics.
# Boots SONiC switches in small waves, waiting for each wave to create its
# Ethernet ports before starting the next, so syncd/orchagent never thunder-herd
# the host (the cold-boot port race). GPUs (instant boot) go last in one wave.
#
# usage:
#   ./deploy_staggered.sh -t twoplane.clab.yml -v fabric_vars.json [-w 2] [-p 32] [--fresh]
set -uo pipefail

TOPO=""; VARS=""; WAVE=2; PORTS=32; TRIES=40; DELAY=8; FRESH=0
usage(){ echo "usage: $0 -t <topo.clab.yml> -v <fabric_vars.json> [-w wave_size] [-p ports] [--fresh]"; exit 1; }
while [ $# -gt 0 ]; do case "$1" in
  -t) TOPO="$2"; shift 2;;
  -v) VARS="$2"; shift 2;;
  -w) WAVE="$2"; shift 2;;
  -p) PORTS="$2"; shift 2;;
  --fresh) FRESH=1; shift;;
  *) usage;;
esac; done
[ -n "$TOPO" ] && [ -n "$VARS" ] || usage

NAME=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["name"])' "$VARS")
mapfile -t SWITCHES < <(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));[print(k) for k,v in d["nodes"].items() if v["role"] in ("spine","leaf")]' "$VARS")
mapfile -t GPUS < <(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));[print(k) for k,v in d["nodes"].items() if v["role"]=="gpu"]' "$VARS")

echo ">> fabric '$NAME': ${#SWITCHES[@]} switches in waves of $WAVE, then ${#GPUS[@]} GPUs"

if [ "$FRESH" -eq 1 ]; then
  echo ">> destroying any existing lab (frees the host)"
  sudo containerlab destroy -t "$TOPO" --cleanup || true
fi

wait_ports(){  # args: node names; wait until each has >= PORTS Ethernet netdevs
  local n c i
  for n in "$@"; do
    c=0
    for i in $(seq 1 "$TRIES"); do
      c=$(docker exec "clab-${NAME}-${n}" sh -c 'ls /sys/class/net 2>/dev/null | grep "^Ethernet" | wc -l' 2>/dev/null || echo 0)
      c=$(printf '%s' "$c" | tr -dc '0-9'); c=${c:-0}
      printf "   %-12s ports=%-3s (try %s/%s, load=%s)\n" "$n" "$c" "$i" "$TRIES" "$(cut -d' ' -f1 /proc/loadavg)"
      [ "$c" -ge "$PORTS" ] && break
      sleep "$DELAY"
    done
    [ "$c" -ge "$PORTS" ] || { echo "!! $n did not reach $PORTS ports — host may still be too loaded; lower -w or add cores"; return 1; }
  done
}

i=0
while [ "$i" -lt "${#SWITCHES[@]}" ]; do
  wave=("${SWITCHES[@]:$i:$WAVE}")
  csv=$(IFS=,; echo "${wave[*]}")
  echo ">> deploying switch wave: $csv"
  sudo containerlab deploy -t "$TOPO" --node-filter "$csv" || { echo "!! deploy failed for $csv"; exit 1; }
  wait_ports "${wave[@]}" || exit 1
  i=$((i+WAVE))
done

if [ "${#GPUS[@]}" -gt 0 ]; then
  csv=$(IFS=,; echo "${GPUS[*]}")
  echo ">> deploying GPU wave: $csv"
  sudo containerlab deploy -t "$TOPO" --node-filter "$csv" || { echo "!! GPU deploy failed"; exit 1; }
fi

echo ">> staggered deploy complete — all switches made ports"
