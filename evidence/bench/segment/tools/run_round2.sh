#!/usr/bin/env bash
# usage: run_round2.sh <round> <plan-file>; plan lines: label case out-name run# [env assignments...] -- [bench args...]
# Copies each result JSON into the checkout immediately (the checkout copy is authoritative).
set -uo pipefail
round=$1; plan=$2
repo=/Users/michaelchen/Projects/Apps/coverage-map
ev=/Users/michaelchen/coverage-map-work/evidence/bench/segment/round-$round
dest=$repo/evidence/bench/segment/round-$round
mkdir -p "$dest"
log=/tmp/s2/round-$round.log
while IFS= read -r line <&3; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  read -r label case outname run rest <<<"$line"
  envs="${rest%%--*}"; args="${rest#*--}"
  out="$ev/$case-$label-r$run.json"
  echo "$(date +%H:%M:%S) start $label $case r$run" >>"$log"
  # shellcheck disable=SC2086
  res=$("$repo/scripts/mini.sh" --heavy=8 -- env $envs python bench/bench_segment.py --case "$case" --out-name "$outname" --out "$out" $args 2>>"$log.stderr")
  rc=$?
  scp -q "mini:$out" "$dest/" 2>>"$log.stderr" || echo "$(date +%H:%M:%S) COPY FAILED $out" >>"$log"
  echo "$(date +%H:%M:%S) done $label $case r$run rc=$rc $res" >>"$log"
done 3<"$plan"
echo "$(date +%H:%M:%S) ROUND $round COMPLETE" >>"$log"
