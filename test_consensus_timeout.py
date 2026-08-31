"""Consensus-aware job timeouts + death-penalty propagation.

Regression suite for the 364 Main failure (submission a5d2d26a, 2026-08-31):
a 23-page / 19.5 MB set was enqueued with the flat 1h tier, ran K=3 per-sheet
consensus, hit RQ's death penalty on sheet 17 of 20, had the exception
swallowed by _call_sheet_api's catch-all, and was SIGKILL'd a minute later —
leaving the row stuck at 'processing' for the watchdog to reap.

Covers:
  1. _pick_timeout scales with the effective consensus N
  2. _call_sheet_api re-raises RQ's JobTimeoutException instead of
     logging it as a failed sheet
  3. the DD cutoff that forces single-read is 20 pages, not 30
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import Takeoff_DIRECT as td


_CONSENSUS_ENV = ("NIGHTSHIFT_PER_SHEET_CONSENSUS",
                  "NIGHTSHIFT_CONSENSUS_DD_MIN_PAGES")


class _ConsensusEnvCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _CONSENSUS_ENV}
        for k in _CONSENSUS_ENV:
            os.environ.pop(k, None)
        self._saved_pages = td._CONSENSUS_JOB_PAGE_COUNT

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        td._CONSENSUS_JOB_PAGE_COUNT = self._saved_pages


class TestDDCutoff(_ConsensusEnvCase):
    """Fix 3: mid-size sets stop paying for consensus they don't benefit
    from. 364 Main was 23 pages — under the old 30-page cutoff."""

    def test_default_cutoff_is_20_pages(self):
        self.assertEqual(config.CONSENSUS_DD_MIN_PAGES_DEFAULT, 20)

    def test_364main_page_count_forces_single_read(self):
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        self.assertEqual(config.effective_consensus_n(23), 1)

    def test_small_sets_still_get_consensus(self):
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        self.assertEqual(config.effective_consensus_n(19), 3)
        self.assertEqual(config.effective_consensus_n(8), 3)

    def test_cutoff_is_overridable(self):
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        os.environ["NIGHTSHIFT_CONSENSUS_DD_MIN_PAGES"] = "30"
        self.assertEqual(config.effective_consensus_n(23), 3)

    def test_off_by_default(self):
        self.assertEqual(config.effective_consensus_n(8), 1)

    def test_extraction_path_delegates_to_config(self):
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        td._CONSENSUS_JOB_PAGE_COUNT = 23
        self.assertEqual(td._effective_consensus_n(), 1)
        td._CONSENSUS_JOB_PAGE_COUNT = 8
        self.assertEqual(td._effective_consensus_n(), 3)


class TestTimeoutScaling(_ConsensusEnvCase):
    """Fix 1: the enqueue-time timeout knows what the extraction path is
    about to do."""

    def _pick(self, pages, mb):
        import web_app
        return web_app._pick_timeout(pages, mb * 1024 * 1024)

    def test_unscaled_when_consensus_off(self):
        self.assertEqual(self._pick(15, 40), 3600)
        self.assertEqual(self._pick(30, 120), 2 * 3600)

    def test_scaled_when_consensus_on(self):
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        # 15 pages is under the DD cutoff -> really does 3 reads/sheet
        self.assertEqual(self._pick(15, 40), 3 * 3600)

    def test_364main_would_have_survived(self):
        """The exact failing payload: 23 pages / 19.5 MB at K=3.

        Post-fix the DD cutoff drops it to single-read, so 1h is honest.
        Belt-and-braces: if the cutoff is raised back to 30 it still gets
        3h rather than the 1h that killed it.
        """
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        self.assertEqual(self._pick(23, 19.5), 3600)
        os.environ["NIGHTSHIFT_CONSENSUS_DD_MIN_PAGES"] = "30"
        self.assertEqual(self._pick(23, 19.5), 3 * 3600)

    def test_never_shrinks_and_respects_cap(self):
        os.environ["NIGHTSHIFT_PER_SHEET_CONSENSUS"] = "3"
        self.assertEqual(config.scale_timeout_for_consensus(3600, 8),
                         3 * 3600)
        # DD-scale base already at the cap: scaling must not shrink it
        self.assertEqual(config.scale_timeout_for_consensus(4 * 3600, 60),
                         4 * 3600)
        self.assertLessEqual(config.scale_timeout_for_consensus(2 * 3600, 8),
                             config.CONSENSUS_TIMEOUT_CAP_SECONDS)

    def test_bad_input_is_not_fatal(self):
        self.assertEqual(config.scale_timeout_for_consensus(3600, None), 3600)


class _FakeJobTimeout(Exception):
    """Stands in for rq.timeouts.JobTimeoutException by class name, so the
    test works whether or not rq is installed."""
    pass


_FakeJobTimeout.__name__ = "JobTimeoutException"


class TestDeathPenaltyPropagates(unittest.TestCase):
    """Fix 2: a blown deadline aborts the job instead of being logged as
    one more failed sheet."""

    def test_classifier_recognises_rq_timeout(self):
        self.assertTrue(td._is_control_flow_exc(_FakeJobTimeout("boom")))

    def test_classifier_ignores_ordinary_errors(self):
        self.assertFalse(td._is_control_flow_exc(ValueError("bad json")))
        self.assertFalse(td._is_control_flow_exc(ConnectionError("reset")))

    def test_call_sheet_api_reraises(self):
        class _Client:
            class messages:
                @staticmethod
                def stream(*a, **kw):
                    raise _FakeJobTimeout(
                        "Task exceeded maximum timeout value (3600 seconds)")

        with self.assertRaises(_FakeJobTimeout):
            td._call_sheet_api(_Client(), [], {}, label="sheet A301",
                               max_retries=1)

    def test_call_sheet_api_still_swallows_ordinary_errors(self):
        class _Client:
            class messages:
                @staticmethod
                def stream(*a, **kw):
                    raise ValueError("unparseable")

        self.assertIsNone(
            td._call_sheet_api(_Client(), [], {}, label="sheet A301",
                               max_retries=1))

    def test_describe_exc_gives_a_real_reason(self):
        import jobs
        msg = jobs._describe_exc(_FakeJobTimeout(
            "Task exceeded maximum timeout value (3600 seconds)"))
        self.assertIn("time limit", msg)
        self.assertNotIn("maximum timeout value", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
