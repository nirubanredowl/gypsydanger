# AWS naming and tagging — Gypsy Danger

All Stage 2 fetch resources use the **`gypsy-danger`** prefix so they are easy to find in the console, cost reports, and `Resource Groups`.

## Naming convention

| Pattern | Example | Used for |
|---------|---------|----------|
| `gypsy-danger-asx-{account_id}` | `gypsy-danger-asx-691811257790` | S3 bucket (globally unique) |
| `gypsy-danger-fetch-{resource}` | `gypsy-danger-fetch-sg` | Shared fetch infra |
| `gypsy-danger-soak-{nn}` | `gypsy-danger-soak-01` | Soak-test EC2 (Name tag) |
| `gypsy-danger-fetch-ec2-role` | — | IAM role for worker/soak instances |

**Region:** `ap-southeast-2` (Sydney) for all Stage 2 resources.

## Required tags (every resource)

| Tag | Value | Purpose |
|-----|-------|---------|
| `Project` | `gypsy-danger` | Cost / resource group filter |
| `Application` | `gypsy-danger-asx-fetch` | Sub-application |
| `Environment` | `lab` | Your lab account |
| `Stage` | `soak` or `fetch` | Soak test vs full fetch |
| `ManagedBy` | `gypsy-danger-bootstrap` | Created by repo scripts |
| `Owner` | `niruban` | Human owner |

Optional on EC2: `Name` = `gypsy-danger-soak-01`.

## Baseline stack (soak phase)

Minimum resources before B0 CDN soak on AWS:

| Resource | Name | Purpose |
|----------|------|---------|
| **S3 bucket** | `gypsy-danger-asx-691811257790` | Corpus + index manifests + soak logs |
| **IAM role** | `gypsy-danger-fetch-ec2-role` | Instance profile — S3 access, no keys on VM |
| **Security group** | `gypsy-danger-fetch-sg` | Egress HTTPS only; no inbound |
| **EC2** | `gypsy-danger-soak-01` | One VM to run `07_cdn_soak_test.py` from AWS IP |

Not created until after soak ladder: SQS, ASG, spot fleet.

## S3 prefix layout

```
s3://gypsy-danger-asx-691811257790/
  entities.csv
  entities/{TICKER}/{TICKER}_Announcements.csv
  entities/{TICKER}/raw/{documentKey}.pdf      # empty until fetch
  logs/soak/{instance_id}/{timestamp}.json     # soak output
  logs/fetch/...                               # later
  manifests/...
```

## Resource group (optional)

Console → Resource Groups → Create with tag filter:

```
Project = gypsy-danger
```

Shows all project resources in one view.

## Bootstrap

Script: [`0-work/scripts/aws/bootstrap_baseline.sh`](../scripts/aws/bootstrap_baseline.sh)

Requires IAM permissions in [`0-work/infra/iam-baseline-policy.json`](../infra/iam-baseline-policy.json) attached to `niruban_cursor` (or run with an admin profile).
