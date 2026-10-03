$ErrorActionPreference='Stop'
$b = '__SYMPHONY_BOOTSTRAP__'
$v = '__SYMPHONY_PROVIDER__'
$root=$env:PLUGIN_ROOT;if($v -eq 'claude'){$root=$env:CLAUDE_PLUGIN_ROOT}
$py=$null
$w='No candidates'
function N($d) {$d=$d.TrimEnd('\','/');if($d -match '^[a-z]:$') {$d+='\'};$d}
$ds=@($env:PATH -split [IO.Path]::PathSeparator | ForEach-Object {$d=$_.Trim();if($d.StartsWith('"') -and $d.EndsWith('"')) {$d=$d.Trim('"')};if([IO.Path]::IsPathRooted($d) -and ($d -match '^(?:[a-z]:[\\/]|\\\\[^\\]+\\[^\\]+)' -or [IO.Path]::DirectorySeparatorChar -eq '/') -and $d -notmatch '["\x00-\x1f]') {N $d}})
$ds=@($ds | Where-Object {$_ -notlike '\\*'})+@($ds | Where-Object {$_ -like '\\*'})
$ps=@();foreach($d in $ds) {if(!$ps -or $ps[-1].Length+$d.Length -gt 3000) {$ps+=''};$ps[-1]+=$d+';'}
$all=@();$seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase);$end=[DateTime]::UtcNow.AddSeconds(3)
foreach($part in $ps) {
$p=$null
try {
$q=[Diagnostics.ProcessStartInfo]::new($env:SystemRoot+'\System32\cmd.exe','/d /u /v:off /c for %d in ("%SYMPHONY_PY_PATH:;=" "%") do @for %n in (python.exe python3.exe py.exe) do @if exist "%~d\%n" @echo "%~d\%n"')
$q.EnvironmentVariables['SYMPHONY_PY_PATH']=$part.TrimEnd(';')
$q.UseShellExecute=$false;$q.RedirectStandardOutput=$true;$q.StandardOutputEncoding=[Text.Encoding]::Unicode
$ms=[int]($end-[DateTime]::UtcNow).TotalMilliseconds;if($ms -le 0) {break}
$p=[Diagnostics.Process]::Start($q);$out=$p.StandardOutput.ReadToEndAsync()
if(-not $p.WaitForExit($ms)) {$w='Python discovery timed out';$p.Kill();[void]$p.WaitForExit(250)}
if($out.Wait(250)) {foreach($line in $out.Result -split '\r?\n') {if($line -match '^"([^"\r\n]+)"$') {$src=$Matches[1];if($ds -contains (N ([IO.Path]::GetDirectoryName($src))) -and $seen.Add($src)) {$all += [pscustomobject]@{Name=[IO.Path]::GetFileName($src);Source=$src}}}}}
} catch {$w='Python discovery failed: '+$_.Exception.Message} finally {if($p) {$p.Dispose()}}
}
$end=[DateTime]::UtcNow.AddSeconds(4)
$cs=$all|Group-Object Name -AsHashTable
:probe for($n=0;$n -lt $all.Count;$n++){foreach($name in 'python.exe','python3.exe','py.exe'){$g=$cs[$name];if($n -ge $g.Count){continue};$c=$g[$n]
$ms=[int]($end-[DateTime]::UtcNow).TotalMilliseconds;if($ms -le 0){break probe}
$p=$null
try {
$a='-I -X utf8 -c "import sys;assert sys.version_info>=(3,10);print(sys.executable)"'
if($c.Name -eq 'py.exe') {$a='-3 '+$a}
$q=[Diagnostics.ProcessStartInfo]::new($c.Source,$a)
$q.UseShellExecute=$false
$q.StandardOutputEncoding=[Text.Encoding]::UTF8
$q.RedirectStandardInput=$true
$q.RedirectStandardOutput=$true
$q.RedirectStandardError=$true
$p=[Diagnostics.Process]::Start($q)
$p.StandardInput.Close()
if(-not $p.WaitForExit([Math]::Min(2500,$ms))) {$w=$c.Source+': probe timed out';try {$p.Kill()} catch {};continue}
$path=$p.StandardOutput.ReadToEnd().Trim()
if($p.ExitCode -eq 0 -and [IO.Path]::IsPathRooted($path) -and (Test-Path -LiteralPath $path -PathType Leaf)) {$py=$path;break probe}
$w=$c.Source+': probe exit '+$p.ExitCode
} catch {$w=$c.Source+': '+$_.Exception.Message;continue} finally {if($p) {$p.Dispose()}}
}}
if(-not $py) {[Console]::Error.WriteLine('Symphony pending activation: no working Python 3.10+ on PATH;'+$w);exit 1}
$i=[Diagnostics.ProcessStartInfo]::new($py,'-I -X utf8 -c "'+$b+'" "'+$root+'" '+$v)
$i.UseShellExecute=$false
$i.RedirectStandardInput=$true
$i.RedirectStandardOutput=$true
$i.RedirectStandardError=$true
try {
$p=[Diagnostics.Process]::Start($i)
} catch {
[Console]::Error.WriteLine('Symphony launcher could not start '+$py+': '+$_.Exception.Message)
exit 1
}
$out=$p.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
$err=$p.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
[Console]::OpenStandardInput().CopyTo($p.StandardInput.BaseStream)
$p.StandardInput.BaseStream.Close()
$p.WaitForExit()
[Threading.Tasks.Task]::WaitAll(@($out,$err))
exit $p.ExitCode
