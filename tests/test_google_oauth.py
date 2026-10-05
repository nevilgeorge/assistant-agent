from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from assistant_agent import google_oauth as oauth


@pytest.mark.parametrize("fails", [False, True])
def test_token_exchange_uses_timeout_and_closes_transport(monkeypatch, fails):
    flow = SimpleNamespace(fetch_token=Mock(), oauth2session=Mock(), credentials=object())
    if fails:
        flow.fetch_token.side_effect = ValueError("exchange failed")
    monkeypatch.setattr(oauth, "build_flow", lambda **kwargs: flow)
    if fails:
        with pytest.raises(ValueError, match="exchange failed"):
            oauth.exchange_code("https://app.test/callback", "state", "verifier")
    else:
        assert (
            oauth.exchange_code("https://app.test/callback", "state", "verifier")
            is flow.credentials
        )
    flow.fetch_token.assert_called_once_with(
        authorization_response="https://app.test/callback", timeout=10
    )
    flow.oauth2session.close.assert_called_once()


@pytest.mark.parametrize("operation", ["refresh", "identity"])
def test_indirect_google_requests_use_timeout_and_close_transport(monkeypatch, operation):
    session = MagicMock()
    session.__enter__.return_value = session
    session.request.return_value = SimpleNamespace(status_code=200, content=b"{}", headers={})
    monkeypatch.setattr(oauth.requests, "Session", lambda: session)

    def network(request):
        request("https://google.test/resource", timeout=120)

    if operation == "refresh":
        creds = SimpleNamespace(refresh=network)
        oauth.refresh_credentials(creds)
    else:
        monkeypatch.setattr(
            oauth, "get_settings", lambda: SimpleNamespace(google_client_id="client")
        )

        def verify(token, request, audience, **kwargs):
            network(request)
            return {
                "email": "a@example.com",
                "sub": "sub",
                "email_verified": True,
                "nonce": "nonce",
            }

        monkeypatch.setattr(oauth.id_token, "verify_oauth2_token", verify)
        assert oauth.account_identity(
            SimpleNamespace(id_token="signed"), expected_nonce="nonce"
        ) == ("a@example.com", "sub")
    assert session.request.call_args.kwargs["timeout"] == 10
    session.__exit__.assert_called_once()
