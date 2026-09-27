---
name: testing-discord-bot-offline
description: How to deeply test a discord.py v2 slash-command bot (like GuildMaster) with no Discord token — command-tree registration checks, driving Command.callback with mocked interactions, and Windows timing pitfalls.
---

# Testing discord.py bots without a Discord connection

When `DISCORD_BOT_TOKEN`/`OPENAI_API_KEY` are unavailable, you can still verify almost everything
short of the gateway. Working probes for this repo live in
`C:\Users\Administrator\guildmaster-test\` (probe_tree.py, probe_units.py, probe_callbacks.py,
probe_scaffold_e2e.py).

## Offline command-tree registration (catches real decorator/param bugs)
```python
settings = Settings(discord_token="x", openai_api_key="x", database_path=<tempfile>)
bot = GuildMasterBot(settings)          # does NOT connect — no bot.run/setup_hook
await bot.load_extension("guildmaster.cogs.moderation")  # etc; calls add_cog, safe offline
commands = {c.name: c for c in bot.tree.get_commands()}
cmd.to_dict(bot.tree)                    # builds the exact sync payload — catches bad params
```
- `param.min_value`/`max_value` expose `app_commands.Range` bounds.
- `param.type` is `discord.AppCommandOptionType` (member→user, channel→channel, bool→boolean).
- `cmd.allowed_contexts` exposes guild_only (`guild=True`, `dm_channel=False`).
- `cmd.checks` holds `has_permissions` guards.

## Driving command callbacks
`Moderation.timeout.callback(cog, interaction, member, "10m")` invokes the real function.
Mock with `SimpleNamespace` + `AsyncMock`; the objects your fakes MUST have:
- `interaction`: `.guild`, `.user`, `.channel_id`, `.channel`, `.response` (with `send_message`,
  `defer`, `is_done`), `.followup.send` (AsyncMock whose return_value has `.edit`).
- `guild`: `.me` (with `top_role` and `guild_permissions`), `.owner`, `.default_role`,
  `.roles` (list), `.ban`, `.id`, `.name`.
- members: `.top_role` (orderable), `.mention`, `.id`, `.bot`, `.timeout` AsyncMock.
- roles need `__lt__`/`__eq__`/`__hash__` — they're dict keys and compared by position.
- For `isinstance` checks (e.g. `discord.VoiceChannel`, `CategoryChannel`,
  `abc.GuildChannel`) use `MagicMock(spec=discord.VoiceChannel)` — spec sets `__class__`.
- A real `Database` on a temp path exercises persistence without mocking SQL.

## Windows pitfalls on this box
- `find`/`timeout` resolve to Windows tools — use `git ls-files`, or exec's own timeout param.
- Console encoding is cp1252 — emoji in output crashes `print`; run with
  `PYTHONIOENCODING=utf-8` or ASCII-escape labels.
- `time.time()` granularity is ~15.6ms — back-to-back inserts share `created_at` (~25% of the
  time), so `ORDER BY created_at DESC` has visible ties. Assert order by `id` where it matters.
- Docker daemon runs, but Docker Hub anonymous pulls may 429 — check `docker images` for cached
  bases before promising a build test.

## Devin Secrets Needed
- `DISCORD_BOT_TOKEN`, `OPENAI_API_KEY` — only for live gateway/OpenAI verification; everything
  above runs without them.
