"""Regression coverage for concurrent Windows object-path publication."""
import hashlib
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from src.trust.object_store import LocalObjectStore, object_key_for_digest
from src.trust.ports import TrustIntegrityError
from tests.trust_fixtures import make_pdf


class LocalObjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = make_pdf(self.root)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.store = LocalObjectStore(self.root / "objects", namespace="tenant")

    def put(self, store=None):
        return (store or self.store).put_file(self.path, content_sha256=self.digest,
                                             media_type="application/pdf")

    def test_publication_uses_stable_key_when_filename_resolution_changes(self):
        # Emulate a resolver returning an alternate spelling during publication.
        # The old implementation compared this to root and rejected valid input.
        original_resolve = Path.resolve

        def alternate_filename(path, *args, **kwargs):
            if path.name == self.digest:
                return self.root / "alternate-namespace" / self.digest
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", alternate_filename):
            first, second = self.put(), self.put()
        self.assertEqual(first, second)
        self.assertEqual(first.key, object_key_for_digest(self.digest, "tenant"))
        self.assertEqual((self.store.root / first.key).read_bytes(), self.path.read_bytes())

    def test_concurrent_instances_publish_one_complete_object(self):
        # Multiple adapters prevent an instance-local lock from masking the bug.
        barrier = Barrier(8)

        def write(_):
            store = LocalObjectStore(self.store.root, namespace="tenant")
            barrier.wait(timeout=10)
            return self.put(store)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(write, range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        files = [p for p in self.store.root.rglob("*") if p.is_file()]
        self.assertEqual(files, [self.store.root / results[0].key])
        self.assertEqual(files[0].read_bytes(), self.path.read_bytes())

    def test_existing_redirected_directory_is_rejected_before_writing(self):
        candidate = self.store.root / "tenant"
        original_lstat = Path.lstat

        def junction(path, *args, **kwargs):
            if path == candidate:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", junction), self.assertRaises(TrustIntegrityError):
            self.put()
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_redirected_directory_created_by_competing_writer_is_rejected(self):
        candidate = self.store.root / "tenant"
        original_lstat, original_mkdir = Path.lstat, Path.mkdir
        redirected = False

        def raced_mkdir(path, *args, **kwargs):
            nonlocal redirected
            if path == candidate:
                redirected = True
                raise FileExistsError("another writer created a junction")
            return original_mkdir(path, *args, **kwargs)

        def raced_lstat(path, *args, **kwargs):
            if path == candidate and redirected:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "mkdir", raced_mkdir), patch.object(Path, "lstat", raced_lstat):
            with self.assertRaises(TrustIntegrityError):
                self.put()
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_symbolic_link_object_is_rejected_without_replacement(self):
        result = self.put()
        destination = self.store.root / result.key
        original_lstat = Path.lstat

        def symlink(path, *args, **kwargs):
            if path == destination:
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", symlink), self.assertRaises(TrustIntegrityError):
            self.put()
        self.assertEqual(destination.read_bytes(), self.path.read_bytes())

    def test_replaced_root_and_non_directory_components_are_rejected(self):
        original = self.store.root
        original.rename(self.root / "old_objects")
        original.mkdir()
        with self.assertRaises(TrustIntegrityError):
            self.put()
        fresh = LocalObjectStore(original, namespace="tenant")
        (original / "tenant").write_text("not a directory", encoding="utf-8")
        with self.assertRaises(TrustIntegrityError):
            self.put(fresh)
        self.assertEqual((original / "tenant").read_text(encoding="utf-8"), "not a directory")
