@echo off
REM ATERag MCP 服务启动 (streamable-http, 默认 0.0.0.0:8080)
cd /d %~dp0..
.venv\Scripts\python.exe -m aterag.mcp_server.server
