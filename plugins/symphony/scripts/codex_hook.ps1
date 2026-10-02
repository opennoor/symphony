$ErrorActionPreference = 'Stop'
$b = '__SYMPHONY_BOOTSTRAP__'
$v = '__SYMPHONY_PROVIDER__'
$root = [Environment]::GetEnvironmentVariable($(if ($v -eq 'codex') { 'PLUGIN_ROOT' } else { 'CLAUDE_PLUGIN_ROOT' }))
$py = $null
$why = 'No Python executable found'
$began = [DateTime]::UtcNow
$discover = {[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); foreach ($n in 0,1) { foreach ($name in 'python.exe','python3.exe','py.exe') { Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue | Select-Object -Skip $n -First 1 | Select-Object Name,Source | ConvertTo-Json -Compress } }}.ToString()
$cs = @()
$p = $null
try {
$q = [Diagnostics.ProcessStartInfo]::new([Diagnostics.Process]::GetCurrentProcess().MainModule.FileName, '-NoProfile -NonInteractive -Command "' + $discover + '"')
$q.UseShellExecute = $false
$q.RedirectStandardOutput = $true
$q.StandardOutputEncoding = [Text.UTF8Encoding]::new()
$p = [Diagnostics.Process]::Start($q)
$out = $p.StandardOutput.ReadToEndAsync()
if (-not $p.WaitForExit(3000)) { $why = 'Python discovery timed out'; $p.Kill(); [void]$p.WaitForExit(250) }
if ($out.Wait(250)) { foreach ($line in $out.Result -split '\r?\n') { try { $c = ConvertFrom-Json $line; if ($c.Name -in 'python.exe','python3.exe','py.exe' -and $c.Source -is [string]) { $cs += $c } } catch {} } }
} catch { $why = 'Python discovery failed: ' + $_.Exception.Message } finally { if ($p) { $p.Dispose() } }
$enumerationMs = [int]([DateTime]::UtcNow - $began).TotalMilliseconds
$until = [DateTime]::UtcNow.AddSeconds(4)
$attempts = 0
foreach ($c in $cs) {
if ([DateTime]::UtcNow -ge $until) { break }
$attempts++
$p = $null
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
if (-not $p.WaitForExit(1500)) { $why = $c.Source + ': probe timed out'; try { $p.Kill() } catch {}; continue }
$path = $p.StandardOutput.ReadToEnd().Trim()
$errorText = $p.StandardError.ReadToEnd().Trim()
if ($p.ExitCode -eq 0 -and [IO.Path]::IsPathRooted($path) -and (Test-Path -LiteralPath $path -PathType Leaf)) { $py = $path; break }
$why = $c.Source + ': probe exit ' + $p.ExitCode + ' ' + $errorText.Substring(0,[Math]::Min(160,$errorText.Length))
} catch { $why = $c.Source + ': ' + $_.Exception.Message; continue } finally { if ($p) { $p.Dispose() } }
}
if (-not $py) { [Console]::Error.WriteLine('Symphony pending activation: no working Python 3.10+ on PATH; candidates=' + $cs.Count + ' attempted=' + $attempts + ' enumeration_ms=' + $enumerationMs + ' elapsed_ms=' + [int]([DateTime]::UtcNow - $began).TotalMilliseconds + '; ' + $why); exit 1 }
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
