$ErrorActionPreference = 'Stop'
$b = '__SYMPHONY_BOOTSTRAP__'
$v = '__SYMPHONY_PROVIDER__'
$root = [Environment]::GetEnvironmentVariable($(if ($v -eq 'codex') { 'PLUGIN_ROOT' } else { 'CLAUDE_PLUGIN_ROOT' }))
$py = $null
$why = 'No Python executable found'
$until = [DateTime]::UtcNow.AddSeconds(2)
foreach ($c in @(Get-Command python.exe,python3.exe,py.exe -All -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 6)) {
if ([DateTime]::UtcNow -ge $until) { break }
try {
$a = '-I -c "import sys;assert sys.version_info>=(3,10);print(sys.executable)"'
if ($c.Name -eq 'py.exe') { $a = '-3 ' + $a }
$q = [Diagnostics.ProcessStartInfo]::new($c.Source, $a)
$q.UseShellExecute = $false
$q.RedirectStandardInput = $true
$q.RedirectStandardOutput = $true
$q.RedirectStandardError = $true
$p = [Diagnostics.Process]::Start($q)
$p.StandardInput.Close()
if (-not $p.WaitForExit(700)) { $p.Kill(); $why = $c.Source + ': probe timed out'; continue }
$path = $p.StandardOutput.ReadToEnd().Trim()
$errorText = $p.StandardError.ReadToEnd().Trim()
if ($p.ExitCode -eq 0 -and [IO.Path]::IsPathRooted($path) -and (Test-Path -LiteralPath $path -PathType Leaf)) { $py = $path; break }
$why = $c.Source + ': probe exit ' + $p.ExitCode + ' ' + $errorText.Substring(0,[Math]::Min(160,$errorText.Length))
} catch { $why = $c.Source + ': ' + $_.Exception.Message; continue }
}
if (-not $py) { [Console]::Error.WriteLine('Symphony pending activation: no working Python 3.10+ on PATH; ' + $why); exit 1 }
$i = [Diagnostics.ProcessStartInfo]::new($py, '-I -c "' + $b + '" "' + $root + '" ' + $v)
$i.UseShellExecute = $false
$i.RedirectStandardInput = $true
$i.RedirectStandardOutput = $true
$i.RedirectStandardError = $true
try {
$p = [Diagnostics.Process]::Start($i)
} catch {
[Console]::Error.WriteLine('Symphony launcher could not start ' + $py + ': ' + $_.Exception.Message)
exit 1
}
$out = $p.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
$err = $p.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
[Console]::OpenStandardInput().CopyTo($p.StandardInput.BaseStream)
$p.StandardInput.BaseStream.Close()
$p.WaitForExit()
[Threading.Tasks.Task]::WaitAll(@($out, $err))
exit $p.ExitCode
