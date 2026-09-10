import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github/scripts/staging/publish_runtime_db.py"
spec = importlib.util.spec_from_file_location("publish_runtime_db", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
URL = "postgresql+asyncpg://postgres.branch:p%40ss%3Aword@aws-0-us-west-1.pooler.supabase.com:5432/postgres?ssl=require"


class RuntimeDatabaseTests(unittest.TestCase):
    def test_workflow_capacity_and_migration_runtime_separation(self):
        workflow = yaml.safe_load(
            (REPO / ".github/workflows/staging-deploy.yml").read_text()
        )
        job = workflow["jobs"]["deploy"]
        self.assertEqual(job["env"]["ODDISH_MODAL_WORKER_MAX_CONTAINERS"], "400")
        self.assertEqual(job["env"]["ODDISH_MODAL_MAX_WORKERS_PER_POLL"], "400")
        steps = job["steps"]
        names = [step.get("name") for step in steps]
        publish = names.index("Publish staging runtime database connection")
        self.assertLess(names.index("Backend migrations"), publish)
        self.assertLess(publish, names.index("Deploy"))
        for name in ["Core migrations", "Backend migrations"]:
            self.assertEqual(
                steps[names.index(name)]["env"]["ODDISH_DATABASE_URL"],
                "${{ secrets.STAGING_DATABASE_URL }}",
            )
        bootstrap = yaml.safe_load(
            (REPO / ".github/workflows/staging-db-bootstrap.yml").read_text()
        )
        step = next(
            s
            for s in bootstrap["jobs"]["bootstrap"]["steps"]
            if s.get("name") == "Publish oddish-staging-db Modal secret"
        )
        self.assertEqual(step["run"], steps[publish]["run"])
        self.assertEqual(
            step["env"]["STAGING_DATABASE_URL"], "${{ steps.branch.outputs.db_url }}"
        )

    def test_preserves_credentials_host_and_query(self):
        self.assertEqual(module.transaction_url(URL), URL.replace(":5432/", ":6543/"))

    def test_already_transaction_mode(self):
        url = URL.replace(":5432/", ":6543/")
        self.assertEqual(module.transaction_url(url), url)

    def test_rejects_non_pooler_or_incomplete_urls(self):
        for url in [
            "",
            URL.replace(".pooler.supabase.com", ".supabase.co"),
            URL.replace(":5432/", ":9999/"),
            URL.replace("/postgres?", "/other?"),
            URL.replace("postgresql+asyncpg", "https"),
            URL.replace(":p%40ss%3Aword", ""),
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                module.transaction_url(url)

    def test_publishes_only_staging_with_existing_runtime_settings(self):
        with (
            patch.dict(os.environ, {"STAGING_DATABASE_URL": URL}),
            patch.object(module.subprocess, "run") as run,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            module.main()
        args = run.call_args.args[0]
        self.assertEqual(
            args[:9],
            [
                "uv",
                "run",
                "modal",
                "secret",
                "create",
                "--env",
                "staging",
                "--force",
                "oddish-staging-db",
            ],
        )
        self.assertEqual(
            set(args[9:]),
            {
                "ODDISH_DATABASE_URL=" + URL.replace(":5432/", ":6543/"),
                "ODDISH_DASHBOARD_URL=https://staging.oddish.app",
                "CORS_ALLOWED_ORIGINS=https://staging.oddish.app",
                "LOGFIRE_ENVIRONMENT=staging",
            },
        )
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertEqual(
            output.getvalue(), "::add-mask::" + URL.replace(":5432/", ":6543/") + "\n"
        )

    def test_invalid_url_never_publishes(self):
        with (
            patch.dict(os.environ, {"STAGING_DATABASE_URL": "invalid"}),
            patch.object(module.subprocess, "run") as run,
            self.assertRaises(ValueError),
        ):
            module.main()
        run.assert_not_called()

    def test_publish_failure_stops_deployment(self):
        with (
            patch.dict(os.environ, {"STAGING_DATABASE_URL": URL}),
            patch.object(
                module.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "modal"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            module.main()


if __name__ == "__main__":
    unittest.main()
