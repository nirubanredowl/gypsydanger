# Fetch progress notifications

Email progress updates during Phase C full fetch via **AWS SNS** → `niruban@redowl.ai`.

## On-demand progress (any time)

**Instant email** (from your laptop or this VM):

```bash
set -a && source 0-work/scripts/.env && set +a
0-work/scripts/aws/progress_email.sh
# or
0-work/scripts/aws/request_progress.sh --now
```

**Request via S3 trigger** (no shell — works from AWS Console mobile app):

```bash
aws s3 cp /dev/null s3://$GYPSY_S3_BUCKET/manifests/request_progress.trigger
```

The watcher on `gypsy-danger-soak-01` picks this up within **~5 minutes** and emails you.

> **Reply-to email:** true “email this address for status” needs SES inbound + Lambda (not set up). S3 trigger or `--now` script is the lightweight equivalent.

---

## Automatic event emails

A cron job on **soak-01** runs `progress_watcher.sh` every **5 minutes** and sends SNS when:

| Event | Trigger | Email subject (example) |
|-------|---------|-------------------------|
| **Fetch started** | First PDFs appear (`fetch_progress.json` or counts) | `Gypsy Danger fetch started` |
| **Milestone** | Every **10%** (10, 20, … 90) | `Gypsy Danger fetch — 30% complete` |
| **Daily digest** | Every **24 h** while still running | `Gypsy Danger fetch — daily digest` |
| **Stall alert** | No new PDFs for **45 min** (max once/hour) | `Gypsy Danger fetch — stalled?` |
| **Error spike** | **+100** new failures since last check | `Gypsy Danger fetch — errors now N` |
| **Complete** | 100% or status `complete` | `Gypsy Danger fetch complete` |
| **On-demand** | S3 `request_progress.trigger` file | `Gypsy Danger progress — on-demand` |

Duplicate milestones/alerts are suppressed via `s3://$BUCKET/manifests/progress_notify_state.json`.

---

## Progress data source

Workers / coordinator write:

`s3://$GYPSY_S3_BUCKET/manifests/fetch_progress.json`

```json
{
  "status": "running",
  "started_at": "2026-07-02T12:00:00Z",
  "pdfs_uploaded": 126000,
  "pdfs_total": 1260000,
  "errors": 42,
  "docs_hr": 17870,
  "workers": 20,
  "run_id": "fetch-20260702"
}
```

Until Phase C is deployed, on-demand emails report **index-only** state (0 PDFs, status `not_started`).

---

## Install watcher (once, before fetch)

```bash
set -a && source 0-work/scripts/.env && set +a
0-work/scripts/aws/install_progress_watcher.sh
```

This syncs scripts to S3, installs **cronie**, and adds a **5-minute root crontab** on soak-01 (logs to `/var/log/gypsy-progress-watcher.log`).

---

## Manual check (no email)

```bash
python3 0-work/scripts/09_fetch_progress.py
```
