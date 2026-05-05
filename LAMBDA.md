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

**Config file:** The handler loads JSON from `event["config_path"]` **or** environment variables **`MULTIESTATE_CONFIG`** or **`CONFIG_PATH`**. The file **must** include an **`s3`** object (`bucket`, optional `prefix`, optional `region`). You may overlay secrets with **`MULTIESTATE_*`** env vars (nested keys use `__`; see `load_config` in `multiestate_collector.py`).

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

1. Open the role you just created → **Permissions** tab → **Add permissions** → **Create inline policy**.
2. Choose the **JSON** tab and paste the policy below.
3. Replace **`YOUR-BUCKET-NAME`** with your S3 bucket name.
4. Replace **`YOUR-PREFIX`** with the prefix segment used in keys (no leading `/`; match what you set in **`s3.prefix`**, for example `multiestate/prod/`). The `*` after the prefix matches all objects under that path.

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
      "Sid": "ListBucketForPrefix",
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

5. **Next** → Policy name: for example `multiestate-collector-s3` → **Create policy**.

If **`AccessDenied`** appears for **`ListBucket`** in CloudWatch Logs, broaden the second statement (some teams allow **`ListBucket`** on the bucket **without** the prefix condition for simplicity).

---

## 3. Build the deployment package (local — Windows and Linux)

Lambda needs a **.zip** whose **root** contains **`multiestate_collector.py`** and installed dependencies (`httpx`, `pydantic`, `boto3` from `requirements.txt`). The zip must **not** add an extra parent folder above those files when Lambda unpacks it.

**Recommendation:** These libraries are pure Python for typical installs; building on **Windows** often works. If the upload fails at import time or you use extensions that need Linux binaries, build inside **WSL2** (Ubuntu) or a Linux CI job using the same commands as **Linux** below.

Prepare your config file (see `examples/lambda-config.example.json`): include **`s3.bucket`** and **`s3.prefix`** matching the bucket you created. Name it **`config.json`** when placing it in the package folder so you can set **`CONFIG_PATH=/var/task/config.json`** on Lambda.

### Windows (PowerShell)

From your project folder (where `requirements.txt` and `multiestate_collector.py` live):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

New-Item -ItemType Directory -Force -Path build\package | Out-Null
pip install -r requirements.txt -t build\package\
Copy-Item multiestate_collector.py build\package\
Copy-Item path\to\your\config.json build\package\config.json

Compress-Archive -Path build\package\* -DestinationPath build\function.zip -Force
```

- **`Compress-Archive`** puts the **contents** of `package` at the root of the zip (correct for Lambda).
- If execution policy blocks scripts: run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use **cmd** with `venv\Scripts\activate.bat`.

### Linux or macOS (bash)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

mkdir -p build/package
pip install -r requirements.txt -t build/package/
cp multiestate_collector.py build/package/
cp path/to/your/config.json build/package/config.json

(cd build/package && zip -r ../function.zip .)
```

You should now have **`build/function.zip`**.

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
2. Add **`CONFIG_PATH`** = **`/var/task/config.json`** if **`config.json`** is in the zip root (alternatively use **`MULTIESTATE_CONFIG`** with the same value).  
3. Optionally add **`MULTIESTATE_TAEGIS__CLIENT_SECRET`** (and other **`MULTIESTATE_*`** keys) to override secrets without editing the zip. Save.

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
| Handler returns **`Missing config path`** | **Lambda** → **Configuration** → **Environment variables**: **`CONFIG_PATH`** / **`MULTIESTATE_CONFIG`**, or EventBridge target **Constant JSON** with **`config_path`**. Ensure the path matches a file **inside the zip** (typically **`/var/task/config.json`**). |
| Error about **`s3`** in the response | Edit **`config.json`** (re-upload zip) or env overrides so **`s3.bucket`** (and **`prefix`**) is present. |
| **AccessDenied** on S3 | **IAM** → role → inline policy: bucket name, **`Resource`** ARNs, and **`s3:prefix`** condition match real object keys. |
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
| Config | **`CONFIG_PATH`** / **`MULTIESTATE_CONFIG`**, or **`event["config_path"]`** |
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
