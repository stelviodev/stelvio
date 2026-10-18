"""Small conditional/versioned S3 stand-in for interrupted cleanup tests."""

from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace

from botocore.exceptions import ClientError


class Storage:
    def __init__(self):
        self.objects = {}
        self.events = []
        self.fail_read = False

    def _etag(self, key):
        return '"' + sha256(self.objects[key]).hexdigest() + '"'

    def put_object(self, **request):
        self.events.append(("put", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        if "IfMatch" not in request:
            assert request["IfNoneMatch"] == "*"
        assert request["ServerSideEncryption"] == "AES256"
        key = request["Key"]
        if "IfMatch" in request and (
            key not in self.objects or request["IfMatch"] != self._etag(key)
        ):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if key in self.objects and "IfMatch" not in request:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[key] = request["Body"]
        return {"ETag": self._etag(key)}

    def get_object(self, **request):
        self.events.append(("get", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        if self.fail_read:
            raise ClientError({"Error": {"Code": "ExpiredToken"}}, "GetObject")
        if request["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(self.objects[request["Key"]]), "ETag": self._etag(request["Key"])}

    def delete_object(self, **request):
        self.events.append(("delete", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        if request["IfMatch"] != self._etag(request["Key"]):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "DeleteObject")
        del self.objects[request["Key"]]


class VersionedStorage(Storage):
    def __init__(self):
        super().__init__()
        self.versions = []
        self.delete_markers = []
        self.next_version = 0

    def put_object(self, **request):
        result = super().put_object(**request)
        self.next_version += 1
        self.versions.append(
            {"Key": request["Key"], "VersionId": str(self.next_version), "Body": request["Body"]}
        )
        return result

    def delete_object(self, **request):
        super().delete_object(**request)
        self.next_version += 1
        self.delete_markers.append({"Key": request["Key"], "VersionId": str(self.next_version)})

    def get_paginator(self, operation):
        assert operation in {"list_object_versions", "list_objects_v2"}

        def paginate(**request):
            assert request["ExpectedBucketOwner"] == "123456789012"
            if operation == "list_objects_v2":
                return [
                    {
                        "Contents": [
                            {"Key": key}
                            for key in self.objects
                            if key.startswith(request["Prefix"])
                        ]
                    }
                ]
            return [
                {
                    "Versions": [
                        v for v in self.versions if v["Key"].startswith(request["Prefix"])
                    ],
                    "DeleteMarkers": [
                        v for v in self.delete_markers if v["Key"].startswith(request["Prefix"])
                    ],
                }
            ]

        return SimpleNamespace(paginate=paginate)

    def delete_objects(self, **request):
        assert request["ExpectedBucketOwner"] == "123456789012"
        self.events.append(("delete_versions", request))
        removed = {(v["Key"], v["VersionId"]) for v in request["Delete"]["Objects"]}
        self.versions = [v for v in self.versions if (v["Key"], v["VersionId"]) not in removed]
        self.delete_markers = [
            v for v in self.delete_markers if (v["Key"], v["VersionId"]) not in removed
        ]
        for key, _ in removed:
            versions = [v for v in self.versions if v["Key"] == key]
            if any(v["Key"] == key for v in self.delete_markers):
                self.objects.pop(key, None)
            elif versions:
                self.objects[key] = versions[-1]["Body"]
            else:
                self.objects.pop(key, None)
        return {}
