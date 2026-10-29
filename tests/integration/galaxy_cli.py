"""Run Galaxy normally, adding safe endpoint diagnostics for HTTP failures."""

import io
import json
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit


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

    ansible.galaxy.api.open_url = instrument(ansible.galaxy.api.open_url)
    ansible.galaxy.token.open_url = instrument(ansible.galaxy.token.open_url)
    galaxy(["ansible-galaxy", *sys.argv[1:]])


if __name__ == "__main__":
    main()
