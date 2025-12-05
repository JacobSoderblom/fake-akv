using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using Azure.Core;
using Azure.Core.Pipeline;

namespace FakeAkv.IntegrationTests.Credentials;

/// <summary>
/// Simple bearer credential for local/fake Key Vault deployments.
/// </summary>
public sealed class FakeCredential : TokenCredential
{
    public override AccessToken GetToken(TokenRequestContext _, CancellationToken __) =>
        new("fake-token", DateTimeOffset.UtcNow.AddHours(1));

    public override ValueTask<AccessToken> GetTokenAsync(
        TokenRequestContext _,
        CancellationToken __
    ) => ValueTask.FromResult(new AccessToken("fake-token", DateTimeOffset.UtcNow.AddHours(1)));
}

/// <summary>
/// HTTP handler that follows redirects while stamping a static bearer token.
/// </summary>
public sealed class FollowRedirectWithAuthHandler : DelegatingHandler
{
    private readonly AuthenticationHeaderValue _auth;
    private const int MaxRedirects = 5;

    public FollowRedirectWithAuthHandler(string token) =>
        _auth = new AuthenticationHeaderValue("Bearer", token);

    protected override async Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken ct
    )
    {
        int redirects = 0;

        // Always stamp auth header before sending
        request.Headers.Remove("Authorization");
        request.Headers.Authorization = _auth;

        HttpResponseMessage resp = await base.SendAsync(request, ct);

        while (
            (
                resp.StatusCode == HttpStatusCode.TemporaryRedirect
                || (int)resp.StatusCode == 308
            )
            && redirects++ < MaxRedirects
        )
        {
            if (!resp.Headers.Location?.IsAbsoluteUri ?? true)
                break;

            var next = new HttpRequestMessage(request.Method, resp.Headers.Location);

            foreach (var hdr in request.Headers)
                next.Headers.TryAddWithoutValidation(hdr.Key, hdr.Value);

            if (
                request.Content is not null
                && request.Method != HttpMethod.Get
                && request.Method != HttpMethod.Head
            )
                next.Content = await CloneContentAsync(request.Content, ct);

            next.Headers.Remove("Authorization");
            next.Headers.Authorization = _auth;

            resp.Dispose();
            resp = await base.SendAsync(next, ct);
            request = next;
        }

        return resp;
    }

    private static async Task<HttpContent> CloneContentAsync(
        HttpContent content,
        CancellationToken ct
    )
    {
        var ms = new MemoryStream();
        await content.CopyToAsync(ms, ct);
        ms.Position = 0;
        var clone = new StreamContent(ms);
        foreach (var h in content.Headers)
            clone.Headers.TryAddWithoutValidation(h.Key, h.Value);
        return clone;
    }
}
