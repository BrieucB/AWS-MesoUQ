"""Authenticated, unmodified historical sources for checkout-independent compatibility tests."""
from hashlib import sha1, sha256
from pathlib import Path

SOURCES = {('0369bfa6cad62517a4e01dcbaee0207c2955a2a5', 'src/aws_mesouq/benchmark_accuracy_protocol.py'): ('0369bfa6-benchmark_accuracy_protocol.py.txt', '0650fc7a22991db9b9b4e2336f93e4aabd8c367860d7fdcf8e78645ec5216956', 'a0873fe347f07dd846c31e24d5c52732cc50983c', 56978), ('0369bfa6cad62517a4e01dcbaee0207c2955a2a5', 'src/aws_mesouq/benchmark_accuracy_contexts.py'): ('0369bfa6-benchmark_accuracy_contexts.py.txt', '43f51680e429f268d1c01eb95dc469cb83f73ce1309658c619ed5fc8b7e90a89', '2289727d38a29f03df8f4f1026c0e34ed9c47b5a', 15833), ('ede35e7d0cfa39bf9ec6dca0af27fbfd0c6bf44f', 'src/aws_mesouq/benchmark_accuracy_protocol.py'): ('ede35e7d-benchmark_accuracy_protocol.py.txt', '76c5f0bb33db641390cdbc23429abd90c11577a1d9508aa0851aea3701c5e4f0', 'b9d74570c5273a4bb7620a46e32507ac2aa81ff6', 58704), ('ede35e7d0cfa39bf9ec6dca0af27fbfd0c6bf44f', 'src/aws_mesouq/benchmark_accuracy_contexts.py'): ('ede35e7d-benchmark_accuracy_contexts.py.txt', 'bad8eb5229a3c7fad8c48a6582d9a3c0ecc45ecb5032cfad2d979afb9732ce5c', '780af1ba0e8e05de1f118b564a52f5f84686542a', 16364)}

def load_source(root, ref, relative):
    name, expected_sha256, expected_blob, expected_size = SOURCES[(ref, relative)]
    raw = (Path(root) / "tests" / "fixtures" / "accuracy_legacy_protocol" / name).read_bytes()
    if len(raw) != expected_size or sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("historical source fixture integrity")
    blob = sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if blob != expected_blob:
        raise ValueError("historical source fixture origin")
    return raw
