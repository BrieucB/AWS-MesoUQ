"""Dense curves fail before verification/HPC; accepted data remains byte-identical."""

import pytest

from aws_mesouq.release import validate_curve_csv
from test_api import env, upload, error


@pytest.mark.parametrize("header", [b"", b"\xef\xbb\xbfforce,displacement\r\n"])
def test_thousand_rows_are_verified_without_decimation(env, header):
    env[0].release_policy = "v1"
    data = header + b"1,2\n" * 1000
    ref = upload(env, data=data)
    artifact = env[1].state["artifacts"][ref["id"]]
    assert artifact["verified"] is True
    assert env[2].data[artifact["storage_key"]] == data


def test_thousand_and_one_rows_never_become_verified(env):
    env[0].release_policy = "v1"
    data = b"force,displacement\n" + b"1,2\n" * 1001
    error("invalid_curve", lambda: upload(env, data=data))
    assert not env[1].state["jobs"]
    assert len(env[1].state["artifacts"]) == 1
    artifact = next(iter(env[1].state["artifacts"].values()))
    assert artifact["verified"] is False
    assert env[2].data[artifact["storage_key"]] == data


@pytest.mark.parametrize("data", [
    b"1,2\n", b"force,d\n1,2\n", b"force,d\n1,2,3\n3,4,5\n",
    b'force,d\n"1",2\n3,4\n', b"force,d\nNaN,2\n3,4\n",
    b"force,d\nInfinity,2\n3,4\n", b"force,d\n0x10,2\n3,4\n",
    b"force,d\n1_000,2\n3,4\n", b"force,d\n1e999,2\n3,4\n",
])
def test_csv_dialect_rejects_nonfinite_quoted_or_nondecimal_samples(data):
    with pytest.raises(ValueError):
        validate_curve_csv(data)


def test_csv_accepts_explicit_scientific_notation_and_blank_lines():
    assert validate_curve_csv(b" force,displacement\n +1e-2, .2\n\n 3., -4E+0\n") == 2
