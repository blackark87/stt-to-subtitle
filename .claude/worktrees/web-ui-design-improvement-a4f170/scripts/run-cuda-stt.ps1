param(
    [string]$EnvFile,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $EnvFile = Join-Path $ProjectDir ".env.cuda"
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $ProjectDir ".venv-cuda\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "CUDA STT environment file was not found: $EnvFile"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "CUDA STT Python was not found: $PythonPath"
}

foreach ($Line in Get-Content -LiteralPath $EnvFile) {
    $Trimmed = $Line.Trim()
    if (-not $Trimmed -or $Trimmed.StartsWith("#")) {
        continue
    }
    $Parts = $Trimmed.Split("=", 2)
    if ($Parts.Count -ne 2) {
        throw "Invalid environment line: $Line"
    }
    [Environment]::SetEnvironmentVariable(
        $Parts[0].Trim(),
        $Parts[1].Trim(),
        "Process"
    )
}

if ([string]::IsNullOrWhiteSpace($env:HF_TOKEN)) {
    throw "HF_TOKEN must be set"
}

if (-not $env:HF_HOME) {
    $env:HF_HOME = Join-Path $ProjectDir "var\cuda-cache\huggingface"
}
if (-not $env:PYANNOTE_CACHE) {
    $env:PYANNOTE_CACHE = Join-Path $ProjectDir "var\cuda-cache\pyannote"
}
if (-not $env:TORCH_HOME) {
    $env:TORCH_HOME = Join-Path $ProjectDir "var\cuda-cache\torch"
}
if (-not $env:STT_STATE_DIR) {
    $env:STT_STATE_DIR = Join-Path $ProjectDir "var\cuda-stt"
}
$env:PYTHONPATH = if ($env:PYTHONPATH) {
    "$ProjectDir\src$([IO.Path]::PathSeparator)$env:PYTHONPATH"
} else {
    "$ProjectDir\src"
}

New-Item -ItemType Directory -Force -Path @(
    $env:HF_HOME,
    $env:PYANNOTE_CACHE,
    $env:TORCH_HOME,
    $env:STT_STATE_DIR
) | Out-Null

Push-Location $ProjectDir
try {
    & $PythonPath -m stt_to_subtitle.macos_api
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
