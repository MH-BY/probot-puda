@echo off
setlocal

for %%E in (
  smu-keysight-probot
  stage-probot
) do (
  start "%%E" /D "%~dp0%%E" cmd /k start_edge.bat
)

endlocal
