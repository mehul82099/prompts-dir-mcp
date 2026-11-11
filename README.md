# prompts-dir-mcp

A zero-dependency, single-file MCP stdio server that serves a directory of
Markdown files as MCP **prompts** (protocol version `2026-07-28`), with:

- `prompts/list` / `prompts/get` and `{{argument}}` substitution
- argument completion through `completion/complete` (`ref/prompt`), driven by
  per-argument `[enum,lists]` declared right in the template
- `prompts` `listChanged` delivered through the new `subscriptions/listen`
  pattern (`promptsListChanged`), backed by a polling watcher over the
  template directory - add, edit, or delete a `.md` file and subscribed
  clients get `notifications/prompts/list_changed`

Drop Markdown files in a folder, get a live prompt library. No packages, no
config file, no build step.

## Template format

One `.md` file per prompt; the prompt name is the file stem
(`code_review.md` -> `code_review`):

```markdown
<!-- args
code: The code to review (required)
language: Programming language [python,javascript,go,rust]
-->
# Request Code Review
Asks the LLM to analyze code quality and suggest concrete improvements.

Please review this {{language}} code:

{{code}}
```

- `<!-- args ... -->` block (optional): one `name: description` line per
  argument. Suffix `(required)` marks it required; a trailing `[a,b,c]`
  declares completion candidates for `completion/complete`.
- `# Title` line (optional): becomes the prompt's `title`.
- Next non-empty line (optional): becomes the `description`.
- Everything else is the template body. `{{name}}` placeholders are filled
  from `prompts/get` arguments.

Three ready-to-use examples ship in [`prompts/`](prompts/).

## Usage

```bash
python3 prompts_dir_server.py            # serves ./prompts if present, else cwd
PROMPTS_DIR=/path/to/templates python3 prompts_dir_server.py
```

Example session:

```json
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28"}}
← {"jsonrpc":"2.0","id":1,"result":{"resultType":"complete","protocolVersion":"2026-07-28","capabilities":{"prompts":{"listChanged":true},"completions":{}},"serverInfo":{"name":"prompts-dir-mcp","version":"1.0.0"}}}
→ {"jsonrpc":"2.0","id":2,"method":"prompts/get","params":{"name":"code_review","arguments":{"code":"print(1)","language":"python"}}}
← {"jsonrpc":"2.0","id":2,"result":{"resultType":"complete","description":"Asks the LLM to analyze code quality and suggest concrete improvements.","messages":[{"role":"user","content":{"type":"text","text":"Please review this python code. ... print(1)"}}]}}
→ {"jsonrpc":"2.0","id":3,"method":"completion/complete","params":{"ref":{"type":"ref/prompt","name":"code_review"},"argument":{"name":"language","value":"ja"}}}
← {"jsonrpc":"2.0","id":3,"result":{"resultType":"complete","completion":{"values":["javascript"],"total":1,"hasMore":false}}}
→ {"jsonrpc":"2.0","id":4,"method":"subscriptions/listen","params":{"notifications":{"promptsListChanged":true}}}
← {"jsonrpc":"2.0","method":"notifications/subscriptions/acknowledged","params":{"_meta":{"io.modelcontextprotocol/subscriptionId":4},"notifications":{"promptsListChanged":true}}}
... a template is added, edited, or deleted ...
← {"jsonrpc":"2.0","method":"notifications/prompts/list_changed","params":{"_meta":{"io.modelcontextprotocol/subscriptionId":4}}}
```

## Spec mechanics implemented (2026-07-28)

| Feature | Behavior here |
| --- | --- |
| `prompts/list` | Parses the template directory on each call; result is the same for every connection, as the spec requires |
| `prompts/get` | Single `user` message with substituted text; unknown prompt, missing required arguments, or malformed params are rejected with `-32602` |
| `completion/complete` | Only `ref/prompt` refs; prefix-filters the argument's declared `[enum]`, envelope carries `values`, `total`, `hasMore`, capped at 100 values |
| `prompts.listChanged` | Emitted via `subscriptions/listen` with `promptsListChanged: true`, exactly as the 2026-07-28 prompts page prescribes (the old standalone flow is gone) |
| Subscriptions pattern | Ack-first ordering, `io.modelcontextprotocol/subscriptionId` correlation, `notifications/cancelled`, graceful closure responses - same mechanics as the spec's Subscriptions page |

Interpretation notes (the spec leaves these open):

- Optional arguments not supplied to `prompts/get` are substituted with an
  empty string.
- Extra arguments not used by the template are ignored.
- A template content edit also fires `prompts/list_changed` (the watcher
  tracks name + mtime + size, not just file presence).
- On client cancellation this server completes the listen request with the
  graceful-closure result, so cancellation and server-initiated teardown look
  the same to the client.
- `prompts/list` pagination (`cursor`) is not implemented; the directory cap
  (`PROMPTS_MCP_MAX_PROMPTS`, default 500) bounds the list instead.

## Tests

```bash
python3 -m unittest -v
```

23 stdio integration tests (Python 3.9+, stdlib only) spawn the server as a
subprocess and cover: capability declaration, template parsing (args block,
title, description, required flags), substitution including empty-default
optional arguments, `-32602`/`-32601`/`-32700` error paths, completion
prefix filtering against declared enums, ack-honored subsets, list_changed on
add/edit/remove, subscription demux and cancellation, and graceful closure on
transport EOF. CI runs the suite on ubuntu-24.04 and windows-latest against
Python 3.9 and 3.13.

## Limits (honest)

- Polling watcher (`PROMPTS_MCP_POLL_MS`, default 200 ms) - fine for a
  template directory, not inotify/FSEvents. A change between subscription
  registration and the first poll cycle is baselined, not notified.
- stdio transport only; prompt messages are text-only (no embedded resource
  or image content).
- Python 3.9+. No third-party packages.
- Malformed template files degrade to plainer prompts instead of failing
  (the parser never raises).

## Why this exists (gap check)

GitHub repository searches for `mcp server prompts directory markdown python`
and `"prompts/list" mcp server markdown` (api.github.com, November 11, 2026)
both returned zero repositories. This repo is a use case for the 2026-07-28
prompts surface: a minimal, tested prompt-library server you can read in one
sitting, showing how `prompts`, `completion/complete`, and the new
`subscriptions/listen` listChanged flow fit together.

## Sources

- Prompts:
  https://modelcontextprotocol.io/specification/2026-07-28/server/prompts
- Subscriptions pattern (listChanged delivery):
  https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions
- Completion:
  https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/completion
- MCP specification index: https://modelcontextprotocol.io/llms.txt

## License

MIT (c) 2026 Mehul Jhabak
