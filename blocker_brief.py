#!/usr/bin/env python3
"""
Standup Blocker Brief
Queries Jira for stuck in-progress tickets, asks Claude to surface blockers,
and posts a summary to Slack before standup.
"""

import os
import sys
import json
import requests
import anthropic


def get_stuck_tickets(jira_base_url: str, jira_email: str, jira_token: str) -> list[dict]:
    jql = 'project = ENG AND sprint in openSprints() AND status = "In Progress" AND updated <= -3d'
    url = f"{jira_base_url}/rest/api/3/search"
    response = requests.get(
        url,
        params={
            "jql": jql,
            "fields": "summary,assignee,status,priority,comment,labels,updated",
            "maxResults": 50,
        },
        auth=(jira_email, jira_token),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    issues = response.json().get("issues", [])
    return [
        {
            "key": issue["key"],
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
    comments = comment_field.get("comments", [])
    if not comments:
        return "(no comments)"
    last = comments[-1]
    author = last.get("author", {}).get("displayName", "Unknown")
    # comment body is Atlassian Document Format; grab plain text from top-level content
    body = last.get("body", {})
    text = _extract_adf_text(body)
    return f"{author}: {text[:300]}"


def _extract_adf_text(node: dict) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_extract_adf_text(child) for child in node.get("content", []))


def detect_blockers(tickets: list[dict], anthropic_api_key: str) -> str:
    if not tickets:
        return "No stuck tickets found in the current sprint."

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

Format your response as a bulleted list grouped by urgency (🔴 Blocked / 🟡 At Risk / 🟢 Needs Nudge).
Keep the total response under 400 words. Be direct — this goes straight to the engineering team."""

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def post_to_slack(webhook_url: str, brief: str, ticket_count: int) -> None:
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


def main() -> None:
    jira_base_url = os.environ["JIRA_BASE_URL"]        # e.g. https://yourorg.atlassian.net
    jira_email = os.environ["JIRA_EMAIL"]
    jira_token = os.environ["JIRA_API_TOKEN"]
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
