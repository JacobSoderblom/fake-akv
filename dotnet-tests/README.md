# .NET integration tests

This folder contains an xUnit project that exercises the fake Key Vault service using the official **Azure.Security.KeyVault.Secrets** SDK.
Authentication uses a static bearer token and a redirect-following HTTP handler suited for local/fake deployments.
The tests build the repository's Docker image and launch it via Testcontainers so they exercise the real service.

## Prerequisites
- .NET 8 SDK
- Running instance of the fake AKV service (HTTPS). Set `FAKE_AKV_BASE_URL` to point to it; defaults to `https://127.0.0.1:8443`.
- The tests skip TLS validation to work with self-signed certificates and disable the SDK's resource verification to allow non-`*.vault.azure.net` hosts.

## Running

From the repository root:

```bash
dotnet test dotnet-tests/FakeAkv.IntegrationTests/FakeAkv.IntegrationTests.csproj
```

Testcontainers requires Docker to be available locally. The suite builds the repo's `dockerfile` and mounts a temporary
self-signed certificate into the container for HTTPS.
