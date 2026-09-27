$ErrorActionPreference = 'Stop'
$b = '__SYMPHONY_BOOTSTRAP__'
$py = @(Get-Command python.exe -CommandType Application)[0].Source
$i = [Diagnostics.ProcessStartInfo]::new($py, '-I -c "' + $b + '" "' + $env:PLUGIN_ROOT + '" codex')
$i.UseShellExecute = $false
$i.RedirectStandardInput = $true
$i.RedirectStandardOutput = $true
$i.RedirectStandardError = $true
try {
$p = [Diagnostics.Process]::Start($i)
} catch {
[Console]::Error.WriteLine('Symphony launcher could not start Python')
exit 1
}
$out = $p.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
$err = $p.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
[Console]::OpenStandardInput().CopyTo($p.StandardInput.BaseStream)
$p.StandardInput.BaseStream.Close()
$p.WaitForExit()
[Threading.Tasks.Task]::WaitAll(@($out, $err))
exit $p.ExitCode
