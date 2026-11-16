import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord_bot as app
import legacy_bot as untis
from timetable_changes import diff, format_change


NAMES = {"su": {"1": "Music", "2": "History"}, "te": {"1": "Smith", "2": "Jones"},
         "ro": {"1": "M2", "2": "112", "3": "M1"}}


def lesson(**updates):
    return {"id": 100, "date": 20261117, "startTime": 855, "endTime": 945,
            "su": [{"id": 1}], "te": [{"id": 1}], "ro": [{"id": 1}], **updates}


class TimetableChangeTests(unittest.TestCase):
    def describe(self, before, after):
        changes = diff([before], [after])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, "Changed")
        self.assertEqual(changes[0].before, before)
        self.assertEqual(changes[0].after, after)
        return format_change(changes[0], NAMES)

    def test_cancellation_and_reinstatement_have_explicit_status(self):
        for code in ("cancelled", "Canceled", " CANCEL ", "entfall", "ausfall"):
            with self.subTest(code=code):
                cancelled = lesson(code=code)
                title, body = self.describe(lesson(), cancelled)
                self.assertEqual(title, "Lesson cancelled")
                self.assertIn("Status: Scheduled → Cancelled", body)
                self.assertIn("Music · 17.11.2026 · 08:55–09:45", body)
                title, body = self.describe(cancelled, lesson())
                self.assertEqual(title, "Lesson reinstated")
                self.assertIn("Status: Cancelled → Scheduled", body)

    def test_status_fallback_is_shared_with_timetable_image(self):
        title, body = self.describe(lesson(), lesson(status="cancelled"))
        self.assertEqual(title, "Lesson cancelled")
        self.assertTrue(untis._is_cancelled(lesson(status="cancelled")))
        title, body = self.describe(lesson(), lesson(code="irregular"))
        self.assertEqual(title, "Status change")
        self.assertIn("Status: Scheduled → Irregular", body)

    def test_teacher_substitution_and_missing_teacher(self):
        title, body = self.describe(lesson(), lesson(te=[{"id": 2, "orgid": 1}]))
        self.assertEqual(title, "Teacher substitution")
        self.assertIn("Teacher: Smith → Jones [originally Smith]", body)
        self.assertIn("Room: M2", body)
        title, body = self.describe(lesson(), lesson(te=[]))
        self.assertEqual(title, "Teacher removed")
        self.assertIn("Teacher: Smith → Not assigned", body)

    def test_all_tags_and_before_after_values_are_kept(self):
        title, body = self.describe(lesson(), lesson(
            code="cancelled", su=[{"id": 2}], te=[{"id": 2}], ro=[{"id": 2}],
            date=20261118, startTime=1000, endTime=1050,
            lstext="Lesson note", substText="Substitution note", info="Information"))
        self.assertEqual(title, "Lesson cancelled · Teacher substitution · Room change · Subject change · Lesson moved · Note change")
        for detail in ("Status: Scheduled → Cancelled", "Subject: Music → History",
                       "Teacher: Smith → Jones", "Room: M2 → 112",
                       "When: 17.11.2026 · 08:55–09:45 → 18.11.2026 · 10:00–10:50",
                       "Lesson note: None → Lesson note", "Substitution note: None → Substitution note",
                       "Information: None → Information"):
            self.assertIn(detail, body)

    def test_moved_unique_id_is_one_change_even_with_an_added_lesson_in_old_slot(self):
        before = lesson()
        after = lesson(date=20261118, startTime=1000, endTime=1050)
        added = lesson(id=101)
        changes = diff([before], [added, after])
        self.assertEqual([change.kind for change in changes], ["Added", "Changed"])
        self.assertEqual(format_change(changes[1], NAMES)[0], "Lesson moved")

    def test_duration_change(self):
        title, body = self.describe(lesson(), lesson(endTime=955))
        self.assertEqual(title, "Lesson duration changed")
        self.assertIn("08:55–09:45 → 17.11.2026 · 08:55–09:55", body)

    def test_added_and_removed_are_distinct_from_cancellation(self):
        for before, after, expected in (([], [lesson()], "Lesson added"),
                                        ([lesson()], [], "Removed from timetable")):
            title, body = format_change(diff(before, after)[0], NAMES)
            self.assertEqual(title, expected)
            self.assertNotIn("cancelled", title.lower())
            self.assertIn("Teacher: Smith", body)
            self.assertIn("Room: M2", body)
        title, body = format_change(diff([], [lesson(code="cancelled", substText="Absent")])[0], NAMES)
        self.assertEqual(title, "Lesson cancelled · Lesson added")
        self.assertIn("Substitution note: Absent", body)

    def test_recurring_lesson_ids_do_not_collapse_multiple_occurrences(self):
        before = [lesson(id=None, lsid=7), lesson(id=None, lsid=7, startTime=1000, endTime=1050),
                  lesson(id=None, lsid=7, date=20261118)]
        after = copy.deepcopy(before)
        after[1]["te"] = [{"id": 2}]
        after[2]["ro"] = [{"id": 2}]
        changes = diff(before, list(reversed(after)))
        self.assertEqual(len(changes), 2)
        self.assertEqual([format_change(change, NAMES)[0] for change in changes],
                         ["Teacher substitution", "Room change"])

    def test_ambiguous_moves_are_not_guessed(self):
        before = [lesson(id=None, lsid=7), lesson(id=None, lsid=7, date=20261118)]
        after = [lesson(id=None, lsid=7, date=20261119), lesson(id=None, lsid=7, date=20261120)]
        self.assertEqual([change.kind for change in diff(before, after)], ["Removed", "Removed", "Added", "Added"])

    def test_duplicate_keys_preserve_each_occurrence(self):
        before = [lesson(), lesson(te=[{"id": 2}])]
        self.assertEqual(diff(before, list(reversed(before))), [])
        after = [lesson(), lesson(te=[{"id": 2}], ro=[{"id": 2}])]
        self.assertEqual(len(diff(before, after)), 1)
        self.assertEqual(format_change(diff(before, after)[0], NAMES)[0], "Room change")
        self.assertEqual(len(diff([lesson(), lesson()], [])), 2)

    def test_entity_order_and_label_hydration_do_not_create_changes(self):
        before = lesson(te=[{"id": 1}, {"id": 2}], code="normal", info="")
        after = lesson(te=[{"id": "2", "name": "Jones"}, {"id": "1", "name": "Smith"}])
        self.assertEqual(diff([before], [after]), [])
        self.assertEqual(diff([lesson(code="canceled")], [lesson(status="cancelled")]), [])

    def test_missing_ids_match_unambiguous_slots_without_using_list_positions(self):
        before = [lesson(id=None), lesson(id=None, startTime=1000, endTime=1050)]
        after = [lesson(id=None, startTime=1000, endTime=1050, ro=[{"id": 2}]), lesson(id=None)]
        changes = diff(before, after)
        self.assertEqual(len(changes), 1)
        self.assertEqual(format_change(changes[0], NAMES)[0], "Room change")

    def test_unmapped_teacher_ids_remain_distinguishable(self):
        change = diff([lesson(te=[{"id": 80}])], [lesson(te=[{"id": 81}])])[0]
        self.assertIn("Teacher: Teacher #80 → Teacher #81", format_change(change)[1])

    def test_all_notes_and_extra_changed_fields_are_visible(self):
        title, body = self.describe(lesson(info="Keep this", substText="Before", lstext="Keep this too"),
                                    lesson(info="Keep this", substText="After", lstext="Keep this too", activityType="exam"))
        self.assertEqual(title, "Note change · Activity type change")
        self.assertIn("Substitution note: Before → After", body)
        self.assertIn("Information: Keep this", body)
        self.assertIn("Lesson note: Keep this too", body)
        self.assertIn("Activity type: None → exam", body)

    def test_notes_removed_and_assignment_added(self):
        title, body = self.describe(lesson(info="Old note", ro=[]), lesson(ro=[{"id": 2}]))
        self.assertEqual(title, "Room assigned · Note change")
        self.assertIn("Room: Not assigned → 112", body)
        self.assertIn("Information: Old note → None", body)

    def test_no_network_demo_uses_detailed_alerts(self):
        output = io.StringIO()
        with redirect_stdout(output):
            untis.demo()
        self.assertIn("Room change", output.getvalue())
        self.assertIn("Room: 12 → 14", output.getvalue())
        self.assertIn("Lesson added", output.getvalue())


class TimetablePollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.temp.name)
        for owner, key, filename in ((untis, "STATE", "timetable.json"), (untis, "STATUS", "status.json"),
                                     (app, "PLAN_STATE", "plan.json")):
            self.stack.enter_context(patch.object(owner, key, root / filename))
        for key in ("snapshot", "last_checked", "last_error"):
            self.stack.enter_context(patch.object(app.bot, key, None))
        self.log = self.stack.enter_context(patch.object(app, "log_event", AsyncMock()))
        self.channel = SimpleNamespace(send=AsyncMock())
        self.stack.enter_context(patch.object(app.bot, "get_alert_channel", AsyncMock(return_value=self.channel)))
        self.today = date(2026, 11, 16)
        self.fresh = {"today": self.today, "end": self.today + timedelta(days=35),
                      "plan_end": self.today + timedelta(days=21), "exam_end": app.add_months(self.today, 5),
                      "timetable": [], "homework": [], "exams": [], "entity_names": NAMES}
        self.fetch = self.stack.enter_context(patch.object(app, "fetch_snapshot", side_effect=lambda full: self.fresh))

    def baseline(self, items, **status):
        untis.STATE.write_text(json.dumps(items))
        untis.STATUS.write_text(json.dumps({"timetable_days": 35, "plan_days": 21, **status}))
        app.PLAN_STATE.write_text(json.dumps({"homework": [], "exams": [], "exam_identity_version": 2}))

    async def test_posts_one_complete_alert_for_combined_change_and_saves_baseline(self):
        self.baseline([lesson()])
        self.fresh["timetable"] = [lesson(te=[{"id": 2}], ro=[{"id": 2}], su=[{"id": 2}])]
        result = await app.bot.poll_once()
        self.assertEqual(result["changes"], 1)
        self.channel.send.assert_awaited_once()
        message = self.channel.send.await_args.args[0]
        self.assertIn("**Teacher substitution · Room change · Subject change**", message)
        for detail in ("Teacher: Smith → Jones", "Room: M2 → 112", "Subject: Music → History"):
            self.assertIn(detail, message)
        self.assertEqual(json.loads(untis.STATE.read_text()), self.fresh["timetable"])
        second = await app.bot.poll_once()
        self.assertEqual(second["changes"], 0)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_failed_delivery_keeps_old_baseline_for_retry(self):
        self.baseline([lesson()])
        self.fresh["timetable"] = [lesson(code="cancelled")]
        self.channel.send.side_effect = RuntimeError("Discord unavailable")
        with self.assertRaisesRegex(RuntimeError, "Discord unavailable"):
            await app.bot.poll_once()
        self.assertEqual(json.loads(untis.STATE.read_text()), [lesson()])
        self.channel.send.side_effect = None
        result = await app.bot.poll_once()
        self.assertEqual(result["changes"], 1)
        self.assertIn("**Lesson cancelled**", self.channel.send.await_args.args[0])

    async def test_initial_snapshot_does_not_send_alerts(self):
        self.fresh["timetable"] = [lesson(code="cancelled")]
        result = await app.bot.poll_once()
        self.assertTrue(result["initial"])
        self.channel.send.assert_not_awaited()
        self.assertEqual(json.loads(untis.STATE.read_text()), self.fresh["timetable"])

    async def test_widened_window_suppresses_newly_visible_lessons(self):
        self.baseline([lesson()])
        self.fresh["timetable"] = [lesson(), lesson(id=101, date=20270101)]
        result = await app.bot.poll_once(length="full")
        self.assertEqual(result["changes"], 0)
        self.channel.send.assert_not_awaited()

    async def test_long_alert_keeps_all_details_and_only_owner_can_be_pinged(self):
        self.baseline([lesson()])
        long_note = "Beginning " + "x" * 5000 + " ending @everyone **untrusted**"
        self.fresh["timetable"] = [lesson(info=long_note)]
        result = await app.bot.poll_once()
        self.assertEqual(result["changes"], 1)
        calls = self.channel.send.await_args_list
        self.assertGreater(len(calls), 1)
        message = "\n".join(call.args[0] for call in calls)
        self.assertEqual(message.count("x"), 5000)
        self.assertIn("Beginning", message)
        self.assertIn("ending", message)
        self.assertNotIn("@everyone", message)
        self.assertIn(r"\*\*untrusted\*\*", message)
        for call in calls:
            self.assertLessEqual(len(call.args[0]), 1850)
            allowed = call.kwargs["allowed_mentions"]
            self.assertEqual([user.id for user in allowed.users], [app.BOT_OWNER_ID])
            self.assertFalse(allowed.everyone)
            self.assertFalse(allowed.roles)


if __name__ == "__main__":
    unittest.main()
