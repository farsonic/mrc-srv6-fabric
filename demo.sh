#!/usr/bin/env bash
# =============================================================================
# MRC SRv6 Fabric — end-to-end demo walkthrough
#
# Runs REAL actions against a live lab and tells you what to watch in the GUI.
# Paced with prompts (Enter to advance).  Nothing here is simulated — every
# impairment is real netem; every metric is a real probe measurement.
#
#   ./demo.sh                 # interactive, watch the GUI alongside
#   AUTO=1 ./demo.sh          # unattended (sleeps instead of prompts)
#   GUI=http://<host>:8080 ./demo.sh
#
# Prereqs: the lab is deployed with  -e do_gui=true -e gui_console=true
#          -e spec_mode=true -e compute_carriers=true  (see README).
# =============================================================================
set -uo pipefail
GUI="${GUI:-http://localhost:8080}"; API="$GUI/api"; CLAB="${CLAB:-clab-srv6lab}"
AUTO="${AUTO:-0}"

b=$'\033[1m'; c=$'\033[0;36m'; y=$'\033[0;33m'; m=$'\033[0;35m'; g=$'\033[0;32m'; d=$'\033[2m'; z=$'\033[0m'
hdr(){ printf '\n%s══════════════════════════════════════════════════════════════%s\n%s  %s%s\n' "$c" "$z" "$b$c" "$*" "$z"; }
say(){ printf '   %s\n' "$*"; }
run(){ printf '%s $ %s%s\n' "$y" "$*" "$z"; }
eye(){ printf '%s   👁  %s%s\n' "$m" "$*" "$z"; }
ok(){  printf '%s   ✓ %s%s\n' "$g" "$*" "$z"; }
pause(){ if [ "$AUTO" = 1 ]; then sleep "${1:-6}"; else printf '\n%s   — Enter to continue —%s' "$d" "$z"; read -r _; fi; }
sweep(){ printf '%s   …waiting %ss for a probe sweep…%s\n' "$d" "${1:-30}" "$z"; sleep "${1:-30}"; }
api(){ curl -s "$API/$1"; }
post(){ curl -s -X POST "$API/$1" -H 'Content-Type: application/json' -d "$2"; }
PY(){ python3 -c "$1"; }

mesh_up(){ api mesh | PY "import sys,json;d=json.load(sys.stdin);ps=d['paths'];u=sum(1 for p in ps if (p.get('health') or {}).get('up'));mm=sum(1 for p in ps if p.get('health'));print(f'   fabric: {u}/{mm} measured paths up')"; }
show_paths(){ # $1 = spine filter
  api mesh | PY "
import sys,json
for p in sorted([p for p in json.load(sys.stdin)['paths'] if p.get('health') and p.get('spine')=='$1'],key=lambda x:(x['src'],x['dst'])):
  h=p['health']; w=p.get('weight'); w=f'{round(w*100)}%' if w is not None else '·'
  print(f\"     {p['src']}->{p['dst']:5} loss={str(h['loss_pct']):>5}  rtt={str(h.get('rtt_avg')):>6}  share={w:>4}\")"; }

# --------------------------------------------------------------------------
hdr "MRC SRv6 Fabric — live demo"
say "GUI:  ${b}$GUI/gui/topology.html${z}"
say "Open it now — keep it visible; this script drives it."
if ! api topology >/dev/null 2>&1; then
  printf '%s   ! cannot reach %s — is the lab + GUI up?%s\n' "$y" "$API" "$z"; exit 1
fi
mesh_up
pause

# --------------------------------------------------------------------------
hdr "1. The fabric & SRv6 source routing"
say "2 isolated planes; each GPU has one NIC per plane. The core is a dumb IPv6"
say "LPM forwarder — all path choice is at the NIC via SRv6 uSID carriers."
eye "GUI: see PLANE 1 / PLANE 2, spines (orange) → leaves (teal) → gpus (blue)."
eye "GUI: click the row ${b}gpu1 → gpu3 (spine1, plane 1)${z} in the paths table."
eye "     → the 'SRv6 carrier · outer DA per hop' box appears. Click ${b}End.X · uSID shift${z}"
eye "       to read what that hop does. Watch the outer DA change at the spine."
eye "GUI: click the ${b}gpu1${z} node → inspector shows 'SRv6 formula — inputs → carriers':"
eye "     the ~700-byte descriptor and the carriers it DERIVES (computed, not stored)."
pause

# --------------------------------------------------------------------------
hdr "2. Always-on probing — real per-path health & traffic share"
say "Every NIC probes every path (loss, RTT, one-way delay, jitter, reorder, ECN)."
say "The adaptive-spray weigher turns that health into a traffic distribution."
mesh_up
say "Baseline traffic share across gpu1→gpu3's 4 paths (healthy = ~even):"
show_paths spine1-p1
eye "GUI: the ${b}share${z} column + leaf↔spine ${b}edge thickness${z} = live traffic %."
eye "GUI: top-right ${b}sparkline${z} = paths-up / loss / rtt over time."
pause

# --------------------------------------------------------------------------
hdr "3. Real impairment → adaptive spray steers away"
LINK="leaf1-p1:eth1 ↔ spine1-p1:eth1"
say "Applying REAL netem: 30% loss on the ${b}leaf1↔spine1 (plane 1)${z} link, BOTH ends."
run "POST /api/impair  {link: '$LINK', loss:30}"
post impair "{\"link\":\"$LINK\",\"loss\":30,\"on\":true}" | PY "import sys,json;print('   ',('applied to both ends' if json.load(sys.stdin).get('ok') else 'FAILED'))"
eye "GUI (⚡ Impair button): it now lists this impairment (live-verified)."
sweep 35
say "Paths via spine1-p1 — loss climbs, and share collapses to the floor:"
show_paths spine1-p1
ok "The weigher measured real loss and STEERED TRAFFIC OFF spine1-p1 (share → ~5%)."
eye "GUI: that spine's edges ${b}thin${z}; the ${b}share${z} column drops; loss shows in the"
eye "     ← return column (netem direction). History sparkline logs the spike."
pause

# --------------------------------------------------------------------------
hdr "4. Fault localization (Clustermapper) — WHICH component?"
say "Now fail a GPU's plane link hard: 100% loss on ${b}gpu3↔leaf2 (plane 2)${z}, both ends."
GL="$(api impair | PY "import sys,json;print(next(l['key'] for l in json.load(sys.stdin)['links'] if 'gpu3:' in l['key'] and 'p2' in l['key']))")"
run "POST /api/impair  {link:'$GL', loss:100}"
post impair "{\"link\":\"$GL\",\"loss\":100,\"on\":true}" >/dev/null
sweep 35
say "Instead of a list of red rows, the system localizes the culprit:"
api faults | PY "import sys,json
fs=json.load(sys.stdin)['faults']
[print(f\"     [{f['kind']}] {f['where']} — {f['affected']} paths\") for f in fs] or print('     (none yet — give it another sweep)')"
eye "GUI: the ${b}fault banner${z} (top) names the component; click it to highlight on the map."
pause

# --------------------------------------------------------------------------
hdr "5. Maintenance drain — steer off a node, keep probing it"
say "Drain ${b}spine2 in plane 1${z} for 'maintenance' — traffic leaves it in ~2s,"
say "but it keeps being probed so you can see when it is safe to return."
run "POST /api/bypass  {node:'spine2-p1', on:true}"
post bypass '{"node":"spine2-p1","on":true}' >/dev/null
sweep 8
show_paths spine2-p1
eye "GUI: spine2-p1's paths show ${b}drained${z}; traffic re-sprays onto survivors."
say "Restoring it…"; post bypass '{"node":"spine2-p1","on":false}' >/dev/null; ok "restored"
pause

# --------------------------------------------------------------------------
hdr "6. Collectives — shape the fabric like a training job"
say "By default every GPU meshes with every GPU. Real jobs don't. Allocate just"
say "${b}gpu1 ↔ gpu4${z} to talk; the rest go idle (no carriers, no probing)."
run "POST /api/collectives  {groups:[[gpu1,gpu4]]}"
post collectives '{"groups":[["gpu1","gpu4"]]}' | PY "import sys,json;d=json.load(sys.stdin);print('   ',{k:[e['peer'] for e in v] for k,v in d['map'].items()})"
say "Readiness pre-flight (spec: 'ensure all NIC ports operational at job startup'):"
api "readiness?members=gpu1,gpu4" | PY "import sys,json;d=json.load(sys.stdin);print('   ',('READY' if d['go'] else 'NOT READY: '+str(d['blockers'])),'—',d['paths_measured'],'member paths measured')"
eye "GUI (◇ Collectives): also builds TP/DP/PP/EP presets + ring/mesh/star groups."
say "Back to full mesh…"; post collectives '{"clear":true}' >/dev/null; ok "cleared"
pause

# --------------------------------------------------------------------------
hdr "7. Scale planning — size a real fabric"
say "Plan a fabric from GPU count, ports, speeds, planes — and see the config cost."
run "python3 plan_fabric.py --gpus 4096 --ports-per-leaf 64 --port-speed 800 --nic-speed 100 --planes 4"
python3 plan_fabric.py --gpus 4096 --ports-per-leaf 64 --port-speed 800 --nic-speed 100 --planes 4 2>/dev/null \
  | sed -n '/MRC SRv6 fabric/,/deploy (from/p' | sed 's/^/     /' | head -20
say "Add --dry-run to project on-disk config size (and the O(GPUs² · planes · spines)"
say "all-to-all wall that --compute-carriers / --sparse collapse)."
pause

# --------------------------------------------------------------------------
hdr "8. Reset to a clean fabric"
say "Clearing every impairment applied during the demo…"
api impair | PY "import sys,json;[print(a['link']) for a in json.load(sys.stdin).get('active',[])]" | while IFS= read -r L; do
  [ -n "$L" ] && post impair "{\"link\":\"$L\",\"on\":false}" >/dev/null && printf '   cleared: %s\n' "$L"
done
post bypass '{"nodes":[],"planes":[]}' >/dev/null; post collectives '{"clear":true}' >/dev/null
say "Give the paths a minute to resurrect…"; [ "$AUTO" = 1 ] && sleep 45 || true
mesh_up
ok "Demo complete. For a pristine 40/40, redeploy (see README 'Bring the lab up')."
