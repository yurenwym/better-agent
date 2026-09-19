$ErrorActionPreference = 'Stop'
$logPath = 'D:\RAG\better\data\logs\acceptance-port-8000.log'
Start-Transcript -Path $logPath -Append
try {
    Stop-Service -Name winnat -ErrorAction Stop
    try {
        netsh interface ipv4 show excludedportrange protocol=tcp
        netsh interface ipv4 add excludedportrange protocol=tcp startport=8000 numberofports=1 store=persistent
        if ($LASTEXITCODE -ne 0) { throw 'Could not reserve port 8000' }
    }
    finally {
        Start-Service -Name winnat -ErrorAction Stop
    }
    netsh interface ipv4 show excludedportrange protocol=tcp
    Get-Service -Name winnat
}
finally {
    Stop-Transcript
}
