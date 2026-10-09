"""File service boundaries and download publication, using isolated temp roots."""
from __future__ import annotations

import errno
from pathlib import Path
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.tests.support import IsolatedAppTestCase
from app.extensions import db
from app.models.storage import StorageLocation
from app.services import storage_service as storage


class StorageServiceTests(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.other = self.make_user("storage_other", False)
        self.root = Path(self.temp_directory.name) / "media"
        self.root.mkdir()
        self.location = storage.create_location(self.user.id, {
            "name": "本机媒体", "root_path": str(self.root), "download_path": "/downloads",
        })

    def assert_storage_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(storage.StorageError) as raised:
            fn(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_registration_requires_admin_existing_absolute_root_and_no_overlap(self):
        self.assert_storage_error("admin_required", storage.create_location, self.other.id, {
            "name": "Secret", "root_path": str(self.root),
        })
        self.assert_storage_error("invalid_argument", storage.create_location, self.user.id, {
            "name": "Relative", "root_path": "../../outside",
        })
        missing = self.root.parent / "absent"
        self.assert_storage_error("storage_offline", storage.create_location, self.user.id, {
            "name": "Missing", "root_path": str(missing),
        })
        self.assertFalse(missing.exists())
        nested = self.root / "nested"
        nested.mkdir()
        self.assert_storage_error("storage_overlap", storage.create_location, self.user.id, {
            "name": "Overlapping", "root_path": str(nested),
        })
        self.assertEqual(StorageLocation.query.count(), 1)

    def test_users_cannot_list_resolve_or_download_another_users_storage(self):
        (self.root / "private.txt").write_text("private", encoding="utf-8")
        self.assertEqual(storage.list_locations(self.other.id), [])
        self.assert_storage_error("storage_not_found", storage.get_location, self.other.id, self.location.id)
        self.assert_storage_error("storage_not_found", storage.list_files, self.other.id, self.location.id)
        self.assert_storage_error("storage_not_found", storage.get_file, self.other.id,
                                  self.location.id, "private.txt")
        metadata = storage.list_locations(self.user.id)[0]
        self.assertNotIn("root_path", metadata)
        self.assertNotIn("marker_token", metadata)
        self.assertNotIn("marker_name", metadata)

    def test_admin_can_assign_only_independent_existing_directory_to_other_user(self):
        root = self.root.parent / "other-user-media"
        root.mkdir()
        (root / "owned.txt").write_text("member file", encoding="utf-8")
        assigned = storage.create_location(self.user.id, {
            "name": "用户独立媒体库", "owner_id": self.other.id, "root_path": str(root),
        })
        self.assertEqual(assigned.user_id, self.other.id)
        self.assertEqual(storage.list_locations(self.other.id)[0]["id"], assigned.id)
        self.assertNotIn("root_path", storage.list_locations(self.other.id)[0])
        self.assert_storage_error("storage_not_found", storage.get_location, self.user.id, assigned.id)
        self.assertEqual(storage.get_file(self.other.id, assigned.id, "owned.txt").read_text(), "member file")
        self.assert_storage_error("admin_required", storage.create_location, self.other.id, {
            "name": "Impersonation", "owner_id": self.user.id, "root_path": str(root),
        })
        self.assert_storage_error("storage_overlap", storage.create_location, self.user.id, {
            "name": "Leaked admin files", "owner_id": self.other.id, "root_path": str(self.root),
        })
        self.assert_storage_error("storage_overlap", storage.create_location, self.user.id, {
            "name": "Leaked member files", "owner_id": self.user.id, "root_path": str(self.root.parent),
        })
        self.assert_storage_error("owner_not_found", storage.create_location, self.user.id, {
            "name": "Missing account", "owner_id": 999999, "root_path": str(root),
        })

    def test_storage_assignment_api_preserves_owner_isolation(self):
        from app.utils.api_auth import assign_user_api_token
        root = self.root.parent / "api-member-media"
        root.mkdir()
        headers = {"Authorization": "Bearer " + assign_user_api_token(self.user)}
        member_headers = {"Authorization": "Bearer " + assign_user_api_token(self.other)}
        client = self.app.test_client(use_cookies=False)
        response = client.post("/api/v1/storage-locations", headers=headers, json={
            "name": "分配给普通用户", "owner_id": self.other.id, "root_path": str(root),
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        assigned = response.get_json()["data"]
        self.assertEqual(assigned["user_id"], self.other.id)
        response = client.get("/api/v1/storage-locations", headers=member_headers)
        self.assertEqual([row["id"] for row in response.get_json()["data"]], [assigned["id"]])
        self.assertNotIn("root_path", response.get_json()["data"][0])
        for token_headers, id_ in ((headers, assigned["id"]), (member_headers, self.location.id)):
            response = client.get("/api/v1/files", headers=token_headers, query_string={"storage_id": id_})
            self.assertEqual(response.status_code, 404, response.get_json())

    def test_paths_reject_both_os_absolute_paths_dot_segments_and_ads(self):
        for value in ("../secret", "nested/../../secret", "/etc/passwd", "C:\\Windows\\win.ini",
                      "\\\\server\\share", "data.txt:stream", "./private", "folder//file",
                      "folder/../file", "folder. /file", "folder/\x00secret"):
            with self.subTest(value=value):
                self.assert_storage_error("unsafe_path", storage.resolve_path, self.location,
                                          value, must_exist=False)

    def test_internal_marker_and_pending_files_are_not_exposed(self):
        paths = storage.prepare_attempt(self.location, 12, 34)
        (Path(paths["local_path"]) / "unfinished.mp4").write_bytes(b"incomplete")
        (self.root / "finished.mp4").write_bytes(b"done")
        data = storage.list_files(self.user.id, self.location.id)
        self.assertEqual([entry["name"] for entry in data["entries"]], ["finished.mp4"])
        self.assert_storage_error("file_not_found", storage.get_file, self.user.id,
                                  self.location.id, self.location.marker_name)
        self.assert_storage_error("file_not_found", storage.list_files, self.user.id,
                                  self.location.id, paths["relative_path"])
        self.assert_storage_error("file_not_found", storage.get_file, self.user.id,
                                  self.location.id, paths["relative_path"] + "/unfinished.mp4")
        self.assert_storage_error("file_not_found", storage.get_file, self.user.id,
                                  self.location.id, paths["relative_path"].upper() + "/unfinished.mp4")
        self.assertEqual(storage.get_file(self.user.id, self.location.id, "finished.mp4").read_bytes(), b"done")

    def test_missing_or_wrong_mount_marker_blocks_writes_even_when_root_still_exists(self):
        marker = self.root / self.location.marker_name
        marker.unlink()
        health = storage.probe_location(self.location)
        self.assertFalse(health["ok"])
        self.assertEqual(health["code"], "storage_offline")
        self.assert_storage_error("storage_offline", storage.prepare_attempt, self.location, 1, 1)
        self.assertFalse((self.root / ".lingxi-pending").exists())
        marker.write_text("wrong-mounted-drive", encoding="ascii")
        self.assertEqual(storage.probe_location(self.location)["code"], "storage_identity_changed")
        marker.write_text(self.location.marker_token, encoding="ascii")
        self.assertTrue(storage.probe_location(self.location)["ok"])

    def test_mount_registration_rejects_unmounted_directory_before_writing_marker(self):
        root = self.root.parent / "nas"
        root.mkdir()
        with patch.object(storage, "_mount_backed", return_value=False):
            self.assert_storage_error("storage_offline", storage.create_location, self.user.id, {
                "name": "NAS", "kind": "mount", "root_path": str(root),
            })
        self.assertEqual(list(root.iterdir()), [])
        with patch.object(storage, "_mount_backed", return_value=True):
            nas = storage.create_location(self.user.id, {
                "name": "NAS", "kind": "mount", "root_path": str(root),
            })
            # Genuine remote remounts may change st_dev, but preserve their marker.
            nas.root_device = "previous-remote-device"
            self.assertTrue(storage.probe_location(nas)["ok"])
        with patch.object(storage, "_mount_backed", return_value=False):
            self.assertEqual(storage.probe_location(nas)["code"], "storage_offline")

    def test_disk_space_check_happens_before_attempt_directory_creation(self):
        with patch.object(storage.shutil, "disk_usage", return_value=SimpleNamespace(
                total=1024, used=1000, free=24)):
            self.assert_storage_error("storage_full", storage.prepare_attempt, self.location, 1, 1, 100)
        self.assertFalse((self.root / ".lingxi-pending").exists())

    def test_each_attempt_has_distinct_bytes_and_correct_downloader_mapping(self):
        one = storage.prepare_attempt(self.location, 10, 1)
        two = storage.prepare_attempt(self.location, 10, 2)
        self.assertNotEqual(one["relative_path"], two["relative_path"])
        self.assertEqual(one["download_path"], "/downloads/.lingxi-pending/tasks/10/attempts/1")
        self.assertEqual(storage.prepare_attempt(self.location, 10, 1), one)
        (Path(one["local_path"]) / "a.mp4").write_bytes(b"first resource")
        self.assertFalse((Path(two["local_path"]) / "a.mp4").exists())

    def test_finalize_is_atomic_and_idempotent_after_database_update_interruption(self):
        attempt = storage.prepare_attempt(self.location, 10, 1)
        (Path(attempt["local_path"]) / "episode.mp4").write_bytes(b"verified download")
        result = storage.finalize_attempt(self.location, 10, 1)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["total_bytes"], 17)
        self.assertFalse(Path(attempt["local_path"]).exists())
        self.assertEqual(storage.finalize_attempt(self.location, 10, 1), result)
        public_file = storage.get_file(self.user.id, self.location.id, result["files"][0]["path"])
        self.assertEqual(public_file.read_bytes(), b"verified download")
        self.assert_storage_error("attempt_completed", storage.prepare_attempt, self.location, 10, 1)

    def test_finalize_rejects_empty_and_incomplete_downloads(self):
        attempt = storage.prepare_attempt(self.location, 1, 1)
        self.assert_storage_error("download_empty", storage.finalize_attempt, self.location, 1, 1)
        (Path(attempt["local_path"]) / "episode.mp4").write_bytes(b"data")
        (Path(attempt["local_path"]) / "episode.mp4.aria2").write_bytes(b"progress")
        self.assert_storage_error("download_incomplete", storage.finalize_attempt, self.location, 1, 1)
        self.assertFalse((self.root / "completed").exists())

    def test_finalize_checks_expected_files_before_move_and_on_restart(self):
        attempt = storage.prepare_attempt(self.location, 1, 1)
        source = Path(attempt["local_path"])
        (source / "episode.mp4").write_bytes(b"content")
        expected = [{"path": "episode.mp4", "size": 7}]
        self.assert_storage_error("download_size_mismatch", storage.finalize_attempt, self.location, 1, 1,
                                  expected_files=[{"path": "episode.mp4", "size": 700}])
        self.assertTrue(source.exists())
        result = storage.finalize_attempt(self.location, 1, 1, expected_files=expected)
        self.assertEqual(storage.finalize_attempt(self.location, 1, 1, expected_files=expected), result)
        (self.root / result["files"][0]["path"]).write_bytes(b"changed")
        self.assert_storage_error("download_size_mismatch", storage.finalize_attempt, self.location, 1, 1,
                                  expected_files=[{"path": "episode.mp4", "size": 8}])

    def test_finalize_rejects_extra_or_unsafe_manifest_files(self):
        attempt = storage.prepare_attempt(self.location, 1, 1)
        source = Path(attempt["local_path"])
        (source / "episode.mp4").write_bytes(b"content")
        (source / "unexpected.bin").write_bytes(b"untracked")
        self.assert_storage_error("download_size_mismatch", storage.finalize_attempt, self.location, 1, 1,
                                  expected_files=[{"path": "episode.mp4", "size": 7}])
        self.assert_storage_error("unsafe_path", storage.finalize_attempt, self.location, 1, 1,
                                  expected_files=[{"path": "../other", "size": 7}])
        self.assertFalse((self.root / "completed").exists())

    def test_media_page_renders_for_admin_and_member_without_exposing_admin_form(self):
        admin = self.client_for(self.user).get("/media/")
        self.assertEqual(admin.status_code, 200)
        page = admin.get_data(as_text=True)
        for marker in ('data-api-base="/media/api/"', 'id="media-config-form"', 'id="media-create-form"',
                       'id="media-agent-form"', 'id="media-files-panel"'):
            self.assertIn(marker, page)
        member = self.client_for(self.other).get("/media/")
        self.assertEqual(member.status_code, 200)
        self.assertNotIn('id="media-config-form"', member.get_data(as_text=True))
        self.assertNotIn(self.location.marker_token, page)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript syntax validation")
    def test_media_javascript_syntax(self):
        script = Path(__file__).resolve().parents[2] / "app/static/js/media.js"
        result = subprocess.run([shutil.which("node"), "--check", str(script)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cross_device_finalize_verifies_copy_before_publishing_and_preserves_source(self):
        attempt = storage.prepare_attempt(self.location, 1, 1)
        source = Path(attempt["local_path"])
        (source / "episode.mp4").write_bytes(b"media bytes")
        original_rename = Path.rename

        def rename(path, target):
            if path == source:
                raise OSError(errno.EXDEV, "Different filesystem")
            return original_rename(path, target)

        with patch.object(Path, "rename", rename):
            result = storage.finalize_attempt(self.location, 1, 1)
        self.assertEqual((self.root / result["files"][0]["path"]).read_bytes(), b"media bytes")
        self.assertEqual((source / "episode.mp4").read_bytes(), b"media bytes")
        self.assertEqual(storage.finalize_attempt(self.location, 1, 1), result)

    def test_corrupted_cross_device_copy_is_never_published(self):
        attempt = storage.prepare_attempt(self.location, 1, 1)
        source = Path(attempt["local_path"])
        (source / "episode.mp4").write_bytes(b"original bytes")
        original_rename = Path.rename
        original_copy = storage.shutil.copytree

        def rename(path, target):
            if path == source:
                raise OSError(errno.EXDEV, "Different filesystem")
            return original_rename(path, target)

        def corrupt_copy(src, dst, **kwargs):
            result = original_copy(src, dst, **kwargs)
            (Path(dst) / "episode.mp4").write_bytes(b"corrupt bytes!")
            return result

        with patch.object(Path, "rename", rename), patch.object(storage.shutil, "copytree", corrupt_copy):
            self.assert_storage_error("copy_verification_failed", storage.finalize_attempt, self.location, 1, 1)
        self.assertFalse((self.root / "completed/tasks/1/attempts/1").exists())
        self.assertEqual((source / "episode.mp4").read_bytes(), b"original bytes")
        listing = storage.list_files(self.user.id, self.location.id, "completed/tasks/1/attempts")
        self.assertEqual(listing["entries"], [])

    def test_symbolic_links_cannot_escape_even_through_nested_or_final_components(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        try:
            (self.root / "escape").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit test symlinks")
        self.assert_storage_error("unsafe_path", storage.resolve_path, self.location, "escape/secret.txt")
        self.assert_storage_error("unsafe_path", storage.resolve_path, self.location,
                                  "escape/new/file", must_exist=False)
        self.assertEqual(storage.list_files(self.user.id, self.location.id)["entries"], [])
        attempt = storage.prepare_attempt(self.location, 1, 1)
        (Path(attempt["local_path"]) / "escape").symlink_to(outside, target_is_directory=True)
        self.assert_storage_error("unsafe_path", storage.finalize_attempt, self.location, 1, 1)
