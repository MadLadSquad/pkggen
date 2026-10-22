#!/usr/bin/env python3
# Looks dependencies up on repology.org: what every project is called, and
# which version it has, in every release pkggen supports. Repology asks for at
# most one request a second and an identifying User-Agent. Its answers are
# kept in the HTTP cache for as long as its Cache-Control allows (an hour), so
# repeated runs don't ask again.
import time
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

import lib

API = "https://repology.org/api/v1/project/"
INTERVAL = 1.0      # seconds between requests that reach repology
RETRY_LIMIT = 10    # at most this many seconds of Retry-After are waited for, once


class Throttle(HTTPAdapter):
    """Spaces out the requests that reach the network; answers from the cache don't wait."""
    last = None

    def send(self, request, *args, **kwargs):
        if Throttle.last is not None:
            wait = INTERVAL - (time.monotonic() - Throttle.last)
            if wait > 0:
                time.sleep(wait)
        try:
            return super().send(request, *args, **kwargs)
        finally:
            Throttle.last = time.monotonic()


class Adapter(lib.fetch.CachingAdapter, Throttle):
    """The HTTP cache in front of the throttle: CachingAdapter's network requests go through Throttle."""


def session(user_agent):
    result = requests.Session()
    adapter = Adapter()
    result.mount("https://", adapter)
    result.mount("http://", adapter)
    result.headers["User-Agent"] = user_agent
    return result


def fetch_project(http, name):
    """Every package of a repology project, or [] when repology doesn't know it."""
    url = API + quote(name, safe="")
    for attempt in range(2):
        response = http.get(url, timeout=lib.TIMEOUT)
        if response.status_code in (429, 503) and attempt == 0:
            delay = response.headers.get("Retry-After", "")
            time.sleep(min(int(delay), RETRY_LIMIT) if delay.isdigit() else INTERVAL * 2)
            continue
        break
    if response.status_code == 404:
        return []
    if response.status_code != 200:
        raise lib.fetch.http_error(response, f"look '{name}' up on repology", secret_hint="nothing (repology has no keys)")
    try:
        data = response.json()
    except ValueError:
        raise lib.TinyError(f"repology's answer for '{name}' isn't JSON ({url})")
    return data if isinstance(data, list) else []


def main(job):
    user_agent = job.get("user_agent") or "pkggen"
    repositories = job.get("repositories") or []
    wildcards = job.get("wildcards") or []
    http = session(user_agent)
    projects, missing = {}, []
    names = list(dict.fromkeys(job["projects"]))
    for i, name in enumerate(names, 1):
        if len(names) > 1:
            lib.log(f"Looking up dependencies on repology ({i}/{len(names)}): {name}")
        found = lib.trim(fetch_project(http, name), repositories, wildcards)
        projects[name] = found
        if not found:
            missing.append(name)
    return {"projects": projects, "missing": missing, "warnings": []}


if __name__ == "__main__":
    lib.run(main)
