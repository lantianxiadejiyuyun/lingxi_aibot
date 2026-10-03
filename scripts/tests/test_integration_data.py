"""Calendar/task integration contracts with real isolated ORM and Token requests."""
from datetime import datetime
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.event import Event
from app.models.task import Task
from app.utils.api_auth import assign_user_api_token


class IntegrationDataTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        from app.blueprints.integration_data import bp
        if bp.name not in self.app.blueprints:
            self.app.register_blueprint(bp)
        self.user = self.make_user()
        self.other = self.make_user("other_data_user", False)
        self.token = assign_user_api_token(self.user)
        self.other_token = assign_user_api_token(self.other)
        self.client = self.app.test_client(use_cookies=False)

    def request(self, method, path, body=None, *, other=False):
        headers = {"Authorization": "Bearer " + (self.other_token if other else self.token)}
        return self.client.open("/api/v1" + path, method=method, json=body, headers=headers)

    def task(self, **changes):
        body = {"title": "Deadline", "due_at": "2026-10-03T10:00:00+08:00"}
        body.update(changes)
        response = self.request("POST", "/tasks", body)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["data"]

    def event(self, **changes):
        body = {"title": "Meeting", "start_at": "2026-10-03T10:00:00+08:00",
                "end_at": "2026-10-03T11:00:00+08:00"}
        body.update(changes)
        response = self.request("POST", "/events", body)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["data"]

    def test_token_auth_works_without_cookies_and_does_not_create_session(self):
        response = self.request("POST", "/tasks", {"title": "Token only"})
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(Task.query.one().user_id, self.user.id)
        self.assertEqual(self.client.get("/api/v1/tasks").status_code, 401)
        logged_in = self.client_for(self.user)
        self.assertEqual(logged_in.get("/api/v1/tasks").status_code, 401)
        response = self.client.get("/api/v1/tasks", headers={"X-API-Token": self.token})
        self.assertEqual(response.status_code, 200)

    def test_foreign_and_deleted_tasks_events_never_visible_or_mutable(self):
        for resource, item in (("tasks", self.task()), ("events", self.event())):
            path = f"/{resource}/{item['id']}"
            for method, body in (("GET", None), ("PATCH", {"title": "Intrusion"}), ("DELETE", None)):
                with self.subTest(resource=resource, method=method):
                    self.assertEqual(self.request(method, path, body, other=True).status_code, 404)
            self.assertEqual(self.request("GET", f"/{resource}", other=True).get_json()["data"], [])
            self.assertEqual(self.request("GET", path).get_json()["data"]["title"], item["title"])
            self.assertEqual(self.request("DELETE", path).status_code, 200)
            self.assertEqual(self.request("GET", path).status_code, 404)
            self.assertEqual(self.request("PATCH", path, {"title": "Restore"}).status_code, 404)
            self.assertEqual(self.request("GET", f"/{resource}").get_json()["pagination"]["total"], 0)
        self.assertIsNotNone(Task.query.one().deleted_at)
        self.assertIsNotNone(Event.query.one().deleted_at)

    def test_task_status_tags_deadline_roundtrip_and_null(self):
        task = self.task(status="done", priority=3, tags=["work", "work", " urgent "], notes="note", project="demo")
        self.assertEqual(task["due_at"], "2026-10-03T02:00:00Z")
        self.assertEqual(task["tags"], ["work", "urgent"])
        self.assertIsNotNone(task["completed_at"])
        response = self.request("PATCH", f"/tasks/{task['id']}", {"status": "open", "due_at": None})
        data = response.get_json()["data"]
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(data["completed_at"])
        self.assertIsNone(data["due_at"])
        self.assertEqual(data["priority"], 3)

    def test_task_date_range_inclusive_start_exclusive_end_and_pagination(self):
        first = self.task(due_at="2026-10-03T00:00:00Z")
        second = self.task(due_at="2026-10-03T01:00:00Z")
        self.task(due_at="2026-10-04T00:00:00Z")
        self.task(due_at=None)
        query = "/tasks?due_after=2026-10-03T00:00:00Z&due_before=2026-10-04T00:00:00Z"
        data = self.request("GET", query).get_json()
        self.assertEqual([t["id"] for t in data["data"]], [first["id"], second["id"]])
        data = self.request("GET", query + "&limit=1&offset=1").get_json()
        self.assertEqual(data["pagination"], {"limit": 1, "offset": 1, "total": 2})
        self.assertEqual(data["data"][0]["id"], second["id"])

    def test_invalid_task_patch_never_partially_writes(self):
        task = self.task()
        invalid = [[], {}, {"title": ""}, {"priority": True}, {"priority": 4}, {"status": "bad"},
                   {"due_at": "2026-10-03T00:00:00"}, {"due_at": "2026-10-03"},
                   {"tags": "tag"}, {"tags": [123]}, {"user_id": self.other.id}, {"id": 999},
                   {"title": "x" * 256}, {"notes": None}, {"project": []}, {"title": "bad\ud800"}]
        for body in invalid:
            with self.subTest(body=body):
                response = self.request("PATCH", f"/tasks/{task['id']}", body)
                self.assertEqual(response.status_code, 400, response.get_json())
        response = self.request("PATCH", f"/tasks/{task['id']}", {"title": "Changed", "status": "done", "priority": 99})
        self.assertEqual(response.status_code, 400)
        db.session.expire_all()
        row = db.session.get(Task, task["id"])
        self.assertEqual(row.title, "Deadline")
        self.assertEqual(row.status, "open")
        self.assertIsNone(row.completed_at)

    def test_event_overlap_point_events_and_master_only_list(self):
        crossing = self.event(start_at="2026-10-02T23:00:00Z", end_at="2026-10-03T01:00:00Z")
        point = self.event(start_at="2026-10-03T00:00:00Z", end_at=None)
        self.event(start_at="2026-10-02T23:00:00Z", end_at="2026-10-03T00:00:00Z")
        self.event(start_at="2026-10-04T00:00:00Z", end_at=None)
        self.event(start_at="2026-10-01T08:00:00Z", end_at=None, rrule="FREQ=DAILY")
        data = self.request("GET", "/events?start=2026-10-03T00:00:00Z&end=2026-10-04T00:00:00Z").get_json()
        self.assertEqual([e["id"] for e in data["data"]], [crossing["id"], point["id"]])

    def test_event_reminder_state_and_atomic_cross_field_validation(self):
        event = self.event(reminder_minutes=30, location="Room", description="Detail", all_day=False)
        row = db.session.get(Event, event["id"])
        row.last_reminded_occurrence_utc = datetime(2026, 10, 3, 2)
        db.session.commit()
        self.request("PATCH", f"/events/{event['id']}", {"title": "Renamed"})
        self.assertIsNotNone(row.last_reminded_occurrence_utc)
        response = self.request("PATCH", f"/events/{event['id']}", {"title": "Bad", "start_at": "2026-10-04T00:00:00Z"})
        self.assertEqual(response.status_code, 400)
        db.session.refresh(row)
        self.assertEqual(row.title, "Renamed")
        self.assertEqual(row.start_utc, datetime(2026, 10, 3, 2))
        response = self.request("PATCH", f"/events/{event['id']}", {"reminder_minutes": 0, "end_at": None})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(row.last_reminded_occurrence_utc)
        self.assertIsNone(response.get_json()["data"]["end_at"])

    def test_invalid_event_shapes_types_dates_and_rules(self):
        invalid = [[], {}, {"title": "Missing start"}, {"all_day": 1}, {"reminder_minutes": True},
                   {"reminder_minutes": -1}, {"reminder_minutes": 525601}, {"start_at": None},
                   {"end_at": "2026-10-01T00:00:00Z"}, {"start_at": "2026-10-03T12:00:00"},
                   {"rrule": "FREQ=SECONDLY"}, {"rrule": "FREQ=DAILY;BYHOUR=1,2,3"},
                   {"rrule": "FREQ=DAILY;INTERVAL=0"}, {"rrule": "FREQ=DAILY;COUNT=10001"},
                   {"rrule": "FREQ=DAILY\nDTSTART:20260101"}, {"rrule": "FREQ=WEEKLY;BYDAY=1MO"},
                   {"rrule": "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30"}, {"rrule": "FREQ=DAILY;UNTIL=20261231"},
                   {"rrule": "FREQ=DAILY;COUNT=2;UNTIL=20261231T000000Z"}, {"rrule": "FREQ=DAILY;FREQ=WEEKLY"},
                   {"user_id": 999}, {"description": "bad\ud800"}]
        for body in invalid:
            payload = body if isinstance(body, list) or not body or set(body) == {"title"} else {"title": "Invalid", "start_at": "2026-10-03T02:00:00Z", **body}
            with self.subTest(body=body):
                response = self.request("POST", "/events", payload)
                self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(Event.query.count(), 0)

    def test_queries_reject_unknown_repeated_and_invalid_bounds(self):
        for path in ("/tasks?limit=201", "/tasks?offset=-1", "/tasks?limit=1&limit=2", "/tasks?status=bad",
                     "/tasks?user_id=2", "/events?limit=0", "/events?start=nope", "/events?foo=bar",
                     "/events?start=2026-10-04T00:00:00Z&end=2026-10-03T00:00:00Z",
                     "/tasks?due_after=2026-10-04T00:00:00Z&due_before=2026-10-03T00:00:00Z"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path).status_code, 400)

    def test_occurrences_include_overlap_and_repeat_metadata_with_user_isolation(self):
        daily = self.event(start_at="2026-10-01T23:00:00Z", end_at="2026-10-02T01:00:00Z", rrule="FREQ=DAILY;COUNT=5")
        point = self.event(start_at="2026-10-03T00:00:00Z", end_at=None)
        foreign = self.request("POST", "/events", {"title": "Foreign", "start_at": "2026-10-03T00:00:00Z", "rrule": "FREQ=DAILY"}, other=True)
        self.assertEqual(foreign.status_code, 201)
        query = "/events/occurrences?start=2026-10-03T00:00:00Z&end=2026-10-04T00:00:00Z"
        response = self.request("GET", query)
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()["data"]
        self.assertEqual([d["event_id"] for d in data], [daily["id"], point["id"], daily["id"]])
        self.assertEqual(data[0]["start_at"], "2026-10-02T23:00:00Z")
        self.assertEqual(data[0]["end_at"], "2026-10-03T01:00:00Z")
        self.assertEqual(data[0]["occurrence_id"], f"{daily['id']}:2026-10-02T23:00:00Z")
        self.assertEqual(response.get_json()["pagination"]["total"], 3)

    def test_occurrences_keep_wall_clock_across_dst(self):
        self.user.timezone = "America/New_York"
        db.session.commit()
        self.event(start_at="2026-10-31T09:00:00-04:00", end_at=None, rrule="FREQ=DAILY;COUNT=3")
        response = self.request("GET", "/events/occurrences?start=2026-10-31T00:00:00Z&end=2026-11-03T00:00:00Z")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual([d["start_at"] for d in response.get_json()["data"]],
                         ["2026-10-31T13:00:00Z", "2026-11-01T14:00:00Z", "2026-11-02T14:00:00Z"])

    def test_occurrence_date_overflow_is_validation_error(self):
        self.event(start_at="2026-10-01T00:00:00Z", end_at="9999-12-31T00:00:00Z", rrule="FREQ=DAILY")
        response = self.request("GET", "/events/occurrences?start=2026-10-01T00:00:00Z&end=2026-10-04T00:00:00Z")
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["code"], "invalid_request")

    def test_occurrence_bounds_and_unsupported_legacy_rules_fail_explicitly(self):
        for path in ("/events/occurrences", "/events/occurrences?start=2026-10-03T00:00:00Z",
                     "/events/occurrences?start=2026-01-01T00:00:00Z&end=2026-12-31T00:00:00Z"):
            self.assertEqual(self.request("GET", path).status_code, 400)
        event = Event(user_id=self.user.id, title="Legacy", start_utc=datetime(2026, 10, 1), rrule="FREQ=SECONDLY")
        db.session.add(event)
        db.session.commit()
        response = self.request("GET", "/events/occurrences?start=2026-10-03T00:00:00Z&end=2026-10-04T00:00:00Z")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_recurrence_result_candidate_and_scan_budgets_are_not_silent_truncation(self):
        self.event(start_at="2026-10-01T00:00:00Z", end_at=None, rrule="FREQ=DAILY")
        path = "/events/occurrences?start=2026-10-01T00:00:00Z&end=2026-10-05T00:00:00Z"
        for setting, bound in (("_MAX_OCCURRENCES", 2), ("_MAX_CANDIDATES", 0), ("_MAX_ITERATIONS", 2)):
            with self.subTest(setting=setting), patch("app.blueprints.integration_data." + setting, bound):
                response = self.request("GET", path)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["code"], "range_too_complex")
        row = Event.query.one()
        row.start_utc = datetime(1900, 1, 1)
        row.rrule = "FREQ=DAILY;BYMONTH=2;BYMONTHDAY=29"
        db.session.commit()
        with patch("app.blueprints.integration_data.rrulestr") as parser:
            response = self.request("GET", path)
            self.assertEqual(response.status_code, 400)
            # Validation parses syntax once; no iteration ever reaches sparse historical rules.
            self.assertEqual(parser.call_count, 1)
