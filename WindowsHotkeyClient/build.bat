@echo off
REM Builds a standalone DiscordSoundboardHotkeys.exe into dist\.
REM Creates/reuses a local .venv so dependencies never touch your
REM global/system Python - just run this from this folder.

REM A previously-built copy is often still running in the tray (easy to
REM forget to Exit it before rebuilding), which locks the exe file and makes
REM PyInstaller fail with a PermissionError. Kill it first - ignore errors if
REM it wasn't running.
taskkill /IM DiscordSoundboardHotkeys.exe /F >nul 2>&1

if exist dist\DiscordSoundboardHotkeys.exe (
    timeout /t 1 /nobreak >nul
    del /f /q dist\DiscordSoundboardHotkeys.exe 2>nul
    if exist dist\DiscordSoundboardHotkeys.exe (
        echo.
        echo ERROR: dist\DiscordSoundboardHotkeys.exe is still locked by another
        echo process. Check the system tray and Task Manager for
        echo DiscordSoundboardHotkeys.exe, close it, then re-run build.bat.
        exit /b 1
    )
)

if not exist .venv (
    python -m venv .venv || exit /b 1
)

.venv\Scripts\python.exe -m pip install --upgrade pip || exit /b 1
.venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
.venv\Scripts\python.exe build_icon.py || exit /b 1
.venv\Scripts\python.exe -m PyInstaller --onefile --windowed --name DiscordSoundboardHotkeys --icon icon.ico hotkey_client.py || exit /b 1

echo.
echo Done. See dist\DiscordSoundboardHotkeys.exe
