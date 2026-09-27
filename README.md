# GuildMaster

AI-powered Discord server architect & master moderation bot. Describe a community in plain language and GuildMaster designs and builds the whole channel layout — plus a full moderation suite with strikes, locks, anti-raid protection and audit logging.

- `discord.py` v2.x, slash commands only (`app_commands`)
- OpenAI Structured Outputs (`gpt-4o-mini` by default) → strict `pydantic` layout schema
- SQLite via `aiosqlite` (settings, strikes, layout backups)
- Rate-limited channel creation queue with 429 backoff

## Commands

### AI scaffolding
| Command | What it does |
|---|---|
| `/scaffold prompt:<text> [wipe_existing]` | Generates a server layout from a prompt and builds it: roles, categories, text/voice/stage/forum channels, topics, slowmode, NSFW flags, role-gated visibility. Saves a backup first; posts a live progress embed. |
| `/layout-backup` | Snapshots the current channel tree into the database. |
| `/layout-rollback` | Restores the most recent backup: deletes channels not in it, recreates missing ones. |

Prompt example: *"Dark fantasy RPG community with lore archives, tavern voice chats, role-gated admin chambers, and announcement boards."*

### Moderation
| Command | What it does |
|---|---|
| `/timeout <member> <duration> [reason]` | Native Discord timeout. Durations: `30m`, `12h`, `7d`, bare number = minutes. Max 28 days. |
| `/untimeout <member>` | Remove a timeout. |
| `/kick <member> [reason]` | Kick. |
| `/ban <member> [delete_days] [reason]` | Ban; optionally delete up to 7 days of their messages. |
| `/unban <user_id>` | Unban by user ID. |
| `/warn <member> <reason>` | Adds a strike. Auto-timeout at 3 strikes (1h, then 24h), auto-ban at 5. DMs the member. |
| `/warnings <member>` / `/clearwarnings <member>` | View / clear a member's strikes. |
| `/purge <amount> [member]` | Bulk-delete recent messages (Discord's 14-day limit applies). |
| `/lock [channel]` / `/unlock [channel]` | Deny/restore `@everyone` write (or connect on voice channels). |
| `/slowmode <seconds> [channel]` | Set slowmode (0 = off, max 21600). |
| `/antiraid <enabled> [lock_channel] [min_age_hours] [join_threshold] [window_seconds]` | Join-surge detection: locks the gate channel and times out too-new accounts during a raid, then auto-lifts. |

### Logging
| Command | What it does |
|---|---|
| `/setup-logs <channel>` | Pick the audit channel: deleted/edited messages, joins/leaves, bans, channel changes, mod actions. |
| `/disable-logs` | Turn logging off. |

## Setup

### 1. Discord Developer Portal
1. [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application** → **Bot** → **Reset Token**. Copy it (`DISCORD_BOT_TOKEN`).
2. Under **Bot → Privileged Gateway Intents** enable **Server Members Intent** and **Message Content Intent**.
3. Invite the bot (OAuth2 → URL Generator, scopes `bot` + `applications.commands`):

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=8&scope=bot%20applications.commands
```

`permissions=8` is Administrator — simplest for a scaffolder/moderation bot. Restrict it if you prefer, but the bot needs at minimum: Manage Channels, Manage Roles, Kick, Ban, Timeout Members, Manage Messages, Read/Send Messages.

4. **Role hierarchy:** in Server Settings → Roles, drag the bot's role near the top — above every role it must gate or member it must moderate. Otherwise Discord rejects actions with `403 Forbidden`.

### 2. OpenAI key
Create a key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys) (`OPENAI_API_KEY`). `gpt-4o-mini` is the default model; set `OPENAI_MODEL=gpt-4o` for higher quality.

### 3. Configure
```bash
cp .env.example .env   # fill in DISCORD_BOT_TOKEN and OPENAI_API_KEY
```

Set `DEV_GUILD_ID` to your test server's ID while developing — slash commands sync to it instantly (global sync takes up to an hour).

### 4. Run

Docker (recommended):
```bash
docker compose up -d --build
```

Bare metal:
```bash
pip install -r requirements.txt
python main.py
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Layout snapshots, strikes, and settings live in a SQLite file (`DATABASE_PATH`, default `data/guildmaster.db`; a Docker volume in compose).

## Safety notes

- `/scaffold` always saves a backup before touching anything; `/layout-rollback` restores the latest one. It never deletes the log channel or the channel the command was run in.
- Channel writes go through a single-worker queue (~1.25s between operations) with exponential backoff on HTTP 429, so big builds take a few minutes by design.
- All moderation commands check the Discord role hierarchy (invoker and bot vs. target) before acting.
