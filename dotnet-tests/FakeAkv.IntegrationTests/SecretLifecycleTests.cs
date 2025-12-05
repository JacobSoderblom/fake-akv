using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using Azure;
using Azure.Core.Pipeline;
using Azure.Security.KeyVault.Secrets;
using FakeAkv.IntegrationTests.Credentials;
using Xunit;

namespace FakeAkv.IntegrationTests;

sealed class SecretClientFixture : IAsyncLifetime
{
    private readonly HttpClientHandler _baseHandler;
    private readonly FollowRedirectWithAuthHandler _authHandler;

    public HttpClient HttpClient { get; }

    public SecretClient Client { get; }

    public Uri BaseUri { get; }

    public SecretClientFixture()
    {
        var baseUrl = Environment.GetEnvironmentVariable("FAKE_AKV_BASE_URL") ?? "https://127.0.0.1:8443";
        BaseUri = new Uri(baseUrl);

        _baseHandler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = HttpClientHandler.DangerousAcceptAnyServerCertificateValidator,
        };

        _authHandler = new FollowRedirectWithAuthHandler("fake-token")
        {
            InnerHandler = _baseHandler,
        };

        HttpClient = new HttpClient(_authHandler, disposeHandler: false);

        var options = new SecretClientOptions
        {
            Transport = new HttpClientTransport(HttpClient),
        };
        options.DisableChallengeResourceVerification = true;

        Client = new SecretClient(BaseUri, new FakeCredential(), options);
    }

    public Task InitializeAsync() => Task.CompletedTask;

    public Task DisposeAsync()
    {
        Client.Dispose();
        HttpClient.Dispose();
        _authHandler.Dispose();
        _baseHandler.Dispose();
        return Task.CompletedTask;
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
        Assert.EndsWith($"/secrets/{name}/{setResult.Value.Properties.Version}", got.Value.Id?.ToString());
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
        Assert.False(string.IsNullOrEmpty(deleted.Value.RecoveryId));

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
        var initialTags = new Dictionary<string, string>
        {
            ["env"] = "dev",
            ["team"] = "qa",
        };

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

        var listed = await _fixture.Client.GetPropertiesOfSecretsAsync()
            .Where(s => s.Name == name)
            .FirstOrDefaultAsync();
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

        using var resp = await _fixture.HttpClient.GetAsync(uriBuilder.Uri);
        resp.EnsureSuccessStatusCode();

        using var payload = await JsonDocument.ParseAsync(await resp.Content.ReadAsStreamAsync());
        var value = payload.RootElement.GetProperty("value");
        Assert.Equal(JsonValueKind.Array, value.ValueKind);

        var ids = value.EnumerateArray()
            .Select(item => item.TryGetProperty("id", out var id) ? id.GetString() ?? string.Empty : string.Empty)
            .ToList();
        Assert.Contains(ids, id => id.Contains($"/secrets/{name}", StringComparison.OrdinalIgnoreCase));

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
