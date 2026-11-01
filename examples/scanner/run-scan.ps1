param(
    [Parameter(Mandatory = $true)][string]$Repository,
    [string]$Watchlist = "examples\scanner\watchlist.json"
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $Repository
$Uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if (-not (Test-Path -LiteralPath $Uv)) {
    throw "uv.exe was not found at $Uv; set its installed absolute path in this script."
}
if ($Watchlist.Contains('"')) { throw "The watchlist path cannot contain a quote." }
$LogDirectory = Join-Path $Repository "data"
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$Log = Join-Path $LogDirectory "scanner-task.log"
$Info = New-Object System.Diagnostics.ProcessStartInfo
$Info.FileName = $Uv
$Info.Arguments = 'run --locked python -m crow_trading.scanner "' + $Watchlist + '"'
$Info.WorkingDirectory = (Get-Location).Path
$Info.UseShellExecute = $false
$Info.CreateNoWindow = $true
$Info.RedirectStandardOutput = $true
$Info.RedirectStandardError = $true
$Process = New-Object System.Diagnostics.Process
$Process.StartInfo = $Info
try {
    # Read both streams concurrently. Native stderr warnings must not become
    # PowerShell terminating errors or obscure the scanner's actual exit code.
    $null = $Process.Start()
    $Output = $Process.StandardOutput.ReadToEndAsync()
    $Errors = $Process.StandardError.ReadToEndAsync()
    $Process.WaitForExit()
    $ScanExit = $Process.ExitCode
    Add-Content -LiteralPath $Log -Encoding UTF8 -Value ([DateTime]::UtcNow.ToString("o"))
    Add-Content -LiteralPath $Log -Encoding UTF8 -Value $Output.Result
    if ($Errors.Result) { Add-Content -LiteralPath $Log -Encoding UTF8 -Value $Errors.Result }
} finally {
    $Process.Dispose()
}
# This file starts one scan only. Activate a task only by explicit user request.
exit $ScanExit
