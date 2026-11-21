import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord_bot as app


class WebhookLoggingTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, command, options, *, user_id=app.BOT_OWNER_ID):
        return SimpleNamespace(
            type=app.discord.InteractionType.application_command,
            command=command, data={"name": command.name, "options": options},
            guild=SimpleNamespace(name="School", id=10), guild_id=10,
            channel=SimpleNamespace(name="general", id=20),
            user=SimpleNamespace(display_name="Student", name="studybuddy", id=user_id),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=True)),
            followup=SimpleNamespace(send=AsyncMock()), command_failed=False, _state=None,
        )

    async def invoke_slash(self, interaction):
        with patch.object(app.bot, "dispatch") as dispatch:
            await app.bot.tree._call(interaction)
        if dispatch.called:
            dispatch.assert_called_once_with("app_command_completion", interaction, interaction.command)
            await app.bot.on_app_command_completion(interaction, interaction.command)

    async def test_homework_command_logs_spoiler_and_failure(self):
        message = SimpleNamespace(
            content=".hw class:maths time:2", guild=SimpleNamespace(name="School"),
            channel=SimpleNamespace(name="general", send=AsyncMock()),
            author=SimpleNamespace(bot=False, display_name="Student", id=123),
        )
        with patch.object(app, "log_event", new_callable=AsyncMock) as log, \
             patch.object(app, "fetch_homework_range", side_effect=RuntimeError("offline")):
            await app.bot.on_message(message)
        log.assert_awaited_once()
        description = log.await_args.args[1]
        self.assertIn("`.hw`", description)
        self.assertIn("||class:maths time:2||", description)
        self.assertTrue(description.endswith("❌ RuntimeError"))
        self.assertTrue(log.await_args.kwargs["error"])

    async def test_lookup_hides_typed_options_and_shows_username_on_success(self):
        interaction = self.interaction(app.timetable_lookup_command, [
            {"name": "student", "type": 3, "value": "Alex ||Sample||"},
            {"name": "week", "type": 4, "value": 2},
        ])
        with patch.object(app, "log_event", new_callable=AsyncMock) as log, \
             patch.object(app, "render_timetable_lookup", return_value=("Alex Sample", b"image", False)):
            await self.invoke_slash(interaction)
        log.assert_awaited_once()
        description = log.await_args.args[1]
        self.assertIn("`/timetable-lookup`", description)
        self.assertIn("Server: **School** (`10`)", description)
        self.assertIn("Channel: **general** (`20`)", description)
        self.assertIn("@studybuddy", description)
        self.assertIn(r"student: ||Alex \|\|Sample\|\|||", description)
        self.assertIn("week: ||2||", description)
        self.assertTrue(description.endswith("✅"))
        self.assertFalse(log.await_args.kwargs["error"])

    async def test_discord_choices_stay_visible_on_handled_error(self):
        interaction = self.interaction(app.check_command, [{"name": "length", "type": 3, "value": "full"}])
        with patch.object(app, "log_event", new_callable=AsyncMock) as log, \
             patch.object(app.bot, "remember_alert_channel"), patch.object(app.bot, "last_error", None), \
             patch.object(app.bot, "poll_once", side_effect=app.untis.WebUntisApiError(-8509, "private detail")):
            await self.invoke_slash(interaction)
        log.assert_awaited_once()
        description = log.await_args.args[1]
        self.assertIn("length: Full · 1 month back, 5 months ahead", description)
        self.assertNotIn("private detail", description)
        self.assertTrue(description.endswith("❌ WebUntis API error -8509"))
        self.assertTrue(interaction.command_failed)

    async def test_lookup_failures_log_once_without_a_success(self):
        failures = [
            (1, AssertionError("unexpected call"), "Access denied. Bot owner only."),
            (app.BOT_OWNER_ID, app.school_info.SchoolInfoUnavailable("No matching class."), "No matching class."),
            (app.BOT_OWNER_ID, RuntimeError("private detail"), "RuntimeError"),
        ]
        for user_id, error, expected in failures:
            with self.subTest(expected=expected):
                interaction = self.interaction(app.timetable_lookup_command, [
                    {"name": "class_name", "type": 3, "value": "2A"},
                ], user_id=user_id)
                with patch.object(app, "log_event", new_callable=AsyncMock) as log, \
                     patch.object(app, "render_timetable_lookup", side_effect=error):
                    await self.invoke_slash(interaction)
                log.assert_awaited_once()
                self.assertTrue(log.await_args.kwargs["error"])
                description = log.await_args.args[1]
                self.assertNotIn("✅", description)
                self.assertTrue(description.endswith(f"❌ {expected}"))

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
