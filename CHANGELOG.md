# Changelog

## Unreleased

- Teams screen on the page: a live view of the Teams tab that you can click,
  scroll and type in, e.g. to sign in again. The tab is also found while it
  is on Microsoft's sign-in page.

## 0.1.0

First release.

- Reads Teams chats through Teams' own chat service API inside the tab, so
  nothing is marked read; the access token never leaves the tab.
- Sends by pasting the whole text, attaching files and confirming the message
  appeared; reports `not_sent` and `maybe_sent` apart.
- MCP server (stdio) with six tools; push handlers (command or URL).
- Send modes `off`, `draft` (default) and `auto`; pause as an emergency stop.
- Local page for chat settings and drafts; local API with a key.
