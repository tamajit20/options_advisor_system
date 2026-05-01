@echo off
REM ============================================================================
REM  Options Advisor System - One-Click Deploy
REM  Builds the Docker image, starts the stack, and verifies the web dashboard
REM  + scheduler are running. Double-click this file to deploy.
REM ============================================================================

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   Options Advisor System - Docker Deployment
echo ============================================================
echo.

REM --- 1. Verify Docker is installed and running -----------------------------
where docker >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not installed or not on PATH.
    echo         Install Docker Desktop from https://www.docker.com/products/docker-desktop
    goto :fail
)

docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker daemon is not running.
    echo         Please start Docker Desktop and try again.
    goto :fail
)

REM --- 2. Pick the right compose command -------------------------------------
set "COMPOSE=docker compose"
docker compose version >nul 2>&1
if errorlevel 1 (
    where docker-compose >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Neither "docker compose" nor "docker-compose" is available.
        goto :fail
    )
    set "COMPOSE=docker-compose"
)
echo [INFO] Using compose command: %COMPOSE%

REM --- 3. Ensure .env.docker exists ------------------------------------------
if not exist ".env.docker" (
    if exist ".env.docker.example" (
        echo [WARN] .env.docker not found. Creating from .env.docker.example ...
        copy /Y ".env.docker.example" ".env.docker" >nul
        echo [WARN] Please review .env.docker and update credentials/secrets.
    ) else (
        echo [ERROR] .env.docker is missing and no .env.docker.example to copy from.
        goto :fail
    )
)

REM --- 4. Make sure host folders exist (mounted as volumes) ------------------
if not exist "logs"    mkdir "logs"
if not exist "data"    mkdir "data"
if not exist "archive" mkdir "archive"

REM --- 5. Stop any previous instance, build fresh image, start detached ------
echo.
echo [STEP] Stopping any existing containers...
%COMPOSE% down --remove-orphans

echo.
echo [STEP] Building Docker image (this may take a few minutes the first time)...
%COMPOSE% build
if errorlevel 1 (
    echo [ERROR] Docker build failed.
    goto :fail
)

echo.
echo [STEP] Starting the application stack...
%COMPOSE% up -d
if errorlevel 1 (
    echo [ERROR] Failed to start containers.
    goto :fail
)

REM --- 6. Wait for health check to pass --------------------------------------
echo.
echo [STEP] Waiting for the dashboard to become healthy...
set /a TRIES=0
:wait_loop
set /a TRIES+=1
for /f "delims=" %%S in ('docker inspect -f "{{.State.Health.Status}}" stock_options_advisor 2^>nul') do set "HEALTH=%%S"
if /i "!HEALTH!"=="healthy" goto :healthy
if /i "!HEALTH!"=="unhealthy" (
    echo [ERROR] Container reported unhealthy.
    goto :show_logs
)
if !TRIES! GEQ 40 (
    echo [WARN] Health check did not turn healthy within timeout. Current status: !HEALTH!
    goto :show_logs
)
<nul set /p "=."
timeout /t 3 /nobreak >nul
goto :wait_loop

:healthy
echo.
echo.
echo ============================================================
echo   DEPLOYMENT SUCCESSFUL
echo ============================================================
echo   Dashboard : http://localhost:5001
echo   Health    : http://localhost:5001/health
echo   Container : stock_options_advisor
echo.
echo   Useful commands:
echo     View logs : %COMPOSE% logs -f
echo     Stop      : %COMPOSE% down
echo     Restart   : %COMPOSE% restart
echo ============================================================
echo.

REM --- 7. Open dashboard in default browser ----------------------------------
start "" "http://localhost:5001"
goto :end

:show_logs
echo.
echo [INFO] Recent container logs:
echo ------------------------------------------------------------
%COMPOSE% logs --tail=80
echo ------------------------------------------------------------
echo [INFO] Container is running but not healthy yet.
echo        Check logs above or run: %COMPOSE% logs -f
goto :end

:fail
echo.
echo ============================================================
echo   DEPLOYMENT FAILED - see messages above
echo ============================================================
echo.
pause
exit /b 1

:end
echo.
pause
endlocal
