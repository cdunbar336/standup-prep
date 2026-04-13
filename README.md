# Standup Prep — AI Agent Suite

Two automated agents that run every weekday morning and deliver everything you need for standup — before you open a single tab.

**Agent 1 — Blocker Brief** (`8:30 AM`): Queries your Jira sprint board, asks Claude to identify stuck tickets, and posts a prioritized blocker brief to Slack.

**Agent 2 — Thread Summary** (`8:45 AM`): Reads your Slack channels, asks Claude to surface decisions, action items, and open questions, and posts a digest before the meeting starts.

No manual exports. No context-switching. No laptop required to run them.

---

## How it works

Both agents follow the same **Fetch → Reason → Deliver** pattern. The data source and output differ; the architecture is identical.

```
                          ┌──────────────────────────────────────────────┐
                          │             GitHub Actions (cron)             │
                          │                                               │
                          │   8:30 AM → blocker_brief.py                 │
                          │   8:45 AM → thread_summary.py                │
                          └──────────┬───────────────────────────────────┘
                                     │
                    ┌────────────────┴─────────────────┐
                    │                                  │
              FETCH (pull data)                  FETCH (pull data)
                    │                                  │
           ┌────────▼────────┐               ┌────────▼────────┐
           │    Jira API     │               │    Slack API    │
           │  (JQL query)    │               │ (channel msgs)  │
           └────────┬────────┘               └────────┬────────┘
                    │                                  │
              REASON (analyze)                   REASON (analyze)
                    │                                  │
           ┌────────▼──────────────────────────────────▼────────┐
           │                   Claude API                        │
           │  blocker detection prompt │ summarization prompt    │
           └────────┬──────────────────────────────────┬────────┘
                    │                                  │
             DELIVER (post)                     DELIVER (post)
                    │                                  │
           ┌────────▼──────────────────────────────────▼────────┐
           │                  Slack webhook                      │
           └─────────────────────────────────────────────────────┘
```

---

## Example output

### Agent 1 — Blocker Brief (8:30 AM)

```
📋 Standup Blocker Brief
4 ticket(s) stuck 3+ days in current sprint
────────────────────────────────────────────

🔴 Blocked
• ENG-412 — Migrate auth middleware to JWT
  Waiting on security review from @platform team. Last comment 5 days ago.
  → Ask: does the platform team have an ETA on the security review?

🟡 At Risk
• ENG-398 — Add pagination to /orders endpoint
  No comments, no updates. Assigned to someone also on ENG-401.
  → Ask: is this blocked behind ENG-401 or can it move independently?

🟢 Needs Nudge
• ENG-405 — Update Sentry error grouping rules
• ENG-389 — Refactor notification service tests
  Both appear stale but low-priority. Worth a quick status check.
```

### Agent 2 — Thread Summary (8:45 AM)

```
📨 Async Thread Summary
Last 18 hours across #eng-general, #incidents, #releases
──────────────────────────────────────────────────────────

## #incidents
**Key decisions:** Agreed to roll back the caching layer change; deploy
  scheduled for 7 AM.
**Action items:** @maya to monitor error rates post-rollback and post
  a status update by 9 AM.
**Open questions:** Root cause still unclear — was it the TTL change or
  the new eviction policy?

## #releases
**FYI:** v2.4.1 shipped cleanly at 11 PM. No issues reported overnight.

## #eng-general
Low signal — a few async questions, no decisions or action items.
```

---

## Setup

### Prerequisites

- A Jira Cloud account with API access (for Agent 1)
- A Slack workspace where you can create apps
- An [Anthropic API key](https://console.anthropic.com)

### 1. Fork or clone this repo

```bash
git clone https://github.com/cdunbar336/standup-prep.git
cd standup-prep
```

### 2. Create a Slack app for reading messages (Agent 2)

Agent 2 needs to read channel history, which requires a bot token — a webhook URL alone is write-only.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Under **OAuth & Permissions**, add these Bot Token Scopes:
   - `channels:history` — read messages in public channels
   - `channels:read` — list channels to resolve names to IDs
   - `users:read` — look up display names
3. Click **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-...`)
4. Invite the bot to each channel you want it to monitor: `/invite @your-bot-name`

### 3. Create a Slack incoming webhook (both agents)

Both agents post their output via an incoming webhook — simpler than OAuth for writing.

1. In your Slack app (same one or a separate one), go to **Incoming Webhooks** → activate
2. **Add New Webhook to Workspace** → select your standup channel
3. Copy the webhook URL

### 4. Get your Jira API token (Agent 1)

1. Go to [id.atlassian.com](https://id.atlassian.com) → **Security** → **Create and manage API tokens**
2. Create a token and copy it

### 5. Add GitHub Secrets

In your forked repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Used by | Value |
|---|---|---|
| `JIRA_BASE_URL` | Agent 1 | `https://yourorg.atlassian.net` |
| `JIRA_EMAIL` | Agent 1 | Your Atlassian login email |
| `JIRA_API_TOKEN` | Agent 1 | Token from step 4 |
| `SLACK_BOT_TOKEN` | Agent 2 | `xoxb-...` token from step 2 |
| `SLACK_CHANNELS` | Agent 2 | Comma-separated channel names, e.g. `eng-general,incidents` |
| `ANTHROPIC_API_KEY` | Both | Your Anthropic API key |
| `SLACK_WEBHOOK_URL` | Both | Webhook URL from step 3 |

### 6. Adjust Agent 1 for your Jira project

In `blocker_brief.py`, update `ENG` to your Jira project key:

```python
jql = 'project = ENG AND sprint in openSprints() AND status = "In Progress" AND updated <= -3d'
```

### 7. Set your timezone

Both workflows run in UTC. Update the cron expressions in `.github/workflows/` to match your standup time:

| Timezone | 8:30 AM | 8:45 AM |
|---|---|---|
| UTC | `30 8 * * 1-5` | `45 8 * * 1-5` |
| US Eastern (EST) | `30 13 * * 1-5` | `45 13 * * 1-5` |
| US Eastern (EDT) | `30 12 * * 1-5` | `45 12 * * 1-5` |
| US Pacific (PST) | `30 16 * * 1-5` | `45 16 * * 1-5` |
| US Pacific (PDT) | `30 15 * * 1-5` | `45 15 * * 1-5` |

### 8. Test manually

Before waiting for the schedule, trigger each workflow manually:

1. Go to your repo → **Actions** tab
2. Select **Standup Blocker Brief** or **Async Thread Summary** → **Run workflow**

---

## Running locally

```bash
pip install -r requirements.txt

# Agent 1 — Blocker Brief
export JIRA_BASE_URL="https://yourorg.atlassian.net"
export JIRA_EMAIL="you@yourorg.com"
export JIRA_API_TOKEN="your-jira-token"
export ANTHROPIC_API_KEY="sk-ant-..."
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
python blocker_brief.py

# Agent 2 — Thread Summary
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_CHANNELS="eng-general,incidents"
export ANTHROPIC_API_KEY="sk-ant-..."
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
python thread_summary.py
```

---

## Customizing the prompts

Each agent's reasoning lives in a single function with a clearly marked prompt:

| Agent | Function | What to change |
|---|---|---|
| Blocker Brief | `detect_blockers()` in `blocker_brief.py` | Urgency categories, output format, tone |
| Thread Summary | `summarize_channels()` in `thread_summary.py` | Lookback window, summary sections, level of detail |

Both prompts use role assignment ("You are a TPM / chief of staff...") to anchor Claude's perspective, and explicit format instructions to keep output scannable. Adjust either to match your team's preferences.

---

## Extending this pattern

These agents are intentionally minimal — clean examples of the Fetch → Reason → Deliver pattern. The same structure can be adapted to:

- **Different sources**: GitHub PRs, Linear tickets, PagerDuty alerts, Google Docs
- **Different outputs**: email via SendGrid, a shared Notion doc, a dashboard
- **Different triggers**: a Slack slash command, a PR merge event, an on-call rotation change
- **Memory**: store previous briefs, ask Claude to identify recurring blockers or trends over time

---

## Project structure

```
standup-prep/
├── blocker_brief.py               # Agent 1: Jira → Claude → Slack (8:30 AM)
├── thread_summary.py              # Agent 2: Slack → Claude → Slack (8:45 AM)
├── requirements.txt               # Python dependencies
└── .github/
    └── workflows/
        ├── blocker_brief.yml      # Cron schedule for Agent 1
        └── thread_summary.yml     # Cron schedule for Agent 2
```

---

## Dependencies

- [`anthropic`](https://pypi.org/project/anthropic/) — Anthropic Python SDK for Claude API
- [`requests`](https://pypi.org/project/requests/) — HTTP client for Jira, Slack, and webhook calls

---

## License

MIT
