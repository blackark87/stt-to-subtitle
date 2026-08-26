import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.artifact_retention import (
    ArtifactReference,
    audit_artifacts,
    cleanup_orphan_artifacts,
)


class ArtifactRetentionTests(unittest.TestCase):
    def test_audit_protects_references_and_reports_missing_artifacts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "jobs"
            referenced = root / "job-1" / "transcript.json"
            orphan = root / "deleted-job" / "translation.json"
            referenced.parent.mkdir(parents=True)
            orphan.parent.mkdir(parents=True)
            referenced.write_text("referenced", encoding="utf-8")
            orphan.write_text("orphan", encoding="utf-8")
            now = 20 * 24 * 60 * 60
            old = now - 10 * 24 * 60 * 60
            os.utime(referenced, (old, old))
            os.utime(orphan, (old, old))

            audit = audit_artifacts(
                root,
                [
                    ArtifactReference(
                        path=str(referenced),
                        kind="transcript_revision",
                        record_id="revision-1",
                    ),
                    ArtifactReference(
                        path=str(root / "job-1" / "missing.wav"),
                        kind="audio_revision",
                        record_id="revision-2",
                    ),
                    ArtifactReference(
                        path="job-2/planned.json",
                        kind="translation_generation",
                        record_id="generation-1",
                        expected=False,
                    ),
                    ArtifactReference(
                        path=str(Path(directory) / "published.srt"),
                        kind="job.srt_path",
                        record_id="job-1",
                    ),
                ],
                minimum_age_days=7,
                now=now,
            )

            self.assertEqual(audit.total_files, 2)
            self.assertEqual(audit.referenced_files, 1)
            self.assertEqual(audit.external_references, 1)
            self.assertEqual(
                [item.relative_path for item in audit.missing_references],
                ["job-1/missing.wav"],
            )
            self.assertEqual(
                [item.relative_path for item in audit.orphan_files],
                ["deleted-job/translation.json"],
            )
            self.assertTrue(audit.orphan_files[0].cleanup_eligible)

    def test_cleanup_removes_only_eligible_unchanged_orphans(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "jobs"
            root.mkdir()
            old_orphan = root / "old.json"
            recent_orphan = root / "recent.json"
            referenced = root / "referenced.json"
            old_orphan.write_bytes(b"old")
            recent_orphan.write_bytes(b"recent")
            referenced.write_bytes(b"referenced")
            now = 20 * 24 * 60 * 60
            old = now - 10 * 24 * 60 * 60
            os.utime(old_orphan, (old, old))
            os.utime(referenced, (old, old))

            audit = audit_artifacts(
                root,
                [
                    ArtifactReference(
                        path=str(referenced),
                        kind="job.transcript_path",
                        record_id="job-1",
                    )
                ],
                minimum_age_days=7,
                now=now,
            )
            cleanup = cleanup_orphan_artifacts(audit)

            self.assertEqual(cleanup.removed_files, 1)
            self.assertEqual(cleanup.removed_bytes, 3)
            self.assertFalse(old_orphan.exists())
            self.assertTrue(recent_orphan.exists())
            self.assertTrue(referenced.exists())

    def test_cleanup_skips_file_changed_after_audit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "jobs"
            root.mkdir()
            orphan = root / "orphan.json"
            orphan.write_text("old", encoding="utf-8")
            now = 20 * 24 * 60 * 60
            old = now - 10 * 24 * 60 * 60
            os.utime(orphan, (old, old))
            audit = audit_artifacts(
                root,
                [],
                minimum_age_days=7,
                now=now,
            )
            orphan.write_text("changed", encoding="utf-8")

            cleanup = cleanup_orphan_artifacts(audit)

            self.assertEqual(cleanup.removed_files, 0)
            self.assertTrue(orphan.exists())

    def test_symlinks_are_never_audited_or_removed(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "jobs"
            root.mkdir()
            target = base / "external.json"
            target.write_text("external", encoding="utf-8")
            link = root / "external-link.json"
            link.symlink_to(target)

            audit = audit_artifacts(
                root,
                [],
                minimum_age_days=7,
            )
            cleanup = cleanup_orphan_artifacts(audit)

            self.assertEqual(audit.total_files, 0)
            self.assertEqual(audit.skipped_symlinks, 1)
            self.assertEqual(cleanup.removed_files, 0)
            self.assertTrue(link.is_symlink())
            self.assertTrue(target.is_file())

    def test_rejects_zero_day_cleanup_window(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "at least 1 day"):
                audit_artifacts(
                    Path(directory),
                    [],
                    minimum_age_days=0,
                )


if __name__ == "__main__":
    unittest.main()
