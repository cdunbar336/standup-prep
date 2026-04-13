# Standup Blocker Brief — AI Agent

An automated agent that queries your Jira sprint board every weekday morning, uses Claude AI to identify stuck or blocked tickets, and delivers a prioritized brief to Slack before standup starts.

**No manual exports. No context-switching. Just a brief waiting for you.**

---

## How it works

```
┌─────────────┐     JQL query      ┌────────────┐     prompt + tickets     ┌────────────────┐
│  GitHub     │ ─────────────────► │  Jira API  │ ──────────────────────► │  Claude API    │
│  Actions    │                    │            │                          │  (Anthropic)   │
│  (cron)     │                    └────────────┘                          └───────┬────────┘
└─────────────┘                                                                    │
                                                                         blocker brief
                                                                                    │
                                                                           ┌────────▼────────┐
                                                                           │   Slack webhook  │
                                                                           └─────────────────┘
```

The agent follows a **Fetch → Reason → Deliver** pattern:

| Step | What happens |
|------|-------------|
| **Fetch** | Jira REST API is queried with JQL: tickets currently "In Progress" that haven't been updated in 3+ days |
| **Reason** | Those tickets are passed to Claude with a prompt that assigns it the role of a technical program manager — it classifies each ticket as Blocked, At Risk, or Needs Nudge and suggests a standup question |
| **Deliver** | Claude's output is formatted with Slack Block Kit and posted to your channel via an incoming webhook |

The script runs on a `cron: '30 8 * * 1-5'` schedule in GitHub Actions. All secrets live in GitHub Secrets — nothing is hardcoded.

---

## Example Slack output

```
📋 Standup Blocker Brief
4 ticket(s) stuck 3+ days in current sprint
────────────────────────────────────────────

🔴 Blocked
• ENG-412 — Migrate auth middleware to JWT
  Waiting on security review from @platform team. Last comment 5 days ago asking
  for sign-off. → Ask: does the platform team have an ETA on the security review?

🟡 At Risk
• ENG-398 — Add pagination to /orders endpoint
  No comments, no updates. Assigned to someone who is also on ENG-401. → Ask:
  is this blocked behind ENG-401 or can it move independently?

🟢 Needs Nudge
• ENG-405 — Update Sentry error grouping rules
• ENG-389 — Refactor notification service tests
  Both appear low-priority and simply haven't been touched. Worth a quick status check.
```

---

## Setup

### Prerequisites

- A Jira Cloud account with API access
- An [Anthropic API key](https://console.anthropic.com)
- A Slack workspace where you can create an app

### 1. Fork or clone this repo

```bash
git clone https://github.com/cdunbar336/standup-prep.git
cd standup-prep
```

### 2. Create a Slack incoming webhook

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Under **Features**, select **Incoming Webhooks** and activate it
3. Click **Add New Webhook to Workspace**, select your standup channel
4. Copy the webhook URL — you'll need it in the next step

### 3. Get your Jira API token

1. Go to [id.atlassian.com](https://id.atlassian.com) → **Security** → **Create and manage API tokens**
2. Create a token and copy it

### 4. Add GitHub Secrets

In your forked repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `JIRA_BASE_URL` | `https://yourorg.atlassian.net` |
| `JIRA_EMAIL` | Your Atlassian login email |
| `JIRA_API_TOKEN` | The token from step 3 |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `SLACK_WEBHOOK_URL` | The webhook URL from step 2 |

### 5. Adjust the JQL for your project

In `blocker_brief.py`, find this line and update `ENG` to your Jira project key:

```python
jql = 'project = ENG AND sprint in openSprints() AND status = "In Progress" AND updated <= -3d'
```

### 6. Set your timezone

The workflow runs at `30 8 * * 1-5` (8:30 AM UTC). Update `.github/workflows/blocker_brief.yml` to match your team's timezone:

| Timezone | Cron for 8:30 AM |
|---|---|
| UTC | `30 8 * * 1-5` |
| US Eastern (EST) | `30 13 * * 1-5` |
| US Eastern (EDT) | `30 12 * * 1-5` |
| US Pacific (PST) | `30 16 * * 1-5` |
| US Pacific (PDT) | `30 15 * * 1-5` |

### 7. Test it manually

Before waiting for the scheduled run, trigger it manually:

1. Go to your repo on GitHub → **Actions** tab
2. Select **Standup Blocker Brief** → **Run workflow**

---

## Running locally

```bash
pip install -r requirements.txt

export JIRA_BASE_URL="https://yourorg.atlassian.net"
export JIRA_EMAIL="you@yourorg.com"
export JIRA_API_TOKEN="your-token"
export ANTHROPIC_API_KEY="sk-ant-..."
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."

python blocker_brief.py
```

---

## Customizing the prompt

The blocker detection logic lives in the `detect_blockers()` function in `blocker_brief.py`. The prompt sent to Claude is easy to modify — you can:

- Change the urgency categories
- Ask Claude to tag specific people
- Request output in a different format (e.g., a numbered list, or grouped by assignee)
- Add additional context like sprint goals or team capacity

```python
prompt = f"""You are a technical program manager reviewing the engineering sprint board before standup.
# ↑ Change the role to match your workflow
...
Format your response as a bulleted list grouped by urgency:
# ↑ Change the format to whatever your team prefers
```

---

## Extending this pattern

This repo is intentionally minimal — a clean example of the **Fetch → Reason → Deliver** agentic pattern. The same structure can be adapted to:

- Query GitHub instead of Jira (use PyGithub or the GitHub REST API)
- Deliver to email instead of Slack (use SendGrid or SES)
- Run on a different trigger (PR merge, Slack slash command, etc.)
- Add memory by storing previous briefs and asking Claude to identify trends over time

---

## Project structure

```
standup-prep/
├── blocker_brief.py               # Main agent script (fetch → reason → deliver)
├── requirements.txt               # Python dependencies
└── .github/
    └── workflows/
        └── blocker_brief.yml      # GitHub Actions schedule
```

---

## Dependencies

- [`anthropic`](https://pypi.org/project/anthropic/) — Anthropic Python SDK for Claude API
- [`requests`](https://pypi.org/project/requests/) — HTTP client for Jira and Slack APIs

---

## License

MIT
