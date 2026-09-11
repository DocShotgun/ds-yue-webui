@echo off
rem Predownload the model checkpoints (see download-models.ps1 for details).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0download-models.ps1" %*
