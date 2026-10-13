---
title: Migrate Gypsy Danger corpus from AWS S3 to Neon Object Storage
type: plan
status: active
depends_on:
  - neon.ts bucket asx-artifacts
  - 0-work/docs/extraction-pipeline.md
---

# S3 → Neon corpus migration

**Goal:** Move the full ASX corpus (~22k+ annual PDFs, parsed artefacts, manifests, shards) to **Neon Object Storage** on branch **production** (`br-small-lake-b4r1qjs5`) **without changing object keys or pipeline semantics**.

---

## Principles

1. **Keys are the contract** — `entities/`, `parsed/`, `manifests/` paths stay identical; only the bucket/host changes.
2. **Same Python entrypoints** — scripts accept `--bucket` / `GYPSY_STORAGE_BUCKET`; I/O goes through `00_object_store.py`.
3. **Idempotent migration** — re-run safe; skip when destination object size matches source.
4. **Document in one place** — [`extraction-pipeline.md`](../docs/extraction-pipeline.md) is updated on every behavioural change.

---

## Phase 0 — Neon bucket & credentials

- [ ] `neon deploy` on production (Auth/Functions optional for storage-only)
- [ ] `neon env pull` → `AWS_ENDPOINT_URL_S3`, keys, `DATABASE_URL`
- [ ] Set `GYPSY_STORAGE_BUCKET=asx-artifacts`, `GYPSY_STORAGE_BACKEND=neon`

---

## Migration tracks

| Track | When to use | Doc |
|-------|-------------|-----|
| **A — Serial copy** | Pilot prefix, emergency, single VM | Phase 1 below (`27_migrate_object_store.py`) |
| **B — Manifest + queue workers** | Full corpus, resumable, observable | [`neon-corpus-migration-queue-workers.md`](neon-corpus-migration-queue-workers.md) |

---

## Phase 1 — Bulk copy (Track A)

Run from a machine with **both** AWS read access to the legacy bucket and Neon write credentials:

```bash
python3 0-work/scripts/27_migrate_object_store.py \
  --source-backend aws \
  --dest-backend neon \
  --source-bucket "$GYPSY_S3_BUCKET" \
  --dest-bucket asx-artifacts \
  --manifest-out /tmp/migrate-manifest.json
```

Validate spot checks:

- Random `entities/{TICKER}/annual_reports/*.pdf` size match
- Sample `parsed/…/liteparse/manifest.json` + `pages/manifest.json`
- `manifests/parse_enrich_progress.json` present

---

## Phase 2 — Python pipeline cutover

| Component | Status |
|-----------|--------|
| `22_liteparse_document.py` | Uses `00_object_store` |
| `24_split_pdf_pages.py` | Uses `00_object_store` |
| `11_fetch_annual_reports_s3.py` | Uses `00_object_store` |
| `23_liteparse_shard.py`, `25_split_shard.py` | Pass `--bucket`; inherit store via 22/24 |
| Progress watchers (`09_*`, `26_*`) | **TODO:** replace `aws s3 cp` with store |
| EC2 user-data / `aws/run_*.sh` | **TODO:** export Neon env + `GYPSY_STORAGE_*`; stop syncing to AWS bucket |

---

## Phase 3 — Worker hosting (optional)

Neon Functions are **not required** for batch PDF work; EC2/VM workers can keep running with Neon S3 credentials. Long-term options:

- Workers on EC2 with Neon env only (simplest)
- Neon Functions for small HTTP triggers / progress APIs
- Postgres on Neon for catalog layer (`catalog/*.csv` → tables)

---

## Phase 4 — Decommission AWS bucket

Only after:

- [ ] 3B (or current stage) completes against Neon
- [ ] Progress manifests written to Neon `manifests/`
- [ ] Backup / lifecycle policy confirmed on Neon branch

Archive or delete `s3://gypsy-danger-asx-…` per org policy.

---

## Rollback

Keep AWS bucket read-only until Phase 4. Re-point `GYPSY_STORAGE_BACKEND=aws` and legacy bucket name to revert workers without re-copying keys.
