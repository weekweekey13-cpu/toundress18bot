"""
Шуточный Telegram-бот «Раздеть фото».

Человек присылает фото — бот отвечает случайным стикером
из пака, который админ заранее скинул в админке.
Никакой обработки чужого фото нет: это розыгрыш.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

APP_NAME = "undress-bot"
APP_VERSION = "1.1.0"
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_FILE = DATA_DIR / "config.json"

BTN_UNDRESS = "Раздеть фото"
BTN_ADMIN = "Админка"
BTN_SET_PACK = "Загрузить стикерпак"
BTN_SHOW_PACK = "Показать пак"
BTN_STATS = "Статистика"
BTN_BACK = "В меню"

WAIT_USER_PHOTO = "wait_user_photo"
WAIT_ADMIN_STICKER = "wait_admin_sticker"

PROCESS_LINES = (
    "Смотрю на фото…",
    "Ищу, что можно «снять»…",
    "Почти готово…",
)

DEFAULT_ADMINS = "bonamartin69"

log = logging.getLogger(APP_NAME)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BOT_TOKEN = ""
ADMIN_USERNAMES: set[str] = set()
user_state: dict[int, str] = {}
data_lock = threading.Lock()


def load_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if token:
        return token
    fallback = ROOT.parent / "токен2.txt"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip()
    return ""


def parse_admins() -> set[str]:
    raw = os.getenv("ADMIN_USERNAMES", DEFAULT_ADMINS)
    return {
        u.strip().lstrip("@").lower()
        for u in raw.split(",")
        if u.strip()
    }


def load_data() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.is_file():
        return {"sticker_set": None, "requests": 0}
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"sticker_set": None, "requests": 0}
    if not isinstance(raw, dict):
        return {"sticker_set": None, "requests": 0}
    raw.setdefault("sticker_set", raw.get("reply_file_id") and None)
    raw.setdefault("requests", 0)
    return raw


def save_data(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def get_sticker_set_name() -> str | None:
    with data_lock:
        value = load_data().get("sticker_set")
    if isinstance(value, str) and value.strip():
        return value.strip()
    env_name = os.getenv("STICKER_SET", "").strip()
    return env_name or None


def set_sticker_set_name(name: str) -> None:
    with data_lock:
        payload = load_data()
        payload["sticker_set"] = name
        save_data(payload)


def bump_requests() -> int:
    with data_lock:
        payload = load_data()
        payload["requests"] = int(payload.get("requests") or 0) + 1
        save_data(payload)
        return int(payload["requests"])


def is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not user.username:
        return False
    return user.username.lstrip("@").lower() in ADMIN_USERNAMES


def main_keyboard(admin: bool) -> ReplyKeyboardMarkup:
    rows = [[BTN_UNDRESS]]
    if admin:
        rows.append([BTN_ADMIN])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BTN_SET_PACK],
            [BTN_SHOW_PACK, BTN_STATS],
            [BTN_BACK],
        ],
        resize_keyboard=True,
    )


def photo_file_id(update: Update) -> str | None:
    message = update.effective_message
    if not message:
        return None
    if message.photo:
        return message.photo[-1].file_id
    document = message.document
    if document and (document.mime_type or "").startswith("image/"):
        return document.file_id
    return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    user_state[update.effective_user.id] = WAIT_USER_PHOTO
    admin = is_admin(update)
    await update.effective_message.reply_text(
        "Привет! Загрузи фото которое хочешь раздеть",
        reply_markup=main_keyboard(admin),
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not is_admin(update):
        await update.effective_message.reply_text("Админка только для владельца.")
        return
    user_state.pop(update.effective_user.id, None)
    pack = get_sticker_set_name()
    extra = f"\nСейчас стоит пак: {pack}" if pack else "\nПак ещё не загружен."
    await update.effective_message.reply_text(
        "Админка\n\n"
        "Скинь любой стикер из пака — бот будет отвечать случайным стикером из него."
        f"{extra}",
        reply_markup=admin_keyboard(),
    )


async def ask_user_photo(update: Update) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    user_state[user.id] = WAIT_USER_PHOTO
    await message.reply_text(
        "Пришлите фотографию которую хотите раздеть",
        reply_markup=main_keyboard(is_admin(update)),
    )


async def pick_random_sticker(context: ContextTypes.DEFAULT_TYPE, set_name: str):
    sticker_set = await context.bot.get_sticker_set(set_name)
    stickers = list(sticker_set.stickers or [])
    if not stickers:
        return None, sticker_set
    return random.choice(stickers), sticker_set


async def send_reply_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    set_name = get_sticker_set_name()
    if not set_name:
        await message.reply_text(
            "Пока нет стикерпака. Админ ещё не скинул пак в админке.",
            reply_markup=main_keyboard(is_admin(update)),
        )
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.CHOOSE_STICKER)
    status = await message.reply_text(random.choice(PROCESS_LINES))
    await asyncio.sleep(random.uniform(1.6, 3.2))

    try:
        sticker, _pack = await pick_random_sticker(context, set_name)
    except Exception:
        log.exception("get_sticker_set failed name=%s", set_name)
        try:
            await status.edit_text("Не получилось взять стикерпак. Админ, скинь пак ещё раз.")
        except Exception:
            pass
        return

    if not sticker:
        try:
            await status.edit_text("В паке нет стикеров.")
        except Exception:
            pass
        return

    try:
        await status.delete()
    except Exception:
        pass

    await message.reply_sticker(
        sticker=sticker.file_id,
        reply_markup=main_keyboard(is_admin(update)),
    )
    total = bump_requests()
    log.info("sticker sent to %s pack=%s total=%s", user.id, set_name, total)
    user_state[user.id] = WAIT_USER_PHOTO


async def handle_admin_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.sticker:
        return
    set_name = (message.sticker.set_name or "").strip()
    if not set_name:
        await message.reply_text(
            "Этот стикер не из пака. Пришли стикер именно из стикерпака.",
            reply_markup=admin_keyboard(),
        )
        return
    try:
        sticker_set = await context.bot.get_sticker_set(set_name)
    except Exception:
        log.exception("admin get_sticker_set failed name=%s", set_name)
        await message.reply_text(
            "Не смог открыть этот пак. Пришли другой стикер из пака.",
            reply_markup=admin_keyboard(),
        )
        return
    set_sticker_set_name(set_name)
    user_state.pop(user.id, None)
    count = len(sticker_set.stickers or [])
    title = sticker_set.title or set_name
    await message.reply_text(
        f"Сохранил пак «{title}» ({count} шт.).\n"
        "Теперь бот будет отвечать случайным стикером из него.",
        reply_markup=admin_keyboard(),
    )
    log.info("admin %s set sticker pack %s count=%s", user.username, set_name, count)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if user_state.get(user.id) == WAIT_ADMIN_STICKER and is_admin(update):
        await update.effective_message.reply_text(
            "Нужен стикер из пака, не фото.",
            reply_markup=admin_keyboard(),
        )
        return
    await send_reply_sticker(update, context)


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if is_admin(update) and user_state.get(user.id) == WAIT_ADMIN_STICKER:
        await handle_admin_sticker(update, context)
        return
    await update.effective_message.reply_text(
        "Пришли фото, не стикер.",
        reply_markup=main_keyboard(is_admin(update)),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return

    text = message.text.strip()
    admin = is_admin(update)

    if text == BTN_UNDRESS:
        await ask_user_photo(update)
        return

    if text in {BTN_ADMIN, "/admin"}:
        await cmd_admin(update, context)
        return

    if text == BTN_BACK:
        user_state.pop(user.id, None)
        await message.reply_text("Главное меню.", reply_markup=main_keyboard(admin))
        return

    if admin and text == BTN_SET_PACK:
        user_state[user.id] = WAIT_ADMIN_STICKER
        await message.reply_text(
            "Пришли любой стикер из нужного пака.",
            reply_markup=admin_keyboard(),
        )
        return

    if admin and text == BTN_SHOW_PACK:
        set_name = get_sticker_set_name()
        if not set_name:
            await message.reply_text("Пак ещё не загружен.", reply_markup=admin_keyboard())
            return
        try:
            sticker, pack = await pick_random_sticker(context, set_name)
        except Exception:
            await message.reply_text(
                f"Не смог открыть пак {set_name}. Загрузи его ещё раз.",
                reply_markup=admin_keyboard(),
            )
            return
        count = len(pack.stickers or []) if pack else 0
        title = (pack.title if pack else set_name) or set_name
        if sticker:
            await message.reply_sticker(sticker=sticker.file_id)
        await message.reply_text(
            f"Текущий пак: «{title}»\nСтикеров: {count}\nИмя: {set_name}",
            reply_markup=admin_keyboard(),
        )
        return

    if admin and text == BTN_STATS:
        with data_lock:
            payload = load_data()
        pack = payload.get("sticker_set") or "нет"
        await message.reply_text(
            f"Запросов: {payload.get('requests', 0)}\nСтикерпак: {pack}",
            reply_markup=admin_keyboard(),
        )
        return

    if user_state.get(user.id) == WAIT_USER_PHOTO:
        await message.reply_text("Нужно именно фото, не текст.")
        return

    if user_state.get(user.id) == WAIT_ADMIN_STICKER:
        await message.reply_text("Пришли стикер из пака.")
        return

    await message.reply_text(
        "Пришли фото, которое хочешь раздеть.",
        reply_markup=main_keyboard(admin),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error: %s", context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Запуск"),
            BotCommand("admin", "Админка"),
        ]
    )


class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        log.info("http " + fmt, *args)

    def _ok(self) -> None:
        body = json.dumps({"ok": True, "app": APP_NAME, "v": APP_VERSION}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._ok()

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()


def start_http(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("HTTP http://0.0.0.0:%s  (/api/ping)", port)
    server.serve_forever()


def main() -> None:
    global BOT_TOKEN, ADMIN_USERNAMES

    load_dotenv(ROOT / ".env", override=True)
    BOT_TOKEN = load_token()
    ADMIN_USERNAMES = parse_admins()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        port = int(os.getenv("PORT", "3000"))
    except ValueError:
        port = 3000

    if sys.version_info >= (3, 10):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    threading.Thread(target=start_http, args=(port,), daemon=True).start()
    log.info(
        "Boot: port=%s token=%s admins=%s v=%s",
        port,
        "yes" if BOT_TOKEN else "MISSING",
        ",".join(sorted(ADMIN_USERNAMES)) or "-",
        APP_VERSION,
    )

    if not BOT_TOKEN:
        log.error("Нет BOT_TOKEN. На Render: Environment → BOT_TOKEN")
        while True:
            time.sleep(300)

    while True:
        try:
            app = (
                Application.builder()
                .token(BOT_TOKEN)
                .post_init(post_init)
                .build()
            )
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("admin", cmd_admin))
            app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_photo))
            app.add_handler(MessageHandler(filters.Sticker.ALL, on_sticker))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
            app.add_error_handler(on_error)
            log.info("polling…")
            app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        except Exception as exc:
            msg = str(exc)
            if "Conflict" in msg or "getUpdates" in msg:
                log.error("Conflict: этот бот уже запущен где-то ещё. Retry 15s")
                time.sleep(15)
            else:
                log.exception("polling crashed, retry 8s")
                time.sleep(8)


if __name__ == "__main__":
    main()
