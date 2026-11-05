import unittest
from datetime import date, datetime
from unittest.mock import patch

import discord_bot as bot


class HomeworkCommandTests(unittest.TestCase):
    def test_defaults_and_options(self):
        self.assertEqual(bot.parse_homework_options([]), ("all", 4, True, True))
        self.assertEqual(bot.parse_homework_options(["class:maths", "time:2", "past:false"]), ("maths", 2, False, True))
        self.assertEqual(bot.parse_homework_options(["future:false", "range:3"]), ("all", 3, True, False))

    def test_invalid_options(self):
        for tokens in (["time:0"], ["time:53"], ["time:no"], ["past:no"], ["past:false", "future:false"], ["class:"], ["time:1", "range:1"], ["unknown:1"]):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                bot.parse_homework_options(tokens)

    def test_date_windows_include_today(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 5, tzinfo=tz)

        items = [{"dueDate": d} for d in ("20261021", "20261022", "20261105", "20261119", "20261120")]
        with patch.object(bot, "datetime", FixedDatetime), patch.object(bot, "webuntis_connection", return_value=("school", "endpoint", object(), {})), patch.object(bot.untis, "fetch_plan_markers", return_value=(items, [])), patch.object(bot.untis, "load_entity_names", return_value={}):
            for past, future, expected in ((True, True, items[1:4]), (False, True, items[2:4]), (True, False, items[1:3])):
                with self.subTest(past=past, future=future):
                    today, start, end, result, _ = bot.fetch_homework_range(2, past, future)
                    self.assertEqual(today, date(2026, 11, 5))
                    self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
