"""Small conditional/versioned S3 stand-in for interrupted cleanup tests."""

from io import BytesIO
from types import SimpleNamespace

from botocore.exceptions import ClientError


class Storage:
    def __init__(self):
        self.objects = {}
        self.events = []
        self.fail_read = False

    def put_object(self, **request):
        self.events.append(("put", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        if "IfMatch" in request:
            assert request["IfMatch"] == '"owned-version"'
        else:
            assert request["IfNoneMatch"] == "*"
        assert request["ServerSideEncryption"] == "AES256"
        key = request["Key"]
        if key in self.objects and "IfMatch" not in request:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[key] = request["Body"]
        return {"ETag": '"owned-version"'}

    def get_object(self, **request):
        self.events.append(("get", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        if self.fail_read:
            raise ClientError({"Error": {"Code": "ExpiredToken"}}, "GetObject")
        if request["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(self.objects[request["Key"]]), "ETag": '"owned-version"'}

    def delete_object(self, **request):
        self.events.append(("delete", request))
        assert request["ExpectedBucketOwner"] == "123456789012"
        assert request["IfMatch"] == '"owned-version"'
        del self.objects[request["Key"]]


class VersionedStorage(Storage):
    def __init__(self):
        super().__init__()
        self.versions = []

    def put_object(self, **request):
        result = super().put_object(**request)
        self.versions.append(
            {"Key": request["Key"], "VersionId": str(len(self.versions)), "Body": request["Body"]}
        )
        return result

    def get_paginator(self, operation):
        assert operation == "list_object_versions"

        def paginate(**request):
            assert request["ExpectedBucketOwner"] == "123456789012"
            return [
                {"Versions": [v for v in self.versions if v["Key"].startswith(request["Prefix"])]}
            ]

        return SimpleNamespace(paginate=paginate)

    def delete_objects(self, **request):
        assert request["ExpectedBucketOwner"] == "123456789012"
        self.events.append(("delete_versions", request))
        removed = {(v["Key"], v["VersionId"]) for v in request["Delete"]["Objects"]}
        self.versions = [v for v in self.versions if (v["Key"], v["VersionId"]) not in removed]
        for key, _ in removed:
            versions = [v for v in self.versions if v["Key"] == key]
            if versions:
                self.objects[key] = versions[-1]["Body"]
            else:
                self.objects.pop(key, None)
        return {}
