# Research: messaging channels that reach the owner's phone with no inbound port

Research notes for **Seraphina OS Harness**, serving the "Wake & Messages" work:
a persona that wakes itself and can message the owner, and the owner can answer
back. These notes cover only the **channel**: how a message gets from the server
to the owner's phone, and how a reply gets back, without opening any inbound
port. Wake triggers, context assembly and the model turn are other files.

The design already has a notifier (SERAPHINA_OS_HARNESS.md §6.6 `notify`, with
`"channels": ["inbox", "ntfy-native:<topic>"]` and an engine-independent ntfy
server reached over the tailnet). This file checks that choice against the
alternatives and adds what a two-way, agent-initiated channel needs.

Written 2026-10-21.

## How these notes were made, and their limits

- **Read from source** (shallow clones on 2026-10-21; short commit ids):
  ntfy docs in `binwiederhier/ntfy` (`5e11f4f`, 2026-10-18; server v2.32.0),
  `gotify/website` (`9a0af61`) and `gotify/server` (`f4de619`; latest tag
  v3.1.1), `AsamK/signal-cli` (`6302698`, 2026-10-20; latest tag v0.14.9),
  `bbernhard/signal-cli-rest-api` (`4b0f6f3`; latest tag 0.101),
  `tdlib/telegram-bot-api` (`e3e9dd8`), a machine-readable scrape of the
  Telegram Bot API docs, `PaulSonOfLars/telegram-bot-api-spec` (`d59462b`,
  2026-08-25, "Bot API 10.3, August 24, 2026"), `discord/discord-api-docs`
  (`fd75e22`, 2026-10-19), `continuwuity/continuwuity` (`2f93711`, 2026-10-20),
  `element-hq/element-x-android` (`433d094`, push providers only),
  `gregdel/pushover` (`5cc5324`), `neurolit/ha-pushover-next` (`f18d884`,
  2026-10-06; Pushover end-to-end encryption code) and OpenClaw's channel docs
  (`openclaw/openclaw` `14b7bab`, 2026-10-21) as prior art.
- **Not reachable from the research sandbox**: core.telegram.org, telegram.org,
  docs.ntfy.sh, pushover.net, signal.org, matrix.org, discord.com,
  developers.facebook.com, twilio.com. For Telegram, the GitHub scrape above is
  a field-for-field copy of the official reference, so method and field limits
  are quoted from it; the prose sections of the Telegram docs (FAQ, "Getting
  updates") came from search-engine extracts. Pushover, WhatsApp and Signal's
  terms also came from search extracts. Anything that rests only on such an
  extract, or on my own knowledge, is marked **UNVERIFIED**.
- **Nothing here was tested live.** No phone, bot account or number was used.
  Every code sketch is untested and is there to show endpoints and payloads.
- Cited file paths in cloned repos are given as GitHub links. Versions and
  prices move; re-check before building.

---

## 0. The answer in one screen

- **No inbound port is needed for any channel worth using.** Every good option
  works by the server making *outbound* connections: Telegram long polling,
  an ntfy or Matrix server on the tailnet that the phone reaches over
  Tailscale, signal-cli's own outbound connection to Signal, IMAP polling,
  Pushover receipt polling. Only WhatsApp's official API and Discord's HTTP
  interactions demand a public webhook, which "Funnel off" rules out.
- **Default for a novice: a Telegram bot, in "pointer-only" mode.** Five
  minutes in Telegram, no server software, free, the same on iPhone and
  Android, real two-way chat with buttons, and messages the owner sends while
  the server is powered off wait at Telegram for up to 24 hours. Its cost is
  privacy: Telegram's servers read every bot message, so Seraphina may only
  send fixed, harmless text ("Seraphina needs a decision: open the cockpit")
  and never anything from the owner's personal or health data (§3).
- **Most private: a self-hosted ntfy server on the server, reached only over
  the tailnet.** Content travels only between the owner's own devices, inside
  WireGuard. On Android nothing goes through Google at all for a self-hosted
  server. On iPhone only an opaque wake-up ping ("New message", a message id
  and a hash of the topic URL) goes through ntfy.sh, Firebase and Apple; the
  phone then fetches the real text from the owner's server (§4). Buttons can
  answer back by publishing to a reply topic. It is also what the design
  already names as the engine-independent channel.
- **What Seraphina should build, in order:** (1) the channel interface and the
  outbound content gate (§13), (2) ntfy on the tailnet, set up by Seraphina
  itself so the novice only installs an app, (3) Telegram as the opt-in
  "works anywhere" channel in pointer-only mode, (4) Pushover only as an
  optional loud alarm for "server unreachable" heartbeats. Signal with a
  dedicated bot number is the best private *chat* option but needs a second
  phone number and upkeep every three months; Matrix is the most capable
  private chat but too heavy for a novice. Discord, email and WhatsApp are
  not recommended as the primary channel (§9 to §11).
- **Two rules matter more than the channel.** (a) A content gate in code, not
  in the prompt: what may be sent depends on the channel's privacy class, and
  for any channel a third party can read, only templated text goes out.
  (b) Inbound trust: only an immutable sender id that was paired once is
  heard; buttons carry single-use, expiring nonces; a phone reply can approve
  only items marked phone-approvable, and never anything that reaches the AI
  admin workers' SSH keys without the cockpit (§13.3).
- **A private channel and an independent channel fail at different times.**
  A tailnet-only channel dies when Tailscale on the server or phone breaks;
  a Telegram or Pushover channel dies when the internet or the vendor does.
  The owner should have one of each for "needs you" alerts, with the
  independent one carrying only minimal text (§13.7).

---

## 1. The constraints that decide

| Constraint (owner's setup) | Consequence for channels |
| --- | --- |
| No ports forwarded, Tailscale only, Funnel off | Nothing can receive a public webhook. Telegram must use `getUpdates` long polling, not `setWebhook`. WhatsApp Cloud API and Discord's Interactions Endpoint URL are out. Self-hosted servers are reachable by the phone only over Tailscale. |
| Server holds sensitive personal and health data that must never leave | Channels whose operator can read content (Telegram, Discord, email, Pushover without e2ee, WhatsApp, SMS) may carry only pointer text. Even end-to-end encrypted channels send ciphertext and metadata off the machine; whether *encrypted* personal content may go to the owner's phone is an owner decision (§15). |
| Nightly power-off 23:00 to 07:00 | Nothing is sent overnight from any channel. What matters is what happens to the owner's replies sent overnight: queued by a third party (Telegram 24 h, Signal, email, Discord) or lost because the self-hosted server is off (ntfy, Gotify, Matrix). |
| Local models only for the agent's turns; TabbyAPI shared with Open WebUI | Channels themselves need no model. Deterministic handlers (approve, pause, snooze) must work even when the model server is busy or asleep. |
| AI admin workers exist with SSH keys | Whoever controls the channel can talk to an agent that can reach SSH keys. The channel's account security (Telegram 2FA, bot-token secrecy, ntfy ACLs) is part of the server's security boundary. |
| API keys only from env or an env file | Bot tokens, ntfy tokens, Pushover keys and mail app passwords go in the env file (DESIGN.md §25 `envfile.py`), so the EventBus's `env_secret_values()` redaction covers them in logs. |
| Owner is security-conscious but not a Linux expert | Prefer channels where Seraphina does the server side and the owner only installs a phone app and scans a code. |
| Engine-independent alerts (design §6.6) | Self-hosted pieces should be native packages (ntfy and continuwuity have apt repositories; Gotify and signal-cli ship single binaries), not containers. `signal-cli-rest-api` is Docker-only and fails that test. |

---

## 2. Comparison at a glance

"Reads content" means who besides the owner's devices can read message text.
"Overnight reply" means a message the owner sends while the server is off.

| Channel | Two-way | Buttons on the phone | Reads content | Phone apps | Novice setup | Cost | Overnight reply | stdlib Python |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **Telegram bot** | Yes, chat | Yes (inline keyboards, callback queries) | Telegram | iOS, Android, desktop | Easy (BotFather, paste token, tap Start) | Free | Kept up to 24 h | Yes (long polling) |
| **ntfy, self-hosted on tailnet** | Partly: buttons POST back; Android can type replies | Yes, up to 3 per notification | Nobody (iOS: only an opaque ping goes via ntfy.sh, Firebase, Apple) | Android (Play, F-Droid), iOS (basic, buggy) | Medium for the server (Seraphina can do it), easy on the phone | Free | Lost (server off) | Yes (HTTP stream) |
| ntfy.sh (public) | Same | Same | ntfy.sh operator; Firebase/APNs on phones | Same | Easy | Free tier 250 msgs/day | Kept 12 h by default | Yes |
| **Pushover** | Only "acknowledged" for emergency alerts | No (one link, acknowledge button for emergencies) | Pushover, unless its e2ee is on | iOS, Android, desktop | Easy | $4.99 once per platform | n/a | Send yes; e2ee needs AES (not stdlib) |
| **Gotify** | No | No (click URL only) | Nobody | Android only (official) | Medium | Free | n/a | Yes |
| **Signal via signal-cli** | Yes, chat | No (text replies, reactions) | Nobody (end-to-end encrypted); Signal sees little metadata | iOS, Android, desktop | Hard (second number, captcha, Java or native build, upkeep) | Free (plus a second number) | Queued at Signal | Yes (local JSON-RPC + SSE) |
| **Matrix + Element** | Yes, chat | No standard buttons | Nobody (own server); iOS push via Element's gateway carries ids only | iOS, Android, desktop | Hard (homeserver, TLS, client login) | Free | Lost (server off) | Yes (`/sync` long polling), but no e2ee |
| **Discord bot** | Yes, chat | Only with Gateway WebSocket | Discord (text is not e2ee) | iOS, Android, desktop | Medium | Free | Stored in channel | Send and REST polling yes; Gateway needs a hand-written WebSocket |
| **Email** | Yes, slow | No | Mail provider(s) | Any mail app | Medium (app password; Outlook needs OAuth) | Free | Stored at provider | Yes (`smtplib`, `imaplib`) |
| **WhatsApp Cloud API** | Yes | Yes | Meta (the Cloud API runs on Meta's servers; **UNVERIFIED** detail) | iOS, Android | Very hard (business verification, public webhook) | Per template message | n/a | Needs a public webhook: impossible here |
| WhatsApp unofficial (Baileys, whatsmeow) | Yes | No | WhatsApp sees metadata; the library holds all chats of that number | iOS, Android | Medium | Free | Queued | No (Node or Go) |

---

## 3. Telegram Bot API

**Verdict: the easiest two-way channel and the novice default, but only for
pointer text.** Everything the bot sends and receives is readable by Telegram.

### 3.1 How it works with no inbound port

There are two mutually exclusive ways to receive updates: `getUpdates` (long
polling) and webhooks. "Incoming updates are stored on the server until the bot
receives them either way, but they will not be kept longer than 24 hours"
(Bot API docs, "Getting updates", via search extract,
<https://core.telegram.org/bots/api#getting-updates>). With `getUpdates` the
server holds an outbound HTTPS request open to `api.telegram.org` for up to
`timeout` seconds. The scraped reference gives the exact semantics
(<https://github.com/PaulSonOfLars/telegram-bot-api-spec>, `api.json`):

- `offset`: "An update is considered confirmed as soon as getUpdates is called
  with an offset higher than its update_id." So the bot stores the next offset
  and only advances it after handling an update.
- `limit` 1 to 100, default 100. `timeout`: "Defaults to 0, i.e. usual short
  polling. Should be positive, short polling should be used for testing
  purposes only."
- `allowed_updates`: e.g. `["message", "callback_query"]`; "If not specified,
  the previous setting will be used."
- `setWebhook` and `deleteWebhook` exist; `getUpdates` does not work while a
  webhook is set. If two processes poll the same token, Telegram answers
  `409 Conflict` to one of them (community reports, e.g.
  <https://community.openhab.org/t/telegram-exception-getupdates-failed-with-error-code-409/149661>).
  Seraphina must be the only poller for its token: no OpenClaw, n8n or Home
  Assistant on the same bot.

All methods are `https://api.telegram.org/bot<token>/<method>`, JSON in and
out, `{"ok": true, "result": ...}` or `{"ok": false, "error_code",
"description", "parameters": {"retry_after"}}` (`ResponseParameters` in the
scrape).

Telegram's open-source Bot API server (<https://github.com/tdlib/telegram-bot-api>)
can be run locally, but it needs an `api_id`/`api_hash` and still talks to
Telegram's MTProto servers; it changes where the HTTP API runs, not who sees the
content. Not worth it here.

### 3.2 Novice setup

1. Have Telegram on the phone (it needs a phone number). Turn on Telegram's
   two-step verification password, because whoever takes over this account can
   talk to Seraphina (path in the app: Settings, Privacy and Security,
   Two-Step Verification; **UNVERIFIED** menu wording).
2. In Telegram, open **@BotFather** (check the verified badge), send `/newbot`,
   choose a display name and a username ending in `bot`. BotFather replies with
   the token.
3. Still in BotFather: `/setjoingroups` then **Disable**, so nobody can add the
   bot to a group (**UNVERIFIED** command name, from memory).
4. In Seraphina's Secrets page, paste the token. Seraphina stores it in the env
   file as `SERAPHINA_TELEGRAM_BOT_TOKEN` and calls `getMe` to check it.
5. Pairing: Seraphina shows a QR code for `https://t.me/<botname>?start=<one-time
   code>`. The owner scans it, Telegram opens the bot, the owner taps **Start**.
   The bot receives `/start <code>`, records the sender's numeric user id as
   the owner, and replies "Paired". (Deep-link `start` payload limits, up to 64
   characters of `A-Za-z0-9_-`, are from Telegram's deep-linking docs from
   memory: **UNVERIFIED**.) A bot cannot message a user first; the user must
   have messaged it (search extracts, e.g.
   <https://wk0.medium.com/send-and-receive-messages-with-the-telegram-api-17de9102ab78>),
   so this step is also what allows Seraphina to send at all.
6. Seraphina sends a test message with two buttons; the owner taps one.

### 3.3 Privacy: what Telegram's servers can see

- **All message text both ways, button labels and `callback_data`.** Bot chats
  are ordinary "cloud chats": encrypted between app and server and stored
  encrypted on Telegram's servers, with Telegram holding the keys. Secret
  (end-to-end) chats are only between people and cannot be used with bots
  (Telegram's own E2EE FAQ, <https://tsf.telegram.org/manuals/e2ee-simple>;
  community answers; **UNVERIFIED** against the current telegram.org FAQ).
- **The bot token** on every request (it is in the URL path), the server's
  public IP address, timing and volume, and the owner's Telegram account and
  phone number.
- **History is kept until deleted.** A bot can delete its own and incoming
  messages in a private chat, but only within 48 hours of sending
  (`deleteMessage` in the scrape). `protect_content: true` stops forwarding
  and saving inside Telegram apps; it does not hide anything from Telegram.
- Notification previews go through Apple's or Google's push service; whether
  Telegram encrypts push payloads end to end is **UNVERIFIED**, so assume the
  text may be visible there too.

Consequence: Telegram is a **third-party-readable** channel (§13.2). Allowed:
"Seraphina needs a decision (1 item). Open the cockpit." Not allowed: service
names that reveal health matters, file names, any text written by a model
from the owner's data, error output.

### 3.4 Two-way: messages, inline keyboards, callback queries

- Free text: the owner types; the update has `message.from.id`,
  `message.chat.id`, `message.chat.type` (`"private"`), `message.text`.
- Buttons: `reply_markup = {"inline_keyboard": [[{"text": "Approve",
  "callback_data": "ap:7f3k9q"}]]}`. `callback_data` is "1-64 bytes", so it
  carries a short opaque id, never content. A tap arrives as
  `callback_query` with `id`, `from`, `message` and `data`. The bot must call
  `answerCallbackQuery` (optional text 0 to 200 characters), or the phone
  keeps showing a loading indicator (library docs restating the Bot API,
  <https://docs.aiogram.dev/en/dev-3.x/api/types/callback_query.html>).
- After a decision, `editMessageText` with an empty keyboard turns the
  message into a record ("Approved at 19:42") so a second tap is impossible.
- `ForceReply` shows the reply interface for step-by-step answers.
- Newer additions (ephemeral messages, rich messages in Bot API 10.2 and 10.3,
  button `style` "danger"/"success"/"primary") exist but are not needed.

### 3.5 Limits

- Message text: "1-4096 characters after entities parsing" (`sendMessage`).
  Prefer plain text: no `parse_mode`, or `HTML` with `html.escape` on every
  inserted value; MarkdownV2 escaping is a common source of failed sends.
- Rate: "In a single chat, avoid sending more than one message per second. We
  may allow short bursts that go over this limit, but eventually you'll begin
  receiving 429 errors"; about 30 messages per second for bulk; 20 per minute
  in a group (Bots FAQ via search extract, <https://core.telegram.org/bots/faq>).
  On 429, wait `parameters.retry_after` seconds. A personal assistant should
  rate-limit itself far below this anyway (§13.5).
- Up to 100 commands with `setMyCommands`.

### 3.6 Bot token security and chat-id allowlisting

- **The token is full control of the bot**: anyone holding it can read pending
  updates with `getUpdates`, send as the bot, or set a webhook to steal
  future messages. It does not expose the owner's personal Telegram account.
  If it leaks, BotFather `/revoke` issues a new one and the old one stops
  working at once (<https://www.deployhq.com/support/integrations/telegram>).
- Keep it in the env file only. **Never log a request URL** (the token is in
  it); log the method name. `urllib.error.HTTPError.url` and tracebacks can
  carry it, so catch and re-raise without the URL. The EventBus redacts env
  secrets, which is a second line of defence, not the first.
- **Allowlist by numeric user id**, not `@username` (usernames can change and
  be re-registered). The ids "may have more than 32 significant bits ... at
  most 52 significant bits" (`User.id` in the scrape); Python ints are fine.
  Also require `chat.type == "private"`. Anyone in the world can find a bot by
  its username and message it; unknown senders get no reply at all (or one
  "This bot is private" reply), and the attempt is logged as an event.
- A sensible second factor: callback buttons carry single-use nonces that
  expire (§13.3), so a stolen token or a forwarded old message cannot replay an
  approval.

### 3.7 Reliability and the nightly power-off

Very reliable on both phone platforms; Telegram's apps are mature. Long polling
recovers from network loss by just retrying. Overnight: the server is off, so
nothing is sent; messages the owner sends to the bot wait at Telegram and are
read at 07:00 boot, as long as they are less than 24 hours old. On boot, the
first `getUpdates` returns them; Seraphina should treat anything older than,
say, 12 hours as "seen late" and say so ("You asked at 23:40 last night; doing
it now?") rather than acting silently.

### 3.8 Cost

Free. (Paid broadcasts exist for bots sending above 30 messages per second;
irrelevant here.)

### 3.9 stdlib sketch

```python
"""Telegram channel: outbound HTTPS only (long polling). Untested sketch."""
import json, os, time, urllib.error, urllib.request

TOKEN = os.environ["SERAPHINA_TELEGRAM_BOT_TOKEN"]          # env file, never config
OWNER_ID = int(os.environ["SERAPHINA_TELEGRAM_OWNER_ID"])    # numeric id from pairing
BASE = "https://api.telegram.org/bot" + TOKEN + "/"          # holds the token: never log it

class TelegramError(Exception):
    def __init__(self, code, description, retry_after=None):
        super().__init__(f"telegram {code}: {description}")
        self.code, self.retry_after = code, retry_after

def call(method, params, http_timeout=30):
    req = urllib.request.Request(BASE + method, data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as err:      # 400/403/409/429 carry a JSON body
        body = json.load(err)
    if not body.get("ok"):
        raise TelegramError(body.get("error_code"), body.get("description"),
                            (body.get("parameters") or {}).get("retry_after"))
    return body["result"]

def send(text, buttons=(), silent=False):
    """buttons: [("Approve", "ap:7f3k9q"), ("Not now", "no:7f3k9q")]; data <= 64 bytes."""
    params = {"chat_id": OWNER_ID, "text": text[:4096], "protect_content": True,
              "disable_notification": silent,
              "link_preview_options": {"is_disabled": True}}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in buttons]]}
    return call("sendMessage", params)["message_id"]

def poll(handle, state):
    """Runs in its own thread. state persists the next offset and seen update ids."""
    while not state.stopping():
        try:
            updates = call("getUpdates", {"offset": state.offset, "timeout": 50,
                                          "allowed_updates": ["message", "callback_query"]},
                           http_timeout=65)
        except TelegramError as err:            # 409: second poller or webhook; 429: flood
            state.report(err)
            time.sleep(err.retry_after or 10)
            continue
        except OSError:                         # offline, DNS, timeout (URLError is OSError)
            time.sleep(10)
            continue
        for upd in updates:
            item = upd.get("message") or upd.get("callback_query") or {}
            chat = (item.get("message") or item).get("chat", {})
            if item.get("from", {}).get("id") == OWNER_ID and chat.get("type") == "private":
                if "callback_query" in upd:
                    call("answerCallbackQuery", {"callback_query_id": upd["callback_query"]["id"]})
                handle(upd)                     # idempotent: dedupe on update_id
            else:
                state.report_stranger(item.get("from", {}).get("id"))
            state.save_offset(upd["update_id"] + 1)   # confirms the update to Telegram
```

The pairing step is the same loop in a "pairing" state: the first private
message whose text is exactly `/start <code>` (code shown in the cockpit, single
use, 10-minute expiry) sets `OWNER_ID`.

---

## 4. ntfy

**Verdict: the most private practical channel, and the right first build.**
Self-hosted on the server and reachable only over Tailscale, message content
never leaves the owner's devices. Weaker for chat than Telegram; best as "tap on
the shoulder, with buttons", with the conversation itself in the cockpit.

### 4.1 Self-hosted on the tailnet versus ntfy.sh

ntfy is a pub/sub server: publish with an HTTP `PUT`/`POST`, subscribe with an
HTTP stream, WebSocket or the phone app. Current stable: server v2.32.0
(2026-10-18), Android v1.25.2, iOS v1.7.0 (`docs/releases.md`; the iOS repo
also has a `v1.8.0` tag not yet in those notes).

- **ntfy.sh** (the public instance): zero setup, but "all topics on ntfy.sh are
  public" unless reserved with an account, and messages are cached on its
  servers (default 12 hours) and pass through Google Firebase for the Play
  and iOS apps (`docs/publish.md` "Public topics"; `docs/privacy.md`, updated
  2026-06-15). Limits: 250 messages a day, 4,096-byte messages, IP bans for
  repeated abuse (`docs/publish.md` "Limitations"). Acceptable only for the
  minimal "needs you" ping the design allows with consent.
- **Self-hosted on the tailnet**: install the native package, bind it to
  `127.0.0.1`, put `tailscale serve` in front for HTTPS on the server's
  `*.ts.net` name, and turn authentication on with `deny-all`. The phone runs
  the Tailscale app and the ntfy app pointed at that URL.

### 4.2 Setup (Seraphina does the server part)

Server (each command is a step Seraphina runs with consent and records):

```sh
# 1. Install from the official apt repository (docs/install.md, "Debian/Ubuntu repository")
sudo mkdir -p /etc/apt/keyrings
sudo curl -L -o /etc/apt/keyrings/ntfy.gpg https://archive.ntfy.sh/apt/keyring.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/ntfy.gpg] https://archive.ntfy.sh/apt stable main" \
  | sudo tee /etc/apt/sources.list.d/ntfy.list
sudo apt update && sudo apt install ntfy
```

```yaml
# 2. /etc/ntfy/server.yml
base-url: "https://<server>.<tailnet>.ts.net:8443"   # must match what the phone uses exactly
listen-http: "127.0.0.1:2586"                         # never on a LAN or public interface
behind-proxy: true
cache-file: "/var/cache/ntfy/cache.db"
cache-duration: "24h"
auth-file: "/var/lib/ntfy/user.db"
auth-default-access: "deny-all"
# Only if the owner uses an iPhone (see 4.3). Sends a message id and a topic hash, never text:
# upstream-base-url: "https://ntfy.sh"
```

```sh
# 3. Users and access (docs/config.md "Access control"); tokens grant a user's full rights,
#    so each role is its own user.
sudo ntfy user add seraphina            # the harness: publishes alerts, reads replies
sudo ntfy user add owner-phone          # the phone app logs in as this user
sudo ntfy user add phone-reply          # write-only; its token rides inside button actions
sudo ntfy access seraphina   seraphina          write-only
sudo ntfy access seraphina   seraphina-replies  read-only
sudo ntfy access owner-phone seraphina          read-only
sudo ntfy access owner-phone seraphina-replies  write-only
sudo ntfy access phone-reply seraphina-replies  write-only
sudo ntfy token add seraphina           # -> env file SERAPHINA_NTFY_TOKEN
sudo ntfy token add phone-reply         # -> env file SERAPHINA_NTFY_REPLY_TOKEN
sudo systemctl enable --now ntfy

# 4. HTTPS inside the tailnet only (not Funnel). Exact flags: check `tailscale serve --help`.
sudo tailscale serve --bg --https=8443 http://127.0.0.1:2586     # UNVERIFIED flag syntax
```

Notes on step 4. Tailscale's HTTPS certificates are real Let's Encrypt
certificates, so every certificate name, which is the machine name plus the
tailnet name, is published in the public Certificate Transparency log. Rename
the machine first if its name says anything personal
(<https://tailscale.com/kb/1153/enabling-https>). `serve` stays inside the
tailnet; `funnel` would make it public and is off. Plain HTTP over the tailnet
is already encrypted by WireGuard, but iOS apps usually refuse plain HTTP
(App Transport Security; **UNVERIFIED** for the ntfy iOS app), so use HTTPS.

Phone (the only part the owner does):

1. Tailscale app installed and set to stay connected: on iPhone, VPN On Demand
   with "Always" for Wi-Fi and cellular
   (<https://tailscale.com/kb/1291/ios-vpn-on-demand>); on Android, the system
   "Always-on VPN" setting for Tailscale (**UNVERIFIED** wording).
2. ntfy app: Android from Google Play or F-Droid; iPhone from the App Store.
   Add the server URL, log in as `owner-phone`, subscribe to `seraphina`. On
   Android an `ntfy://<host>/seraphina` link (shown as a QR code in the
   cockpit) opens the app and subscribes (`docs/subscribe/phone.md`).
3. Seraphina sends a test notification with buttons; the owner taps one.

### 4.3 Phone apps: Android, and the iOS caveats

- **Android**: for a self-hosted server the app keeps its own connection open
  (a foreground service, "instant delivery"); "The ntfy Android app uses
  Firebase only for the main host ntfy.sh, and only in the Google Play flavor
  of the app. It won't use Firebase for any self-hosted servers, and not at
  all in the F-Droid flavor." The maintainer measures "about 0-1% of battery in
  17h" for that connection (`docs/subscribe/phone.md`; `docs/faq.md`). The
  Android app can also **publish** from a message bar, so the owner can type a
  short reply into `seraphina-replies`.
- **iOS**: "iOS heavily restricts background processing, which sadly makes it
  impossible to implement instant push notifications without a central
  server." With `upstream-base-url: "https://ntfy.sh"`, the self-hosted server
  sends ntfy.sh a `poll_request` that "contains only the message ID (in the
  `X-Poll-ID` header), and the SHA256 checksum of the topic URL", always with
  the text "New message"; ntfy.sh passes it via Firebase to Apple; the phone
  then fetches the real message from the owner's server. Without the upstream,
  "delivery can take hours". If the phone cannot reach the server at that
  moment (Tailscale not connected), it shows only "New message"
  (`docs/config.md` "iOS instant notifications"; `docs/known-issues.md`).
  The maintainer is frank that the iOS app "is very bare bones and quite
  frankly a little buggy" (`docs/faq.md`). The iOS app does not publish from
  the app (publishing is marked Android-only), so on iPhone replies are
  buttons only, or the cockpit.
- Action buttons work on Android, iOS and the web app (`docs/publish.md`).

### 4.4 Answering back: actions that POST to a URL

"You can define **up to three user actions**" per notification: `view` (open a
URL), `broadcast` (Android intent), `http` (send a request), `copy`. The `http`
action "sends a HTTP request when the action button is tapped ... By default,
this action sends a **POST request**"; fields `url`, `method`, `headers`,
`body`, and `clear` ("Clear notification after HTTP request succeeds. If the
request fails, the notification is not cleared") (`docs/publish.md`
"Action buttons", "Send HTTP request").

The request is made **by the phone**, so the URL must be reachable from the
phone: over the tailnet, that is the ntfy server itself. Pattern:

- Buttons POST a small JSON body `{"id": "<approval id>", "verb": "approve",
  "nonce": "<single-use>"}` to `https://<server>.ts.net:8443/seraphina-replies`
  with the `phone-reply` token in an `Authorization` header.
- Seraphina subscribes to `seraphina-replies` and checks the nonce, expiry and
  single use before acting (§13.3).
- This avoids pointing buttons at Seraphina's own HTTP API, which needs the
  cockpit bearer token (DESIGN.md §14) that must never ride in a notification.
- The `phone-reply` token is visible to anyone who can read the `seraphina`
  topic, which is only `owner-phone` and `seraphina`. Being write-only on the
  reply topic, a leaked copy could only post replies, and replies without a
  valid nonce do nothing.

### 4.5 Access control

`auth-default-access: deny-all` plus per-user ACLs (`read-write`,
`read-only`, `write-only`, `deny`), with tokens per user ("access tokens grant
users **full access to the user account**", so one user per role)
(`docs/config.md` "Access control", "Access tokens", "Example: Private
instance"). On a private, deny-all server, topic names do not need to be
secret. The server listens only on `127.0.0.1`; only `tailscale serve` exposes
it, and only to tailnet devices that Tailscale's own ACLs allow.

### 4.6 Privacy

Self-hosted on the tailnet: message text lives in the server's cache file (24 h
as configured) and on the phone. It crosses the network only inside Tailscale's
WireGuard tunnels (DERP relays, if used, see only ciphertext). On iOS with
the upstream set, ntfy.sh, Firebase and Apple see a message id, a hash of the
topic URL and timing; never text. No end-to-end encryption feature exists in
ntfy itself (none in the docs), so the server's own disk is the trust anchor,
which is the server Seraphina already guards.

### 4.7 Limits, reliability and the power-off

- Message bodies up to 4,096 bytes (longer become attachments; keep
  attachments off), title 1 KB; default 60-request burst per visitor refilling
  one per 5 seconds (`docs/publish.md` "Limitations").
- Reliability depends on Tailscale being up on both ends. That is a different
  failure mode from Telegram's and is why §13.7 pairs channels.
- **Power-off**: while the server is off, the phone cannot reach ntfy; button
  taps fail and the notification stays (because `clear` only happens on
  success), and the Android message bar cannot publish. Nothing is queued
  anywhere. Seraphina's 22:45 "going to sleep" notice should say so ("Replies
  will not reach me until 07:00").
- Engine-independent: the apt package runs as a native systemd service, so it
  works with Docker down (design §6.6).

### 4.8 Cost

Free (Apache 2.0 / GPLv2 dual licence). The apps are free.

### 4.9 stdlib sketch

```python
"""ntfy channel on the tailnet. Untested sketch."""
import json, os, time, urllib.parse, urllib.request

NTFY = os.environ["SERAPHINA_NTFY_URL"]                    # https://<server>.<tailnet>.ts.net:8443
TOKEN = os.environ["SERAPHINA_NTFY_TOKEN"]                 # user "seraphina"
REPLY_TOKEN = os.environ["SERAPHINA_NTFY_REPLY_TOKEN"]     # user "phone-reply", write-only

def _request(url, payload=None):
    headers = {"Authorization": "Bearer " + TOKEN}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    return urllib.request.Request(url, data=data, headers=headers)

def send(text, title=None, priority=3, click=None, approval=None):
    """approval: (approval_id, nonce). JSON publishing goes to the ROOT url, not the topic url."""
    msg = {"topic": "seraphina", "message": text, "priority": priority}
    if title:
        msg["title"] = title
    if click:
        msg["click"] = click                       # a cockpit page on the tailnet
    if approval:
        approval_id, nonce = approval
        msg["actions"] = [
            {"action": "http", "label": label, "url": NTFY + "/seraphina-replies",
             "headers": {"Authorization": "Bearer " + REPLY_TOKEN},
             "body": json.dumps({"id": approval_id, "verb": verb, "nonce": nonce}),
             "clear": True}
            for label, verb in (("Approve", "approve"), ("Not now", "defer"))]
        if click:
            msg["actions"].append({"action": "view", "label": "Details", "url": click})
    with urllib.request.urlopen(_request(NTFY + "/", msg), timeout=20) as resp:
        return json.load(resp)["id"]

def listen(handle, state):
    """Newline-delimited JSON stream; the server sends a keepalive event every 45 s."""
    while not state.stopping():
        since = state.last_id or "1h"             # message id, duration or unix time
        url = f"{NTFY}/seraphina-replies/json?since={urllib.parse.quote(since)}"
        try:
            with urllib.request.urlopen(_request(url), timeout=120) as resp:
                for line in resp:
                    event = json.loads(line)
                    if event.get("event") != "message":
                        continue                  # open, keepalive
                    try:
                        handle(json.loads(event["message"]))     # a button: check the nonce
                    except ValueError:
                        handle({"verb": "text", "text": event["message"]})  # typed on Android
                    state.save_last_id(event["id"])
        except (OSError, ValueError):
            time.sleep(10)                         # server restarting, tailnet down
```

---

## 5. Pushover

**Verdict: a reliable, cheap, one-way alarm; good as the independent "server
unreachable" channel, not as the main two-way channel.**

- **How it works**: `POST https://api.pushover.net/1/messages.json` with an
  application `token`, the owner's `user` key and `message`. Outbound only.
  Limits used by maintained clients: message 1,024 characters, title 250, URL
  512, URL title 100, attachment 5 MB (`gregdel/pushover` `pushover.go`;
  `ha-pushover-next` `const.py`).
- **Two-way**: only emergency priority (`priority=2`, with `retry` at least 30
  seconds and `expire` at most 10,800 seconds) returns a `receipt`; polling
  `GET /1/receipts/<receipt>.json?token=...` returns `acknowledged`,
  `acknowledged_by`, `acknowledged_at`, `expired`, `called_back` and
  timestamps (`gregdel/pushover` `receipt_details.go`). That is a yes/seen
  signal without any webhook. There is a `callback` URL option, but it needs a
  public endpoint. No buttons, no text replies.
- **Privacy**: Pushover reads content by default. Its apps (version 5.0 and
  later, per its support pages) support **end-to-end encryption**: "messages
  are sent to our API encrypted with a 256-bit key you create and store on
  your device, which is not visible to Pushover servers. When passing the
  `encrypted=1` parameter ..., the message, title, url, and url_title fields
  are also sent ... encrypted" (pushover.net/api via search extract,
  <https://pushover.net/api#e2ee>; **UNVERIFIED** wording). Scheme per field,
  as implemented and validated against Pushover's OpenSSL example by
  `ha-pushover-next` (`crypto.py`): gzip the UTF-8 text, AES-256-CBC with
  PKCS7 padding and a random 16-byte IV, HMAC-SHA256 over IV and ciphertext
  with the same key, then base64 of IV, ciphertext and HMAC. One key per
  account, shared by all devices. Metadata (timing, sender app, the owner's
  account) stays visible. A Pushover feature request criticised the design
  (shared hex key, CBC with HMAC instead of an AEAD mode)
  (<https://support.pushover.net/i385-asymetric-end-to-end-encryption>).
- **stdlib catch**: Python's standard library has no AES. Options: shell out to
  `openssl enc -aes-256-cbc -K <hex> -iv <hex>` (present on Ubuntu; but the key
  is then on a command line, visible to other local users in the process
  list while it runs), or allow the `cryptography` package for this one
  feature.
- **Reliability**: high on both platforms; emergency priority repeats until
  acknowledged.
- **Cost**: free 30-day trial, then $4.99 one-time per platform (iOS,
  Android, desktop); 10,000 messages a month free (sources disagree whether
  per application or per account: **UNVERIFIED**)
  (<https://pushover.net/licensing>, <https://support.pushover.net/i8-cost>,
  <https://support.pushover.net/i12-message-size-and-frequency-limitations>).
- **Setup**: create a Pushover account, install the app, create an
  "application" on the website for its token, paste the user key and app
  token into Seraphina's Secrets page.
- **Power-off**: n/a (one-way). Emergency receipts expire after at most three
  hours, so an unacknowledged alert at 22:00 is moot by 01:00 anyway.

```python
"""Pushover: send, and poll an emergency receipt. Untested sketch."""
import json, os, urllib.parse, urllib.request

APP, USER = os.environ["PUSHOVER_APP_TOKEN"], os.environ["PUSHOVER_USER_KEY"]

def send(text, title="Seraphina", priority=0, url=None):
    form = {"token": APP, "user": USER, "message": text[:1024], "title": title[:250],
            "priority": priority}
    if url:
        form["url"] = url[:512]                     # a cockpit page on the tailnet
    if priority == 2:
        form.update(retry=120, expire=3600)          # re-alert every 2 min for up to 1 h
    data = urllib.parse.urlencode(form).encode()
    with urllib.request.urlopen("https://api.pushover.net/1/messages.json", data, timeout=20) as r:
        return json.load(r).get("receipt")          # only for priority 2

def acknowledged(receipt):
    query = urllib.parse.urlencode({"token": APP})
    with urllib.request.urlopen(
            f"https://api.pushover.net/1/receipts/{receipt}.json?{query}", timeout=20) as r:
        d = json.load(r)
    return bool(d.get("acknowledged")), bool(d.get("expired"))
```

---

## 6. Gotify

**Verdict: private and simple, but one-way and Android-only. ntfy does
everything Gotify does here, plus buttons and iOS.**

- **How it works**: a self-hosted server (Go, single binary or Docker). An
  "application" token can only send; a "client" token can only receive and
  manage ("A client cannot send messages", `docs/index.md`). Send with
  `POST /message`, header `X-Gotify-Key: <apptoken>`, JSON `{"title",
  "message", "priority", "extras"}` (`docs/pushmsg.md`). Clients receive over a
  WebSocket at `/stream`.
- **Gotify 3** (current tag v3.1.1) dropped the YAML config for an env file
  (`/etc/gotify/server.env`) and shows tokens only once, on creation
  (`docs/migrate-to-3.md`). It also ships an MCP endpoint at `/mcp` so an AI
  agent can push notifications with an application token (`docs/mcp.md`):
  interesting, but Seraphina does not need MCP for a single POST.
- **Two-way**: none. `extras` give a click URL (`client::notification` →
  `click.url`), a big image, Markdown rendering, and an Android intent on
  receipt (`docs/msgextras.md`). The docs warn that Markdown images are
  fetched automatically and recommend `text/plain` for text from outside
  sources.
- **Phones**: official Android app only. On iPhone there is a third-party app,
  iGotify (<https://apps.apple.com/app/igotify/id6473452512>), not from the
  Gotify project (**UNVERIFIED** how it delivers push).
- **Privacy**: like self-hosted ntfy (content stays on the tailnet).
- **Cost**: free. **Power-off**: the server is off, nothing queued.

```python
def gotify_send(text, title, priority=5, click=None):
    body = {"title": title, "message": text, "priority": priority,
            "extras": {"client::display": {"contentType": "text/plain"}}}
    if click:
        body["extras"]["client::notification"] = {"click": {"url": click}}
    req = urllib.request.Request(os.environ["GOTIFY_URL"] + "/message",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Gotify-Key": os.environ["GOTIFY_APP_TOKEN"]})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["id"]
```

---

## 7. Signal (signal-cli or signal-cli-rest-api)

**Verdict: the best private two-way chat on the owner's existing phone, for an
owner willing to get a second phone number and accept upkeep. Not the novice
default.**

- **How it works**: signal-cli is an unofficial Signal client: "signal-cli is
  primarily intended to be used on servers to notify admins of important
  events", with a daemon offering JSON-RPC and D-Bus. With `daemon --http
  127.0.0.1:8080` it serves `POST /api/v1/rpc` (JSON-RPC), `GET
  /api/v1/events` (Server-Sent Events of incoming messages; "signal-cli keeps
  the last 1000 events in memory. A client that reconnects with a
  `Last-Event-ID` header first receives the events after that id") and `GET
  /api/v1/check` (`README.md`; `man/signal-cli-jsonrpc.5.adoc`). It keeps an
  outbound connection to Signal's servers, so no inbound port.
- **Registration needs**: "For registering you need a phone number where you
  can receive SMS or incoming calls"; landlines work with `register --voice`;
  "Registering may require solving a CAPTCHA". Registering a number "will
  unregister any existing client associated with the same number"
  (`README.md`). Most VoIP numbers reportedly do not work, and repeated
  attempts hit `429` rate limits (community reports via search: **UNVERIFIED**).
  A cheap prepaid SIM used only for this is the safe path.
- **Do not link it to the owner's own account.** `signal-cli link` makes it a
  linked device of an existing account. A linked device receives *all* of
  that account's messages, so the bot process would hold the owner's whole
  Signal inbox; and a bot on the owner's own account ignores the owner's own
  messages (OpenClaw's Signal docs: "For 'I text the bot and it replies,' use
  a **separate bot number**", `docs/channels/signal.md`). `--note-to-self`
  with `--notify-self` might give a notifying message to oneself
  (`man/signal-cli.1.adoc`), but this is **UNVERIFIED** on phones and still
  carries the whole-inbox problem.
- **Install**: native GraalVM build (no Java) or the JVM build, which needs
  "at least Java Runtime Environment (JRE) 25"; native libsignal is bundled
  for x86_64 Linux (`README.md`). "signal-cli releases older than three months
  may not work correctly" because official clients expire and the server can
  change, so upgrading is a recurring commitment Seraphina must track (a
  Housekeeping Tracker item with a due date).
- **signal-cli-rest-api** (Docker, tag 0.101) wraps it as REST
  (`/v2/send`, `/v1/receive`, `/v1/qrcodelink`); its quick start publishes
  `-p 8080:8080` on all interfaces with no authentication, and Docker's port
  publishing bypasses ufw. If used at all, bind `127.0.0.1:8080:8080`. As a
  container it fails the engine-independence rule; prefer the native daemon.
- **Two-way**: real chat; no bot buttons. Replies are text ("yes 7f3k") or
  reactions. Trust model: `--trust-new-identities on-first-use` (default)
  means a changed safety number (owner's new phone) blocks sending until
  re-trusted, which is both a security feature and a reliability chore.
- **Privacy**: end-to-end encrypted; Signal's servers see little metadata.
  Ciphertext still leaves the machine.
- **Terms**: Signal's terms bar bulk and auto-messaging, and its privacy
  policy bars "creating accounts ... through unauthorized or automated
  means" (mirrors and extracts:
  <https://tostracker.app/document/signal>,
  <https://conductatlas.com/platform/signal/signal-privacy-policy/legal-and-acceptable-use-obligation/>;
  **UNVERIFIED** current text). A low-volume personal notifier is common
  practice, but there is a non-zero ban risk and no official bot API.
- **Power-off**: messages the owner sends overnight queue at Signal for the
  bot's device and arrive at boot (Signal: "End-to-end encrypted Signal
  messages are queued for delivery when a device is offline",
  <https://support.signal.org/hc/en-us/articles/5532268300186-Disappearing-Messages-with-a-Linked-Device>;
  retention period **UNVERIFIED**).
- **Cost**: free, plus the second number.

Novice setup (Seraphina guides each step): buy a prepaid SIM; install the native
build to `/opt`; `signal-cli -a +44BOT register` (captcha link if asked), then
`verify CODE`; set a profile name and PIN; run `signal-cli -a +44BOT daemon
--http 127.0.0.1:8080` as a systemd service under a dedicated user; the owner
messages the bot number once; Seraphina pairs on a code, like Telegram.

```python
"""Signal through the local signal-cli daemon. Untested sketch."""
import itertools, json, os, urllib.request

RPC = "http://127.0.0.1:8080/api/v1/rpc"
OWNER = os.environ["SERAPHINA_SIGNAL_OWNER"]          # +44..., or the owner's ACI from pairing
_ids = itertools.count(1)

def rpc(method, params):
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}
    req = urllib.request.Request(RPC, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]

def send(text):
    return rpc("send", {"recipient": [OWNER], "message": text})

def listen(handle, state):
    headers = {"Accept": "text/event-stream"}
    if state.last_event_id:
        headers["Last-Event-ID"] = state.last_event_id
    req = urllib.request.Request("http://127.0.0.1:8080/api/v1/events", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:       # daemon pings every 15 s
        data = []
        for raw in resp:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("id:"):
                state.last_event_id = line[3:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif line == "" and data:                              # end of one event
                envelope = json.loads("\n".join(data)).get("envelope", {})
                data = []
                text = (envelope.get("dataMessage") or {}).get("message")
                if text and OWNER in (envelope.get("sourceNumber"), envelope.get("sourceUuid")):
                    handle(text)
```

---

## 8. Matrix and Element

**Verdict: the most capable fully private chat (own server, real apps on both
platforms, rooms, history), but too much to run for a novice. Revisit if the
owner later wants several personas as chat partners.**

- **Homeserver weight**: Synapse (Python, PostgreSQL recommended) is the
  reference server; the Rust servers continuwuity (latest v26.9.1, has a
  Debian/Ubuntu apt repository, `docs/deploying/debian.mdx`) and tuwunel are
  lighter. Reports are anecdotal: Synapse ran for years on a 1 CPU, 1 GB VPS
  but grew to about 15 GB of disk for four users; a single-user continuwuity
  "would use around 2GB RAM" untuned, "a lot less" without federation
  (<https://lemmy.manganiello.tech/post/270526>; **UNVERIFIED**). With
  `allow_federation = false` and the server only on the tailnet, it is a
  private island.
- **Client requirements**: Element X needs Simplified Sliding Sync; recent
  continuwuity changelogs show active Simplified Sliding Sync work
  (MSC4186), so it is supported there (`CHANGELOG.md`). Element X's SSO login
  needs the Matrix Authentication Service, which Conduit-family servers do
  not integrate; plain password login is the path for them (Element docs via
  search, <https://docs.element.io/latest/element-cloud-documentation/element-x-status-on-ems-cloud/>;
  **UNVERIFIED** for continuwuity specifically). The `server_name` is fixed
  forever at setup; on a tailnet it would be the `*.ts.net` name.
- **Push**: on Android, Element X supports UnifiedPush
  (`libraries/pushproviders/unifiedpush` in element-x-android), and "ntfy ...
  includes an embedded Matrix push gateway and can be self-hosted"
  (continuwuity `docs/guides/push.mdx`). So Matrix plus the §4 ntfy server
  gives push with no Google. On iPhone, push goes through Element's push
  gateway to Apple; Element says a notification "will not contain sensitive
  content" (room and event ids, unread count), and you cannot run your own
  gateway for the stock apps
  (<https://ems-docs.element.io/books/element-on-premise-documentation/page/notifications-mdm-push-gateway>).
- **Two-way**: full chat. No standard bot buttons (reactions or text replies).
- **stdlib**: login `POST /_matrix/client/v3/login`, send `PUT
  /_matrix/client/v3/rooms/{roomId}/send/m.room.message/{txnId}`, receive
  `GET /_matrix/client/v3/sync?since=<next_batch>&timeout=30000` (long
  polling). End-to-end encrypted rooms need Olm/Megolm (vodozemac or libolm),
  which stdlib cannot do, so the bot's room would be unencrypted. On a
  single-owner server reached only over the tailnet that is acceptable, but
  Element shows warnings and the server disk holds plaintext.
- **Power-off**: the homeserver is off overnight; the owner's client cannot
  send until 07:00 (whether Element queues and retries is **UNVERIFIED**).
- **Cost**: free; RAM and disk.

---

## 9. Discord bots

**Verdict: works, but Discord reads everything and receiving well needs a
WebSocket client stdlib does not have. Not recommended.**

- **Sending**: REST, `Authorization: Bot <token>`, a `User-Agent: DiscordBot
  ($url, $versionNumber)` header (`developers/reference.mdx`), API v10. Open a
  DM with `POST /users/@me/channels {"recipient_id": "<owner id>"}`, then
  `POST /channels/{id}/messages`. The bot and the owner must share a server
  (the owner makes a private one and invites the bot).
- **Receiving without a port**: the Gateway is a WebSocket (no stdlib client;
  a minimal RFC 6455 client is about 150 lines), or poll `GET
  /channels/{dm id}/messages?after=<last id>` over REST. Message content is a
  privileged intent, but bots without it still get "Content in DMs with the
  app" (`developers/events/gateway.mdx`). Global REST limit is 50 requests
  per second (`developers/topics/rate-limits.mdx`); polling every few seconds
  is fine. Buttons (message components) send interactions via the Gateway or
  an Interactions Endpoint URL, which is a public webhook; with REST polling
  only, there are no buttons.
- **Privacy**: Discord made voice and video end-to-end encrypted by default in
  March 2026 but says text messages "are not end-to-end encrypted"
  (<https://discord.com/blog/every-voice-and-video-call-on-discord-is-now-end-to-end-encrypted>,
  <https://www.techrepublic.com/article/news-discord-dave-end-to-end-encryption/>).
- **Power-off**: messages stay in the DM channel; polling with `after=` picks
  them up at boot. **Cost**: free.

---

## 10. Email (SMTP and IMAP)

**Verdict: universal and survives anything, good for a daily digest; poor for
urgent alerts and risky for inbound commands. Not the main channel.**

- **How it works**: send with `smtplib` (port 465 TLS or 587 STARTTLS to the
  provider; residential port 25 is usually blocked), read replies by polling
  IMAP with `imaplib` every minute or two. IMAP IDLE (push) arrived in the
  standard library only in Python 3.14 (`IMAP4.idle()`, CPython gh-55454);
  the harness supports 3.10, so poll.
- **Accounts**: use a dedicated mailbox for Seraphina, not the owner's own.
  Gmail needs 2-Step Verification and an app password for IMAP and SMTP
  (still available per 2026 guides, **UNVERIFIED** against Google's help).
  Personal Outlook.com accounts turned basic authentication off in September
  2024, so IMAP and SMTP there need OAuth2, which is not novice-friendly
  (<https://bluemail.me/app-password/outlook/>; **UNVERIFIED**). Fastmail and
  similar providers offer app passwords.
- **Privacy**: the mail providers on both ends read content. PGP through the
  `gpg` command could encrypt bodies (not headers or subjects), at the cost of
  key setup on the phone.
- **Inbound trust is the hard part**: the `From:` header is trivially forged.
  Accept a reply only if (a) the provider's `Authentication-Results` header
  shows `dkim=pass` for the owner's domain, (b) it answers a message Seraphina
  sent (`In-Reply-To` matches), and (c) it carries the nonce Seraphina put in
  the subject or a plus-address (`seraphina+7f3k9q@...`). Even then, treat
  email replies as low trust.
- **Reliability**: delivery is near-certain but latency is minutes, and
  notifications depend on the owner's mail app settings (mark the address as
  a VIP). Spam filters may bury automated mail.
- **Power-off**: mail waits at the provider; replies are read at boot.
  **Cost**: free with an existing provider.

```python
import email, imaplib, smtplib, ssl
from email.message import EmailMessage

def mail_send(text, subject, to, host, user, password):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(text)
    with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context()) as s:
        s.login(user, password)
        s.send_message(msg)
    return msg["Message-ID"]          # set one with email.utils.make_msgid() to thread replies

def mail_poll(host, user, password):
    with imaplib.IMAP4_SSL(host, ssl_context=ssl.create_default_context()) as m:
        m.login(user, password)
        m.select("INBOX")
        _, ids = m.search(None, "UNSEEN")
        for num in ids[0].split():
            _, parts = m.fetch(num, "(RFC822)")
            yield email.message_from_bytes(parts[0][1])   # caller checks DKIM, In-Reply-To, nonce
```

---

## 11. WhatsApp

**Verdict: not usable here. The official route needs a public webhook and a
business; the unofficial route risks a ban and hands a library the whole
account.**

- **Official (WhatsApp Business Platform, Cloud API)**: incoming messages
  arrive only as webhooks to a publicly reachable HTTPS endpoint with a valid
  certificate (provider docs, e.g.
  <https://docs.360dialog.com/docs/waba-messaging/webhook>; **UNVERIFIED** that
  no polling alternative exists). That is impossible with no forwarded ports
  and Funnel off. It also needs a Meta business account and number; since
  2025-07-01 Meta charges per delivered template message, by category and
  country, with free-form messages only inside an open customer-service
  window (<https://developers.facebook.com/docs/whatsapp/pricing>).
- **Policy**: Meta's updated Business Solution terms bar general-purpose AI
  chatbots where the AI is the core product; new users from 2025-10-15,
  everyone from 2026-01-15 (<https://respond.io/blog/whatsapp-general-purpose-chatbots-ban>,
  TechCrunch via
  <https://tech.yahoo.com/ai/meta-ai/articles/whatssapp-changes-terms-bar-general-141214399.html>).
  A personal AI assistant is at best a grey area under that rule.
- **Unofficial libraries** (Baileys, whatsmeow, whatsapp-web.js) act as a
  linked device of a normal WhatsApp account. They violate WhatsApp's terms,
  and numbers get banned based on behaviour; no library is immune
  (<https://zylos.ai/research/2026-01-26-whatsapp-api-automation>; **UNVERIFIED**
  ban rates). The library holds every chat of that account. OpenClaw's
  WhatsApp channel is Baileys-based and recommends a dedicated number
  (`docs/channels/whatsapp.md`). None is Python stdlib.

---

## 12. Also seen, not assessed in depth

- **SMS (Twilio and similar)**: outbound REST; inbound SMS can be fetched by
  polling the messages list instead of a webhook (**UNVERIFIED**). SMS content
  passes carriers in clear text; per-message cost; UK and US numbers need
  regulatory registration. Only as a last-resort alarm.
- **SimpleX Chat**: no phone numbers, end-to-end encrypted, a CLI with a
  WebSocket API for bots (**UNVERIFIED**; not checked). Worth a look later as a
  private alternative to Signal that needs no second number.
- **OpenClaw** (prior art, not a channel): its "pairing" policy is the right
  pattern. Unknown senders get an 8-character code that expires after an hour,
  their message is "**not processed** until you approve", and pending
  requests are capped at 3 per channel (`docs/channels/pairing.md`). Seraphina
  should do the same, with approval in the cockpit.

---

## 13. How this plugs into Seraphina

### 13.1 One channel interface

Each channel is a small module behind one interface, so the wake loop, the
notifier (§6.6) and the Housekeeping Tracker never know which one is in use.

```python
class Channel(Protocol):
    name: str                       # "telegram", "ntfy", "pushover", ...
    privacy: str                    # "tailnet" | "e2ee-third-party" | "third-party-readable"
    can_receive: bool               # two-way?
    has_buttons: bool
    max_text: int                   # 4096 (Telegram chars), 4096 (ntfy bytes), 1024 (Pushover)
    engine_independent: bool        # works with Docker stopped

    def send(self, notice: "Notice") -> "SendResult": ...       # raises ChannelError
    def receive(self, stop: threading.Event) -> Iterator["Inbound"]: ...   # long poll / stream
    def health(self) -> dict: ...   # last success, last error, token valid (getMe etc.)
```

- A receiver thread per two-way channel publishes `channel.inbound` events
  onto the EventBus; the wake loop subscribes. **An inbound message is a wake
  trigger**: it is exactly "message me and I wake up".
- Outbound notices go through a persisted queue (a JSONL file in the state
  directory), so a send that fails at 22:59 is retried after boot rather than
  lost, with `notify.sent`, `notify.failed` and `notify.reply` events.
- `health()` feeds Home: "Telegram: last message 2 h ago, token valid";
  a channel that has failed for an hour is a `needs_you` item on the other
  channels.

### 13.2 The outbound content gate (in code, not in the prompt)

The code that creates a notice sets its **sensitivity**; the model never does.

| Sensitivity | Examples | tailnet | e2ee-third-party | third-party-readable |
| --- | --- | --- | --- | --- |
| `pointer` | "Seraphina needs a decision (1). Open the cockpit." "Going to sleep, 2 items waiting." | yes | yes | yes |
| `operational` | "Docker update ready for Sunday 03:00", "Disk 91% full on /srv" | yes | yes | owner opt-in per channel |
| `personal` | anything a model wrote from the owner's files, chats or memory; names; health-adjacent service names | owner opt-in (§15) | owner opt-in (§15) | never |
| `secret` | tokens, keys, passwords, paths in the sensitive-data registry | never | never | never |

- Default for any free text a model wrote: `personal`. To reach Telegram it
  must be replaced by a fixed template plus a cockpit link ("Seraphina wrote
  you a note. Open it here.").
- The gate also runs the EventBus redaction (`redact` with env secrets) and a
  deny-list of terms derived from the sensitive-data registry, and refuses
  rather than truncates when in doubt.
- Links in notifications point to cockpit pages on the tailnet; a link is
  useless to anyone without the owner's tailnet, and opens the full content
  for the owner.

### 13.3 Inbound trust

1. **Pairing once, then an allowlist of immutable ids**: Telegram numeric user
   id plus `chat.type == "private"`; ntfy user (by ACL); Signal number or ACI;
   Matrix user id; email requires DKIM, `In-Reply-To` and a nonce. Strangers
   get nothing back and are logged; pairing requests appear in the cockpit,
   OpenClaw-style.
2. **Verbs are deterministic, not model output**: `approve`, `defer`,
   `snooze 2h`, `wake me at 07:30`, `pause` (stop all agent wakes until
   resumed), `status`. These run even when TabbyAPI is busy or the model is
   asleep. `pause` must work from every two-way channel; it is the kill switch.
3. **Buttons carry a single-use nonce** (8 to 12 random characters, well
   under Telegram's 64-byte limit) bound to one approval, with an expiry
   (default 30 minutes) and recorded use. An old message, a forwarded
   screenshot or a replayed callback does nothing.
4. **Phone approval is limited**: only items the cockpit marks
   `phone_ok` (low and medium risk) can be approved from a channel. Anything
   reboot-class, anything that runs an AI admin worker's SSH key against
   another host, deletes data or changes security settings needs the cockpit
   over the tailnet, and the notification says "Approve in the cockpit".
5. **Free text from the owner** goes to the persona as owner input tagged with
   its channel. Content the owner forwards or pastes (a link, a forwarded
   message) is untrusted data, like web content: it can inform, never command.
6. **Rate limits on inbound**: more than N commands a minute pauses the channel
   and alerts through another one (a stolen phone or token).

### 13.4 Account security is server security

Because a channel can talk to agents with SSH reach: Telegram two-step
verification on, bot token only in the env file, BotFather `/revoke` drill
documented in the Restore journey; ntfy tokens per role and deny-all; Signal
registration lock PIN set. Seraphina's Security card shows each channel's
state.

### 13.5 Quiet hours and the power-off

- At 22:45 (before the 23:00 power-off) one summary: what is pending, and
  which channels will hold replies overnight (Telegram, Signal, email) and
  which will not (ntfy, Gotify, Matrix).
- After boot and the design's quiet period, one morning digest, then replies
  received overnight, each with its age ("sent 23:41").
- Self-limits per persona: at most a few messages an hour, batching into a
  digest beyond that; `needs_you` items bypass batching.

### 13.6 Secrets

Env-file names (suggested): `SERAPHINA_TELEGRAM_BOT_TOKEN`,
`SERAPHINA_TELEGRAM_OWNER_ID` (not a secret, but kept beside the token),
`SERAPHINA_NTFY_URL`, `SERAPHINA_NTFY_TOKEN`, `SERAPHINA_NTFY_REPLY_TOKEN`,
`PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`, `PUSHOVER_E2EE_KEY`,
`SERAPHINA_MAIL_PASSWORD`. All are picked up by `env_secret_values()` for
redaction. Never log a Telegram request URL.

### 13.7 Pair a private channel with an independent one

| Failure | ntfy on tailnet | Telegram | Pushover |
| --- | --- | --- | --- |
| Docker engine down | works (native) | works | works |
| Tailscale broken on server or phone | **fails** | works | works |
| Internet down at home | works on home Wi-Fi only | **fails** | **fails** |
| Server crashed or powered off | fails | fails | fails |
| Vendor outage or account ban | n/a | **fails** | **fails** |

So: ntfy for content, Telegram (or Pushover) for `pointer` alerts, and the
design's off-machine heartbeat for the last row. Phase B's acceptance test
("stop the engine and confirm an alert still arrives") should also stop
`tailscaled` and confirm the independent channel still fires.

### 13.8 Config sketch

```json
{
  "channels": {
    "ntfy":     {"enabled": true,  "privacy": "tailnet", "url_env": "SERAPHINA_NTFY_URL",
                 "topic": "seraphina", "reply_topic": "seraphina-replies",
                 "allow": ["pointer", "operational"]},
    "telegram": {"enabled": false, "privacy": "third-party-readable",
                 "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN", "owner_id_env": "SERAPHINA_TELEGRAM_OWNER_ID",
                 "allow": ["pointer"]},
    "pushover": {"enabled": false, "privacy": "third-party-readable", "e2ee": false,
                 "allow": ["pointer"], "emergency_for": ["needs_you"]}
  },
  "routes": {"needs_you": ["ntfy", "telegram"], "rolled_back": ["*"],
             "persona_note": ["ntfy"], "digest": ["inbox", "ntfy"]},
  "approvals": {"nonce_minutes": 30, "phone_ok_max_risk": "medium"},
  "quiet": {"sleep_notice": "22:45", "max_per_hour": 4}
}
```

---

## 14. Recommendation in detail

1. **Build the interface and the content gate first** (§13.1, §13.2), with the
   cockpit inbox as the always-on channel.
2. **ntfy on the tailnet as the main channel.** It is the only option where
   real content stays on the owner's devices, it is already in the design, it
   runs natively (engine-independent), its buttons give approvals, and
   Seraphina can do all of the server work. The owner installs two apps and
   scans a code. On Android the experience is good; on iPhone it works, with
   the caveats in §4.3, and replies are buttons only.
3. **Telegram as the novice-friendly, works-anywhere channel**, opt-in, in
   pointer-only mode. If the owner wants a single answer to "what is easiest",
   this is it, and for a generic novice without a tailnet it would be the
   default. For this owner it complements ntfy rather than replacing it.
4. **Pushover only as an optional loud alarm** for `needs_you` and heartbeat
   failures, with e2ee if allowed (needs `openssl` or `cryptography`).
5. **Signal later**, if the owner wants to chat with a persona privately on the
   phone from anywhere: dedicated number, native signal-cli, upgrade tracked
   every three months.
6. **Not now**: Matrix (heavy), Discord (privacy, WebSocket), email (trust and
   latency; fine for a weekly digest), WhatsApp (impossible officially,
   unsafe unofficially).

---

## 15. Open questions for the owner

1. **iPhone or Android?** It changes how good ntfy is (§4.3) and whether
   F-Droid's Google-free build is possible.
2. **May any personal content appear on your own phone?** Over ntfy on the
   tailnet it never reaches a third party, but it does sit in the phone's
   notification history and lock screen. Default: no; notifications point to
   the cockpit.
3. **Is a Telegram account acceptable**, knowing Telegram reads the (pointer-
   only) messages? And will you turn on its two-step verification?
4. **Is the iOS ntfy wake-up ping through ntfy.sh, Firebase and Apple
   acceptable** (message id, topic hash, timing; no text)? Without it, iPhone
   notifications can be hours late.
5. **Which approvals may a phone tap give?** Proposed: low and medium risk
   only, everything else in the cockpit.
6. **Is a second phone number for Signal worth it** later?
7. **Which off-machine heartbeat service** (design §6.6) do you accept, and
   should Pushover be its alarm?

---

## Sources

Read from source (GitHub, cloned 2026-10-21):

- Telegram Bot API reference, machine-readable scrape (Bot API 10.3):
  <https://github.com/PaulSonOfLars/telegram-bot-api-spec>
- Telegram Bot API server: <https://github.com/tdlib/telegram-bot-api>
- ntfy docs: <https://github.com/binwiederhier/ntfy/tree/main/docs>
  (`publish.md`, `config.md`, `install.md`, `privacy.md`, `faq.md`,
  `known-issues.md`, `releases.md`, `subscribe/phone.md`, `subscribe/api.md`);
  site <https://docs.ntfy.sh/>
- Gotify docs: <https://github.com/gotify/website/tree/master/docs>
  (`index.md`, `pushmsg.md`, `msgextras.md`, `migrate-to-3.md`, `mcp.md`);
  server <https://github.com/gotify/server>
- signal-cli: <https://github.com/AsamK/signal-cli> (`README.md`,
  `man/signal-cli.1.adoc`, `man/signal-cli-jsonrpc.5.adoc`,
  `src/main/java/org/asamk/signal/http/`)
- signal-cli-rest-api: <https://github.com/bbernhard/signal-cli-rest-api>
- Discord API docs: <https://github.com/discord/discord-api-docs>
  (`developers/events/gateway.mdx`, `developers/topics/rate-limits.mdx`,
  `developers/resources/message.mdx`, `developers/resources/user.mdx`,
  `developers/reference.mdx`)
- continuwuity: <https://github.com/continuwuity/continuwuity>
  (`docs/guides/push.mdx`, `docs/deploying/debian.mdx`, `CHANGELOG.md`)
- Element X Android push providers:
  <https://github.com/element-hq/element-x-android/tree/develop/libraries/pushproviders>
- Pushover clients: <https://github.com/gregdel/pushover>,
  <https://github.com/neurolit/ha-pushover-next>
- OpenClaw channel docs: <https://github.com/openclaw/openclaw/tree/main/docs/channels>
  (`pairing.md`, `signal.md`, `whatsapp.md`, `telegram.md`)

Official pages known from search extracts (not fetched):

- Telegram: <https://core.telegram.org/bots/api>,
  <https://core.telegram.org/bots/api-changelog>,
  <https://core.telegram.org/bots/faq>, <https://tsf.telegram.org/manuals/e2ee-simple>
- Pushover: <https://pushover.net/api>, <https://pushover.net/api#e2ee>,
  <https://pushover.net/licensing>, <https://pushover.net/pricing>,
  <https://support.pushover.net/i8-cost>,
  <https://support.pushover.net/i12-message-size-and-frequency-limitations>,
  <https://support.pushover.net/i385-asymetric-end-to-end-encryption>
- Tailscale: <https://tailscale.com/kb/1153/enabling-https>,
  <https://tailscale.com/kb/1291/ios-vpn-on-demand>
- Signal: <https://support.signal.org/hc/en-us/articles/5532268300186-Disappearing-Messages-with-a-Linked-Device>
- Discord: <https://discord.com/blog/every-voice-and-video-call-on-discord-is-now-end-to-end-encrypted>
- Element: <https://docs.element.io/latest/element-cloud-documentation/element-x-status-on-ems-cloud/>,
  <https://ems-docs.element.io/books/element-on-premise-documentation/page/notifications-mdm-push-gateway>
- WhatsApp pricing: <https://developers.facebook.com/docs/whatsapp/pricing>
- Python `imaplib` IDLE (3.14): <https://docs.python.org/3/library/imaplib.html>

Secondary sources (marked UNVERIFIED where relied on):

- <https://www.deployhq.com/support/integrations/telegram> (token revoke)
- <https://community.openhab.org/t/telegram-exception-getupdates-failed-with-error-code-409/149661>
- <https://docs.aiogram.dev/en/dev-3.x/api/types/callback_query.html>
- <https://wk0.medium.com/send-and-receive-messages-with-the-telegram-api-17de9102ab78>
- <https://www.techrepublic.com/article/news-discord-dave-end-to-end-encryption/>
- <https://respond.io/blog/whatsapp-general-purpose-chatbots-ban>,
  <https://tech.yahoo.com/ai/meta-ai/articles/whatssapp-changes-terms-bar-general-141214399.html>
- <https://docs.360dialog.com/docs/waba-messaging/webhook>
- <https://zylos.ai/research/2026-01-26-whatsapp-api-automation>
- <https://tostracker.app/document/signal>,
  <https://conductatlas.com/platform/signal/signal-privacy-policy/legal-and-acceptable-use-obligation/>
- <https://lemmy.manganiello.tech/post/270526> (homeserver resource anecdotes)
- <https://bluemail.me/app-password/outlook/> (Outlook.com basic auth)
- <https://apps.apple.com/app/igotify/id6473452512> (third-party Gotify iOS app)
