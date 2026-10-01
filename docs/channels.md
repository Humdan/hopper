# Channels, media jobs and the relay

Three optional pieces that turn Hopper into a chat front door for your agents. None of
them run unless configured, and none add dependencies (`ffmpeg` is needed for audio and
video).

## Channels

A channel is a messaging app behind one small interface (receive, send, send a file,
edit, typing). Every channel has an allowlist; messages from anyone else are dropped.

| | Telegram | WhatsApp |
|---|---|---|
| Inbound | long polling, no public URL | webhook, needs a public HTTPS URL |
| Edit messages (live streaming) | yes | no, replies arrive whole |
| Voice notes, photos, video, files | in and out | in and out |
| Setup | a bot from @BotFather | a Meta developer app with the WhatsApp product |

```toml
[channels.telegram]
token = "env:HOPPER_TELEGRAM_TOKEN"
allow = ["123456789"]                 # numeric user ids (ask @userinfobot)

[channels.whatsapp]
token = "env:HOPPER_WHATSAPP_TOKEN"   # a permanent system-user token
phone_number_id = "1234567890"
app_secret = "env:HOPPER_WHATSAPP_APP_SECRET"   # webhook signatures are checked
verify_token = "env:HOPPER_WHATSAPP_VERIFY"
listen = "127.0.0.1:8771"             # put this behind HTTPS, e.g. tailscale funnel 8771
allow = ["15551234567"]
```

Only one program may poll a Telegram bot. If another agent already polls the same token,
give Hopper its own bot, or stop the other poller. Sending is fine from anywhere.

WhatsApp only lets a business message someone within 24 hours of their last message;
after that it needs pre-approved templates, so a late notification may be refused.
Unofficial "WhatsApp Web" libraries are deliberately not supported: they break
WhatsApp's terms and get numbers banned.

**Sending:** `hop send telegram:123456789 "build is green" --file report.png`. Jobs with
`reply_to = "telegram:<chat>"` (or `whatsapp:<number>`) get their result, and any files
the job produced, delivered through the channel with the outbox's retries.

## Media job types

Built-in jobs that need no agent, only an OpenRouter key:

| type | input | output |
|---|---|---|
| `transcribe` | `{file}` audio, voice note or video | text |
| `speak` | `{text, voice?}` | a voice note (`.ogg`, plays inline in chat apps) |
| `describe` | `{file, question?}` image or video | text |
| `image` | `{prompt, files?}` (files = images to edit) | image file(s) |
| `video` | – | not available: no video provider; refused clearly |

```sh
hop add --type image "a red fox at dusk, watercolour" --reply-to telegram:123456789
hop add --type transcribe "meeting" --file ~/.local/share/hopper/inbox/telegram/voice.ogg
hop work --builtin media          # the worker that runs them (or examples/systemd/hopper-media.service)
```

A typed job requires the capability `media.<type>`, so only workers that can do it claim
it. Agents queue them through MCP: `hop_add(type="speak", input={"text": "..."})`.

Jobs can come from remote producers, so a job that names a file may only read under
`[media] input_dirs` (default: Hopper's data folder, where chat attachments and generated
media land). Otherwise a job could send any file on the machine to a cloud model.

```toml
[media]
openrouter_key = "env:OPENROUTER_API_KEY"
transcribe_model = "google/gemini-3.8-flash"
describe_model = "google/gemini-3.8-flash"
speak_model = "openai/gpt-audio-mini"
speak_voice = "alloy"
image_model = "openai/gpt-5-image-mini"
```

## The relay

`hop relay` lets you talk to Claude Code from a chat app without keeping a session open:

- The relay itself is a small always-on process with no AI in it.
- Your first message starts `claude -p` in streaming mode (a few seconds on a Raspberry
  Pi). While you keep talking, the same process answers in about a second. After
  `warm_minutes` of quiet it exits; the next message resumes the same conversation.
- Replies stream into the chat (edited in place on Telegram).
- Permission prompts arrive as `Reply yes abcde or no abcde`. Nothing runs without your
  answer, and silence is a no after `approval_minutes`.
- Voice notes are transcribed and videos described before Claude sees them; photos and
  files are passed by path so Claude can read them.
- `instant` rules answer simple phrases with a command and no AI at all.
- If Claude can't answer (usage limit, login expired, crash), the message goes to the
  `fallback` endpoint (any OpenAI-compatible URL) and the reply says so.

Chat commands: `/new` fresh conversation, `/opus` `/sonnet` `/haiku` switch model (same
conversation; `/opus <question>` for one hard question), `/stop` interrupt, `/status`.

```toml
[relay]
channels = ["telegram"]
cwd = "~"
model = "sonnet"
warm_minutes = 10
approval_minutes = 10
allowed_tools = ["Read", "Grep", "Glob", "WebSearch", "Bash(git status:*)", "mcp__hopper"]
append_system_prompt_file = "~/.config/hopper/relay-prompt.md"   # examples/relay/
mcp_config = "~/.config/hopper/relay-mcp.json"                   # gives Claude hop_add

[[relay.instant]]
match = '(?i)^(lights?|leds?)\b.*'
run = ["~/bin/lights", "{text}"]

[relay.fallback]
url = "http://127.0.0.1:8642/v1/chat/completions"
key = "env:FALLBACK_KEY"
model = "anthropic/claude-sonnet-5.5"
```

Run it with `examples/systemd/hopper-relay.service` (`Restart=always`), and
`loginctl enable-linger` so it survives logouts.

**Personal use only.** The relay runs Claude Code under your own login, on your own
machine, for you. Anthropic does not allow offering Claude to other people through
consumer plan credentials, so don't add other people to the allowlist of a relay that
runs on your subscription.
