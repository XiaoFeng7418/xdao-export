@echo off
chcp 65001 >nul
title X岛串导出工具 - 写入诊断
echo 正在检查各个目录能不能写入，请稍等（大约几秒）...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0诊断写入.ps1"
echo.
echo 诊断结束。上面最后一行的「已保存」就是结果文件的路径。
pause
