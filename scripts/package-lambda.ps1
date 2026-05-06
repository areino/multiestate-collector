<#
.SYNOPSIS
  Build build/function.zip for AWS Lambda using the official Lambda Python container.

  pydantic_core ships native binaries — always build inside the Lambda Linux image.
  Do not use `pip install -t` on Windows for the artifact you upload to Lambda.

.PARAMETER Runtime
  Lambda Python tag (image public.ecr.aws/lambda/python:<Runtime>).

.PARAMETER Arm64
  Use the arm64 Lambda image. Match Lambda console Architecture (arm64).

.PARAMETER ConfigPath
  Optional path to config JSON to include as config.json in the zip.
#>
param(
    [string]$Runtime = "3.12",
    [switch]$Arm64,
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$image = if ($Arm64) {
    "public.ecr.aws/lambda/python:${Runtime}-arm64"
} else {
    "public.ecr.aws/lambda/python:${Runtime}"
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Docker CLI not found. Install Docker Desktop and ensure 'docker' is on PATH."
}
& docker info 1>$null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker is not running or not accessible. Start Docker Desktop and retry."
}

Write-Host "Image: $image"
Write-Host "Repo:  $RepoRoot"

New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "build") | Out-Null

if ($ConfigPath) {
    $cfg = Resolve-Path $ConfigPath
    Copy-Item -LiteralPath $cfg -Destination (Join-Path $RepoRoot "build\_lambda_config.json") -Force
    Write-Host "Staged config for zip: $cfg"
}

docker run --rm `
    --entrypoint /bin/bash `
    -v "${RepoRoot}:/workspace" `
    -w /workspace `
    $image `
    /workspace/scripts/docker-pack-inner.sh

if (-not (Test-Path (Join-Path $RepoRoot "build\function.zip"))) {
    Write-Error "build/function.zip was not created. See Docker output above."
}

Write-Host ""
Write-Host "Created: $(Join-Path $RepoRoot 'build\function.zip')"
Write-Host "Lambda console: Runtime Python $Runtime, Architecture $(if ($Arm64) { 'arm64' } else { 'x86_64' }), Handler multiestate_collector.lambda_handler"
