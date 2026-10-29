"""Run Galaxy normally, adding safe endpoint diagnostics for HTTP failures."""

import io
import base64
import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit


def token_kind(value):
    if not value:
        return "missing"
    try:
        payload = value.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        kind = claims.get("typ")
        return kind if kind in ("Offline", "Refresh", "Bearer") else "unclassified"
    except (ValueError, IndexError, AttributeError):
        return "unclassified"


def public_error(body):
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "unclassified"
    if not isinstance(data, dict):
        return "unclassified"
    if data.get("error") in (
        "invalid_grant",
        "invalid_client",
        "invalid_request",
        "invalid_token",
        "insufficient_scope",
    ):
        return data["error"]
    return "unclassified"


def instrument(original):
    def request(url, *args, **kwargs):
        try:
            return original(url, *args, **kwargs)
        except HTTPError as error:
            body = error.read()
            location = urlsplit(url)
            if location.hostname in (
                "sso.redhat.com",
                "cloud.redhat.com",
                "console.redhat.com",
            ):
                print(
                    "Hub HTTP diagnostic: "
                    + json.dumps(
                        {
                            "host": location.hostname,
                            "path": location.path,
                            "status": error.code,
                            "error": public_error(body),
                        }
                    ),
                    flush=True,
                )
            raise HTTPError(
                error.url, error.code, error.msg, error.hdrs, io.BytesIO(body)
            ) from error

    return request


def main():
    import ansible.galaxy.api
    import ansible.galaxy.token
    from ansible.cli.galaxy import main as galaxy

    original_get = ansible.galaxy.token.KeycloakToken.get

    def authenticated(token):
        if getattr(token, "_token", None):
            return original_get(token)
        supplied = [
            os.environ.get("ANSIBLE_GALAXY_SERVER_" + name + "_TOKEN")
            for name in ("CERTIFIED", "VALIDATED")
        ]
        print(
            "Hub auth selection: "
            + json.dumps(
                {
                    "token_kind": token_kind(token.access_token),
                    "matches_build_input": bool(token.access_token)
                    and token.access_token in supplied,
                    "client_id": "cloud-services"
                    if token.client_id == "cloud-services"
                    else "other",
                }
            ),
            flush=True,
        )
        return original_get(token)

    ansible.galaxy.token.KeycloakToken.get = authenticated

    ansible.galaxy.api.open_url = instrument(ansible.galaxy.api.open_url)
    ansible.galaxy.token.open_url = instrument(ansible.galaxy.token.open_url)
    galaxy(["ansible-galaxy", *sys.argv[1:]])


if __name__ == "__main__":
    main()
