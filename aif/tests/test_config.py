import tempfile
import unittest
from pathlib import Path

from aif import config


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_match_plan(self) -> None:
        s = config.load(self.root, env={})
        self.assertEqual(s.region, "us-west-2")
        self.assertEqual(s.project, "ai-foundations-eks")
        self.assertEqual(s.argocd_mode, "capability")
        self.assertEqual(s.groups, config.GROUPS)
        self.assertEqual(
            s.contexts.as_dict(), {"hub": "aif-hub", "dev": "aif-dev", "prod": "aif-prod"}
        )

    def test_file_then_env(self) -> None:
        (self.root / "aif.toml").write_text(
            'region = "eu-west-1"\nargocd_mode = "helm"\ngroups = ["b"]\n'
            '[contexts]\nhub = "file-hub"\n[project_tags]\nproject = "p"\nowner = "o"\n'
        )
        s = config.load(self.root, env={"AIF_REGION": "us-east-1", "AIF_DEV_CONTEXT": "env-dev"})
        self.assertEqual(s.region, "us-east-1")
        self.assertEqual(s.argocd_mode, "helm")
        self.assertEqual(s.groups, ("base", "b"))
        self.assertEqual(s.contexts.hub, "file-hub")
        self.assertEqual(s.contexts.dev, "env-dev")
        self.assertEqual(s.project_tags, {"project": "p", "owner": "o"})

    def test_env_lists_are_comma_separated(self) -> None:
        s = config.load(self.root, env={"AIF_GROUPS": "e, a", "AIF_KEEP_STACKS": "launch,ide"})
        self.assertEqual(s.groups, ("base", "a", "e"))
        self.assertEqual(s.keep_stacks, ("launch", "ide"))

    def test_bad_values_are_rejected(self) -> None:
        cases = [
            {"AIF_ARGOCD_MODE": "local"},
            {"AIF_GROUPS": "z"},
            {"AIF_REGION": ""},
        ]
        for env in cases:
            with self.subTest(env=env), self.assertRaises(config.ConfigError):
                config.load(self.root, env=env)

    def test_bad_file_values_are_rejected(self) -> None:
        bodies = [
            "nope = 1\n",
            "region = [\n",
            "project_tags = 3\n",
            '[project_tags]\nowner = "o"\n',
            "contexts = 1\n",
            '[contexts]\nstaging = "x"\n',
            "groups = 3\n",
        ]
        for body in bodies:
            (self.root / "aif.toml").write_text(body)
            with self.subTest(body=body), self.assertRaises(config.ConfigError):
                config.load(self.root, env={})

    def test_missing_explicit_file_is_an_error(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(self.root, env={"AIF_CONFIG": str(self.root / "missing.toml")})

    def test_explicit_file(self) -> None:
        path = self.root / "other.toml"
        path.write_text('argocd_namespace = "gitops"\n')
        s = config.load(self.root, env={"AIF_CONFIG": str(path)})
        self.assertEqual(s.argocd_namespace, "gitops")

    def test_find_repo_root(self) -> None:
        (self.root / "Taskfile.yaml").write_text("version: '3'\n")
        (self.root / "stages").mkdir()
        nested = self.root / "aif" / "src"
        nested.mkdir(parents=True)
        self.assertEqual(config.find_repo_root(nested), self.root)

    def test_find_repo_root_fails_outside_repo(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.find_repo_root(self.root)
