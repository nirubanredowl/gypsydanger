# Script log

Audit trail for scripts run from `0-work/scripts/`. The agent appends an entry after every script execution (including git push).

## Entry format

```markdown
## YYYY-MM-DD HH:MM — script-name
- **Command:** `./0-work/scripts/example.sh arg1`
- **Exit:** 0
- **Result:** brief outcome
```

---

<!-- Entries below this line -->

## 2026-06-30 — 00_pick_pilot_tickers.py
- **Command:** `python3 0-work/scripts/00_pick_pilot_tickers.py --count 10 --seed 42`
- **Exit:** 0
- **Result:** Wrote 10 tickers to data/pilot_tickers.txt — ACS, AUG, AX8, BLX, CSL, DBF, EDU, PTL, SOL, SPL

## 2026-06-30 — 01_normalise_entities.py
- **Command:** `python3 0-work/scripts/01_normalise_entities.py`
- **Exit:** 0
- **Result:** Wrote 1838 rows to data/entities.csv (full ASX list; entity_xid column empty pending Step 2)

## 2026-06-30 — entity_xid resolution (discovery + code change)
- **Command:** (investigation — browser network trace + API probes on BLX/ACS)
- **Exit:** n/a
- **Result:** `xidEntity` is **not** in CDN PDF URLs or `companies/{TICKER}/announcements` (`data.xid` is security id, wrong for pagination). ASX page uses `GET .../search/predictive?searchText={TICKER}` → exact `symbol` match → `xidEntity`. Updated `00_asx_api.py` `resolve_entity_xid()` to use predictive search first (1 req/ticker, ~1s). Verified BLX→234722142 (404 ann.), ACS→204158037 (440 ann.), 20/20 spot-check. Added per-ticker run log at `data/logs/02_index_announcements.log` (flush after each line; `entities.csv` saved after each success).

## 2026-06-30 — 02_index_announcements.py (pilot, partial)
- **Command:** `python3 0-work/scripts/02_index_announcements.py --pilot-only`
- **Exit:** 1
- **Result:** 8/10 pilot tickers indexed (AUG, AX8, CSL, DBF, EDU, PTL, SOL, SPL). ACS and BLX failed under old market-scan bootstrap (pre–predictive-search fix). Re-run pilot or full list with updated script.

## 2026-06-30 — 02_index_announcements.py --ticker ACS
- **Command:** `python3 0-work/scripts/02_index_announcements.py --ticker ACS --no-cache`
- **Exit:** 0
- **Result:** Resolved entity_xid=204158037; indexed 440 announcements. Run log at data/logs/02_index_announcements.log

## 2026-06-30 — 02_index_announcements.py (full run, started)
- **Command:** `python3 0-work/scripts/02_index_announcements.py`
- **Exit:** 143
- **Result:** Interrupted when agent terminal closed (~6 min, 25/1838 tickers OK, 0 errors; last line `START A2M`). Detail: data/logs/02_index_announcements.log

## 2026-06-30 — 02_index_announcements.py (full run, restarted)
- **Command:** `screen -dmS gypsy-index ./0-work/scripts/run_02_index_full.sh`
- **Exit:** 1
- **Result:** Full ASX list (~1838 tickers). Screen session `gypsy-index`. Monitor: `tail -f data/logs/02_index_announcements.log`

## 2026-06-30 — 02_index_announcements.py --ticker A2M
- **Command:** `python3 0-work/scripts/02_index_announcements.py --ticker A2M`
- **Exit:** 0
- **Result:** entity_xid=204143076; 1052 announcements indexed (completed stuck ticker from prior run)

## 2026-06-30 — 02_index_announcements.py (full run, restart #2)
- **Command:** `nohup ./0-work/scripts/run_02_index_full.sh`
- **Exit:** 1
- **Result:** Completed 1831/1838 tickers; 1,255,355 announcements indexed. 7 failures: LAM, LKE, LKO, LKY, LLM, SRV (transient API errors), TRUNB (no Markit entity — same issuer as TRU). Detail: `data/logs/02_index_announcements.log` (`RUN DONE` 2026-07-01T03:14:37Z)

## 2026-07-01 — 02_index_announcements.py (retry failures)
- **Command:** `python3 0-work/scripts/02_index_announcements.py --ticker LAM --ticker LKE --ticker LKO --ticker LKY --ticker LLM --ticker SRV --ticker TRUNB`
- **Exit:** 0
- **Result:** All 7 retried OK (4680 announcements). TRUNB via `data/overrides.json` → same entity as TRU (234156224). Full list now 1838/1838 with entity_xid.

## 2026-07-01 — 04_verify_entity_folders.py
- **Command:** `python3 0-work/scripts/04_verify_entity_folders.py --require-announcements`
- **Exit:** 0
- **Result:** 1838/1838 tickers in `entities.csv` have `data/entities/{TICKER}/` folders with announcements CSV (pre-rename: `announcements.csv`).

## 2026-07-01 — 05_rename_announcements_csv.py
- **Command:** `python3 0-work/scripts/05_rename_announcements_csv.py`
- **Exit:** 0
- **Result:** Renamed 1838 files `announcements.csv` → `{TICKER}_Announcements.csv`. Updated `00_asx_api.announcements_csv_path()` for new naming.

## 2026-07-01 — 04_verify_entity_folders.py (post-rename)
- **Command:** `python3 0-work/scripts/04_verify_entity_folders.py --require-announcements`
- **Exit:** 0
- **Result:** 1838/1838 folders OK with renamed announcement CSVs.

## 2026-07-01 — aws/bootstrap_baseline.sh
- **Command:** `0-work/scripts/aws/bootstrap_baseline.sh`
- **Exit:** 254 (AccessDenied)
- **Result:** Blocked at S3 CreateBucket — `niruban_cursor` needs `0-work/infra/iam-baseline-policy.json` attached. Script and naming doc ready to re-run.

## 2026-07-01 — aws/bootstrap_baseline.sh (retry)
- **Command:** `0-work/scripts/aws/bootstrap_baseline.sh`
- **Exit:** 0
- **Result:** S3 `gypsy-danger-asx-691811257790`, IAM role/profile, SG `sg-06b188c6063d53a96`, EC2 `i-0812f82dd21298e96` (`gypsy-danger-soak-01`).

## 2026-07-01 — S3 index upload
- **Command:** `aws s3 cp data/entities.csv s3://gypsy-danger-asx-691811257790/` + `aws s3 sync data/entities/ ... --include '*_Announcements.csv'`
- **Exit:** 0
- **Result:** 1838 ticker announcement CSVs + entities.csv uploaded.

## 2026-07-01 — aws/run_soak_on_ec2.sh (B0 micro-soak)
- **Command:** `0-work/scripts/aws/run_soak_on_ec2.sh 50 1.0`
- **Exit:** 0
- **Result:** EC2 `i-0812f82dd21298e96`, CBA ticker, 50 CDN GETs, 0% errors, ~788 docs/hr effective throughput.

## 2026-07-01 — aws/run_soak_on_ec2.sh (B0 full soak, attempt 1)
- **Command:** `0-work/scripts/aws/run_soak_on_ec2.sh 500 1.0`
- **Exit:** 254 (pager) / SSM TimedOut
- **Result:** Poll loop exited after 5 min; final `aws ssm get-command-invocation` failed (no `less` pager). SSM default 3600s timeout killed soak mid-run (exit 137) before 500 requests completed.

## 2026-07-01 — aws/run_soak_on_ec2.sh (B0 full soak, attempt 2)
- **Command:** `0-work/scripts/aws/run_soak_on_ec2.sh 500 1.0`
- **Exit:** 0
- **Result:** SSM `786b89a4-aa01-42e4-a967-7dfd2a69b011`. CBA, 500/500 CDN GETs, 0% errors, ~1271 docs/hr, ~502 MB, 1416s elapsed.

## 2026-07-01 — scaling ladder + SNS notifications (design/scripts)
- **Command:** (doc + script commit)
- **Exit:** —
- **Result:** `scaling-ladder-execution.md`, `08_build_ladder_pool.py`, `run_ladder_rung.sh --async`, `bootstrap_notifications.sh`, soak `--async` + SNS email on completion.

## 2026-07-01 — bootstrap_notifications.sh + test email
- **Command:** `bootstrap_notifications.sh` (after adding `GYPSY_NOTIFY_EMAIL=niruban@redowl.ai`)
- **Exit:** 254
- **Result:** Blocked — `niruban_cursor` needs updated `iam-baseline-policy.json` (SNS sid) attached. Re-run bootstrap + `notify_sns.sh` after attach.

## 2026-07-01 — bootstrap_notifications.sh + test email (retry)
- **Command:** `bootstrap_notifications.sh` + `notify_sns.sh "Gypsy Danger test" ...`
- **Exit:** 0
- **Result:** SNS topic `gypsy-danger-notify` created; `niruban@redowl.ai` subscribed (pending confirmation); test message published.
