from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from time import monotonic
from typing import Any

from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait, RPCError
from pyrogram.handlers import MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Config, load_config
from .storage import JsonStore

CONFIG: Config = load_config()
STORE = JsonStore(CONFIG.data_file)
APP = Client("blaze_controller", api_id=CONFIG.api_id, api_hash=CONFIG.api_hash, bot_token=CONFIG.bot_token, workdir=str(CONFIG.session_dir))
LIVE_CLIENTS: dict[str, Client] = {}
BULK_TASKS: dict[int, asyncio.Task[Any]] = {}

TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
BUTTON_RE = re.compile(r"^\[(?P<text>.+)]\[buttonurl:(?P<url>https?://[^\s\]]+)]$")
HELP_TEXT = (
    "🔥 **Blaze Forward Bot**\n\n"
    "**Forwarding**\n/forward <source> <destination> [start_id] [end_id]\n"
    "/liveforward <source> <destination> • /stop • /stoplive • /ongoing\n\n"
    "**Setup**\n/addchannel <chat> • /addbot <token> • /adduserbot <session>\n\n"
    "**Customize**\n/settings • /setcaption <template> • /setbutton [Text][buttonurl:https://…]\n"
    "/setmin <MB> • /setmax <MB> • /keywords <word ...> • /blockext <ext ...>\n"
    "/toggle <text|photo|video|document|audio|voice|animation|sticker|poll>\n"
    "/setregex <include|exclude> <pattern> • /replacements <find|replace, one per line>\n"
    "/reset • /unequify <chat> [limit]"
)


def owner_only(_, __, message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in CONFIG.owner_ids)

OWNER = filters.create(owner_only)


def media_info(message: Message) -> tuple[str, int, str]:
    for kind in ("document", "video", "audio", "voice", "animation", "sticker", "photo"):
        media = getattr(message, kind, None)
        if media:
            size = getattr(media, "file_size", 0) or 0
            name = getattr(media, "file_name", "") or f"{kind}_{message.id}"
            return kind, size, name
    if message.poll:
        return "poll", 0, "poll"
    return "text", 0, "text"


def fingerprint(message: Message) -> str:
    kind, size, name = media_info(message)
    file_unique_id = getattr(getattr(message, kind, None), "file_unique_id", "") if kind not in {"text", "poll"} else ""
    raw = f"{kind}|{file_unique_id}|{size}|{name}|{message.text or message.caption or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def allowed(message: Message) -> tuple[bool, str]:
    settings = STORE.data["settings"]
    kind, size, name = media_info(message)
    if not settings.get(f"allow_{kind}", True):
        return False, f"{kind} disabled"
    size_mb = size / (1024 * 1024)
    if settings["min_mb"] and size_mb < settings["min_mb"]:
        return False, "below min size"
    if settings["max_mb"] and size_mb > settings["max_mb"]:
        return False, "above max size"
    low_name = name.lower()
    searchable = f"{name}\n{message.text or message.caption or ''}".lower()
    if settings["keywords"] and not any(k.lower() in searchable for k in settings["keywords"]):
        return False, "keyword mismatch"
    if any(low_name.endswith(f".{ext.lower().lstrip('.')}") for ext in settings["blocked_extensions"]):
        return False, "blocked extension"
    if settings.get("regex"):
        try:
            matched = bool(re.search(settings["regex"], searchable, re.IGNORECASE))
        except re.error:
            return False, "invalid regex"
        if (settings.get("regex_mode") == "include" and not matched) or (settings.get("regex_mode") == "exclude" and matched):
            return False, "regex filter"
    return True, "ok"


def reply_markup() -> InlineKeyboardMarkup | None:
    text = STORE.data["settings"].get("button_text")
    url = STORE.data["settings"].get("button_url")
    if text and url:
        return InlineKeyboardMarkup([[InlineKeyboardButton(text, url=url)]])
    return None


def caption_for(message: Message) -> str | None:
    template = STORE.data["settings"].get("caption", "{caption}")
    kind, size, name = media_info(message)
    if kind == "text":
        return None
    original = apply_replacements(message.caption or "")
    try:
        return template.format(filename=name, size=f"{size / (1024 * 1024):.2f} MB", caption=original)[:1024]
    except (KeyError, ValueError):
        return original[:1024]


def apply_replacements(value: str) -> str:
    for find, replacement in STORE.data["settings"].get("replacements", []):
        value = value.replace(find, replacement)
    return value


async def copy_with_retry(client: Client, message: Message, dest: int | str) -> Message:
    while True:
        try:
            if media_info(message)[0] == "text":
                return await client.send_message(dest, apply_replacements(message.text or ""), reply_markup=reply_markup())
            return await message.copy(dest, caption=caption_for(message), reply_markup=reply_markup())
        except FloodWait as exc:
            await asyncio.sleep(exc.value)


def parse_chat(value: str) -> int | str:
    value = value.strip()
    if value.startswith("@"):
        return value
    return int(value)


@APP.on_message(filters.command("start") & OWNER)
async def start(_: Client, message: Message) -> None:
    await message.reply(HELP_TEXT)


@APP.on_message(filters.command(["help", "about"]) & OWNER)
async def help_command(_: Client, message: Message) -> None:
    if message.command[0] == "help":
        await message.reply(HELP_TEXT)
    else:
        await message.reply("Blaze Forward Bot v1.1 — owner-controlled, filtered forwarding.")


@APP.on_message(filters.command("addchannel") & OWNER)
async def add_channel(client: Client, message: Message) -> None:
    if len(message.command) < 2:
        await message.reply("Usage: /addchannel <chat_id|@username>")
        return
    chat = await client.get_chat(parse_chat(message.command[1]))
    STORE.data["channels"][str(chat.id)] = {"title": chat.title or chat.username or str(chat.id)}
    STORE.save()
    await message.reply(f"✅ Channel saved: {chat.title or chat.id} (`{chat.id}`)")


@APP.on_message(filters.command("addbot") & OWNER)
async def add_bot(_: Client, message: Message) -> None:
    text = message.text or ""
    match = TOKEN_RE.search(text)
    if not match:
        await message.reply("Usage: /addbot <token from @BotFather>")
        return
    token = match.group(0)
    name = f"bot_{token.split(':')[0]}"
    test = Client(name, api_id=CONFIG.api_id, api_hash=CONFIG.api_hash, bot_token=token, workdir=str(CONFIG.session_dir))
    async with test:
        me = await test.get_me()
    STORE.data["forwarders"][name] = {"type": "bot", "token": token, "name": me.username or me.first_name}
    STORE.save()
    await message.reply(f"✅ Bot added: @{me.username or me.first_name}")


@APP.on_message(filters.command("adduserbot") & OWNER)
async def add_userbot(_: Client, message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Usage: /adduserbot <Pyrogram v2 session string>")
        return
    session = parts[1].strip()
    name = f"user_{hashlib.sha1(session.encode()).hexdigest()[:10]}"
    test = Client(name, api_id=CONFIG.api_id, api_hash=CONFIG.api_hash, session_string=session, workdir=str(CONFIG.session_dir))
    async with test:
        me = await test.get_me()
    STORE.data["forwarders"][name] = {"type": "user", "session": session, "name": me.username or me.first_name}
    STORE.save()
    await message.reply(f"✅ Userbot added: {me.first_name}")


async def best_client() -> Client:
    for name, item in STORE.data["forwarders"].items():
        if name in LIVE_CLIENTS:
            return LIVE_CLIENTS[name]
        if item["type"] == "user":
            client = Client(name, api_id=CONFIG.api_id, api_hash=CONFIG.api_hash, session_string=item["session"], workdir=str(CONFIG.session_dir))
        else:
            client = Client(name, api_id=CONFIG.api_id, api_hash=CONFIG.api_hash, bot_token=item["token"], workdir=str(CONFIG.session_dir))
        await client.start()
        LIVE_CLIENTS[name] = client
        return client
    return APP


async def best_user_client() -> Client | None:
    for name, item in STORE.data["forwarders"].items():
        if item["type"] != "user":
            continue
        if name not in LIVE_CLIENTS:
            client = Client(name, api_id=CONFIG.api_id, api_hash=CONFIG.api_hash, session_string=item["session"], workdir=str(CONFIG.session_dir))
            await client.start()
            LIVE_CLIENTS[name] = client
        return LIVE_CLIENTS[name]
    return None


async def run_bulk(message: Message, source: int | str, dest: int | str, start_id: int, end_id: int) -> None:
    client = await best_client()
    status = await message.reply("⏳ Forwarding started...")
    stats = {"fetched": 0, "sent": 0, "dupe": 0, "filtered": 0, "deleted": 0}
    seen = set(STORE.data["duplicates"])
    started = monotonic()
    try:
        for msg_id in range(start_id, end_id + 1):
            with suppress(RPCError):
                msg = await client.get_messages(source, msg_id)
                stats["fetched"] += 1
                if not msg or msg.empty:
                    stats["deleted"] += 1
                    continue
                ok, _ = allowed(msg)
                fp = fingerprint(msg)
                if STORE.data["settings"].get("skip_duplicates", True) and fp in seen:
                    stats["dupe"] += 1
                    continue
                if not ok:
                    stats["filtered"] += 1
                    continue
                await copy_with_retry(client, msg, dest)
                seen.add(fp)
                stats["sent"] += 1
            if stats["fetched"] % CONFIG.status_update_every == 0:
                await status.edit(f"📊 {stats}\n⏱ {int(monotonic() - started)}s")
    except asyncio.CancelledError:
        await status.edit(f"🛑 Stopped\n📊 {stats}")
        raise
    finally:
        STORE.data["duplicates"] = list(seen)[-50000:]
        STORE.save()
        if message.from_user:
            BULK_TASKS.pop(message.from_user.id, None)
    await status.edit(f"✅ Done\n📊 {stats}")


@APP.on_message(filters.command("forward") & OWNER)
async def forward(_: Client, message: Message) -> None:
    if len(message.command) < 3:
        await message.reply("Usage: /forward <source_chat_id|@username> <dest_chat_id|@username> [start_id=1] [end_id=last]")
        return
    client = await best_client()
    try:
        source, dest = parse_chat(message.command[1]), parse_chat(message.command[2])
        start_id = int(message.command[3]) if len(message.command) > 3 else 1
        if len(message.command) > 4:
            end_id = int(message.command[4])
        else:
            newest = await anext(client.get_chat_history(source, limit=1), None)
            end_id = newest.id if newest else 0
    except (RPCError, ValueError) as exc:
        await message.reply(f"Cannot start forwarding: {exc}")
        return
    if start_id < 1 or end_id < start_id:
        await message.reply("No messages found in that range.")
        return
    existing = BULK_TASKS.get(message.from_user.id)
    if existing and not existing.done():
        await message.reply("A bulk task is already running. Use /stop first.")
        return
    task = asyncio.create_task(run_bulk(message, source, dest, start_id, end_id))
    BULK_TASKS[message.from_user.id] = task


@APP.on_message(filters.command("stop") & OWNER)
async def stop(_: Client, message: Message) -> None:
    task = BULK_TASKS.pop(message.from_user.id, None)
    if task:
        task.cancel()
        await message.reply("🛑 Bulk task stop requested.")
    else:
        await message.reply("No bulk task is running.")


@APP.on_message(filters.command("setcaption") & OWNER)
async def set_caption(_: Client, message: Message) -> None:
    STORE.data["settings"]["caption"] = (message.text or "").split(maxsplit=1)[1] if len(message.command) > 1 else "{caption}"
    STORE.save()
    await message.reply("✅ Caption template saved.")


@APP.on_message(filters.command("setbutton") & OWNER)
async def set_button(_: Client, message: Message) -> None:
    payload = (message.text or "").split(maxsplit=1)[1] if len(message.command) > 1 else ""
    match = BUTTON_RE.match(payload)
    if not match:
        STORE.data["settings"]["button_text"] = ""
        STORE.data["settings"]["button_url"] = ""
        await message.reply("Button cleared. Usage: /setbutton [Join][buttonurl:https://t.me/example]")
    else:
        STORE.data["settings"]["button_text"] = match.group("text")
        STORE.data["settings"]["button_url"] = match.group("url")
        await message.reply("✅ Button saved.")
    STORE.save()


@APP.on_message(filters.command("filters") & OWNER)
async def show_filters(_: Client, message: Message) -> None:
    await message.reply(f"Current settings:\n`{STORE.data['settings']}`")


@APP.on_message(filters.command("settings") & OWNER)
async def settings(_: Client, message: Message) -> None:
    settings_data = STORE.data["settings"]
    enabled = ", ".join(kind.removeprefix("allow_") for kind, value in settings_data.items() if kind.startswith("allow_") and value)
    await message.reply(
        f"⚙️ **Settings**\nEnabled types: {enabled}\n"
        f"Size: {settings_data['min_mb']}–{settings_data['max_mb'] or '∞'} MB\n"
        f"Keywords: {', '.join(settings_data['keywords']) or 'none'}\n"
        f"Blocked extensions: {', '.join(settings_data['blocked_extensions']) or 'none'}\n"
        f"Duplicate skipping: {'on' if settings_data['skip_duplicates'] else 'off'}\n"
        f"Regex: {settings_data['regex'] or 'off'} ({settings_data['regex_mode']})\n\n"
        "Use /help for configuration commands."
    )


def command_value(message: Message) -> str:
    return (message.text or "").partition(" ")[2].strip()


@APP.on_message(filters.command(["setmin", "setmax"]) & OWNER)
async def set_size(_: Client, message: Message) -> None:
    try:
        value = float(command_value(message))
        if value < 0:
            raise ValueError
    except ValueError:
        await message.reply(f"Usage: /{message.command[0]} <non-negative MB>")
        return
    key = "min_mb" if message.command[0] == "setmin" else "max_mb"
    proposed_min = value if key == "min_mb" else STORE.data["settings"]["min_mb"]
    proposed_max = value if key == "max_mb" else STORE.data["settings"]["max_mb"]
    if proposed_max and proposed_min > proposed_max:
        await message.reply("Minimum size cannot exceed maximum size.")
        return
    STORE.data["settings"][key] = value
    STORE.save()
    await message.reply(f"✅ {key} set to {value} MB.")


@APP.on_message(filters.command(["keywords", "blockext"]) & OWNER)
async def set_list(_: Client, message: Message) -> None:
    values = command_value(message).replace(",", " ").split()
    key = "keywords" if message.command[0] == "keywords" else "blocked_extensions"
    STORE.data["settings"][key] = values
    STORE.save()
    await message.reply(f"✅ {key.replace('_', ' ').title()} updated: {', '.join(values) or 'none'}.")


@APP.on_message(filters.command("toggle") & OWNER)
async def toggle_filter(_: Client, message: Message) -> None:
    kind = command_value(message).lower()
    key = f"allow_{kind}"
    if key not in STORE.data["settings"] or not key.startswith("allow_"):
        await message.reply("Usage: /toggle <text|photo|video|document|audio|voice|animation|sticker|poll>")
        return
    STORE.data["settings"][key] = not STORE.data["settings"][key]
    STORE.save()
    await message.reply(f"✅ {kind} forwarding is {'enabled' if STORE.data['settings'][key] else 'disabled'}.")


@APP.on_message(filters.command("setregex") & OWNER)
async def set_regex(_: Client, message: Message) -> None:
    parts = command_value(message).split(maxsplit=1)
    if parts == ["off"]:
        STORE.data["settings"]["regex"] = ""
        STORE.save()
        await message.reply("✅ Regex filter cleared.")
        return
    if len(parts) != 2 or parts[0] not in {"include", "exclude"}:
        await message.reply("Usage: /setregex <include|exclude> <pattern>. Send /setregex off to clear it.")
        return
    try:
        re.compile(parts[1])
    except re.error as exc:
        await message.reply(f"Invalid regex: {exc}")
        return
    STORE.data["settings"].update(regex_mode=parts[0], regex=parts[1])
    STORE.save()
    await message.reply("✅ Regex filter saved.")


@APP.on_message(filters.command("replacements") & OWNER)
async def replacements(_: Client, message: Message) -> None:
    rules: list[list[str]] = []
    for line in command_value(message).splitlines():
        find, separator, replacement = line.partition("|")
        if not separator or not find:
            await message.reply("Usage: /replacements find|replace (one rule per line)")
            return
        rules.append([find, replacement])
    STORE.data["settings"]["replacements"] = rules
    STORE.save()
    await message.reply(f"✅ Saved {len(rules)} replacement rule(s).")


@APP.on_message(filters.command("reset") & OWNER)
async def reset(_: Client, message: Message) -> None:
    from .storage import DEFAULT_DATA

    STORE.data["settings"] = JsonStore._merge(STORE, DEFAULT_DATA["settings"], {})
    STORE.save()
    await message.reply("✅ Filters, captions, buttons, and replacement rules reset. Identities and channels were kept.")


@APP.on_message(filters.command("ongoing") & OWNER)
async def ongoing(_: Client, message: Message) -> None:
    task = BULK_TASKS.get(message.from_user.id) if message.from_user else None
    await message.reply("⏳ A bulk task is running." if task and not task.done() else "No bulk task is running.")


@APP.on_message(filters.command("unequify") & OWNER)
async def unequify(_: Client, message: Message) -> None:
    if len(message.command) < 2:
        await message.reply("Usage: /unequify <chat_id|@username> [message_limit]")
        return
    client = await best_client()
    limit = int(message.command[2]) if len(message.command) > 2 and message.command[2].isdigit() else 1000
    seen: set[str] = set()
    removed = 0
    async for item in client.get_chat_history(parse_chat(message.command[1]), limit=limit):
        item_fingerprint = fingerprint(item)
        if item_fingerprint in seen:
            with suppress(RPCError):
                await item.delete()
                removed += 1
        else:
            seen.add(item_fingerprint)
    await message.reply(f"✅ Duplicate scan complete. Removed {removed} duplicate message(s).")


async def live_handler(client: Client, message: Message) -> None:
    for job in STORE.data["live_jobs"].values():
        if str(message.chat.id) == str(job["source"]):
            ok, _ = allowed(message)
            fp = fingerprint(message)
            if ok and fp not in STORE.data["duplicates"]:
                await copy_with_retry(client, message, job["dest"])
                STORE.data["duplicates"].append(fp)
                STORE.save()


@APP.on_message(filters.command("liveforward") & OWNER)
async def liveforward(_: Client, message: Message) -> None:
    if len(message.command) < 3:
        await message.reply("Usage: /liveforward <source_chat_id> <dest_chat_id>")
        return
    client = await best_user_client()
    if client is None:
        await message.reply("Live forwarding requires an added userbot. Use /adduserbot <Pyrogram v2 session string> first.")
        return
    key = f"{message.from_user.id}:{message.command[1]}:{message.command[2]}"
    STORE.data["live_jobs"][key] = {"source": str(parse_chat(message.command[1])), "dest": parse_chat(message.command[2])}
    STORE.save()
    client.add_handler(MessageHandler(live_handler, filters.chat(parse_chat(message.command[1]))))
    await message.reply("✅ Live forwarding enabled. Use /stoplive to disable saved live jobs.")


@APP.on_message(filters.command("stoplive") & OWNER)
async def stoplive(_: Client, message: Message) -> None:
    STORE.data["live_jobs"] = {}
    STORE.save()
    await message.reply("🛑 Live jobs cleared. Restart the process to remove already attached in-memory handlers.")


async def main() -> None:
    await APP.start()
    print("Blaze Forward Bot started")
    await idle()
    for client in LIVE_CLIENTS.values():
        await client.stop()
    await APP.stop()


if __name__ == "__main__":
    asyncio.run(main())
