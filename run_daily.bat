@echo off
REM Daily - tier 1 (Bangkok + 5 metro provinces)
REM Skips pages with no new listings, so repeat runs take ~3 min.
REM Also fetches full addresses for up to 150 properties per run,
REM which improves map accuracy from district to subdistrict level.
call "%~dp0_run_common.bat" 1 30 daily 150
