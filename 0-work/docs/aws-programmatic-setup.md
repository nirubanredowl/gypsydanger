# AWS programmatic setup — replicate for another project

How to stand up AWS resources from code/scripts (no Console clicking), using patterns proven on **Gypsy Danger**. Adapt names and policies for your project; keep the structure.

**Gypsy Danger reference:** [`aws-setup.md`](aws-setup.md), [`aws-naming.md`](aws-naming.md), scripts in [`0-work/scripts/aws/`](../scripts/aws/), IAM in [`0-work/infra/`](../infra/).

---

## Philosophy

| Principle | Why |
|-----------|-----|
| **Bash + AWS CLI in repo** | Repeatable, diffable, agent-runnable; no manual Console steps |
| **Idempotent bootstrap** | `head-bucket` / `get-role` before create; safe to re-run |
| **Human IAM user + EC2 instance role** | Keys in `.env` for bootstrap only; workers use roles, no keys on disk |
| **Tags on everything** | Cost allocation, Resource Groups, cleanup by `Project=` |
| **Side effects in `scripts/`** | Log every run; never commit secrets |
| **SSM for remote ops** | Run long jobs on EC2 without SSH; async + email when done |

Infrastructure-as-code (CDK/Terraform) is fine for production; **bootstrap scripts** are ideal for lab projects and agent-driven setup.

---

## Directory layout (copy this shape)

```
your-project/
  0-work/
    docs/
      aws-programmatic-setup.md    ← this file
      aws-naming.md                ← your naming convention
      aws-setup.md                 ← project-specific quick start
    infra/
      iam-baseline-policy.json     ← permissions for bootstrap user
    scripts/
      .env                         ← gitignored credentials
      .env.example                 ← template committed
      log.md                       ← log every script run
      aws/
        bootstrap_baseline.sh      ← S3 + IAM + SG + optional EC2
        bootstrap_notifications.sh   ← SNS email topic
        notify_sns.sh                ← publish helper
        run_job_on_ec2.sh            ← SSM remote command pattern
      your_app_scripts.py
```

---

## Step 1 — Pick naming constants

Before writing scripts, fill this table (Gypsy Danger example in parentheses):

| Variable | Your project | Example |
|----------|--------------|---------|
| `PROJECT` | short slug | `gypsy-danger` |
| `APP` | app slug | `gypsy-danger-asx-fetch` |
| `ENV` | lab / prod | `lab` |
| `OWNER` | email local-part or name | `niruban` |
| `REGION` | AWS region | `ap-southeast-2` |
| S3 bucket | `{project}-data-{account_id}` | `gypsy-danger-asx-691811257790` |
| IAM role | `{project}-ec2-role` | `gypsy-danger-fetch-ec2-role` |
| Security group | `{project}-sg` | `gypsy-danger-fetch-sg` |

Document in `0-work/docs/aws-naming.md`. Use the same tags on every resource:

```
Project, Application, Environment, Stage, ManagedBy, Owner
```

---

## Step 2 — Credentials (headless / agent)

**File:** `0-work/scripts/.env` (gitignored)

```bash
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...          # exactly 40 characters
AWS_DEFAULT_REGION=ap-southeast-2

# Filled by bootstrap:
# MYPROJECT_S3_BUCKET=myproject-data-691811257790
# MYPROJECT_NOTIFY_EMAIL=you@example.com
# MYPROJECT_SNS_TOPIC_ARN=arn:aws:sns:...
```

**Rules:**

- One variable per line; file must end with a newline
- Never commit `.env`; commit `.env.example` with placeholders only
- Verify: `set -a && source 0-work/scripts/.env && set +a && aws sts get-caller-identity`

Create an IAM user (e.g. `yourname_cursor`) with programmatic access — not root.

---

## Step 3 — IAM policy for the bootstrap user

**File:** `0-work/infra/iam-baseline-policy.json`

Grant the minimum to run bootstrap scripts:

| Sid | Typical actions | Resource |
|-----|-----------------|----------|
| S3 | CreateBucket, PutObject, GetObject, ListBucket, encryption, tagging | Your bucket ARN |
| EC2 | RunInstances, Describe*, CreateSecurityGroup, CreateTags, TerminateInstances | `*` (scoped by tags in practice) |
| IAM | CreateRole, PutRolePolicy, CreateInstanceProfile, PassRole | Your role/profile ARNs |
| SSM | SendCommand, GetCommandInvocation, DescribeInstanceInformation | `*` |
| SNS | CreateTopic, Subscribe, Publish | Your topic ARN |

Attach in Console: **IAM → Users → Add permissions → Create inline policy → JSON paste**.

Re-attach when you add new script capabilities (e.g. SNS was added later).

**EC2 instance role** (separate, created by bootstrap): trust `ec2.amazonaws.com`; inline policy for S3 + optional SNS publish — no access keys on instances.

---

## Step 4 — Bootstrap script anatomy

Pattern from `bootstrap_baseline.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# 1. Load .env
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
[[ -f "$ROOT/0-work/scripts/.env" ]] && source "$ROOT/0-work/scripts/.env"

export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-2}"
export AWS_PAGER=""   # headless — no `less`

# 2. Constants + account-derived names
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${MYPROJECT_S3_BUCKET:-myproject-data-${ACCOUNT}}"

# 3. Idempotent create — check exists, else create
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "Bucket exists"
else
  aws s3api create-bucket --bucket "$BUCKET" \
    --region "$AWS_DEFAULT_REGION" \
    --create-bucket-configuration "LocationConstraint=$AWS_DEFAULT_REGION"
fi

# 4. Harden + tag (always apply)
aws s3api put-public-access-block --bucket "$BUCKET" ...
aws s3api put-bucket-encryption --bucket "$BUCKET" ...

# 5. IAM role + instance profile + inline policy (JSON heredoc)

# 6. Security group — egress 443 only, no inbound

# 7. Optional EC2 — user-data installs deps, writes /etc/profile.d/myproject.sh

# 8. Append bucket name to .env (ensure trailing newline first!)
```

**Idempotency patterns:**

```bash
aws iam get-role --role-name "$ROLE" >/dev/null 2>&1 || aws iam create-role ...
aws ec2 describe-security-groups --filters "Name=group-name,Values=$SG" ... || aws ec2 create-security-group ...
```

**Region note:** S3 `create-bucket` outside `us-east-1` requires `LocationConstraint`.

---

## Step 5 — Run bootstrap

```bash
set -a && source 0-work/scripts/.env && set +a
chmod +x 0-work/scripts/aws/*.sh
0-work/scripts/aws/bootstrap_baseline.sh
```

Log the run in `0-work/scripts/log.md`:

```markdown
## YYYY-MM-DD — aws/bootstrap_baseline.sh
- **Command:** `0-work/scripts/aws/bootstrap_baseline.sh`
- **Exit:** 0
- **Result:** S3, IAM, SG, EC2 created (ids…)
```

---

## Step 6 — Email notifications (optional)

Pattern: **SNS topic + email subscription + EC2 role can Publish**.

```bash
# .env: MYPROJECT_NOTIFY_EMAIL=you@example.com
0-work/scripts/aws/bootstrap_notifications.sh
# Confirm subscription in inbox (once)
0-work/scripts/aws/notify_sns.sh "Test" "Hello"
```

At end of remote jobs, publish summary:

```bash
aws sns publish --topic-arn "$MYPROJECT_SNS_TOPIC_ARN" \
  --subject "Job complete" --message "$(tail -40 /tmp/job.log)"
```

Use S3 conditional lock (`.notify_sent`) if multiple waiters might fire — see `ladder_wait_and_notify.sh`.

---

## Step 7 — Remote execution without SSH

### Option A — SSM Run Command (preferred for agents)

```bash
# Instance must have SSM agent + instance profile (Amazon Linux 2023: default)
aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --timeout-seconds 3600 \
  --parameters 'commands=["your script here"]' \
  --query Command.CommandId --output text

# Poll
aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID"
```

**Tips:**

- Base64-encode multi-line scripts: `echo "$B64" | base64 -d | bash`
- Set `--timeout-seconds` from estimated job duration
- `export AWS_PAGER=""` everywhere in headless environments

### Option B — EC2 user-data (fleet / one-shot workers)

Pass cloud-init script at `run-instances` — installs deps, pulls scripts from S3, runs job, uploads results to S3. See `run_ladder_rung.sh` `launch_worker()`.

### Option C — Async local fire-and-forget

Script starts SSM command, prints `CommandId`, exits. User gets SNS email when remote job finishes. See `--async` flags in `run_soak_on_ec2.sh`.

---

## Step 8 — Agent / Cursor workflow

1. Read `0-work/docs/spec.md` and this doc before planning
2. Put plans in `0-work/plans/`; side effects only via `0-work/scripts/`
3. Agent loads `.env`, runs bootstrap, logs to `log.md`
4. **AWS MCP** (optional): `awslabs.aws-api-mcp-server` — agent invokes CLI via MCP; same credentials as shell
5. Never echo secrets; never commit `.env`

For Cloud Agents: push scripts to git; bootstrap from branch; use `--async` + SNS so the agent does not need to stay alive for hour-long jobs.

---

## Step 9 — Checklist for a new project

```
[ ] Choose PROJECT slug, region, owner
[ ] Write aws-naming.md
[ ] Copy .env.example → .env (keys from IAM user)
[ ] Copy iam-baseline-policy.json — replace ARNs/names
[ ] Attach policy to IAM user in Console
[ ] aws sts get-caller-identity — verify
[ ] Adapt bootstrap_baseline.sh constants
[ ] Run bootstrap; fix AccessDenied by extending IAM JSON
[ ] (Optional) bootstrap_notifications.sh + confirm email
[ ] Upload app scripts to S3; test SSM command on EC2
[ ] Log every script run in log.md
[ ] Tag filter in Resource Groups: Project = your-project
```

---

## Minimal IAM JSON template

Replace `MYPROJECT`, account id, and bucket name:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket", "s3:ListBucket", "s3:GetObject", "s3:PutObject",
        "s3:HeadObject", "s3:PutBucketTagging", "s3:PutBucketPublicAccessBlock",
        "s3:PutEncryptionConfiguration"
      ],
      "Resource": [
        "arn:aws:s3:::myproject-data-ACCOUNT_ID",
        "arn:aws:s3:::myproject-data-ACCOUNT_ID/*"
      ]
    },
    {
      "Sid": "EC2",
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances", "ec2:TerminateInstances", "ec2:DescribeInstances",
        "ec2:DescribeImages", "ec2:DescribeSubnets", "ec2:DescribeVpcs",
        "ec2:CreateSecurityGroup", "ec2:AuthorizeSecurityGroupEgress",
        "ec2:CreateTags"
      ],
      "Resource": "*"
    },
    {
      "Sid": "IAM",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole", "iam:GetRole", "iam:PutRolePolicy",
        "iam:CreateInstanceProfile", "iam:GetInstanceProfile",
        "iam:AddRoleToInstanceProfile", "iam:PassRole"
      ],
      "Resource": [
        "arn:aws:iam::ACCOUNT_ID:role/myproject-ec2-role",
        "arn:aws:iam::ACCOUNT_ID:instance-profile/myproject-ec2-profile"
      ]
    },
    {
      "Sid": "SSM",
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand", "ssm:GetCommandInvocation",
        "ssm:DescribeInstanceInformation"
      ],
      "Resource": "*"
    }
  ]
}
```

Add SNS sid when you add notifications (copy from [`iam-baseline-policy.json`](../infra/iam-baseline-policy.json)).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `SignatureDoesNotMatch` | Secret key wrong length (40 chars); missing newline at end of `.env` |
| `AccessDenied` on CreateBucket | Attach/update `iam-baseline-policy.json` on IAM user |
| `AccessDenied` on `iam:TagRole` | Create role without tags first (API quirk); tags on EC2/S3 instead |
| SSM `Online` but command fails | Check instance profile S3/IAM permissions; script path on instance |
| SSM times out at 1 hour | Pass `--timeout-seconds` on `send-command` |
| Pager error (`less` not found) | `export AWS_PAGER=""` and `--no-cli-pager` |
| Duplicate SNS emails | Scope S3 paths per run ID; use `.notify_sent` lock object |
| `crontab: command not found` on AL2023 | `dnf install -y cronie && systemctl enable --now crond` |

---

## What to copy from Gypsy Danger verbatim

| File | Adapt |
|------|--------|
| `bootstrap_baseline.sh` | PROJECT names, bucket prefix, user-data |
| `bootstrap_notifications.sh` | Topic name, role name |
| `notify_sns.sh` | Env var names |
| `run_soak_on_ec2.sh` | Remote job + async + SNS pattern |
| `iam-baseline-policy.json` | ARNs, optional SQS/ASG later |

---

## Related docs

- [`aws-setup.md`](aws-setup.md) — Gypsy Danger quick start
- [`aws-naming.md`](aws-naming.md) — Gypsy Danger naming
- [`fetch-progress-notifications.md`](fetch-progress-notifications.md) — progress email events
- [`../plans/aws-distributed-fetch.md`](../plans/aws-distributed-fetch.md) — full fetch architecture
