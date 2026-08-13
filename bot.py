"""
Шуточный Telegram-бот «Раздеть фото».

Человек жмёт кнопку, присылает фото — бот отвечает картинкой,
которую админ заранее загрузил в админке.
Никакой обработки чужого фото нет: это розыгрыш.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
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
APP_VERSION = "1.0.0"
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_FILE = DATA_DIR / "config.json"

BTN_UNDRESS = "Раздеть фото"
BTN_ADMIN = "Админка"
BTN_SET_PHOTO = "Загрузить фото ответа"
BTN_SHOW_PHOTO = "Показать текущее фото"
BTN_STATS = "Статистика"
BTN_BACK = "В меню"

WAIT_USER_PHOTO = "wait_user_photo"
WAIT_ADMIN_PHOTO = "wait_admin_photo"

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
        return {"reply_file_id": None, "requests": 0}
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reply_file_id": None, "requests": 0}
    if not isinstance(raw, dict):
        return {"reply_file_id": None, "requests": 0}
    raw.setdefault("reply_file_id", None)
    raw.setdefault("requests", 0)
    return raw


def save_data(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def get_reply_file_id() -> str | None:
    with data_lock:
        value = load_data().get("reply_file_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    env_id = os.getenv("REPLY_FILE_ID", "").strip()
    return env_id or None


def set_reply_file_id(file_id: str) -> None:
    with data_lock:
        payload = load_data()
        payload["reply_file_id"] = file_id
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
            [BTN_SET_PHOTO],
            [BTN_SHOW_PHOTO, BTN_STATS],
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
    user_state.pop(update.effective_user.id, None)
    admin = is_admin(update)
    text = (
        "Привет! Это шуточный бот.\n\n"
        "Нажми «Раздеть фото» и пришли снимок — "
        "я отвечу заранее подготовленной картинкой."
    )
    if admin:
        text += "\n\nТы админ. Открой «Админка», чтобы загрузить фото ответа."
    await update.effective_message.reply_text(text, reply_markup=main_keyboard(admin))


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not is_admin(update):
        await update.effective_message.reply_text("Админка только для владельца.")
        return
    user_state.pop(update.effective_user.id, None)
    await update.effective_message.reply_text(
        "Админка\n\n"
        "Загрузи фото — его будут получать все, кто нажмёт «Раздеть фото».",
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


async def send_reply_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    file_id = get_reply_file_id()
    if not file_id:
        await message.reply_text(
            "Пока нет готового ответа. Админ ещё не загрузил фото.",
            reply_markup=main_keyboard(is_admin(update)),
        )
        return

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_PHOTO)
    status = await message.reply_text(random.choice(PROCESS_LINES))
    await asyncio.sleep(random.uniform(1.6, 3.2))
    try:
        await status.edit_text("Готово.")
    except Exception:
        pass

    await message.reply_photo(
        photo=file_id,
        caption="Держи 😄",
        reply_markup=main_keyboard(is_admin(update)),
    )
    total = bump_requests()
    log.info("reply sent to %s total=%s", user.id, total)
    user_state.pop(user.id, None)


async def handle_admin_upload(update: Update) -> None:
    message = update.effective_message
    user = update.effective_user
    file_id = photo_file_id(update)
    if not message or not user or not file_id:
        return
    set_reply_file_id(file_id)
    user_state.pop(user.id, None)
    await message.reply_photo(
        photo=file_id,
        caption="Сохранил. Теперь это фото будут получать все, кто пришлёт снимок.",
        reply_markup=admin_keyboard(),
    )
    log.info("admin %s set reply photo", user.username)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    state = user_state.get(user.id)
    if state == WAIT_ADMIN_PHOTO and is_admin(update):
        await handle_admin_upload(update)
        return
    if state == WAIT_USER_PHOTO or state is None:
        await send_reply_photo(update, context)


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

    if admin and text == BTN_SET_PHOTO:
        user_state[user.id] = WAIT_ADMIN_PHOTO
        await message.reply_text(
            "Пришли фото, которое бот будет отдавать вместо «раздетого».",
            reply_markup=admin_keyboard(),
        )
        return

    if admin and text == BTN_SHOW_PHOTO:
        file_id = get_reply_file_id()
        if not file_id:
            await message.reply_text("Фото ответа ещё не загружено.", reply_markup=admin_keyboard())
            return
        await message.reply_photo(photo=file_id, caption="Текущее фото ответа.", reply_markup=admin_keyboard())
        return

    if admin and text == BTN_STATS:
        with data_lock:
            payload = load_data()
        ready = "да" if payload.get("reply_file_id") else "нет"
        await message.reply_text(
            f"Запросов: {payload.get('requests', 0)}\nФото ответа загружено: {ready}",
            reply_markup=admin_keyboard(),
        )
        return

    if user_state.get(user.id) == WAIT_USER_PHOTO:
        await message.reply_text("Нужно именно фото, не текст.")
        return

    if user_state.get(user.id) == WAIT_ADMIN_PHOTO:
        await message.reply_text("Пришли именно фото для ответа.")
        return

    await message.reply_text(
        "Нажми «Раздеть фото», потом пришли снимок.",
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
