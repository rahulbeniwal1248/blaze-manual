# Blaze Forward Bot - Working Telegram Bot

A runnable Pyrogram implementation based on the included Blaze Forward Bot manuals. It supports owner-only setup, saved destination channels, extra bot/userbot identities, bulk forwarding, live forwarding, captions, URL buttons, filters, duplicate skipping, and persistent local JSON storage.

## Environment variables

Copy `.env.example` to `.env` and fill these values:

| Variable | Required | What to put |
| --- | --- | --- |
| `API_ID` | Yes | Numeric API ID from <https://my.telegram.org/apps>. |
| `API_HASH` | Yes | API hash from <https://my.telegram.org/apps>. |
| `BOT_TOKEN` | Yes | Controller bot token from `@BotFather`. |
| `OWNER_IDS` | Yes | Comma-separated Telegram user IDs allowed to control the bot. Get yours from `@userinfobot`. |
| `MONGO_URI` | No | MongoDB URI for future external duplicate storage; local JSON is used when blank. |
| `MONGO_DB` | No | MongoDB database name. Default: `blaze_forward_bot`. |
| `DATA_FILE` | No | JSON state path. Default: `data.json`. |
| `SESSION_DIR` | No | Pyrogram session folder. Default: `sessions`. |
| `STATUS_UPDATE_EVERY` | No | Progress edit interval in processed messages. Default: `10`. |

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env
python run.py
```

## Telegram commands

- `/start` - show command help.
- `/addchannel <chat_id|@username>` - save a destination channel. Make the selected bot/userbot admin there.
- `/addbot <BotFather token>` - add another bot identity for forwarding.
- `/adduserbot <Pyrogram v2 session string>` - add a userbot identity for private sources/live forwarding.
- `/forward <source> <dest> [start_id] [end_id]` - copy old messages from source to destination. When the end ID is omitted, the newest source message is used.
- `/liveforward <source_chat_id> <dest_chat_id>` - forward new messages in real time.
- `/stop` - cancel the current bulk task.
- `/stoplive` - clear saved live-forward jobs.
- `/setcaption <template>` - set media caption template. Placeholders: `{filename}`, `{size}`, `{caption}`.
- `/setbutton [Text][buttonurl:https://example.com]` - attach a URL button, or call without a valid payload to clear.
- `/settings` or `/filters` - display current settings and filters.
- `/setmin <MB>`, `/setmax <MB>`, `/keywords <word ...>`, `/blockext <ext ...>` - configure file attribute filters.
- `/toggle <type>` - enable or disable one message type.
- `/setregex <include|exclude> <pattern>` and `/replacements <find|replace>` - apply regex and caption/text replacements.
- `/ongoing` - check whether a bulk task is running.
- `/unequify <chat> [limit]` - remove duplicate messages from a chat where the forwarding identity has delete permission.
- `/reset` - reset custom forwarding settings while keeping saved identities and channels.

## Notes and fixed issues

- Imports are plain top-level imports; no import-time try/except wrappers are used.
- Required environment variables fail fast with clear errors.
- FloodWait is handled by sleeping and retrying the copy.
- Duplicate media/text detection is stored persistently in `DATA_FILE`.
- State writes are atomic, so a process interruption will not leave a partially written JSON file.
- The bot is owner-only, so random Telegram users cannot control forwarding.
