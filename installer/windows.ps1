# Embedded into setup.bat by installer/build.py. Windows PowerShell 5.1 compatible.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$setupRoot = Join-Path $env:LOCALAPPDATA 'MyAnalysis\setup'
[IO.Directory]::CreateDirectory($setupRoot) | Out-Null
$bootstrapLog = Join-Path $setupRoot 'bootstrap-windows.log'
Start-Transcript -Path $bootstrapLog -Append | Out-Null

function Probe-Python([string]$candidate) {
    if (!$candidate -or !(Test-Path -LiteralPath $candidate -PathType Leaf)) { return $null }
    if ($candidate -match '(?i)WindowsApps') { return $null }
    if ((Get-Item -LiteralPath $candidate).VersionInfo.CompanyName -ne 'Python Software Foundation') { return $null }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $candidate
    $info.Arguments = '-I -c "import sys,struct,sysconfig,json;print(json.dumps(dict(exe=sys.executable,base=sys.base_prefix,v=list(sys.version_info[:2]),bits=struct.calcsize(''P'')*8,gil=sysconfig.get_config_var(''Py_GIL_DISABLED''))))"'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (!$process.WaitForExit(10000)) {
        $process.Kill()
        $process.WaitForExit()
        throw "Python detection timed out; existing environment preserved: $candidate"
    }
    if ($process.ExitCode -ne 0) { return $null }
    try { $data = $stdout.Result | ConvertFrom-Json } catch { return $null }
    if ($data.bits -eq 64 -and $data.v[0] -eq 3 -and $data.v[1] -ge 11 -and !$data.gil -and $data.base -notmatch '(?i)WindowsApps' -and (Test-Path -LiteralPath (Join-Path $data.base 'Lib\venv\__init__.py'))) {
        return $data.exe
    }
    return $null
}

function Find-Python {
    $candidates = New-Object 'Collections.Generic.List[string]'
    $locations = @($env:MYANALYSIS_INSTALL_DIR, $env:MYANALYSIS_SETUP_SOURCE, (Join-Path $env:USERPROFILE 'MyAnalysis'), (Join-Path $env:LOCALAPPDATA 'Programs\MyAnalysis'))
    $selection = Join-Path $setupRoot 'selection.json'
    if (Test-Path -LiteralPath $selection) {
        try { $locations += (Get-Content -LiteralPath $selection -Raw -Encoding UTF8 | ConvertFrom-Json).target } catch { }
    }
    foreach ($location in $locations) {
        if (!$location) { continue }
        $cfg = Join-Path $location '.venv\pyvenv.cfg'
        if (Test-Path -LiteralPath $cfg) {
            foreach ($line in Get-Content -LiteralPath $cfg -Encoding UTF8) {
                if ($line -match '^home\s*=\s*(.+)$') { $candidates.Add((Join-Path $Matches[1].Trim() 'python.exe')) }
            }
        }
    }
    # Only PythonCore registration (python.org), never Store launch aliases.
    foreach ($root in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        if (Test-Path -LiteralPath $root) {
            foreach ($version in Get-ChildItem -LiteralPath $root) {
                $install = Join-Path $version.PSPath 'InstallPath'
                if (Test-Path -LiteralPath $install) {
                    $key = Get-Item -LiteralPath $install
                    $exe = $key.GetValue('ExecutablePath')
                    if (!$exe) { $exe = Join-Path $key.GetValue('') 'python.exe' }
                    $candidates.Add($exe)
                }
            }
        }
    }
    foreach ($name in @('python.exe', 'python3.exe', 'python3.13.exe', 'python3.12.exe', 'python3.11.exe')) {
        foreach ($command in Get-Command $name -All -ErrorAction SilentlyContinue) {
            if ($command.CommandType -eq 'Application' -and $command.Source -notmatch '(?i)WindowsApps') { $candidates.Add($command.Source) }
        }
    }
    $known = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path -LiteralPath $known) {
        foreach ($directory in Get-ChildItem -LiteralPath $known -Directory) { $candidates.Add((Join-Path $directory.FullName 'python.exe')) }
    }
    # Probe checks PSF build metadata and the running base environment as well.
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        $usable = Probe-Python $candidate
        if ($usable) { return $usable }
    }
    return $null
}

function Get-OfficialPython {
    $asset = Join-Path $setupRoot 'python-3.13.16-amd64.exe'
    $url = 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe'
    $sha = 'fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b'
    $size = 29935584
    $valid = (Test-Path -LiteralPath $asset) -and (Get-Item -LiteralPath $asset).Length -eq $size -and (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash -eq $sha
    if (!$valid) {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $part = $asset + '.' + [Guid]::NewGuid().ToString('N') + '.part'
            try {
                Write-Host "Downloading official Python ($attempt/3)..."
                # Use a bounded asynchronous transfer; no dependence on curl aliases.
                Add-Type -AssemblyName System.Net.Http
                $handler = New-Object Net.Http.HttpClientHandler
                $handler.DefaultProxyCredentials = [Net.CredentialCache]::DefaultCredentials
                if ($env:HTTPS_PROXY) { $handler.Proxy = New-Object Net.WebProxy($env:HTTPS_PROXY) }
                $client = New-Object Net.Http.HttpClient($handler)
                $client.Timeout = [TimeSpan]::FromMinutes(15)
                $response = $client.GetAsync($url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead)
                if (!$response.Wait(15000)) { throw 'Download connection timed out' }
                $response.Result.EnsureSuccessStatusCode() | Out-Null
                $source = $response.Result.Content.ReadAsStreamAsync().Result
                $destination = [IO.File]::Create($part)
                try {
                    $buffer = New-Object byte[] 65536
                    $deadline = [DateTime]::UtcNow.AddMinutes(15)
                    while ($true) {
                        $read = $source.ReadAsync($buffer, 0, $buffer.Length)
                        if (!$read.Wait(60000)) { throw 'Download read timed out' }
                        if ($read.Result -eq 0) { break }
                        $destination.Write($buffer, 0, $read.Result)
                        if ($destination.Length -gt $size -or [DateTime]::UtcNow -gt $deadline) { throw 'Download size/time exceeded' }
                    }
                } finally { $destination.Dispose(); $source.Dispose(); $client.Dispose(); $handler.Dispose() }
                if ((Get-Item -LiteralPath $part).Length -ne $size -or (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash -ne $sha) { throw 'Python installer integrity check failed' }
                Move-Item -LiteralPath $part -Destination $asset -Force
                break
            } catch {
                if ($attempt -eq 3 -or $_ -match 'integrity|size/time|40[0134]|certificate|SSL') { throw }
            } finally {
                if (Test-Path -LiteralPath $part) { Remove-Item -LiteralPath $part }
            }
        }
    }
    # The GUI installer runs separately; closing our terminal must not kill it.
    $pendingFile = Join-Path $setupRoot 'python-installer.json'
    if (Test-Path -LiteralPath $pendingFile) {
        $pending = Get-Content -LiteralPath $pendingFile -Raw | ConvertFrom-Json
        $old = Get-Process -Id $pending.pid -ErrorAction SilentlyContinue
        if ($old -and $old.StartTime.ToUniversalTime().Ticks.ToString() -eq $pending.started) {
            throw 'A previous Python installer is still running. Complete its dialog and rerun setup.'
        }
    }
    Write-Host 'Installing python.org Python. Complete any installer dialog; automatic restart is disabled.'
    $process = Start-Process -FilePath $asset -ArgumentList @('/passive', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0', 'Include_test=0', '/norestart') -WindowStyle Hidden -PassThru
    @{ pid=$process.Id; started=$process.StartTime.ToUniversalTime().Ticks.ToString() } | ConvertTo-Json | Set-Content -LiteralPath $pendingFile -Encoding UTF8
    $deadline = [DateTime]::UtcNow.AddMinutes(30)
    while (!$process.WaitForExit(1000)) {
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Python installer still running. Finish it and rerun setup. It has not been terminated.' }
        if ([DateTime]::UtcNow.Second % 10 -eq 0) { Write-Host 'Waiting for Python installer...' }
    }
    if ($process.ExitCode -eq 3010) { throw 'Python requires a restart. Restart manually and run setup again.' }
    if ($process.ExitCode -ne 0) { throw "Python installer exited with $($process.ExitCode). Rerun setup after resolving the installer message." }
    return Find-Python
}

$exitCode = 1
try {
    if ([Environment]::Is64BitOperatingSystem -eq $false -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { throw 'Supported Windows target: Windows 11 x64.' }
    if ([Environment]::OSVersion.Version.Build -lt 22000) { throw 'Windows 11 or later is required.' }
    # The bootstrap mutex is transient, never a global PATH/registry modification.
    $mutex = New-Object Threading.Mutex($false, 'Local\MyAnalysisPythonBootstrap')
    try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (!$acquired) { throw 'Another setup is preparing Python. Retry after it finishes.' }
    try {
        $python = Find-Python
        if (!$python) { $python = Get-OfficialPython }
        if (!$python) { throw 'Official Python is not ready. See bootstrap-windows.log and rerun setup.' }
    } finally { $mutex.ReleaseMutex(); $mutex.Dispose() }
    Write-Host "Using official Python: $python"
    $core = Join-Path $setupRoot ('core-' + [Guid]::NewGuid().ToString('N') + '.py')
    [IO.File]::WriteAllBytes($core, [Convert]::FromBase64String('__CORE_BASE64__'))
    # The batch owns the single failure pause, including a failure to start PS.
    $arguments = @('--no-pause')
    if ($env:MYANALYSIS_SETUP_SOURCE) { $arguments += @('--source-dir', $env:MYANALYSIS_SETUP_SOURCE) }
    if ($env:MYANALYSIS_INSTALL_DIR) { $arguments += @('--install-dir', $env:MYANALYSIS_INSTALL_DIR) }
    if ($env:MYANALYSIS_NO_LAUNCH -eq '1') { $arguments += '--no-launch' }
    # PowerShell 5.1's native array passing corrupts quoted paths ending in a
    # backslash. Build a CommandLineToArgvW-compatible argument string instead.
    function Quote-NativeArgument([string]$value) {
        $escaped = [regex]::Replace($value, '(\\*)"', '$1$1\"')
        $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
        return '"' + $escaped + '"'
    }
    $nativeInfo = New-Object Diagnostics.ProcessStartInfo
    $nativeInfo.FileName = $python
    $nativeInfo.UseShellExecute = $false
    $nativeInfo.Arguments = ((@('-u', $core) + $arguments) | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
    $nativeProcess = [Diagnostics.Process]::Start($nativeInfo)
    $nativeProcess.WaitForExit()
    $exitCode = $nativeProcess.ExitCode
} catch {
    Write-Host "Setup incomplete: $_"
    Write-Host "Log: $bootstrapLog"
} finally { Stop-Transcript | Out-Null }
exit $exitCode
