# ALPHA-WARNING-APPROVE-ALL-INFINITY-LOOPS

> [!CAUTION]
> **Alpha. Read this before you install it.**
>
> - **It auto-approves things.** Permission prompts, questions and "should I?" stops get answered for you, in every Claude Code session on your machine.
> - **It can loop.** Sessions keep going without you until a limit hands them back.
> - **It spends API credit** on every review.
> - **A session misled by a malicious page can get its recommendation accepted.** The reviewer and the guards reduce that risk. They don't remove it.

## What it does

`approve-all` is one Python script that runs as Claude Code hooks. When a session stops to ask something, it answers with what the session recommended, and the session carries on.

| When | What it does |
| --- | --- |
| A session stops | Takes the first wrap-up button, or replies "go with your recommendation" |
| `AskUserQuestion` | Picks the "(Recommended)" option, else the first |
| A permission prompt | Allows it |

With an API key, a Claude reviewer decides first using the session's recent activity: whether anything is really being asked, which option fits, and what only you can do. Without a key, or if a review fails, the rules above take over.

Replies are always labeled as auto-replies. They never claim you typed them.

## Setup

You need [uv](https://docs.astral.sh/uv/). It brings Python and the Anthropic SDK. Runs on macOS and Linux.

```bash
git clone https://github.com/swack-tools/ALPHA-WARNING-APPROVE-ALL-INFINITY-LOOPS.git
cd ALPHA-WARNING-APPROVE-ALL-INFINITY-LOOPS
cp .env.example .env
chmod 600 .env
```

Put your key in `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Test the key, which makes one real review call, then install:

```bash
uv run --script approve-all check
uv run --script approve-all install
```

`install` adds the hooks to `~/.claude/settings.json`, and they point at this folder. If you move it, run `install` again. Until you do, the hooks do nothing.

## Where the key goes

Hooks don't inherit your shell, so the key goes in `.env` next to the script. Reviews use the first key they find:

1. `.env`
2. The file `set-key` saves, `~/.claude/approve-all/api_key`
3. `ANTHROPIC_API_KEY` in the hook's environment

With no key, it runs on the rules alone.

> [!WARNING]
> **Never put the key in `settings.json` under `env`.** Claude Code would pick it up too, which could bill all your Claude sessions to it.

## Commands

Run each one as `uv run --script approve-all <command>`.

| Command | What it does |
| --- | --- |
| `install` / `uninstall` | Add or remove the hooks |
| `pause` / `resume` | Stop or restart answering, in every session, at once |
| `scan [--hours 24]` | List sessions waiting right now. Changes nothing, but with a key it reviews each one, which costs credit |
| `log [-n 20]` | Show recent decisions and errors |
| `status` | Show whether it's installed, paused and reviewing, and the spend so far |
| `check` | Make one real review call to test the key |
| `set-key` | Save the key outside `.env`, if you prefer |

## Choosing the model

The reviewer defaults to `claude-opus-5-5` at medium effort. To change it, set this in `.env`:

```bash
APPROVE_ALL_MODEL=claude-sonnet-5-5
```

Any model ID works. A model the script has no price for is estimated at Opus 5.5 rates. If a model rejects the request, the review fails, the rules take over and the error is logged. A real environment variable beats `.env`. All the settings are in [.env.example](.env.example).

## What it never answers for you

The reviewer can't lift these. Edit `NEVER` in the script to change them.

| Guard | Examples |
| --- | --- |
| `secret` | Passwords, API keys, tokens, 2FA and verification codes |
| `hands-on` | "Paste the key", "tell me when", screenshots, anything on your phone or TV, signing in |
| `production` | Deploying, releasing, restarting or migrating production |
| `destructive` | `rm -rf`, dropping tables, deleting databases, repos, buckets, backups, volumes, clusters or accounts, force-pushing to main |
| `money` | Payments, purchases, credit cards, wire transfers, trading |
| `outbound` | Sending or publishing email, tweets, newsletters, press releases, anything to customers or the public |

These are pattern matches: a safety net, not a guarantee. `APPROVE_ALL_NO_GUARD=1` in the real environment removes all but `secret` and `hands-on`. `.env` can't set it.

Sessions also come back to you when they repeat a question after being answered, reach 25 auto-replies since you last wrote, or go 3 turns without doing any work. Scripted runs (`claude -p`, the SDK) are left alone.

## The dialog

When something is left for you, a dialog shows the question with answers to pick from. Your pick goes straight to the session.

- It replaced a notification that opened Script Editor when clicked.
- It uses `osascript`, so dialogs are macOS only. Elsewhere, the session just stops and waits for you.
- It stays up for `APPROVE_ALL_ANSWER_WAIT` seconds (default 1 hour), and closes early if the session receives a message another way.
- "Later" closes it and leaves the session waiting.

## When something fails

No failure should crash or block a session. Check `uv run --script approve-all log` to see what happened.

| Failure | What happens |
| --- | --- |
| API error, missing SDK, bad model reply | The rules take over, and it's logged |
| No key, or an empty one | Rules only |
| Malformed `.env` | Bad lines are skipped, bad numbers use the default |
| Corrupt `spend.jsonl` | Bad lines are skipped. If the file can't be read or written, reviews stop, because the cap can't be enforced |
| Transcript can't be read | No auto-reply when a session stops, because the loop limits are counted from it. Prompts and questions still use the rules |
| `osascript` missing | No dialog. It's left for you in the session |
| uv fails, or the script moved | The hook does nothing and the session stops as usual. Nothing is logged, since the script never ran |
| Malformed `settings.json` | `install` and `uninstall` refuse and change nothing |

## Cost

About 2.5 cents per review, measured on Opus 5.5. Reviews stop at a default cap of $150, set with `APPROVE_ALL_BUDGET_USD`, and the rules take over. Spend is an estimate from token counts, so check your Anthropic console for the real bill.

## Limits

- Sessions already waiting when you install need one manual reply. Hooks only run at a session's next stop.
- Replies from the desktop app are delivered only at a session's next stop.

## Tests

```bash
uv run --with "anthropic>=2.3,<3" python test_approve_all.py
```

Without the SDK, the tests that need it are skipped:

```bash
uv run python test_approve_all.py
```
