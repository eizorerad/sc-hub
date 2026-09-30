@echo off
rem Your sc-hub dashboard on this computer: runs schub-view.ps1 even where PowerShell scripts are blocked.
rem   schub-view.cmd            start and open        schub-view.cmd status ^| stop ^| serve ^| --once
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0schub-view.ps1" %*
