"""HTTP routes for the tests, shared by in-process tests and generator subprocesses.

A route is a JSON-serialisable dict:
    {"method": "GET", "url": "https://...", "query": {"page": "1"} or None,
     "status": 200, "headers": {...}, and one of "json", "body" (text) or "body_b64"}

Without "query", a route matches the URL with any query string. Requests that
match no route fail with a ConnectionError, so nothing reaches the network.
"""
import base64
import json

import responses
from responses import matchers


def add_route(mock, route):
    kwargs = {
        "method": route.get("method", "GET"),
        "url": route["url"],
        "status": route.get("status", 200),
        "headers": route.get("headers") or {},
    }
    if route.get("query") is not None:
        kwargs["match"] = [matchers.query_param_matcher({k: str(v) for k, v in route["query"].items()})]
    if "json" in route:
        kwargs["json"] = route["json"]
    elif "body_b64" in route:
        kwargs["body"] = base64.b64decode(route["body_b64"])
    else:
        kwargs["body"] = route.get("body", "")
    mock.add(**kwargs)


class Routes:
    """Builds a list of routes, to install in-process or write for a subprocess."""

    def __init__(self):
        self.routes = []

    def json(self, url, data, query=None, status=200, headers=None):
        self.routes.append({"url": url, "query": query, "status": status, "headers": headers, "json": data})
        return self

    def body(self, url, data=b"", query=None, status=200, headers=None):
        self.routes.append({"url": url, "query": query, "status": status, "headers": headers,
                            "body_b64": base64.b64encode(data).decode("ascii")})
        return self

    def install(self, mock):
        for route in self.routes:
            add_route(mock, route)
        return mock

    def write(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"routes": self.routes}, f)
        return path


def start_from_file(routes_path, log_dir=None):
    """Start mocking every requests call in this process (used by sitecustomize)."""
    import atexit
    import os

    with open(routes_path, encoding="utf-8") as f:
        routes = json.load(f)["routes"]
    mock = responses.RequestsMock(assert_all_requests_are_fired=False)
    mock.start()
    for route in routes:
        add_route(mock, route)

    def finish():
        if log_dir:
            calls = [{"method": call.request.method, "url": call.request.url,
                      "headers": dict(call.request.headers)} for call in mock.calls]
            with open(os.path.join(log_dir, f"{os.getpid()}.json"), "w", encoding="utf-8") as f:
                json.dump(calls, f)
        mock.stop(allow_assert=False)

    atexit.register(finish)
    return mock
