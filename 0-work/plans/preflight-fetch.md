# Preflight fetch — end-to-end validation

Run this **once** after legal clearance and before Phase C full fetch.

## What it validates

| Check | How |
|-------|-----|
| Loose annual report filter | `11_fetch_annual_reports_s3.py --annual-filter loose` |
| S3 folder structure | `entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf` |
| Burned EC2 replacement | Worker 01 simulates burn after 2 uploads; waiter terminates + relaunches |
| SNS email | Summary published via `GYPSY_SNS_TOPIC_ARN` |
| Idempotent skip | Re-run skips existing S3 objects (HEAD) |

## S3 layout (preflight + production)

```
s3://$GYPSY_S3_BUCKET/
  entities/{TICKER}/{TICKER}_Announcements.csv     # index (already uploaded)
  entities/{TICKER}/annual_reports/
    {YYYY}_{documentKey}.pdf                       # annual report PDFs
  logs/preflight/{run_id}/
    worker_{NN}.json                               # worker result
    worker_{NN}.log
    worker_{NN}_meta.json
  manifests/preflight/{run_id}/summary.json        # pass/fail summary
  scripts/                                         # worker scripts (synced at launch)
```

## PDF file naming

There is **no separate PDF renamer script**. Naming is **deterministic at upload time**:

- Helper: `00_asx_api.s3_annual_report_key()` / `annual_report_pdf_basename()`
- Pattern: `{YYYY}_{documentKey_with_slashes_as_underscores}.pdf`
- Example: `entities/CBA/annual_reports/2025_2924-02977830-2A1613327.pdf`

The existing `05_rename_announcements_csv.py` only renames **announcements index CSVs** (`announcements.csv` → `{TICKER}_Announcements.csv`). It does not touch PDFs.

## Run preflight

```bash
set -a && source 0-work/scripts/.env && set +a

# Fire-and-forget (email when done)
0-work/scripts/aws/run_preflight_fetch.sh --async
```

Workers:

| Worker | Ticker | Reports | Burn test |
|--------|--------|---------|-----------|
| 00 | CBA | 5 loose annual | none |
| 01 | QGL | 5 loose annual | `--simulate-burn-after 2` → rotate → finish |

## Verify results

```bash
# Summary JSON
aws s3 cp s3://$GYPSY_S3_BUCKET/manifests/preflight/{run_id}/summary.json -

# List uploaded PDFs
aws s3 ls s3://$GYPSY_S3_BUCKET/entities/CBA/annual_reports/
aws s3 ls s3://$GYPSY_S3_BUCKET/entities/QGL/annual_reports/
```

Pass criteria (`summary.json`): `"passed": true` — all workers complete, at least one PDF uploaded.

## Local dry-run (no EC2)

```bash
python3 0-work/scripts/11_fetch_annual_reports_s3.py \
  --ticker CBA \
  --bucket "$GYPSY_S3_BUCKET" \
  --max-reports 2 \
  --annual-filter loose \
  --announcements-source local \
  --no-cache
```

Parent: [`aws-distributed-fetch.md`](aws-distributed-fetch.md)
