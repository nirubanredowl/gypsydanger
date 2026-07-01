# Distributed fetch architecture — Stage 2

**Status:** draft  
**Scope:** Stage 2 full corpus PDF fetch (~1.26M documents, ~1,838 tickers)  
**Prerequisite:** Step 2 index complete locally (`*_Announcements.csv` per ticker)

---

## Context

This pipeline is **structured API fetch**, not HTML scraping:

| Step | Endpoint | Output |
|------|----------|--------|
| 1 | Directory CSV / API | `entities.csv` |
| 2 | `markets/announcements` (paginated JSON) | `{TICKER}_Announcements.csv` |
| 3 | CDN `file/{documentKey}` | `{documentKey}.pdf` |

Existing scripts already support ticker-level sharding via `--ticker` (repeatable). Idempotency is file-existence + min-bytes check.

### Current scale (local, 2026-07-01)

| Metric | Value |
|--------|-------|
| Entities | 1,839 |
| Indexed tickers | 1,838 |
| Announcement rows (documentKeys) | ~1,261,873 |
| PDFs downloaded | 0 |
| Avg docs/ticker | ~686 |

### Back-of-envelope

| Assumption | Estimate |
|------------|----------|
| Avg PDF size | 400–600 KB |
| Total storage | **500 GB – 1.2 TB** |
| Sequential @ 1 req/s | ~14.6 days |
| 10 workers @ 1 req/s each | ~1.5 days |
| 20 workers @ 1 req/s each | ~17 hours |

**Bottleneck:** network I/O + API/CDN rate limits, not CPU. Parallelism helps only if each worker has its own rate budget (typically per-IP).

---

## Design goals

1. **Ticker as the work unit** — one manifest (`*_Announcements.csv`) per job; no cross-ticker coupling.
2. **Object storage as source of truth** — S3 (or R2/GCS) holds index + PDFs; VMs are ephemeral.
3. **Idempotent everywhere** — re-run safe; skip objects that already exist with valid size.
4. **Observable** — per-worker logs + consolidated audit (`fetch_log.json` pattern).
5. **Minimal ops** — avoid Kubernetes unless already in use; prefer queue + stateless workers.

---

## Recommended architecture

**Pattern:** S3 manifest store + SQS work queue + N identical spot workers + optional global rate limiter.

```mermaid
flowchart TB
  subgraph prep [Prep — once]
    UP[Upload index to S3]
    ENQ[Enqueue ticker batches]
    UP --> ENQ
  end

  subgraph storage [S3 bucket]
    ENT[entities.csv]
    ANN[entities/TICKER/*_Announcements.csv]
    PDF[entities/TICKER/raw/*.pdf]
    LOG[logs/fetch_log/{worker_id}.jsonl]
    DONE[manifests/completed_tickers.txt]
  end

  subgraph queue [Orchestration]
    SQS[SQS queue<br/>ticker batches]
    DLQ[DLQ — failed tickers]
  end

  subgraph workers [Worker pool — 5–20 VMs]
    W1[Worker 1]
    W2[Worker 2]
    WN[Worker N]
  end

  ENQ --> SQS
  SQS --> W1 & W2 & WN
  W1 & W2 & WN --> PDF
  W1 & W2 & WN --> LOG
  W1 & W2 & WN --> DONE
  SQS -.-> DLQ
  W1 & W2 & WN -.->|read manifest| ANN
```

### S3 layout

```
s3://gypsy-danger-asx/
  entities.csv
  entities/{TICKER}/{TICKER}_Announcements.csv
  entities/{TICKER}/raw/{documentKey}.pdf
  logs/
    fetch_log/{worker_id}/{run_id}.jsonl   # append-only per worker
    index_log/...
  manifests/
    work_batches.json                      # optional: pre-computed shards
    completed_tickers.txt                  # one ticker per line
    failed_tickers.json                    # retry queue input
```

Mirror the local `data/` tree so existing scripts need minimal path changes.

---

## Work partitioning

### Batch unit: ticker (default)

- **Pros:** Already implemented; natural manifest; easy retry; balanced enough for most tickers.
- **Cons:** Heavy tickers (e.g. CBA, BHP) skew wall time.

### Optional: sub-ticker batches for whales

For tickers with >5k documents, split `*_Announcements.csv` into row-range chunks:

```json
{"ticker": "CBA", "offset": 0, "limit": 2000}
{"ticker": "CBA", "offset": 2000, "limit": 2000}
```

Defer until pilot fetch shows skew (>10× median ticker time).

### Shard sizing

| Workers | Tickers/worker | Docs/worker (avg) |
|---------|----------------|-------------------|
| 10 | ~184 | ~126k |
| 20 | ~92 | ~63k |
| 50 | ~37 | ~25k |

Start with **10–15 workers**; scale up if no 429/rate-limit errors after 1 hour soak.

---

## Worker behaviour

Each worker is a stateless process (Docker on EC2 spot, ECS Fargate task, or Railway one-off):

```
1. Poll SQS for message: { "tickers": ["ABC", "ABX", ...] }  (5–20 tickers)
2. For each ticker:
   a. GET announcements CSV from S3 (or sync prefix locally)
   b. For each documentKey:
      - HEAD s3://.../raw/{documentKey}.pdf
      - if exists and Content-Length >= 51200 → skip
      - else GET CDN URL → PUT S3 (streaming, no full local corpus)
      - append {status, ticker, document_key, bytes} to local jsonl
   c. Append ticker to completed_tickers (S3 conditional write or DynamoDB)
3. Upload worker jsonl log to S3
4. Delete SQS message (ack)
5. On unrecoverable ticker failure → send to DLQ
```

### Rate limiting

| Strategy | When |
|----------|------|
| **Per-worker fixed delay** (1 req/s default) | Start here; matches current `AsxClient` |
| **Per-worker adaptive** | Back off on 429/503; exponential retry |
| **Global token bucket** (Redis / DynamoDB) | Only if CDN blocks per-ASN regardless of IP count |

Do **not** assume 50 workers = 50× throughput until validated against Markit CDN.

### Concurrency within a worker

- **Default:** 1 concurrent download per worker (simplest, matches current code).
- **Optional:** 3–5 async downloads per worker *after* soak test shows headroom — still cap aggregate per-IP.

---

## Orchestration options (pick one)

### Option A — Minimal (fastest to ship)

**Best for:** proving throughput before cloud investment.

1. Split ticker list into N text files (`shards/shard_01.txt`, …).
2. Launch N cheap VMs (Hetzner, EC2 spot, DigitalOcean).
3. Each VM: clone repo, `pip install`, `aws s3 sync` index from bucket.
4. Run existing script per shard:
   ```bash
   while read ticker; do
     python 03_fetch_documents.py --ticker "$ticker"
     aws s3 sync data/entities/$ticker/raw s3://bucket/entities/$ticker/raw/
   done < shard_01.txt
   ```
5. Merge `fetch_log.json` locally after.

**Pros:** No new code; uses `--ticker` today.  
**Cons:** Manual shard balancing; sync-after-each-ticker adds latency; no DLQ.

### Option B — Recommended (SQS + spot fleet)

**Best for:** full Stage 2 run (~1.26M PDFs).

| Component | Service |
|-----------|---------|
| Storage | S3 Standard → Intelligent-Tiering after 30 days |
| Queue | SQS standard queue |
| Workers | EC2 spot (e.g. `t3.small` or `c7g.medium`) or ECS Fargate |
| IAM | Worker role: `s3:GetObject/PutObject/HeadObject`, `sqs:ReceiveMessage/DeleteMessage` |
| Secrets | None required (public API) |

Coordinator script (run once):

```python
# Pseudocode — enqueue all tickers in batches of 10
for batch in chunks(all_tickers, size=10):
    sqs.send_message({"tickers": batch})
```

**Pros:** Auto-retry via visibility timeout; scale workers elastically; spot ≈ 70% cheaper.  
**Cons:** Small amount of glue code (S3-aware fetch script).

### Option C — Single elastic VM

**Best for:** avoiding multi-VM ops if rate limit is global anyway.

- One `c7g.2xlarge` (8 vCPU) running 8 processes × `--ticker` shards.
- If throughput ≈ 1× single process → **rate limit is IP/global**; extra VMs won't help much.
- If throughput ≈ 8× → CDN allows per-connection parallelism; still cap at ~5–10 to avoid blocks.

**Verdict:** Run a **2-hour soak test** (1 VM vs 4 VMs) before committing to fleet size.

---

## Code changes (small, targeted)

Keep Stage 1 scripts; add a thin storage + queue layer:

| Change | Purpose |
|--------|---------|
| `StorageBackend` protocol (`local` \| `s3`) | `exists`, `get`, `put` for PDFs and CSVs |
| `04_enqueue_fetch_jobs.py` | Build SQS messages from `entities.csv` minus completed |
| `05_fetch_worker.py` | Pull queue → fetch ticker batch → write S3 → log |
| `06_merge_fetch_logs.py` | Consolidate worker jsonl → `fetch_log.json` |
| Env: `GYPSY_S3_BUCKET`, `GYPSY_SQS_QUEUE` | Config |

Existing `03_fetch_documents.py` logic (`fetch_ticker`) moves into shared module; worker calls it with S3 backend.

---

## Execution phases

### Phase 0 — Upload index (done locally, not in S3 yet)

```bash
aws s3 sync data/entities/ s3://gypsy-danger-asx/entities/ \
  --exclude "*/raw/*" --include "*_Announcements.csv"
aws s3 cp data/entities.csv s3://gypsy-danger-asx/entities.csv
```

~1,838 CSVs, negligible size vs PDFs.

### Phase 1 — Soak test (required)

| Test | Workers | Duration | Measure |
|------|---------|----------|---------|
| T1 | 1 VM, 1 req/s | 2 h | docs/hr, error rate |
| T2 | 4 VMs, 1 req/s each | 2 h | linear scaling? |
| T3 | 1 VM, 5 concurrent | 1 h | per-IP concurrency cap |

Pick worker count from T1–T3. Target: **<1% failure rate**, no sustained 429s.

### Phase 2 — Full fetch

1. Enqueue all tickers (skip those in `completed_tickers.txt`).
2. Run worker fleet until queue empty.
3. Merge logs; run retry pass on `failed` documentKeys.
4. Validate: row count in announcements ≈ object count in S3 (per ticker sample).

### Phase 3 — Cutover

- Local dev reads from S3 (or sync subset for parse stage).
- `data/` on laptop becomes cache, not source of truth.

---

## Cost sketch (AWS ap-southeast-2, rough)

| Item | Estimate |
|------|----------|
| S3 1 TB storage | ~USD 23/mo (Standard) |
| S3 PUT 1.26M | ~USD 6 (one-time) |
| S3 GET (HEAD checks) | ~USD 1 |
| EC2 10× spot t3.small × 2 days | ~USD 5–15 |
| SQS | < USD 1 |
| **Total one-time fetch** | **~USD 15–35** + storage |

Cheaper than one developer-day of waiting on sequential local fetch.

---

## What not to do

| Anti-pattern | Why |
|--------------|-----|
| Kubernetes for this job | Ops overhead >> benefit for a batch job |
| Scrape HTML / bypass API | Fragile; API + CDN already structured |
| Single giant `fetch_log.json` writes from all workers | Write contention; use per-worker jsonl + merge |
| Download all to one VM disk then upload | 1 TB disk + double transfer; stream to S3 |
| Unbounded parallelism | Likely triggers CDN blocks; no gain |

---

## Open decisions

1. **Cloud provider** — AWS S3+SQS vs Cloudflare R2 (+ custom queue) vs GCS. AWS is the default recommendation given tooling maturity.
2. **Worker host** — EC2 spot fleet vs ECS Fargate vs Railway (Railway better for long-running single service, less ideal for 10+ parallel batch workers).
3. **Global rate limit** — unknown until soak test; may cap effective worker count at ~5–10 regardless of fleet size.
4. **Whale ticker splitting** — defer until fetch metrics collected.
5. **When to upload index** — before soak test (recommended) so workers never depend on local `data/`.

---

## Recommendation summary

| Question | Answer |
|----------|--------|
| Batch unit | **Ticker** (optionally row-range for whales later) |
| Distribution | **Multiple small spot VMs** (5–20), not one big elastic box |
| Storage | **S3 bucket** mirroring `data/entities/` layout |
| Orchestration | **Option B** (SQS + workers) for full run; **Option A** for soak test |
| Next concrete step | Upload index to S3 → 2 h soak test (1 vs 4 workers) → enqueue full fetch |
