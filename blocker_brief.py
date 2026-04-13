#!/usr/bin/env python3
"""
Standup Blocker Brief — AI Agent
=================================
This script is the core of an automated standup prep agent. It runs every
weekday morning via GitHub Actions and does three things:

  1. FETCH  — queries Jira for tickets that are "In Progress" but haven't
              been updated in 3+ days (i.e., potentially stuck)

  2. REASON — sends those tickets to Claude (Anthropic API) with a prompt
              that asks it to act as a technical program manager and classify
              each ticket by urgency

  3. DELIVER — posts Claude's blocker brief to a Slack channel via webhook

No human involvement required. The agent runs, reasons, and reports on its own.

All secrets (API keys, webhook URLs) are injected as environment variables
from GitHub Secrets — nothing sensitive is stored in the code.
"""

import os
import sys
import requests
import anthropic


# ─────────────────────────────────────────────
# STEP 1: FETCH — Pull stuck tickets from Jira
# ─────────────────────────────────────────────

def get_stuck_tickets(jira_base_url: str, jira_email: str, jira_token: str) -> list[dict]:
    """
    Query Jira using JQL (Jira Query Language) for tickets that appear stuck.

    The query finds tickets that are:
      - In the current active sprint
      - Currently "In Progress" (not Done, not To Do)
      - Not updated in the last 3 days

    Jira's REST API returns results in Atlassian's JSON format. We extract
    only the fields we need for Claude's analysis.
    """
    jql = 'project = ENG AND sprint in openSprints() AND status = "In Progress" AND updated <= -3d'

    # Jira REST API v3 search endpoint
    url = f"{jira_base_url}/rest/api/3/search"

    response = requests.get(
        url,
        params={
            "jql": jql,
            "fields": "summary,assignee,status,priority,comment,labels,updated",
            "maxResults": 50,
        },
        # Jira uses Basic Auth: email + API token (not your password)
        auth=(jira_email, jira_token),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()

    issues = response.json().get("issues", [])

    # Normalize each issue into a flat dict that's easy to format into a prompt
    return [
        {
            "key": issue["key"],                          # e.g. ENG-412
            "summary": issue["fields"]["summary"],
            "assignee": (issue["fields"]["assignee"] or {}).get("displayName", "Unassigned"),
            "priority": issue["fields"]["priority"]["name"],
            "updated": issue["fields"]["updated"],
            "labels": issue["fields"].get("labels", []),
            "last_comment": _last_comment(issue["fields"].get("comment", {})),
        }
        for issue in issues
    ]


def _last_comment(comment_field: dict) -> str:
    """Extract the most recent comment text from a Jira issue."""
    comments = comment_field.get("comments", [])
    if not comments:
        return "(no comments)"
    last = comments[-1]
    author = last.get("author", {}).get("displayName", "Unknown")
    # Jira stores comment bodies in Atlassian Document Format (ADF), a nested
    # JSON structure. We recursively extract the plain text nodes from it.
    text = _extract_adf_text(last.get("body", {}))
    return f"{author}: {text[:300]}"


def _extract_adf_text(node: dict) -> str:
    """
    Recursively walk Atlassian Document Format (ADF) JSON and collect plain text.

    ADF is a tree of nodes. Leaf nodes with type "text" contain the actual
    string content. Everything else is structural (paragraphs, lists, etc.).
    """
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_extract_adf_text(child) for child in node.get("content", []))


# ──────────────────────────────────────────────────────────────────────
# STEP 2: REASON — Ask Claude to analyze the tickets and surface blockers
# ──────────────────────────────────────────────────────────────────────

def detect_blockers(tickets: list[dict], anthropic_api_key: str) -> str:
    """
    This is where the "agent" thinking happens.

    We give Claude:
      - A role: technical program manager reviewing the sprint board
      - Context: the list of stuck tickets with their details
      - A task: classify each ticket by urgency and suggest a standup action

    The prompt is designed to produce structured, actionable output that an
    engineering team can scan in under 60 seconds.

    Prompt engineering notes:
      - Assigning a role ("You are a TPM...") improves relevance
      - Explicit output format instructions reduce variance in responses
      - A word limit keeps the output scannable rather than verbose
    """
    if not tickets:
        return "No stuck tickets found in the current sprint. All clear!"

    # Format ticket data as readable text to include in the prompt.
    # Plain text works well here — Claude doesn't need structured input
    # to reason about structured data.
    ticket_text = "\n\n".join(
        f"**{t['key']}** — {t['summary']}\n"
        f"  Assignee: {t['assignee']} | Priority: {t['priority']} | Last updated: {t['updated']}\n"
        f"  Labels: {', '.join(t['labels']) or 'none'}\n"
        f"  Last comment: {t['last_comment']}"
        for t in tickets
    )

    prompt = f"""You are a technical program manager reviewing the engineering sprint board before standup.

The following tickets have been "In Progress" without an update for 3+ days. Review them and produce a concise blocker brief:

{ticket_text}

For each ticket, identify:
1. Whether it appears genuinely blocked (dependency, unclear requirements, waiting on review, etc.) or just stale
2. A suggested action or question for standup (one sentence)

Format your response as a bulleted list grouped by urgency:
  🔴 Blocked — needs immediate attention or is waiting on someone/something
  🟡 At Risk — unclear status, may be blocked soon
  🟢 Needs Nudge — probably fine but hasn't been updated

Keep the total response under 400 words. Be direct — this goes straight to the engineering team."""

    # Initialize the Anthropic client and call the Claude API
    client = anthropic.Anthropic(api_key=anthropic_api_key)

    message = client.messages.create(
        model="claude-opus-4-6",   # Use the most capable model for nuanced reasoning
        max_tokens=600,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )

    # The response content is a list of blocks; [0] is the text block
    return message.content[0].text


# ─────────────────────────────────────────────────────
# STEP 3: DELIVER — Post the brief to Slack via webhook
# ─────────────────────────────────────────────────────

def post_to_slack(webhook_url: str, brief: str, ticket_count: int) -> None:
    """
    Send the blocker brief to Slack using Block Kit formatting.

    Slack's Block Kit lets you compose rich messages with headers, dividers,
    and context lines. The webhook URL is generated when you create an
    "Incoming Webhooks" app in Slack — it acts as a push endpoint, no OAuth needed.

    See: https://api.slack.com/messaging/webhooks
    """
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Standup Blocker Brief",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"{ticket_count} ticket(s) stuck 3+ days in current sprint",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": brief},
            },
        ]
    }

    response = requests.post(
        webhook_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    response.raise_for_status()


# ────────────────────
# Entrypoint
# ────────────────────

def main() -> None:
    """
    Orchestrate the three-step agent pipeline.

    Environment variables are set by GitHub Actions from GitHub Secrets.
    Running locally? Export them in your shell first:

        export JIRA_BASE_URL="https://yourorg.atlassian.net"
        export JIRA_EMAIL="you@yourorg.com"
        export JIRA_API_TOKEN="your-jira-api-token"
        export ANTHROPIC_API_KEY="sk-ant-..."
        export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
        python blocker_brief.py
    """
    jira_base_url    = os.environ["JIRA_BASE_URL"]
    jira_email       = os.environ["JIRA_EMAIL"]
    jira_token       = os.environ["JIRA_API_TOKEN"]
    anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    slack_webhook_url = os.environ["SLACK_WEBHOOK_URL"]

    print("Fetching stuck tickets from Jira...")
    tickets = get_stuck_tickets(jira_base_url, jira_email, jira_token)
    print(f"Found {len(tickets)} stuck ticket(s).")

    print("Asking Claude to detect blockers...")
    brief = detect_blockers(tickets, anthropic_api_key)
    print("Brief generated.")

    print("Posting to Slack...")
    post_to_slack(slack_webhook_url, brief, len(tickets))
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
