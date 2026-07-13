# AWS Deployment Guide

How to get RiskGuard AI running on AWS with a public URL, matching the architecture
`PLAN.md` was designed around: **ECS Fargate + ALB + Secrets Manager + a task IAM role for
Bedrock** (no long-lived AWS keys in the app). Written for someone doing this for the
first time and using it as a job-search portfolio piece — every step says *why*, not just
*what*, so you can talk about it in an interview.

Repo: https://github.com/Apolloat2022/riskguard-ai

## Why Fargate (and not something cheaper)

`App Runner` or `Lightsail` would be cheaper and simpler. This guide uses **ECS Fargate
behind an Application Load Balancer** anyway because:

1. It's what `PLAN.md` already documents, so your README/architecture diagram and your
   actual deployment match — no story to explain away in an interview.
2. It's the AWS setup employers most often expect: VPC, security groups, ECS task
   definitions, task **execution** role vs task **role**, ALB, Secrets Manager. Getting
   hands-on with all of it is the point, not just having a green checkmark.
3. It's honest about the cost tradeoff — flagged clearly below so it doesn't surprise you.

**Cost reality check**: the ALB is the dominant recurring cost (~$16–20/month) and it
bills **whether or not any tasks are running**. Fargate compute itself (0.5 vCPU / 1GB,
one task) is roughly $18/month if left running 24/7, or pennies per hour if you start/stop
it around interviews. See [Cost control & teardown](#cost-control--teardown) — read that
section before you start, not after your first bill.

## Architecture

```
GitHub (Apolloat2022/riskguard-ai)
   │  push to main
   ▼
GitHub Actions CI  ──build & push──▶  Amazon ECR (riskguard-ai image)
                                            │
                                            ▼
                                     ECS Fargate Service
                                     (task def: 1 container, port 8000)
                                     ├─ execution role → pull from ECR, write CloudWatch
                                     │                    logs, read Secrets Manager
                                     └─ task role       → bedrock:InvokeModel
                                            ▲
                                            │ HTTP :8000
                                     Application Load Balancer (public)
                                            ▲
                                            │
                                       your browser
                                            │
                              (outbound, from inside the task)
                              ├──▶ Neon Postgres (external, already set up)
                              └──▶ Amazon Bedrock (Claude Sonnet 5, same account/region)
```

No NAT Gateway, no private subnets: the Fargate task runs in a **public subnet with a
public IP**, so it can reach Neon and Bedrock over the internet directly. This is the
single biggest cost trap in most ECS tutorials — a NAT Gateway is ~$32+/month by itself for
something this app doesn't need, since there's no private-subnet resource (like RDS) to
protect. Skip it.

## Prerequisites

- An AWS account (not your everyday account if you can help it — use a fresh one, or at
  least an IAM user with scoped permissions, not the root login).
- AWS CLI v2 installed and configured (`aws configure`).
- Docker (already installed and verified working locally per `BUILD_STATUS.md`).
- The GitHub repo already created: https://github.com/Apolloat2022/riskguard-ai

Install AWS CLI (Windows):
```powershell
winget install Amazon.AWSCLI
```

---

## Step 1 — Push the code to GitHub

This directory isn't a git repo yet. `.gitignore` already excludes `.env`,
`ml/artifacts/`, and `.venv/` — double-check `git status` before your first commit that
none of those slip in anyway.

```bash
git init
git branch -M main
git remote add origin https://github.com/Apolloat2022/riskguard-ai.git
git add .
git status   # confirm no .env, no ml/artifacts/, no .venv/ in the list
git commit -m "Initial commit: RiskGuard AI"
git push -u origin main
```

## Step 2 — Set a budget alarm before creating anything

Do this first. Five minutes now avoids a surprise bill later.

```bash
aws budgets create-budget \
  --account-id <YOUR_ACCOUNT_ID> \
  --budget '{
    "BudgetName": "riskguard-ai-monthly",
    "BudgetLimit": {"Amount": "25", "Unit": "USD"},
    "TimeUnit": "MONTHLY",
    "BudgetType": "COST"
  }' \
  --notifications-with-subscribers '[{
    "Notification": {"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80},
    "Subscribers": [{"SubscriptionType":"EMAIL","Address":"<your-email>"}]
  }]'
```

(Or do this in the console: Billing → Budgets → Create budget. Either way, don't skip it.)

## Step 3 — Create a scoped IAM user for deployment

Don't use root credentials for anything below. Create an IAM user (or better, an IAM
Identity Center permission set) with a policy scoped to what this project actually
touches: ECR, ECS, IAM (to create the two roles below), Secrets Manager, CloudWatch Logs,
ELB, and Bedrock model-access management. Attaching `AdministratorAccess` temporarily
while you learn is fine for a personal/portfolio account — just don't leave it that way,
and don't commit the access keys anywhere.

```bash
aws iam create-user --user-name riskguard-deployer
aws iam attach-user-policy --user-name riskguard-deployer \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess   # tighten later
aws iam create-access-key --user-name riskguard-deployer
```

Configure a named CLI profile with those keys: `aws configure --profile riskguard`. Use
`--profile riskguard` on every command below (omitted from examples for readability).

## Step 4 — Confirm Bedrock model access

AWS retired the old manual "Model access" console page — serverless foundation models are
now enabled automatically on first invocation. In practice this still needs verifying
before you build anything on top of it, because two things can silently block a model:

1. **Flagship/newest models need extra account approval.** `anthropic.claude-sonnet-5`
   (the bare model ID) returned `AccessDeniedException: ... is not available for this
   account ... contact AWS Sales` on this project, even after "using" it through the
   console Workbench — that's a real account-level gate, not a one-time click-through.
2. **Some models require an inference-profile ID, not the bare model ID.** Newer/larger
   models fail on-demand invocation with `ValidationException: ... isn't supported. Retry
   your request with the ID or ARN of an inference profile` unless you use the
   region-prefixed profile ID (e.g. `us.anthropic.claude-sonnet-4-5-20250929-v1:0` instead
   of `anthropic.claude-sonnet-4-5-20250929-v1:0`).

Don't assume access — test it directly with a real `invoke-model` call before wiring up
anything downstream:

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'claude')].inferenceProfileId"

aws bedrock-runtime invoke-model \
  --region us-east-1 \
  --model-id "us.anthropic.claude-sonnet-4-5-20250929-v1:0" \
  --content-type application/json --accept application/json \
  --cli-binary-format raw-in-base64-out \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":16,"messages":[{"role":"user","content":"Say OK"}]}' \
  /tmp/out.json && cat /tmp/out.json
```

If that returns a real completion, you're set. On this project, `claude-sonnet-5` and
`claude-fable-5` were both gated behind AWS Sales approval, while
`us.anthropic.claude-sonnet-4-5-20250929-v1:0` and
`us.anthropic.claude-haiku-4-5-20251001-v1:0` worked immediately with no approval step —
`BEDROCK_MODEL_ID` in `.env`/`.env.example` is set to the Sonnet 4.5 profile ID
accordingly. If you later get Sonnet 5 approved, switching the model back is a one-line
env var change (`app/agent/llm.py` just passes `BEDROCK_MODEL_ID` through).

**A third, separate gate**: Anthropic requires a one-time "use case details" form per AWS
account before their models can be invoked *through the Anthropic SDK* — independent of
the IAM/model access above, which the raw `invoke-model` CLI call bypasses entirely. The
error looks like `Model use case details have not been submitted for this account`. The
old manual-approval "Model access" page is retired, but the form itself still exists:
open the **classic** Bedrock console (drop `-mantle` from the console URL) → **Model
catalog** → click into any Claude model → a banner reads *"Anthropic requires first-time
customers to submit use case details..."* with a **Submit use case details** button.
Fill in company/website/industry/intended-users/use-case description and submit — takes a
few minutes to propagate.

**A fourth gotcha that cost real debugging time**: `app/agent/llm.py` originally used
`AnthropicBedrockMantle` (the SDK's newer bedrock-mantle-endpoint client). That endpoint
turned out to be gated *separately* from classic Bedrock runtime access on this account —
every model returned `403 ... is not available for this account`, even after the use-case
form above was submitted and confirmed working for the classic path. The fix was to
switch the import to the classic `AnthropicBedrock` client (same inference-profile model
ID otherwise) — verify with:

```bash
.venv/Scripts/python.exe -c "
from anthropic import AnthropicBedrock
client = AnthropicBedrock(aws_region='us-east-1')
r = client.messages.create(model='us.anthropic.claude-sonnet-4-5-20250929-v1:0', max_tokens=16, messages=[{'role':'user','content':'Say OK'}])
print(r.content)
"
```

If your account's mantle access is enabled (unlike this one), `AnthropicBedrockMantle`
may work fine and would be the more modern choice — just confirm with a real call before
building on top of it either way, for the same reason as everything else in this step.

## Step 5 — One required code change: switch the checkpointer to Postgres

`app/agent/graph.py` defaults `CHECKPOINTER_BACKEND` to `"memory"` (LangGraph's
`MemorySaver`), which lives in the process's RAM. Fargate tasks are ephemeral — a
deployment, a scaling event, or a crash kills the task and any paused
human-in-the-loop remediation workflow is gone. `PLAN.md` already calls this out and the
switch is a one-line env var, not a code change:

```
CHECKPOINTER_BACKEND=postgres
```

This makes `app/agent/graph.py` use `AsyncPostgresSaver` against the same Neon database
as `DATABASE_URL`, and it calls `.setup()` automatically on first startup — no manual
migration step. Set this in Secrets Manager / the task definition below, not in `memory`
mode, for anything you're going to demo live.

## Step 6 — Put secrets in Secrets Manager

Don't put `DATABASE_URL` in plaintext task-definition environment variables — anyone with
`ecs:DescribeTaskDefinition` (a very common read-only permission) could read it. Use
Secrets Manager and reference it from the task definition instead.

```bash
aws secretsmanager create-secret \
  --name riskguard-ai/database-url \
  --secret-string "postgresql+asyncpg://<user>:<pass>@<host>/riskguard?ssl=require"
```

The other env vars (`AWS_REGION`, `BEDROCK_MODEL_ID`, `MODEL_ARTIFACT_DIR`,
`RISK_TRIGGER_THRESHOLD`, `CHECKPOINTER_BACKEND`, `LOG_LEVEL`) aren't secrets — they go as
plain environment variables in the task definition (Step 10).

## Step 7 — Create the ECR repository and push the image

```bash
aws ecr create-repository --repository-name riskguard-ai

# Train artifacts if you haven't already (ml/artifacts/v1 is gitignored, not baked into git)
python ml/train.py

# Log in, build, tag, push
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com

docker build -t riskguard-ai .
docker tag riskguard-ai:latest <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/riskguard-ai:latest
docker push <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/riskguard-ai:latest
```

## Step 8 — The two IAM roles ECS needs

This is the detail worth understanding, not just copy-pasting: ECS Fargate tasks use
**two separate roles** with different jobs.

- **Task execution role** — used by the ECS agent *before your code runs*: pull the image
  from ECR, write logs to CloudWatch, fetch the Secrets Manager secret to inject as an
  env var.
- **Task role** — assumed *by your application code* at runtime. This is what
  `boto3`/the Anthropic Bedrock client picks up automatically (the "standard AWS
  credential chain" mentioned in `.env`) — no keys anywhere in the app.

```bash
# Execution role
aws iam create-role --role-name riskguard-execution-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }'
aws iam attach-role-policy --role-name riskguard-execution-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name riskguard-execution-role \
  --policy-name read-db-secret \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Action":"secretsmanager:GetSecretValue",
      "Resource":"arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:riskguard-ai/database-url-*"}]
  }'

# Task role — this is the one that lets app/agent/llm.py call Bedrock with no static keys
aws iam create-role --role-name riskguard-task-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }'
aws iam put-role-policy --role-name riskguard-task-role \
  --policy-name invoke-bedrock \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
      "Resource":[
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:us-east-1:<ACCOUNT_ID>:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
      ]}]
  }'
```

(Using the inference-profile ID here, per Step 4 — IAM needs permission on both the
underlying foundation-model ARN and the inference-profile ARN that routes to it.)

## Step 9 — Register the task definition

```json
{
  "family": "riskguard-ai",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/riskguard-execution-role",
  "taskRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/riskguard-task-role",
  "containerDefinitions": [{
    "name": "riskguard-ai",
    "image": "<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/riskguard-ai:latest",
    "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
    "environment": [
      {"name": "AWS_REGION", "value": "us-east-1"},
      {"name": "BEDROCK_MODEL_ID", "value": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"},
      {"name": "MODEL_ARTIFACT_DIR", "value": "ml/artifacts/v1"},
      {"name": "RISK_TRIGGER_THRESHOLD", "value": "0.70"},
      {"name": "CHECKPOINTER_BACKEND", "value": "postgres"},
      {"name": "LOG_LEVEL", "value": "INFO"}
    ],
    "secrets": [
      {"name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:us-east-1:<ACCOUNT_ID>:secret:riskguard-ai/database-url"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "/ecs/riskguard-ai",
        "awslogs-region": "us-east-1",
        "awslogs-stream-prefix": "riskguard"
      }
    }
  }]
}
```

```bash
aws logs create-log-group --log-group-name /ecs/riskguard-ai
aws ecs register-task-definition --cli-input-json file://task-def.json
```

## Step 10 — Cluster, ALB, and Service (console is easier here)

Creating the ALB, target group, and security groups by hand via CLI is a lot of ARNs to
juggle for a one-off deploy. The ECS console's **Create Cluster → Create Service** wizard
does all three in one flow when you pick "Application Load Balancer":

1. **ECS console → Clusters → Create cluster** → name `riskguard-cluster`, infrastructure
   = Fargate.
2. Inside the cluster → **Create service**:
   - Launch type: Fargate. Task definition: `riskguard-ai`.
   - Desired tasks: **1** (a portfolio demo doesn't need HA; bump to 2 across AZs if you
     want to talk about that in an interview).
   - Networking: pick the **default VPC**, all **public subnets**, and check
     **"Auto-assign public IP"** — this is what avoids the NAT Gateway.
   - Security group: allow inbound TCP 8000 from the ALB's security group only (the
     wizard creates this correctly if you let it manage the SG).
   - Load balancer: Application Load Balancer → create new → target group health check
     path **`/docs`** (there's no dedicated `/healthz` route yet — FastAPI's
     auto-generated `/docs` returns 200 once the app is up, confirmed in the local
     container smoke test).
3. Create. Wait for the service to reach `RUNNING` / target group `healthy`.

Get the ALB's public DNS name from the console (or `aws elbv2 describe-load-balancers`) —
that's your public URL.

## Step 11 — Verify it

```bash
curl -s http://<alb-dns-name>/openapi.json | head -c 200
curl -s -X POST http://<alb-dns-name>/api/v1/risk-assessment/28
```

Customer 28 is the same seeded high-risk demo customer already exercised locally — you
should see `risk_flag: CRITICAL` and, this time, a real Bedrock-drafted remediation plan
instead of the `Could not resolve AWS credentials` error from the local container test
(that error only happened because no AWS credentials existed in the local dev
environment — the task role fixes exactly that).

## Step 12 — CI/CD: auto-deploy on push to `main`

`.github/workflows/ci.yml` already lints, trains artifacts, tests, and does a
`docker build` — it just never pushes anywhere. Add a deploy job. Use GitHub's OIDC
provider to assume an AWS role instead of storing long-lived access keys as GitHub
secrets — this is worth doing right and worth mentioning in an interview.

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

aws iam create-role --role-name riskguard-gha-deploy \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Federated":"arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"},
      "Action":"sts:AssumeRoleWithWebIdentity",
      "Condition":{"StringEquals":{
        "token.actions.githubusercontent.com:sub":"repo:Apolloat2022/riskguard-ai:ref:refs/heads/main"
      }}
    }]
  }'
# Attach a policy scoped to ecr:*, ecs:UpdateService/DescribeServices/RegisterTaskDefinition
```

Append to `.github/workflows/ci.yml`:

```yaml
  deploy:
    runs-on: ubuntu-latest
    needs: docker-build
    if: github.ref == 'refs/heads/main'
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::<ACCOUNT_ID>:role/riskguard-gha-deploy
          aws-region: us-east-1
      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr
      - name: Build, tag, push
        run: |
          python ml/generate_dataset.py --rows 5000 --seed 42
          python ml/train.py
          docker build -t ${{ steps.ecr.outputs.registry }}/riskguard-ai:${{ github.sha }} .
          docker push ${{ steps.ecr.outputs.registry }}/riskguard-ai:${{ github.sha }}
      - name: Update ECS service
        run: |
          aws ecs update-service --cluster riskguard-cluster \
            --service riskguard-ai --force-new-deployment
```

(This assumes you update the task definition's image tag separately, or switch to
`:latest` and just force a new deployment — fine for a portfolio project; a stricter
pipeline would render a new task-definition revision per SHA.)

## Cost control & teardown

The ALB bills hourly whether tasks are running or not. Two options:

**Pause between interviews/demos** (keeps everything configured, ECS + ALB charges stop
for compute but ALB itself still bills ~$16-20/mo):
```bash
aws ecs update-service --cluster riskguard-cluster --service riskguard-ai --desired-count 0
```

**Full teardown** (stops all charges, takes ~10 min to recreate from this doc + your
pushed image):
```bash
aws ecs update-service --cluster riskguard-cluster --service riskguard-ai --desired-count 0
aws ecs delete-service --cluster riskguard-cluster --service riskguard-ai
# then delete the ALB, target group, and cluster from the console (fastest for the
# ALB/target-group pair specifically — it's two clicks vs several CLI calls)
aws ecs delete-cluster --cluster riskguard-cluster
aws ecr delete-repository --repository-name riskguard-ai --force
```

Since your image is already in ECR and this doc has every command, redeploying before an
interview is fast — you don't need to leave it running between demos.

## What this demonstrates (for your resume/interview prep)

- Containerized a Python ML + FastAPI + LangGraph app for Fargate (multi-stage
  Dockerfile, non-root user).
- IAM task roles vs execution roles, least-privilege Bedrock access — no static AWS keys
  in the app.
- Secrets Manager for connection strings instead of plaintext env vars.
- Recognized and avoided the NAT Gateway cost trap by using public subnets with
  auto-assigned public IPs.
- OIDC federation from GitHub Actions to AWS — no long-lived credentials in CI.
- Understood *why* the checkpointer had to move from in-memory to Postgres-backed before
  the human-in-the-loop workflow could survive on ephemeral Fargate tasks.
