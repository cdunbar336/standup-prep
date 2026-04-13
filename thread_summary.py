#!/usr/bin/env python3
"""
Slack Thread Summarizer — AI Agent
====================================
This script is the second agent in the standup prep suite. It runs 15 minutes
after the blocker brief (8:45 AM) and does three things:

  1. FETCH  — calls the Slack API to pull recent messages from one or more
              channels you configure (e.g., #eng-general, #incidents, #releases)

  2. REASON — sends those messages to Claude with a summarization prompt,
              asking it to surface decisions made, action items, and anything
              that needs a response

  3. DELIVER — posts the summary back to Slack so it's waiting before standup

The goal: you arrive at standup already knowing what happened async overnight
or since the last sync. No thread-reading required.

Requires one additional secret vs. the blocker brief:
  SLACK_BOT_TOKEN — a bot token with channels:history and channels:read scopes
                    (needed to READ messages; the webhook URL is write-only)

All secrets are injected as environment variables from GitHub Secrets.
"""

import os
import sys
import time
import requests
import anthropic


# ────────────────────────────────────────────────────────────
# STEP 1: FETCH — Pull recent messages from Slack channels
# ────────────────────────────────────────────────────────────

def get_channel_id(channel_name: str, bot_token: str) -> str | None:
    """
    Resolve a channel name (e.g. "eng-general") to its Slack channel ID.

    Slack's API requires channel IDs (e.g. C012AB3CD) for most calls, not
    human-readable names. This function paginates through the workspace's
    public channels to find a match.

    Note: The bot must be invited to private channels before it can see them.
    For public channels, no invitation is needed.
    """
    url = "https://slack.com/api/conversations.list"
    headers = {"Authorization": f"Bearer {bot_token}"}
    cursor = None

    while True:
        params = {"limit": 200, "exclude_archived": True}
        if cursor:
            params["cursor"] = cursor

        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error')}")

        for channel in data.get("channels", []):
            if channel["name"] == channel_name.lstrip("#"):
                return channel["id"]

        # Slack paginates results using cursors — follow them until exhausted
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return None


def fetch_recent_messages(channel_id: str, bot_token: str, lookback_hours: int = 18) -> list[dict]:
    """
    Fetch messages from a Slack channel posted in the last N hours.

    Why 18 hours by default? If standup is at 9 AM, this captures everything
    since ~3 PM the previous day — covering the async tail of the prior
    workday and any overnight activity.

    The Slack conversations.history API returns messages in reverse
    chronological order (newest first). We reverse them for the prompt
    so Claude reads the thread in natural order.

    We skip bot messages and join/leave events to keep the context clean.
    """
    url = "https://slack.com/api/conversations.history"
    headers = {"Authorization": f"Bearer {bot_token}"}

    oldest = str(time.time() - (lookback_hours * 3600))

    response = requests.get(
        url,
        headers=headers,
        params={
            "channel": channel_id,
            "oldest": oldest,
            "limit": 200,   # max per request; increase if your channels are very active
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")

    messages = data.get("messages", [])

    # Filter out noise: bot messages, channel join/leave events, etc.
    human_messages = [
        m for m in messages
        if m.get("type") == "message"
        and not m.get("bot_id")
        and m.get("subtype") is None
    ]

    # Reverse to chronological order for the prompt
    return list(reversed(human_messages))


def resolve_user_names(messages: list[dict], bot_token: str) -> dict[str, str]:
    """
    Look up display names for each unique user ID that appears in the messages.

    Slack messages reference users by ID (e.g. U012AB3CD). We batch-resolve
    these to display names so Claude's summary uses real names, not IDs.
    """
    user_ids = {m["user"] for m in messages if "user" in m}
    names = {}

    for uid in user_ids:
        try:
            response = requests.get(
                "https://slack.com/api/users.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"user": uid},
                timeout=10,
            )
            data = response.json()
            if data.get("ok"):
                profile = data["user"].get("profile", {})
                # Prefer display_name, fall back to real_name
                names[uid] = profile.get("display_name") or profile.get("real_name") or uid
        except Exception:
            names[uid] = uid  # if lookup fails, just show the ID

    return names


def format_messages_for_prompt(
    messages: list[dict],
    user_names: dict[str, str],
    channel_name: str,
) -> str:
    """
    Convert Slack message objects into a readable transcript for Claude.

    Format: [HH:MM] DisplayName: message text
    Timestamps are converted from Unix epoch to readable time.
    """
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


# ──────────────────────────────────────────────────────────────────────
# STEP 2: REASON — Ask Claude to summarize and surface what matters
# ──────────────────────────────────────────────────────────────────────

def summarize_channels(channel_transcripts: dict[str, str], anthropic_api_key: str) -> str:
    """
    Send channel transcripts to Claude and ask for a standup-ready summary.

    The prompt is structured to focus Claude on what's actionable, not just
    what was said. We want decisions, action items, and open questions —
    not a play-by-play of the conversation.

    If all channels were quiet, Claude will say so cleanly rather than
    producing an empty or confusing output.
    """
    all_quiet = all("(no messages" in t for t in channel_transcripts.values())
    if all_quiet:
        return "All monitored channels were quiet in the last 18 hours. Nothing to catch up on."

    # Build one section per channel so Claude can organize its output the same way
    sections = "\n\n".join(
        f"--- #{channel} ---\n{transcript}"
        for channel, transcript in channel_transcripts.items()
    )

    prompt = f"""You are a chief of staff preparing a standup briefing for an engineering team.

Below are transcripts from Slack channels over the last 18 hours. Read them and produce a concise summary organized by channel. For each channel with activity, surface:

1. **Key decisions made** — anything the team agreed on or resolved
2. **Action items** — explicit or implied next steps, and who owns them
3. **Open questions** — things raised but not yet answered
4. **FYIs** — important context that doesn't require action

Skip channels with no meaningful activity. If a channel only has brief, low-signal messages, summarize in one sentence.

{sections}

Format: use the channel name as a header (e.g. ## #eng-general), then bullet points for each category that has content. Omit empty categories. Keep the whole summary under 500 words. Write for someone who has 60 seconds to read it before a meeting."""

    client = anthropic.Anthropic(api_key=anthropic_api_key)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=700,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )

    return message.content[0].text


# ─────────────────────────────────────────────────────
# STEP 3: DELIVER — Post the summary to Slack
# ─────────────────────────────────────────────────────

def post_to_slack(webhook_url: str, summary: str, channels_monitored: list[str]) -> None:
    """
    Post the thread summary to Slack using Block Kit.

    We use the same webhook-based delivery as the blocker brief — no
    additional OAuth scopes needed for posting. The webhook URL determines
    which channel receives the summary.
    """
    channel_list = ", ".join(f"#{c}" for c in channels_monitored)

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Async Thread Summary",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Last 18 hours across {channel_list}",
                    }
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": summary},
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
    Orchestrate the fetch → reason → deliver pipeline for Slack thread summarization.

    SLACK_CHANNELS is a comma-separated list of channel names to monitor.
    Example: "eng-general,incidents,releases"

    Running locally? Export these in your shell:

        export SLACK_BOT_TOKEN="xoxb-..."
        export SLACK_CHANNELS="eng-general,incidents"
        export ANTHROPIC_API_KEY="sk-ant-..."
        export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
        python thread_summary.py
    """
    bot_token         = os.environ["SLACK_BOT_TOKEN"]
    channels_raw      = os.environ["SLACK_CHANNELS"]          # e.g. "eng-general,incidents"
    anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    slack_webhook_url = os.environ["SLACK_WEBHOOK_URL"]

    channel_names = [c.strip().lstrip("#") for c in channels_raw.split(",")]

    channel_transcripts = {}

    for channel_name in channel_names:
        print(f"Fetching messages from #{channel_name}...")

        channel_id = get_channel_id(channel_name, bot_token)
        if not channel_id:
            print(f"  Warning: channel #{channel_name} not found — skipping.")
            channel_transcripts[channel_name] = f"(channel not found or bot not invited)"
            continue

        messages = fetch_recent_messages(channel_id, bot_token)
        print(f"  Found {len(messages)} message(s).")

        user_names = resolve_user_names(messages, bot_token) if messages else {}
        transcript = format_messages_for_prompt(messages, user_names, channel_name)
        channel_transcripts[channel_name] = transcript

    print("Asking Claude to summarize...")
    summary = summarize_channels(channel_transcripts, anthropic_api_key)
    print("Summary generated.")

    print("Posting to Slack...")
    post_to_slack(slack_webhook_url, summary, channel_names)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
