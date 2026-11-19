from http import HTTPMethod

import httpx2


def send_request(
    *, method: HTTPMethod, url: str, headers: dict[str, str], payload: object = None, form: dict[str, str] | None = None
) -> httpx2.Response:
    with httpx2.Client(timeout=5.0, follow_redirects=False, trust_env=False) as client:
        return client.request(method, url, headers=headers, json=payload, data=form).raise_for_status()
