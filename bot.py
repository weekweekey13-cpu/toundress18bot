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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

APP_NAME = "undress-bot"
APP_VERSION = "1.3.0"
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_FILE = DATA_DIR / "config.json"

BTN_UNDRESS = "Раздеть фото"
BTN_ADMIN = "Админка"
BTN_SET_PACK = "Загрузить стикерпак"
BTN_SET_WELCOME = "Картинка на старт"
BTN_DEL_WELCOME = "Убрать картинку старта"
BTN_PAY_ON = "Включить оплату"
BTN_PAY_OFF = "Выключить оплату"
BTN_SHOW_PACK = "Показать пак"
BTN_STATS = "Статистика"
BTN_BACK = "В меню"
BTN_BUY = "Получить 10 генераций"
CALLBACK_BUY = "buy_gens"
PACK_PRICE_STARS = 50
PACK_GENERATIONS = 10
START_TEXT = "Привет! Загрузи фото которое хочешь раздеть"

WAIT_USER_PHOTO = "wait_user_photo"
WAIT_ADMIN_STICKER = "wait_admin_sticker"
WAIT_ADMIN_WELCOME = "wait_admin_welcome"

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
        return {
            "sticker_set": None,
            "requests": 0,
            "users": {},
            "welcome_file_id": None,
            "pay_enabled": True,
        }
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "sticker_set": None,
            "requests": 0,
            "users": {},
            "welcome_file_id": None,
            "pay_enabled": True,
        }
    if not isinstance(raw, dict):
        return {
            "sticker_set": None,
            "requests": 0,
            "users": {},
            "welcome_file_id": None,
            "pay_enabled": True,
        }
    raw.setdefault("sticker_set", None)
    raw.setdefault("requests", 0)
    raw.setdefault("users", {})
    raw.setdefault("welcome_file_id", None)
    raw.setdefault("pay_enabled", True)
    if not isinstance(raw["users"], dict):
        raw["users"] = {}
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


def get_generations(user_id: int) -> int:
    with data_lock:
        users = load_data().get("users") or {}
        rec = users.get(str(user_id)) or {}
    try:
        return max(0, int(rec.get("gens") or 0))
    except (TypeError, ValueError):
        return 0


def add_generations(user_id: int, amount: int) -> int:
    with data_lock:
        payload = load_data()
        users = payload.setdefault("users", {})
        rec = users.setdefault(str(user_id), {"gens": 0})
        rec["gens"] = max(0, int(rec.get("gens") or 0) + int(amount))
        save_data(payload)
        return int(rec["gens"])


def consume_generation(user_id: int) -> int:
    with data_lock:
        payload = load_data()
        users = payload.setdefault("users", {})
        rec = users.setdefault(str(user_id), {"gens": 0})
        current = max(0, int(rec.get("gens") or 0))
        if current <= 0:
            return 0
        rec["gens"] = current - 1
        save_data(payload)
        return rec["gens"]


def get_welcome_file_id() -> str | None:
    with data_lock:
        value = load_data().get("welcome_file_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def set_welcome_file_id(file_id: str | None) -> None:
    with data_lock:
        payload = load_data()
        payload["welcome_file_id"] = file_id
        save_data(payload)


def is_pay_enabled() -> bool:
    with data_lock:
        return bool(load_data().get("pay_enabled", True))


def set_pay_enabled(enabled: bool) -> None:
    with data_lock:
        payload = load_data()
        payload["pay_enabled"] = bool(enabled)
        save_data(payload)


def buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(BTN_BUY, callback_data=CALLBACK_BUY)]]
    )


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
    pay_btn = BTN_PAY_OFF if is_pay_enabled() else BTN_PAY_ON
    rows = [
        [BTN_SET_PACK],
        [BTN_SET_WELCOME],
        [pay_btn],
        [BTN_SHOW_PACK, BTN_STATS],
        [BTN_BACK],
    ]
    if get_welcome_file_id():
        rows.insert(2, [BTN_DEL_WELCOME])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


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


async def send_start_message(update: Update) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    user_state[user.id] = WAIT_USER_PHOTO
    admin = is_admin(update)
    welcome = get_welcome_file_id()
    if welcome:
        try:
            await message.reply_photo(
                photo=welcome,
                caption=START_TEXT,
                reply_markup=main_keyboard(admin),
            )
            return
        except Exception:
            log.exception("welcome photo failed")
    await message.reply_text(START_TEXT, reply_markup=main_keyboard(admin))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_start_message(update)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if not is_admin(update):
        await update.effective_message.reply_text("Админка только для владельца.")
        return
    user_state.pop(update.effective_user.id, None)
    pack = get_sticker_set_name()
    extra = f"\nСейчас стоит пак: {pack}" if pack else "\nПак ещё не загружен."
    pay = "включена" if is_pay_enabled() else "выключена"
    welcome = "есть" if get_welcome_file_id() else "нет"
    await update.effective_message.reply_text(
        "Админка\n\n"
        "Скинь стикер — бот возьмёт весь пак.\n"
        f"Картинка на старт: {welcome}\n"
        f"Оплата 50⭐: {pay}"
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


async def send_random_sticker(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_markup=None,
) -> bool:
    set_name = get_sticker_set_name()
    if not set_name:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Пока нет стикерпака. Админ ещё не скинул стикер в бота.",
            reply_markup=reply_markup,
        )
        return False
    try:
        sticker, _pack = await pick_random_sticker(context, set_name)
    except Exception:
        log.exception("get_sticker_set failed name=%s", set_name)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Не получилось взять стикерпак. Админ, скинь стикер ещё раз.",
            reply_markup=reply_markup,
        )
        return False
    if not sticker:
        await context.bot.send_message(
            chat_id=chat_id,
            text="В паке нет стикеров.",
            reply_markup=reply_markup,
        )
        return False
    await context.bot.send_sticker(
        chat_id=chat_id,
        sticker=sticker.file_id,
        reply_markup=reply_markup,
    )
    bump_requests()
    return True


async def send_invoice_for_gens(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    payload = f"gens_{user_id}_{int(time.time())}"
    await context.bot.send_invoice(
        chat_id=chat_id,
        title="10 генераций",
        description="10 генераций раздевания фото",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="10 генераций", amount=PACK_PRICE_STARS)],
    )


async def run_process_steps(message) -> None:
    status = await message.reply_text(PROCESS_LINES[0])
    for line in PROCESS_LINES[1:]:
        await asyncio.sleep(random.uniform(1.1, 2.0))
        try:
            await status.edit_text(line)
        except Exception:
            status = await message.reply_text(line)
    await asyncio.sleep(random.uniform(0.8, 1.4))
    try:
        await status.delete()
    except Exception:
        pass


async def offer_or_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    pay_on = is_pay_enabled()
    has_gens = get_generations(user.id) > 0
    admin = is_admin(update)
    skip_pay = (not pay_on) or admin or has_gens

    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    if not skip_pay:
        status = await message.reply_text(PROCESS_LINES[0])
        await asyncio.sleep(random.uniform(1.2, 2.0))
        try:
            await status.edit_text("Готово. Нажми кнопку, чтобы получить результат.")
        except Exception:
            pass
        await message.reply_text(BTN_BUY, reply_markup=buy_keyboard())
        user_state[user.id] = WAIT_USER_PHOTO
        return

    await run_process_steps(message)
    if pay_on and has_gens and not admin:
        left = consume_generation(user.id)
    else:
        left = get_generations(user.id)
    ok = await send_random_sticker(
        context,
        message.chat_id,
        reply_markup=main_keyboard(admin),
    )
    if ok and pay_on and not admin:
        await message.reply_text(
            f"Осталось генераций: {left}",
            reply_markup=main_keyboard(False),
        )
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


async def handle_admin_welcome(update: Update) -> None:
    message = update.effective_message
    user = update.effective_user
    file_id = photo_file_id(update)
    if not message or not user or not file_id:
        return
    set_welcome_file_id(file_id)
    user_state.pop(user.id, None)
    await message.reply_photo(
        photo=file_id,
        caption="Сохранил. Эта картинка будет после Start вместе с приветствием.",
        reply_markup=admin_keyboard(),
    )
    log.info("admin %s set welcome photo", user.username)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if is_admin(update) and user_state.get(user.id) == WAIT_ADMIN_WELCOME:
        await handle_admin_welcome(update)
        return
    if is_admin(update) and user_state.get(user.id) == WAIT_ADMIN_STICKER:
        await update.effective_message.reply_text(
            "Нужен стикер из пака, не фото.",
            reply_markup=admin_keyboard(),
        )
        return
    await offer_or_send(update, context)


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if is_admin(update):
        await handle_admin_sticker(update, context)
        return
    await update.effective_message.reply_text(
        "Пришли фото, не стикер.",
        reply_markup=main_keyboard(False),
    )


async def on_buy_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not update.effective_user:
        return
    await query.answer()
    await send_invoice_for_gens(context, query.message.chat_id, update.effective_user.id)


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if not query:
        return
    payload = query.invoice_payload or ""
    if payload.startswith("gens_"):
        await query.answer(ok=True)
        return
    await query.answer(ok=False, error_message="Неизвестный платёж")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.successful_payment:
        return
    payload = message.successful_payment.invoice_payload or ""
    if not payload.startswith("gens_"):
        return
    left = add_generations(user.id, PACK_GENERATIONS)
    log.info("stars paid user=%s gens=%s payload=%s", user.id, left, payload)
    await message.reply_text(
        f"Оплата прошла. Начислено {PACK_GENERATIONS} генераций.",
        reply_markup=main_keyboard(is_admin(update)),
    )
    left = consume_generation(user.id)
    ok = await send_random_sticker(
        context,
        message.chat_id,
        reply_markup=main_keyboard(is_admin(update)),
    )
    if ok:
        await message.reply_text(
            f"Осталось генераций: {left}",
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

    if admin and text == BTN_SET_WELCOME:
        user_state[user.id] = WAIT_ADMIN_WELCOME
        await message.reply_text(
            "Пришли картинку для сообщения после Start.",
            reply_markup=admin_keyboard(),
        )
        return

    if admin and text == BTN_DEL_WELCOME:
        set_welcome_file_id(None)
        user_state.pop(user.id, None)
        await message.reply_text("Картинку со старта убрал.", reply_markup=admin_keyboard())
        return

    if admin and text == BTN_PAY_ON:
        set_pay_enabled(True)
        await message.reply_text(
            "Оплата включена. После фото будет кнопка «Получить 10 генераций».",
            reply_markup=admin_keyboard(),
        )
        return

    if admin and text == BTN_PAY_OFF:
        set_pay_enabled(False)
        await message.reply_text(
            "Оплату выключил. После фото сразу «Смотрю на фото…» и стикер.",
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
        pay = "вкл" if payload.get("pay_enabled", True) else "выкл"
        welcome = "есть" if payload.get("welcome_file_id") else "нет"
        await message.reply_text(
            f"Запросов: {payload.get('requests', 0)}\n"
            f"Стикерпак: {pack}\n"
            f"Картинка старта: {welcome}\n"
            f"Оплата: {pay}",
            reply_markup=admin_keyboard(),
        )
        return

    if user_state.get(user.id) == WAIT_USER_PHOTO:
        await message.reply_text("Нужно именно фото, не текст.")
        return

    if user_state.get(user.id) == WAIT_ADMIN_STICKER:
        await message.reply_text("Пришли стикер из пака.")
        return

    if user_state.get(user.id) == WAIT_ADMIN_WELCOME:
        await message.reply_text("Пришли картинку для старта.")
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
            app.add_handler(CallbackQueryHandler(on_buy_click, pattern=f"^{CALLBACK_BUY}$"))
            app.add_handler(PreCheckoutQueryHandler(pre_checkout))
            app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
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
