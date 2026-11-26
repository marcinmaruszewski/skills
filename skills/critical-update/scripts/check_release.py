#!/usr/bin/env python3
"""Check whether a release of a package or image is published.

Usage:
  check_release.py <ecosystem> <package> <version>     # is this version published?
  check_release.py <ecosystem> <package> --latest [P]  # latest stable version, optionally with prefix P

Ecosystems:
  packagist  laravel/framework, symfony/http-kernel    (Composer)
  npm        next, @angular/core
  pypi       django
  docker     php, library/php, bitnami/redis            (Docker Hub; version = full tag, e.g. 8.5.23-fpm-alpine)
  github     owner/repo                                 (release or tag; a leading "v" is ignored)

Exit status: 0 published (or latest found), 1 not published (or nothing matches), 2 error.
Stdlib only; reads GITHUB_TOKEN from the environment for the github ecosystem when set.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 20
USER_AGENT = "check-release/1.0"
STABLE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


class CheckError(Exception):
    pass


def get_json(url: str, headers: dict[str, str] | None = None) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CheckError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CheckError(f"network error: {getattr(exc, 'reason', exc)}") from exc


def norm(version: str) -> str:
    return version[1:] if version[:1] in "vV" and version[1:2].isdigit() else version


def key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in norm(version).split("."))


def versions(ecosystem: str, package: str) -> list[str]:
    """All published version strings (for docker: tags) of a package."""
    if ecosystem == "packagist":
        data = get_json(f"https://repo.packagist.org/p2/{package}.json")
        if not data:
            raise CheckError(f"package not found: {package}")
        return [v["version"] for v in data["packages"].get(package, [])]
    if ecosystem == "npm":
        data = get_json(f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}")
        if not data:
            raise CheckError(f"package not found: {package}")
        return list(data.get("versions", {}))
    if ecosystem == "pypi":
        data = get_json(f"https://pypi.org/pypi/{package}/json")
        if not data:
            raise CheckError(f"package not found: {package}")
        return list(data.get("releases", {}))
    if ecosystem == "github":
        headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}
        found: list[str] = []
        for kind in ("releases", "tags"):
            data = get_json(f"https://api.github.com/repos/{package}/{kind}?per_page=100", headers)
            if data is None:
                raise CheckError(f"repository not found: {package}")
            found += [item.get("tag_name") or item.get("name") for item in data if not item.get("draft")]
        return [norm(v) for v in found if v]
    raise CheckError(f"unknown ecosystem: {ecosystem}")


def docker_repo(package: str) -> str:
    return package if "/" in package else f"library/{package}"


def docker_has_tag(package: str, tag: str) -> bool:
    return get_json(f"https://hub.docker.com/v2/repositories/{docker_repo(package)}/tags/{urllib.parse.quote(tag)}") is not None


def docker_tags(package: str, prefix: str) -> list[str]:
    url = f"https://hub.docker.com/v2/repositories/{docker_repo(package)}/tags?page_size=100"
    if prefix:
        url += f"&name={urllib.parse.quote(prefix)}"
    tags: list[str] = []
    for _ in range(10):  # 1000 tags is plenty for one prefix
        data = get_json(url)
        if not data:
            break
        tags += [t["name"] for t in data.get("results", [])]
        url = data.get("next")
        if not url:
            break
    return tags


def latest(ecosystem: str, package: str, prefix: str) -> str | None:
    found = docker_tags(package, prefix) if ecosystem == "docker" else versions(ecosystem, package)
    stable = [norm(v) for v in found if STABLE.match(v)]
    if prefix:
        stable = [v for v in stable if v == prefix or v.startswith(prefix.rstrip(".") + ".")]
    return max(stable, key=key, default=None)


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    ecosystem, package, arg = argv[0], argv[1], argv[2]
    try:
        if arg == "--latest":
            prefix = argv[3] if len(argv) > 3 else ""
            found = latest(ecosystem, package, prefix)
            if found is None:
                print(f"none {ecosystem} {package} {prefix or '(any)'}")
                return 1
            print(f"latest {ecosystem} {package} {found}")
            return 0
        if ecosystem == "docker":
            ok = docker_has_tag(package, arg)
        else:
            ok = norm(arg) in {norm(v) for v in versions(ecosystem, package)}
    except CheckError as exc:
        print(f"error {ecosystem} {package}: {exc}")
        return 2
    print(f"{'available' if ok else 'missing'} {ecosystem} {package} {arg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
