import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord_bot as bot


class ExamEmbedTests(unittest.TestCase):
    def test_exam_details_and_empty_results(self):
        names = {"_subject_label_ids": {"biu": 1}, "su": {"1": "Biology"}}
        items = [{"examDate": 20261125, "startTime": 1200, "endTime": 1250,
                  "subject": "BiU", "name": "BiU-Test"}]
        embed = bot.exam_embeds(items, names, date(2026, 11, 2), date(2027, 4, 5))[0]
        self.assertEqual(embed.title, "Upcoming exams")
        self.assertEqual(embed.colour.value, bot.FAVICON_COLOR)
        self.assertIn("**Biology** · **25.11.2026**\n-# 12:00–12:50", embed.description)
        self.assertEqual(embed.footer.text, "02.11.2026 – 05.04.2027 · 1 exam")
        empty = bot.exam_embeds([], names, date(2026, 11, 2), date(2027, 4, 5))[0]
        self.assertIn("No exams", empty.description)
        self.assertIn("0 exams", empty.footer.text)

    def test_subject_is_preferred_and_names_are_only_a_fallback(self):
        names = {"_subject_label_ids": {"f5": 1}, "su": {"1": "French"}}
        items = [{"subject": "F5", "name": "1. F5"},
                 {"subject": "F5", "name": "2. F5"},
                 {"name": "Geography test"}]
        embed = bot.exam_embeds(items, names, date(2026, 11, 2), date(2027, 4, 5))[0]
        self.assertEqual(embed.description.count("**French**"), 2)
        self.assertNotIn("1. ", embed.description)
        self.assertNotIn("2. ", embed.description)
        self.assertIn("**Geography test**", embed.description)
        self.assertEqual(bot.exam_display_title({"subject": "2. French"}, {}), "2. French")

    def test_pagination_preserves_entries(self):
        items = [{"name": f"Exam {i} " + "x" * 500, "examDate": 20261112} for i in range(12)]
        embeds = bot.exam_embeds(items, {}, date(2026, 11, 2), date(2027, 4, 5))
        self.assertGreater(len(embeds), 1)
        self.assertTrue(all(len(embed.description) <= 3500 for embed in embeds))
        text = "\n".join(embed.description for embed in embeds)
        for i in range(12):
            self.assertIn(f"Exam {i} ", text)
        self.assertIn(f"1/{len(embeds)}", embeds[0].footer.text)


class ExamCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_sorts_and_sends_after_defer(self):
        interaction = SimpleNamespace(response=SimpleNamespace(is_done=Mock(return_value=True), send_message=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        snapshot = {"today": date(2026, 11, 5), "exam_end": date(2027, 4, 5), "entity_names": {},
                    "exams": [{"date": 20261112, "name": "Later"}, {"date": 20261102, "name": "Earlier"},
                              {"date": 20261101, "name": "Outside"}, {"date": 20270406, "name": "Too late"}]}
        with patch.object(bot, "get_snapshot", AsyncMock(return_value=snapshot)):
            await bot.exams_command.callback(interaction)
        interaction.response.send_message.assert_not_awaited()
        embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertLess(embed.description.index("Earlier"), embed.description.index("Later"))
        self.assertNotIn("Outside", embed.description)
        self.assertNotIn("Too late", embed.description)
        self.assertIn("2 exams", embed.footer.text)


if __name__ == "__main__":
    unittest.main()
