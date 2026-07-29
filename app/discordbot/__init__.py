"""Discord integration.

Importing this package registers every ``@jobs.handler`` in :mod:`.tasks`, so the
job worker can dispatch Discord side effects.
"""

from app.discordbot import tasks  # noqa: F401  (import for handler registration)
from app.discordbot.bot import DiscordService, ReceiptBot, get_service

__all__ = ["DiscordService", "ReceiptBot", "get_service", "tasks"]
