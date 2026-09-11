"""Object storage, RBAC, authentication, signing, telemetry and SLOs."""

import base64
import tempfile
import unittest
from pathlib import Path

from src.telemetry.pricing import PricingTable
from src.telemetry.recorder import TelemetryRecorder
from src.telemetry.slo import evaluate_slos, percentile
from src.trust.ports import TrustIntegrityError
from src.webapi.auth import AuthenticationError, authenticate, bearer_from_header
from src.workspace import rbac
from src.workspace.objects import LocalWorkspaceObjectStore, content_key, digest_bytes
from src.workspace.queue import QueueSettings
from src.workspace.signing import Signer, SigningError, verify_manifest

TENANT = "00000000-0000-4000-8000-000000000001"


class ObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LocalWorkspaceObjectStore(Path(self.temp.name) / "objects")

    def tearDown(self):
        self.temp.cleanup()

    def test_content_addressed_write_is_idempotent_and_verified_on_read(self):
        payload = b"%PDF-1.7 evidence"
        digest = digest_bytes(payload)
        key = content_key(TENANT, "manuscripts", digest, suffix=".pdf")
        first = self.store.put_bytes(payload, key=key, media_type="application/pdf")
        second = self.store.put_bytes(payload, key=key, media_type="application/pdf")
        self.assertEqual(first.key, second.key)
        self.assertEqual(first.content_sha256, digest)
        self.assertEqual(self.store.get_bytes(key, expected_sha256=digest), payload)
        self.assertEqual(b"".join(self.store.iter_chunks(key)), payload)

    def test_a_corrupted_object_is_refused_on_read(self):
        payload = b"original evidence bytes"
        digest = digest_bytes(payload)
        key = content_key(TENANT, "artifacts", digest, suffix=".json")
        self.store.put_bytes(payload, key=key, media_type="application/json")
        path = self.store.local_path(key)
        assert path is not None
        path.write_bytes(b"tampered evidence bytes")
        with self.assertRaises(TrustIntegrityError):
            self.store.get_bytes(key, expected_sha256=digest)

    def test_traversal_and_malformed_keys_are_rejected(self):
        for key in ("../escape", "/absolute", "a/../../b", "back\\slash"):
            with self.assertRaises(ValueError, msg=key):
                self.store.get_bytes(key)
        with self.assertRaises(ValueError):
            content_key(TENANT, "manuscripts", "not-a-digest")
        with self.assertRaises(ValueError):
            content_key("../evil", "manuscripts", "a" * 64)

    def test_missing_objects_raise_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            self.store.get_bytes(content_key(TENANT, "exports", "c" * 64))


class RbacTests(unittest.TestCase):
    def test_permissions_follow_the_declared_matrix(self):
        self.assertTrue(rbac.evaluate("reviewer", "review.decide").allowed)
        self.assertFalse(rbac.evaluate("reviewer", "audit.run").allowed)
        self.assertTrue(rbac.evaluate("operator", "audit.run").allowed)
        self.assertFalse(rbac.evaluate("operator", "review.decide").allowed)
        self.assertFalse(rbac.evaluate("viewer", "export.create").allowed)
        self.assertTrue(rbac.evaluate("viewer", "review.read").allowed)

    def test_correction_acceptance_needs_a_lead_reviewer(self):
        self.assertFalse(rbac.evaluate("reviewer", "correction.transition").allowed)
        self.assertTrue(rbac.evaluate("lead_reviewer", "correction.transition").allowed)

    def test_administrator_holds_every_permission(self):
        for permission in rbac.PERMISSIONS:
            self.assertTrue(rbac.evaluate("administrator", permission).allowed, permission)

    def test_unknown_roles_are_dropped_and_aliases_resolve(self):
        self.assertEqual(
            sorted(rbac.normalize_roles("Admin, citeprism-operator, wizard")),
            ["administrator", "operator"],
        )

    def test_denial_reason_names_the_permission_and_the_held_roles(self):
        reason = rbac.evaluate("viewer", "audit.run").reason
        self.assertIn("audit.run", reason)
        self.assertIn("viewer", reason)

    def test_unknown_permission_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            rbac.evaluate("reviewer", "not.a.permission")


class AuthenticationTests(unittest.TestCase):
    def principal(self, **overrides):
        arguments = {
            "mode": "local", "tenant": None, "actor": None, "roles": None,
            "supplied_proxy_secret": None, "configured_tenant": TENANT,
            "configured_proxy_secret": None,
        }
        arguments.update(overrides)
        return authenticate(**arguments)

    def test_local_mode_is_a_full_administrator_of_one_tenant(self):
        user = self.principal()
        self.assertEqual(user.tenant_id, TENANT)
        self.assertTrue(user.can("audit.run"))
        self.assertTrue(user.can("review.decide"))
        self.assertTrue(user.can("correction.transition"))

    def test_trusted_proxy_requires_the_shared_secret(self):
        with self.assertRaises(AuthenticationError):
            self.principal(mode="trusted_proxy", supplied_proxy_secret="wrong",
                           configured_proxy_secret="right", tenant=TENANT, actor="a")
        user = self.principal(mode="trusted_proxy", supplied_proxy_secret="right",
                              configured_proxy_secret="right", tenant=TENANT,
                              actor="a", roles="viewer")
        self.assertEqual(sorted(user.roles), ["viewer"])

    def test_a_non_uuid_tenant_is_refused(self):
        with self.assertRaises(AuthenticationError):
            self.principal(tenant="not-a-uuid")

    def test_oidc_mode_requires_a_verifier_and_a_token(self):
        with self.assertRaises(AuthenticationError):
            self.principal(mode="oidc", bearer_token="a.b.c", verifier=None)

    def test_oidc_claims_are_projected_onto_a_principal(self):
        class FakeVerifier:
            class settings:  # noqa: N801 - mirrors the real attribute shape
                require_tenant_claim = True

            def verify(self, token):
                assert token == "header.payload.signature"
                return {"sub": "u-1", "org_id": TENANT, "roles": ["Editorial-Staff"]}

            def tenant(self, claims):
                return claims["org_id"]

            def roles(self, claims):
                return ["reviewer" if r == "Editorial-Staff" else r for r in claims["roles"]]

            def profile(self, claims):
                return {"subject": claims["sub"], "email": "u@example.org",
                        "display_name": "A User"}

        user = self.principal(mode="oidc", bearer_token="header.payload.signature",
                              verifier=FakeVerifier())
        self.assertEqual(user.actor_id, "u-1")
        self.assertEqual(user.auth_mode, "oidc")
        self.assertEqual(sorted(user.roles), ["reviewer"])
        self.assertEqual(user.email, "u@example.org")

    def test_an_oidc_account_with_no_mapped_group_can_only_read(self):
        class FakeVerifier:
            class settings:  # noqa: N801
                require_tenant_claim = True

            def verify(self, token):
                return {"sub": "u-2", "org_id": TENANT, "roles": []}

            def tenant(self, claims):
                return claims["org_id"]

            def roles(self, claims):
                return []

            def profile(self, claims):
                return {"subject": claims["sub"], "email": None, "display_name": None}

        user = self.principal(mode="oidc", bearer_token="a.b.c", verifier=FakeVerifier())
        self.assertEqual(sorted(user.roles), ["viewer"])
        self.assertFalse(user.can("review.decide"))

    def test_bearer_header_parsing(self):
        self.assertEqual(bearer_from_header("Bearer abc.def.ghi"), "abc.def.ghi")
        self.assertIsNone(bearer_from_header("Basic dXNlcjpwYXNz"))
        self.assertIsNone(bearer_from_header(None))


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Signer.generate_key(Path(self.temp.name) / "export.key")

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self):
        return {"audit_id": "a1", "files": [{"name": "review.json", "sha256": "a" * 64}]}

    def test_ed25519_signature_round_trips_and_detects_tampering(self):
        signer = Signer("ed25519", key_path=self.key, key_id="k1")
        manifest = self.manifest()
        signature = signer.sign_manifest(manifest)
        self.assertEqual(signature.algorithm, "ed25519")
        self.assertTrue(verify_manifest(manifest, signature.value, algorithm="ed25519",
                                        public_key_pem=signature.public_key))
        self.assertFalse(verify_manifest({**manifest, "audit_id": "a2"}, signature.value,
                                         algorithm="ed25519",
                                         public_key_pem=signature.public_key))

    def test_the_signature_is_stable_across_key_ordering(self):
        signer = Signer("ed25519", key_path=self.key, key_id="k1")
        left = signer.sign_manifest({"a": 1, "b": 2})
        right = signer.sign_manifest({"b": 2, "a": 1})
        self.assertEqual(left.value, right.value)

    def test_hmac_requires_its_secret_for_both_operations(self):
        with self.assertRaises(SigningError):
            Signer("hmac-sha256").sign_manifest(self.manifest())
        signer = Signer("hmac-sha256", hmac_secret="shared", key_id="h1")
        signature = signer.sign_manifest(self.manifest())
        self.assertTrue(verify_manifest(self.manifest(), signature.value,
                                        algorithm="hmac-sha256", hmac_secret="shared"))
        self.assertFalse(verify_manifest(self.manifest(), signature.value,
                                         algorithm="hmac-sha256", hmac_secret="other"))
        base64.b64decode(signature.value)  # a well-formed MAC

    def test_an_ephemeral_key_is_labelled_as_such(self):
        signature = Signer("ed25519", key_path=None, key_id="k").sign_manifest(self.manifest())
        self.assertTrue(signature.key_id.endswith("-ephemeral"))

    def test_an_unsupported_algorithm_is_refused(self):
        with self.assertRaises(SigningError):
            Signer("md5")


class TelemetryTests(unittest.TestCase):
    def test_percentile_handles_empty_and_single_samples(self):
        self.assertEqual(percentile([], 0.95), 0.0)
        self.assertEqual(percentile([4.2], 0.95), 4.2)
        self.assertEqual(percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5), 5.0)

    def test_cost_is_derived_from_the_pricing_table(self):
        table = PricingTable()
        self.assertGreater(
            table.cost(provider="google", model="gemini-2.5-flash",
                       input_tokens=1_000_000, output_tokens=0),
            0.0,
        )
        self.assertEqual(table.cost(provider="unknown-provider", model="x", input_tokens=10), 0.0)

    def test_recorder_captures_stage_outcomes_and_provider_usage(self):
        recorder = TelemetryRecorder(pricing=PricingTable(), tenant_id=TENANT, audit_id="a1")
        with recorder.stage("parse"):
            pass
        with self.assertRaises(RuntimeError):
            with recorder.stage("enrich"):
                raise RuntimeError("provider outage")
        recorder.record_provider_calls([
            {"provider": "google", "model": "gemini-2.5-flash", "purpose": "extraction",
             "elapsed_seconds": 2.0, "status": "succeeded", "attempts": [1],
             "usage": {"prompt_token_count": 100_000, "candidates_token_count": 10_000}},
        ])
        outcomes = {event.stage: event.outcome for event in recorder.events}
        self.assertEqual(outcomes["parse"], "succeeded")
        self.assertEqual(outcomes["enrich"], "failed")
        self.assertGreater(recorder.totals()["cost_usd"], 0.0)

    def test_telemetry_failure_never_breaks_an_audit(self):
        class BrokenStore:
            def record_telemetry(self, *_args, **_kwargs):
                raise RuntimeError("the database is unreachable")

        recorder = TelemetryRecorder(BrokenStore(), tenant_id=TENANT, audit_id="a1")
        recorder.record_stage("parse", 1.0)
        self.assertEqual(recorder.flush(), 0)

    def test_slo_report_marks_a_missed_objective(self):
        class Settings:
            SLO_WINDOW_HOURS = 168
            SLO_AUDIT_P95_SECONDS = 60.0
            SLO_QUEUE_WAIT_P95_SECONDS = 60.0
            SLO_AUDIT_SUCCESS_RATIO = 0.98
            SLO_RESOLUTION_RATIO = 0.85
            SLO_SYSTEM_ISSUE_RATIO = 0.05
            SLO_COST_PER_AUDIT_USD = 1.5

        report = evaluate_slos(
            Settings,
            jobs=[{"state": "succeeded", "run_seconds": 900, "queue_wait_seconds": 2,
                   "summary": {"resolved_references": 5, "total_references": 20,
                               "system_issues": 0, "findings": 10}}],
            telemetry=[{"stage": "parse", "duration_ms": 1000, "cost_usd": 0.1}],
            queue={"queued": 0, "running": 0, "dead_letter": 0},
        ).as_dict()
        objectives = {item["key"]: item for item in report["objectives"]}
        self.assertFalse(objectives["audit_latency_p95"]["met"])
        self.assertFalse(objectives["reference_resolution_ratio"]["met"])
        self.assertFalse(report["met"])

    def test_an_empty_window_does_not_report_a_breach(self):
        class Settings:
            SLO_WINDOW_HOURS = 168
            SLO_AUDIT_P95_SECONDS = 900.0
            SLO_QUEUE_WAIT_P95_SECONDS = 60.0
            SLO_AUDIT_SUCCESS_RATIO = 0.98
            SLO_RESOLUTION_RATIO = 0.85
            SLO_SYSTEM_ISSUE_RATIO = 0.05
            SLO_COST_PER_AUDIT_USD = 1.5

        report = evaluate_slos(Settings, jobs=[], telemetry=[], queue={}).as_dict()
        self.assertTrue(report["met"])
        self.assertTrue(all(item["sample_size"] == 0 for item in report["objectives"]))


class QueueSettingsTests(unittest.TestCase):
    def test_backoff_grows_and_is_capped(self):
        settings = QueueSettings(backoff_seconds=10, backoff_ceiling_seconds=100)
        self.assertLess(settings.backoff_for(1), settings.backoff_for(3))
        self.assertLessEqual(settings.backoff_for(50), 130.0)


if __name__ == "__main__":
    unittest.main()
