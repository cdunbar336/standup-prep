#!/usr/bin/env python3
"""
Standup Prep — Combined AI Agent
==================================
Orchestrates both agents in the suite and delivers a single Slack post
before standup starts:

  ┌─────────────────────────────────────────┐
  │         Morning Standup Brief            │
  ├─────────────────────────────────────────┤
  │  📋 Blocker Brief                        │
  │  (stuck Jira tickets, classified by      │
  │   urgency with suggested standup Qs)     │
  ├─────────────────────────────────────────┤
  │  📨 Async Thread Summary                 │
  │  (decisions, action items, open Qs       │
  │   from Slack channels overnight)         │
  └─────────────────────────────────────────┘

Both agents run in sequence, their outputs are combined, and the result
is delivered as one message — so the team sees everything in one place.

Secrets required (all injected from GitHub Secrets, nothing hardcoded):
  JIRA_BASE_URL      — e.g. https://yourorg.atlassian.net
  JIRA_EMAIL         — your Atlassian login email
  JIRA_API_TOKEN     — from id.atlassian.com → Security → API tokens
  SLACK_BOT_TOKEN    — xoxb-... token with channels:history, channels:read, users:read
  SLACK_CHANNELS     — comma-separated channel names, e.g. "eng-general,incidents"
  ANTHROPIC_API_KEY  — from console.anthropic.com
  SLACK_WEBHOOK_URL  — incoming webhook URL for the standup channel
"""

import os
import sys
import time
import requests
import anthropic


# ══════════════════════════════════════════════════════
# AGENT 1: BLOCKER BRIEF
# Source: Jira REST API
# ══════════════════════════════════════════════════════

def get_stuck_tickets(jira_base_url: str, jira_email: str, jira_token: str) -> list[dict]:
    """
    Query Jira for tickets that are "In Progress" but haven't been updated
    in 3+ days. These are candidates for being blocked or stale.

    Uses JQL (Jira Query Language) — the same query language you'd type
    into Jira's search bar. Customize the project key and time window here.
    """
    jql = 'project = ENG AND sprint in openSprints() AND status = "In Progress" AND updated <= -3d'

    response = requests.get(
        f"{jira_base_url}/rest/api/3/search",
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
        for issue in response.json().get("issues", [])
    ]


def _last_comment(comment_field: dict) -> str:
    comments = comment_field.get("comments", [])
    if not comments:
        return "(no comments)"
    last = comments[-1]
    author = last.get("author", {}).get("displayName", "Unknown")
    text = _extract_adf_text(last.get("body", {}))
    return f"{author}: {text[:300]}"


def _extract_adf_text(node: dict) -> str:
    """Recursively extract plain text from Atlassian Document Format (ADF)."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_extract_adf_text(child) for child in node.get("content", []))


def detect_blockers(tickets: list[dict], client: anthropic.Anthropic) -> str:
    """
    Pass the stuck ticket list to Claude with a role-assigned prompt.
    Claude classifies each ticket as Blocked, At Risk, or Needs Nudge
    and suggests a standup question for each.
    """
    if not tickets:
        return "No stuck tickets in the current sprint."

    ticket_text = "\n\n".join(
        f"**{t['key']}** — {t['summary']}\n"
        f"  Assignee: {t['assignee']} | Priority: {t['priority']} | Last updated: {t['updated']}\n"
        f"  Labels: {', '.join(t['labels']) or 'none'}\n"
        f"  Last comment: {t['last_comment']}"
        for t in tickets
    )

    prompt = f"""You are a technical program manager reviewing the engineering sprint board before standup.

The following tickets have been "In Progress" without an update for 3+ days:

{ticket_text}

For each ticket, identify whether it's genuinely blocked or just stale, and suggest one standup question.

Format as a bulleted list grouped by urgency:
  🔴 Blocked — waiting on someone or something external
  🟡 At Risk — unclear status, may be blocked soon
  🟢 Needs Nudge — probably fine, just hasn't been updated

Keep the total under 300 words. Be direct."""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ══════════════════════════════════════════════════════
# AGENT 2: THREAD SUMMARY
# Source: Slack API
# ══════════════════════════════════════════════════════

def get_channel_id(channel_name: str, bot_token: str) -> str | None:
    """
    Resolve a channel name to its Slack ID by paginating conversations.list.
    Slack's API requires IDs, not names, for history calls.
    """
    headers = {"Authorization": f"Bearer {bot_token}"}
    cursor = None

    while True:
        params = {"limit": 200, "exclude_archived": True}
        if cursor:
            params["cursor"] = cursor

        data = requests.get(
            "https://slack.com/api/conversations.list",
            headers=headers,
            params=params,
            timeout=15,
        ).json()

        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error')}")

        for channel in data.get("channels", []):
            if channel["name"] == channel_name.lstrip("#"):
                return channel["id"]

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return None


def fetch_recent_messages(channel_id: str, bot_token: str, lookback_hours: int = 18) -> list[dict]:
    """
    Fetch messages from the last N hours. Default 18h captures the async
    tail of the prior workday plus any overnight activity.
    Bot messages and system events (joins, leaves) are filtered out.
    """
    oldest = str(time.time() - (lookback_hours * 3600))

    data = requests.get(
        "https://slack.com/api/conversations.history",
        headers={"Authorization": f"Bearer {bot_token}"},
        params={"channel": channel_id, "oldest": oldest, "limit": 200},
        timeout=15,
    ).json()

    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")

    messages = [
        m for m in data.get("messages", [])
        if m.get("type") == "message"
        and not m.get("bot_id")
        and m.get("subtype") is None
    ]
    return list(reversed(messages))  # chronological order for the prompt


def resolve_user_names(messages: list[dict], bot_token: str) -> dict[str, str]:
    """Batch-resolve Slack user IDs to display names for readable transcripts."""
    names = {}
    for uid in {m["user"] for m in messages if "user" in m}:
        try:
            data = requests.get(
                "https://slack.com/api/users.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"user": uid},
                timeout=10,
            ).json()
            if data.get("ok"):
                profile = data["user"].get("profile", {})
                names[uid] = profile.get("display_name") or profile.get("real_name") or uid
        except Exception:
            names[uid] = uid
    return names


def format_transcript(messages: list[dict], user_names: dict[str, str], channel_name: str) -> str:
    """Format messages as a readable [HH:MM] Name: text transcript."""
    if not messages:
        return f"(no messages in #{channel_name} in the last 18 hours)"

    lines = []
    for m in messages:
        ts = float(m.get("ts", 0))
        time_str = time.strftime("%H:%M", time.localtime(ts))
        name = user_names.get(m.get("user", ""), "Unknown")
        text = m.get("text", "").strip()
        if text:
            lines.append(f"[{time_str}] {name}: {text}")

    return "\n".join(lines)


def summarize_channels(channel_transcripts: dict[str, str], client: anthropic.Anthropic) -> str:
    """
    Send all channel transcripts to Claude and ask for a structured summary.
    Output is organized by channel with sections for decisions, action items,
    open questions, and FYIs.
    """
    all_quiet = all("(no messages" in t for t in channel_transcripts.values())
    if all_quiet:
        return "All monitored channels were quiet in the last 18 hours."

    sections = "\n\n".join(
        f"--- #{channel} ---\n{transcript}"
        for channel, transcript in channel_transcripts.items()
    )

    prompt = f"""You are a chief of staff preparing a standup briefing for an engineering team.

Below are Slack channel transcripts from the last 18 hours. For each channel with meaningful activity, surface:
1. **Key decisions made**
2. **Action items** (and who owns them)
3. **Open questions** — raised but not yet answered
4. **FYIs** — important context, no action needed

Skip channels with no meaningful activity or summarize in one sentence.

{sections}

Use the channel name as a header (e.g. ## #eng-general), then bullet points per category. Omit empty categories. Keep the whole summary under 400 words."""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ══════════════════════════════════════════════════════
# DELIVER: Post combined brief to Slack
# ══════════════════════════════════════════════════════

def post_combined_brief(
    webhook_url: str,
    blocker_brief: str,
    thread_summary: str,
    ticket_count: int,
    channels_monitored: list[str],
) -> None:
    """
    Build a single Slack Block Kit message with both agent outputs.

    Layout:
      [Header]   Morning Standup Brief
      [Section]  📋 Blocker Brief  (from Jira)
      [Divider]
      [Section]  📨 Async Thread Summary  (from Slack)

    Using Block Kit lets us add headers, dividers, and context lines
    rather than sending a wall of unformatted text.
    """
    channel_list = ", ".join(f"#{c}" for c in channels_monitored)

    payload = {
        "blocks": [
            # ── Header ──
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Morning Standup Brief"},
            },
            # ── Blocker Brief section ──
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📋 Blocker Brief*\n_{ticket_count} ticket(s) stuck 3+ days in current sprint_"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": blocker_brief},
            },
            {"type": "divider"},
            # ── Thread Summary section ──
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📨 Async Thread Summary*\n_Last 18 hours across {channel_list}_"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": thread_summary},
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


# ══════════════════════════════════════════════════════
# Entrypoint
# ══════════════════════════════════════════════════════

def main() -> None:
    """
    Run both agents in sequence, then post a single combined brief to Slack.

    Running locally? Export all secrets in your shell first:

        export JIRA_BASE_URL="https://yourorg.atlassian.net"
        export JIRA_EMAIL="you@yourorg.com"
        export JIRA_API_TOKEN="your-jira-token"
        export SLACK_BOT_TOKEN="xoxb-..."
        export SLACK_CHANNELS="eng-general,incidents"
        export ANTHROPIC_API_KEY="sk-ant-..."
        export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
        python standup_prep.py
    """
    # Load all secrets from environment
    jira_base_url     = os.environ["JIRA_BASE_URL"]
    jira_email        = os.environ["JIRA_EMAIL"]
    jira_token        = os.environ["JIRA_API_TOKEN"]
    bot_token         = os.environ["SLACK_BOT_TOKEN"]
    channels_raw      = os.environ["SLACK_CHANNELS"]
    anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    webhook_url       = os.environ["SLACK_WEBHOOK_URL"]

    channel_names = [c.strip().lstrip("#") for c in channels_raw.split(",")]

    # One shared Anthropic client for both agents
    client = anthropic.Anthropic(api_key=anthropic_api_key)

    # ── Agent 1: Blocker Brief ──
    print("Fetching stuck Jira tickets...")
    tickets = get_stuck_tickets(jira_base_url, jira_email, jira_token)
    print(f"Found {len(tickets)} stuck ticket(s).")

    print("Detecting blockers with Claude...")
    blocker_brief = detect_blockers(tickets, client)

    # ── Agent 2: Thread Summary ──
    channel_transcripts = {}
    for channel_name in channel_names:
        print(f"Fetching messages from #{channel_name}...")
        channel_id = get_channel_id(channel_name, bot_token)
        if not channel_id:
            print(f"  Warning: #{channel_name} not found or bot not invited — skipping.")
            channel_transcripts[channel_name] = f"(channel not found or bot not invited to #{channel_name})"
            continue
        messages = fetch_recent_messages(channel_id, bot_token)
        print(f"  Found {len(messages)} message(s).")
        user_names = resolve_user_names(messages, bot_token) if messages else {}
        channel_transcripts[channel_name] = format_transcript(messages, user_names, channel_name)

    print("Summarizing threads with Claude...")
    thread_summary = summarize_channels(channel_transcripts, client)

    # ── Deliver: one combined Slack post ──
    print("Posting combined brief to Slack...")
    post_combined_brief(webhook_url, blocker_brief, thread_summary, len(tickets), channel_names)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
