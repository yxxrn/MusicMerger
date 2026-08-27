@echo off
setlocal
title MusicMerger
pushd "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python belum ditemukan. Pasang Python 3.11 dan aktifkan Add Python to PATH.
  popd
  pause
  exit /b 1
)
python -B -m musicmerger %*
set "MUSICMERGER_EXIT=%ERRORLEVEL%"
echo.
pause
popd
exit /b %MUSICMERGER_EXIT%
