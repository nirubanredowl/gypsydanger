# AWS setup — CLI and MCP

Tools for deploying and operating the Stage 2 distributed fetch from Cursor.

## AWS CLI

Installed in this environment:

```bash
aws --version   # aws-cli/2.x
```

### Authenticate

**Cloud VM / headless (recommended):** access keys in `0-work/scripts/.env` (gitignored):

```bash
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=ap-southeast-2
```

Load and verify:

```bash
set -a && source 0-work/scripts/.env && set +a
aws sts get-caller-identity
```

**Local dev (optional):** use **`aws login`** (CLI 2.32+) for short-lived credentials instead of keys.

Never commit `.env`. EC2 workers use **IAM instance roles**, not keys.

### Baseline infrastructure (soak phase)

Naming and tags: [`aws-naming.md`](aws-naming.md)

```bash
set -a && source 0-work/scripts/.env && set +a
0-work/scripts/aws/bootstrap_baseline.sh
```

Creates (idempotent):

- S3 `gypsy-danger-asx-{account_id}`
- IAM role + instance profile for EC2 → S3
- Security group (egress 443 only)
- One EC2 `gypsy-danger-soak-01` (t3.small)

**IAM:** attach [`0-work/infra/iam-baseline-policy.json`](../infra/iam-baseline-policy.json) to `niruban_cursor` first (Console → IAM → Users → Add permissions → Create inline policy from JSON).

## AWS MCP (Cursor)

MCP is configured in two places (Cursor merges them):

| File | Servers |
|------|---------|
| [`.cursor/mcp.json`](../../.cursor/mcp.json) | `awslabs.aws-api-mcp-server` — runs AWS CLI via MCP |
| [`~/.cursor/mcp.json`](file:///home/ubuntu/.cursor/mcp.json) | Same + **`aws-mcp`** (AWS Core plugin proxy) |

Uses the official [AWS API MCP Server](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server) and [AWS MCP proxy](https://aws.amazon.com/solutions/guidance/using-model-context-protocol-with-aws-services/) from the AWS Cursor plugin.

| Variable | Value |
|----------|-------|
| `AWS_REGION` | `ap-southeast-2` |
| Credentials | Same chain as AWS CLI (`aws login` / SSO / env) |

After changing MCP config, reload Cursor (or restart the MCP server in **Settings → MCP**).

### Verify MCP locally

```bash
export PATH="$HOME/.local/bin:$PATH"
uvx awslabs.aws-api-mcp-server@latest --help
```

MCP tools appear once credentials are valid:

```bash
aws sts get-caller-identity
```

### What MCP enables

The agent can propose and run AWS CLI operations (create S3 bucket, list queues, describe instances) without opening the AWS Console. Infrastructure-as-code (CDK in `0-work/infra/`) remains the preferred way to create resources repeatably.

## Related docs

- [`0-work/plans/aws-distributed-fetch.md`](../plans/aws-distributed-fetch.md) — architecture
- [`0-work/plans/plan.md`](../plans/plan.md) — Stage 2 execution phases
