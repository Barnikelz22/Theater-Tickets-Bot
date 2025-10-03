import argparse
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Union
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp
import toml
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ======================
# Configuration & Constants
# ======================

BOT_TOKEN_ENV_VAR = "BOT_TOKEN"
DB_FILE = "theater_bot_db.toml"
FETCH_URL = "https://t-hazafon.smarticket.co.il/iframe/api/chairmap"

LOG_FILE = "/data/telegram_bot.log" if os.environ.get("IS_PRODUCTION") == "TRUE" else "telegram_bot.log"
DEFAULT_MIN_SEATS = 2
MONITORING_INTERVAL = 300  # 5 minutes in production

# ======================
# Logging Setup
# ======================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("theater_bot")


# ======================
# Data Models
# ======================

@dataclass
class Seat:
    row: str
    chair: str
    status: str


@dataclass
class MonitoredShow:
    chat_id: int
    theater_id: str
    min_seats: int
    created_at: str
    last_available_groups: List[Dict]
    max_row: Optional[int] = None
    original_url: Optional[str] = None  # <-- Store original URL for notifications


# ======================
# State Management
# ======================

class UserState:
    pass


@dataclass
class FindSeatsState(UserState):
    pass


@dataclass
class MonitorSetupState(UserState):
    temp_theater_id: Optional[str] = None
    original_url: Optional[str] = None
    waiting_for: Optional[str] = None  # 'min_seats' or 'max_row_setup'
    temp_min_seats: Optional[int] = None


@dataclass
class ChangeMaxRowState(UserState):
    key: str


# ======================
# Main Bot Class
# ======================

class TheaterBot:
    def __init__(self, token: str, debug: bool = False):
        self.token = token
        self.debug = debug
        self.db_file = DB_FILE
        self.monitored_shows: Dict[str, MonitoredShow] = self.load_db()
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        self._setup_logging()

    def _setup_logging(self):
        level = logging.DEBUG if self.debug else logging.WARNING
        logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
        for name in ["httpx", "telegram", "aiohttp"]:
            logging.getLogger(name).setLevel(level)

    def load_db(self) -> Dict[str, MonitoredShow]:
        try:
            with open(self.db_file, "r", encoding="utf-8") as f:
                data = toml.load(f)
                shows = {}
                for key, value in data.get("monitored_shows", {}).items():
                    shows[key] = MonitoredShow(**value)
                return shows
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"Error loading DB: {e}")
            if os.path.exists(self.db_file):
                os.remove(self.db_file)
            return {}

    def save_db(self):
        try:
            data = {"monitored_shows": {k: asdict(v) for k, v in self.monitored_shows.items()}}
            with open(self.db_file, "w", encoding="utf-8") as f:
                toml.dump(data, f)
        except Exception as e:
            logger.error(f"Error saving DB: {e}")

    def extract_theater_id(self, url: str) -> Optional[str]:
        match = re.search(r"showURL=(\d+)", url)
        return match.group(1) if match else None

    def get_main_menu(self):
        return ReplyKeyboardMarkup(
            [
                ["🔍 Find Available Seats", "➕ Monitor Show"],
                ["📋 My Monitored Shows", "❌ Stop Monitoring"],
                ["❓ Help"],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    # ======================
    # Seat Logic
    # ======================

    async def fetch_and_parse_chairmap(self, theater_id: str) -> List[Seat]:
        payload = {"show_theater": theater_id}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(FETCH_URL, data=payload) as resp:
                    resp.raise_for_status()
                    html = await resp.text()
                    return self.parse_seats_from_html(html)
        except Exception as e:
            logger.error(f"Fetch error for {theater_id}: {e}")
            return []

    def parse_seats_from_html(self, html: str) -> List[Seat]:
        pattern = r'<a[^>]*class="([^"]*)"[^>]*data-chair="([^"]*)"[^>]*data-row="([^"]*)"[^>]*>'
        matches = re.findall(pattern, html, re.DOTALL)
        return [
            Seat(row=row, chair=chair, status="taken" if "taken" in cls else "available")
            for cls, chair, row in matches
        ]

    def find_adjacent_seats(self, seats: List[Seat], min_seats: int, max_row: Optional[int]) -> List[Dict]:
        if max_row is not None:
            seats = [s for s in seats if s.row.isdigit() and int(s.row) <= max_row]

        by_row: Dict[str, List[Seat]] = {}
        for s in seats:
            if s.status == "available":
                by_row.setdefault(s.row, []).append(s)

        groups = []
        for row, row_seats in by_row.items():
            row_seats.sort(key=lambda s: int(s.chair) if s.chair.isdigit() else float("inf"))
            seq = []
            for seat in row_seats:
                if not seq or (seat.chair.isdigit() and seq[-1].chair.isdigit() and int(seat.chair) == int(seq[-1].chair) + 1):
                    seq.append(seat)
                else:
                    if len(seq) >= min_seats:
                        groups.append({
                            "row": seq[0].row,
                            "start_chair": seq[0].chair,
                            "end_chair": seq[-1].chair,
                            "count": len(seq),
                        })
                    seq = [seat]
            if len(seq) >= min_seats:
                groups.append({
                    "row": seq[0].row,
                    "start_chair": seq[0].chair,
                    "end_chair": seq[-1].chair,
                    "count": len(seq),
                })
        return groups

    def _compare_groups(self, old: List[Dict], new: List[Dict]) -> List[Dict]:
        old_keys = {(g["row"], g["start_chair"], g["end_chair"]) for g in old}
        return [g for g in new if (g["row"], g["start_chair"], g["end_chair"]) not in old_keys]

    # ======================
    # Handlers
    # ======================

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🎭 Welcome to Theater Seat Finder Bot!\nUse buttons below or commands.",
            reply_markup=self.get_main_menu(),
        )
        context.user_data["state"] = UserState()

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "❓ Send a show URL → Find or Monitor seats → Get notified when available!",
            reply_markup=self.get_main_menu(),
        )

    async def find_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Please send the show URL.", reply_markup=self.get_main_menu())
        context.user_data["state"] = FindSeatsState()

    async def monitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Please send the show URL to monitor.", reply_markup=self.get_main_menu())
        context.user_data["state"] = FindSeatsState()

    async def myshows_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._show_user_monitored_shows(update, context, back_button=True)

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._show_user_monitored_shows(update, context, for_stop=True)

    async def _show_user_monitored_shows(self, update: Update, context: ContextTypes.DEFAULT_TYPE, for_stop=False, back_button=False):
        chat_id = update.effective_chat.id
        user_shows = {k: v for k, v in self.monitored_shows.items() if v.chat_id == chat_id}
        if not user_shows:
            msg = "You're not monitoring any shows." if for_stop else "You're not monitoring any shows. Use '➕ Monitor Show'."
            await update.message.reply_text(msg, reply_markup=self.get_main_menu())
            return

        prefix = "Select a show to stop:\n" if for_stop else "📋 Your monitored shows:\n"
        keyboard = []
        for key, show in user_shows.items():
            label = f"Stop: {show.theater_id}" if for_stop else f"Manage: {show.theater_id}"
            cb = f"stop_{key}" if for_stop else f"manage_{key}"
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

        if back_button:
            keyboard.append([InlineKeyboardButton("Back to Menu", callback_data="main_menu")])

        await update.message.reply_text(prefix, reply_markup=InlineKeyboardMarkup(keyboard))

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        chat_id = update.effective_chat.id

        # Handle menu button presses even during input states
        menu_commands = {
            "🔍 Find Available Seats": self.find_command,
            "➕ Monitor Show": self.monitor_command,
            "📋 My Monitored Shows": self.myshows_command,
            "❌ Stop Monitoring": self.stop_command,
            "❓ Help": self.help_command,
        }
        if text in menu_commands:
            context.user_data.pop("state", None)
            await menu_commands[text](update, context)
            return

        state = context.user_data.get("state")

        # Handle special input states
        if isinstance(state, ChangeMaxRowState):
            await self._handle_change_max_row(update, context, text, state.key)
            return
        if isinstance(state, MonitorSetupState):
            if state.waiting_for == "min_seats":
                await self._handle_min_seats(update, context, text, state.temp_theater_id, state.original_url)
            elif state.waiting_for == "max_row_setup":
                await self._handle_max_row(update, context, text, state.temp_theater_id, state.temp_min_seats, state.original_url)
            return
        if isinstance(state, FindSeatsState):
            await self._handle_find_seats(update, context, text)
            context.user_data.pop("state", None)
            return

        # Default: treat as URL
        if text.startswith("http"):
            await self._handle_url(update, context, text)
        else:
            await update.message.reply_text("Please send a valid show URL.", reply_markup=self.get_main_menu())
            context.user_data["state"] = UserState()

    async def _handle_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
        theater_id = self.extract_theater_id(url)
        if not theater_id:
            await update.message.reply_text("Invalid URL.", reply_markup=self.get_main_menu())
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Find Seats Now", callback_data=f"find_now_{theater_id}")],
            [InlineKeyboardButton("➕ Monitor This Show", callback_data=f"monitor_{theater_id}|{url}")],
            [InlineKeyboardButton("Back to Menu", callback_data="main_menu")],
        ])
        await update.message.reply_text(f"Show ID: {theater_id}\nWhat would you like to do?", reply_markup=keyboard)

    async def _handle_find_seats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
        theater_id = self.extract_theater_id(url)
        if not theater_id:
            await update.message.reply_text("Invalid URL.", reply_markup=self.get_main_menu())
            return
        await update.message.reply_text("Searching for seats...")
        seats = await self.fetch_and_parse_chairmap(theater_id)
        groups = self.find_adjacent_seats(seats, DEFAULT_MIN_SEATS, None)
        msg = (
            "\n".join(
                f"{i}. {g['count']} seats in row {g['row']}: {g['start_chair']}–{g['end_chair']}"
                for i, g in enumerate(groups, 1)
            )
            if groups
            else "No adjacent seats found."
        )
        await update.message.reply_text(msg or "No adjacent seats found.", reply_markup=self.get_main_menu())

    async def _handle_min_seats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, theater_id: str, url: str):
        if not text.isdigit():
            await update.message.reply_text("Enter a number.", reply_markup=self.get_main_menu())
            return
        context.user_data["state"] = MonitorSetupState(
            temp_theater_id=theater_id,
            original_url=url,
            waiting_for="max_row_setup",
            temp_min_seats=int(text),
        )
        await update.message.reply_text("Max row? (0 = unlimited)", reply_markup=self.get_main_menu())

    async def _handle_max_row(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, theater_id: str, min_seats: int, url: str):
        if not text.isdigit():
            await update.message.reply_text("Enter a number (0 = unlimited).", reply_markup=self.get_main_menu())
            return
        max_row = None if int(text) == 0 else int(text)
        chat_id = update.effective_chat.id
        key = f"{chat_id}_{theater_id}"
        self.monitored_shows[key] = MonitoredShow(
            chat_id=chat_id,
            theater_id=theater_id,
            min_seats=min_seats,
            created_at=datetime.now().isoformat(),
            last_available_groups=[],
            max_row=max_row,
            original_url=url,
        )
        self.save_db()
        await self._start_monitoring_task(key, theater_id, min_seats, chat_id)
        status = "Unlimited" if max_row is None else str(max_row)
        await update.message.reply_text(
            f"✅ Monitoring show {theater_id} for {min_seats} seats (max row: {status}).",
            reply_markup=self.get_main_menu(),
        )
        context.user_data.pop("state", None)

    async def _handle_change_max_row(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, key: str):
        if not text.isdigit():
            await update.message.reply_text("Enter a number (0 = unlimited).", reply_markup=self.get_main_menu())
            return
        if key not in self.monitored_shows:
            await update.message.reply_text("❌ Show not found.", reply_markup=self.get_main_menu())
            context.user_data.pop("state", None)
            return
        max_row = None if int(text) == 0 else int(text)
        self.monitored_shows[key].max_row = max_row
        self.save_db()
        status = "unlimited" if max_row is None else str(max_row)
        await update.message.reply_text(
            f"✅ Max row updated to {status} for show {self.monitored_shows[key].theater_id}.",
            reply_markup=self.get_main_menu(),
        )
        context.user_data.pop("state", None)

    # ======================
    # Inline Callbacks
    # ======================

    async def inline_button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "main_menu":
            await query.edit_message_text("🎭 Back to main menu.", reply_markup=self.get_main_menu())
            context.user_data["state"] = UserState()
            return

        if data.startswith("find_now_"):
            theater_id = data.split("_", 2)[2]
            await query.edit_message_text("Searching...")
            seats = await self.fetch_and_parse_chairmap(theater_id)
            groups = self.find_adjacent_seats(seats, DEFAULT_MIN_SEATS, None)
            msg = (
                "\n".join(
                    f"{i}. {g['count']} seats in row {g['row']}: {g['start_chair']}–{g['end_chair']}"
                    for i, g in enumerate(groups, 1)
                )
                if groups
                else "No adjacent seats found."
            ) or "No adjacent seats found."
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="main_menu")]]))
            return

        if data.startswith("monitor_"):
            parts = data.split("|", 1)
            theater_id = parts[0].split("_", 1)[1]
            url = parts[1] if len(parts) > 1 else ""
            await query.edit_message_text("How many adjacent seats?")
            context.user_data["state"] = MonitorSetupState(temp_theater_id=theater_id, original_url=url, waiting_for="min_seats")
            return

        if data.startswith("manage_"):
            key = data.split("_", 1)[1]
            show = self.monitored_shows.get(key)
            if not show:
                await query.edit_message_text("❌ Show not found.")
                return
            msg = (
                f"Manage Show: {show.theater_id}\n"
                f"Min seats: {show.min_seats}\n"
                f"Max row: {show.max_row or 'Unlimited'}\n"
                f"Groups found: {len(show.last_available_groups)}"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Change Max Row", callback_data=f"change_max_row_{key}")],
                [InlineKeyboardButton("Stop Monitoring", callback_data=f"stop_{key}")],
                [InlineKeyboardButton("Back", callback_data="main_menu")],
            ])
            await query.edit_message_text(msg, reply_markup=keyboard)
            return

        if data.startswith("change_max_row_"):
            key = data.split("_", 3)[3]  # Ensures full key is captured even with underscores in theater_id
            if key not in self.monitored_shows:
                await query.edit_message_text("❌ Show not found.")
                return
            await query.edit_message_text("New max row? (0 = unlimited)")
            context.user_data["state"] = ChangeMaxRowState(key=key)
            return

        if data.startswith("stop_"):
            key = data.split("_", 1)[1]
            if key in self.monitored_shows:
                theater_id = self.monitored_shows[key].theater_id
                del self.monitored_shows[key]
                self.save_db()
                await self._stop_monitoring_task(key)
                await query.edit_message_text(f"✅ Stopped monitoring {theater_id}.")
            else:
                await query.edit_message_text("❌ Already stopped.")
            return

    # ======================
    # Monitoring Tasks
    # ======================

    async def _start_monitoring_task(self, key: str, theater_id: str, min_seats: int, chat_id: int):
        if key in self.monitoring_tasks:
            self.monitoring_tasks[key].cancel()
        self.monitoring_tasks[key] = asyncio.create_task(self._monitor_show(key, theater_id, min_seats, chat_id))

    async def _stop_monitoring_task(self, key: str):
        if key in self.monitoring_tasks:
            self.monitoring_tasks[key].cancel()
            del self.monitoring_tasks[key]

    async def _monitor_show(self, key: str, theater_id: str, min_seats: int, chat_id: int):
        logger.info(f"Started monitoring {theater_id} for user {chat_id}")
        try:
            while key in self.monitored_shows:
                show = self.monitored_shows[key]
                seats = await self.fetch_and_parse_chairmap(theater_id)
                groups = self.find_adjacent_seats(seats, min_seats, show.max_row)
                new_groups = self._compare_groups(show.last_available_groups, groups)
                if new_groups:
                    url_line = f"\n🔗 Book now: {show.original_url}" if show.original_url else ""
                    msg = "🎉 New seats available!\n" + "\n".join(
                        f"• {g['count']} seats in row {g['row']}: {g['start_chair']}–{g['end_chair']}"
                        for g in new_groups
                    ) + f"\n\nTotal groups: {len(groups)}" + url_line
                    try:
                        await self.application.bot.send_message(chat_id=chat_id, text=msg)
                        logger.info(f"Notification sent to {chat_id} for {theater_id}")
                    except Exception as e:
                        logger.error(f"Failed to send notification: {e}")
                self.monitored_shows[key].last_available_groups = groups
                self.save_db()
                await asyncio.sleep(MONITORING_INTERVAL)
        except asyncio.CancelledError:
            logger.info(f"Monitoring cancelled for {theater_id}")
        except Exception as e:
            logger.error(f"Monitoring error for {theater_id}: {e}")

    # ======================
    # Run
    # ======================

    def run(self):
        app = Application.builder().token(self.token).build()
        self.application = app

        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CommandHandler("help", self.help_command))
        app.add_handler(CommandHandler("find", self.find_command))
        app.add_handler(CommandHandler("monitor", self.monitor_command))
        app.add_handler(CommandHandler("myshows", self.myshows_command))
        app.add_handler(CommandHandler("stop", self.stop_command))
        app.add_handler(CallbackQueryHandler(self.inline_button_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        logger.info("Bot started.")
        app.run_polling()


# ======================
# Entry Point
# ======================

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    token = os.environ.get(BOT_TOKEN_ENV_VAR)
    if not token:
        raise EnvironmentError(f"Set {BOT_TOKEN_ENV_VAR} env var.")

    bot = TheaterBot(token, debug=args.debug)
    bot.run()