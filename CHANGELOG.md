# Patch notes

## 2026-10-01

**No more old roles.** Every alert role must have a real post date from the company (or Simplify) within the last 14 days. The "Older, strong fit" section is gone, and roles with no post date are no longer sent. Previously an undated role could be labelled "Just posted" just because the scanner hadn't seen it before, which let months-old postings through.

**Post dates for more sites.** Oracle, SmartRecruiters, SuccessFactors, BambooHR, Rippling and Apple postings now report their real post date.

**Only EE work.** A title must clearly be EE (electrical, hardware, PCB, RF, embedded, FPGA, power, ...) to qualify on its own. Manufacturing, quality, process, production, construction, technician, supplier and similar roles are dropped unless the title also says electrical/hardware. Generic titles ("Engineering Intern", "Test Engineering Intern") are only sent when the posting's own description asks for EE.

**More EE role types recognized by title.** EV and battery systems / BMS, high voltage, transmission & distribution, protection & control, relay, solar PV and inverters, semiconductor ATE test, radar, PLC / SCADA / controls & automation, and PCB/IC layout now count as clearly EE, alongside hardware, PCB, HIL, signal/power integrity, avionics, wire harness, power electronics and power systems, data center power, embedded, firmware, FPGA/digital, ASIC, design verification, analog/mixed-signal, RF, antenna, wireless, DSP, and medical device / robotics / MEP electrical roles. "Project Controls" (cost scheduling) is excluded.

## 2026-09-30

**Reliable hourly runs.** GitHub's built-in cron skipped most hours on this repo, so each run now starts the next one itself at :00. The cron (now at :05) is only a backup that restarts the cycle if it ever breaks. A backup or manual run steps aside if a scan is already running, so there are never two cycles or duplicate emails.

**Alerts on a fixed clock.** The scan starts at :00 (~12–15 minutes), then the alert email goes out at :20. If a scan runs late, the alert is sent as soon as it finishes instead of skipping the hour.

**Expired postings are filtered out.** Before sending, each role's own page is checked. It's dropped (and never sent later) if the page is gone (404/410), its listed end date has passed, or it says the position is filled or no longer accepting applications.

**An email every hour.** Hours with nothing new send a short "scan finished, no new roles" email, so a missing alert always means something broke.

**Diagnosable email failures.** If sending fails, `last_run.txt` records why, and which secrets arrived (never their values).
