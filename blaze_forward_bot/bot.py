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
    if settings["keywords"] and not any(k.lower() in low_name for k in settings["keywords"]):
        return False, "keyword mismatch"
    if any(low_name.endswith(f".{ext.lower().lstrip('.')}") for ext in settings["blocked_extensions"]):
        return False, "blocked extension"
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
    original = message.caption or ""
    return template.format(filename=name, size=f"{size / (1024 * 1024):.2f} MB", caption=original)[:1024]


async def copy_with_retry(client: Client, message: Message, dest: int | str) -> Message:
    while True:
        try:
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
    await message.reply(
        "🔥 Blaze Forward Bot ready.\n\n"
        "Commands:\n"
        "/addchannel <chat_id|@username>\n/addbot <BotFather token>\n/adduserbot <Pyrogram session string>\n"
        "/forward <source> <dest> [start_id] [end_id]\n/liveforward <source> <dest>\n/stop /stoplive\n"
        "/setcaption <template> /setbutton [Text][buttonurl:https://...]\n/filters"
    )


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


async def run_bulk(message: Message, source: int | str, dest: int | str, start_id: int, end_id: int) -> None:
    client = await best_client()
    status = await message.reply("⏳ Forwarding started...")
    stats = {"fetched": 0, "sent": 0, "dupe": 0, "filtered": 0, "deleted": 0}
    seen = set(STORE.data["duplicates"])
    started = monotonic()
    for msg_id in range(start_id, end_id + 1):
        if message.from_user.id in BULK_TASKS and BULK_TASKS[message.from_user.id].cancelled():
            break
        with suppress(RPCError):
            msg = await client.get_messages(source, msg_id)
            stats["fetched"] += 1
            if not msg or msg.empty:
                stats["deleted"] += 1
                continue
            ok, _ = allowed(msg)
            fp = fingerprint(msg)
            if fp in seen:
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
    STORE.data["duplicates"] = list(seen)[-50000:]
    STORE.save()
    await status.edit(f"✅ Done\n📊 {stats}")


@APP.on_message(filters.command("forward") & OWNER)
async def forward(_: Client, message: Message) -> None:
    if len(message.command) < 3:
        await message.reply("Usage: /forward <source_chat_id|@username> <dest_chat_id|@username> [start_id=1] [end_id=last]")
        return
    client = await best_client()
    source, dest = parse_chat(message.command[1]), parse_chat(message.command[2])
    start_id = int(message.command[3]) if len(message.command) > 3 else 1
    end_id = int(message.command[4]) if len(message.command) > 4 else (await client.get_chat(source)).last_message_id
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
    client = await best_client()
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
