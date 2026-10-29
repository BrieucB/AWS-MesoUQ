"""Execute generated benchmark shells with synthetic, hash-pinned runtime profiles.

These metadata-only checks use no AWS calls, Slurm submission, journal or model.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from aws_mesouq.benchmark_worker import BenchmarkWorker


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("sha256sum") is None,
    reason="The generated native Linux batch shell requires bash and sha256sum",
)


def shell_fixture(tmp_path, profile_pythonpath, *, unusual_paths=False):
    folder = "runtime's $(touch SHELL_INJECTION) directory" if unusual_paths else "runtime"
    root = tmp_path / folder
    repository = root / "science"
    control = root / "control"
    for path in (repository / "src", control):
        path.mkdir(parents=True)
    environment = root / "environment.sh"
    environment.write_text(
        "unset PYTHONPATH\n" if profile_pythonpath is None
        else "export PYTHONPATH=" + shlex.quote(profile_pythonpath) + "\n"
    )
    adapter = control / "adapter.py"
    adapter.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'pythonpath': os.environ['PYTHONPATH'], "
        "'site': os.environ['MESOUQ_SITE'], 'argv': sys.argv[1:]}))\n"
    )
    operator = {
        "repository": str(repository), "python_bin": sys.executable,
        "environment_script": str(environment),
        "environment_sha256": hashlib.sha256(environment.read_bytes()).hexdigest(),
        "scheduler": {"partition": "qgpu_exp", "account": "synthetic", "qos": "synthetic",
                      "cpus-per-task": 1, "mem": "1G"},
    }
    # Exercise only the shell renderer; authenticating or opening a journal is unrelated.
    worker = object.__new__(BenchmarkWorker)
    worker.operator = operator
    script = root / "submit.sh"
    script.write_text(worker.script({"marker": "synthetic-runtime"}, str(control), {"time": "00:01:00"}))
    return script, repository, control, environment


def execute_shell(script, tmp_path):
    tools = {str(Path(shutil.which(name)).parent) for name in ("bash", "sha256sum")}
    environment = {
        "PATH": os.defpath + os.pathsep + os.pathsep.join(sorted(tools)),
        # The approved runtime profile must replace this ambient value first.
        "PYTHONPATH": "/ambient-path-must-not-survive",
    }
    return subprocess.run(
        [shutil.which("bash"), str(script)], cwd=tmp_path, env=environment,
        text=True, capture_output=True, timeout=15,
    )


@pytest.mark.parametrize("runtime_path", [None, "", "/runtime/first:/runtime/second"])
def test_generated_shell_prefixes_science_and_preserves_only_profile_path(tmp_path, runtime_path):
    script, repository, control, _ = shell_fixture(tmp_path, runtime_path)
    result = execute_shell(script, tmp_path)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    expected = str(repository / "src") + (":" + runtime_path if runtime_path else "")
    assert observed == {
        "pythonpath": expected, "site": "karolina",
        "argv": ["--manifest", str(control / "manifest.json")],
    }
    assert "/ambient-path-must-not-survive" not in observed["pythonpath"]


def test_profile_overlays_remain_importable_after_science_takes_precedence(tmp_path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "priority_module.py").write_text("VALUE = 'overlay'\n")
    (overlay / "overlay_only_module.py").write_text("VALUE = 'native-overlay'\n")
    script, repository, control, _ = shell_fixture(tmp_path, str(overlay))
    (repository / "src" / "priority_module.py").write_text("VALUE = 'science'\n")
    (control / "adapter.py").write_text(
        "import json, priority_module, overlay_only_module\n"
        "print(json.dumps([priority_module.VALUE, overlay_only_module.VALUE]))\n"
    )
    result = execute_shell(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["science", "native-overlay"]


def test_runtime_path_and_command_arguments_are_literal_shell_data(tmp_path):
    runtime_path = "/runtime with spaces:/literal'quote/$(touch SHELL_INJECTION);" + chr(96) + "touch SHELL_INJECTION" + chr(96)
    script, repository, control, _ = shell_fixture(tmp_path, runtime_path, unusual_paths=True)
    result = execute_shell(script, tmp_path)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["pythonpath"] == str(repository / "src") + ":" + runtime_path
    assert observed["argv"] == ["--manifest", str(control / "manifest.json")]
    assert not (tmp_path / "SHELL_INJECTION").exists()


def test_changed_profile_is_rejected_before_it_or_the_adapter_executes(tmp_path):
    script, _, control, environment = shell_fixture(tmp_path, "/approved/overlay")
    environment.write_text("touch PROFILE_EXECUTED\nexport PYTHONPATH=/changed\n")
    (control / "adapter.py").write_text("from pathlib import Path\nPath('ADAPTER_EXECUTED').touch()\n")
    result = execute_shell(script, tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "PROFILE_EXECUTED").exists()
    assert not (tmp_path / "ADAPTER_EXECUTED").exists()
