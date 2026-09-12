param(
    [string]$PythonExe = "python",
    [string]$LegacyDir = "",
    [string]$LegacyModule = "hp60c_td_bridge_v7",
    [switch]$Mock
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Mock) {
    & $PythonExe "$Root\hp60c_supervisor_v13.py" `
        --mode mock `
        --mock-fps 30
    exit $LASTEXITCODE
}

if ([string]::IsNullOrWhiteSpace($LegacyDir)) {
    $LegacyDir = Join-Path `
        (Split-Path -Parent $Root) `
        "hp60c_ndi_sender"
}

& $PythonExe "$Root\hp60c_supervisor_v13.py" `
    --mode legacy `
    --legacy-dir "$LegacyDir" `
    --legacy-module "$LegacyModule"

exit $LASTEXITCODE
