@echo off
rem Start the ds-yue-webui server on Windows (see run.ps1 for details).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
