---
name: smoke-test
description: Smoke test a deployed website by checking its home page and random sitemap URLs for HTTP 200, with a cache-bypassing pass behind a CDN or Varnish. Use when the user asks to smoke-test a site, or to offer one after a deploy.
license: MIT
compatibility: Requires python3 (3.9+) and network access to the tested sites.
---

# Smoke test

The test sends dozens of requests to a live site, often production, so it runs only on the user's say-so: either the user asked for it, or you offered it and the user said yes. When you reached this skill on your own, offer the test with the site URLs and wait.

All requests come from one script, in this skill's `scripts/` directory:

```
python3 scripts/smoke_test.py <site URL> [<site URL> ...] [--count 20] [--sitemap URL] [--cachebuster auto|always|never]
```

`--help` lists every option.

1. Take the site URLs from the user or from the deploy you just finished. A site served under a path (`https://example.com/pl`) is tested under that path. Test every site in one run.
2. Run the script. By default it checks the home page plus 20 random sitemap URLs. When the home page carries cache headers, it runs a second pass on a fresh sample with a random `?cachebuster=` value, so those requests reach the origin.
3. When a site has no sitemap the script finds, look for one in the page source or a language path (`/pl/sitemap.xml`), then re-run with `--sitemap`.
4. Re-run the failing URLs once with `curl -sIL` to separate a flaky request from a broken page.
5. Report one line per site and pass (OK count, slowest response), followed by each URL that did not answer 200 with its status, so the user can judge it.

Exit 0 means every URL answered 200, 1 means some did not, and 2 means a site could not be tested. If requests fail with a network error inside a sandbox, re-run the script outside it, asking for approval if needed.
