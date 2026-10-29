# Site Contract API

Turn static HTML into a small JSON API using explicit selectors and field types. A missing price, an ambiguous match or an unexpected item count produces a contract error instead of partial data.

[Desktop view](docs/desktop.png) / [Mobile view](docs/mobile.png)

## Run

Python 3.11 or later. No packages or API keys are required.

```sh
python -m site_contract_api serve
```

Open `http://127.0.0.1:8788`. The sample is a fictional product catalog. Edit the HTML or rules, run the contract, then try the broken-page button. Import controls accept your own authorized HTML capture and rules file. JSON downloads contain the displayed result, not the source HTML.

Imports require UTF-8 text. Invalid bytes are rejected without replacing the current input.

The fixed endpoint is `GET /api/data`. Editor previews do not change its source.

Repeated fixed-endpoint reads reuse one validated result when the source content, loaded rules and resolution URL match. Each request still reopens and reads a local file, or calls the configured HTTPS reader so its refresh and robots policy remain in force. Read or validation failures discard the entry on that request. Editing or deleting a source does not clear retained values until the next read. The cache holds at most one 3 MiB result, including selected values and provenance, in process memory; it does not retain a separate copy of the input capture. Local capture times remain fresh and HTTPS capture times follow the remote reader. Editor previews are not cached.

```sh
python -m site_contract_api preview --source page.html --rules rules.json
python -m site_contract_api serve --source page.html --rules rules.json
```

## Rules

```json
{
  "item_selector": "article.product",
  "min_items": 1,
  "max_items": 100,
  "fields": [
    {"name": "title", "selector": ".name", "required": true},
    {"name": "price", "selector": ".price", "attribute": "data-price", "type": "number"}
  ]
}
```

Types are `text`, `number`, `integer` and `url`. Fields are required by default; an absent optional field becomes `null`. `expected_items` can enforce an exact count. `base_url` resolves relative links without fetching anything; without it, local previews use `https://example.invalid/`.

The exact count must fit `min_items` and `max_items`. Adjacent text and inline markup join without inserted spaces, including React comment boundaries; other elements and `<br>` separate words. Whitespace is normalized and CSS layout is not interpreted.

Extraction uses scripting-enabled HTML semantics without running scripts: `script`, `style`, `template` and `noscript` content is excluded. For lazy images, select the real image's `data-src` attribute instead of a `noscript` fallback. Text-only elements need explicit start and end tags. Older Python patch levels that cannot parse text-only elements safely reject nested markup there. On parsers that corrupt ambiguous attribute references such as `&region=us`, extraction fails if a field or value-dependent selector uses that attribute; unrelated attributes are ignored. Update Python or encode attribute ampersands as `&amp;`.

Supported selectors: tags, `*`, `.class`, `#id`, `[attribute]`, `[attribute=value]`, descendant ASCII whitespace and child `>`. Non-ASCII spacing characters and vertical tabs are rejected outside quoted attribute values. A standalone `:scope` selects the item itself. Selector groups, pseudo-classes and JavaScript are unsupported. Write optional closing tags such as `</td>`, `</p>` and `</li>` explicitly; ambiguous omissions and nested links are rejected. The parser expects reasonably well-formed static HTML; it is not a browser DOM implementation.

Successful responses include typed items, a source hash, a rules hash, selectors and the capture time. Contract failures return named issues and no partial items. `/api/data` returns HTTP 409 for a failed contract.

CLI output uses JSON Unicode escapes so redirected results also work with Windows terminal encodings.

## Optional HTTPS source

Remote reads must be configured in the CLI. There is no browser URL-fetch endpoint.

```sh
python -m site_contract_api serve --remote-url https://example.org/catalog --allow-host example.org --rules rules.json
```

Use only a source you are allowed to access. The client checks robots.txt, identifies itself as `SiteContractAPI/1.0`, caches refreshes for at least 60 seconds, validates every resolved address and pins the connection while verifying the original TLS hostname. Redirects, proxies, private destinations, credentials, query strings, dot segments, unencoded non-ASCII paths and compressed responses are unsupported. robots.txt failures stop the read; a missing robots.txt returns standard 404 handling. Long crawl delays require a local capture instead.

## Checks and limits

```sh
python -m unittest discover -s tests -t . -v
```

Tests use fictional HTML, temporary local HTTP servers and mocked remote transports. A local socket test verifies that slowly delivered response headers are interrupted. No third-party site is contacted by the test suite.

Limits include 1 MB HTML, 32 KB rules, 200 items, 16 fields, eight selector steps, bounded tree/work counts, two processing slots and 16 connections. Repeated fields and nested items can exceed the output budget even with small inputs. Extraction stops early and HTTP JSON serialization is capped at 3 MiB, returning a small error instead of partial data. Remote DNS has a three-second budget and HTTPS reads have a ten-second watchdog; robots checks and crawl delays add separate bounded work. This is a loopback tool, not a production hosting platform. JavaScript sites need an authorized static capture. There is no authentication bypass, CAPTCHA solving or automatic selector repair.
