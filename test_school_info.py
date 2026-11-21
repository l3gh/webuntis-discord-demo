import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord_bot as bot
import school_info


class TimetableLookupTests(unittest.TestCase):
    monday = date(2026, 11, 23)

    def test_student_read_uses_full_name_and_does_not_query_class(self):
        with patch.object(school_info.untis, "rpc", return_value=[{
            "id": 1, "name": "Sample", "foreName": "Alex",
        }]) as rpc, patch.object(school_info, "fetch_timetable", return_value=[{}]) as fetch:
            result = school_info.timetable_lookup(None, "endpoint", "sample  ALEX", "2A", self.monday)

        self.assertEqual(result, ("Alex Sample", [{}], False))
        rpc.assert_called_once_with(None, "endpoint", "getStudents", {})
        fetch.assert_called_once_with(None, "endpoint", 1, 5, self.monday, date(2026, 11, 27))

    def test_class_is_used_when_student_access_is_denied(self):
        denied = school_info.untis.WebUntisApiError(-8509, "Access denied")
        with patch.object(school_info.untis, "rpc", side_effect=[denied, [{"id": 2, "name": "2A"}]]), \
                patch.object(school_info, "fetch_timetable", return_value=[{}]) as fetch:
            result = school_info.timetable_lookup(None, "endpoint", "Alex Sample", "2a", self.monday)

        self.assertEqual(result, ("2A", [{}], True))
        fetch.assert_called_once_with(None, "endpoint", 2, 1, self.monday, date(2026, 11, 27))

    def test_non_permission_error_is_not_reported_as_missing_student(self):
        with patch.object(school_info.untis, "rpc", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                school_info.timetable_lookup(None, "endpoint", "Alex Sample", "2A", self.monday)


class SchoolCommandTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, user_id=bot.BOT_OWNER_ID):
        return SimpleNamespace(user=SimpleNamespace(id=user_id),
                               response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()))

    def assert_private_orange(self, call):
        self.assertTrue(call.kwargs["ephemeral"])
        self.assertEqual(call.kwargs["embed"].colour.value, bot.FAVICON_COLOR)

    async def test_announcements_are_private_on_request_and_preserve_links(self):
        interaction = self.interaction(user_id=1)
        announcement = {"subject": "Assembly", "text": '<p>Meet at 9</p><a href="https://example.org/">Details</a>'}
        with patch.object(bot, "fetch_announcements", return_value=[announcement]):
            await bot.announcements_command.callback(interaction)

        reply = interaction.followup.send.await_args
        self.assert_private_orange(reply)
        self.assertEqual(reply.kwargs["embed"].description, "Meet at 9\nDetails (https://example.org/)")

    async def test_absences_and_lookup_are_owner_only(self):
        for command, fetch in ((bot.absences_command, "fetch_absences"),
                               (bot.timetable_lookup_command, "render_timetable_lookup")):
            with self.subTest(command=command.name), patch.object(bot, fetch) as fetch_mock:
                interaction = self.interaction(user_id=1)
                await command.callback(interaction)
                fetch_mock.assert_not_called()
                self.assert_private_orange(interaction.response.send_message.await_args)

    async def test_owner_sees_absence_dates_and_excuse_status(self):
        interaction = self.interaction()
        with patch.object(bot.untis, "school_today", return_value=date(2026, 11, 23)), \
                patch.object(bot, "fetch_absences", return_value=[{
                    "startDate": 20261121, "startTime": 900, "reason": "Illness", "isExcused": True,
                }]) as fetch:
            await bot.absences_command.callback(interaction, days=3)

        reply = interaction.followup.send.await_args
        self.assert_private_orange(reply)
        self.assertIn("21.11.2026 09:00", reply.kwargs["embed"].description)
        self.assertIn("Illness · Excused", reply.kwargs["embed"].description)
        fetch.assert_called_once_with(date(2026, 11, 21), date(2026, 11, 23))

    async def test_timetable_fallback_image_is_inside_private_orange_embed(self):
        interaction = self.interaction()
        with patch.object(bot, "render_timetable_lookup", return_value=("2A", b"image contents", True)):
            await bot.timetable_lookup_command.callback(interaction, student="Alex Sample", class_name="2A")

        reply = interaction.followup.send.await_args
        self.assert_private_orange(reply)
        self.assertIn("class timetable", reply.kwargs["embed"].description)
        self.assertEqual(reply.kwargs["embed"].image.url, f"attachment://{reply.kwargs['file'].filename}")

    async def test_announcement_poll_retries_only_unsent_pages(self):
        channel = SimpleNamespace(send=AsyncMock(side_effect=[None, RuntimeError("offline"), None]))
        client = SimpleNamespace(get_channel=Mock(return_value=channel), fetch_channel=AsyncMock())
        announcement = {"id": 1, "subject": "Assembly", "text": "a" * 3501}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "announcement_state.json"
            with patch.object(bot, "ANNOUNCEMENT_STATE", path), \
                    patch.object(bot, "fetch_announcements", return_value=[announcement]):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    await bot.UntisDiscordBot.post_announcements(client, 42)
                await bot.UntisDiscordBot.post_announcements(client, 42)
                await bot.UntisDiscordBot.post_announcements(client, 42)
            self.assertEqual(len(json.loads(path.read_text())), 2)

        self.assertEqual(channel.send.await_count, 3)
        self.assertEqual(channel.send.await_args.kwargs["embed"].description, "a")
        self.assertEqual(channel.send.await_args.kwargs["embed"].colour.value, bot.FAVICON_COLOR)
        self.assertFalse(channel.send.await_args.kwargs["allowed_mentions"].everyone)


if __name__ == "__main__":
    unittest.main()
