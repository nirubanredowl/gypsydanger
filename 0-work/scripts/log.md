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

## 2026-07-01 — notify_sns.sh test (post-confirm)
- **Command:** `notify_sns.sh "Gypsy Danger test" ...`
- **Exit:** 0
- **Result:** Re-sent after subscription confirmed (first send was before confirm, so SNS dropped it).

## 2026-07-02 — run_ladder_rung.sh --async 2
- **Command:** `run_ladder_rung.sh --async 2` (3 attempts — fixed `--ticker` required with `--keys-file`, fixed shard path)
- **Exit:** 0
- **Result:** Rung 2 running: workers `i-019b2dd79d2a3cd2b` … `i-09d6d788c8ead4f48`, waiter SSM `59041f5a-2778-4798-a5bf-8162fd15960f`. ~1.1 h; SNS email on completion.

## 2026-07-02 — run_ladder_rung.sh --async 4
- **Command:** `run_ladder_rung.sh --async 4`
- **Exit:** 0
- **Result:** 20 workers launched; run `20260702T014116Z-98609`; waiter SSM `0d82c840-4460-4be8-8230-dda705d55d60`; ~15–25 min ETA; single SNS email on completion.

## 2026-07-02 — run_ladder_rung.sh --async 4 (complete)
- **Result:** 1,999/2,000 success; aggregate **17,870 docs/hr**; FAIL strict threshold but 4.8× rung 2. **Fleet: 20 workers** for Phase C.

## 2026-07-02 — fetch progress notifications
- **Command:** `progress_email.sh`, `install_progress_watcher.sh`
- **Exit:** 0
- **Result:** On-demand + event-driven SNS progress emails; 5-min cron on soak-01.

## 2026-07-05 — 10_probe_annual_reports.py
- **Command:** `python3 10_probe_annual_reports.py --show-excluded` and `--all-indexed --json`
- **Exit:** 0
- **Result:** `announcementTypes` contains filterable `Annual Report` tag. Sample tickers (CBA,BHP,WOW,QGL,TLS): 78 strict / 112 loose annual reports out of 12,642 rows. Full index: **22,128 strict** / 22,573 loose out of 1,260,035 rows. Fetcher updated with `--annual-reports-only`; schema doc at `0-work/docs/announcements-schema.md`.

## 2026-07-06 — IP burn rotation + loose annual fetch default
- **Command:** (code change — `CdnBurnTracker`, `launch_ladder_worker.sh`, waiter rotation)
- **Exit:** —
- **Result:** Workers exit code 2 when CDN IP burned (429/503 thresholds). `ladder_wait_and_notify.sh` terminates burned EC2 and relaunches with new public IP (max 3 rotations/slot). `03_fetch_documents.py` default `--annual-filter loose` when `--annual-reports-only` is set.

## 2026-07-06 — preflight fetch pipeline
- **Command:** (code) `11_fetch_annual_reports_s3.py`, `aws/run_preflight_fetch.sh`
- **Exit:** —
- **Result:** Preflight orchestrator: 2 EC2 workers (CBA+QGL, 5 loose annual reports each), S3 path `entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf`, simulated burn+rotate on worker 01, SNS summary. Run with `run_preflight_fetch.sh --async`. No PDF renamer script — naming via `s3_annual_report_key()`.

## 2026-07-06 — preflight autostart (blocked at AWS auth)
- **Command:** `tmux: run_preflight_autostart.sh`
- **Exit:** pending
- **Result:** Cloud agent missing AWS keys. Autostart waiting on `aws login --remote` or credentials in `.env`. On auth success → `run_preflight_fetch.sh --async` (loose annual reports). Log: `0-work/scripts/preflight-run.log`.

## 2026-07-06 02:53 — preflight run 20260706T025349Z-preflight (FAIL)
- **Command:** `run_preflight_fetch.sh --async`
- **Exit:** 0 (orchestrator); waiter FAIL
- **Result:** Worker 00 (CBA) uploaded 5 PDFs. Worker 01 (QGL) simulated burn but rotation failed — waiter ran on soak-01 via SSM; soak IAM lacked `ec2:RunInstances`/`TerminateInstances`. Summary: `passed: false`, `worker_results: 1`.

## 2026-07-06 07:50 — Phase C failed PDF retry
- **Command:** `13_retry_failed_annual_reports.py --from-logs-dir /tmp/phase-c-logs`
- **Exit:** 1 (3 permanent CDN misses)
- **Result:** 44/47 recovered and uploaded. Root cause: 39 valid PDFs rejected by 50KB `MIN_PDF_BYTES` threshold; 4 transient CDN errors (IncompleteRead/timeout); 3 permanently unavailable (CDN returns `[]`). Fixed validation → `is_valid_pdf()` (%PDF + ≥1KB). Permanent fails: SIX, CCR, REZ. Manifest: `manifests/fetch/20260706T033644Z-fetch/retry_summary.json`.

## 2026-07-06 — CFO change fetch scripts
- **Command:** (code) `16_fetch_cfo_changes_s3.py`, `17_build_cfo_change_shards.py`, `18_fetch_cfo_changes_shard.py`, `aws/run_cfo_changes_fetch.sh`
- **Exit:** —
- **Result:** Headline-filter fetch mirroring Phase C. S3 naming via `cfo_change_date()` → `{YYYY-MM-DD}_{documentKey}.pdf` under `entities/{TICKER}/cfo_changes/`. Shard build: 899 tickers, 2,133 tier-A docs / 10 workers. Test: ticker 360 uploaded 2 PDFs OK.

## 2026-07-06 — CFO change fetch launch
- **Command:** `aws/run_cfo_changes_fetch.sh --async`
- **Exit:** 0
- **Result:** Run `20260706T223406Z-cfo-fetch`; 10 EC2 workers; 2,133 tier-A targets; waiter tmux `cfo-fetch-waiter-20260706T223406Z-cfo-fetch`; progress `manifests/cfo_fetch_progress.json`. **Early stop:** waiter counted interim progress JSON as complete (~821/2133 uploaded); fixed `worker_done` + separate `_progress.json` key.

## 2026-07-06 — CFO change fetch resume
- **Command:** `aws/run_cfo_changes_fetch.sh --async` (after waiter fix)
- **Exit:** pending
- **Result:** (updated after launch)
