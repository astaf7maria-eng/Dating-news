"""Period conversation and CLI: selection must precede artifacts."""

import json
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from dating_digest.__main__ import CONFIG_NAMES, read_json, validate_configs, write_json
from dating_digest.orchestration.period import choose_period


class ConfigAndWriteTests(unittest.TestCase):
    def test_invalid_cross_config_links_and_ranking_are_rejected(self):
        root = Path(__file__).resolve().parents[1]
        configs = {name: read_json(root / "config" / f"{name}.json") for name in CONFIG_NAMES}
        validate_configs(configs, root / "schemas")
        for mutate in (
            lambda c: c["companies"]["companies"][0]["source_ids"].append("missing"),
            lambda c: c["sources"]["sources"][0]["company_ids"].append("missing"),
            lambda c: c["companies"]["companies"].append(copy.deepcopy(c["companies"]["companies"][0])),
            lambda c: c["companies"]["companies"][0].update(enabled=False),
            lambda c: c["queries"]["company_profiles"]["always"]["thematic_sections"].append("missing"),
            lambda c: c["queries"]["discovery"]["source_seed_ids"].append("missing"),
            lambda c: c["editorial_policy"]["sections"].pop(),
            lambda c: c["golden_set"]["cases"][0]["company_ids"].append("missing"),
            lambda c: c["pipeline"]["ranking"]["weights"].update(business_relevance=0.5),
            lambda c: c["pipeline"]["ranking"]["weights"].update(unknown=0),
            lambda c: c["pipeline"]["ranking"]["weights"].update(business_relevance=True),
            lambda c: c["pipeline"]["ranking"].update(weights=[]),
            lambda c: c["editorial_policy"].update(max_news_total=11),
        ):
            with self.subTest(mutation=mutate):
                invalid = copy.deepcopy(configs)
                mutate(invalid)
                with self.assertRaises(ValueError):
                    validate_configs(invalid, root / "schemas")

    def test_failed_write_leaves_no_plan_and_retry_succeeds(self):
        def interrupted(value, output, **kwargs):
            output.write('{"partial":')
            raise OSError("disk full")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "search_plan.json"
            for target, options in (("json.dump", {"side_effect": interrupted}),
                                    ("os.fsync", {"side_effect": OSError("sync failed")}),
                                    ("os.link", {"side_effect": OSError("link failed")})):
                with self.subTest(target=target), patch(f"dating_digest.__main__.{target}", **options):
                    with self.assertRaises(OSError):
                        write_json(path, {"status": "planned"})
                self.assertEqual(list(path.parent.iterdir()), [])
            write_json(path, {"текст": "данные"})
            self.assertEqual(read_json(path), {"текст": "данные"})

    def test_concurrent_writer_cannot_be_overwritten(self):
        link = os.link
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "search_plan.json"

            def concurrent_publish(staging, destination):
                self.assertEqual(read_json(staging), {"status": "planned"})
                destination.write_text('{"winner": true}', encoding="utf-8")
                link(staging, destination)

            with patch("dating_digest.__main__.os.link", side_effect=concurrent_publish):
                with self.assertRaises(FileExistsError):
                    write_json(path, {"status": "planned"})
            self.assertEqual(read_json(path), {"winner": True})
            self.assertEqual(list(path.parent.iterdir()), [path])


class PeriodConversationTests(unittest.TestCase):
    as_of = datetime.fromisoformat("2026-11-15T12:00:00+03:00")

    def choose(self, answers):
        prompts, messages = [], []
        replies = iter(answers)

        def read(prompt):
            prompts.append(prompt)
            return next(replies)

        return choose_period(self.as_of, read, messages.append), prompts, messages

    def test_yes_accepts_exact_proposed_range(self):
        (start, end, method), prompts, _ = self.choose(["да"])
        self.assertEqual(end - start, timedelta(days=7))
        self.assertEqual(method, "accepted_proposal")
        self.assertIn("08.11.2026 12:00:00", prompts[0])
        self.assertIn("15.11.2026 12:00:00", prompts[0])

    def test_no_requests_custom_dates(self):
        (start, end, method), prompts, _ = self.choose(["нет", "2026-11-01", "2026-11-14"])
        self.assertEqual(end - start, timedelta(days=14))
        self.assertEqual(method, "entered_custom_dates")
        self.assertIn("включительно", prompts[2])

    def test_empty_answer_is_not_acceptance(self):
        (_, _, method), prompts, messages = self.choose(["", "maybe", "yes"])
        self.assertEqual(len(prompts), 3)
        self.assertEqual(len(messages), 2)
        self.assertEqual(method, "accepted_proposal")

    def test_bad_and_reversed_dates_request_reentry(self):
        (start, end, _), _, messages = self.choose([
            "no", "bad", "2026-11-14", "2026-11-01", "2026-11-02", "2026-11-02"
        ])
        self.assertEqual(end - start, timedelta(days=1))
        self.assertEqual(len(messages), 3)


class PeriodCliTests(unittest.TestCase):
    def test_confirm_cancel_and_explicit_selection(self):
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONIOENCODING": "utf-8"}
        (root / "data").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="period-test-", dir=root / "data") as temporary:
            target = Path(temporary) / "runs-data"
            command = [sys.executable, "-m", "dating_digest", "init-run", "--data-dir", str(target)]

            def run(options, answers=""):
                return subprocess.run(command + options, input=answers, capture_output=True,
                                      text=True, encoding="utf-8", env=env, cwd=root)

            cancelled = run(["--as-of", "2026-11-15T12:00:00+03:00"])
            self.assertEqual(cancelled.returncode, 2)
            self.assertFalse(target.exists())
            accepted = run(["--as-of", "2026-11-15T12:00:00+03:00"], "yes\n")
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            custom = run(["--as-of", "2026-11-15T12:00:00+03:00"],
                         "no\n2026-11-01\n2026-11-14\n")
            self.assertEqual(custom.returncode, 0, custom.stderr)
            explicit = run(["--start-date", "2026-10-01", "--end-date", "2026-10-31"])
            self.assertEqual(explicit.returncode, 0, explicit.stderr)
            manifests = [json.loads(p.read_text(encoding="utf-8")) for p in target.glob("runs/*/manifest.json")]
            self.assertEqual(len(manifests), 3)
            by_method = {m["window_confirmation"]["method"]: m for m in manifests}
            self.assertEqual(by_method["entered_custom_dates"]["window_days"], 14)
            self.assertEqual(by_method["explicit_dates"]["window_end_exclusive"], "2026-11-01T00:00:00+03:00")
            self.assertTrue(all(m["window_confirmation"]["status"] == "confirmed" for m in manifests))
            self.assertTrue(all(not m["publication_enabled"] for m in manifests))
            for options in (["--start-date", "2026-11-01"],
                            ["--start-date", "2026-11-15", "--end-date", "2026-11-01"],
                            ["--as-of", "2026-11-15"]):
                self.assertEqual(run(options).returncode, 2)
            self.assertEqual(len(list(target.glob("runs/*/manifest.json"))), 3)


if __name__ == "__main__":
    unittest.main()
