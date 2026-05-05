# Deploying Multiestate Collector on AWS Lambda

This guide walks through running **`multiestate_collector.lambda_handler`** on AWS Lambda with **Amazon S3** for state and spool persistence. For behavior details (key layout, IAM actions), see [README.md](README.md).

**Assumptions:** You have an AWS account, permission to create S3 buckets, IAM roles, Lambda functions, and EventBridge rules. Replace placeholders such as `us-east-1`, `123456789012`, and bucket names with your own values.

---

## What gets deployed

| Component | Role |
|-----------|------|
| **S3 bucket** | Stores cursors, circuit breakers, `health.json`, and pending JSONL batches under a configurable prefix. |
| **Lambda function** | Runs one collection cycle per invocation (`asyncio.run(run_cycle(..., use_s3=True))`). |
| **EventBridge rule** | Invokes Lambda on a schedule (recommended; mirrors `poll_interval_seconds` in config). |
| **IAM** | Lambda execution role: S3 access to your prefix and CloudWatch Logs. |

The handler resolves the JSON config file from (first match):

1. `event["config_path"]` (useful for EventBridge scheduled events), or  
2. Environment variable **`MULTIESTATE_CONFIG`** or **`CONFIG_PATH`**.

The JSON file **must** include an **`s3`** block (`bucket`, optional `prefix`, optional `region`). Optional **`MULTIESTATE_*`** environment variables overlay values from that file (nested keys use `__`; see `load_config` in `multiestate_collector.py`).

---

## 1. Create the S3 bucket

Use a **dedicated bucket** (or a dedicated prefix in a shared bucket) so lifecycle and IAM policies stay simple.

### AWS CLI

```bash
export REGION=us-east-1
export BUCKET=my-org-multiestate-collector-state

aws s3api create-bucket \
  --bucket "$BUCKET" \
  --region "$REGION" \
  $( [ "$REGION" != "us-east-1" ] && echo "--create-bucket-configuration LocationConstraint=$REGION" )

aws s3api put-public-access-block \
  --bucket "$BUCKET" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption \
  --bucket "$BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

Optional: enable **versioning** if you want to recover overwritten state objects:

```bash
aws s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
```

Note the bucket name and prefix you will put in **`s3.bucket`** and **`s3.prefix`** in the Lambda config JSON (see `examples/lambda-config.example.json`).

### AWS Console

1. Open **S3** → **Create bucket**.
2. Choose the Region (must match Lambda unless you accept cross-region latency and complexity).
3. Block all public access (default).
4. Enable **Default encryption** (SSE-S3 or KMS).
5. Create the bucket.

---

## 2. Create the Lambda function

### 2.1 Execution role (IAM)

Create a role that **Lambda** can assume and attach:

1. **Trust policy** (who can assume the role): AWS service `lambda.amazonaws.com`.
2. **Permissions:**
   - **`AWSLambdaBasicExecutionRole`** (managed policy) — CloudWatch Logs.
   - **Inline policy** for your bucket and prefix (adjust `BUCKET` and `PREFIX` — use the prefix from config, with trailing path semantics; keys look like `multiestate/prod/state/...`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "StateAndSpoolObjects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::BUCKET/PREFIX*"
    },
    {
      "Sid": "ListPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::BUCKET",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["PREFIX*"]
        }
      }
    }
  ]
}
```

Replace `BUCKET` with your bucket name. Replace `PREFIX` with the normalized prefix (no leading slash; include the folder segment you use, e.g. `multiestate/prod/` — wildcards must match how your keys are stored).

CLI example (after editing the policy file):

```bash
aws iam create-role --role-name multiestate-collector-lambda \
  --assume-role-policy-document file://trust-lambda.json

aws iam attach-role-policy --role-name multiestate-collector-lambda \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

aws iam put-role-policy --role-name multiestate-collector-lambda \
  --policy-name multiestate-s3-access \
  --policy-document file://s3-inline-policy.json
```

### 2.2 Deployment package

The handler is **`multiestate_collector.lambda_handler`**, so the deployment artifact must include **`multiestate_collector.py`** at the **top level** of the zip (not nested under a parent folder when you upload).

Dependencies (`requirements.txt`: `httpx`, `pydantic`, `boto3`) must be installed into the same zip root so imports resolve.

**Build on Linux (recommended)** — matches the Lambda execution environment and avoids Windows-specific binary issues:

```bash
mkdir -p build/package
pip install -r requirements.txt -t build/package/
cp multiestate_collector.py build/package/
( cd build/package && zip -r ../function.zip . )
```

**Config in the zip:** Copy your real config (with `s3` filled in) into the package, e.g. `build/package/config.json`, then zip. At runtime set **`CONFIG_PATH=/var/task/config.json`** (or **`MULTIESTATE_CONFIG=/var/task/config.json`**) on the function. Prefer **not** committing secrets: keep a minimal JSON in the zip and inject secrets via **`MULTIESTATE_TAEGIS__CLIENT_SECRET`**-style env vars.

### 2.3 Create the function (CLI)

Pick a runtime that matches your local testing (e.g. **Python 3.12**).

```bash
aws lambda create-function \
  --function-name multiestate-collector \
  --runtime python3.12 \
  --role arn:aws:iam::123456789012:role/multiestate-collector-lambda \
  --handler multiestate_collector.lambda_handler \
  --zip-file fileb://build/function.zip \
  --timeout 900 \
  --memory-size 512 \
  --environment "Variables={CONFIG_PATH=/var/task/config.json}" \
  --region "$REGION"
```

**Timeout:** One run is a **full cycle** (Sophos pull, batching, Taegis uploads). Start with **5–15 minutes**; increase toward **900 seconds** if many estates or large backlogs cause timeouts.

**Memory:** More memory increases CPU proportionally; adjust after you see cold-start and cycle duration in CloudWatch.

### 2.4 Create the function (Console)

1. **Lambda** → **Create function** → Author from scratch.  
2. Runtime: **Python 3.11** or **3.12**. Architecture: **x86_64** unless you built arm64 wheels.  
3. Execution role: use the role created above.  
4. After creation: **Code** → **Upload from** → **.zip file** → upload `function.zip`.  
5. **Runtime settings** → **Handler** → `multiestate_collector.lambda_handler`.  
6. **Configuration** → **Environment variables**: set **`CONFIG_PATH`** or **`MULTIESTATE_CONFIG`** to `/var/task/config.json` if the config is inside the zip.  
7. **Configuration** → **General configuration**: set **Timeout** and **Memory** as above.

---

## 3. Schedule the function (EventBridge)

Lambda is **event-driven**: each invocation runs **one** cycle. Schedule should align with **`poll_interval_seconds`** in your JSON (e.g. hourly → rule every hour).

### EventBridge rule invoking Lambda (CLI)

**Rate expression** (every hour):

```bash
aws events put-rule \
  --name multiestate-collector-hourly \
  --schedule-expression "rate(1 hour)" \
  --state ENABLED \
  --region "$REGION"

aws lambda add-permission \
  --function-name multiestate-collector \
  --statement-id AllowEventBridgeInvoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:$REGION:123456789012:rule/multiestate-collector-hourly

aws events put-targets \
  --rule multiestate-collector-hourly \
  --targets "Id"="1","Arn"="arn:aws:lambda:$REGION:123456789012:function:multiestate-collector","Input"='{"config_path":"/var/task/config.json"}'
```

Use **`Input`** (or **Input transformer**) so **`config_path`** is set when you rely on `event["config_path"]`. If you only use **`CONFIG_PATH`** env, a constant **`{}`** input is fine.

**Cron expression** (weekdays at minute 0 past each hour UTC):

```bash
aws events put-rule \
  --name multiestate-collector-weekday-hourly \
  --schedule-expression "cron(0 * ? * MON-FRI *)" \
  --state ENABLED
```

### Console

1. **Amazon EventBridge** → **Rules** → **Create rule**.  
2. **Schedule pattern** → define rate or cron.  
3. **Target** → **AWS Lambda** → select **`multiestate-collector`**.  
4. Under **Configure target**, set **Additional configuration** → **Configure input** → **Constant (JSON text)** → e.g. `{"config_path":"/var/task/config.json"}`.  
5. Create the rule; accept the prompt to add **invoke permission** for EventBridge on the Lambda function.

---

## 4. Monitor and troubleshoot

### 4.1 CloudWatch Logs

Each invocation writes to a **log group** named **`/aws/lambda/multiestate-collector`** (unless renamed). Search for:

- **`lambda_handler_failed`** — exception during the cycle; stack trace in the same log entry.
- **`cycle_failed`** — cycle-level failure (if logged before handler exit).
- **`ERROR`** level lines from configured `log_level`.

**Tips:**

- Enable **CloudWatch Logs Insights** queries, e.g. filter `@message like /ERROR/` over the last 24 hours.
- If logs never appear, confirm the execution role has **`logs:CreateLogGroup`**, **`logs:CreateLogStream`**, **`logs:PutLogEvents`** (via `AWSLambdaBasicExecutionRole`).

### 4.2 Lambda metrics and alarms

In **CloudWatch** → **Metrics** → **Lambda**:

| Metric | Use |
|--------|-----|
| **Invocations** | Confirms the schedule is firing. |
| **Errors** | Non-zero indicates thrown exceptions or runtime failures. |
| **Duration** | Compare to **Timeout**; increase timeout or memory if near limit. |
| **Throttles** | Account concurrency limits; request increase if needed. |

Create **alarms** on **Errors > 0** or **Duration > threshold** for proactive paging.

### 4.3 S3 state and health

Inspect objects under your prefix:

- **`{prefix}state/health.json`** — last successful Sophos pull timestamps, last Taegis upload, spool depth.
- **`{prefix}state/cursors/*.json`** — cursor progression per estate.
- **`{prefix}spool/*.log`** — if these accumulate, Taegis upload may be failing or throttled; check logs and Taegis credentials.

### 4.4 Common issues

| Symptom | Things to check |
|---------|------------------|
| Return value `{"ok": false, "error": "Missing config path..."}` | Set **`event.config_path`** on the schedule target or **`CONFIG_PATH`** / **`MULTIESTATE_CONFIG`** env to a path that exists in the zip (e.g. `/var/task/config.json`). |
| Return value mentions **`s3`** | Config JSON must include **`s3.bucket`** (and optional **`prefix`**) for Lambda mode. |
| **AccessDenied** on S3 | IAM policy **Resource** / **prefix** conditions must cover actual keys; verify bucket name and prefix string. |
| **Timeout** | Increase Lambda timeout; reduce **`max_pages_per_estate_per_cycle`** or estates per function; split estates across functions if needed. |
| **SSL / connection errors** to Sophos or Taegis | Lambda must have **internet egress** (default when not in a VPC); if using a VPC, add **NAT Gateway** or VPC endpoints as appropriate. |
| Works locally with **`--lambda`**, fails on Lambda | Compare region, IAM, and exact **`s3`** settings; confirm deployment zip layout and handler string. |

### 4.5 Optional hardening

- **Dead-letter queue (DLQ)** on async invocation configuration for failures (more relevant for async patterns).  
- **AWS X-Ray** if you add tracing downstream.  
- **Secrets Manager** or **SSM Parameter Store** for secrets; you can resolve secrets into env vars at deploy time or extend the loader (today the script reads JSON + **`MULTIESTATE_*`** overrides).

---

## Quick reference

| Item | Value |
|------|--------|
| Handler | `multiestate_collector.lambda_handler` |
| Config path | `event["config_path"]` or env **`MULTIESTATE_CONFIG`** / **`CONFIG_PATH`** |
| Config requirements | Valid collector JSON + **`s3`** block |
| Scheduled payload | e.g. `{"config_path":"/var/task/config.json"}` |

For local testing against S3 before deploying:

```bash
python multiestate_collector.py --config your-config-with-s3.json --lambda
```
