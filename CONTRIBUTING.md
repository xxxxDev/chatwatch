# Contributing

Thanks for helping. chatwatch is small, so a short, focused change is the
easiest to review.

## Before you start

- For a bug, open an issue with what you did, what you expected and what
  happened, plus the version (`python3 -c "import chatwatch; print(chatwatch.__version__)"`).
- When Teams changes its web page and reading or sending breaks, say which step
  failed (the error text names it, e.g. `NO_COMPOSE_BOX`).
- For a larger change or a new feature, open an issue first.
- For a security problem, follow [SECURITY.md](SECURITY.md) instead.

## Development

```sh
python3 -m unittest discover tests      # no browser needed
```

The tests use a fake Teams. To try a change against real Teams, use a chat of
your own (a bot or a test account), never someone else's, and set it to
`draft` until you have seen what would be sent.

## Guidelines

- Standard library only; Python 3.9+.
- Match the style of the code around your change.
- Explain any change to what gets sent, when, and to which chat, so it can be
  checked. Keep the rule that a send which may have gone out is never resent
  by itself.
- The Teams access token must stay inside the tab: page code may use it, but
  never return it to Python.
- Don't commit anything from `~/.chatwatch`, or real keys, tokens or chat
  contents.

By contributing, you agree that your contribution is released under the
[MIT License](LICENSE).
