"""
RadioDan Telegram Channel

Provides user interaction through Telegram:
- /start - Welcome message
- /tunein - Get stream URL
- /status - Check system status
- /say <text> - Speak text through the stream
- Restricts access to allowed user IDs
"""

import logging
from typing import TYPE_CHECKING, Callable

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

if TYPE_CHECKING:
    from bridge.services.tts_service import TTSService
    from bridge.services.stt_service import STTService
    from bridge.services.llm_service import LLMService
    from bridge.audio.mixer import LiquidsoapMixer
    from bridge.plugins.base import DJPlugin

from bridge.booth import booth

logger = logging.getLogger(__name__)


class TelegramChannel:
    """Telegram bot for RadioDan control interface."""

    def __init__(
        self,
        token: str,
        allowed_users: list[int],
        stream_url_getter: Callable[[], str],
        tts_service: "TTSService | None" = None,
        mixer: "LiquidsoapMixer | None" = None,
        stt_service: "STTService | None" = None,
        llm_service: "LLMService | None" = None,
        station_name: str = "Radio Dan",
    ):
        """
        Initialize Telegram channel.

        Args:
            token: Telegram bot token from BotFather
            allowed_users: List of allowed Telegram user IDs
            stream_url_getter: Callable that returns the current stream URL
            tts_service: TTS service for generating speech
            mixer: Liquidsoap mixer for queueing audio
            stt_service: STT service for transcribing voice messages
            llm_service: LLM service for chat
            station_name: Station name for user-facing messages
        """
        self.token = token
        self.allowed_users = set(allowed_users)
        self.get_stream_url = stream_url_getter
        self.tts_service = tts_service
        self.mixer = mixer
        self.stt_service = stt_service
        self.llm_service = llm_service
        self.station_name = station_name
        self.app: Application | None = None

        # Volume step size for ±buttons
        self.volume_step = 0.1

        # LLM chat mode toggle
        self.llm_chat_mode = False

        # Registered plugins
        self._plugins: list["DJPlugin"] = []

    def register_plugins(self, plugins: list["DJPlugin"]) -> None:
        """Register plugins for Telegram integration (menu buttons, commands, callbacks)."""
        self._plugins = plugins

    def _is_allowed(self, user_id: int) -> bool:
        """Check if user is allowed to interact with the bot."""
        # If no users are configured, allow all (useful for testing)
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    async def _check_access(self, update: Update) -> bool:
        """Check access and send rejection message if not allowed."""
        user = update.effective_user
        if user is None:
            return False

        if not self._is_allowed(user.id):
            logger.warning(f"Unauthorized access attempt from user {user.id} (@{user.username})")
            await update.message.reply_text(
                "Sorry, you're not authorized to use this bot."
            )
            return False
        return True

    def _build_main_menu_keyboard(self) -> InlineKeyboardMarkup:
        """Build the main menu inline keyboard."""
        # LLM chat button shows current state
        llm_label = "🤖 LLM Chat ON" if self.llm_chat_mode else "🤖 LLM Chat"

        keyboard = [
            [
                InlineKeyboardButton("🎚️ Audio Controls", callback_data="menu:audio"),
                InlineKeyboardButton("📊 Status", callback_data="menu:status"),
            ],
            [
                InlineKeyboardButton("📻 Tune In", callback_data="menu:tunein"),
                InlineKeyboardButton("🕐 Time", callback_data="menu:time"),
                InlineKeyboardButton(llm_label, callback_data="menu:llm_chat"),
            ],
        ]

        # Append plugin menu buttons (rows of 3)
        plugin_buttons = []
        for plugin in self._plugins:
            for btn in plugin.telegram_menu_buttons():
                plugin_buttons.append(
                    InlineKeyboardButton(btn.label, callback_data=btn.callback_data)
                )
        # Group into rows of 3
        for i in range(0, len(plugin_buttons), 3):
            keyboard.append(plugin_buttons[i : i + 3])

        return InlineKeyboardMarkup(keyboard)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command - welcome message with main menu."""
        if not await self._check_access(update):
            return

        user = update.effective_user
        logger.info(f"User {user.id} (@{user.username}) started the bot")

        await update.message.reply_text(
            f"🎧 *Welcome to {self.station_name}!*\n\n"
            "I'm your ambient AI work companion. "
            "I'll keep you connected through audio.\n\n"
            f"_Vibe with {self.station_name}_ 🎵",
            parse_mode="Markdown",
            reply_markup=self._build_main_menu_keyboard(),
        )

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /menu command - show main menu."""
        if not await self._check_access(update):
            return

        await update.message.reply_text(
            f"🎧 *{self.station_name} Menu*",
            parse_mode="Markdown",
            reply_markup=self._build_main_menu_keyboard(),
        )

    async def cmd_tunein(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /tunein command - provide stream URL."""
        if not await self._check_access(update):
            return

        stream_url = self.get_stream_url()
        logger.info(f"Sending stream URL to user {update.effective_user.id}")

        await update.message.reply_text(
            f"🎵 *Tune In to {self.station_name}*\n\n"
            f"Stream URL:\n`{stream_url}`\n\n"
            f"Open this URL in any audio player (VLC, your phone's music app, etc.)\n\n"
            f"_Tip: On mobile, try opening the link in your browser - "
            f"it should offer to open in your music player._",
            parse_mode="Markdown",
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command - show system status."""
        if not await self._check_access(update):
            return

        # Check service health
        tts_status = "🟢 Online" if self.tts_service and await self.tts_service.health_check() else "🔴 Offline"
        mixer_status = "🟢 Online" if self.mixer and await self.mixer.health_check() else "🔴 Offline"

        await update.message.reply_text(
            f"📊 *{self.station_name} Status*\n\n"
            "• Stream: 🟢 Online\n"
            f"• TTS: {tts_status}\n"
            f"• Mixer: {mixer_status}\n"
            "• Claude Code: ⚪ Idle\n\n"
            "_Phase 2: TTS Integration_",
            parse_mode="Markdown",
        )

    async def cmd_say(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /say command - speak text through the stream."""
        if not await self._check_access(update):
            return

        # Get the text to speak
        if not context.args:
            await update.message.reply_text(
                "🎤 *Usage:* `/say <text>`\n\n"
                f"Example: `/say Hello, welcome to {self.station_name}!`",
                parse_mode="Markdown",
            )
            return

        text = " ".join(context.args)
        user = update.effective_user
        booth.telegram(f"/say {text}", user.username)
        logger.info(f"User {user.id} requested TTS: '{text[:50]}...'")

        # Check if services are available
        if not self.tts_service or not self.mixer:
            await update.message.reply_text(
                "❌ TTS services are not configured."
            )
            return

        # Send acknowledgment
        status_msg = await update.message.reply_text("🎤 Generating speech...")

        try:
            # Generate TTS audio
            audio_path = await self.tts_service.speak(text)

            # Queue it for playback
            success = await self.mixer.queue_tts(audio_path)

            if success:
                await status_msg.edit_text(
                    f"🔊 *Speaking:* _{text[:100]}{'...' if len(text) > 100 else ''}_",
                    parse_mode="Markdown",
                )
            else:
                await status_msg.edit_text(
                    "⚠️ Generated audio but failed to queue for playback."
                )

        except RuntimeError as e:
            logger.error(f"TTS error: {e}")
            await status_msg.edit_text(f"❌ TTS error: {e}")

    # =========================================================================
    # AUDIO CONTROLS
    # =========================================================================

    def _volume_bar(self, value: float, width: int = 10) -> str:
        """Generate a visual volume bar."""
        filled = int(value * width)
        empty = width - filled
        return "█" * filled + "░" * empty

    async def _build_audio_keyboard(self) -> tuple[str, InlineKeyboardMarkup]:
        """Build the audio control message and keyboard."""
        if not self.mixer:
            return "❌ Mixer not available", InlineKeyboardMarkup([])

        # Get current volumes
        volumes = await self.mixer.get_volumes()
        music_vol = volumes["music_vol"]
        tts_vol = volumes["tts_vol"]
        duck_amount = volumes["duck_amount"]

        # Build message with volume bars
        music_bar = self._volume_bar(music_vol)
        tts_bar = self._volume_bar(tts_vol)
        duck_bar = self._volume_bar(duck_amount)

        music_pct = int(music_vol * 100)
        tts_pct = int(tts_vol * 100)
        duck_pct = int(duck_amount * 100)

        # Mute indicator
        music_icon = "🔇" if self.mixer.music_muted else "🎵"
        tts_icon = "🔇" if self.mixer.tts_muted else "🎤"

        # Random mode indicator
        random_state = "ON" if self.mixer.random_mode else "OFF"

        message = (
            f"🎧 *{self.station_name} Audio*\n"
            "─────────────────────────\n"
            f"{music_icon} Music: `{music_bar}` {music_pct}%\n\n"
            f"{tts_icon} Voice: `{tts_bar}` {tts_pct}%\n\n"
            f"🔉 Duck:  `{duck_bar}` {duck_pct}%\n"
            "─────────────────────────\n"
            f"🔀 Random: {random_state}"
        )

        # Build keyboard
        keyboard = [
            # Music row
            [
                InlineKeyboardButton("🔇 Mute" if not self.mixer.music_muted else "🔊 Unmute", callback_data="m:mute"),
                InlineKeyboardButton("➖", callback_data="m:down"),
                InlineKeyboardButton("➕", callback_data="m:up"),
            ],
            # Voice row
            [
                InlineKeyboardButton("🔇 Mute" if not self.mixer.tts_muted else "🔊 Unmute", callback_data="v:mute"),
                InlineKeyboardButton("➖", callback_data="v:down"),
                InlineKeyboardButton("➕", callback_data="v:up"),
            ],
            # Duck row
            [
                InlineKeyboardButton("Duck ➖", callback_data="d:down"),
                InlineKeyboardButton("Duck ➕", callback_data="d:up"),
            ],
            # Action row
            [
                InlineKeyboardButton("⏭ Next", callback_data="next"),
                InlineKeyboardButton("🔀 Random", callback_data="random"),
                InlineKeyboardButton("🗑 Flush", callback_data="flush"),
            ],
            # Back to menu
            [
                InlineKeyboardButton("« Back to Menu", callback_data="menu:back"),
            ],
        ]

        return message, InlineKeyboardMarkup(keyboard)

    async def cmd_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /audio command - show audio controls."""
        if not await self._check_access(update):
            return

        user = update.effective_user
        logger.info(f"User {user.id} opened audio controls")
        booth.telegram("/audio", user.username)

        message, keyboard = await self._build_audio_keyboard()
        await update.message.reply_text(
            message,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle all inline keyboard button presses."""
        query = update.callback_query
        user = update.effective_user

        if not user or not self._is_allowed(user.id):
            await query.answer("Not authorized", show_alert=True)
            return

        await query.answer()  # Acknowledge the button press

        action = query.data
        username = user.username

        # Handle menu navigation
        if action.startswith("menu:"):
            await self._handle_menu_callback(query, action)
            return

        # Handle plugin callbacks (format: "plugin:<name>:<action>")
        if action.startswith("plugin:"):
            await self._handle_plugin_callback(query, action)
            return

        # Handle audio controls (require mixer)
        if not self.mixer:
            await query.answer("Mixer not available", show_alert=True)
            return

        # Handle volume adjustments
        if action == "m:up":
            volumes = await self.mixer.get_volumes()
            new_vol = min(1.0, volumes["music_vol"] + self.volume_step)
            await self.mixer.set_music_volume(new_vol)
            booth.mixer_volume("Music", new_vol, username)

        elif action == "m:down":
            volumes = await self.mixer.get_volumes()
            new_vol = max(0.0, volumes["music_vol"] - self.volume_step)
            await self.mixer.set_music_volume(new_vol)
            booth.mixer_volume("Music", new_vol, username)

        elif action == "m:mute":
            is_muted, vol = await self.mixer.toggle_music_mute()
            booth.mixer_volume("Music", vol, username)

        elif action == "v:up":
            volumes = await self.mixer.get_volumes()
            new_vol = min(1.0, volumes["tts_vol"] + self.volume_step)
            await self.mixer.set_tts_volume(new_vol)
            booth.mixer_volume("Voice", new_vol, username)

        elif action == "v:down":
            volumes = await self.mixer.get_volumes()
            new_vol = max(0.0, volumes["tts_vol"] - self.volume_step)
            await self.mixer.set_tts_volume(new_vol)
            booth.mixer_volume("Voice", new_vol, username)

        elif action == "v:mute":
            is_muted, vol = await self.mixer.toggle_tts_mute()
            booth.mixer_volume("Voice", vol, username)

        elif action == "d:up":
            volumes = await self.mixer.get_volumes()
            new_vol = min(1.0, volumes["duck_amount"] + self.volume_step)
            await self.mixer.set_duck_amount(new_vol)
            booth.mixer_volume("Duck", new_vol, username)

        elif action == "d:down":
            volumes = await self.mixer.get_volumes()
            new_vol = max(0.0, volumes["duck_amount"] - self.volume_step)
            await self.mixer.set_duck_amount(new_vol)
            booth.mixer_volume("Duck", new_vol, username)

        elif action == "next":
            await self.mixer.next_track()
            booth.mixer_skip("Next track", username)

        elif action == "random":
            new_state = await self.mixer.toggle_random()
            booth.mixer_random(new_state, username)

        elif action == "flush":
            await self.mixer.flush_tts()
            booth.mixer_flush("TTS queue", username)

        # Update the message with new state
        message, keyboard = await self._build_audio_keyboard()
        try:
            await query.edit_message_text(
                message,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            # Message might not have changed
            logger.debug(f"Could not update audio menu: {e}")

    async def _handle_menu_callback(self, query, action: str) -> None:
        """Handle menu navigation callbacks."""
        menu_action = action.split(":")[1]

        if menu_action == "back":
            # Return to main menu
            await query.edit_message_text(
                f"🎧 *{self.station_name} Menu*",
                parse_mode="Markdown",
                reply_markup=self._build_main_menu_keyboard(),
            )

        elif menu_action == "audio":
            # Show audio controls
            message, keyboard = await self._build_audio_keyboard()
            await query.edit_message_text(
                message,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        elif menu_action == "status":
            # Show status inline
            tts_status = "🟢 Online" if self.tts_service and await self.tts_service.health_check() else "🔴 Offline"
            mixer_status = "🟢 Online" if self.mixer and await self.mixer.health_check() else "🔴 Offline"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to Menu", callback_data="menu:back")],
            ])

            await query.edit_message_text(
                f"📊 *{self.station_name} Status*\n\n"
                "• Stream: 🟢 Online\n"
                f"• TTS: {tts_status}\n"
                f"• Mixer: {mixer_status}\n"
                "• Claude Code: ⚪ Idle",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        elif menu_action == "tunein":
            # Show tune in info inline
            stream_url = self.get_stream_url()

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to Menu", callback_data="menu:back")],
            ])

            await query.edit_message_text(
                f"📻 *Tune In to {self.station_name}*\n\n"
                f"Stream URL:\n`{stream_url}`\n\n"
                f"Open in VLC or any audio player.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        elif menu_action == "time":
            # Announce current time via TTS
            from datetime import datetime

            now = datetime.now()
            time_text = f"Server time is {now.hour}, {now.minute}, {now.second}"

            if self.tts_service and self.mixer:
                audio_path = await self.tts_service.speak(time_text)
                await self.mixer.queue_tts(audio_path)

                await query.edit_message_text(
                    f"🕐 *Time announced:* {now.strftime('%H:%M:%S')}",
                    parse_mode="Markdown",
                    reply_markup=self._build_main_menu_keyboard(),
                )
            else:
                await query.edit_message_text(
                    "❌ TTS not available",
                    parse_mode="Markdown",
                    reply_markup=self._build_main_menu_keyboard(),
                )

        elif menu_action == "llm_chat":
            # Toggle LLM chat mode
            self.llm_chat_mode = not self.llm_chat_mode
            state = "ON" if self.llm_chat_mode else "OFF"
            logger.info(f"LLM chat mode toggled: {state}")

            status_text = (
                f"🤖 *LLM Chat: {state}*\n\n"
                + ("Send voice or text messages to chat!\n"
                   "Responses will play on the stream."
                   if self.llm_chat_mode else
                   "Tap the button again to enable chat.")
            )

            await query.edit_message_text(
                status_text,
                parse_mode="Markdown",
                reply_markup=self._build_main_menu_keyboard(),
            )

    async def _handle_plugin_callback(self, query, action: str) -> None:
        """Route plugin callbacks to the matching plugin."""
        # Format: "plugin:<name>:<action>"
        parts = action.split(":", 2)
        if len(parts) < 3:
            return

        _, plugin_name, plugin_action = parts

        for plugin in self._plugins:
            if plugin.instance_id == plugin_name or plugin.name == plugin_name:
                try:
                    response = await plugin.handle_telegram_callback(plugin_action)
                    if response:
                        await query.edit_message_text(
                            response,
                            parse_mode="Markdown",
                            reply_markup=self._build_main_menu_keyboard(),
                        )
                    else:
                        # Refresh menu to reflect any state change
                        await query.edit_message_text(
                            f"🎧 *{self.station_name} Menu*",
                            parse_mode="Markdown",
                            reply_markup=self._build_main_menu_keyboard(),
                        )
                except Exception as e:
                    logger.exception(f"Plugin callback error ({plugin_name})")
                    await query.edit_message_text(
                        f"❌ Plugin error: {e}",
                        parse_mode="Markdown",
                        reply_markup=self._build_main_menu_keyboard(),
                    )
                return

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle non-command messages (voice and text for LLM chat)."""
        if not await self._check_access(update):
            return

        user = update.effective_user
        message = update.message

        if message.voice:
            logger.info(f"Voice message from {user.id}: duration={message.voice.duration}s")
            booth.telegram(f"Voice message ({message.voice.duration}s)", user.username)

            # Check if LLM chat mode is enabled
            if not self.llm_chat_mode:
                await message.reply_text(
                    "🤖 Enable LLM Chat mode first (tap button in /menu)"
                )
                return

            # Check if services are available
            if not self.stt_service or not self.llm_service or not self.tts_service or not self.mixer:
                await message.reply_text("❌ Voice chat services not configured.")
                return

            # Send processing indicator
            status_msg = await message.reply_text("👂 Listening...")

            try:
                # 1. Download voice file
                import tempfile
                from pathlib import Path

                voice_file = await context.bot.get_file(message.voice.file_id)
                tmp_dir = Path(tempfile.gettempdir())
                ogg_path = tmp_dir / f"voice_{message.voice.file_unique_id}.ogg"
                await voice_file.download_to_drive(ogg_path)

                # 2. Transcribe with Whisper
                await status_msg.edit_text("🎤 Transcribing...")
                user_text = await self.stt_service.transcribe(ogg_path)

                if not user_text:
                    await status_msg.edit_text("❌ Could not transcribe audio.")
                    return

                # 3. Process through LLM → TTS → Stream
                await status_msg.edit_text("🤖 Thinking...")
                await self._process_llm_chat(message, user_text, status_msg, show_transcription=True)

                # Cleanup temp file
                ogg_path.unlink(missing_ok=True)

            except Exception as e:
                logger.exception(f"Voice processing error: {e}")
                await status_msg.edit_text(f"❌ Error: {e}")

        elif message.text:
            logger.info(f"Text message from {user.id}: {message.text[:50]}...")
            booth.telegram(message.text[:50], user.username)

            # Check if LLM chat mode is enabled
            if not self.llm_chat_mode:
                await message.reply_text(
                    "💬 Enable LLM Chat mode to chat (tap button in /menu)"
                )
                return

            # Check if services are available
            if not self.llm_service or not self.tts_service or not self.mixer:
                await message.reply_text("❌ Chat services not configured.")
                return

            # Send processing indicator
            status_msg = await message.reply_text("🤖 Thinking...")

            try:
                await self._process_llm_chat(message, message.text, status_msg)
            except Exception as e:
                logger.exception(f"Text chat error: {e}")
                await status_msg.edit_text(f"❌ Error: {e}")

    async def _process_llm_chat(
        self,
        message,
        user_text: str,
        status_msg,
        show_transcription: bool = False,
    ) -> None:
        """
        Common handler for LLM chat (voice or text input).

        Args:
            message: The original Telegram message
            user_text: The user's text (either typed or transcribed)
            status_msg: The status message to update
            show_transcription: Whether to show the transcription in the response
        """
        # 1. Get LLM response
        response_text = await self.llm_service.chat(user_text)

        # 2. Generate TTS
        await status_msg.edit_text("🔊 Generating speech...")
        audio_path = await self.tts_service.speak(response_text)

        # 3. Queue for playback
        await self.mixer.queue_tts(audio_path)

        # 4. Show in Telegram
        if show_transcription:
            reply_text = f"👂 _{user_text}_\n\n🤖 {response_text}"
        else:
            reply_text = f"🤖 {response_text}"

        await status_msg.edit_text(reply_text, parse_mode="Markdown")

    async def start(self) -> None:
        """Start the Telegram bot."""
        if not self.token:
            logger.error("No Telegram bot token configured!")
            raise ValueError("TELEGRAM_BOT_TOKEN not set")

        logger.info("Starting Telegram bot...")

        self.app = Application.builder().token(self.token).build()

        # Register handlers
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("menu", self.cmd_menu))
        self.app.add_handler(CommandHandler("tunein", self.cmd_tunein))
        self.app.add_handler(CommandHandler("say", self.cmd_say))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("audio", self.cmd_audio))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_handler(MessageHandler(
            filters.VOICE | filters.TEXT & ~filters.COMMAND,
            self.handle_message
        ))

        # Register plugin command handlers before polling starts
        for plugin in self._plugins:
            for cmd in plugin.telegram_commands():
                # Create a closure to capture the plugin reference
                async def _plugin_cmd_handler(
                    update: Update,
                    context: ContextTypes.DEFAULT_TYPE,
                    _plugin=plugin,
                    _cmd=cmd,
                ) -> None:
                    if not await self._check_access(update):
                        return
                    response = await _plugin.handle_telegram_callback("command")
                    if response:
                        await update.message.reply_text(
                            response,
                            parse_mode="Markdown",
                            reply_markup=self._build_main_menu_keyboard(),
                        )

                self.app.add_handler(CommandHandler(cmd.command, _plugin_cmd_handler))

        # Start polling
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        # Set up the command menu (appears as "/" button in Telegram)
        bot_commands = [
            BotCommand("menu", "Main menu"),
            BotCommand("audio", "Audio controls"),
            BotCommand("tunein", "Get stream URL"),
            BotCommand("say", "Speak text"),
            BotCommand("status", "System status"),
        ]
        # Add plugin commands to bot menu
        for plugin in self._plugins:
            for cmd in plugin.telegram_commands():
                bot_commands.append(BotCommand(cmd.command, cmd.description))

        await self.app.bot.set_my_commands(bot_commands)

        logger.info("Telegram bot started successfully")

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        if self.app:
            logger.info("Stopping Telegram bot...")
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info("Telegram bot stopped")
