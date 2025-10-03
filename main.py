import argparse
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Union
import aiohttp
import toml
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)
# Constants
BOT_TOKEN_ENV_VAR = 'BOT_TOKEN'
DB_FILE = 'theater_bot_db.toml'
# Corrected URL
# Removed trailing space
FETCH_URL = "https://t-hazafon.smarticket.co.il/iframe/api/chairmap"
if os.environ.get('IS_PRODUCTION') == 'TRUE':
    LOG_FILE = '/data/telegram_bot.log'
else:
    LOG_FILE = 'telegram_bot.log'
DEFAULT_MIN_SEATS = 2
# 30 seconds for testing, change back to 300 (5 min) for production
MONITORING_INTERVAL = 30
# --- NEW: State Dataclasses ---
@dataclass
class InitialState:
    """Default state when no specific action is pending."""
    pass
@dataclass
class FindSeatsState:
    """State when waiting for a URL to find seats."""
    pass
@dataclass
class MonitorSetupState:
    """State during the monitoring setup process."""
    temp_theater_id: Optional[str] = None
    waiting_for: Optional[str] = None  # 'min_seats' or 'max_row_setup'
    temp_min_seats: Optional[int] = None
@dataclass
class ChangeMaxRowState:
    """State when waiting for max row input for an existing monitored show."""
    key: str
# --- END NEW: State Dataclasses ---
# Data classes (existing)
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
    # Store each group as {row, start_chair, end_chair, count}
    last_available_groups: List[Dict]
    max_row: Optional[int] = None  # Maximum row number to consider
# Setup logging (existing)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
main_logger = logging.getLogger('theater_bot')
class TheaterBot:
    """Main class for the Theater Seat Finder Bot."""
    def __init__(self, token: str, debug: bool = False):
        """Initialize the bot with the provided token."""
        self.token = token
        self.db_file = DB_FILE
        self.monitored_shows = self.load_db()
        self.monitoring_tasks: Dict[str, asyncio.Task] = {}
        self.debug = debug
        # Set logging level based on debug flag
        if debug:
            main_logger.setLevel(logging.DEBUG)
            logging.getLogger("httpx").setLevel(logging.DEBUG)
            logging.getLogger("telegram").setLevel(logging.DEBUG)
            logging.getLogger("aiohttp").setLevel(logging.DEBUG)
        else:
            logging.getLogger("httpx").setLevel(logging.WARNING)
            logging.getLogger("telegram").setLevel(logging.WARNING)
            logging.getLogger("aiohttp").setLevel(logging.WARNING)
    def load_db(self) -> Dict[str, MonitoredShow]:
        """Load monitored shows from TOML database"""
        try:
            with open(self.db_file, 'r', encoding='utf-8') as f:
                data = toml.load(f)
                shows = {}
                for key, value in data.get('monitored_shows', {}).items():
                    shows[key] = MonitoredShow(
                        chat_id=value['chat_id'],
                        theater_id=value['theater_id'],
                        min_seats=value['min_seats'],
                        created_at=value['created_at'],
                        last_available_groups=value.get(
                            'last_available_groups', []),
                        # Load max_row if it exists
                        max_row=value.get('max_row')
                    )
                return shows
        except FileNotFoundError:
            return {}
        except Exception as e:
            main_logger.error(f"Error loading database: {e}")
            # If the file is corrupted, delete it and create a new one
            if os.path.exists(self.db_file):
                main_logger.info(
                    "Database file is corrupted, deleting and creating a new one...")
                os.remove(self.db_file)
            return {}
    def save_db(self):
        """Save monitored shows to TOML database"""
        try:
            data = {'monitored_shows': {}}
            for key, show in self.monitored_shows.items():
                data['monitored_shows'][key] = {
                    'chat_id': show.chat_id,
                    'theater_id': show.theater_id,
                    'min_seats': show.min_seats,
                    'created_at': show.created_at,
                    'last_available_groups': show.last_available_groups,
                    'max_row': show.max_row  # Save max_row
                }
            with open(self.db_file, 'w', encoding='utf-8') as f:
                toml.dump(data, f)
        except Exception as e:
            main_logger.error(f"Error saving database: {e}")
    async def fetch_and_parse_chairmap(self, theater_id: str):
        """Fetch and parse the chairmap for a given theater ID."""
        payload = {"show_theater": theater_id}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(FETCH_URL, data=payload) as response:
                    response.raise_for_status()
                    html_content = await response.text()
                    seats = self.parse_seats_from_html(html_content)
                    available_seats = [
                        s for s in seats if s.status == "available"]
                    # Log available seats if debug is enabled
                    if self.debug:
                        main_logger.debug(
                            f"Fetched {len(seats)} total seats, {len(available_seats)} available for theater {theater_id}")
                        for seat in available_seats:
                            main_logger.debug(
                                f"Available seat: Row {seat.row}, Chair {seat.chair}")
                    return available_seats
        except aiohttp.ClientError as e:
            main_logger.error(f"An error occurred during the request: {e}")
            return []
    def parse_seats_from_html(self, html_content: str) -> List[Seat]:
        """Parse seats from HTML content."""
        seats = []
        # Pattern to match <a> tags with data-chair, data-row, and class attributes
        # The class contains either "taken" or other values indicating status
        pattern = r'<a.*?class="(.*?)".*?data-chair="(.*?)".*?data-row="(.*?)".*?</a>'
        matches = re.findall(pattern, html_content,
                             flags=re.MULTILINE | re.DOTALL)
        for class_attr, chair_num, row_num in matches:
            # Determine status from class - look for "taken" in the class string
            status = 'taken' if 'taken' in class_attr else "available"
            seat = Seat(
                row=row_num,
                chair=chair_num,
                status=status
            )
            seats.append(seat)
        return seats
    def find_adjacent_seats(self, seats: List[Seat], min_seats: int = DEFAULT_MIN_SEATS, max_row: Optional[int] = None) -> List[Dict]:
        """
        Find groups of adjacent available seats.
        Args:
            seats: List of available seats
            min_seats: Minimum number of adjacent seats required in a group
            max_row: Maximum row number to consider (optional)
        Returns:
            List of dictionaries, where each dict contains row, start_chair, end_chair, and count
        """
        # Filter seats by max_row if provided
        if max_row is not None:
            try:
                seats = [s for s in seats if int(s.row) <= max_row]
            except ValueError:
                # If row is not numeric, we can't compare, so skip filtering
                main_logger.warning(f"Row value is not numeric: {s.row}")
        # Group seats by row
        seats_by_row: Dict[str, List[Seat]] = {}
        for seat in seats:
            if seat.row not in seats_by_row:
                seats_by_row[seat.row] = []
            seats_by_row[seat.row].append(seat)
        # Sort each row by chair number
        for row in seats_by_row:
            seats_by_row[row].sort(key=lambda s: int(
                s.chair) if s.chair.isdigit() else s.chair)
        adjacent_groups = []
        # Process each row separately
        for row, row_seats in seats_by_row.items():
            if len(row_seats) < min_seats:
                continue
            # Find consecutive sequences
            current_sequence = [row_seats[0]]
            for i in range(1, len(row_seats)):
                current_seat = row_seats[i]
                previous_seat = current_sequence[-1]
                # Check if chairs are consecutive
                try:
                    current_chair = int(current_seat.chair)
                    previous_chair = int(previous_seat.chair)
                    is_consecutive = current_chair == previous_chair + 1
                except ValueError:
                    # If chairs are not numeric, compare as strings
                    is_consecutive = False  # For non-numeric chair identifiers, you might need custom logic
                if is_consecutive:
                    current_sequence.append(current_seat)
                else:
                    # End of current sequence
                    if len(current_sequence) >= min_seats:
                        adjacent_groups.append({
                            'row': row,
                            'start_chair': current_sequence[0].chair,
                            'end_chair': current_sequence[-1].chair,
                            'count': len(current_sequence)
                        })
                    current_sequence = [current_seat]
            # Don't forget the last sequence
            if len(current_sequence) >= min_seats:
                adjacent_groups.append({
                    'row': row,
                    'start_chair': current_sequence[0].chair,
                    'end_chair': current_sequence[-1].chair,
                    'count': len(current_sequence)
                })
        return adjacent_groups
    def extract_theater_id(self, url: str) -> Optional[str]:
        """Extract theater_id from URL"""
        match = re.search(r'.*?showURL=(\d+).*', url)
        if match:
            return match.group(1)
        return None
    def get_main_menu_keyboard(self):
        """Create main menu keyboard with command buttons"""
        keyboard = [
            [
                KeyboardButton("🔍 Find Available Seats"),
                KeyboardButton("➕ Monitor Show")
            ],
            [
                KeyboardButton("📋 My Monitored Shows"),
                KeyboardButton("❌ Stop Monitoring")
            ],
            [
                KeyboardButton("❓ Help")
            ]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
        welcome_message = (
            "🎭 Welcome to Theater Seat Finder Bot!\n"
            "I'll help you find available seats for shows.\n"
            "Use the buttons below or commands:\n"
            "/find - Find available seats\n"
            "/monitor - Monitor a show\n"
            "/myshows - View your monitored shows\n"
            "/stop - Stop monitoring shows\n"
            "/help - Show help information"
        )
        await update.message.reply_text(
            welcome_message,
            reply_markup=self.get_main_menu_keyboard()
        )
        # Set initial state explicitly (though it's the default)
        context.user_data['state'] = InitialState()
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = (
            "❓ Theater Seat Finder Bot Help\n"
            "1. Send me a show URL to find seats\n"
            "2. Select from the results to monitor\n"
            "3. I'll notify you when seats become available\n"
            "Available commands:\n"
            "/find - Find available seats\n"
            "/monitor - Monitor a show\n"
            "/myshows - View your monitored shows\n"
            "/stop - Stop monitoring shows\n"
            "/help - Show this help\n"
            "Use the buttons at the bottom of your screen for quick access!"
        )
        await update.message.reply_text(help_text, reply_markup=self.get_main_menu_keyboard())
    async def find_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /find command"""
        await update.message.reply_text(
            "Please send me the show URL",
            reply_markup=self.get_main_menu_keyboard()
        )
        # Store the new state object
        context.user_data['state'] = FindSeatsState()
    async def monitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /monitor command"""
        await update.message.reply_text(
            "Please send me the show URL to monitor",
            reply_markup=self.get_main_menu_keyboard()
        )
        # Store the new state object, initially waiting for the URL
        # We'll update the state object after the URL is received
        # Reuse for initial URL input, or create a MonitorWaitURL state
        context.user_data['state'] = FindSeatsState()
    async def myshows_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /myshows command"""
        chat_id = update.effective_message.chat_id
        user_shows = {k: v for k, v in self.monitored_shows.items()
                      if v.chat_id == chat_id}
        if not user_shows:
            message = "You are not monitoring any shows.\nUse the '➕ Monitor Show' button to start monitoring!"
        else:
            message = "📋 Your monitored shows:\n"
            keyboard = []
            for key, show in user_shows.items():
                row_info = f"Max row: {show.max_row if show.max_row is not None else 'Unlimited'}"
                message += f"• Show ID: {show.theater_id}\n"
                message += f"  Min seats: {show.min_seats}\n"
                message += f"  {row_info}\n"
                message += f"  Last checked: {len(show.last_available_groups)} groups found\n"
                # Add inline button for each show to manage it
                keyboard.append([InlineKeyboardButton(
                    f"Manage: {show.theater_id}",
                    callback_data=f'manage_{key}')])
            # Add back button if there are shows
            if keyboard:
                keyboard.append([InlineKeyboardButton(
                    "Back to Menu", callback_data='main_menu')])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(message, reply_markup=reply_markup)
            return
        await update.message.reply_text(message, reply_markup=self.get_main_menu_keyboard())
    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command"""
        chat_id = update.effective_message.chat_id
        user_shows = {k: v for k, v in self.monitored_shows.items()
                      if v.chat_id == chat_id}
        if not user_shows:
            message = "You are not monitoring any shows."
        else:
            message = "Select a show to stop monitoring:\n"
            keyboard = []
            for key, show in user_shows.items():
                keyboard.append([InlineKeyboardButton(
                    f"Stop: {show.theater_id} (Min: {show.min_seats})",
                    callback_data=f'stop_{key}')])
            # Add back button
            keyboard.append([InlineKeyboardButton(
                "Back to Menu", callback_data='main_menu')])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(message, reply_markup=reply_markup)
            return
        await update.message.reply_text(message, reply_markup=self.get_main_menu_keyboard())
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages and button commands"""
        text = update.message.text.strip()
        chat_id = update.effective_message.chat_id
        # --- NEW: Check current state BEFORE processing button commands ---
        current_state = context.user_data.get('state')
        # If we are in a specific input state (like waiting for max row),
        # and the user clicks a button command, we should clear the state first
        # and then process the button command as if the state was InitialState.
        if isinstance(current_state, (ChangeMaxRowState, MonitorSetupState)):
            # Check if the input *is* a button command
            button_commands = [
                "🔍 Find Available Seats", "➕ Monitor Show",
                "📋 My Monitored Shows", "❌ Stop Monitoring", "❓ Help"
            ]
            if text in button_commands:
                main_logger.debug(
                    f"User clicked button '{text}' while in state {type(current_state).__name__}. Clearing state and processing button.")
                # Clear the specific input state
                context.user_data.pop('state', None)
                # The code below will now process the button command with InitialState or no state set
        # Handle button commands (These are processed *after* potential state clearing)
        if text == "🔍 Find Available Seats":
            await update.message.reply_text(
                "Please send me the show URL",
                reply_markup=self.get_main_menu_keyboard()
            )
            # Store the new state object
            context.user_data['state'] = FindSeatsState()
            return
        elif text == "➕ Monitor Show":
            await update.message.reply_text(
                "Please send me the show URL to monitor",
                reply_markup=self.get_main_menu_keyboard()
            )
            # Store the state object for initial URL input for monitoring
            # Could also be MonitorWaitURLState
            context.user_data['state'] = FindSeatsState()
            return
        elif text == "📋 My Monitored Shows":
            user_shows = {
                k: v for k, v in self.monitored_shows.items() if v.chat_id == chat_id}
            if not user_shows:
                message = "You are not monitoring any shows.\nUse the '➕ Monitor Show' button to start monitoring!"
            else:
                message = "📋 Your monitored shows:\n"
                keyboard = []
                for key, show in user_shows.items():
                    row_info = f"Max row: {show.max_row if show.max_row is not None else 'Unlimited'}"
                    message += f"• Show ID: {show.theater_id}\n"
                    message += f"  Min seats: {show.min_seats}\n"
                    message += f"  {row_info}\n"
                    message += f"  Last checked: {len(show.last_available_groups)} groups found\n"
                    # Add inline button for each show to manage it
                    keyboard.append([InlineKeyboardButton(
                        f"Manage: {show.theater_id}",
                        callback_data=f'manage_{key}')])
                # Add back button if there are shows
                if keyboard:
                    keyboard.append([InlineKeyboardButton(
                        "Back to Menu", callback_data='main_menu')])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(message, reply_markup=reply_markup)
                return
            await update.message.reply_text(message, reply_markup=self.get_main_menu_keyboard())
            return
        elif text == "❌ Stop Monitoring":
            user_shows = {
                k: v for k, v in self.monitored_shows.items() if v.chat_id == chat_id}
            if not user_shows:
                message = "You are not monitoring any shows."
            else:
                message = "Select a show to stop monitoring:\n"
                keyboard = []
                for key, show in user_shows.items():
                    keyboard.append([InlineKeyboardButton(
                        f"Stop: {show.theater_id} (Min: {show.min_seats})",
                        callback_data=f'stop_{key}')])
                # Add back button
                keyboard.append([InlineKeyboardButton(
                    "Back to Menu", callback_data='main_menu')])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(message, reply_markup=reply_markup)
                return
            await update.message.reply_text(message, reply_markup=self.get_main_menu_keyboard())
            return
        elif text == "❓ Help":
            help_text = (
                "❓ Theater Seat Finder Bot Help\n"
                "1. Send me a show URL to find seats\n"
                "2. Select from the results to monitor\n"
                "3. I'll notify you when seats become available\n"
                "Available commands:\n"
                "/find - Find available seats\n"
                "/monitor - Monitor a show\n"
                "/myshows - View your monitored shows\n"
                "/stop - Stop monitoring shows\n"
                "/help - Show this help\n"
                "Use the buttons at the bottom of your screen for quick access!"
            )
            await update.message.reply_text(help_text, reply_markup=self.get_main_menu_keyboard())
            return
        # --- Handle states based on the object type (After button commands are processed) ---
        # Refresh the state in case it was cleared by a button command
        current_state = context.user_data.get('state')
        # Handle max row setting for specific show (ChangeMaxRowState)
        if isinstance(current_state, ChangeMaxRowState):
            if not text.isdigit():
                await update.message.reply_text(
                    "Please enter a valid number (0 for unlimited).",
                    reply_markup=self.get_main_menu_keyboard()
                )
                return
            max_row = int(text)
            if max_row == 0:
                max_row = None  # 0 means unlimited
            key = current_state.key
            if key and key in self.monitored_shows:
                self.monitored_shows[key].max_row = max_row
                self.save_db()
                status = f"unlimited" if max_row is None else str(max_row)
                await update.message.reply_text(
                    f"✅ Successfully updated max row to {status} for show {self.monitored_shows[key].theater_id}.",
                    reply_markup=self.get_main_menu_keyboard()
                )
            else:
                await update.message.reply_text(
                    "❌ Error updating max row. Please try again.",
                    reply_markup=self.get_main_menu_keyboard()
                )
            # Clear the state after handling
            context.user_data.pop('state', None)
            return
        # Handle monitoring setup steps (MonitorSetupState)
        if isinstance(current_state, MonitorSetupState):
            if current_state.waiting_for == 'min_seats':
                await self.handle_min_seats_input(update, context, text, current_state.temp_theater_id)
                return
            elif current_state.waiting_for == 'max_row_setup':
                await self.handle_max_row_input(update, context, text, current_state.temp_theater_id, current_state.temp_min_seats)
                return
        # Handle finding seats (FindSeatsState)
        if isinstance(current_state, FindSeatsState):
            await self.find_seats_for_url(update, context, text)
            # Clear the state after handling the URL
            context.user_data.pop('state', None)
            return
        # Default state handling: check if it's a URL
        if text.startswith('http'):
            await self.handle_url(update, context, text)
        else:
            await update.message.reply_text("Please send a valid show URL or use the menu buttons.",
                                            reply_markup=self.get_main_menu_keyboard())
            # Ensure state is InitialState if no specific action is pending
            if not isinstance(current_state, InitialState):
                context.user_data['state'] = InitialState()
    async def find_seats_for_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
        """Find seats for a given URL"""
        theater_id = self.extract_theater_id(url)
        if not theater_id:
            await update.message.reply_text(
                "Invalid URL format. Please send a valid URL",
                reply_markup=self.get_main_menu_keyboard()
            )
            return
        await update.message.reply_text("Searching for available seats...")
        available_seats = await self.fetch_and_parse_chairmap(theater_id)
        if not available_seats:
            await update.message.reply_text("No available seats found or error occurred.",
                                            reply_markup=self.get_main_menu_keyboard())
            return
        # Find adjacent seats with default min of 2 (no max row filter for immediate search)
        adjacent_groups = self.find_adjacent_seats(
            available_seats, min_seats=DEFAULT_MIN_SEATS, max_row=None)
        if adjacent_groups:
            message = f"Found {len(adjacent_groups)} groups of adjacent seats:\n"
            for i, group in enumerate(adjacent_groups, 1):
                message += f"{i}. {group['count']} adjacent seats at row {group['row']}: Seat numbers {group['start_chair']} - {group['end_chair']}\n"
        else:
            message = "No adjacent seats found that meet your criteria."
        await update.message.reply_text(message, reply_markup=self.get_main_menu_keyboard())
    async def start_monitoring(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
        """Start monitoring a show for available seats - Called from URL handler or command"""
        theater_id = self.extract_theater_id(url)
        if not theater_id:
            await update.message.reply_text(
                "Invalid URL format. Please send a valid URL",
                reply_markup=self.get_main_menu_keyboard()
            )
            return
        # Ask for minimum number of seats
        await update.message.reply_text(
            "How many adjacent seats do you need? (Enter a number)",
            reply_markup=self.get_main_menu_keyboard()
        )
        # Store the new state object for monitoring setup, waiting for min_seats
        context.user_data['state'] = MonitorSetupState(
            temp_theater_id=theater_id, waiting_for='min_seats')
    async def handle_min_seats_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, theater_id: str):
        """Handle min seats input during monitoring setup"""
        if not text.isdigit():
            await update.message.reply_text(
                "Please enter a valid number.",
                reply_markup=self.get_main_menu_keyboard()
            )
            # Don't clear the state so user can try again
            return
        min_seats = int(text)
        chat_id = update.effective_message.chat_id
        if not theater_id:
            await update.message.reply_text(
                "Something went wrong. Please try again.",
                reply_markup=self.get_main_menu_keyboard()
            )
            # Clear state if error occurs
            context.user_data.pop('state', None)
            return
        # Ask for max row after getting min seats
        await update.message.reply_text(
            "What is the maximum row number you want to consider? (Enter a number, or 0 for unlimited)",
            reply_markup=self.get_main_menu_keyboard()
        )
        # Update the state object to wait for max_row
        context.user_data['state'] = MonitorSetupState(
            temp_theater_id=theater_id, waiting_for='max_row_setup', temp_min_seats=min_seats)
    async def handle_max_row_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, theater_id: str, min_seats: int):
        """Handle max row input during monitoring setup"""
        if not text.isdigit():
            await update.message.reply_text(
                "Please enter a valid number (0 for unlimited).",
                reply_markup=self.get_main_menu_keyboard()
            )
            # Don't clear the state so user can try again
            return
        max_row = int(text)
        if max_row == 0:
            max_row = None  # 0 means unlimited
        chat_id = update.effective_message.chat_id
        if not theater_id or min_seats is None:
            await update.message.reply_text(
                "Something went wrong. Please try again.",
                reply_markup=self.get_main_menu_keyboard()
            )
            context.user_data.pop('state', None)
            return
        # Create a unique key for this monitoring
        key = f"{chat_id}_{theater_id}"
        # Add to monitored shows
        from datetime import datetime
        self.monitored_shows[key] = MonitoredShow(
            chat_id=chat_id,
            theater_id=theater_id,
            min_seats=min_seats,
            created_at=datetime.now().isoformat(),
            last_available_groups=[],
            max_row=max_row
        )
        self.save_db()
        # Start monitoring task
        await self.start_monitoring_task(key, theater_id, min_seats, chat_id)
        await update.message.reply_text(
            f"✅ Successfully started monitoring show {theater_id} for {min_seats} adjacent seats!\n"
            f"Maximum row: {max_row if max_row is not None else 'Unlimited'}\n"
            "I'll notify you when available seats are found.",
            reply_markup=self.get_main_menu_keyboard()
        )
        # Clear the state after successful setup
        context.user_data.pop('state', None)
    async def start_monitoring_task(self, key: str, theater_id: str, min_seats: int, chat_id: int):
        """Start a monitoring task for a specific show"""
        if key in self.monitoring_tasks:
            # Cancel existing task if any
            self.monitoring_tasks[key].cancel()
        # Create and store the monitoring task
        task = asyncio.create_task(
            self.monitor_show(theater_id, min_seats, chat_id, key)
        )
        self.monitoring_tasks[key] = task
    async def stop_monitoring_task(self, key: str):
        """Stop a monitoring task for a specific show"""
        if key in self.monitoring_tasks:
            # Cancel the task
            self.monitoring_tasks[key].cancel()
            del self.monitoring_tasks[key]
    def _compare_groups(self, old_groups: List[Dict], new_groups: List[Dict]) -> List[Dict]:
        """
        Compare old and new seat groups to find new additions and changes.
        Args:
            old_groups: List of previous seat groups
            new_groups: List of current seat groups
        Returns:
            List of groups that are new or have changed
        """
        # Create a set of unique identifiers for old groups
        old_group_keys = set()
        for group in old_groups:
            key = (group['row'], group['start_chair'], group['end_chair'])
            old_group_keys.add(key)
        # Find new groups that weren't in the old list
        new_added = []
        for group in new_groups:
            key = (group['row'], group['start_chair'], group['end_chair'])
            if key not in old_group_keys:
                new_added.append(group)
        return new_added
    async def monitor_show(self, theater_id: str, min_seats: int, chat_id: int, key: str):
        """Monitor a show and notify when seats are available"""
        main_logger.info(
            f"Started monitoring show {theater_id} for {min_seats} seats for user {chat_id}")
        try:
            while key in self.monitored_shows and self.monitored_shows[key].chat_id == chat_id:
                available_seats = await self.fetch_and_parse_chairmap(theater_id)
                if available_seats:
                    # Use the max_row setting from the monitored show
                    max_row = self.monitored_shows[key].max_row
                    adjacent_groups = self.find_adjacent_seats(
                        available_seats, min_seats=min_seats, max_row=max_row)
                    # Check for changes since last check using the new comparison method
                    old_groups = self.monitored_shows[key].last_available_groups
                    new_groups = self._compare_groups(
                        old_groups, adjacent_groups)
                    if new_groups:
                        message = f"🎉 New available seats found for show {theater_id}!\n"
                        # Show all new groups
                        for i, group in enumerate(new_groups, 1):
                            message += f"{i}. {group['count']} adjacent seats: Row {group['row']}, Chair {group['start_chair']} - {group['end_chair']}\n"
                        # Also include total available groups
                        message += f"\nTotal available groups: {len(adjacent_groups)}"
                        # Send notification to user
                        try:
                            await self.application.bot.send_message(
                                chat_id=chat_id,
                                text=message
                            )
                            main_logger.info(
                                f"Notification sent to chat {chat_id} for show {theater_id}")
                        except Exception as e:
                            main_logger.error(
                                f"Error sending message to chat {chat_id}: {e}")
                    # Update the stored groups
                    self.monitored_shows[key].last_available_groups = adjacent_groups
                    self.save_db()
                # Wait before next check
                await asyncio.sleep(MONITORING_INTERVAL)
        except asyncio.CancelledError:
            main_logger.info(
                f"Monitoring task for show {theater_id} was cancelled")
        except Exception as e:
            main_logger.error(
                f"Error in monitoring loop for show {theater_id}: {e}")
    async def handle_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
        """Handle URL sent without context"""
        theater_id = self.extract_theater_id(url)
        if not theater_id:
            await update.message.reply_text(
                "Invalid URL format. Please send a valid URL",
                reply_markup=self.get_main_menu_keyboard()
            )
            return
        keyboard = [
            [InlineKeyboardButton("🔍 Find Seats Now",
                                  callback_data=f'find_now_{theater_id}')],
            [InlineKeyboardButton("➕ Monitor This Show",
                                  callback_data=f'monitor_{theater_id}')],
            [InlineKeyboardButton("Back to Menu", callback_data='main_menu')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Found show ID: {theater_id}\nWhat would you like to do?",
            reply_markup=reply_markup
        )
    async def inline_button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button callbacks"""
        query = update.callback_query
        await query.answer()
        if query.data.startswith('find_now_'):
            theater_id = query.data.split('_')[2]
            await query.edit_message_text("Searching for available seats...")
            available_seats = await self.fetch_and_parse_chairmap(theater_id)
            if not available_seats:
                await query.edit_message_text("No available seats found or error occurred.")
                return
            # Find adjacent seats with default min of 2 (no max row filter for immediate search)
            adjacent_groups = self.find_adjacent_seats(
                available_seats, min_seats=DEFAULT_MIN_SEATS, max_row=None)
            if adjacent_groups:
                message = f"Found {len(adjacent_groups)} groups of adjacent seats:\n"
                for i, group in enumerate(adjacent_groups, 1):  # Show ALL groups
                    message += f"{i}. {group['count']} adjacent seats: Row {group['row']}, Chair {group['start_chair']} - {group['end_chair']}\n"
            else:
                message = "No adjacent seats found that meet your criteria."
            keyboard = [[InlineKeyboardButton(
                "Back to Menu", callback_data='main_menu')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(message, reply_markup=reply_markup)
        elif query.data.startswith('monitor_'):
            theater_id = query.data.split('_')[1]
            # Call start_monitoring to handle the initial steps
            # We need to simulate the message flow. We'll call start_monitoring with a mock update or just trigger the state change here.
            # For simplicity, let's trigger the state change directly here.
            await query.edit_message_text("How many adjacent seats do you need? (Enter a number)")
            # Store the new state object for monitoring setup, waiting for min_seats
            context.user_data['state'] = MonitorSetupState(
                temp_theater_id=theater_id, waiting_for='min_seats')
        elif query.data.startswith('stop_'):
            key = query.data.split('_', 1)[1]  # Get the full key after 'stop_'
            # Remove from monitored shows
            if key in self.monitored_shows:
                theater_id = self.monitored_shows[key].theater_id
                del self.monitored_shows[key]
                self.save_db()
                # Stop the monitoring task
                await self.stop_monitoring_task(key)
                await query.edit_message_text(f"✅ Successfully stopped monitoring show {theater_id}")
            else:
                await query.edit_message_text("❌ The show is no longer being monitored.")
        elif query.data.startswith('manage_'):
            # Get the full key after 'manage_'
            key = query.data.split('_', 1)[1]
            if key in self.monitored_shows:
                show = self.monitored_shows[key]
                message = f"Manage Show: {show.theater_id}\n"
                message += f"Min seats: {show.min_seats}\n"
                message += f"Max row: {show.max_row if show.max_row is not None else 'Unlimited'}\n"
                message += f"Last checked: {len(show.last_available_groups)} groups found\n"
                keyboard = [
                    [InlineKeyboardButton(
                        "Change Max Row", callback_data=f'change_max_row_{key}')],
                    [InlineKeyboardButton(
                        "Stop Monitoring", callback_data=f'stop_{key}')],
                    [InlineKeyboardButton(
                        "Back to Menu", callback_data='main_menu')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(message, reply_markup=reply_markup)
            else:
                await query.edit_message_text("❌ Show not found.")
        elif query.data.startswith('change_max_row_'):
            # --- DEBUG PRINTS START ---
            main_logger.info(f"[DEBUG] Received callback_data: {query.data}")
            # Get the full key after 'change_max_row_'
            # OLD (buggy) logic: key = query.data.split('_')[-1]
            # NEW (correct) logic: key = query.data.split('_', 1)[1]
            # Let's test both and see what they produce:
            split_all = query.data.split('_')
            split_one = query.data.split('_', 1)
            main_logger.info(f"[DEBUG] query.data.split('_'): {split_all}")
            main_logger.info(f"[DEBUG] query.data.split('_', 1): {split_one}")
            old_key = split_all[-1] # What the old code used
            new_key = split_one[1] # What the new code should use
            main_logger.info(f"[DEBUG] Old key (split_all[-1]): '{old_key}'")
            main_logger.info(f"[DEBUG] New key (split_one[1]): '{new_key}'")
            # Check which one exists in monitored_shows
            old_exists = old_key in self.monitored_shows
            new_exists = new_key in self.monitored_shows
            main_logger.info(f"[DEBUG] old_key exists in monitored_shows: {old_exists}")
            main_logger.info(f"[DEBUG] new_key exists in monitored_shows: {new_exists}")
            # Use the NEW key for the lookup
            key = new_key
            # --- DEBUG PRINTS END ---
            if key in self.monitored_shows:
                await query.edit_message_text(
                    "What is the new maximum row number you want to consider? (Enter a number, or 0 for unlimited)"
                )
                # Store the new state object for changing max row
                context.user_data['state'] = ChangeMaxRowState(key=key)
            else:
                await query.edit_message_text("❌ Show not found.")
        elif query.data == 'main_menu':
            await query.edit_message_text(
                "🎭 Welcome to the Theater Seat Finder Bot!\n"
                "Use the buttons below or send a show URL:",
                reply_markup=self.get_main_menu_keyboard()
            )
            # Clear state when returning to main menu, set to InitialState
            context.user_data['state'] = InitialState()
    def run(self):
        """Run the bot"""
        application = Application.builder().token(self.token).build()
        # Store application reference for monitoring tasks
        self.application = application
        # Add handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("find", self.find_command))
        application.add_handler(CommandHandler(
            "monitor", self.monitor_command))
        application.add_handler(CommandHandler(
            "myshows", self.myshows_command))
        application.add_handler(CommandHandler("stop", self.stop_command))
        application.add_handler(
            CallbackQueryHandler(self.inline_button_handler))
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self.handle_message))
        main_logger.info("Bot started successfully!")
        application.run_polling()
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Theater Seat Finder Bot')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug logging')
    args = parser.parse_args()
    # Replace with your bot token
    bot_token = os.environ.get(BOT_TOKEN_ENV_VAR)
    if not bot_token:
        print(
            f"Error: Please set the {BOT_TOKEN_ENV_VAR} environment variable.")
        exit(1)
    bot = TheaterBot(bot_token, debug=args.debug)
    bot.run()