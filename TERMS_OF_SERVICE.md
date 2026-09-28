# Terms of Service — GuildMaster ("Reverse")

**Effective date:** 2026-09-28
**Contact:** melihgul2024@gmail.com

These Terms of Service ("Terms") govern the use of GuildMaster (the "Bot", application name "Reverse"), a self-hosted Discord application that provides AI-assisted server scaffolding and moderation tooling. By inviting or using the Bot in a Discord server, the server owner and its administrators ("you") accept these Terms.

## 1. Description of the Service

The Bot offers:

- AI-generated server layouts: natural-language prompts are converted into Discord categories, channels, roles, topics, and permission overwrites (`/scaffold` or the web control panel).
- Moderation tools: timeouts, kicks, bans, warnings/strikes, message purging, channel locks, slowmode, and anti-raid automation.
- Audit logging of moderation and server events.
- Layout snapshots (backups) and rollback.

The Bot runs on infrastructure operated by the person hosting it (the "Operator"), not on public infrastructure operated as a hosted service.

## 2. Acceptable Use

You agree to:

- Comply with the [Discord Terms of Service](https://discord.com/terms), [Discord Community Guidelines](https://discord.com/guidelines), and applicable law.
- Only use the Bot on servers where you hold administrator authority or have permission from the server owner.
- Not use the Bot to harass users, violate privacy, spam, or abuse Discord's API.
- Ensure the Bot's role sits appropriately in your server's role hierarchy and grant it only the permissions it needs.

## 3. Destructive Operations

The `/scaffold` command and panel can delete and recreate server channels and roles. A layout backup is created automatically before changes, and `/layout-rollback` (or the panel's Backups tab) can restore it. You acknowledge that:

- These operations are performed at your explicit request.
- Backups capture channel/role structure and permission overwrites — **not** message content, member data, or server settings.
- The Operator is not responsible for data loss if a backup does not exist, is stale, or cannot fully restore a prior state.

## 4. Moderation Actions

Timeouts, kicks, bans, warnings, and message deletions are executed only when invoked by your server's authorized users or the configured anti-raid automation. You are solely responsible for moderation decisions made through the Bot and for communicating them to your members.

## 5. Third-Party Services

Scaffold prompts are sent to the OpenAI API to generate layouts. Your use of that feature is also subject to OpenAI's terms. The Bot relies on the Discord API; availability may be affected by either service.

## 6. No Warranty

THE BOT IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. The Operator does not warrant that the Bot will be uninterrupted, error-free, or compatible with every server configuration.

## 7. Limitation of Liability

To the maximum extent permitted by law, the Operator shall not be liable for indirect, incidental, special, consequential, or punitive damages — including lost data, lost messages, server disruption, or moderation errors — arising from use of the Bot.

## 8. Termination

You may stop using the Bot at any time by removing it from your server. The Operator may discontinue the Bot or revoke access at any time. Sections 6 and 7 survive termination.

## 9. Changes

These Terms may be updated; the current version is always available at this URL. Continued use after changes constitutes acceptance.
