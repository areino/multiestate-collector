# Deploying Multiestate Collector on AWS Lambda

This guide uses the **AWS Management Console** for creating resources and schedules. For behavior details (S3 key layout, IAM actions, config schema), see [README.md](README.md) and `examples/lambda-config.example.json`.

**Assumptions:** You can sign in to the correct AWS account and Region, and you have rights to create S3 buckets, IAM roles, Lambda functions, and EventBridge rules.

---

## What you will create

| Component | Role |
|-----------|------|
| **S3 bucket** | Stores cursors, circuit breakers, `health.json`, and pending JSONL batches under your configured prefix. |
| **IAM role** | Lets Lambda write logs and read/write objects under that bucket prefix. |
| **Lambda function** | Runs **one** collection cycle per invocation (`multiestate_collector.lambda_handler`). |
| **EventBridge rule** | Invokes Lambda on a schedule (aligned with `poll_interval_seconds` in your JSON). |

**Config:** The handler loads JSON in this order: environment **`MULTIESTATE_CONFIG_JSON`** (full JSON inline; env size limits apply), **or** a file path from **`event["config_path"]`** / **`MULTIESTATE_CONFIG`** / **`CONFIG_PATH`**. The JSON **must** include an **`s3`** object (`bucket`, optional `prefix`, optional `region`). You may overlay secrets with **`MULTIESTATE_*`** env vars (nested keys use `__`; see `load_config_dict` in `multiestate_collector.py`).

---

## 1. Create the S3 bucket (console)

Use a **dedicated bucket** so IAM stays straightforward.

1. Sign in to **AWS Management Console** → open **Amazon S3**.
2. Choose **Buckets** → **Create bucket**.
3. **Bucket name:** Globally unique name (for example `mycompany-multiestate-collector-state`).
4. **AWS Region:** Pick the Region where you will run Lambda (same Region avoids cross-region latency and simplifies IAM).
5. **Object Ownership:** Keep **ACLs disabled** unless your org requires otherwise.
6. **Block Public Access settings for this bucket:** Leave **Block *all* public access** selected.
7. **Bucket Versioning:** Optional — enable if you want to recover overwritten state files.
8. **Default encryption:** Enable **Server-side encryption** with **Amazon S3 managed keys (SSE-S3)** (or KMS if your policy requires it).
9. Choose **Create bucket**.

**Write down** the bucket name and the **prefix** you will use in config (for example `multiestate/prod/`). You will put them in **`s3.bucket`** and **`s3.prefix`** in the JSON that Lambda loads.

---

## 2. Create the Lambda execution role (console)

The function needs a role with **CloudWatch Logs** and **S3** access to your prefix.

### 2.1 Create the role

1. Open **IAM** → **Roles** → **Create role**.
2. **Trusted entity type:** **AWS service**.
3. **Use case:** **Lambda** → **Next**.
4. **Add permissions —** search for and select **`AWSLambdaBasicExecutionRole`** (AWS managed). This allows writing to CloudWatch Logs.
5. **Next.** Role name: for example `multiestate-collector-lambda`. **Create role**.

### 2.2 Add S3 permissions for your bucket and prefix

The collector calls **`ListObjectsV2`** for the spool prefix (uploads, depth, listing pending files). That API requires **`s3:ListBucket`** on the **bucket** ARN. Object-only policies (`…/*` without `ListBucket`) will fail with **AccessDenied** on `ListObjectsV2`.

1. Open the role you just created → **Permissions** tab → **Add permissions** → **Create inline policy** (or edit the existing S3 policy).
2. Choose the **JSON** tab and use one of the options below.
3. Replace **`YOUR-BUCKET-NAME`** with your S3 bucket name (e.g. `multiestate-collector-s3`).

**Option A — Simple (recommended to get running)** — list and read/write any object in this bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME"
    },
    {
      "Sid": "ObjectReadWrite",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/*"
    }
  ]
}
```

**Option B — Prefix-scoped objects** — limits **`GetObject`/`PutObject`/`DeleteObject`** to keys under your prefix; **`ListBucket`** must still be allowed, and any **`s3:prefix`** condition must cover **every** prefix the app uses (including **`…/state/`** and **`…/spool/`** — see README key layout). If in doubt, use Option A or **`ListBucket`** without a prefix condition.

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
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME/YOUR-PREFIX*"
    },
    {
      "Sid": "ListBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::YOUR-BUCKET-NAME",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["YOUR-PREFIX*"]
        }
      }
    }
  ]
}
```

4. **Next** → Policy name: for example `multiestate-collector-s3` → **Create policy**.

If you still see **`AccessDenied`** on **`ListObjectsV2`**, switch **`ListBucket`** to **no** `Condition` block (Option A pattern), or widen **`s3:prefix`** so it matches the configured **`s3.prefix`** plus **`state/`** and **`spool/`** segments.

---

## 3. Build the deployment package (required: Docker)

Lambda runs **Amazon Linux**. **`pydantic`** depends on **`pydantic_core`**, which ships **native binaries**. If you run `pip install -r requirements.txt -t …` on **Windows**, you get Windows `.pyd` files. Lambda then fails with:

`Runtime.ImportModuleError: No module named 'pydantic_core._pydantic_core'`

**Always build the zip using the official Lambda Python base image** (same OS and ABI as production). The repo includes scripts that run **`pip install`** inside **`public.ecr.aws/lambda/python`** so wheels match Lambda.

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows or macOS) or Docker on Linux, running before you execute the script.

### Option A — Scripts (recommended)

From the **repository root**:

**Windows (PowerShell):**

```powershell
.\scripts\package-lambda.ps1
```

Optional: bundle `config.json` into the zip:

```powershell
.\scripts\package-lambda.ps1 -ConfigPath .\my-lambda-config.json
```

If your Lambda function uses **arm64**:

```powershell
.\scripts\package-lambda.ps1 -Arm64
```

The scripts mount your repo into **`public.ecr.aws/lambda/python`**, install dependencies there (Linux wheels), and write **`build/function.zip`** using Python’s **`zipfile`** inside the container so you avoid Docker-on-Windows permission quirks.

**Linux or macOS (bash):**

```bash
chmod +x scripts/package-lambda.sh scripts/docker-pack-inner.sh   # once
./scripts/package-lambda.sh
```

Optional:

```bash
CONFIG_PATH=./my-lambda-config.json RUNTIME=3.12 ARCH=x86_64 ./scripts/package-lambda.sh
ARCH=arm64 ./scripts/package-lambda.sh
```

Each run creates a new folder **`build/package-<timestamp>/`** (older runs are not deleted automatically because Docker Desktop on Windows sometimes blocks removing prior **`pip -t`** trees on the bind-mounted volume). You can delete **`build\package-*`** or the whole **`build`** folder periodically when disk space matters.

Under the hood both wrappers call **`scripts/docker-pack-inner.sh`** inside the container.

Output artifact: **`build/function.zip`**. Upload it in the Lambda console (**Code** → **Upload from** → **.zip file**).

### Option B — One Docker command

Same as the scripts, without PowerShell (from repo root; **PowerShell** or **bash**):

```text
docker run --rm --entrypoint /bin/bash -v "%CD%:/workspace" -w /workspace public.ecr.aws/lambda/python:3.12 /workspace/scripts/docker-pack-inner.sh
```

(On PowerShell, **`${PWD}`** or an explicit path works instead of **`%CD%`**.)

Staging **`build/_lambda_config.json`** before this command (optional) bundles **`config.json`** into the zip; the inner script copies it when present.

### After building — quick sanity checks

Expand **`build/function.zip`** locally and confirm the **root** of the archive contains **`multiestate_collector.py`**, folders like **`httpx`**, **`pydantic`**, **`pydantic_core`**, **`boto3`**, etc. There must **not** be a single top-level folder wrapping everything (Lambda expects the handler module at the zip root).

### Match Lambda settings

In the Lambda console, set **Runtime** (e.g. Python **3.12**) and **Architecture** (**x86_64** vs **arm64**) to match the image you used when building **`function.zip`**.

---

## 4. Create the Lambda function (console)

1. Open **AWS Lambda** → **Create function**.
2. **Author from scratch.**  
   - **Function name:** for example `multiestate-collector`.  
   - **Runtime:** **Python 3.12** or **Python 3.11** (match what you use locally).  
   - **Architecture:** **x86_64** unless you intentionally build for **arm64**.  
   - Expand **Change default execution role** → **Use an existing role** → select **`multiestate-collector-lambda`** (or the role name you created).
3. **Create function.**

### 4.1 Upload code

1. Open the function → **Code** tab.
2. **Upload from** → **.zip file** → upload **`build/function.zip`** → **Save**.

### 4.2 Runtime settings

1. **Configuration** → **Runtime settings** → **Edit**.  
2. **Handler:** `multiestate_collector.lambda_handler`  
3. Save.

### 4.3 Environment variables

1. **Configuration** → **Environment variables** → **Edit** → **Add environment variable**.  
2. **Option A (file in zip):** Add **`CONFIG_PATH`** = **`/var/task/config.json`** (or **`MULTIESTATE_CONFIG`** with the same value). Rebuild the deployment zip with **`.\scripts\package-lambda.ps1 -ConfigPath .\your-config.json`** so that file exists on Lambda.  
3. **Option B (no file):** Add **`MULTIESTATE_CONFIG_JSON`** with the **entire** config as a single-line JSON string (keep under combined Lambda env size limits, typically 4 KB for the full env block). **Remove** **`CONFIG_PATH`** if you use this, so the handler does not look for a missing file.  
4. Optionally add **`MULTIESTATE_TAEGIS__CLIENT_SECRET`** (and other **`MULTIESTATE_*`** keys) to override fields from the JSON. Save.

### 4.4 Memory and timeout

1. **Configuration** → **General configuration** → **Edit**.  
2. **Timeout:** One invocation runs a **full** cycle (Sophos → batch → Taegis). Start with **5 minutes (300 s)** and increase toward **15 minutes (900 s)** if CloudWatch shows timeouts.  
3. **Memory:** Start with **512 MB**; raise if **Duration** is high or you need more CPU (Lambda scales CPU with memory).

Save. Then **Deploy** if the console shows unsaved code changes.

---

## 5. Schedule the function (EventBridge — console)

Each scheduled run should execute **one** cycle. Match the rule’s interval to **`poll_interval_seconds`** in your config (for example hourly → **rate(1 hour)**).

1. Open **Amazon EventBridge** → **Rules** → **Create rule**.
2. **Name:** for example `multiestate-collector-hourly`.  
3. **Rule type:** **Schedule**.  
4. **Schedule pattern:**  
   - **A schedule that runs at a regular rate**, e.g. every **1 hour**, **or**  
   - **Cron expression** for finer control (times are **UTC**).
5. **Next** → **Select targets** → **Target type:** **AWS service** → **Lambda function** → choose **`multiestate-collector`**.
6. Expand **Additional settings**. Under **Configure target**:  
   - **Configure version / alias:** leave **$LATEST** unless you use aliases.  
   - **Configure execution role:** EventBridge may create or reuse a role to invoke Lambda — accept the default prompt if offered.
7. **Configure input:** choose **Constant (JSON text)** and enter:

   `{"config_path":"/var/task/config.json"}`

   (Use the same path as your packaged **`config.json`**. If you rely only on **`CONFIG_PATH`** env and do not read **`event["config_path"]`**, you can use `{}`.)

8. **Next** → review → **Create rule**.  
9. If the console asks to **add permission** for EventBridge to invoke your Lambda, **approve** it.

To confirm the rule: **EventBridge** → **Rules** → select the rule → verify **State** is **Enabled** and the target ARN points at your function.

---

## 6. Monitor and troubleshoot (console)

### 6.1 CloudWatch Logs

1. Open **CloudWatch** → **Log groups**.  
2. Open **`/aws/lambda/multiestate-collector`** (name matches your function).  
3. Open the latest **log stream** after a scheduled run.

Search the log messages for:

- **`lambda_handler_failed`** — uncaught exception; stack trace should follow.  
- **`cycle_failed`** — cycle-level failure.  
- **`ERROR`** — matches your configured **`log_level`**.

**Logs Insights:** **CloudWatch** → **Logs Insights** → select the Lambda log group → run a query such as filtering for `ERROR` over the last day.

If **no log group** appears after an invocation, confirm the role includes **`AWSLambdaBasicExecutionRole`** and that the function actually ran (**Lambda** → **Monitor** → **Invocations**).

### 6.2 Lambda metrics and alarms

1. **Lambda** → your function → **Monitor** tab — charts for **Invocations**, **Duration**, **Errors**, **Throttles**.  
2. **CloudWatch** → **Alarms** → **Create alarm** → choose **Lambda** metrics → alarm on **Errors ≥ 1** or **Duration** near your timeout.

| Metric | What it tells you |
|--------|-------------------|
| **Invocations** | Schedule (or manual test) is firing. |
| **Errors** | Exceptions or failed handler execution. |
| **Duration** | Compare to **Timeout** in configuration. |
| **Throttles** | Concurrency limits; rare for a single scheduled job unless the account is constrained. |

### 6.3 S3 health and spool (console)

1. **S3** → your bucket → browse under your prefix.  
2. Check **`…/state/health.json`** — timestamps for Sophos pulls and Taegis upload, **spool_depth**.  
3. **`…/state/cursors/`** — cursor JSON per estate.  
4. **`…/spool/`** — `*.log` batches. Growth without shrinking after runs suggests Taegis upload or credential issues; correlate with CloudWatch errors.

### 6.4 Common issues

| Symptom | What to check in the console |
|---------|------------------------------|
| **`ImportModuleError`** / **`pydantic_core._pydantic_core`** | Rebuild **`function.zip`** inside Docker (`scripts/package-lambda.ps1` or **`public.ecr.aws/lambda/python`**). Do not use Windows **`pip install -t`** for the Lambda artifact. Match **Runtime** and **Architecture** to the build image. |
| **`ImportModuleError`** / **`httpx`** (or other deps missing) | Same as above: zip root must include dependency folders from **`pip install -t`**. Re-run the packaging script; confirm **`Compress-Archive`** / **`zip`** adds **`package\*`** contents at the **top level** of **`function.zip`**, not nested under another folder. |
| Handler returns error about **missing** or **not found** config | Set **`CONFIG_PATH=/var/task/config.json`** only after bundling **`config.json`** in the zip (`.\scripts\package-lambda.ps1 -ConfigPath .\your-config.json`), **or** use **`MULTIESTATE_CONFIG_JSON`** and remove **`CONFIG_PATH`**, **or** set EventBridge **Constant JSON** `{"config_path":"/var/task/config.json"}` with a file that exists in the zip. |
| Error about **`s3`** in the response | Edit **`config.json`** (re-upload zip) or env overrides so **`s3.bucket`** (and **`prefix`**) is present. |
| **AccessDenied** on **`ListObjectsV2`** / **`s3:ListBucket`** | The execution role must allow **`s3:ListBucket`** on **`arn:aws:s3:::your-bucket-name`** (bucket ARN, not `/*`). Add or fix the inline policy (see §2.2 Option A). Prefix **`Condition`** keys are easy to get wrong—use Option A if unsure. |
| **AccessDenied** on S3 objects | IAM **`Resource`** for objects must include **`arn:aws:s3:::bucket/prefix*`** (or **`…/*`**). Verify **`s3.prefix`** in config matches your policy. |
| **Task timed out** after **Duration** ≈ timeout | **Lambda** → **Configuration** → increase **Timeout**; tune **`max_pages_per_estate_per_cycle`** in config; consider splitting estates across functions. |
| Network / SSL errors to Sophos or Taegis | Default Lambda has internet access when **not** in a VPC. If you attached a **VPC**, ensure **NAT Gateway** or endpoints allow HTTPS egress. |
| Works on PC with **`python multiestate_collector.py --config … --lambda`**, fails in Lambda | Same **Region**, **IAM**, and **`s3`** settings; **zip** layout (handler at top level); **Handler** string exactly **`multiestate_collector.lambda_handler`**. |

### 6.5 Manual test (console)

**Lambda** → **Test** tab → create an event with JSON **`{"config_path":"/var/task/config.json"}`** (or `{}` if using env only) → **Test**. Inspect **Execution result** and CloudWatch Logs.

---

## Quick reference

| Item | Value |
|------|--------|
| Handler | `multiestate_collector.lambda_handler` |
| Config | **`MULTIESTATE_CONFIG_JSON`**, or **`CONFIG_PATH`** / **`MULTIESTATE_CONFIG`**, or **`event["config_path"]`** |
| Config JSON | Must include **`s3`** (`bucket`, optional `prefix`, optional `region`) |
| Config inside zip | Often **`/var/task/config.json`** |

**Local test (Windows PowerShell, after venv activate):**

```powershell
python multiestate_collector.py --config path\to\config-with-s3.json --lambda
```

**Local test (Linux/macOS):**

```bash
python multiestate_collector.py --config path/to/config-with-s3.json --lambda
```
