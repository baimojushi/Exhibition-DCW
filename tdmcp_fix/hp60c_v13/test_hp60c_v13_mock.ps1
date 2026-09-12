param(
    [string]$PythonExe = "python"
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

& $PythonExe "$Root\hp60c_v13_soak_test.py" `
    --seconds 12 `
    --hang-after-s 3

exit $LASTEXITCODE
