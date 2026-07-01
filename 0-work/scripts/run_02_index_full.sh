#!/usr/bin/env bash
set -u
cd /Users/niruban/RO-Projects/prj-gypsydanger
python3 0-work/scripts/02_index_announcements.py 2>&1 | tee data/logs/02_index_announcements.stdout.log
EXIT=${PIPESTATUS[0]}
LOGMD="0-work/scripts/log.md"
if grep -q "RUN DONE" data/logs/02_index_announcements.log 2>/dev/null; then
  SUM=$(grep "RUN DONE" data/logs/02_index_announcements.log | tail -1)
  BRIEF="Completed. ${SUM}. Detail: data/logs/02_index_announcements.log"
elif [ "$EXIT" -ne 0 ]; then
  ERR=$(tail -5 data/logs/02_index_announcements.stdout.log 2>/dev/null | tr '\n' ' ')
  BRIEF="Failed (exit ${EXIT}). ${ERR} Detail: data/logs/02_index_announcements.log"
else
  BRIEF="Finished exit ${EXIT}. Detail: data/logs/02_index_announcements.log"
fi
python3 - "$EXIT" "$BRIEF" << 'PY'
import sys
from pathlib import Path
exit_code, brief = int(sys.argv[1]), sys.argv[2]
p = Path("0-work/scripts/log.md")
text = p.read_text()
text = text.replace("- **Exit:** (running)", f"- **Exit:** {exit_code}", 1)
marker = "- **Result:** Full ASX list (~1838 tickers). Monitor:"
if marker in text:
    text = text.replace(
        marker + " `tail -f data/logs/02_index_announcements.log`",
        f"- **Result:** {brief}",
        1,
    )
p.write_text(text)
PY
exit "$EXIT"
