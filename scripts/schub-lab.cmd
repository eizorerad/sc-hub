@echo off
rem Runs schub-lab.ps1 even where PowerShell scripts are blocked by the default execution policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0schub-lab.ps1" %*
