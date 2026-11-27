@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title HoweMine — установка и сборка ролика
echo.
echo   HoweMine — обзор сборки, 1920x1080 60fps
echo   ------------------------------------------------
echo.
set "WORK=%USERPROFILE%\HoweMine_build"
set "DL=%USERPROFILE%\Downloads\HoweMine"
if not exist "%WORK%" mkdir "%WORK%" >nul 2>&1
if not exist "%DL%" mkdir "%DL%" >nul 2>&1
cd /d "%WORK%"

echo [1/5] Проверяю Python...
set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
    echo       Python не найден, ставлю через winget...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements >nul 2>&1
    if not exist "!LOCALAPPDATA!\Programs\Python\Python312\python.exe" (
        echo       Качаю установщик с python.org...
        curl -L -o python-installer.exe "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" || goto :fail
        echo       Ставлю Python, подождите 1-2 минуты...
        start /wait "" python-installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
    )
    set "PY="
    if exist "!LOCALAPPDATA!\Programs\Python\Python312\python.exe" set "PY=!LOCALAPPDATA!\Programs\Python\Python312\python.exe"
    if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
    if not defined PY python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo       Python поставить не удалось. Поставьте вручную:
    echo       https://www.python.org/downloads/  — и запустите файл снова.
    goto :fail
)
echo       OK

echo [2/5] Проверяю ffmpeg...
set "FF="
if exist "%WORK%\ffmpeg\bin\ffmpeg.exe" set "FF=%WORK%\ffmpeg\bin\ffmpeg.exe"
where ffmpeg >nul 2>&1 && set "FF=ffmpeg"
if not defined FF (
    echo       Качаю ffmpeg (около 40 МБ)...
    curl -L --retry 3 -o ffmpeg.zip "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" || goto :fail
    powershell -NoProfile -Command "Expand-Archive -Force -Path 'ffmpeg.zip' -DestinationPath 'ffmpeg_tmp'" || goto :fail
    for /d %%D in (ffmpeg_tmp\ffmpeg-*) do move "%%D" "ffmpeg" >nul
    rmdir /s /q ffmpeg_tmp >nul 2>&1
    del /q ffmpeg.zip >nul 2>&1
    if not exist "%WORK%\ffmpeg\bin\ffmpeg.exe" goto :fail
    set "FF=%WORK%\ffmpeg\bin\ffmpeg.exe"
)
echo       OK

echo [3/5] Качаю части ролика...
rem HoweMine_review_1080p60_part00.mp4: 56.39 МБ
set "URL1=https://www.dropbox.com/scl/fi/wf9knstyiedba5dd4dc2u/HoweMine_review_1080p60_part00.mp4?rlkey=dkhhwjq0li3hs3w1z1cepmd5m&dl=1"
rem HoweMine_review_1080p60_part01.mp4: 47.46 МБ
set "URL2=https://www.dropbox.com/scl/fi/ajk2isz7uqvkinqz7rpna/HoweMine_review_1080p60_part01.mp4?rlkey=1eva5ox7zhj4eocsrey0dth7w&dl=1"

rem --- HoweMine_review_1080p60_part00.mp4 (56.39 МБ)
if exist "HoweMine_review_1080p60_part00.mp4" goto have1
echo       HoweMine_review_1080p60_part00.mp4 ...
curl -L --retry 3 --retry-delay 2 -o "HoweMine_review_1080p60_part00.mp4" "%URL1%"
if not exist "HoweMine_review_1080p60_part00.mp4" (
    echo       Не скачался HoweMine_review_1080p60_part00.mp4. Проверьте интернет и запустите снова.
    goto :fail
)
:have1
for %%S in ("HoweMine_review_1080p60_part00.mp4") do if %%~zS LSS 1000000 (
    echo       HoweMine_review_1080p60_part00.mp4 скачался не полностью.
    echo       Удалите его из "%WORK%" и запустите файл снова.
    goto :fail
)
powershell -NoProfile -Command "if ((Get-FileHash -Algorithm SHA256 'HoweMine_review_1080p60_part00.mp4').Hash -ne '945F471B413AB27A4DF0A6847A836A562B955260E8FFCFBD98D0E783F18810D6') { exit 1 }"
if errorlevel 1 (
    echo       HoweMine_review_1080p60_part00.mp4: хэш не совпал — файл удалён, запустите снова.
    del /q "HoweMine_review_1080p60_part00.mp4"
    goto :fail
)
echo       OK HoweMine_review_1080p60_part00.mp4
rem --- HoweMine_review_1080p60_part01.mp4 (47.46 МБ)
if exist "HoweMine_review_1080p60_part01.mp4" goto have2
echo       HoweMine_review_1080p60_part01.mp4 ...
curl -L --retry 3 --retry-delay 2 -o "HoweMine_review_1080p60_part01.mp4" "%URL2%"
if not exist "HoweMine_review_1080p60_part01.mp4" (
    echo       Не скачался HoweMine_review_1080p60_part01.mp4. Проверьте интернет и запустите снова.
    goto :fail
)
:have2
for %%S in ("HoweMine_review_1080p60_part01.mp4") do if %%~zS LSS 1000000 (
    echo       HoweMine_review_1080p60_part01.mp4 скачался не полностью.
    echo       Удалите его из "%WORK%" и запустите файл снова.
    goto :fail
)
powershell -NoProfile -Command "if ((Get-FileHash -Algorithm SHA256 'HoweMine_review_1080p60_part01.mp4').Hash -ne 'B5280848214784E6A2703CDFF18A9A5256EF4189B1ADA1F92078EAE866502498') { exit 1 }"
if errorlevel 1 (
    echo       HoweMine_review_1080p60_part01.mp4: хэш не совпал — файл удалён, запустите снова.
    del /q "HoweMine_review_1080p60_part01.mp4"
    goto :fail
)
echo       OK HoweMine_review_1080p60_part01.mp4

echo [4/5] Проверяю целостность файлов...
echo       все части на месте и совпадают по SHA-256

echo [5/5] Собираю ролик (без перекодирования, качество не теряется)...
(echo file '%WORK%\HoweMine_review_1080p60_part00.mp4')>join.txt
(echo file '%WORK%\HoweMine_review_1080p60_part01.mp4')>>join.txt
"%FF%" -v error -y -f concat -safe 0 -i join.txt -c copy -movflags +faststart "%DL%\HoweMine_review_1080p60.mp4"
if not exist "%DL%\HoweMine_review_1080p60.mp4" goto :fail

echo.
echo   ============ ГОТОВО ============
echo    Ролик: "%DL%\HoweMine_review_1080p60.mp4"
echo.
start "" "%DL%"
pause
exit /b 0

:fail
echo.
echo   Что-то не получилось. Запустите файл ещё раз: уже скачанное
echo   повторно не качается. Если не помогает — пришлите скриншот окна.
echo.
pause
exit /b 1
