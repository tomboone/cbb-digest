from collections.abc import Iterator

import httpx
import pytest
import respx

from cbb_digest.sources import http
from cbb_digest.sources.http import make_client

BASE = "https://api.example.com"


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(http.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    with make_client(BASE, user_agent="cbb-digest-test/1.0") as c:
        yield c


@respx.mock
def test_two_503s_then_200_succeeds_after_three_requests(
    client: httpx.Client, sleeps: list[float]
) -> None:
    route = respx.get(f"{BASE}/teams").mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json={})]
    )

    response = client.get("/teams")

    assert response.status_code == 200
    assert route.call_count == 3
    assert sleeps == [0.5, 1.0]


@respx.mock
def test_gives_up_after_three_retries_and_returns_last_5xx(
    client: httpx.Client, sleeps: list[float]
) -> None:
    route = respx.get(f"{BASE}/teams").mock(return_value=httpx.Response(502))

    response = client.get("/teams")

    assert response.status_code == 502
    assert route.call_count == 4
    assert sleeps == [0.5, 1.0, 2.0]


@respx.mock
def test_retries_connect_errors_then_reraises(client: httpx.Client) -> None:
    route = respx.get(f"{BASE}/teams").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(httpx.ConnectError):
        client.get("/teams")

    assert route.call_count == 4


@respx.mock
def test_connect_error_then_success(client: httpx.Client) -> None:
    route = respx.get(f"{BASE}/teams").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json={})]
    )

    assert client.get("/teams").status_code == 200
    assert route.call_count == 2


@respx.mock
def test_4xx_is_not_retried(client: httpx.Client, sleeps: list[float]) -> None:
    route = respx.get(f"{BASE}/teams").mock(return_value=httpx.Response(404))

    assert client.get("/teams").status_code == 404
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_read_timeout_is_not_retried(client: httpx.Client) -> None:
    route = respx.get(f"{BASE}/teams").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(httpx.ReadTimeout):
        client.get("/teams")

    assert route.call_count == 1


@respx.mock
def test_redirects_are_not_followed(client: httpx.Client) -> None:
    # Torvik's unpublished game logs 302 to `/`; the source client must see the redirect itself.
    respx.get(f"{BASE}/2027_all_advgames.json.gz").mock(
        return_value=httpx.Response(302, headers={"Location": "/"})
    )
    home = respx.get(f"{BASE}/").mock(return_value=httpx.Response(200))

    response = client.get("/2027_all_advgames.json.gz")

    assert response.status_code == 302
    assert home.call_count == 0


@respx.mock
def test_sends_user_agent_extra_headers_and_sets_timeout() -> None:
    route = respx.get(f"{BASE}/teams").mock(return_value=httpx.Response(200))

    with make_client(BASE, headers={"Authorization": "Bearer k"}) as c:
        c.get("/teams")
        assert c.timeout == httpx.Timeout(20.0)

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer k"
    assert request.headers["User-Agent"] == http.USER_AGENT


def test_default_user_agent_is_a_browser() -> None:
    assert http.USER_AGENT.startswith("Mozilla/5.0 ")
    assert "Chrome/" in http.USER_AGENT
