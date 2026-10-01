# Patch notes

## 2026-09-30

**Reliable hourly runs.** GitHub's built-in cron skipped most hours on this repo, so each run now starts the next one itself at :00. The cron (now at :05) is only a backup that restarts the cycle if it ever breaks. A backup or manual run steps aside if a scan is already running, so there are never two cycles or duplicate emails.

**Alerts on a fixed clock.** The scan starts at :00 (~12–15 minutes), then the alert email goes out at :20. If a scan runs late, the alert is sent as soon as it finishes instead of skipping the hour.

**Expired postings are filtered out.** Before sending, each role's own page is checked. It's dropped (and never sent later) if the page is gone (404/410), its listed end date has passed, or it says the position is filled or no longer accepting applications.

**An email every hour.** Hours with nothing new send a short "scan finished, no new roles" email, so a missing alert always means something broke.

**Diagnosable email failures.** If sending fails, `last_run.txt` records why, and which secrets arrived (never their values).
