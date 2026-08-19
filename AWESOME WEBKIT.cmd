@echo off
where py >nul 2>nul
if not errorlevel 1 py -3 -c "import sys; raise SystemExit(sys.version_info[0] != 3 or sys.version_info[1] not in range(7, 100))" >nul 2>nul
if not errorlevel 1 goto use_py
where python3 >nul 2>nul
if not errorlevel 1 python3 -c "import sys; raise SystemExit(sys.version_info[0] != 3 or sys.version_info[1] not in range(7, 100))" >nul 2>nul
if not errorlevel 1 goto use_python3
where python >nul 2>nul
if not errorlevel 1 python -c "import sys; raise SystemExit(sys.version_info[0] != 3 or sys.version_info[1] not in range(7, 100))" >nul 2>nul
if not errorlevel 1 goto use_python
echo AWESOME WEBKIT requires Python 3.7 or newer. Install Python and try again. 1>&2
exit /b 1
:use_py
py -3 "%~dp0control-center\launch.py" %*
exit /b %errorlevel%
:use_python3
python3 "%~dp0control-center\launch.py" %*
exit /b %errorlevel%
:use_python
python "%~dp0control-center\launch.py" %*
exit /b %errorlevel%
