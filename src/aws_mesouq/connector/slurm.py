"""Slurm submission reconciliation with an unambiguous own-user job marker."""
from __future__ import annotations

import re
from .transport import Transport


# Executed by the explicitly verified interpreter on the scheduler host. Keep
# this stdlib-only: SSH sessions need no separately installed connector package.
SUBMISSION_HELPER = """import os,shutil,subprocess,sys
allowed=('PATH','HOME','USER','LOGNAME','LANG','LC_ALL','TZ')
environment={key:os.environ[key] for key in allowed if key in os.environ}
environment.setdefault('PATH',os.defpath)
executable=shutil.which('sbatch',path=environment['PATH'])
if executable is None:
    raise FileNotFoundError('sbatch is unavailable on the trusted scheduler PATH')
script=os.path.abspath(sys.argv[1])
result=subprocess.run([executable,'--parsable','--nodes=1','--ntasks=1','--no-requeue','--export=NIL',script],
    env=environment,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
sys.stdout.buffer.write(result.stdout)
"""


class Slurm:
    def __init__(self, transport: Transport):
        self.transport = transport
        self.user = transport.identity()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.user):
            raise ValueError("Invalid scheduler identity")

    def submit(self, script: str) -> str:
        # Interrupted scientific runs require explicit journal/operator recovery;
        # automatic scheduler requeue would bypass that execution boundary.
        # sbatch environment options override #SBATCH directives, including
        # job arrays. Never pass ambient SBATCH_*, SLURM_* aliases, loader or
        # shell hooks into the submission process or exported batch environment.
        output = self.transport.run([getattr(self.transport, "staging_python", "/usr/bin/python3.11"),
                                     "-I", "-c", SUBMISSION_HELPER, script]).strip()
        job_id = output.split(";")[0]
        if not re.fullmatch(r"[0-9]+", job_id):
            raise RuntimeError("Ambiguous sbatch response")
        return job_id

    def lookup(self, marker: str) -> list[tuple[str, str]]:
        records = {}
        for argv in (["squeue", "--noheader", "--user", self.user, "--name", marker, "--format=%i|%j|%u|%T"],
                     ["sacct", "--noheader", "--parsable2", "--user", self.user, "--starttime=1970-01-01", "--name", marker, "--format=JobIDRaw,JobName%128,User%128,State"]):
            for line in self.transport.run(argv).splitlines():
                fields = line.strip().split("|")
                if len(fields) >= 4 and fields[1] == marker and fields[2] == self.user and re.fullmatch(r"[0-9]+", fields[0]):
                    records[fields[0]] = fields[3].split()[0].rstrip("+")
        return list(records.items())

    def cancel(self, job_id: str, marker: str, *, authorize=None) -> None:
        if not re.fullmatch(r"[0-9]+", job_id):
            raise ValueError("Invalid scheduler job id")
        if job_id not in dict(self.lookup(marker)):
            raise RuntimeError("Cancellation identity could not be verified")
        if authorize is not None:
            authorize()
        self.transport.run(["scancel", "--user", self.user, job_id])
