using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Net.Http.Headers;
using Azure.Core.Pipeline;
using Azure.Security.KeyVault.Secrets;
using FakeAkv.IntegrationTests.Credentials;
using DotNet.Testcontainers.Builders;
using DotNet.Testcontainers.Configurations;
using DotNet.Testcontainers.Containers;
using DotNet.Testcontainers.Images;
using Xunit;

namespace FakeAkv.IntegrationTests;

public sealed class SecretClientFixture : IAsyncLifetime
{
    private readonly HttpClientHandler _baseHandler;
    private readonly LoggingHandler _logHandler;
    private IContainer? _container;
    private string? _certDirectory;

    public HttpClient HttpClient { get; private set; } = default!;

    public SecretClient Client { get; private set; } = default!;

    public Uri BaseUri { get; private set; } = default!;

    public SecretClientFixture()
    {
        TestcontainersSettings.ResourceReaperEnabled = false;

        var baseUrl =
            Environment.GetEnvironmentVariable("FAKE_AKV_BASE_URL") ?? "https://127.0.0.1:8443";

        BaseUri = new Uri(baseUrl);

        _baseHandler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback =
                HttpClientHandler.DangerousAcceptAnyServerCertificateValidator,
        };

        _logHandler = new LoggingHandler { InnerHandler = _baseHandler };
    }

    public async Task InitializeAsync()
    {
        _certDirectory = Directory.CreateTempSubdirectory("fake-akv-cert").FullName;
        var certFile = Path.Combine(_certDirectory, "dev.crt");
        var keyFile = Path.Combine(_certDirectory, "dev.key");
        WriteSelfSignedCertificate(certFile, keyFile);

        var repoRoot = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../../"));

        var image = new ImageFromDockerfileBuilder()
            .WithName($"fake-akv-tests:{Guid.NewGuid():N}")
            .WithDockerfileDirectory(repoRoot)
            .WithDockerfile("dockerfile")
            .Build();

        await image.CreateAsync();

        _container = new ContainerBuilder()
            .WithImage(image)
            .WithPortBinding(8443, true)
            .WithBindMount(_certDirectory, "/certs", AccessMode.ReadOnly)
            .WithEnvironment("FAKE_AKV_SSL_CERTFILE", "/certs/dev.crt")
            .WithEnvironment("FAKE_AKV_SSL_KEYFILE", "/certs/dev.key")
            .WithEnvironment("FAKE_AKV_REQUIRE_AUTH", "true")
            .WithWaitStrategy(Wait.ForUnixContainer().UntilPortIsAvailable(8443))
            .Build();

        await _container.StartAsync();

        var mappedPort = _container.GetMappedPublicPort(8443);
        BaseUri = new Uri($"https://localhost:{mappedPort}");

        HttpClient = new HttpClient(_logHandler, disposeHandler: false);

        var options = new SecretClientOptions { Transport = new HttpClientTransport(HttpClient) };
        options.DisableChallengeResourceVerification = true;

        Client = new SecretClient(BaseUri, new FakeCredential(), options);
    }

    public Task DisposeAsync()
    {
        HttpClient?.Dispose();
        _logHandler.Dispose();
        _baseHandler.Dispose();

        if (_container is not null)
        {
            return DisposeContainerAsync();
        }

        CleanupCerts();
        return Task.CompletedTask;
    }

    private async Task DisposeContainerAsync()
    {
        await _container!.DisposeAsync();
        CleanupCerts();
    }

    private void CleanupCerts()
    {
        if (!string.IsNullOrEmpty(_certDirectory) && Directory.Exists(_certDirectory))
        {
            try
            {
                Directory.Delete(_certDirectory, recursive: true);
            }
            catch
            {
                // ignore cleanup failures
            }
        }
    }

    private static void WriteSelfSignedCertificate(string certPath, string keyPath)
    {
        using var rsa = RSA.Create(2048);
        var request = new CertificateRequest(
            "CN=fake-akv-test",
            rsa,
            HashAlgorithmName.SHA256,
            RSASignaturePadding.Pkcs1
        );

        // Use a self-signed cert that does not require accessing the platform keychain.
        var serialNumber = RandomNumberGenerator.GetBytes(16);
        serialNumber[^1] |= 1; // ensure non-zero serial
        var certificate = request.Create(
            request.SubjectName,
            X509SignatureGenerator.CreateForRSA(rsa, RSASignaturePadding.Pkcs1),
            DateTimeOffset.UtcNow.AddMinutes(-5),
            DateTimeOffset.UtcNow.AddHours(1),
            serialNumber
        );

        var certPem = ExportCertificatePem(certificate);
        File.WriteAllText(certPath, certPem);

        var keyPem = ExportPrivateKeyPem(rsa);
        File.WriteAllText(keyPath, keyPem);
    }

    private static string ExportCertificatePem(X509Certificate2 certificate)
    {
        var builder = new StringBuilder();
        builder.AppendLine("-----BEGIN CERTIFICATE-----");
        builder.AppendLine(
            Convert.ToBase64String(
                certificate.Export(X509ContentType.Cert),
                Base64FormattingOptions.InsertLineBreaks
            )
        );
        builder.AppendLine("-----END CERTIFICATE-----");
        return builder.ToString();
    }

    private static string ExportPrivateKeyPem(RSA rsa)
    {
        var builder = new StringBuilder();
        builder.AppendLine("-----BEGIN PRIVATE KEY-----");
        builder.AppendLine(
            Convert.ToBase64String(
                rsa.ExportPkcs8PrivateKey(),
                Base64FormattingOptions.InsertLineBreaks
            )
        );
        builder.AppendLine("-----END PRIVATE KEY-----");
        return builder.ToString();
    }
}

public class SecretLifecycleTests : IClassFixture<SecretClientFixture>
{
    private readonly SecretClientFixture _fixture;

    public SecretLifecycleTests(SecretClientFixture fixture) => _fixture = fixture;

    [Fact]
    public async Task SetAndGetSecret()
    {
        var name = $"it-{Guid.NewGuid():N}"[..11];
        const string expectedValue = "hello-world";

        var setResult = await _fixture.Client.SetSecretAsync(name, expectedValue);
        Assert.Equal(name, setResult.Value.Name);

        var got = await _fixture.Client.GetSecretAsync(name);
        Assert.Equal(expectedValue, got.Value.Value);
        Assert.NotNull(got.Value.Id);
        Assert.EndsWith(
            $"/secrets/{name}/{setResult.Value.Properties.Version}",
            got.Value.Id?.ToString()
        );
    }

    [Fact]
    public async Task VersioningAndListVersions()
    {
        var name = $"it-{Guid.NewGuid():N}"[..11];

        var v1 = (await _fixture.Client.SetSecretAsync(name, "v1")).Value.Properties.Version;
        var v2 = (await _fixture.Client.SetSecretAsync(name, "v2")).Value.Properties.Version;

        Assert.NotEqual(v1, v2);

        var seenVersions = new HashSet<string>();
        await foreach (var version in _fixture.Client.GetPropertiesOfSecretVersionsAsync(name))
        {
            if (version.Version is not null)
            {
                seenVersions.Add(version.Version);
            }
        }

        Assert.Contains(v1, seenVersions);
        Assert.Contains(v2, seenVersions);
    }

    [Fact]
    public async Task DeleteAndRecover()
    {
        var name = $"it-{Guid.NewGuid():N}"[..11];
        const string value = "to-delete";

        await _fixture.Client.SetSecretAsync(name, value);

        var deleteOp = await _fixture.Client.StartDeleteSecretAsync(name);
        var deleted = await deleteOp.WaitForCompletionAsync();
        Assert.NotNull(deleted.Value.RecoveryId);

        var deletedSecret = await _fixture.Client.GetDeletedSecretAsync(name);
        Assert.Equal(name, deletedSecret.Value.Name);

        var recoverOp = await _fixture.Client.StartRecoverDeletedSecretAsync(name);
        var recovered = await recoverOp.WaitForCompletionAsync();
        Assert.Equal(name, recovered.Value.Name);

        var got = await _fixture.Client.GetSecretAsync(name);
        Assert.Equal(value, got.Value.Value);
    }

    [Fact]
    public async Task TagsLifecycle()
    {
        var name = $"it-{Guid.NewGuid():N}"[..11];
        var initialTags = new Dictionary<string, string> { ["env"] = "dev", ["team"] = "qa" };

        var secret = new KeyVaultSecret(name, "tagged-value");
        foreach (var tag in initialTags)
        {
            secret.Properties.Tags[tag.Key] = tag.Value;
        }

        var created = (await _fixture.Client.SetSecretAsync(secret)).Value;
        Assert.Equal(initialTags, created.Properties.Tags);

        var fetched = await _fixture.Client.GetSecretAsync(name);
        Assert.Equal(initialTags, fetched.Value.Properties.Tags);
        Assert.Equal("tagged-value", fetched.Value.Value);

        SecretProperties? listed = null;
        await foreach (var secretProps in _fixture.Client.GetPropertiesOfSecretsAsync())
        {
            if (secretProps.Name == name)
            {
                listed = secretProps;
                break;
            }
        }
        Assert.NotNull(listed);
        Assert.Equal(initialTags, listed!.Tags);

        var updated = created.Properties;
        updated.Tags["env"] = "prod";
        updated.Tags.Remove("team");

        var updatedResult = await _fixture.Client.UpdateSecretPropertiesAsync(updated);
        Assert.Equal("prod", updatedResult.Value.Tags["env"]);
        Assert.False(updatedResult.Value.Tags.ContainsKey("team"));

        var refreshed = await _fixture.Client.GetSecretAsync(name);
        Assert.Equal("prod", refreshed.Value.Properties.Tags["env"]);
        Assert.False(refreshed.Value.Properties.Tags.ContainsKey("team"));
        Assert.Equal("tagged-value", refreshed.Value.Value);

        var uriBuilder = new UriBuilder(_fixture.BaseUri)
        {
            Path = "secrets",
            Query = "api-version=7.4&tag-name=env&tag-value=prod",
        };

        using var request = new HttpRequestMessage(HttpMethod.Get, uriBuilder.Uri);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", "fake-token");

        using var resp = await _fixture.HttpClient.SendAsync(request);
        resp.EnsureSuccessStatusCode();

        using var payload = await JsonDocument.ParseAsync(await resp.Content.ReadAsStreamAsync());
        var value = payload.RootElement.GetProperty("value");
        Assert.Equal(JsonValueKind.Array, value.ValueKind);

        var ids = value
            .EnumerateArray()
            .Select(item =>
                item.TryGetProperty("id", out var id)
                    ? id.GetString() ?? string.Empty
                    : string.Empty
            )
            .ToList();
        Assert.Contains(
            ids,
            id => id.Contains($"/secrets/{name}", StringComparison.OrdinalIgnoreCase)
        );

        foreach (var item in value.EnumerateArray())
        {
            if (!item.TryGetProperty("tags", out var tags))
            {
                throw new InvalidOperationException("Missing tags payload");
            }

            Assert.True(tags.TryGetProperty("env", out var envTag));
            Assert.Equal("prod", envTag.GetString());
        }
    }
}
