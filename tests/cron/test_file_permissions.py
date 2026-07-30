"""Tests for file permissions hardening on sensitive files."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestCronFilePermissions(unittest.TestCase):
    """Verify cron files get secure permissions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cron_dir = Path(self.tmpdir) / "cron"
        self.output_dir = self.cron_dir / "output"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_execution_store_permissions_repair_private_paths_only(self):
        import cron.executions as executions

        cron_dir = Path(self.tmpdir) / "cron"
        executions_db = cron_dir / "executions.db"
        cron_dir.mkdir(parents=True)
        executions_db.write_bytes(b"sqlite contents")
        sidecar_contents = {
            "executions.db-wal": b"wal contents",
            "executions.db-shm": b"shm contents",
        }
        for name, contents in sidecar_contents.items():
            (cron_dir / name).write_bytes(contents)
        for name in (".tick.lock", ".jobs.lock"):
            (cron_dir / name).write_bytes(b"lock contents")
        unrelated = cron_dir / "unrelated.txt"
        unrelated.write_bytes(b"leave me alone")
        outside = Path(self.tmpdir) / "outside.txt"
        outside.write_bytes(b"outside")
        linked_lock = cron_dir / ".tick.lock-link"
        linked_lock.symlink_to(outside)

        cron_dir.chmod(0o775)
        executions_db.chmod(0o664)
        for name in sidecar_contents:
            (cron_dir / name).chmod(0o664)
        for name in (".tick.lock", ".jobs.lock"):
            (cron_dir / name).chmod(0o664)
        unrelated.chmod(0o664)
        outside.chmod(0o664)

        with patch.object(executions, "EXECUTIONS_FILE", executions_db):
            connection = executions._connect()
            connection.close()

        self.assertEqual(stat.S_IMODE(cron_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(executions_db.stat().st_mode), 0o600)
        for name, contents in sidecar_contents.items():
            path = cron_dir / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_bytes(), contents)
        for name in (".tick.lock", ".jobs.lock"):
            self.assertEqual(stat.S_IMODE((cron_dir / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(unrelated.stat().st_mode), 0o664)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o664)
        self.assertTrue(linked_lock.is_symlink())

    def test_new_execution_database_is_created_owner_only(self):
        import cron.executions as executions

        executions_db = Path(self.tmpdir) / "cron" / "executions.db"
        previous_umask = os.umask(0)
        try:
            with patch.object(executions, "EXECUTIONS_FILE", executions_db):
                executions.create_execution("new-store", source="builtin")
        finally:
            os.umask(previous_umask)

        self.assertEqual(stat.S_IMODE(executions_db.stat().st_mode), 0o600)

    def test_execution_store_permissions_reject_symlinked_lock(self):
        import cron.executions as executions

        cron_dir = Path(self.tmpdir) / "cron"
        cron_dir.mkdir(parents=True)
        executions_db = cron_dir / "executions.db"
        executions_db.write_bytes(b"sqlite contents")
        outside = Path(self.tmpdir) / "outside.lock"
        outside.write_bytes(b"outside lock")
        tick_lock = cron_dir / ".tick.lock"
        tick_lock.symlink_to(outside)
        jobs_lock = cron_dir / ".jobs.lock"
        jobs_lock.write_bytes(b"jobs lock")
        cron_dir.chmod(0o775)
        executions_db.chmod(0o664)
        outside.chmod(0o664)
        jobs_lock.chmod(0o664)

        with self.assertRaises(OSError):
            executions._normalize_execution_store_permissions(executions_db)

        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o664)
        self.assertTrue(tick_lock.is_symlink())

    def _assert_execution_store_rejects_exact_sidecar_symlink(self, sidecar_name):
        import cron.executions as executions

        cron_dir = Path(self.tmpdir) / "cron"
        cron_dir.mkdir(parents=True)
        executions_db = cron_dir / "executions.db"
        executions_db.write_bytes(b"sqlite contents")
        outside = Path(self.tmpdir) / f"outside-{sidecar_name}"
        outside.write_bytes(b"outside sidecar")
        outside.chmod(0o664)
        sidecar = cron_dir / sidecar_name
        sidecar.symlink_to(outside)

        with self.assertRaises(OSError):
            executions._normalize_execution_store_permissions(executions_db)

        self.assertTrue(sidecar.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside sidecar")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o664)

    def test_execution_store_permissions_reject_exact_wal_symlink(self):
        self._assert_execution_store_rejects_exact_sidecar_symlink(
            "executions.db-wal"
        )

    def test_execution_store_permissions_reject_exact_shm_symlink(self):
        self._assert_execution_store_rejects_exact_sidecar_symlink(
            "executions.db-shm"
        )

    def test_execution_store_rejects_ancestor_symlink(self):
        import cron.executions as executions

        real_home = Path(self.tmpdir) / "real-home"
        real_cron_dir = real_home / "cron"
        real_cron_dir.mkdir(parents=True)
        real_cron_dir.chmod(0o775)
        executions_db = real_cron_dir / "executions.db"
        executions_db.write_bytes(b"sqlite contents")
        executions_db.chmod(0o664)
        linked_home = Path(self.tmpdir) / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        linked_db = linked_home / "cron" / "executions.db"

        with (
            patch.object(executions, "EXECUTIONS_FILE", linked_db),
            self.assertRaises(OSError),
        ):
            connection = executions._connect()
            connection.close()

        self.assertTrue(linked_home.is_symlink())
        self.assertEqual(stat.S_IMODE(real_cron_dir.stat().st_mode), 0o775)
        self.assertEqual(stat.S_IMODE(executions_db.stat().st_mode), 0o664)

    def test_execution_store_fails_closed_when_chmod_fails(self):
        import cron.executions as executions

        cron_dir = Path(self.tmpdir) / "cron"
        cron_dir.mkdir()
        executions_db = cron_dir / "executions.db"
        executions_db.write_bytes(b"sqlite contents")

        with (
            patch.object(
                executions.os,
                "chmod",
                side_effect=PermissionError("chmod denied"),
            ),
            patch.object(
                executions.os,
                "fchmod",
                side_effect=PermissionError("fchmod denied"),
            ),
            self.assertRaises(OSError),
        ):
            executions._normalize_execution_store_permissions(executions_db)

    @patch("cron.jobs.CRON_DIR")
    @patch("cron.jobs.OUTPUT_DIR")
    @patch("cron.jobs.JOBS_FILE")
    def test_ensure_dirs_sets_0700(self, mock_jobs_file, mock_output, mock_cron):
        mock_cron.__class__ = Path
        # Use real paths
        cron_dir = Path(self.tmpdir) / "cron"
        output_dir = cron_dir / "output"

        with patch("cron.jobs.CRON_DIR", cron_dir), \
             patch("cron.jobs.OUTPUT_DIR", output_dir):
            from cron.jobs import ensure_dirs
            ensure_dirs()

            cron_mode = stat.S_IMODE(os.stat(cron_dir).st_mode)
            output_mode = stat.S_IMODE(os.stat(output_dir).st_mode)
            self.assertEqual(cron_mode, 0o700)
            self.assertEqual(output_mode, 0o700)

    @patch("cron.jobs.CRON_DIR")
    @patch("cron.jobs.OUTPUT_DIR")
    @patch("cron.jobs.JOBS_FILE")
    def test_save_jobs_sets_0600(self, mock_jobs_file, mock_output, mock_cron):
        cron_dir = Path(self.tmpdir) / "cron"
        output_dir = cron_dir / "output"
        jobs_file = cron_dir / "jobs.json"

        with patch("cron.jobs.CRON_DIR", cron_dir), \
             patch("cron.jobs.OUTPUT_DIR", output_dir), \
             patch("cron.jobs.JOBS_FILE", jobs_file):
            from cron.jobs import save_jobs
            save_jobs([{"id": "test", "prompt": "hello"}])

            file_mode = stat.S_IMODE(os.stat(jobs_file).st_mode)
            self.assertEqual(file_mode, 0o600)

    def test_save_job_output_sets_0600(self):
        output_dir = Path(self.tmpdir) / "output"
        with patch("cron.jobs.OUTPUT_DIR", output_dir), \
             patch("cron.jobs.CRON_DIR", Path(self.tmpdir)), \
             patch("cron.jobs.ensure_dirs"):
            output_dir.mkdir(parents=True, exist_ok=True)
            from cron.jobs import save_job_output
            output_file = save_job_output("test-job", "test output content")

            file_mode = stat.S_IMODE(os.stat(output_file).st_mode)
            self.assertEqual(file_mode, 0o600)

            # Job output dir should also be 0700
            job_dir = output_dir / "test-job"
            dir_mode = stat.S_IMODE(os.stat(job_dir).st_mode)
            self.assertEqual(dir_mode, 0o700)

    def test_jobs_lock_is_created_0600_with_permissive_umask(self):
        import cron.jobs as jobs

        cron_dir = Path(self.tmpdir) / "cron"
        output_dir = cron_dir / "output"
        with (
            patch.object(jobs, "CRON_DIR", cron_dir),
            patch.object(jobs, "JOBS_FILE", cron_dir / "jobs.json"),
            patch.object(jobs, "OUTPUT_DIR", output_dir),
        ):
            previous_umask = os.umask(0)
            try:
                with jobs._jobs_lock():
                    pass
            finally:
                os.umask(previous_umask)

        self.assertEqual(
            stat.S_IMODE((cron_dir / ".jobs.lock").stat().st_mode), 0o600
        )

    def test_tick_lock_is_created_0600_with_permissive_umask(self):
        import cron.scheduler as scheduler

        cron_dir = Path(self.tmpdir) / "cron"
        lock_file = cron_dir / ".tick.lock"
        with patch.object(
            scheduler, "_get_lock_paths", return_value=(cron_dir, lock_file)
        ):
            previous_umask = os.umask(0)
            try:
                self.assertEqual(scheduler.tick(verbose=False), 0)
            finally:
                os.umask(previous_umask)

        self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o600)

    def test_jobs_lock_rejects_exact_target_symlink_without_mutation(self):
        import cron.jobs as jobs
        from cron.executions import _PrivateStatePermissionError

        cron_dir = Path(self.tmpdir) / "cron"
        cron_dir.mkdir()
        outside = Path(self.tmpdir) / "outside-jobs-lock"
        outside.write_bytes(b"outside jobs lock")
        outside.chmod(0o664)
        jobs_lock = cron_dir / ".jobs.lock"
        jobs_lock.symlink_to(outside)

        with (
            patch.object(jobs, "CRON_DIR", cron_dir),
            patch.object(jobs, "JOBS_FILE", cron_dir / "jobs.json"),
            patch.object(jobs, "OUTPUT_DIR", cron_dir / "output"),
            self.assertRaises(_PrivateStatePermissionError),
        ):
            with jobs._jobs_lock():
                self.fail("unsafe jobs lock must not enter the critical section")

        self.assertTrue(jobs_lock.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside jobs lock")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o664)

    def test_jobs_lock_rejects_ancestor_symlink_without_mutation(self):
        import cron.jobs as jobs
        from cron.executions import _PrivateStatePermissionError

        real_cron_dir = Path(self.tmpdir) / "real-cron"
        real_cron_dir.mkdir()
        real_cron_dir.chmod(0o775)
        marker = real_cron_dir / "marker"
        marker.write_bytes(b"outside cron state")
        marker.chmod(0o664)
        linked_cron_dir = Path(self.tmpdir) / "linked-cron"
        linked_cron_dir.symlink_to(real_cron_dir, target_is_directory=True)

        with (
            patch.object(jobs, "CRON_DIR", linked_cron_dir),
            patch.object(jobs, "JOBS_FILE", linked_cron_dir / "jobs.json"),
            patch.object(jobs, "OUTPUT_DIR", linked_cron_dir / "output"),
            self.assertRaises(_PrivateStatePermissionError),
        ):
            with jobs._jobs_lock():
                self.fail("unsafe jobs lock must not enter the critical section")

        self.assertTrue(linked_cron_dir.is_symlink())
        self.assertEqual(stat.S_IMODE(real_cron_dir.stat().st_mode), 0o775)
        self.assertEqual(marker.read_bytes(), b"outside cron state")
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o664)
        self.assertFalse((real_cron_dir / "output").exists())

    def test_tick_lock_rejects_exact_target_symlink_without_mutation(self):
        import cron.scheduler as scheduler
        from cron.executions import _PrivateStatePermissionError

        cron_dir = Path(self.tmpdir) / "cron"
        cron_dir.mkdir()
        outside = Path(self.tmpdir) / "outside-tick-lock"
        outside.write_bytes(b"outside tick lock")
        outside.chmod(0o664)
        tick_lock = cron_dir / ".tick.lock"
        tick_lock.symlink_to(outside)

        with (
            patch.object(
                scheduler,
                "_get_lock_paths",
                return_value=(cron_dir, tick_lock),
            ),
            self.assertRaises(_PrivateStatePermissionError),
        ):
            scheduler.tick(verbose=False)

        self.assertTrue(tick_lock.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside tick lock")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o664)

    def test_tick_lock_rejects_ancestor_symlink_without_mutation(self):
        import cron.scheduler as scheduler
        from cron.executions import _PrivateStatePermissionError

        real_cron_dir = Path(self.tmpdir) / "real-cron"
        real_cron_dir.mkdir()
        real_cron_dir.chmod(0o775)
        marker = real_cron_dir / "marker"
        marker.write_bytes(b"outside cron state")
        marker.chmod(0o664)
        linked_cron_dir = Path(self.tmpdir) / "linked-cron"
        linked_cron_dir.symlink_to(real_cron_dir, target_is_directory=True)
        tick_lock = linked_cron_dir / ".tick.lock"

        with (
            patch.object(
                scheduler,
                "_get_lock_paths",
                return_value=(linked_cron_dir, tick_lock),
            ),
            self.assertRaises(_PrivateStatePermissionError),
        ):
            scheduler.tick(verbose=False)

        self.assertTrue(linked_cron_dir.is_symlink())
        self.assertEqual(stat.S_IMODE(real_cron_dir.stat().st_mode), 0o775)
        self.assertEqual(marker.read_bytes(), b"outside cron state")
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o664)
        self.assertFalse((real_cron_dir / ".tick.lock").exists())

    def test_tick_lock_contention_still_returns_zero(self):
        import cron.scheduler as scheduler

        if scheduler.fcntl is None:
            self.skipTest("fcntl lock contention is Unix-only")

        cron_dir = Path(self.tmpdir) / "cron"
        cron_dir.mkdir()
        tick_lock = cron_dir / ".tick.lock"
        holder_fd = os.open(tick_lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            scheduler.fcntl.flock(holder_fd, scheduler.fcntl.LOCK_EX)
            with patch.object(
                scheduler,
                "_get_lock_paths",
                return_value=(cron_dir, tick_lock),
            ):
                self.assertEqual(scheduler.tick(verbose=False), 0)
        finally:
            scheduler.fcntl.flock(holder_fd, scheduler.fcntl.LOCK_UN)
            os.close(holder_fd)


class TestConfigFilePermissions(unittest.TestCase):
    """Verify config files get secure permissions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_config_sets_0600(self):
        config_path = Path(self.tmpdir) / "config.yaml"
        with patch("hermes_cli.config.get_config_path", return_value=config_path), \
             patch("hermes_cli.config.ensure_hermes_home"):
            from hermes_cli.config import save_config
            save_config({"model": "test/model"})

            file_mode = stat.S_IMODE(os.stat(config_path).st_mode)
            self.assertEqual(file_mode, 0o600)

    def test_save_env_value_sets_0600(self):
        env_path = Path(self.tmpdir) / ".env"
        with patch("hermes_cli.config.get_env_path", return_value=env_path), \
             patch("hermes_cli.config.ensure_hermes_home"):
            from hermes_cli.config import save_env_value
            save_env_value("TEST_KEY", "test_value")

            file_mode = stat.S_IMODE(os.stat(env_path).st_mode)
            self.assertEqual(file_mode, 0o600)

    def test_ensure_hermes_home_sets_0700(self):
        home = Path(self.tmpdir) / ".hermes"
        with patch("hermes_cli.config.get_hermes_home", return_value=home):
            from hermes_cli.config import ensure_hermes_home
            ensure_hermes_home()

            home_mode = stat.S_IMODE(os.stat(home).st_mode)
            self.assertEqual(home_mode, 0o700)

            for subdir in ("cron", "sessions", "logs", "memories"):
                subdir_mode = stat.S_IMODE(os.stat(home / subdir).st_mode)
                self.assertEqual(subdir_mode, 0o700, f"{subdir} should be 0700")


class TestSecureHelpers(unittest.TestCase):
    """Test the _secure_file and _secure_dir helpers."""

    def test_secure_file_nonexistent_no_error(self):
        from cron.jobs import _secure_file
        _secure_file(Path("/nonexistent/path/file.json"))  # Should not raise


if __name__ == "__main__":
    unittest.main()
