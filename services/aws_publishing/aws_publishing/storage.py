"""Optimistic atomic metadata storage, and private object storage adapters."""

import base64
import copy
import hashlib
import json
import threading

BUCKETS = (
    "identities",
    "projects",
    "jobs",
    "artifacts",
    "models",
    "requests",
    "events",
)


def empty():
    return {key: {} for key in BUCKETS}


class Conflict(Exception):
    pass


class MemoryStore:
    def __init__(self):
        self.state, self.revision, self.lock = empty(), 0, threading.Lock()

    def load(self):
        with self.lock:
            return self.revision, copy.deepcopy(self.state)

    def save(self, revision, before, after):
        with self.lock:
            if self.revision != revision:
                raise Conflict()
            self.state, self.revision = copy.deepcopy(after), revision + 1


class LazyRecords(dict):
    """Point-read dedup records; heartbeat history is never scanned per request."""

    def __init__(self, client, table, bucket):
        super().__init__()
        self.client, self.table, self.bucket = client, table, bucket

    def get(self, key, default=None):
        if key in self:
            return super().get(key, default)
        if not isinstance(key, str):
            return default
        item = self.client.get_item(
            TableName=self.table,
            Key={"pk": {"S": "STATE"}, "sk": {"S": f"{self.bucket}:{key}"}},
            ConsistentRead=True,
        ).get("Item")
        return json.loads(item["payload"]["S"]) if item else default

    def __deepcopy__(self, memo):
        other = type(self)(self.client, self.table, self.bucket)
        for key, value in self.items():
            other[key] = copy.deepcopy(value, memo)
        return other


class DynamoStore:
    """CAS serializes admissions across projects; records remain individually bounded.

    A consistent paginated query bracketed by the global revision gives a coherent
    snapshot. Every mutation checks/increments that revision in the same transaction.
    Intended for the approved five-user control plane, not high-throughput workloads.
    """

    def __init__(self, client, table):
        self.client, self.table = client, table

    def _revision(self):
        item = self.client.get_item(
            TableName=self.table,
            Key={"pk": {"S": "STATE"}, "sk": {"S": "!revision"}},
            ConsistentRead=True,
        ).get("Item")
        return int(item["version"]["N"]) if item else 0

    def load(self):
        for _ in range(12):
            revision, state = self._revision(), empty()
            for bucket in BUCKETS:
                if bucket in ("events", "requests"):
                    state[bucket] = LazyRecords(self.client, self.table, bucket)
                    continue
                cursor = None
                while True:
                    args = dict(
                        TableName=self.table,
                        KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                        ExpressionAttributeValues={
                            ":pk": {"S": "STATE"},
                            ":prefix": {"S": bucket + ":"},
                        },
                        ConsistentRead=True,
                    )
                    if cursor:
                        args["ExclusiveStartKey"] = cursor
                    page = self.client.query(**args)
                    for item in page["Items"]:
                        ident = item["sk"]["S"].split(":", 1)[1]
                        state[bucket][ident] = json.loads(item["payload"]["S"])
                    cursor = page.get("LastEvaluatedKey")
                    if not cursor:
                        break
            if revision == self._revision():
                return revision, state
        raise Conflict("Metadata busy")

    def save(self, revision, before, after):
        writes = [
            {
                "Put": {
                    "TableName": self.table,
                    "Item": {
                        "pk": {"S": "STATE"},
                        "sk": {"S": "!revision"},
                        "version": {"N": str(revision + 1)},
                    },
                    "ConditionExpression": (
                        "attribute_not_exists(pk)" if revision == 0 else "#v = :v"
                    ),
                    **(
                        {}
                        if revision == 0
                        else {
                            "ExpressionAttributeNames": {"#v": "version"},
                            "ExpressionAttributeValues": {":v": {"N": str(revision)}},
                        }
                    ),
                }
            }
        ]
        for bucket in BUCKETS:
            for key, value in after[bucket].items():
                if before[bucket].get(key) != value:
                    payload = json.dumps(value, separators=(",", ":"), allow_nan=False)
                    if len(payload.encode()) > 300_000:
                        raise ValueError("Metadata record exceeds safe storage limit")
                    writes.append(
                        {
                            "Put": {
                                "TableName": self.table,
                                "Item": {
                                    "pk": {"S": "STATE"},
                                    "sk": {"S": f"{bucket}:{key}"},
                                    "payload": {"S": payload},
                                },
                            }
                        }
                    )
        if len(writes) > 100:
            raise ValueError("Transaction exceeds safe operation limit")
        try:
            self.client.transact_write_items(TransactItems=writes)
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
                raise Conflict() from exc
            raise


class MemoryObjects:
    def __init__(self):
        self.data, self.tags = {}, {}

    def upload_url(self, key, artifact):
        return f"memory://{key}", {
            "x-amz-checksum-sha256": base64.b64encode(
                bytes.fromhex(artifact["sha256"])
            ).decode(),
            "Content-Type": artifact["media_type"],
            "Content-Length": str(artifact["size_bytes"]),
        }

    def verify(self, key, artifact):
        data = self.data.get(key)
        return (
            data is not None
            and len(data) == artifact["size_bytes"]
            and hashlib.sha256(data).hexdigest() == artifact["sha256"]
        )

    def read_json(self, key, artifact, *, maximum=100_000):
        return json.loads(self.read_bytes(key, artifact, maximum=maximum))

    def read_bytes(self, key, artifact, *, maximum):
        data = self.data.get(key, b"")
        if (
            len(data) > maximum
            or len(data) != artifact["size_bytes"]
            or hashlib.sha256(data).hexdigest() != artifact["sha256"]
        ):
            raise ValueError("Invalid bounded JSON artifact")
        return data

    def download_url(self, key):
        return f"memory://{key}"

    def discard(self, key):
        self.data.pop(key, None)

    def absent(self, key):
        return key not in self.data

    def archived(self, key):
        self.tags[key] = {"archive_verified": "true"}


class S3Objects:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def upload_url(self, key, artifact):
        checksum = base64.b64encode(bytes.fromhex(artifact["sha256"])).decode()
        params = {
            "Bucket": self.bucket,
            "Key": key,
            "ChecksumSHA256": checksum,
            "ContentType": artifact["media_type"],
            "ContentLength": artifact["size_bytes"],
            "IfNoneMatch": "*",
        }
        return self.client.generate_presigned_url(
            "put_object", Params=params, ExpiresIn=900
        ), {
            "x-amz-checksum-sha256": checksum,
            "Content-Type": artifact["media_type"],
            "If-None-Match": "*",
            "Content-Length": str(artifact["size_bytes"]),
        }

    def verify(self, key, artifact):
        try:
            obj = self.client.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED"
            )
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        checksum = base64.b64encode(bytes.fromhex(artifact["sha256"])).decode()
        return (
            obj["ContentLength"] == artifact["size_bytes"]
            and obj.get("ChecksumSHA256") == checksum
        )

    def read_json(self, key, artifact, *, maximum=100_000):
        return json.loads(self.read_bytes(key, artifact, maximum=maximum))

    def read_bytes(self, key, artifact, *, maximum):
        if artifact["size_bytes"] > maximum:
            raise ValueError("Validation artifact exceeds bounded JSON limit")
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            data = body.read(maximum + 1)
        finally:
            body.close()
        if (
            len(data) != artifact["size_bytes"]
            or hashlib.sha256(data).hexdigest() != artifact["sha256"]
        ):
            raise ValueError("Validation artifact checksum mismatch")
        return data

    def download_url(self, key):
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=300
        )

    def discard(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def absent(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return False
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return True
            raise

    def archived(self, key):
        try:
            self.client.put_object_tagging(
                Bucket=self.bucket,
                Key=key,
                Tagging={"TagSet": [{"Key": "archive_verified", "Value": "true"}]},
            )
        except self.client.exceptions.ClientError as exc:
            # A prior successful tag may already have expired an old object
            # before the metadata acknowledgement. Its persisted receipt remains.
            if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise


class SQSNotifications:
    """Best-effort encrypted revision notifications; DynamoDB is the work ledger.

    Messages contain no inputs, user identities, results or scheduler credentials.
    Lost/duplicate notifications cannot cause a submission or lose an admitted job.
    """

    def __init__(self, client, queue_url):
        self.client, self.queue_url = client, queue_url

    def changed(self, revision):
        import logging

        try:
            self.client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps({"schema_version": 1, "revision": revision}),
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Revision notification unavailable; durable ledger remains authoritative"
            )
