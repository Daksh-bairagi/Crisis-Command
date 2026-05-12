#!/usr/bin/env pwsh

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Host "Python executable not found at $PythonExe" -ForegroundColor Red
    exit 1
}

function Start-McpServerWindow {
    param(
        [string]$ScriptPath
    )

    $command = @"
Set-Location '$RepoRoot'
`$env:ENABLE_EXTERNAL_ACTIONS='true'
`$env:MCP_TRANSPORT='sse'
& '$PythonExe' '$ScriptPath'
"@

    Start-Process powershell -ArgumentList "-NoExit", "-Command", $command
}

Start-McpServerWindow "agents\chat_agent\run_sse.py"
Start-McpServerWindow "agents\docs_agent\run_sse.py"
Start-McpServerWindow "agents\calendar_agent\run_sse.py"
Start-McpServerWindow "agents\github_agent\run_sse.py"
Start-McpServerWindow "agents\logging_agent\run_sse.py"

$env:ENABLE_EXTERNAL_ACTIONS = "true"
$env:MCP_TRANSPORT = "sse"
Set-Location $RepoRoot
& $PythonExe -m uvicorn webhook.main:app --host 127.0.0.1 --port 8000 --reload
