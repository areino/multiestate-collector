# Multiestate Collector

Middleware-style collector: **many Sophos Central SIEM estates** → **one Taegis XDR tenant** via JSONL batches and the [Taegis File Upload API](https://docs.taegis.secureworks.com/apis/using_file_upload_api/).

Everything ships as a **single Python script**: `multiestate_collector.py` (plus `examples/config.json`, `requirements.txt`, and tests).

References: [Sophos SIEM events](https://developer.sophos.com/docs/siem-v1/1/routes/events/get), [Taegis rate limits](https://docs.taegis.secureworks.com/apis/using_xdr_apis/).

---

## Requirements

- Python **3.11+**
- Dependencies: `pip install -r requirements.txt` (`httpx`, `pydantic`, `boto3` for S3 / Lambda)

---

## Run

You must pass **exactly one** of `--once`, `--loop`, or `--lambda`.

```bash
cd C:\git\multiestate-collector
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python multiestate_collector.py --config examples\config.json --once
```

Daemon mode (sleeps `poll_interval_seconds` from JSON between cycles; **local** `state_dir` / `spool_dir`):

```bash
python multiestate_collector.py --config examples\config.json --loop
```

Single cycle with **S3** for state and spool (same code path as Lambda; config must include `s3`):

```bash
python multiestate_collector.py --config path\to\config-with-s3.json --lambda
```

---

## AWS Lambda

- **Handler:** `multiestate_collector.lambda_handler`
- **Config:** optional env **`MULTIESTATE_CONFIG_JSON`** (full JSON), **or** **`event["config_path"]`** / **`MULTIESTATE_CONFIG`** / **`CONFIG_PATH`** for a file path on Lambda (e.g. **`config.json`** packaged with `scripts/package-lambda.ps1 -ConfigPath …`).
- **Persistence:** the config **must** include an **`s3`** object (`bucket`, optional `prefix`, optional `region`). Cursors, circuit breakers, `health.json`, and pending JSONL batches are stored under that bucket; `state_dir` / `spool_dir` are not used when `use_s3` is true.

**S3 key layout** (prefix is normalized to a single trailing slash; default in schema is `multiestate/`):

| Purpose        | Key pattern                                      |
|----------------|--------------------------------------------------|
| Cursor         | `{prefix}state/cursors/{estate_key}.json`      |
| Circuit breaker| `{prefix}state/breakers/{estate_key}.json`     |
| Health         | `{prefix}state/health.json`                      |
| Spool batches  | `{prefix}spool/*.log`                            |

Grant the execution role **`s3:GetObject`**, **`s3:PutObject`**, **`s3:DeleteObject`**, and **`s3:ListBucket`** (scoped to `arn:aws:s3:::bucket-name` and `arn:aws:s3:::bucket-name/{prefix}*` as appropriate). See `examples/lambda-config.example.json` for a full JSON shape including `s3`.

---

## Configuration

Edit `examples/config.json` (or copy it). For Lambda or `--lambda`, add **`s3`** and see `examples/lambda-config.example.json`. Environment overrides use `MULTIESTATE_*` and nested `MULTIESTATE_PARENT__CHILD`, same as before (see inline `load_config` in `multiestate_collector.py`).

Per-event metadata for Taegis parsers: **`estate_name`**, **`estate_tenant_id`**, **`pulled_at`**.

---

## State & health

**Local (`--once` / `--loop`):**

- `state_dir`: cursors + per-estate circuit breaker JSON
- `spool_dir`: batched events (JSON lines in plain-text files, `*.log`) pending Taegis upload
- `state_dir/health.json`: last successful Sophos pull per tenant, last Taegis upload, spool depth

**S3 (`--lambda` / `lambda_handler`):** same logical data under `{prefix}state/…` and `{prefix}spool/…` in the configured bucket (see table above).

---

## Operations

Host the script with a process supervisor (systemd, Windows Service, Nomad, etc.). For local runs, persist `state_dir` and `spool_dir` on disk. For Lambda, rely on S3 durability for state and spool. Keep secrets out of git (inject via env or secret store).

If Sophos volume risks exceeding the **~24h SIEM window**, lower `poll_interval_seconds` or tune `max_pages_per_estate_per_cycle` / concurrency carefully.

### Taegis presign `400 Bad Request`

The [File Upload API](https://docs.taegis.secureworks.com/apis/using_file_upload_api/) documents only `file_name`, `content_length`, and optional `sensor_id` on `POST …/s3-signer/v2/signed-s3url`. This script therefore **omits** optional `service` / `sensor_id` query parameters unless you set them in `taegis` in JSON. Spool files use a **`.log`** suffix (content is still JSON lines) because some stacks reject presign for non–plain-text extensions.

If presign still fails, the error line now includes the **response body** from Taegis. You can try `sensor_id` like `yourname.localhost`, or set `service` only if your tenant documentation requires a specific value.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

---

## License

MIT — see `LICENSE`.
