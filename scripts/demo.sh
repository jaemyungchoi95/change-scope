#!/usr/bin/env bash
# End-to-end ChangeScope demo on synthetic fixtures. No LLM call, no network after `uv sync`.
# Approvals here are scripted stand-ins for a human reviewer and are labelled reviewer=demo-script.
# Usage: scripts/demo.sh [output-dir]   (default: runs/demo; must not exist)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:-runs/demo}
if [ -e "$OUT" ]; then echo "Output $OUT already exists; choose a new path" >&2; exit 4; fi
mkdir -p "$OUT"
CS="uv run --no-sync change-scope"
quiet() { "$@" > /dev/null; }
show() { python3 -c "import json,sys; d=json.load(sys.stdin); print('  ', $1)"; }

echo "1) Extract pending candidates from a recorded AI output"
$CS extract --source data/fixtures/t03/topology.md --source-id topology-review --source-version 1 \
  --recording data/fixtures/t03/topology-recorded.json --workspace "$OUT/ws" \
  | show "d['summary']"

echo "2) Review: approve the 5 correct relations, reject the reversed and failed ones"
decide() { quiet $CS review decide --workspace "$OUT/ws" --id "$1" --revision 1 --decision "$2" \
  --reviewer demo-script --reason "$3"; }
for id in c-64fa8cb1b9a9fb76 c-136a916921694394 c-15fa854fcfac56e7 c-cab5bf8d33af3866 c-c3e0780f7d7dbc49; do
  decide "$id" approve "quote matches the HTTP calls section"
done
decide c-9828ccf4a34cee85 reject "source says webapi calls answer; direction reversed"
decide c-02176a76dfc0b316 reject "quote is not in the source"
decide c-2eba4d9382e7afcd reject "quote is in another section"
$CS review show --workspace "$OUT/ws" | show "d['summary']"

echo "3) Export the reviewed snapshot and analyze reranker with the real engine"
quiet $CS export --workspace "$OUT/ws" --output "$OUT/s1"
$CS analyze --snapshot "$OUT/s1/snapshot.json" --target reranker --evidence data/fixtures/t06/catalog.json \
  --report "$OUT/s1-reranker.md" --output "$OUT/s1-reranker.json" \
  | show "(d['analysis_status'], d['upstream_candidates'])"

echo "4) Change proposal: remove retrieval -> reranker, approve it, build scenario S2"
python3 - "$OUT" <<'EOF'
import hashlib, json, shutil, sys
from pathlib import Path
out = Path(sys.argv[1]); src = Path("data/fixtures/t07")
dst = out / "proposal-input"; shutil.copytree(src, dst)
data = json.loads((dst / "proposals.json").read_text())
snap = out / "s1/snapshot.json"
data["base_snapshot_id"] = json.loads(snap.read_text())["snapshot_id"]
data["base_snapshot_hash"] = hashlib.sha256(snap.read_bytes()).hexdigest()
(dst / "proposals.json").write_text(json.dumps(data, indent=2) + "\n")
EOF
quiet $CS proposal init --proposals "$OUT/proposal-input/proposals.json" --base "$OUT/s1/snapshot.json" --workspace "$OUT/pw"
quiet $CS proposal decide --workspace "$OUT/pw" --id P-001 --revision 1 --decision approve --reviewer demo-script --reason "change request CR-1"
quiet $CS proposal decide --workspace "$OUT/pw" --id P-002 --revision 1 --decision reject --reviewer demo-script --reason "out of scope"
quiet $CS scenario build --workspace "$OUT/pw" --base "$OUT/s1/snapshot.json" --output "$OUT/s2"

echo "5) Compare S1 and S2"
$CS compare --base "$OUT/s1/snapshot.json" --scenario "$OUT/s2/scenario.json" --target reranker \
  --evidence data/fixtures/t06/catalog.json --report "$OUT/compare-reranker.md" \
  | show "d['comparison']['diff']"

echo "6) Holdout evaluation (hand-transcribed gold snapshot)"
$CS evaluate --questions data/holdout/questions.json --snapshot data/holdout/gold-snapshot.json \
  --output "$OUT/holdout-evaluation.json" | show "d['summary']"

cat <<EOF

Done. Reports: $OUT/s1-reranker.md, $OUT/compare-reranker.md
Open the local viewer (127.0.0.1 only):
  uv run --no-sync change-scope serve --snapshot $OUT/s1/snapshot.json --workspace $OUT/ws \\
    --evidence data/fixtures/t06/catalog.json --scenario $OUT/s2/scenario.json
  -> http://localhost:8765
Add --llm live to enable real Codex suggestions (subscription login required; see README).
EOF
