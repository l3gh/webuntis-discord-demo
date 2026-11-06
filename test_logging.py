import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord_bot as app


class WebhookLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_homework_command_logs_options(self):
        message = SimpleNamespace(
            content=".hw class:maths time:2", guild=SimpleNamespace(name="School"),
            channel=SimpleNamespace(name="general", send=AsyncMock()),
            author=SimpleNamespace(bot=False, display_name="Student", id=123),
        )
        with patch.object(app, "log_event", new_callable=AsyncMock) as log, \
             patch.object(app, "fetch_homework_range", side_effect=RuntimeError("offline")):
            await app.bot.on_message(message)
            await asyncio.sleep(0)
        self.assertIn(".hw class:maths time:2", log.await_args_list[0].args[1])

    async def test_sends_embed_without_mentions(self):
        webhook = Mock(send=AsyncMock())
        with patch.object(app.untis, "load_config", return_value={"WEBUNTIS_LOG_WEBHOOK_URL": "https://example.invalid"}), \
             patch.dict(app.os.environ, {"WEBUNTIS_LOG_WEBHOOK_URL": ""}), \
             patch.object(app.discord.Webhook, "from_url", return_value=webhook):
            await app.log_event("Check", "One change")
        args = webhook.send.await_args.kwargs
        self.assertEqual(args["embed"].description, "One change")
        self.assertEqual(args["allowed_mentions"].to_dict()["parse"], [])

    async def test_failure_is_nonfatal(self):
        with patch.object(app.untis, "load_config", side_effect=OSError("unavailable")):
            await app.log_event("Check", "One change")

    async def test_no_webhook_does_not_send(self):
        with patch.object(app.untis, "load_config", return_value={}), \
             patch.dict(app.os.environ, {"WEBUNTIS_LOG_WEBHOOK_URL": ""}), \
             patch.object(app.discord.Webhook, "from_url") as factory:
            await app.log_event("Check", "One change")
        factory.assert_not_called()
