param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$dotenvPath = Join-Path $repoRoot ".env"
if (Test-Path -LiteralPath $dotenvPath) {
    $lineNumber = 0
    foreach ($rawLine in Get-Content -LiteralPath $dotenvPath -Encoding UTF8) {
        $lineNumber++
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) { continue }
        if ($line.StartsWith("export ")) { $line = $line.Substring(7).TrimStart() }
        $separator = $line.IndexOf("=")
        if ($separator -lt 1) { throw "Invalid .env entry at line $lineNumber" }
        $name = $line.Substring(0, $separator).Trim()
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "Invalid .env name at line $lineNumber" }
        $value = $line.Substring($separator + 1).Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($name, "Process"))) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

$userEnvironmentNames = @(
    "AGENT_MODEL_API_KEY",
    "AGENT_MODEL_BASE_URL",
    "AGENT_MODEL_ID",
    "AGENT_MODEL_PROVIDER",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "AGENT_FALLBACK_MODEL_API_KEY",
    "AGENT_FALLBACK_MODEL_BASE_URL",
    "AGENT_FALLBACK_MODEL_ID",
    "AGENT_FALLBACK_MODEL_PROVIDER_NAME",
    "AGENT_FALLBACK_MODEL_CAPABILITIES",
    "EMBEDDING_API_KEY_ENV",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS"
)
foreach ($name in $userEnvironmentNames) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, "Process"))) {
        $userValue = [Environment]::GetEnvironmentVariable($name, "User")
        if (-not [string]::IsNullOrWhiteSpace($userValue)) {
            [Environment]::SetEnvironmentVariable($name, $userValue, "Process")
        }
    }
}

$llmApPath = $env:LLM_AP_PATH
if ([string]::IsNullOrWhiteSpace($llmApPath)) {
    $llmApPath = [Environment]::GetEnvironmentVariable("LLM_AP_PATH", "User")
}
$hasModelEnvironment = -not [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY) -or -not [string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY) -or -not [string]::IsNullOrWhiteSpace($env:ANTHROPIC_API_KEY) -or -not [string]::IsNullOrWhiteSpace($env:AGENT_MODEL_BASE_URL)
if (-not $hasModelEnvironment -and -not [string]::IsNullOrWhiteSpace($llmApPath) -and -not (Test-Path -LiteralPath $llmApPath)) {
    throw "LLM_AP_PATH points to a missing model configuration file"
}

$databaseUrl = $env:DATABASE_URL
if ([string]::IsNullOrWhiteSpace($databaseUrl)) {
    $databaseUrl = [Environment]::GetEnvironmentVariable("DATABASE_URL", "User")
}
if ([string]::IsNullOrWhiteSpace($databaseUrl)) {
    $databaseUrl = "postgresql://better_agent:change-this-local-password@127.0.0.1:5432/better_agent"
}

if (-not [string]::IsNullOrWhiteSpace($llmApPath)) {
    $env:LLM_AP_PATH = $llmApPath
}
$env:DATABASE_URL = $databaseUrl
$env:PORT = [string]$Port
$env:BETTER_AGENT_HOST = "127.0.0.1"
if ([string]::IsNullOrWhiteSpace($env:AGENT_MODEL_CAPABILITIES)) {
    $env:AGENT_MODEL_CAPABILITIES = "streaming,tool_calling,json_object"
}
if ([string]::IsNullOrWhiteSpace($env:AGENT_FALLBACK_MODEL_CAPABILITIES)) {
    $env:AGENT_FALLBACK_MODEL_CAPABILITIES = "streaming,tool_calling,json_object"
}

Push-Location $repoRoot
try {
    docker compose up -d postgres
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL startup failed" }
    Push-Location (Join-Path $repoRoot "backend")
    try {
        python -m alembic -c alembic.ini upgrade head
        if ($LASTEXITCODE -ne 0) { throw "Database migration failed; application was not started" }
    }
    finally {
        Pop-Location
    }
    python scripts/start.py
}
finally {
    Pop-Location
}
