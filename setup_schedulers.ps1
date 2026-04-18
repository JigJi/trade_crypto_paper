$python = 'C:\Users\alprdev\AppData\Local\Programs\Python\Python310\python.exe'
$scriptDir = 'D:\1. Smart Trade\Market factors'

$tasks = @(
    @{ Name = 'SmartTrade\get_basis';           Script = 'get_basis.py' },
    @{ Name = 'SmartTrade\get_order_book';      Script = 'get_order_book.py' },
    @{ Name = 'SmartTrade\get_btc_dominance';   Script = 'get_btc_dominance.py' },
    @{ Name = 'SmartTrade\get_macro';           Script = 'get_macro.py' },
    @{ Name = 'SmartTrade\get_deribit_options';  Script = 'get_deribit_options.py' }
)

foreach ($t in $tasks) {
    $scriptPath = Join-Path $scriptDir $t.Script
    $action = New-ScheduledTaskAction -Execute $python -Argument "`"$scriptPath`"" -WorkingDirectory $scriptDir
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 9999)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings -Force
    Write-Host "Created: $($t.Name) -> $($t.Script) every 15m"
}
