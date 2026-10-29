"""Small Controller client with bounded requests and no response-body logging."""

from urllib.parse import urljoin, urlparse
import re
import time

import requests


class ControllerError(RuntimeError):
    pass


class Controller:
    def __init__(
        self,
        base,
        username=None,
        password=None,
        token=None,
        session=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        parsed = urlparse(base)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise ControllerError(
                "Controller URL must be HTTPS without embedded credentials"
            )
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ControllerError(
                "Supply the Controller origin URL without a path, query or fragment"
            )
        self.base = base.rstrip("/")
        self.session = session or requests.Session()
        self.clock, self.sleep = clock, sleep
        self.session.trust_env = False
        if token:
            self.session.headers["Authorization"] = "Bearer " + token
        else:
            self.session.auth = (username, password)

    def request(self, method, path, data=None, missing_ok=False):
        url = urljoin(self.base + "/", path)
        if (
            urlparse(url).netloc != urlparse(self.base).netloc
            or urlparse(url).scheme != "https"
        ):
            raise ControllerError("Controller response attempted to change API origin")
        try:
            response = self.session.request(
                method, url, json=data, timeout=(10, 60), allow_redirects=False
            )
        except requests.RequestException:
            raise ControllerError(
                "Controller connection or TLS verification failed"
            ) from None
        if missing_ok and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            detail = ""
            if (
                response.status_code == 400
                and isinstance(data, dict)
                and isinstance(data.get("inputs"), dict)
            ):
                try:
                    rejected = response.json().get("inputs", {})
                except (ValueError, AttributeError):
                    rejected = {}
                # Validation messages can contain submitted secrets. Publish field IDs only.
                fields = sorted(
                    name
                    for name in data["inputs"]
                    if name
                    in {
                        "host",
                        "username",
                        "password",
                        "oauth_token",
                        "verify_ssl",
                        "request_timeout",
                        "ssh_key_data",
                        "ssh_key_unlock",
                    }
                    and re.search(r"\b" + re.escape(name) + r"\b", str(rejected))
                )
                if fields:
                    detail = "; rejected credential input fields: " + ", ".join(fields)
            raise ControllerError(
                f"Controller API {method} returned HTTP {response.status_code}" + detail
            )
        # Inventory deletion is asynchronous: Controller returns an empty 202.
        if method == "DELETE" or response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError:
            raise ControllerError("Controller API returned non-JSON data") from None

    def credential_inputs(self):
        inputs = {"host": self.base, "verify_ssl": True}
        bearer = self.session.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            inputs["oauth_token"] = bearer[7:]
        else:
            inputs.update(
                username=self.session.auth[0],
                password=self.session.auth[1],
                oauth_token="",
            )
        return inputs

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, data=None):
        return self.request("POST", path, data or {})

    def patch(self, path, data):
        return self.request("PATCH", path, data)

    def delete(self, path, timeout=120):
        accepted = self.request("DELETE", path, missing_ok=True)
        if accepted is None:
            return None
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if self.get(path, missing_ok=True) is None:
                return {}
            self.sleep(2)
        raise ControllerError(
            "Controller resource deletion did not complete before timeout"
        )

    def list(self, path):
        rows = []
        seen = set()
        while path:
            if path in seen:
                raise ControllerError("Controller pagination cycle")
            seen.add(path)
            document = self.get(path)
            rows.extend(document["results"])
            path = document.get("next")
        return rows

    def collection(self, name):
        return self.list(f"/api/controller/v2/{name}/?page_size=200")

    def unique(self, collection, name, organization=None):
        rows = [
            row
            for row in self.collection(collection)
            if row["name"] == name
            and (
                organization is None
                or row.get(
                    "organization",
                    row.get("summary_fields", {}).get("organization", {}).get("id"),
                )
                == organization
            )
        ]
        if len(rows) != 1:
            raise ControllerError(
                f"Expected exactly one {collection} object named {name}"
            )
        return rows[0]
