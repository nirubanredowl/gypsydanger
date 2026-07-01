# AWS setup — CLI and MCP

Tools for deploying and operating the Stage 2 distributed fetch from Cursor.

## AWS CLI

Installed in this environment:

```bash
aws --version   # aws-cli/2.x
```

### Authenticate (one-time per session)

Use **`aws login`** (CLI 2.32+) for short-lived credentials:

```bash
aws configure set region ap-southeast-2
aws login --remote
```

On this cloud VM, `aws login --remote` prints a URL — open it in your browser, sign in, then paste the **authorization code** back into the waiting terminal.

Verify:

```bash
aws sts get-caller-identity
```

On a machine with a local browser, `aws login` (without `--remote`) works too.

Optional: store non-secret defaults in `0-work/scripts/.env` (gitignored):

```bash
AWS_REGION=ap-southeast-2
GYPSY_S3_BUCKET=gypsy-danger-asx   # set after deploy
```

Never commit access keys. Workers use **IAM instance roles**, not keys.

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
