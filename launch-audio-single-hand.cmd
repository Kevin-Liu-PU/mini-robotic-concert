@echo off
call "%~dp0launch.cmd" --config "%~dp0config-responsive.json" --hold-clock --audio-clock --audio-config "%~dp0config-audio.json" %*
