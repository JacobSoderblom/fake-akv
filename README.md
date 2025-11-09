# Fake Azure Key Vault (Secrets)

A lightweight, HTTPS-only mock of **Azure Key Vault – Secrets API**, designed for **local development**, **integration testing**, and **CI/CD pipelines**.  
Implements secret versioning, soft delete, and standard API structure, compatible with official Azure SDKs for Python and .NET.

---

## Container Image

Published image:  

```
ghcr.io/jacobsoderblom/fake-akv:latest
```

---

## Quick Start (Local HTTPS)

Generate a self-signed certificate:

```bash
mkdir -p certs data
openssl req -x509 -newkey rsa:2048 -nodes   -keyout certs/dev.key -out certs/dev.crt -days 365   -subj "/CN=localhost"
```

Run:

```bash
docker run --rm -p 8443:8443   -v "$(pwd)/data:/data"   -v "$(pwd)/certs:/certs:ro"  -e FAKE_AKV_REQUIRE_AUTH=true  -e FAKE_AKV_SSL_CERTFILE=/certs/dev.crt   -e FAKE_AKV_SSL_KEYFILE=/certs/dev.key   ghcr.io/<your-org>/fake-akv:latest
```

Service is available at:  
`https://localhost:8443`

---

## Configuration

| Environment Variable | Description | Default |
|-----------------------|-------------|----------|
| `FAKE_AKV_STORAGE` | Storage backend (`sqlite` or `memory`) | `sqlite` |
| `FAKE_AKV_SQLITE_PATH` | Path to SQLite file | `/data/akv.sqlite` |
| `FAKE_AKV_REQUIRE_AUTH` | Enforce bearer token auth challenge | `true` |
| `FAKE_AKV_SSL_CERTFILE` | Path to TLS certificate (required) | — |
| `FAKE_AKV_SSL_KEYFILE` | Path to TLS private key (required) | — |
| `PORT` | HTTPS port | `8443` |

---

## Kubernetes Deployment

**1. Create a TLS Secret**

```bash
kubectl create secret tls fake-akv-cert   --cert=certs/dev.crt   --key=certs/dev.key
```

**2. Create a PersistentVolumeClaim (optional)**

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: fake-akv-data
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
```

**3. Deploy the service**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fake-akv
spec:
  replicas: 1
  selector:
    matchLabels:
      app: fake-akv
  template:
    metadata:
      labels:
        app: fake-akv
    spec:
      containers:
        - name: fake-akv
          image: ghcr.io/<your-org>/fake-akv:latest
          ports:
            - containerPort: 8443
          env:
            - name: FAKE_AKV_REQUIRE_AUTH
              value: true
            - name: FAKE_AKV_SSL_CERTFILE
              value: /certs/tls.crt
            - name: FAKE_AKV_SSL_KEYFILE
              value: /certs/tls.key
            - name: FAKE_AKV_STORAGE
              value: sqlite
            - name: FAKE_AKV_SQLITE_PATH
              value: /data/akv.sqlite
          volumeMounts:
            - name: certs
              mountPath: /certs
              readOnly: true
            - name: data
              mountPath: /data
      volumes:
        - name: certs
          secret:
            secretName: fake-akv-cert
        - name: data
          persistentVolumeClaim:
            claimName: fake-akv-data
---
apiVersion: v1
kind: Service
metadata:
  name: fake-akv
spec:
  ports:
    - port: 8443
      targetPort: 8443
  selector:
    app: fake-akv
```

**4. Access**

- Inside cluster: `https://fake-akv:8443`
- Outside: expose via `Ingress` or `kubectl port-forward`.

---

## Usage from Python

```python
from datetime import datetime, timedelta, timezone
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import RequestsTransport
from azure.keyvault.secrets import SecretClient

class FakeCredential:
    def get_token(self, *_, **__):
        exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        return AccessToken("fake-token", exp)

client = SecretClient(
    vault_url="https://localhost:8443",
    credential=FakeCredential(),
    transport=RequestsTransport(connection_verify=False),
    verify_challenge_resource=False,
)

client.set_secret("demo-secret", "example")
print(client.get_secret("demo-secret").value)
```

Notes:

- `connection_verify=False` skips TLS validation for self-signed certs.
- `verify_challenge_resource=False` is required for non-`*.vault.azure.net` hosts.

---

## Usage from C #

```csharp
using Azure;
using Azure.Core;
using Azure.Core.Pipeline;
using Azure.Security.KeyVault.Secrets;
using System;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;

sealed class FakeCredential : TokenCredential
{
    public override AccessToken GetToken(TokenRequestContext ctx, CancellationToken _) =>
        new AccessToken("fake-token", DateTimeOffset.UtcNow.AddHours(1));

    public override ValueTask<AccessToken> GetTokenAsync(TokenRequestContext ctx, CancellationToken _) =>
        ValueTask.FromResult(GetToken(ctx, _));
}

var handler = new HttpClientHandler
{
    ServerCertificateCustomValidationCallback = HttpClientHandler.DangerousAcceptAnyServerCertificateValidator
};
var transport = new HttpClientTransport(new HttpClient(handler));

var options = new SecretClientOptions
{
    Transport = transport,
    DisableChallengeResourceVerification = true
};

var client = new SecretClient(new Uri("https://localhost:8443"), new FakeCredential(), options);
client.SetSecret("demo-secret", "example");
Console.WriteLine(client.GetSecret("demo-secret").Value.Value);
```

---

## Notes

- Intended for **development and testing only**.
- Does not implement Azure RBAC, encryption at rest, or networking features.
- For realistic integration, map a host like `myvault.vault.azure.net` to `127.0.0.1` and generate a certificate for that domain.
