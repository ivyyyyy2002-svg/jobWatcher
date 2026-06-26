# jobwatch Deployment Guide (GitHub Actions, Free)

After setup, your computer can be turned off. GitHub Actions will run the watcher on GitHub's servers.

- **Regular alerts, every 30 minutes**: checks jobs posted in the last 1 hour. For sources with precise posting times, such as company ATS boards and community feeds, the watcher sends only newly posted jobs inside the current window. If there are no new jobs, it still sends a short status message. Sources that only provide a date, without a precise time, are skipped in regular alerts so old jobs are not treated as fresh.
- **Daily digest, around midnight to 1 AM Eastern time**: sends a 24-hour summary of all matching jobs from the day, grouped in a cleaner format for review.

Messages include posting time or age when available, with the newest jobs first. Each posting is separated clearly, and links are left as plain URLs so Discord can show its own preview card when available.

Current filtering rules:

- Location must be in Canada. Remote roles are allowed only when the posting clearly supports Canada.
- Intern / co-op roles must fit Fall 2026, September-December, or a 4-month term. Internships explicitly listed as 6, 8, 12, 16 months, or year-long are skipped.
- New grad / entry-level / junior roles are allowed if they are for 2026 or can start in September or later.
- Jobs that explicitly require Canadian citizenship are skipped.
- Jobs with a hard French or bilingual French requirement are skipped.
- Senior, staff, principal, lead, manager, director, architect, executive, VP, and similar senior-level roles are skipped.
- PhD / doctorate-only roles are skipped.
- Jobs with hard requirements for clearly unrelated majors, such as accounting, nursing, mechanical engineering, civil engineering, or chemical engineering, are skipped.
- If the title is vague but the description looks relevant and has no hard blocker, the job can still be sent with a note such as `term/duration not explicit; please verify`.
- Sources include company ATS boards, community feeds, LinkedIn, and Indeed. The search terms are intentionally broad, while the filtering rules decide what actually gets sent.

Precision note:

Sources with minute-level timestamps are checked against the alert window. LinkedIn or other sources that only show a date cannot prove that a job was posted inside the last hour, so they are skipped in regular alerts to avoid noisy old posts. The first run establishes the baseline; later runs only send new matches that were not already sent before.

---

## Files

- `jobwatch.py` - main watcher script
- `jobwatch.yml` - GitHub Actions workflow configuration
- `requirements.txt` - Python dependencies

---

## Step 1: Create a Discord Webhook

A webhook is a private URL that can post messages into one Discord channel. You do not need to build a Discord bot.

1. Open Discord. If you do not have your own server, click `+` on the left and create a private server for yourself.
2. Hover over the text channel where you want job alerts, then click the gear icon to edit the channel. You can also right-click the channel and choose `Edit Channel`.
3. Go to `Integrations` -> `Webhooks` -> `New Webhook`.
4. Click `Copy Webhook URL`.

Save this value as `DISCORD_WEBHOOK`.

Warning: anyone who has this URL can post messages into your channel. Do not commit it into code or paste it publicly. Store it in GitHub Secrets instead.

---

## Step 2: Create a GitHub Repository

1. Log in to github.com.
2. Click `+` in the top-right corner, then choose `New repository`.
3. Choose a name, for example `jobwatch`.
4. Select **Private**.
5. Check `Add a README file`.
6. Click `Create repository`.

---

## Step 3: Upload the Files

In the repository page:

1. Click `Add file` -> `Upload files`.
2. Upload `jobwatch.py` and `requirements.txt`.
3. Click `Commit changes`.
4. Create the workflow file:
   - Click `Add file` -> `Create new file`.
   - In the filename field, enter `.github/workflows/jobwatch.yml`.
   - Paste the content of `jobwatch.yml`.
   - Click `Commit changes`.

Your repository should look like this:

```text
jobwatch.py
requirements.txt
.github/workflows/jobwatch.yml
```

---

## Step 4: Add the Discord Webhook Secret

Do not put the webhook URL directly in the code.

1. Go to your repository.
2. Open `Settings` -> `Secrets and variables` -> `Actions`.
3. Click `New repository secret`.
4. Set:
   - Name: `DISCORD_WEBHOOK`
   - Secret: the Discord webhook URL from Step 1
5. Click `Add secret`.

That is the only secret required.

---

## Step 5: Run the Workflow Once Manually

The first run establishes the baseline. It may send a small batch of recent matching jobs. After that, the watcher uses `seen_jobs.db` to avoid sending duplicates.

1. Open the `Actions` tab in your repository.
2. If GitHub asks you to enable workflows, click `I understand my workflows, go ahead and enable them`.
3. Select the `jobwatch` workflow on the left.
4. Click `Run workflow`.
5. Choose `alert`, then click `Run workflow` again.
6. Wait 1-2 minutes, refresh the page, and open the run logs.

If you see a line like `Fetched N jobs, M new`, the workflow is working. Your Discord channel should also receive the alert message.

After the first successful run, the repository may contain a `seen_jobs.db` file. This file stores jobs that were already sent. Do not delete it unless you intentionally want to reset deduplication.

---

## Step 6: Confirm Automatic Runs

The workflow is configured to run every 30 minutes. Your computer does not need to stay on.

To verify it, wait for the next scheduled run in the `Actions` tab. You can also trigger `Run workflow` manually at any time.

---

## Backup Option: Trigger the Workflow with cron-job.org

If GitHub's built-in `schedule` trigger is unreliable, use an external scheduler to call the GitHub API every 30 minutes. This is equivalent to clicking `Run workflow` automatically.

### 1. Create a GitHub Fine-Grained Token

1. Open GitHub.
2. Click your avatar -> `Settings`.
3. Go to `Developer settings` -> `Personal access tokens` -> `Fine-grained tokens`.
4. Click `Generate new token`.
5. Under repository access, choose `Only select repositories`.
6. Select only your `jobWatcher` repository.
7. Under repository permissions, set `Actions` to `Read and write`.
8. Generate the token and copy it immediately.

Do not paste the token into code, README files, Discord, or public places.

### 2. Create the cron-job.org Job

1. Open `https://cron-job.org`, create an account, and log in.
2. Click `Create cronjob`.
3. Enable the job.
4. Set the schedule to every 30 minutes.
5. Set the URL to:

```text
https://api.github.com/repos/ivyyyyy2002-svg/jobWatcher/actions/workflows/jobwatch.yml/dispatches
```

6. Set request method to `POST`.
7. Set request body to:

```json
{"ref":"main","inputs":{"mode":"alert"}}
```

8. Add these headers:

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_GITHUB_TOKEN
X-GitHub-Api-Version: 2026-03-10
Content-Type: application/json
```

Replace `YOUR_GITHUB_TOKEN` with the token you created.

### 3. Test It

After saving the cronjob, click the test run button. A successful test should show a 2xx response, and GitHub Actions should show a new `workflow_dispatch` run.

After that, cron-job.org will trigger the alert workflow every 30 minutes even if GitHub's own schedule is delayed or skipped.

---

## Common Adjustments

**Change the regular alert window**

Open `jobwatch.py` and change `ALERT_WINDOW_MINUTES`.

The current default is:

```python
ALERT_WINDOW_MINUTES = 60
```

This means regular alerts check the last 1 hour. Deduplication still prevents jobs from being sent twice.

**Change the daily digest time**

Open `.github/workflows/jobwatch.yml` and find:

```yaml
- cron: "0 5 * * *"
```

GitHub cron times use UTC. `0 5 * * *` usually lands around midnight or 1 AM Eastern time, depending on daylight saving time.

The digest lookback window is controlled in `jobwatch.py`:

```python
DIGEST_LOOKBACK_HOURS = 24
```

**Change the alert frequency**

Open `.github/workflows/jobwatch.yml` and find the regular alert cron:

```yaml
- cron: "7,37 * * * *"
```

This runs at minute `07` and `37` of every hour, which is every 30 minutes.

If you use cron-job.org, the equivalent custom crontab expression is:

```text
*/30 * * * *
```

Running more often than every 15-30 minutes is usually not useful because most job boards do not update that quickly.

**Manually test a mode**

Open the `Actions` tab, select `jobwatch`, click `Run workflow`, and choose:

- `alert` for the regular alert
- `digest` for the daily summary

**Add companies**

Open `jobwatch.py` and add companies to the matching list:

- Greenhouse: add the board slug to `GREENHOUSE_COMPANIES`
- Lever: add the company slug to `LEVER_COMPANIES`
- Ashby: add the board slug to `ASHBY_COMPANIES`
- Workday: add a tuple to `WORKDAY_COMPANIES`

For Greenhouse, the slug is the last part of the board URL. For example:

```text
https://boards.greenhouse.io/stripe
```

The slug is `stripe`.

**Change search keywords or location**

Open the config area in `jobwatch.py`.

- `LINKEDIN_QUERIES` and `INDEED_QUERIES` control broad search queries.
- `ROLE_RE` controls the role keywords.
- `LOCATION_INCLUDE` controls the Canada location whitelist.
- The reject keyword lists control hard blockers such as seniority, citizenship, French requirements, unrelated majors, and long internship durations.

The broad search queries are intentionally loose. The filter rules should be the strict part.

**Pause alerts**

Open the `Actions` tab and disable the workflow. You can enable it again later.

---

## Troubleshooting

**Discord did not receive a message**

- Confirm the GitHub secret is named exactly `DISCORD_WEBHOOK`.
- Confirm the secret value is the full Discord webhook URL.
- Confirm the Discord channel and webhook still exist.
- Open the GitHub Actions run logs and search for `[discord]`.

**Some sources fail in the logs**

This can happen because job boards rate-limit, block scraping, or change their pages. The script should continue with the other sources.

**The alert says 0 new postings for a long time**

Check the GitHub Actions logs. The summary should show how many candidates were fetched, how many had usable times inside the window, how many were duplicates, and how many were filtered. If the candidate count is healthy but usable-time count is low, the issue is likely source timestamp precision rather than the watcher being broken.

**You want to test locally without sending Discord messages**

In `jobwatch.py`, temporarily change:

```python
NOTIFY = "discord"
```

to:

```python
NOTIFY = "print"
```

Then run:

```bash
python3 jobwatch.py
```
