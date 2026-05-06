#!/usr/bin/env bash
# Run inside public.ecr.aws/lambda/python (Linux) — invoked by package-lambda.ps1 / package-lambda.sh
set -euo pipefail
cd /workspace

# Use a fresh directory each run so we never have to `rm -rf` a prior bind-mounted
# `pip -t` tree (Docker Desktop on Windows often denies delete on those files).
STAMP=$(date +%s)
PKG="build/package-${STAMP}"

rm -f build/function.zip
mkdir -p "$PKG"

pip install -r requirements.txt -t "$PKG" --upgrade --no-cache-dir
cp multiestate_collector.py "$PKG/"

if [[ -f build/_lambda_config.json ]]; then
  cp build/_lambda_config.json "$PKG/config.json"
fi

python <<PY
import zipfile
from pathlib import Path

root = Path("${PKG}")
out = Path("build/function.zip")
out.unlink(missing_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for p in root.rglob("*"):
        if p.is_file():
            z.write(p, p.relative_to(root))
print("Created", out.resolve())
PY
