import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import RequestsTransport
from azure.keyvault.secrets import SecretClient


class FakeCredential:
    def get_token(self, *scopes, **kwargs):
        exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        return AccessToken("fake-token", exp)


@pytest.fixture(scope="module")
def client():
    base_url = os.getenv("FAKE_AKV_BASE_URL", "https://127.0.0.1:8443")

    transport = RequestsTransport(connection_verify=False)

    return SecretClient(
        vault_url=base_url,
        credential=FakeCredential(),
        transport=transport,
        verify_challenge_resource=False,
    )


def test_set_and_get_secret(client: SecretClient):
    name = f"it-{uuid.uuid4().hex[:8]}"
    expected_value = "hello-world"

    set_result = client.set_secret(name, expected_value)
    assert set_result.name == name

    got = client.get_secret(name)
    assert got.value == expected_value
    assert got.id is not None
    assert got.id.endswith(f"/secrets/{name}/{set_result.properties.version}")


def test_versioning_and_list_versions(client: SecretClient):
    name = f"it-{uuid.uuid4().hex[:8]}"
    v1 = client.set_secret(name, "v1")
    v2 = client.set_secret(name, "v2")
    assert v1.properties.version != v2.properties.version

    versions = list(client.list_properties_of_secret_versions(name))
    seen_versions = {v.version for v in versions}
    assert {v1.properties.version, v2.properties.version} <= seen_versions


def test_delete_and_recover(client: SecretClient):
    name = f"it-{uuid.uuid4().hex[:8]}"
    client.set_secret(name, "to-delete")

    deleted = client.begin_delete_secret(name).result()
    assert deleted.recovery_id

    d = client.get_deleted_secret(name)
    assert d.name == name

    recovered = client.begin_recover_deleted_secret(name).result()
    assert recovered.name == name

    got = client.get_secret(name)
    assert got.value == "to-delete"


def test_tags_lifecycle(client: SecretClient):
    base_url = os.getenv("FAKE_AKV_BASE_URL", "https://127.0.0.1:8443")
    name = f"it-{uuid.uuid4().hex[:8]}"
    initial_tags = {"env": "dev", "team": "qa"}

    created = client.set_secret(name, "tagged-value", tags=initial_tags)
    assert created.properties.tags == initial_tags

    fetched = client.get_secret(name)
    assert fetched.properties.tags == initial_tags
    assert fetched.value == "tagged-value"

    listed = next((s for s in client.list_properties_of_secrets() if s.name == name), None)
    assert listed is not None
    assert listed.tags == initial_tags

    updated = client.update_secret_properties(name, created.properties.version, tags={"env": "prod"})
    assert updated.tags == {"env": "prod"}

    refreshed = client.get_secret(name)
    assert refreshed.properties.tags == {"env": "prod"}
    assert refreshed.value == "tagged-value"

    resp = requests.get(
        f"{base_url}/secrets",
        params={"api-version": "7.4", "tag-name": "env", "tag-value": "prod"},
        headers={"Authorization": "Bearer dummy"},
        verify=False,
        timeout=2,
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("value"), list)
    ids = [item.get("id", "") for item in payload["value"]]
    assert any(f"/secrets/{name}" in i for i in ids)
    assert all(item.get("tags", {}).get("env") == "prod" for item in payload["value"])
