"""Offline normal-client output filtering; no AWS or identity token operations."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest

SPEC = importlib.util.spec_from_file_location("private_aws_action", Path(__file__).resolve().parents[1] / "tools/private_aws_action.py")
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)
ROLE = "arn:aws:iam::123456789012:role/scoped/static-publisher"


class PrivateActionTests(unittest.TestCase):
    def inputs(self):
        return {"PUBLICATION_ROLE_ARN": ROLE, "PUBLICATION_REGION": "eu-central-1",
                "GITHUB_RUN_ID": "42", "PUBLICATION_SESSION_POLICY": json.dumps({"Statement": []})}

    def test_official_inputs_defaults_and_normal_environment_exports(self):
        env = client.action_environment("configure", self.inputs())
        self.assertEqual(env["INPUT_AUDIENCE"], "sts.amazonaws.com")
        self.assertEqual(env["INPUT_ROLE-TO-ASSUME"], ROLE)
        self.assertEqual(env["INPUT_ROLE-DURATION-SECONDS"], "900")
        self.assertEqual(env["INPUT_ALLOWED-ACCOUNT-IDS"], "123456789012")
        for name in ("OUTPUT-ENV-CREDENTIALS", "UNSET-CURRENT-CREDENTIALS", "MASK-AWS-ACCOUNT-ID"):
            self.assertEqual(env["INPUT_" + name], "true")
        for name in ("OUTPUT-CREDENTIALS", "FORCE-SKIP-OIDC", "ROLE-CHAINING"):
            self.assertEqual(env["INPUT_" + name], "false")
        self.assertEqual(env["INPUT_USE-EXISTING-CREDENTIALS"], "")

    def test_configure_discards_ambient_owner_profile_credential_sources(self):
        source = self.inputs()
        names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
                 "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ENDPOINT_URL_STS", "AWS_CA_BUNDLE")
        source.update({name: "synthetic-private" for name in names})
        env = client.action_environment("configure", source)
        self.assertTrue(all(name not in env for name in names))

    def test_inherited_action_inputs_cannot_choose_profile_keys_or_custom_sts(self):
        source = self.inputs()
        inherited = ("INPUT_AWS-PROFILE", "INPUT_AWS-ACCESS-KEY-ID",
                     "INPUT_AWS-SECRET-ACCESS-KEY", "INPUT_AWS-SESSION-TOKEN",
                     "INPUT_WEB-IDENTITY-TOKEN-FILE", "INPUT_STS-ENDPOINT",
                     "INPUT_USE-EXISTING-CREDENTIALS")
        source.update({name: "synthetic-owner-path" for name in inherited})
        env = client.action_environment("configure", source)
        self.assertTrue(all(name not in env for name in inherited[:-1]))
        self.assertEqual(env["INPUT_USE-EXISTING-CREDENTIALS"], "")

    def test_cleanup_keeps_normal_client_post_hook_and_requires_no_private_inputs(self):
        env = client.action_environment("cleanup", {"GITHUB_ENV": "synthetic-file",
                                                   "INPUT_AWS-PROFILE": "synthetic-profile"})
        self.assertNotIn("INPUT_AWS-PROFILE", env)
        self.assertEqual(env["INPUT_OUTPUT-ENV-CREDENTIALS"], "true")
        self.assertEqual(env["GITHUB_ENV"], "synthetic-file")

    def test_only_complete_mask_commands_forwarded_and_both_streams_filtered(self):
        calls = []
        class FakeProcess:
            stdout = io.StringIO("Authenticated as assumedRoleId AROA-SYNTHETIC:session\n"
                                 "::add-mask::synthetic-secret\n"
                                 "::error::private endpoint\n"
                                 "::debug::private identifier\n"
                                 "plain private diagnostic\n"
                                 "::add-mask::incomplete")
            def wait(self):
                return 0
        def popen(command, **kwargs):
            calls.append((command, kwargs))
            return FakeProcess()
        output = io.StringIO()
        self.assertEqual(client.stream_action(["synthetic-client"], {}, output, popen), 0)
        self.assertEqual(output.getvalue(), "::add-mask::synthetic-secret\n")
        self.assertEqual(calls[0][1]["stderr"], subprocess.STDOUT)
        self.assertEqual(calls[0][1]["stdout"], subprocess.PIPE)

    def test_producer_failure_preserved(self):
        class FakeProcess:
            stdout = io.StringIO("private error must not escape\n")
            def wait(self):
                return 7
        output = io.StringIO()
        self.assertEqual(client.stream_action(["synthetic-client"], {}, output,
                                             lambda *a, **kw: FakeProcess()), 7)
        self.assertEqual(output.getvalue(), "")

    def test_bad_metadata_rejected_without_exposing_values(self):
        for patch in ({"PUBLICATION_ROLE_ARN": "root"}, {"GITHUB_RUN_ID": "not-numeric"},
                      {"PUBLICATION_REGION": "bad\nregion"}, {"PUBLICATION_SESSION_POLICY": "invalid"}):
            with self.subTest(patch=patch):
                values = self.inputs()
                values.update(patch)
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    client.action_environment("configure", values)


if __name__ == "__main__":
    unittest.main()
