"""Downloader Bot to download media from a Discord Channel."""

import logging
import os
import platform
import random

import asyncpg
import discord
from discord.ext import commands, tasks
from discord.ext.commands import Context, errors
import discordhealthcheck
from arq.connections import ArqRedis

from config import settings
from db.pool import init_schema, open_pool as open_db_pool
from queue_client import open_pool

intents = discord.Intents.default()
intents.message_content = True


class LoggingFormatter(logging.Formatter):
    """
    Custom logging formatter for discord.py.

    Attributes:
        black (str): black ANSI code.
        red (str): red ANSI code.
        green (str): green ANSI code.
        yellow (str): yellow ANSI code.
        blue (str): blue ANSI code.
        gray (str): gray ANSI code.
        reset (str): reset ANSI code.
        bold (str): bold ANSI code.
        COLORS (Dict[int, str]): Relates logging level to a colour/style.
    """

    # Colors
    black = "\x1b[30m"
    red = "\x1b[31m"
    green = "\x1b[32m"
    yellow = "\x1b[33m"
    blue = "\x1b[34m"
    gray = "\x1b[38m"
    # Styles
    reset = "\x1b[0m"
    bold = "\x1b[1m"

    COLORS = {
        logging.DEBUG: gray + bold,
        logging.INFO: blue + bold,
        logging.WARNING: yellow + bold,
        logging.ERROR: red,
        logging.CRITICAL: red + bold,
    }

    def format(self, record):
        log_color = self.COLORS[record.levelno]
        log_format = "(black){asctime}(reset) (levelcolor){levelname:<8}(reset) (green){name}(reset) {message}"
        log_format = log_format.replace("(black)", self.black + self.bold)
        log_format = log_format.replace("(reset)", self.reset)
        log_format = log_format.replace("(levelcolor)", log_color)
        log_format = log_format.replace("(green)", self.green + self.bold)
        formatter = logging.Formatter(log_format, "%Y-%m-%d %H:%M:%S", style="{")
        return formatter.format(record)


logger = logging.getLogger("discord_bot")
logger.setLevel(settings.LOGGING_LEVEL)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(LoggingFormatter())
logger.addHandler(console_handler)


class DiscordBot(commands.Bot):
    """
    Custom Discord Bot class.

    This class overrides some of the base function of the discord.py Bot class
    to provide better functionality for cogs, error handling, and logging.

    Attributes:
        logger (Logger): Logger instance for this bot.
        bot_prefix (str): Bot command prefix.
        invite_link (str): Bot invite link.
        healthcheck_server (Server): Health check server instance.
    """

    def __init__(self) -> None:
        """Initialise the DiscordBot."""
        super().__init__(
            command_prefix=commands.when_mentioned_or(settings.PREFIX),
            intents=intents,
            help_command=None,
        )
        self.logger = logger
        self.bot_prefix = settings.PREFIX
        self.invite_link = settings.INVITE_LINK
        self.healthcheck_server = None
        self.arq_pool: ArqRedis | None = None
        self.db_pool: asyncpg.Pool | None = None

    async def load_cogs(self) -> None:
        """Load all cog extensions from the /cogs directory."""
        for file in os.listdir(f"{os.path.realpath(os.path.dirname(__file__))}/cogs"):
            if file.endswith(".py"):
                extension = file[:-3]
                try:
                    await self.load_extension(f"cogs.{extension}")
                    self.logger.info("Loaded extension '%s'", extension)
                except errors.ExtensionNotFound as e:
                    self.logger.error(
                        "Couldn't find extension '%s': %s", extension, e
                    )
                except errors.ExtensionAlreadyLoaded as e:
                    self.logger.error(
                        "Extension already loaded '%s': %s", extension, e
                    )
                except errors.NoEntryPointError as e:
                    self.logger.error(
                        "Extension has no setup() entry point '%s': %s", extension, e
                    )
                except errors.ExtensionFailed as e:
                    self.logger.error(
                        "Extension '%s' raised an error during load: %s", extension, e
                    )

    @tasks.loop(minutes=1.0)
    async def status_task(self) -> None:
        """Cycle the bot's presence status."""
        statuses = ["with you!"]
        await self.change_presence(activity=discord.Game(random.choice(statuses)))

    @status_task.before_loop
    async def before_status_task(self) -> None:
        """Wait until the bot is ready before starting the status loop."""
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        self.logger.info("Logged in as %s", self.user.name)
        self.logger.info("discord.py API version: %s", discord.__version__)
        self.logger.info("Python version: %s", platform.python_version())
        self.logger.info(
            "Running on: %s %s (%s)", platform.system(), platform.release(), os.name
        )
        self.logger.info("-------------------")
        self.db_pool = await open_db_pool()
        await init_schema(self.db_pool)
        self.logger.info("Connected to Postgres")
        await self.load_cogs()
        self.healthcheck_server = await discordhealthcheck.start(self)
        self.arq_pool = await open_pool()
        self.logger.info("Connected to Redis at %s", settings.REDIS_URL)
        self.status_task.start()

    async def close(self):
        if self.arq_pool is not None:
            await self.arq_pool.aclose()
        if self.db_pool is not None:
            await self.db_pool.close()
        await self.healthcheck_server.wait_closed()
        await super().close()

    # pylint: disable=arguments-differ
    async def on_message(self, message: discord.Message) -> None:
        """
        Process commands from non-bot users.

        Args:
            message (Message): The message that was sent.
        """
        if message.author == self.user or message.author.bot:
            return
        await self.process_commands(message)

    async def on_command_completion(self, context: Context) -> None:
        """
        Log successfully executed commands.

        Args:
            context (Context): The context of the command.
        """
        executed_command = context.command.qualified_name.split(" ")[0]
        if context.guild is not None:
            self.logger.info(
                "Executed '%s' in '%s' (ID: %s) by %s (ID: %s)",
                executed_command,
                context.guild.name,
                context.guild.id,
                context.author,
                context.author.id,
            )
        else:
            self.logger.info(
                "Executed '%s' by %s (ID: %s) in DMs",
                executed_command,
                context.author,
                context.author.id,
            )

    async def on_command_error(
        self,
        context: Context,
        error: errors.CommandError,
    ) -> None:
        """
        Global command error handler. Sends a user-facing embed for known error
        types and re-raises anything unexpected so it surfaces in logs.

        Args:
            context (Context): The context of the command.
            error (CommandError): The error that was raised.
        """
        if isinstance(error, commands.CommandOnCooldown):
            minutes, seconds = divmod(error.retry_after, 60)
            hours, minutes = divmod(minutes, 60)
            hours = hours % 24
            parts = []
            if round(hours) > 0:
                parts.append(f"{round(hours)} hours")
            if round(minutes) > 0:
                parts.append(f"{round(minutes)} minutes")
            if round(seconds) > 0:
                parts.append(f"{round(seconds)} seconds")
            embed = discord.Embed(
                description=f"**Please slow down** — you can use this command again in {', '.join(parts)}.",
                color=0xE02B2B,
            )
            await context.send(embed=embed)

        elif isinstance(error, commands.NotOwner):
            embed = discord.Embed(
                description="You are not the owner of the bot!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
            if context.guild:
                self.logger.warning(
                    "%s (ID: %s) tried to execute an owner-only command in '%s' (ID: %s).",
                    context.author,
                    context.author.id,
                    context.guild.name,
                    context.guild.id,
                )
            else:
                self.logger.warning(
                    "%s (ID: %s) tried to execute an owner-only command in DMs.",
                    context.author,
                    context.author.id,
                )

        elif isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                description=(
                    "You are missing the permission(s) `"
                    + ", ".join(error.missing_permissions)
                    + "` to execute this command!"
                ),
                color=0xE02B2B,
            )
            await context.send(embed=embed)

        elif isinstance(error, commands.BotMissingPermissions):
            self.logger.warning(
                "Bot is missing permissions %s to run '%s' in channel '%s'.",
                error.missing_permissions,
                context.command,
                context.channel,
            )
            embed = discord.Embed(
                description=(
                    "I am missing the permission(s) `"
                    + ", ".join(error.missing_permissions)
                    + "` to fully perform this command!"
                ),
                color=0xE02B2B,
            )
            await context.send(embed=embed)

        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="Missing argument",
                description=str(error).capitalize(),
                color=0xE02B2B,
            )
            await context.send(embed=embed)

        elif isinstance(error, commands.BadArgument):
            embed = discord.Embed(
                title="Invalid argument",
                description=str(error).capitalize(),
                color=0xE02B2B,
            )
            await context.send(embed=embed)

        elif isinstance(error, commands.MaxConcurrencyReached):
            embed = discord.Embed(
                description=(
                    "This command is already running in this channel. "
                    "Please wait for it to finish before running it again."
                ),
                color=0xE02B2B,
            )
            await context.send(embed=embed)

        elif isinstance(error, commands.CommandNotFound):
            # Silently ignore unknown commands — no need to log or respond.
            return

        else:
            # Genuinely unexpected — log the full traceback and let the user
            # know something went wrong without exposing internal details.
            self.logger.exception(
                "Unhandled error in command '%s' invoked by %s (ID: %s): %s",
                context.command,
                context.author,
                context.author.id,
                error,
            )
            embed = discord.Embed(
                title="Unexpected error",
                description=(
                    "An unexpected error occurred while running this command. "
                    "Please try again later, or contact an administrator if this keeps happening."
                ),
                color=0xE02B2B,
            )
            await context.send(embed=embed)


bot = DiscordBot()
bot.run(settings.TOKEN)
