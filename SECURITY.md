# Security

The server binds to `127.0.0.1`. Windows listeners reserve the port with `SO_EXCLUSIVEADDRUSE` so another reuse-enabled listener cannot share it. It has no account system and should not be exposed to a network. Other software running as your user can access the configured JSON endpoint.

Every route, including `GET /api/data`, rejects a foreign or `null` Origin and any `Sec-Fetch-Site` other than `same-origin` or `none`, so other websites and other local ports cannot trigger extraction or a remote refresh through a browser. Open the page by entering its address rather than following a link from another site.

Browser previews accept HTML and declarative JSON rules. They do not execute scripts, load plugins, read arbitrary files or fetch a URL. Selected field values can still contain private information. Review the displayed result before copying or downloading it. No source HTML, request URL or response body is written to logs.

Remote mode is explicitly configured at startup. It accepts HTTPS to one exact host, rejects any nonpublic DNS result, connects to a validated address with normal TLS verification, and never follows redirects or uses environment proxy settings. It sends no login cookies or authorization headers. DNS and HTTPS timeouts, response limits, robots checks and caching constrain each source. A stalled system resolver occupies one bounded daemon slot until the OS call returns; later lookups fail rather than adding resolver threads.

IPv6 destinations are restricted to public unicast, excluding special-use, site-local, documentation and transition ranges. Incoming request headers and bodies have separate ten-second budgets. Header deadlines stop before extraction or remote refresh begins. Protocol errors and unsupported methods return fixed JSON with security headers.

Extraction accounts for escaped output values, and HTTP JSON serialization stops at 3 MiB before response headers are written. Cached remote failures retain only fixed error data, not exception tracebacks. Malformed robots rate directives fail closed.

An HTML capture is not proof that the data is true. A passing contract checks selected fields, counts and types, not every change to a page. Hashes identify inputs; they are not signatures. The HTML parser supports a documented subset of selectors and cannot reproduce every browser repair of malformed markup.

Report bugs with synthetic HTML and rules. Do not attach credentials, private page captures or business data to an issue.

## Reporting a vulnerability

Use [Security > Report a vulnerability](https://github.com/Jgolding1998/site-contract-api/security/advisories/new) to send a private report. Do not submit vulnerability details through the public feedback form.

Include the affected version, platform, expected behavior and a synthetic reproduction. Leave out credentials, personal information and private files.
