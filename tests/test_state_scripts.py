from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("gpg"), "GnuPG is required for encrypted backups")
class EncryptedStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="ot-state-test-")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.release = self.root / "release"
        self.release.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake_gh = self.bin / "gh"
        fake_gh.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import shutil
            import sys

            args = sys.argv[1:]
            release = Path(os.environ["FAKE_RELEASE_DIR"])
            marker = release / ".exists"
            if args[0] == "api":
                endpoint = next(arg for arg in args if arg.startswith("repos/"))
                if "/releases/tags/" in endpoint:
                    if os.environ.get("FAKE_LOOKUP_ERROR"):
                        print("gh: Service Unavailable (HTTP 503)", file=sys.stderr)
                        sys.exit(1)
                    if not marker.exists():
                        print("gh: Not Found (HTTP 404)", file=sys.stderr)
                        sys.exit(1)
                    print(1)
                elif "/releases/1/assets" in endpoint:
                    for index, asset in enumerate(sorted(release.iterdir()), 1):
                        if not asset.name.startswith("."):
                            print(f"{index}\\t{asset.name}")
                else:
                    raise RuntimeError(f"unexpected API endpoint: {endpoint}")
            elif args[:2] == ["release", "create"]:
                marker.touch()
            elif args[:2] == ["release", "upload"]:
                for arg in args[3:]:
                    if arg.startswith("--"):
                        break
                    source = Path(arg)
                    shutil.copyfile(source, release / source.name)
            elif args[:2] == ["release", "view"]:
                for asset in sorted(release.iterdir()):
                    if not asset.name.startswith("."):
                        print(asset.name)
            elif args[:2] == ["release", "download"]:
                name = args[args.index("--pattern") + 1]
                destination = Path(args[args.index("--dir") + 1]) / name
                shutil.copyfile(release / name, destination)
            else:
                raise RuntimeError(f"unexpected gh command: {args}")
            """))
        fake_gh.chmod(0o700)
        self.source = self.root / "source.sqlite3"
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE notes (body TEXT)")
            db.execute("INSERT INTO notes VALUES (?)", ("private state fixture",))
        self.destination = self.root / "restored.sqlite3"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "example/tracker",
            "RUNNER_TEMP": str(self.root),
            "FAKE_RELEASE_DIR": str(self.release),
            "STATE_ENCRYPTION_KEY": secrets.token_urlsafe(32),
            "STATE_BACKUP_RETENTION_DAYS": "7",
        }
        self.env.pop("FAKE_LOOKUP_ERROR", None)

    def run_script(self, script: str, **overrides: str) -> subprocess.CompletedProcess:
        database = self.source if script == "persist_state.sh" else self.destination
        return subprocess.run(
            ["bash", str(ROOT / "scripts" / script)],
            env={**self.env, "STATE_DB": str(database), **overrides},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def persist(self) -> None:
        result = self.run_script("persist_state.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_restored(self) -> None:
        result = self.run_script("restore_state.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with closing(sqlite3.connect(self.destination)) as db:
            self.assertEqual(db.execute("SELECT body FROM notes").fetchone(),
                             ("private state fixture",))

    def test_uploads_only_encrypted_assets_and_restores_database(self) -> None:
        self.persist()
        assets = [asset for asset in self.release.iterdir() if asset.name != ".exists"]
        self.assertEqual(len(assets), 4)
        for asset in assets:
            self.assertTrue(asset.name.endswith((".gz.gpg", ".gz.gpg.sha256")))
            self.assertNotIn(b"private state fixture", asset.read_bytes())
        self.assert_restored()

    def test_corrupt_latest_backup_falls_back_to_daily_backup(self) -> None:
        self.persist()
        (self.release / "ot-tracker.sqlite3.gz.gpg").write_bytes(b"corrupt backup")
        self.assert_restored()

    def test_wrong_key_preserves_existing_database(self) -> None:
        self.persist()
        self.destination.write_bytes(b"preserve existing state")
        result = self.run_script("restore_state.sh", STATE_ENCRYPTION_KEY=secrets.token_urlsafe(32))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.destination.read_bytes(), b"preserve existing state")

    def test_missing_key_cannot_upload_plaintext(self) -> None:
        result = self.run_script("persist_state.sh", STATE_ENCRYPTION_KEY="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.release.iterdir()), [])

    def test_missing_release_starts_fresh_but_service_error_stops(self) -> None:
        result = self.run_script("restore_state.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.destination.exists())
        result = self.run_script("restore_state.sh", FAKE_LOOKUP_ERROR="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
