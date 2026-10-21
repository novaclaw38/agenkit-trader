# AgenKit Trader

Validation-gated crypto trading agent. See `docs/superpowers/specs/` for the design.

- No performance claims exist yet; none will be made until measured results exist.
- Live trading is locked by default. Only paper/testnet trading is available.

## Development

    python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
    .venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/mypy src
