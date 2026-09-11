@echo off
REM Create, pause or resume the Cloud Scheduler jobs that drive linkedin-outreach.
REM
REM   Usage:  linkedinmcp\scheduler.cmd create      (from Python\)
REM           linkedinmcp\scheduler.cmd pause
REM           linkedinmcp\scheduler.cmd resume
REM
REM With no argument, or any other, it prints this usage, runs nothing and exits 1.
REM
REM RESUMING outreach-tick IS THE SWITCH THAT STARTS AUTONOMOUS SENDING. While it
REM runs, it POSTs /jobs/tick every four minutes in working hours, and each tick
REM sends the next due, approved message to a real person on LinkedIn. `create`
REM leaves all three jobs paused, so nothing runs until `resume`; `pause` stops
REM them again.
REM
REM The jobs -- project vk-linkedin, location us-central1, time zone
REM America/New_York; each an HTTP POST carrying the service's API key in the
REM x-api-key header, each with a 600 s attempt deadline and
REM --max-retry-attempts=0:
REM   outreach-tick    */4 7-20 * * 1-5   POST <service>/jobs/tick
REM   outreach-sync    */15 * * * *       POST <service>/jobs/sync
REM   outreach-daily   30 6 * * 1-5       POST <service>/jobs/daily
REM
REM --max-retry-attempts=0 is written out on all three jobs rather than left to
REM Cloud Scheduler's default, so a run that fails is never retried and the next
REM run comes on its schedule. outreach-daily needs it most: its intro cap counts
REM the intros ONE run creates, so a retried run would queue a second batch the
REM same morning. A retried outreach-tick would send the next due message ahead
REM of the four-minute pacing; a retried outreach-sync would only repeat work.
REM
REM <service> is OUTREACH_URL from linkedinmcp\platform\ids.env -- written by
REM deploy.cmd -- with its trailing /mcp/ removed. The key is OUTREACH_API_KEY
REM from Python\.env, which must hold the line as OUTREACH_API_KEY=<value>
REM (double quotes around the whole value allowed, nothing after it). It is read
REM into a variable and never echoed, and `setlocal` drops the variable when the
REM script ends.
REM
REM The key is, however, STORED in each job's configuration: anyone who can read
REM this project's Cloud Scheduler jobs can read it. gcloud may also record its
REM command lines, the key included, in its own local log files. And it is the
REM service's one API key, the same value the MCP endpoint accepts: whoever reads
REM it from a job can call every MCP tool, the human-side ones included --
REM answer_decision, approve_queued, reject_queued, pause, resume,
REM clear_writes_block, set_require_approval, clear_handling. A separate key for
REM the jobs, accepted only by /jobs/*, is a possible later change.
REM
REM `create` refuses a key holding a comma, a percent sign, a caret or a double
REM quote, before any gcloud call and without showing the key. A comma would
REM split gcloud's --headers list, and gcloud's error could then print part of
REM the key; `call` strips a percent sign and doubles a caret even inside
REM quotes; a double quote would break the command line. Inside its quotes the
REM key keeps the characters & | < > and ! intact. A key made the way the runbook
REM says, secrets.token_urlsafe(32), holds only letters, digits, - and _.
REM
REM Cloud Scheduler has no way to create a job already paused -- gcloud has no
REM such flag -- so `create` makes each job and pauses it straight away, and
REM stops the moment either step fails. For the few seconds between the two, a
REM job is live. For outreach-tick that means a tick could run if its schedule
REM comes due in that window: run `create` outside 07:00-20:59 New York time on a
REM weekday, or first pause sends with the MCP `pause` tool, and that window
REM cannot send anything. The usage text and `create` itself print this warning.
REM If a pause does fail, the script says so and exits 2: run
REM `linkedinmcp\scheduler.cmd pause` at once.
REM
REM There are no labels in this file, on purpose. .gitattributes checks every
REM text file out with LF line endings, and cmd.exe can fail to find a GOTO or
REM CALL label in an LF-only batch file (reproduced on this machine: 3 misses in
REM 240 lookups, depending only on where the label falls in the file). So each
REM action is an `if` block ending in `exit /b`, `create` is the straight-line
REM code after them, and nothing between a job's create and its pause depends on
REM finding a label.
REM
REM Every gcloud call is prefixed with `call`: gcloud is gcloud.cmd, and running a
REM batch file from another one without `call` never returns (see deploy.cmd).
REM `--format="value(name)"` on create and `--format=none` on pause and resume
REM stop gcloud printing the job it gets back, whose headers hold the key.
REM
REM Delayed expansion is switched OFF explicitly, whatever cmd.exe's default on
REM this machine: the key is read through a FOR variable and later passed on
REM inside quotes, and with delayed expansion on, a ! in it would be mangled on
REM the way. It is switched on only for the checks on the key, and off again
REM before the first gcloud call.

setlocal EnableExtensions DisableDelayedExpansion

SET PROJECT=vk-linkedin
SET LOCATION=us-central1
SET TIME_ZONE=America/New_York

SET "ACTION=%~1"
SET "KNOWN="
if /i "%ACTION%"=="create" SET "KNOWN=1"
if /i "%ACTION%"=="pause" SET "KNOWN=1"
if /i "%ACTION%"=="resume" SET "KNOWN=1"
if not "%~2"=="" SET "KNOWN="

if not defined KNOWN (
    echo.
    echo Usage:  linkedinmcp\scheduler.cmd create
    echo         linkedinmcp\scheduler.cmd pause
    echo         linkedinmcp\scheduler.cmd resume
    echo.
    echo   create  Create the three Cloud Scheduler jobs in %PROJECT%, %LOCATION%,
    echo           %TIME_ZONE% -- each one paused straight after it is created:
    echo             outreach-tick    */4 7-20 * * 1-5   POST /jobs/tick
    echo             outreach-sync    */15 * * * *       POST /jobs/sync
    echo             outreach-daily   30 6 * * 1-5       POST /jobs/daily
    echo           Each gets --max-retry-attempts=0: a run that fails is not retried.
    echo           The service URL comes from linkedinmcp\platform\ids.env and the
    echo           x-api-key header from OUTREACH_API_KEY in Python\.env.
    echo           WARNING: Cloud Scheduler cannot create a job already paused, so
    echo           each job is LIVE for the few seconds between its create and its
    echo           pause, and a tick that comes due in that window can send a
    echo           LinkedIn message. Run create on a weekend, or on a weekday before
    echo           07:00 or after 20:59 New York time, or pause sends first with the
    echo           MCP pause tool.
    echo   pause   Pause all three.
    echo   resume  Resume all three. Resuming outreach-tick starts autonomous
    echo           sending: every tick sends the next due message on LinkedIn.
    echo.
    echo Nothing was run.
    exit /b 1
)

REM ---------------------------------------------------------------------------
REM pause: outreach-tick first, since it is the one that sends. A failure does
REM not stop the others from being paused.
REM ---------------------------------------------------------------------------
if /i "%ACTION%"=="pause" (
    SET "FAILED="
    echo.
    echo === Pausing outreach-tick, outreach-sync and outreach-daily ===
    call gcloud scheduler jobs pause outreach-tick --project=%PROJECT% --location=%LOCATION% --format=none
    if errorlevel 1 (
        echo ERROR: outreach-tick was NOT paused. It may still be sending.
        SET "FAILED=1"
    )
    call gcloud scheduler jobs pause outreach-sync --project=%PROJECT% --location=%LOCATION% --format=none
    if errorlevel 1 (
        echo ERROR: outreach-sync was NOT paused.
        SET "FAILED=1"
    )
    call gcloud scheduler jobs pause outreach-daily --project=%PROJECT% --location=%LOCATION% --format=none
    if errorlevel 1 (
        echo ERROR: outreach-daily was NOT paused.
        SET "FAILED=1"
    )
    if defined FAILED (
        echo.
        echo Not every job was paused -- see the errors above.
        exit /b 1
    )
    echo.
    echo All three jobs are paused. Nothing runs until: linkedinmcp\scheduler.cmd resume
    exit /b 0
)

REM ---------------------------------------------------------------------------
REM resume: outreach-tick last, and only once the other two are running.
REM Resuming outreach-tick starts autonomous sending.
REM ---------------------------------------------------------------------------
if /i "%ACTION%"=="resume" (
    echo.
    echo === Resuming outreach-sync, outreach-daily, then outreach-tick ===
    call gcloud scheduler jobs resume outreach-sync --project=%PROJECT% --location=%LOCATION% --format=none
    if errorlevel 1 (
        echo.
        echo ERROR: resuming outreach-sync failed. Nothing was resumed.
        exit /b 1
    )
    call gcloud scheduler jobs resume outreach-daily --project=%PROJECT% --location=%LOCATION% --format=none
    if errorlevel 1 (
        echo.
        echo ERROR: resuming outreach-daily failed. outreach-sync IS running; outreach-tick was not resumed.
        exit /b 1
    )
    call gcloud scheduler jobs resume outreach-tick --project=%PROJECT% --location=%LOCATION% --format=none
    if errorlevel 1 (
        echo.
        echo ERROR: resuming outreach-tick failed. outreach-sync and outreach-daily ARE running;
        echo nothing is sent until outreach-tick runs.
        exit /b 1
    )
    echo.
    echo All three jobs are running. outreach-tick now sends LinkedIn messages on its own
    echo every four minutes in working hours. Stop everything with: linkedinmcp\scheduler.cmd pause
    exit /b 0
)

REM ---------------------------------------------------------------------------
REM create, step 1: the service URL and the key. Every check here runs before
REM any gcloud call, and no message prints the key.
REM
REM The checks on the key's content run with delayed expansion, and only there:
REM !API_KEY! is expanded after cmd.exe has parsed the line, so a double quote or
REM an ampersand in the key cannot close a quoted string or start a command
REM while it is being checked, and no error cmd.exe prints can hold part of it.
REM Nothing expands the key with percent signs until these checks have passed.
REM ---------------------------------------------------------------------------
if not exist "%~dp0platform\ids.env" (
    echo.
    echo ERROR: linkedinmcp\platform\ids.env does not exist. Run linkedinmcp\deploy.cmd first.
    exit /b 1
)
SET "OUTREACH_URL="
for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0platform\ids.env") do if /i "%%a"=="OUTREACH_URL" SET "OUTREACH_URL=%%~b"
if not defined OUTREACH_URL (
    echo.
    echo ERROR: linkedinmcp\platform\ids.env has no OUTREACH_URL line.
    exit /b 1
)
if not "%OUTREACH_URL:~-5%"=="/mcp/" (
    echo.
    echo ERROR: OUTREACH_URL in linkedinmcp\platform\ids.env does not end in /mcp/: %OUTREACH_URL%
    exit /b 1
)
SET "BASE=%OUTREACH_URL:~0,-5%"
if /i not "%BASE:~0,8%"=="https://" (
    echo.
    echo ERROR: OUTREACH_URL in linkedinmcp\platform\ids.env is not an https:// URL: %OUTREACH_URL%
    exit /b 1
)

if not exist "%~dp0..\.env" (
    echo.
    echo ERROR: Python\.env does not exist, so there is no OUTREACH_API_KEY for the jobs to send.
    exit /b 1
)
SET "API_KEY="
for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0..\.env") do if /i "%%a"=="OUTREACH_API_KEY" SET "API_KEY=%%~b"
if not defined API_KEY (
    echo.
    echo ERROR: Python\.env has no OUTREACH_API_KEY=... line.
    exit /b 1
)
setlocal EnableDelayedExpansion
SET "CHECK=!API_KEY:,=!"
SET "CHECK=!CHECK:%%=!"
SET "CHECK=!CHECK:^=!"
SET "CHECK=!CHECK:"=!"
if not "!CHECK!"=="!API_KEY!" (
    echo.
    echo ERROR: OUTREACH_API_KEY in Python\.env contains a comma, a percent sign, a caret
    echo or a double quote, which gcloud would split or mangle. No job was created. Make
    echo a new key with secrets.token_urlsafe, set it on the service as well, and run
    echo create again. The key is not shown.
    exit /b 1
)
if "!API_KEY:~15,1!"=="" (
    echo.
    echo ERROR: OUTREACH_API_KEY in Python\.env is shorter than 16 characters; the service refuses such a key.
    exit /b 1
)
if not "!API_KEY: =!"=="!API_KEY!" (
    echo.
    echo ERROR: OUTREACH_API_KEY in Python\.env contains a space. Write the line as
    echo OUTREACH_API_KEY=value, with nothing after the value.
    exit /b 1
)
endlocal

echo.
echo Service: %BASE%
echo Key:     OUTREACH_API_KEY from Python\.env -- not shown
echo.
echo WARNING: Cloud Scheduler cannot create a job already paused. Each job below is
echo LIVE for the few seconds between its create and its pause, and a tick that
echo comes due in that window can send a LinkedIn message. That cannot happen on a
echo weekend, on a weekday before 07:00 or after 20:59 New York time, or while
echo sends are paused with the MCP pause tool.

REM ---------------------------------------------------------------------------
REM create, step 2: each job, then pause it at once.
REM ---------------------------------------------------------------------------
echo.
echo === outreach-tick: create, then pause ===
call gcloud scheduler jobs create http outreach-tick ^
    --project=%PROJECT% ^
    --location=%LOCATION% ^
    --schedule="*/4 7-20 * * 1-5" ^
    --time-zone=%TIME_ZONE% ^
    --uri=%BASE%/jobs/tick ^
    --http-method=POST ^
    --headers="x-api-key=%API_KEY%" ^
    --attempt-deadline=600s ^
    --max-retry-attempts=0 ^
    --format="value(name)"
if errorlevel 1 (
    echo.
    echo ERROR: creating outreach-tick failed -- see gcloud's message above. No job was created.
    exit /b 1
)
call gcloud scheduler jobs pause outreach-tick --project=%PROJECT% --location=%LOCATION% --format=none
if errorlevel 1 (
    echo.
    echo DANGER: outreach-tick was created but NOT paused, so it is RUNNING and will send
    echo LinkedIn messages on its own. Pause it now:  linkedinmcp\scheduler.cmd pause
    exit /b 2
)

echo.
echo === outreach-sync: create, then pause ===
call gcloud scheduler jobs create http outreach-sync ^
    --project=%PROJECT% ^
    --location=%LOCATION% ^
    --schedule="*/15 * * * *" ^
    --time-zone=%TIME_ZONE% ^
    --uri=%BASE%/jobs/sync ^
    --http-method=POST ^
    --headers="x-api-key=%API_KEY%" ^
    --attempt-deadline=600s ^
    --max-retry-attempts=0 ^
    --format="value(name)"
if errorlevel 1 (
    echo.
    echo ERROR: creating outreach-sync failed -- see gcloud's message above. outreach-tick
    echo exists, paused; outreach-sync and outreach-daily were not created.
    exit /b 1
)
call gcloud scheduler jobs pause outreach-sync --project=%PROJECT% --location=%LOCATION% --format=none
if errorlevel 1 (
    echo.
    echo DANGER: outreach-sync was created but NOT paused, so it is RUNNING.
    echo Pause everything now:  linkedinmcp\scheduler.cmd pause
    exit /b 2
)

echo.
echo === outreach-daily: create, then pause ===
call gcloud scheduler jobs create http outreach-daily ^
    --project=%PROJECT% ^
    --location=%LOCATION% ^
    --schedule="30 6 * * 1-5" ^
    --time-zone=%TIME_ZONE% ^
    --uri=%BASE%/jobs/daily ^
    --http-method=POST ^
    --headers="x-api-key=%API_KEY%" ^
    --attempt-deadline=600s ^
    --max-retry-attempts=0 ^
    --format="value(name)"
if errorlevel 1 (
    echo.
    echo ERROR: creating outreach-daily failed -- see gcloud's message above. outreach-tick
    echo and outreach-sync exist, paused; outreach-daily was not created.
    exit /b 1
)
call gcloud scheduler jobs pause outreach-daily --project=%PROJECT% --location=%LOCATION% --format=none
if errorlevel 1 (
    echo.
    echo DANGER: outreach-daily was created but NOT paused, so it is RUNNING.
    echo Pause everything now:  linkedinmcp\scheduler.cmd pause
    exit /b 2
)

echo.
echo === Created outreach-tick, outreach-sync and outreach-daily, all paused ===
echo Nothing runs until:  linkedinmcp\scheduler.cmd resume
echo Resuming outreach-tick starts autonomous sending.
exit /b 0
