# Contributing

Thanks for your interest in improving **ntfy-icinga-rich-alerts**! This is a small, focused
project, and contributions of all sizes are welcome — bug reports, docs fixes, and features alike.

## Ways to help

- **Found a bug?** Open an issue with what you expected, what happened, and enough detail to
  reproduce (Icinga version, ntfy version, the render backend you use, redacted logs).
- **Have an idea?** Open an issue to discuss it before a large PR, so we can agree on the shape.
- **Small fixes** (typos, docs, obvious bugs) — just send a PR.

## Development notes

The dispatcher is plain Python 3 (standard library + `requests`, `PyYAML`, and optionally
`matplotlib`/`numpy` for the `vm` render backend). The broker is a small Flask app.

- Keep it dependency-light and metric-agnostic — the PromQL/panel lives in *your* config, not
  in the code.
- The dispatcher must **never** let a non-critical failure (graph render, image upload,
  suppression store) block an alert. Fail open and send text-only.
- Try the dispatcher locally with `--dry-run --verbose` and faked Icinga macros in the
  environment; it prints the exact ntfy payload without sending anything.

## Style

- Match the existing style (readable, well-commented, no clever one-liners where a clear loop
  will do).
- No organisation-specific names, hostnames, IPs, or domains — use `example.com` placeholders.

## License

By contributing, you agree that your contributions are licensed under the MIT License (see
[LICENSE](LICENSE)).
