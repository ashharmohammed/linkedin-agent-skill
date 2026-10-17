"""Offline safety tests. Transport is mocked; no LinkedIn calls or real tokens."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import uuid
import unittest
from unittest.mock import patch
import urllib.request

source = Path(__file__).resolve().with_name("linkedin_publisher.py")
spec = importlib.util.spec_from_file_location("publisher", source)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        parent = source.parent / "work" / "tests"
        parent.mkdir(parents=True, exist_ok=True)
        # Plain directories avoid Python 3.14's private-temp ACL mismatch in the
        # Windows execution sandbox. Retain dummy fixtures under ignored work/.
        fixture = parent / str(uuid.uuid4())
        fixture.mkdir()
        self.output = fixture / "outputs"
        self.work = fixture / "work"
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(p, "OUTPUTS", self.output))
        self.stack.enter_context(patch.object(p, "WORK", self.work))
        # Deny accidental real network use even if a test mock is missing.
        self.stack.enter_context(patch.object(urllib.request.OpenerDirector, "open",
                                             side_effect=AssertionError("Network forbidden")))
        self.creds = {"access_token": "OFFLINE_FAKE", "author": "urn:li:person:DEMO_TEST",
                      "intended_name": p.EXPECTED_NAME,
                      "expires_at": "2099-01-01T00:00:00+00:00", "scopes": list(p.SCOPES)}
        self.id = "LI-20261012-TEST-v1"
        self.text = "A reviewed post (API tests) with @literal text, #literal and C:\\path."
        self.save()

    def tearDown(self):
        self.stack.close()

    def save(self, kind="requested", draft_id=None, text=None):
        draft_id = draft_id or self.id
        text = self.text if text is None else text
        folder = self.output / "drafts"
        folder.mkdir(parents=True, exist_ok=True)
        raw = text.encode()
        (folder / (draft_id + ".txt")).write_bytes(raw)
        meta = {"draft_id": draft_id, "sha256": p.digest(raw), "kind": kind}
        (folder / (draft_id + ".json")).write_text(json.dumps(meta))
        return meta

    def approval(self, author="urn:li:person:DEMO_TEST"):
        return p.approve(self.id, p.digest(self.text.encode()), "TEST ONLY human approval", author)

    def live_mocks(self, side_effect=None, response=(201, {"X-RestLi-Id": "urn:li:share:123"}, b"")):
        self.stack.enter_context(patch.object(p, "load_credentials", return_value=self.creds))
        self.stack.enter_context(patch.object(p, "identity", return_value={"author": self.creds["author"], "name": p.EXPECTED_NAME}))
        return self.stack.enter_context(patch.object(p, "request", return_value=response, side_effect=side_effect))

    def row(self):
        with p.database() as db:
            return dict(db.execute("SELECT * FROM submissions WHERE draft_id=?", (self.id,)).fetchone())

    def test_tampered_text_blocks_before_network(self):
        self.approval()
        (self.output / "drafts" / (self.id + ".txt")).write_text("Changed after review")
        with self.assertRaisesRegex(p.GuardError, "hash"):
            p.publish(self.id)

    def test_sample_cannot_be_approved_or_published(self):
        self.save(kind="sample")
        with self.assertRaises(p.GuardError):
            self.approval()
        with self.assertRaises(p.GuardError):
            p.publish(self.id)

    def test_no_approval_blocks_before_auth(self):
        with self.assertRaisesRegex(p.GuardError, "No direct-human approval"):
            p.publish(self.id)

    def test_pending_identity_approval_retained(self):
        self.approval(author=None)
        with self.assertRaisesRegex(p.GuardError, "Approval retained"):
            p.publish(self.id)
        with p.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 1)

    def test_wrong_review_hash_cannot_approve(self):
        with self.assertRaises(p.GuardError):
            p.approve(self.id, "wrong", "human approved")

    def test_account_mismatch_blocks(self):
        self.approval(author="urn:li:person:OTHER")
        transport = self.live_mocks()
        with self.assertRaisesRegex(p.GuardError, "differs"):
            p.publish(self.id)
        transport.assert_not_called()

    def test_live_identity_mismatch_blocks(self):
        self.approval()
        transport = self.live_mocks()
        with patch.object(p, "identity", return_value={"author": "urn:li:person:OTHER"}):
            with self.assertRaisesRegex(p.GuardError, "Live member"):
                p.publish(self.id)
        transport.assert_not_called()

    def test_success_persists_and_blocks_second_submission(self):
        self.approval()
        transport = self.live_mocks()
        result = p.publish(self.id)
        self.assertEqual(result["post_urn"], "urn:li:share:123")
        self.assertEqual(self.row()["status"], "published")
        # Unescape little text, independent of the production helper.
        wire = transport.call_args.args[3]["commentary"]
        restored, i = "", 0
        while i < len(wire):
            if wire[i] == "\\":
                i += 1
            restored += wire[i]
            i += 1
        self.assertEqual(restored, self.text)
        with self.assertRaisesRegex(p.GuardError, "no automatic retry"):
            p.publish(self.id)
        self.assertEqual(transport.call_count, 1)

    def test_in_flight_is_committed_before_post(self):
        self.approval()
        def send(*_):
            self.assertEqual(self.row()["status"], "in_flight")
            return 201, {"x-restli-id": "urn:li:ugcPost:456"}, b""
        self.live_mocks(side_effect=send)
        self.assertEqual(p.publish(self.id)["post_urn"], "urn:li:ugcPost:456")

    def test_timeout_marks_unknown_and_never_retries(self):
        self.approval()
        transport = self.live_mocks(side_effect=TimeoutError("FAKE sensitive transport text"))
        with self.assertRaisesRegex(p.GuardError, "Submission unknown") as error:
            p.publish(self.id)
        self.assertNotIn("sensitive", str(error.exception))
        self.assertEqual(self.row()["status"], "unknown")
        with self.assertRaises(p.GuardError):
            p.publish(self.id)
        self.assertEqual(transport.call_count, 1)

    def test_201_missing_post_id_is_unknown(self):
        self.approval()
        self.live_mocks(response=(201, {}, b""))
        with self.assertRaises(p.GuardError):
            p.publish(self.id)
        self.assertEqual(self.row()["status"], "unknown")

    def test_5xx_is_unknown_and_4xx_is_locked_rejected(self):
        self.approval()
        self.live_mocks(side_effect=p.HttpError(503))
        with self.assertRaises(p.GuardError):
            p.publish(self.id)
        self.assertEqual(self.row()["status"], "unknown")
        # Separate draft with distinct content.
        other = "LI-20261012-OTHER-v1"
        meta = self.save(draft_id=other, text="Other reviewed text")
        p.approve(other, meta["sha256"], "TEST approval", self.creds["author"])
        with patch.object(p, "request", side_effect=p.HttpError(403)):
            with self.assertRaises(p.GuardError):
                p.publish(other)
        with p.database() as db:
            self.assertEqual(db.execute("SELECT status FROM submissions WHERE draft_id=?", (other,)).fetchone()[0], "rejected")

    def test_same_text_new_id_cannot_duplicate(self):
        self.approval()
        transport = self.live_mocks()
        p.publish(self.id)
        other = "LI-20261012-COPY-v1"
        meta = self.save(draft_id=other)
        p.approve(other, meta["sha256"], "TEST approval", self.creds["author"])
        with self.assertRaises(p.GuardError):
            p.publish(other)
        self.assertEqual(transport.call_count, 1)

    def test_reconcile_unknown_does_not_send_request(self):
        self.approval()
        transport = self.live_mocks(side_effect=TimeoutError())
        with self.assertRaises(p.GuardError):
            p.publish(self.id)
        result = p.reconcile(self.id, "urn:li:share:999", "TEST ONLY inspected author and exact text")
        self.assertEqual(result["status"], "published_reconciled")
        self.assertEqual(transport.call_count, 1)

    def test_scheduled_slots_allow_three_post_days_and_block_other_days(self):
        p.claim_slot("2026-10-12", self.id)
        self.assertEqual(p.claim_slot("2026-10-12", self.id)["status"], "already_claimed")
        with self.assertRaises(p.GuardError):
            p.claim_slot("2026-10-12", "another")
        for date, ident in (("2026-10-14", "wednesday"), ("2026-10-16", "friday")):
            self.assertEqual(p.claim_slot(date, ident)["status"], "claimed")
        for date in ("2026-10-11", "2026-10-13", "2026-10-15", "2026-10-17", "2026-10-18"):
            with self.assertRaises(p.GuardError):
                p.claim_slot(date, "off-day")
        with p.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scheduled_slots").fetchone()[0], 3)

    def test_stage_normalizes_and_protects_review_version(self):
        path = self.work.parent / "input.txt"
        path.write_bytes(b"Review this.\r\nSecond line.\r\n")
        result = p.stage("LI-20261014-STAGED-v1", path, "requested", "Review")
        saved = Path(result["text_path"]).read_bytes()
        self.assertEqual(saved, b"Review this.\nSecond line.")
        self.assertEqual(result["sha256"], p.digest(saved))
        with self.assertRaises(p.GuardError):
            p.stage("LI-20261014-STAGED-v1", path, "requested", "Review")
        self.assertEqual(Path(result["text_path"]).read_bytes(), saved)

    def test_custom_days_control_reservations(self):
        self.work.mkdir(parents=True)
        (self.work / "settings.json").write_text(json.dumps({"timezone": "UTC", "post_days": [1, 3]}))
        self.assertEqual(p.claim_slot("2026-10-13", "tuesday")["status"], "claimed")
        with self.assertRaises(p.GuardError):
            p.claim_slot("2026-10-14", "wednesday")

    def test_invalid_settings_fail_closed(self):
        self.work.mkdir(parents=True)
        (self.work / "settings.json").write_text(json.dumps({"timezone": "UTC", "post_days": [True]}))
        with self.assertRaises(p.GuardError):
            p.claim_slot("2026-10-13", "invalid")

    def test_init_does_not_overwrite_settings(self):
        p.initialize("UTC", ["tue", "thu"])
        with self.assertRaises(p.GuardError):
            p.initialize("UTC", ["mon"])
        self.assertEqual(p.settings()["post_days"], [1, 3])

    @unittest.skipUnless(p.os.name == "nt", "DPAPI connection is Windows-only")
    def test_dpapi_roundtrip_with_dummy_bytes(self):
        dummy = b"OFFLINE DUMMY ONLY"
        sealed = p.dpapi(dummy)
        self.assertNotIn(dummy, sealed)
        self.assertEqual(p.dpapi(sealed, decrypt=True), dummy)

    def test_expired_credentials_block(self):
        path = self.work / "private" / "token.dpapi"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"DUMMY")
        self.creds["expires_at"] = "2020-01-01T00:00:00+00:00"
        with patch.object(p, "dpapi", return_value=json.dumps(self.creds).encode()):
            with self.assertRaisesRegex(p.GuardError, "expiry"):
                p.load_credentials()

    def test_redirects_and_unofficial_endpoints_block(self):
        self.assertIsNone(p.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test"))
        with self.assertRaises(p.GuardError):
            p.request("POST", "https://elsewhere.test", "DUMMY")

    def test_dry_run_is_offline_and_secret_free(self):
        with patch.object(p.sys, "argv", ["publisher", "dry-run", self.id]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                p.main()
            result = json.loads(buf.getvalue())
            self.assertEqual(result["network_calls"], 0)
            self.assertNotIn("access_token", buf.getvalue())

    def test_withdrawn_approval_cannot_publish_or_be_reinstated(self):
        self.approval()
        p.withdraw(self.id, "TEST direct human revision request")
        with self.assertRaisesRegex(p.GuardError, "withdrawn"):
            p.publish(self.id)
        with self.assertRaisesRegex(p.GuardError, "withdrawn"):
            self.approval()

    def test_withdraw_cannot_claim_to_cancel_a_sent_post(self):
        self.approval()
        self.live_mocks()
        p.publish(self.id)
        with self.assertRaisesRegex(p.GuardError, "cannot retract"):
            p.withdraw(self.id, "TEST cancellation")



class ExpiryInputTests(unittest.TestCase):
    reference = dt.datetime(1990, 1, 1, tzinfo=dt.timezone.utc)
    expected = dt.datetime(2000, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)

    def test_portal_unix_timestamp_with_or_without_utc(self):
        for raw in ("946684800 UTC", "946684800", " 946684800 utc "):
            with self.subTest(raw=raw):
                self.assertEqual(p.parse_expiry(raw, self.reference), self.expected)

    def test_iso_timezone_normalizes_to_same_instant(self):
        for raw in ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00Z",
                    "1999-12-31T19:00:00-05:00", "2000-01-01T00:00:00 UTC"):
            with self.subTest(raw=raw):
                self.assertEqual(p.parse_expiry(raw, self.reference), self.expected)

    def test_invalid_expired_naive_and_millisecond_values_are_rejected(self):
        for raw in ("", "not a date", "2000-01-01T00:00:00", "1 UTC",
                    "946684800000", str(int(self.reference.timestamp()))):
            with self.subTest(raw=raw):
                with self.assertRaises(p.GuardError):
                    p.parse_expiry(raw, self.reference)

    def test_connect_persists_normalized_expiry_from_portal_timestamp(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(p.sys.stdin, "isatty", return_value=True))
            stack.enter_context(patch.object(p.getpass, "getpass", return_value="OFFLINE_FAKE"))
            stack.enter_context(patch("builtins.input", side_effect=[
                "4102444800 UTC", "openid profile w_member_social", "urn:li:person:DEMO_TEST"]))
            stack.enter_context(patch.object(p, "identity", return_value={
                "author": "urn:li:person:DEMO_TEST", "name": p.EXPECTED_NAME}))
            stack.enter_context(patch.object(p.Path, "exists", return_value=False))
            stack.enter_context(patch.object(p.Path, "mkdir"))
            stack.enter_context(patch.object(p.Path, "open", create=True,
                                           return_value=io.BytesIO()))
            stack.enter_context(patch.object(p.os, "fsync"))
            stack.enter_context(patch.object(p.os, "replace"))
            stack.enter_context(patch.object(p, "atomic_json"))
            sealed = stack.enter_context(patch.object(p, "dpapi", side_effect=RuntimeError("STOP BEFORE SAVE")))
            with self.assertRaisesRegex(RuntimeError, "STOP BEFORE SAVE"):
                p.connect()
            data = json.loads(sealed.call_args.args[0])
            self.assertEqual(data["expires_at"], "2100-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
