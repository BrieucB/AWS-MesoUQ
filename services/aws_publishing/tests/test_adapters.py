import base64
import copy
import hashlib
import json
import pytest
from aws_publishing.storage import (
    DynamoStore,
    S3Objects,
    SQSNotifications,
    Conflict,
    empty,
)


class ConditionalFailure(Exception):
    def __init__(self):
        self.response = {"CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}


class FakeDynamo:
    class exceptions:
        TransactionCanceledException = ConditionalFailure

    def __init__(self):
        self.records = {}
        self.transactions = []
        self.page_size = 2

    def get_item(self, **kwargs):
        assert kwargs["ConsistentRead"]
        key = kwargs["Key"]["sk"]["S"]
        return {"Item": copy.deepcopy(self.records[key])} if key in self.records else {}

    def query(self, **kwargs):
        assert kwargs["ConsistentRead"]
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]["S"]
        keys = sorted(key for key in self.records if key.startswith(prefix))
        cursor = kwargs.get("ExclusiveStartKey", {}).get("sk", {}).get("S")
        if cursor:
            keys = [key for key in keys if key > cursor]
        selected = keys[: self.page_size]
        response = {"Items": [copy.deepcopy(self.records[key]) for key in selected]}
        if len(keys) > self.page_size:
            response["LastEvaluatedKey"] = {
                "pk": {"S": "STATE"},
                "sk": {"S": selected[-1]},
            }
        return response

    def transact_write_items(self, **kwargs):
        writes = kwargs["TransactItems"]
        revision = writes[0]["Put"]
        previous = self.records.get("!revision")
        if revision["ConditionExpression"] == "attribute_not_exists(pk)":
            if previous:
                raise ConditionalFailure()
        elif previous["version"] != revision["ExpressionAttributeValues"][":v"]:
            raise ConditionalFailure()
        self.transactions.append(copy.deepcopy(writes))
        for write in writes:
            item = write["Put"]["Item"]
            self.records[item["sk"]["S"]] = copy.deepcopy(item)


def test_dynamo_pagination_and_atomic_cas():
    client, before = FakeDynamo(), empty()
    store = DynamoStore(client, "table")
    assert store.load() == (0, before)
    after = copy.deepcopy(before)
    for i in range(6):
        after["jobs"][str(i)] = {"id": str(i), "status": "pending"}
    store.save(0, before, after)
    revision, restored = store.load()
    assert revision == 1 and restored == after
    with pytest.raises(Conflict):
        store.save(0, before, after)
    assert len(client.transactions) == 1
    newer = copy.deepcopy(after)
    newer["jobs"]["1"]["status"] = "claimed"
    store.save(revision, after, newer)
    assert len(client.transactions[-1]) == 2  # revision + changed record only
    assert store.load() == (2, newer)


def test_dynamo_record_size_guard():
    store = DynamoStore(FakeDynamo(), "table")
    before, after = empty(), empty()
    after["jobs"]["large"] = {"result": "x" * 300_001}
    with pytest.raises(ValueError, match="safe storage"):
        store.save(0, before, after)


def test_s3_signed_checksum_and_immutable_put():
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    client = boto3.client(
        "s3",
        region_name="eu-central-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    objects = S3Objects(client, "private-bucket")
    sha = hashlib.sha256(b"data").hexdigest()
    artifact = {"sha256": sha, "size_bytes": 4, "media_type": "text/csv"}
    url, headers = objects.upload_url("projects/p/artifacts/a", artifact)
    assert "private-bucket" in url
    assert headers["If-None-Match"] == "*"
    assert (
        headers["x-amz-checksum-sha256"]
        == base64.b64encode(bytes.fromhex(sha)).decode()
    )
    params = {
        "Bucket": "private-bucket",
        "Key": "projects/p/artifacts/a",
        "ChecksumMode": "ENABLED",
    }
    with Stubber(client) as stub:
        stub.add_response(
            "head_object",
            {"ContentLength": 4, "ChecksumSHA256": headers["x-amz-checksum-sha256"]},
            params,
        )
        assert objects.verify("projects/p/artifacts/a", artifact)
        stub.add_response(
            "head_object", {"ContentLength": 4, "ChecksumSHA256": "incorrect"}, params
        )
        assert not objects.verify("projects/p/artifacts/a", artifact)
        stub.add_client_error(
            "head_object",
            service_error_code="404",
            http_status_code=404,
            expected_params=params,
        )
        assert not objects.verify("projects/p/artifacts/a", artifact)
        stub.add_response(
            "put_object_tagging",
            {},
            {
                "Bucket": "private-bucket",
                "Key": "projects/p/artifacts/a",
                "Tagging": {"TagSet": [{"Key": "archive_verified", "Value": "true"}]},
            },
        )
        objects.archived("projects/p/artifacts/a")


def test_notification_contains_only_revision():
    class Client:
        def send_message(self, **kwargs):
            assert kwargs == {
                "QueueUrl": "queue",
                "MessageBody": '{"schema_version": 1, "revision": 4}',
            }

    SQSNotifications(Client(), "queue").changed(4)

    class Unavailable:
        def send_message(self, **kwargs):
            raise OSError("offline")

    SQSNotifications(Unavailable(), "queue").changed(4)


def test_dynamo_dedup_history_uses_point_reads_not_snapshot_queries():
    client = FakeDynamo()
    store = DynamoStore(client, "table")
    before, after = empty(), empty()
    after["events"]["old-event"] = {"hash": "some-hash"}
    after["requests"]["old-request"] = {"job_id": "some-job"}
    store.save(0, before, after)
    _, restored = store.load()
    assert list(restored["events"]) == []  # no heartbeat-history scan
    assert restored["events"].get("old-event") == after["events"]["old-event"]
    assert restored["requests"].get("old-request") == after["requests"]["old-request"]
    assert restored["events"].get("missing") is None


def test_s3_bounded_json_report_is_checksum_verified():
    import io

    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber
    from botocore.response import StreamingBody

    client = boto3.client(
        "s3",
        region_name="eu-central-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    objects = S3Objects(client, "private-bucket")
    data = b'{"passed":true}'
    artifact = {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with Stubber(client) as stub:
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(data), len(data))},
            {"Bucket": "private-bucket", "Key": "validation"},
        )
        assert objects.read_json("validation", artifact) == {"passed": True}
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(data), len(data))},
            {"Bucket": "private-bucket", "Key": "validation"},
        )
        with pytest.raises(ValueError, match="checksum"):
            objects.read_json("validation", {**artifact, "sha256": "0" * 64})
    with pytest.raises(ValueError, match="limit"):
        objects.read_json("validation", {"size_bytes": 100_001, "sha256": "0" * 64})


def test_mature_tag_retries_tolerate_already_expired_objects_but_not_access_denial():
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    client = boto3.client(
        "s3",
        region_name="eu-central-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    objects = S3Objects(client, "private-bucket")
    params = {
        "Bucket": "private-bucket",
        "Key": "mature-object",
        "Tagging": {"TagSet": [{"Key": "archive_verified", "Value": "true"}]},
    }
    with Stubber(client) as stub:
        stub.add_client_error(
            "put_object_tagging",
            service_error_code="NoSuchKey",
            http_status_code=404,
            expected_params=params,
        )
        objects.archived(
            "mature-object"
        )  # previous due tag may have expired it before CAS acknowledgement
        stub.add_client_error(
            "put_object_tagging",
            service_error_code="AccessDenied",
            http_status_code=403,
            expected_params=params,
        )
        with pytest.raises(client.exceptions.ClientError):
            objects.archived("mature-object")


def test_cleanup_distinguishes_absent_objects_from_access_denial():
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    client = boto3.client("s3", region_name="eu-central-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    objects = S3Objects(client, "private-bucket")
    params = {"Bucket": "private-bucket", "Key": "reservation"}
    with Stubber(client) as stub:
        stub.add_response("head_object", {"ContentLength": 1}, params)
        assert objects.absent("reservation") is False
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404, expected_params=params)
        assert objects.absent("reservation") is True
        stub.add_client_error("head_object", service_error_code="AccessDenied", http_status_code=403, expected_params=params)
        with pytest.raises(client.exceptions.ClientError):
            objects.absent("reservation")
        stub.add_response("delete_object", {}, params)
        objects.discard("reservation")
