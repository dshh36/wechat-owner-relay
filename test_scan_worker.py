import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scan_worker


def synthetic_worker(connection):
    # Real spawned process, but no screen access, OCR, queue writes or notifications.
    while connection.recv() == "scan":
        connection.send({"status": "scan_ok", "pid": os.getpid()})


def stuck_worker(connection):
    connection.recv()
    time.sleep(30)


def exited_worker(connection):
    connection.close()


class ScanWorkerTests(unittest.TestCase):
    def test_reuses_worker_then_recycles_and_reaps_it(self):
        worker = scan_worker.ScanWorker(max_scans=2)
        try:
            with patch.object(scan_worker, "worker_main", synthetic_worker):
                first = worker.scan()
                old_process = worker.process
                second = worker.scan()
                self.assertEqual(first["pid"], second["pid"])
                self.assertEqual(worker.scans, 2)
                third = worker.scan()
                self.assertIsNot(worker.process, old_process)
                self.assertEqual(third["status"], "scan_ok")
                self.assertEqual(worker.scans, 1)
        finally:
            worker.close()
        self.assertIsNone(worker.process)
        self.assertIsNone(worker.connection)

    def test_timeout_terminates_worker_before_returning(self):
        worker = scan_worker.ScanWorker()
        try:
            with patch.object(scan_worker, "worker_main", stuck_worker):
                self.assertEqual(worker.scan(timeout=0.2), {
                    "status": "paused", "reason": "Local scan timed out"})
            self.assertIsNone(worker.process)
            self.assertIsNone(worker.connection)
        finally:
            worker.close()

    def test_crash_does_not_return_stale_success(self):
        worker = scan_worker.ScanWorker()
        try:
            with patch.object(scan_worker, "worker_main", exited_worker):
                self.assertEqual(worker.scan(timeout=5)["status"], "paused")
            self.assertIsNone(worker.process)
        finally:
            worker.close()

    def test_stop_interrupts_stuck_scan_without_waiting_for_timeout(self):
        worker = scan_worker.ScanWorker()
        checks = iter([False, False, True])
        try:
            with patch.object(scan_worker, "worker_main", stuck_worker):
                started = time.monotonic()
                self.assertEqual(worker.scan(should_stop=lambda: next(checks)),
                                 {"status": "stopped"})
                self.assertLess(time.monotonic() - started, 5)
            self.assertIsNone(worker.process)
        finally:
            worker.close()

    def test_stop_does_not_start_worker(self):
        worker = scan_worker.ScanWorker()
        self.assertEqual(worker.scan(should_stop=lambda: True), {"status": "stopped"})
        self.assertIsNone(worker.process)

    def test_worker_reports_failures_and_recovery_flag_without_dispatch(self):
        import watcher
        connection = Mock()
        connection.recv.side_effect = ["scan", "stop"]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(watcher, "STATE", Path(directory) / "state.json"), \
                patch.object(watcher, "STATUS", Path(directory) / "status.json"), \
                patch.object(watcher, "main", side_effect=RuntimeError(watcher.WINDOW_UNAVAILABLE)), \
                patch.dict(os.environ):
            scan_worker.worker_main(connection)
            result = connection.send.call_args.args[0]
            self.assertEqual(result["status"], "paused")
            self.assertTrue(watcher.load(watcher.STATE, {})["rebase_on_next_scan"])
            self.assertIn("cpu_seconds", result["scan_metrics"])
            self.assertEqual(os.environ["OPENBLAS_NUM_THREADS"], "1")

    def test_ocr_model_is_reused_and_single_threaded(self):
        import watcher
        import cv2
        import numpy as np
        import rapidocr_onnxruntime
        engine = Mock(return_value=([], None))
        with patch.object(watcher, "_OCR_ENGINE", None), \
                patch.object(cv2, "setNumThreads") as cv_threads, \
                patch.object(rapidocr_onnxruntime, "RapidOCR", return_value=engine) as factory:
            for _ in range(3):
                self.assertEqual(watcher.read_ocr(np.zeros((20, 20, 3), dtype=np.uint8)), [])
            factory.assert_called_once_with(intra_op_num_threads=1, inter_op_num_threads=1)
            cv_threads.assert_called_once_with(1)
            self.assertEqual(engine.call_count, 3)


if __name__ == "__main__":
    unittest.main()
