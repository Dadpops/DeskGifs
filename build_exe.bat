@echo off
REM Build DeskGifs into a standalone Windows .exe (no Python or terminal needed).
REM Double-click this file, or run it from a terminal.

echo Installing PyInstaller (if needed)...
python -m pip install --quiet pyinstaller

echo Building DeskGifs.exe ...
python -m PyInstaller --noconfirm --noconsole --onefile --name DeskGifs desk_gif.py

echo.
echo Done. Your app is at:  dist\DeskGifs.exe
echo Put your animation file (e.g. teen-titans-teen-titans-go.gif) next to the
echo .exe, or just launch it and use "Open image..." in the control panel.
pause
