function Trigger-Job($n) {
    try {
        $r = Invoke-RestMethod -Method Post -Uri "http://localhost:5001/api/jobs/$n/trigger" -TimeoutSec 10
        Write-Host "QUEUED $n -> $($r.status)"
    } catch {
        Write-Host "ERROR $n : $($_.Exception.Message)"
    }
}

function Wait-Job($n, $maxSec = 180) {
    $deadline = (Get-Date).AddSeconds($maxSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        $row = sqlcmd -S "TAMAJITLAPTOP\SQLEXPRESS" -d OptionsAdvisorDB -E -C -h -1 -W -Q "SET NOCOUNT ON; SELECT TOP 1 status, ISNULL(LEFT(error_message,100),'-') FROM options_job_log WHERE job_name='$n' ORDER BY started_at DESC"
        $line = ($row -join " ").Trim()
        if ($line -match "SUCCESS|FAILED|SKIPPED") {
            Write-Host "$n -> $line"
            return
        }
    }
    Write-Host "$n -> TIMEOUT"
}

foreach ($job in @("spot_bhav_download", "fo_bhav_download", "vix_download", "fii_download")) {
    Trigger-Job $job
    Wait-Job $job 180
}
