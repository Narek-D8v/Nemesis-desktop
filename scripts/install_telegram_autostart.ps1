# Автозапуск headless-бота Немуси в Telegram при входе в Windows.
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\install_telegram_autostart.ps1
# Удаление: ... -Uninstall
#
# Задача работает от текущего пользователя и не требует прав администратора.
# Бот поднимается без консольного окна (pythonw) и перезапускается при падении.

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "NemusTelegram"
$ShortcutName = "Немуся Telegram.lnk"
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir $ShortcutName

if ($Uninstall) {
    Remove-Item -LiteralPath $ShortcutPath -Force -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Автозапуск '$TaskName' удалён (задача и ярлык)."
    exit 0
}

$Root = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Root "venv\Scripts\pythonw.exe"
$Launcher = Join-Path $PSScriptRoot "run_telegram_task.vbs"
if (-not (Test-Path $Pythonw)) {
    throw "Не найден $Pythonw. Запусти скрипт из папки проекта с созданным venv."
}
if (-not (Test-Path $Launcher)) {
    throw "Не найден $Launcher. Обнови папку scripts из репозитория."
}

$Action = New-ScheduledTaskAction `
    -Execute $Pythonw `
    -Argument "-m nemus --telegram" `
    -WorkingDirectory $Root

# Триггера нет: задачу запускает ярлык из автозагрузки. Так она видна
# в списке автозагрузки Windows, а Планировщик перезапускает её при падении.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Settings $Settings `
    -Description "Немуся: Telegram-бот без GUI (python -m nemus --telegram)" `
    -Force | Out-Null

# Ярлык в Startup: именно его показывает «Автозагрузка» Windows.
# Запускает не сам Python, а скрытый VBS, который стартует задачу.
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $env:SystemRoot "System32\wscript.exe"
$Shortcut.Arguments = '"' + $Launcher + '"'
$Shortcut.WorkingDirectory = $Root
$Shortcut.WindowStyle = 7
$Shortcut.IconLocation = "$Pythonw,0"
$Shortcut.Description = "Немуся: Telegram-бот без GUI"
$Shortcut.Save()

Write-Host "Готово: '$ShortcutName' добавлен в автозагрузку Windows."
Write-Host "Задача '$TaskName' запускается при входе и перезапускается при падении."
Write-Host "Проверить:  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Запустить:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Логи:       data\nemus.log (путь из logging.file в config.yaml)"
