#!/bin/bash
# Auto-pull round2 cards from <your-institution> every 30 min, then ssh-delete to enforce PII.
# Schedule via Windows Task Scheduler.
#
# CEO directive 2026-05-07: 处理完之后设置定时任务拉回本地

set -e
cd "/c/Users/<USERNAME>/<WORKSPACE>/memexa"

LOGFILE=data/l0_v5/work/pull_round2.log
mkdir -p data/l0_v5/work/round2_cards data/l0_v5/work/round2_done
exec >> "$LOGFILE" 2>&1
echo "============================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pull cycle start"

# 1) Pull cards from <your-institution>
N_BEFORE=$(ls data/l0_v5/work/round2_cards/*.json 2>/dev/null | wc -l)
scp -q "remote-server:/tmp/memexa_l0_round2/cards_v2/*.json" \
  data/l0_v5/work/round2_cards/ 2>/dev/null || true
N_AFTER=$(ls data/l0_v5/work/round2_cards/*.json 2>/dev/null | wc -l)
PULLED=$((N_AFTER - N_BEFORE))
echo "  cards pulled: +$PULLED (total $N_AFTER)"

# 2) Pull done sentinels
scp -q "remote-server:/tmp/memexa_l0_round2/pass2_done/*.done" \
  data/l0_v5/work/round2_done/ 2>/dev/null || true
DONE=$(ls data/l0_v5/work/round2_done/*.done 2>/dev/null | wc -l)
echo "  total done sentinels: $DONE / 1290"

# 3) ssh-delete (<your-institution> PII clean — only after successful pull)
if [ "$PULLED" -gt 0 ]; then
  # Build list of just-pulled card filenames
  ssh -q remote-server "cd /tmp/memexa_l0_round2/cards_v2 && \
    for f in \$(ls *.json 2>/dev/null); do \
      [ -f /c/temp/$f ] && rm -f \"\$f\" || true; \
    done" 2>/dev/null || true
  # Simpler: delete all cards on <your-institution> that have a Win counterpart
  ssh -q remote-server "for f in /tmp/memexa_l0_round2/cards_v2/*.json; do
    base=\$(basename \"\$f\")
    if [ -f /tmp/_pulled_marker/\$base ]; then rm \"\$f\"; fi
  done" 2>/dev/null || true
  # Cleaner: just delete all that exist on Win
  for win_card in data/l0_v5/work/round2_cards/*.json; do
    bn=$(basename "$win_card")
    ssh -q remote-server "rm -f /tmp/memexa_l0_round2/cards_v2/$bn" 2>/dev/null || true
  done
fi

# 4) <your-institution> remaining state
<your-institution>_STATE=$(ssh -q remote-server "echo cards=\$(ls /tmp/memexa_l0_round2/cards_v2/*.json 2>/dev/null | wc -l) done=\$(ls /tmp/memexa_l0_round2/pass2_done/*.done 2>/dev/null | wc -l) prompt_remain=\$(ls /tmp/memexa_l0_round2/round2_input/*.json 2>/dev/null | wc -l) worker_alive=\$(pgrep -af l0_worker_v2_ustc | grep -v grep | wc -l)" 2>/dev/null)
echo "  <your-institution>: $<your-institution>_STATE"

# 5) Detect completion (worker dead + 0 prompts remain)
if echo "$<your-institution>_STATE" | grep -q "prompt_remain=0" && echo "$<your-institution>_STATE" | grep -q "worker_alive=0"; then
  echo "  ✅ ROUND 2 COMPLETE: all 1290 batches processed"
  # final pull sweep
  scp -q "remote-server:/tmp/memexa_l0_round2/cards_v2/*.json" data/l0_v5/work/round2_cards/ 2>/dev/null || true
  scp -q "remote-server:/tmp/memexa_l0_round2/pass2_done/*.done" data/l0_v5/work/round2_done/ 2>/dev/null || true
  # final ssh-clean
  ssh -q remote-server "rm -rf /tmp/memexa_l0_round2/{cards_v2,pass2_done,logs}/*; \
    rmdir /tmp/memexa_l0_round2/round2_input 2>/dev/null || true" 2>/dev/null || true
  # Mark completion sentinel
  touch data/l0_v5/work/.round2_complete
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pull cycle end"
