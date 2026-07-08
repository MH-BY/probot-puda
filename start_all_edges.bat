@echo off
setlocal

for %%E in (
  probot-keysight-pico
  probot-stage
) do (
  start "%%E" /D "%~dp0%%E" cmd /k start_edge.bat
)

endlocal
