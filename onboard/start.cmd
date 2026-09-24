@echo off
rem The sc-hub setup page on Windows: runs start.ps1 even where PowerShell scripts are blocked.
rem   onboard\start.cmd            the page
rem   onboard\start.cmd status     (and open, retry, stop, check, review-prompt)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
