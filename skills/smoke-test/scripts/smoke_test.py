#!/usr/bin/env python3
"""Smoke-test websites: the home page plus random sitemap URLs must answer HTTP 200.

Usage:
  smoke_test.py <site URL> [<site URL> ...] [options]

For each site the script finds the sitemap (--sitemap, else sitemap.xml under the
site's path, then at the root, then the Sitemap: lines of robots.txt), follows sitemap
indexes, samples --count random URLs and requests them with redirects followed. The
final status must be 200.

When the home page carries cache headers (CloudFront, Varnish, Fastly, Cloudflare,
Age, Via), a second pass requests a fresh sample with ?cachebuster=<random>, so the
requests reach the origin. --cachebuster always|never forces the second pass on or off.

Exit status: 0 every URL answered 200, 1 some did not, 2 a site could not be tested.
Stdlib only.
"""

from __future__ import annotations

import argparse
import gzip
import random
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

USER_AGENT = "smoke-test/1.0"
MAX_CHILD_SITEMAPS = 10
CACHE_HEADERS = ("x-cache", "cf-cache-status", "x-varnish", "x-served-by", "x-cache-hits", "age", "via", "x-amz-cf-id")


class SiteError(Exception):
    pass


@dataclass
class Result:
    url: str
    status: int | str
    ms: int


def fetch(url: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, b""
    if headers.get("content-encoding") == "gzip" or url.endswith(".gz"):
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    return status, headers, body


def reason(exc: BaseException) -> str:
    text = str(getattr(exc, "reason", exc))
    if "CERTIFICATE_VERIFY_FAILED" in text:
        text += " (point SSL_CERT_FILE at your CA bundle; on macOS python.org builds run 'Install Certificates.command')"
    return text


def check(url: str, timeout: float) -> Result:
    start = time.monotonic()
    try:
        status, _, _ = fetch(url, timeout)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        status = f"error: {reason(exc)}"
    return Result(url, status, int((time.monotonic() - start) * 1000))


def locs(xml: bytes) -> tuple[str, list[str]]:
    """Return ('index'|'urlset', <loc> values) of a sitemap document."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise SiteError(f"sitemap is not XML: {exc}") from exc
    kind = "index" if root.tag.endswith("sitemapindex") else "urlset"
    return kind, [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def find_sitemaps(base: str, explicit: str | None, timeout: float) -> list[str]:
    if explicit:
        return [explicit]
    # Under the site's path first (https://example.com/pl/sitemap.xml), then at the root.
    for candidate in dict.fromkeys((urllib.parse.urljoin(base.rstrip("/") + "/", "sitemap.xml"),
                                    urllib.parse.urljoin(base, "/sitemap.xml"))):
        status, _, body = fetch(candidate, timeout)
        if status == 200 and (b"<urlset" in body[:2048] or b"<sitemapindex" in body[:2048]):
            return [candidate]
    status, _, body = fetch(urllib.parse.urljoin(base, "/robots.txt"), timeout)
    if status == 200:
        found = [line.split(":", 1)[1].strip() for line in body.decode("utf-8", "replace").splitlines()
                 if line.lower().startswith("sitemap:")]
        if found:
            return found
    raise SiteError("no sitemap.xml under the site path or at the root, none in robots.txt; pass --sitemap")


def page_urls(base: str, explicit: str | None, timeout: float, rng: random.Random) -> list[str]:
    queue, pages, seen = find_sitemaps(base, explicit, timeout), [], set()
    while queue and len(seen) < 1 + MAX_CHILD_SITEMAPS:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        status, _, body = fetch(url, timeout)
        if status != 200:
            raise SiteError(f"sitemap {url} answered {status}")
        kind, found = locs(body)
        if kind == "index":
            rng.shuffle(found)
            queue.extend(found)
        else:
            pages.extend(found)
    if not pages:
        raise SiteError("sitemap lists no pages")
    return pages


def with_cachebuster(url: str, token: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = f"{parts.query}&cachebuster={token}" if parts.query else f"cachebuster={token}"
    return urllib.parse.urlunsplit(parts._replace(query=query))


def run_pass(urls: list[str], parallel: int, timeout: float) -> list[Result]:
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        return list(pool.map(lambda u: check(u, timeout), urls))


def report(label: str, results: list[Result]) -> bool:
    bad = [r for r in results if r.status != 200]
    slowest = max((r.ms for r in results), default=0)
    print(f"  {label}: {len(results) - len(bad)}/{len(results)} OK, slowest {slowest} ms")
    for r in bad:
        print(f"    {r.status}  {r.ms} ms  {r.url}")
    return not bad


def test_site(site: str, args: argparse.Namespace, rng: random.Random) -> int:
    base = site if "://" in site else f"https://{site}"
    print(base)
    try:
        _, headers, _ = fetch(base, args.timeout)
        cached = sorted(h for h in CACHE_HEADERS if h in headers)
        pages = [p for p in dict.fromkeys(page_urls(base, args.sitemap, args.timeout, rng)) if p.rstrip("/") != base.rstrip("/")]
    except (SiteError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        print(f"  error: {reason(exc)}")
        return 2
    rng.shuffle(pages)
    first, rest = pages[: args.count], pages[args.count :]
    ok = report("pass 1", run_pass([base, *first], args.parallel, args.timeout))
    second = args.cachebuster == "always" or (args.cachebuster == "auto" and cached)
    if second:
        sample = rest[: args.count]
        sample += rng.sample(first, min(len(first), args.count - len(sample)))  # small sitemap: reuse pass 1 URLs
        token = secrets.token_hex(4)
        note = f"cache headers: {', '.join(cached)}" if cached else "forced"
        ok &= report(f"pass 2, cachebuster={token} ({note})",
                     run_pass([with_cachebuster(u, token) for u in [base, *sample]], args.parallel, args.timeout))
    elif args.cachebuster == "auto":
        print("  pass 2 skipped: no cache headers on the home page")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("sites", nargs="+", help="site base URL, e.g. https://example.com/pl")
    parser.add_argument("--count", type=int, default=20, help="random sitemap URLs per pass (default 20)")
    parser.add_argument("--sitemap", help="sitemap URL, when it is not at /sitemap.xml or in robots.txt")
    parser.add_argument("--cachebuster", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--parallel", type=int, default=8, help="concurrent requests (default 8)")
    parser.add_argument("--timeout", type=float, default=30, help="seconds per request (default 30)")
    parser.add_argument("--seed", type=int, help="random seed, to repeat a sample")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    return max(test_site(site, args, rng) for site in args.sites)


if __name__ == "__main__":
    sys.exit(main())
