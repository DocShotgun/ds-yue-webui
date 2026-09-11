@echo off
rem ds-yue-webui installer for Windows (see install.ps1 for flags).
rem   deploy\install.bat --help
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
