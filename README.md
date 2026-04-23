# Sophos to Taegis Multiestate Collector

Middleware-style collector: **many Sophos Central SIEM estates** → **one Taegis XDR tenant** via JSONL batches and the [Taegis File Upload API](https://docs.taegis.secureworks.com/apis/using_file_upload_api/).

Everything ships as a **single Python script**: `multiestate_collector.py` (plus `config.json`, `requirements.txt`).

References: [Sophos SIEM events](https://developer.sophos.com/docs/siem-v1/1/routes/events/get), [Taegis rate limits](https://docs.taegis.secureworks.com/apis/using_xdr_apis/).

---

## Requirements

- Python **3.11+**
- Dependencies: `pip install -r requirements.txt` (`httpx`, `pydantic`)

---

## Run

```bash
pip install -r requirements.txt
python multiestate_collector.py --config config.json --once
```

Daemon mode (sleeps `poll_interval_seconds` from JSON between cycles):

```bash
python multiestate_collector.py --config examples\config.json --loop
```

---

## Configuration

Edit `examples/config.json` (or copy it). Environment overrides use `MULTIESTATE_*` and nested `MULTIESTATE_PARENT__CHILD`, same as before (see inline `load_config` in `multiestate_collector.py`).

Per-event metadata for Taegis parsers: **`estate_name`**, **`estate_tenant_id`**, **`pulled_at`**.

---

## State & health

- `state_dir`: cursors + per-estate circuit breaker JSON
- `spool_dir`: batched events (JSON lines in plain-text files, `*.log`) pending Taegis upload
- `state_dir/health.json`: last successful Sophos pull per tenant, last Taegis upload, spool depth

---

## Operations

Host the script with a process supervisor (systemd, Windows Service, Nomad, etc.). Persist `state_dir` and `spool_dir` on disk. Keep secrets out of git (inject via env or secret store).

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
