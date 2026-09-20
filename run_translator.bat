@echo off
rem Launches the Discord hover translator in a minimized console window.
rem Restore that window from the taskbar to type commands (to en, delay 0.5, status, quit).
rem Closing the window also stops the translator.
cd /d "%~dp0"
start "Discord Translator" /min py -3.11 discord_hover_translate.py
