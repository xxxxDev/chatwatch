# Security

## How chatwatch is meant to run

chatwatch runs on your own computer, as your user, with your Teams sign-in.
Everything it reads can reach the agents you connect, and whatever those
agents write can reach your colleagues. Keep these facts in mind:

- The page and API listen on 127.0.0.1 only and refuse a foreign Host or
  Origin. Every API call needs the key in `~/.chatwatch/api-key` (mode 600).
- Any program running as your user can read that key and the Chrome profile,
  as it could with your normal browser. chatwatch does not protect you from
  software already on your machine.
- The DevTools port has no authentication. chatwatch binds it to 127.0.0.1;
  do not expose it.
- MCP tools cannot send drafts or change settings; those are on the page. A
  local program holding the key could still call the API directly, and an
  agent that can run shell commands can run `chatwatch send`, which sends at
  once as the person. Give agents only the MCP tools if that matters to you.
- Agents cannot attach files from `CHATWATCH_HOME`. They can attach any other
  file your user can read; an `auto` chat lets a message's sender ask for one.
- File links in a message are set by its sender, and `teams_get_attachments`
  downloads them with your Microsoft account. Fetch attachments only from
  people you trust.
- A chat is found by id before title (anyone in a group chat can rename it);
  two chats with the same title must be named by id.
- Agents read what other people write to you. Treat an agent connected to
  chats as able to be talked into things by the people in them, and keep
  chats on `draft` until you trust what it sends.
- `CHATWATCH_HOME` (default `~/.chatwatch`) holds the key, drafts, the event
  log with message texts, and the Chrome profile with your Teams sign-in. It
  is made readable only by your user. Keep it out of backups you share and
  never commit it.
- chatwatch drives the Teams web page as you. Check that your organisation
  allows that before using it with a work account.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's
**Report a vulnerability** button (private vulnerability reporting) on this
repository, with steps to reproduce and the version you saw it in.

We will acknowledge the report, work on a fix, and credit you in the release
notes unless you prefer otherwise.
