from datetime import datetime, timedelta
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.event import Event
from app.services import calendar_service
from app.utils.timeutil import get_tz


class CalendarRegressionTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()

    def event(self, **changes):
        values = dict(user_id=self.user.id, title="Recurring review event",
                      start_naive=datetime(2026, 1, 1, 1),
                      end_naive=datetime(2026, 1, 1, 2),
                      rrule="FREQ=DAILY", reminder_minutes=0)
        values.update(changes)
        return calendar_service.create_event(**values)

    def test_recurring_event_appears_in_later_month_day_and_dashboard(self):
        ev = self.event()
        other = self.make_user("other", False)
        self.event(user_id=other.id, title="Other account event")
        self.event(title="Deleted event", rrule="FREQ=DAILY")
        deleted = Event.query.filter_by(title="Deleted event").one()
        calendar_service.soft_delete_event(deleted)
        client = self.client_for(self.user)
        response = client.get("/calendar/api/events?year=2026&month=9")
        self.assertEqual(response.status_code, 200)
        events = response.get_json()["data"]["events"]
        self.assertTrue(any(item["id"] == ev.id and item["date"] == "2026-09-30"
                            for item in events))
        self.assertTrue(all(item["id"] == ev.id for item in events))
        from app.blueprints.calendar import _day_occurrences
        from flask_login import login_user
        with self.app.test_request_context("/"):
            login_user(self.user)
            rows = _day_occurrences("2026-09-30", get_tz("Asia/Shanghai"))
            self.assertEqual([row["id"] for row in rows], [ev.id])
        with patch("app.blueprints.dashboard.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 30, 12, tzinfo=get_tz("Asia/Shanghai"))
            response = client.get("/")
        self.assertIn(ev.title, response.get_data(as_text=True))
        self.assertNotIn("Other account event", response.get_data(as_text=True))

    def test_nonrecurring_past_and_future_events_stay_outside_window(self):
        self.event(rrule="")
        self.event(start_naive=datetime(2027, 1, 1), end_naive=None)
        rows = calendar_service.list_events(datetime(2026, 9, 1), datetime(2026, 10, 1), self.user.id)
        self.assertEqual(rows, [])

    def test_reminder_is_not_early_and_is_sent_once_after_session_restart(self):
        self.event(rrule="", start_naive=datetime(2026, 9, 30, 2),
                   end_naive=None, reminder_minutes=60)
        with patch("app.services.notify_service.notify_for") as notify:
            for now in (datetime(2026, 9, 30, 0), datetime(2026, 9, 30, 0, 59)):
                with patch.object(calendar_service, "utcnow", return_value=now):
                    calendar_service.scan_event_reminders(self.user)
            notify.assert_not_called()
            with patch.object(calendar_service, "utcnow", return_value=datetime(2026, 9, 30, 1)):
                calendar_service.scan_event_reminders(self.user)
            notify.assert_called_once()
            user_id = self.user.id
            db.session.remove()
            from app.models.user import User
            self.user = db.session.get(User, user_id)
            with patch.object(calendar_service, "utcnow", return_value=datetime(2026, 9, 30, 1, 1)):
                calendar_service.scan_event_reminders(self.user)
            notify.assert_called_once()

    def test_recurring_reminders_handle_long_lead_time_and_next_occurrence(self):
        self.event(reminder_minutes=2880)
        start = datetime(2026, 9, 30, 1)
        with patch("app.services.notify_service.notify_for") as notify:
            for now in (start, start + timedelta(minutes=1), start + timedelta(days=1)):
                with patch.object(calendar_service, "utcnow", return_value=now):
                    calendar_service.scan_event_reminders(self.user)
            self.assertEqual(notify.call_count, 2)
        self.assertEqual(Event.query.one().last_reminded_occurrence_utc,
                         datetime(2026, 10, 3, 1))

    def test_delayed_scan_catches_due_event_but_not_another_users_event(self):
        ev = self.event(rrule="", start_naive=datetime(2026, 9, 30, 1), end_naive=None)
        other = self.make_user("other", False)
        self.event(user_id=other.id, rrule="", start_naive=ev.start_utc, end_naive=None)
        with patch("app.services.notify_service.notify_for") as notify, \
                patch.object(calendar_service, "utcnow", return_value=ev.start_utc + timedelta(minutes=5)):
            calendar_service.scan_event_reminders(self.user)
            notify.assert_called_once()
            self.assertEqual(notify.call_args.kwargs["user_id"], self.user.id)

    def test_timing_edit_rearms_reminder_but_title_edit_does_not(self):
        ev = self.event()
        ev.last_reminded_occurrence_utc = datetime(2026, 9, 30, 1)
        db.session.commit()
        calendar_service.update_event(ev, title="Renamed")
        self.assertIsNotNone(ev.last_reminded_occurrence_utc)
        calendar_service.update_event(ev, reminder_minutes=30)
        self.assertIsNone(ev.last_reminded_occurrence_utc)

    def test_stale_scan_cannot_claim_the_same_occurrence_twice(self):
        ev = self.event()
        occurrence = datetime(2026, 9, 30, 1)
        with patch.object(calendar_service, "due_reminders", return_value=[(ev, occurrence)]), \
                patch("app.services.notify_service.notify_for") as notify:
            calendar_service.scan_event_reminders(self.user)
            calendar_service.scan_event_reminders(self.user)
            notify.assert_called_once()

    def test_existing_events_table_gets_reminder_column_without_data_loss(self):
        from sqlalchemy import inspect, text
        from app.services.install_service import ensure_schema
        ev = self.event()
        event_id = ev.id
        db.session.remove()
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE events DROP COLUMN last_reminded_occurrence_utc"))
        messages = []
        ensure_schema(messages)
        ensure_schema(messages)
        self.assertIn("last_reminded_occurrence_utc", {c["name"] for c in inspect(db.engine).get_columns("events")})
        self.assertEqual(db.session.get(Event, event_id).title, "Recurring review event")
        self.assertEqual(sum("提醒去重" in message for message in messages), 1)
