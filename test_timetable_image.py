import unittest
from datetime import date, datetime
from io import BytesIO

from PIL import Image, ImageChops

from legacy_bot import SCHOOL_TZ, timetable_png


class TimetableCurrentTimeTests(unittest.TestCase):
    monday = date(2026, 11, 23)
    lessons = [
        {"date": 20261123, "startTime": 800, "endTime": 1500},
        {"date": 20261125, "startTime": 900, "endTime": 1000},
        {"date": 20261125, "startTime": 1100, "endTime": 1200},
    ]

    def test_current_time_in_a_gap_marks_today_in_both_themes(self):
        now = datetime(2026, 11, 25, 10, 30, tzinfo=SCHOOL_TZ)
        y = 423  # 10:30 on a grid that starts at 07:45.
        for mode in ("light", "dark"):
            with self.subTest(mode=mode):
                with Image.open(BytesIO(timetable_png(self.lessons, self.monday, mode=mode))) as plain, \
                     Image.open(BytesIO(timetable_png(self.lessons, self.monday, mode=mode, now=now))) as marked:
                    ink = marked.getpixel((100, y))
                    self.assertNotEqual(plain.getpixel((100, y)), ink)
                    self.assertEqual(marked.getpixel((1505, y)), ink)
                    self.assertEqual(marked.getpixel((700, y + 1)), ink)  # Wednesday
                    self.assertNotEqual(marked.getpixel((200, y + 1)), ink)  # Monday
                    difference = ImageChops.difference(marked, plain)
                    self.assertIsNotNone(difference.crop((0, y - 8, 92, y + 8)).getbbox())

    def test_current_time_requires_a_scheduled_span_today(self):
        plain = timetable_png(self.lessons, self.monday)
        for day, hour, minute in [
            (25, 8, 59), (25, 12, 0), (26, 10, 30), (28, 10, 30), (30, 10, 30),
        ]:
            with self.subTest(day=day, hour=hour, minute=minute):
                now = datetime(2026, 11, day, hour, minute, tzinfo=SCHOOL_TZ)
                self.assertEqual(timetable_png(self.lessons, self.monday, now=now), plain)

    def test_timed_exam_counts_on_a_day_without_lessons(self):
        exams = [{"examDate": 20261127, "startTime": 1000, "endTime": 1100}]
        now = datetime(2026, 11, 27, 10, 30, tzinfo=SCHOOL_TZ)
        plain = timetable_png(self.lessons, self.monday, exam_items=exams)
        self.assertNotEqual(timetable_png(self.lessons, self.monday, exam_items=exams, now=now), plain)
