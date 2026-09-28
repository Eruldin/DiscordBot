# Privacy Policy — GuildMaster ("Reverse")

**Effective date:** 2026-09-28
**Contact:** melihgul2024@gmail.com

This Privacy Policy explains what data the GuildMaster Discord bot (application name "Reverse", the "Bot") collects, where it is stored, and how it is used.

## 1. Data We Store

The Bot stores the following in a local SQLite database on the machine that runs it (controlled by the Operator — the person hosting the Bot):

| Data | Purpose |
|---|---|
| Guild (server) IDs | Scoping all settings and records to your server |
| Channel/role layout snapshots (names, types, topics, permission overwrites) | Pre-change backups enabling `/layout-rollback` and panel rollback |
| Warning "strikes" (member ID, moderator ID, reason, timestamp) | `/warn` escalation and `/warnings` history |
| Configured log channel ID and anti-raid settings | Audit logging and raid protection |
| Panel job state (in-memory) | Live progress for scaffold/rollback jobs |

## 2. Data Sent to Third Parties

- **OpenAI API:** when `/scaffold` or the panel's "Generate plan" is used, your prompt text and generated layout are sent to OpenAI to produce the design. No member data is included. Subject to [OpenAI's privacy policy](https://openai.com/policies/privacy-policy).
- **Discord API:** all other operations go through Discord and are governed by Discord's own policies.

The Bot does **not** run analytics, telemetry, advertising, or sell any data to anyone.

## 3. Data We Do Not Store

- Message content is not persisted. `/purge` and message-event logs pass through memory only; deleted/edited message text may be posted to your configured audit channel at the time of the event but is not written to the database.
- No user credentials, tokens, or DMs are collected.

## 4. Retention and Deletion

- Layout backups and strikes persist until deleted by the Operator or cleared (`/clearwarnings`, database removal).
- Removing the Bot stops all future data collection. To delete stored data, ask the Operator to delete the Bot's database file or specific records.
- Uninstalling the Bot removes its access to your server; it does not retroactively delete backups — request deletion via the contact above if needed.

## 5. Security

The Bot's database lives only on the Operator's host. Discord tokens and API keys are kept in local configuration and never transmitted anywhere except the services that require them (Discord, OpenAI). Access to the web control panel is protected by a token and bound to localhost by default.

## 6. Children's Privacy

The Bot is not directed at children under 13 and does not knowingly collect their data. Discord's own minimum-age rules apply.

## 7. Changes

This policy may be updated; the current version is always available at this URL. Material changes are announced in this repository.
