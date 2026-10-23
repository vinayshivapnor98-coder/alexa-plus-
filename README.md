<<<<<<< HEAD
# Alexa+ Personal Agent — Conversational Hackathon Prototype

A local, talk-first personal assistant prototype. The main experience is the conversation panel; the feature pages are supporting controls and visual summaries, not the product's only interface.

> **Important:** This is an Alexa+-inspired hackathon prototype, not an official Amazon Alexa+ integration. Without an AI provider key, the app uses a local intent router for the supported actions. With an optional OpenAI-compatible API key, it can also answer general open-ended chat. Browser voice support depends on the browser and permissions. Do not expose the app or MCP server to the public internet.

## What is implemented

- **Conversational interface:** chat messages, recent conversation saved in browser local storage, quick prompts, typing, browser microphone input where supported, optional spoken replies, and contextual follow-up history for the optional language model.
- **Memory:** save, search/retrieve, and delete notes/preferences.
- **Study planner:** create/list/complete tasks, plan study sessions, and show study information in a daily briefing.
- **Inventory:** add/list items and check expired/near-expiry dates.
- **Mood-based meals and multi-day meal planning:** rule-based suggestions based on mood, dietary preference, available ingredients, cooking-time preference, and saved inventory/near-expiry items. Suggestions are not nutrition/allergen verification.
- **Reminders:** store dated reminders from the chat or API and include upcoming reminders in the briefing. **The prototype stores reminders but does not send operating-system or push notifications.**
- **Password vault:** master-passphrase setup/unlock, scrypt-derived key, Fernet authenticated encryption at rest, metadata-only listing, explicit reveal, delete, lock, and idle auto-lock while the process is alive.
- **Shopping assistant:** creates Amazon India and Flipkart product-search links from a request. It does not fetch or verify live prices, stock, sellers, or reviews.
- **Multilingual preference:** choose Auto, English, Hindi, or Hinglish for model prompts and browser voice where supported. Local command parsing remains strongest in English.
- **Cross-device conversation sync:** create a private sync token and enter it on another device connected to the same deployed server. This syncs chat history only; it is a bearer secret and must be kept private. Use HTTPS and proper authentication before any public deployment.
- **Connected-service status:** reports which services are configured and which remain simulated/unconnected.
- **MCP integration:** the same core functions are exposed through a Streamable HTTP MCP server for MCP Inspector and compatible clients.
- **Polished responsive UI:** an Alexa+-inspired dark blue hero, animated voice orb, voice-first call-to-action, feature cards, conversation panel, expandable feature drawer, and dedicated responsive feature pages.

## AI mode vs local mode

The project works without an API key for the supported actions. The local intent router handles save/retrieve memory, study tasks/sessions, inventory/expiry, meal suggestions, reminders, daily briefing, and safely directs password operations to the vault UI.

For open-ended, generative conversation, configure a provider that supports the OpenAI-compatible `POST /chat/completions` endpoint. The app does not ship with an API key.

1. Copy `.env.example` to `.env`.
2. Edit `.env` and set:

For Google's Gemini API, create a key at [Google AI Studio](https://aistudio.google.com/apikey), then copy `.env.example` to `.env` and configure:

```env
AI_API_KEY=your_gemini_api_key_here
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
AI_MODEL=gemini-2.5-flash
```

The available Gemini models and free-tier quotas can change, so check Google's current API pricing and limits before use. The app uses the provider's OpenAI-compatible Chat Completions endpoint; no provider key is included in this ZIP. Keep `.env` private and never commit it. If no key is set or the provider request fails, supported local actions continue to work and general chat returns a clear local-mode response.

## Hands-free voice mode (like Alexa)

Click the blue **🎧** button next to the chat box (or **Talk to your assistant** on the home banner).

- It listens, answers out loud, then listens again, so you can just talk back and forth.
- Optional **wake word**: tick "Say Alexa first" and start requests with "Alexa, ...". After each reply you can keep talking for about 12 seconds without repeating it.
- Say **"stop"**, **"goodbye"** or **"band karo"** to end it, press **Esc**, or tap **✕**. Tap the glowing orb to interrupt a spoken reply.
- Pick **English** or **Hindi** in the Language menu at the top. Voice mode needs **Google Chrome or Microsoft Edge** and microphone permission. Open the app at `http://127.0.0.1:5000`.
- In voice mode, the AI is asked for short, spoken-style answers.

## Troubleshooting the AI chat

1. Open **http://127.0.0.1:5000/api/ai-test**. It tells you whether the key was found and, if Gemini rejects the request, exactly why (HTTP 400/403 = key problem, 404 = wrong `AI_MODEL`, 429 = free-tier limit reached).
2. The file must be named exactly `.env` (not `.env.txt`) and sit next to `web_app.py`. Restart the app after editing it.
3. Chat error messages now show the real reason instead of a generic reply.

## Quick start on Windows

### Easiest option

Double-click `start_web.bat`. It creates `.venv` if needed, installs dependencies, and starts the browser interface.

Open **http://127.0.0.1:5000**.

### Manual setup

Use Python 3.12 if available. In PowerShell inside this folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python web_app.py
```

If Python 3.12 is not installed, use an installed supported Python version instead:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python web_app.py
```

Open **http://127.0.0.1:5000** in your browser. Microsoft Edge or Chrome is recommended for microphone input. You must grant microphone permission; if speech recognition is unsupported, type your message instead.

## Run MCP Inspector tools (optional)

Keep the web interface running. Open a second terminal and run `start_mcp.bat`, or:

```powershell
.\.venv\Scripts\Activate.ps1
python server.py
```

In MCP Inspector, add a **Streamable HTTP** server with URL:

`http://127.0.0.1:8000/mcp`

The MCP endpoint is not a website. Opening `http://127.0.0.1:8000/` directly can return `404 Not Found`; that is expected.

## Example things to say

- `Remember that my DBMS practical is Friday`
- `What do you remember about me?`
- `Add study task revise SQL queries tomorrow`
- `Show my study tasks`
- `Schedule a study session tomorrow`
- `Remind me tomorrow to revise SQL`
- `Which items are expiring soon?`
- `Show inventory`
- `Suggest a dish because I feel stressed`
- `What should I do today?`
- `Save my college password` — the assistant directs you to the dedicated vault rather than collecting secrets in chat.

Date parsing in local mode intentionally supports a small set such as today, tomorrow, day after tomorrow, and `YYYY-MM-DD`. Natural-language reminders default to 09:00 and are stored only; the assistant says so explicitly.

## Password vault safety

- Use **fake credentials only** during the hackathon demo.
- Do not send secrets in chat or paste them into screenshots/logs.
- The master password is not stored; entry content is encrypted before being written to SQLite.
- The vault's in-memory key is process-specific. If you use the web UI and MCP server separately, unlock it separately in each process.
- This implementation has not been independently audited. It is not a production-ready password manager; public deployment, rate limiting, CSRF protection, authentication, recovery, and broader hardening are not included.

## Data and privacy

- SQLite is created at `data/assistant.db` on first run.
- Existing database files are intentionally **not** bundled in the ZIP, so the ZIP will not overwrite the user's local saved memories/tasks.
- A few sample inventory items are added on first run by default. Set `LOAD_DEMO_DATA=0` in `.env` to disable that for a fresh database.
- Conversation history is stored in the current browser's local storage. Use the browser's site-data controls to clear it. Avoid typing secrets into chat.
- Both servers bind to loopback (`127.0.0.1`) and are intended for local demos only.

## Tests

After installing dependencies, run:

```powershell
python -m unittest discover -s tests -v
```

The test suite uses an isolated temporary SQLite database and fake vault credentials.

## Project structure

```text
AlexaPlus_Personal_Agent/
├── server.py                 # SQLite core + MCP tools + encrypted vault
├── web_app.py                # Flask API, local intents, optional LLM chat
├── requirements.txt
├── .env.example
├── start_web.bat             # Install and run the browser interface
├── start_mcp.bat             # Run the MCP tools server
├── templates/index.html      # Conversational UI + feature pages
├── static/app.js             # Chat, voice, UI actions, API calls
├── static/styles.css         # Responsive white/green UI
└── tests/test_web_app.py     # Local integration tests
```


## Advanced demo additions
- **Voice-first home page:** chat remains the default landing page; other tools are separate navigation items.
- **Home safety simulator:** bathroom light/heater and kitchen light/gas stove demo switches produce simulated warnings for the specified combinations. No physical appliance is controlled.
- **Arrival greeting:** add named family profiles and trigger a spoken greeting manually for a demo. No camera or automatic face recognition is used.
- **Local alert center:** add and display sample alerts. A live disaster/news feed is not connected; do not use this as an emergency warning system.
- **Alarms/reminders:** existing reminder flow stores scheduled reminders. Browser notifications are best-effort and depend on the app being open; use a dedicated phone alarm for critical wake-ups.

The requested smart-home and local-alert concepts are implemented as **safe demonstrations**, not live hardware/news integrations. Shopping results are marketplace search links rather than a verified product feed. Cross-device sync currently covers conversation history only; memories, tasks, inventory, and vault entries are not user-account synchronized. To use real switches, a properly secured gateway and fail-safe hardware are needed; gas/heater controls must be designed and reviewed by qualified professionals.


## Smart Delivery Assistant and Visitor Portal

- Open **Smart delivery** from Tools & Features to register orders and maintain a manual/demo shipment timeline. It does not yet connect to a carrier API; statuses and ETAs are not live-verified.
- Open **Visitor messages** in the homeowner dashboard to review visitor notes, urgent flags, and item reports.
- The public visitor form is available at `http://127.0.0.1:5000/visitor`. Visitors can submit text messages or item reports; this initial version does not record audio.
- Visitor messages persist in the local SQLite database. The homeowner inbox endpoint is not authenticated in this hackathon prototype. Keep the server on localhost; before any LAN/public deployment, add homeowner authentication, CSRF/rate limiting, and authorization controls.
- This prototype does not send guaranteed push/SMS notifications, verify parcel drop-offs, or alter official courier status.

## Family Voice Notes & Smart Reminders

- Open **Family voice notes** from Tools & Features. Choose a sender, recipient (one person or Everyone), and message type; record audio, preview it, and save it to the local inbox.
- Recipients can play the original recording, mark it heard, and mark reminder/task messages completed. An optional text note/title helps identify messages such as “Dinner is ready” or “Wash the bike”.
- Browser microphone access is required. Audio is saved under `data/family_audio/` and message metadata is stored in the SQLite database. Uploads are limited to 16 MB.
- This is a local hackathon prototype: the inbox is a shared dashboard, not a real account-based family inbox. It does not send push notifications or automatically deliver messages to a separate phone. Add authenticated family accounts, per-recipient authorization, protected audio delivery, and a remote deployment before using across devices or exposing it to a network.

## Voice & smart commands (new)

Open the app in **Chrome or Edge** at http://127.0.0.1:5000, click the 🎧 button (or "Talk to your assistant") and speak. Say "Alexa" first if you turned on the wake-word switch. You can minimise voice mode (▁) and keep talking while you use the app.

| Say this | What happens |
|---|---|
| "Alexa, remind me to take medicine after 2 minutes" | Saved with an exact time. When it is due the app beeps, shows it and speaks it. Also works with "in 1 hour 30 minutes", "at 5 pm", "tomorrow at 8 am", "set a timer for 10 minutes", "set an alarm for 6 am", and Hinglish like "2 minute baad dawai yaad dilao". |
| "What are my reminders?" / "Cancel all reminders" | Lists or cancels pending reminders. |
| "Alexa, play Kesariya" | Finds the song on YouTube and plays it in a mini player inside the app. "Stop the music", "pause", "resume", "volume down" control it. |
| "Play Shape of You on Spotify" | Opens Spotify (desktop app if installed, otherwise the web player) with the search. Spotify does not allow apps to press play, so you tap play yourself. |
| "Open YouTube" / "Search for best pizza near me" | Opens the site / a Google search on this computer. |
| "Add 2 packets of bread to inventory, expires tomorrow" | Adds the item and schedules expiry reminders (default: 2 days before at 9 AM, and on the expiry day at 8 AM). Add "remind me 3 days before" to change that. |
| "Add milk to inventory" … "25 October" | If you give no expiry, Alexa asks for it. |
| "Add the expiry for this is 30 October" / "Expiry for eggs is 4 November" | Sets or changes the expiry and re-schedules the reminders. |
| "Alexa, add this data in your database: my locker code hint is blue" | Saves a note in memory. |
| Any other question | Answered by Gemini (or Ollama if Gemini is down), with a joke. Set `ALEXA_PERSONALITY=normal` in `.env` to turn the jokes off. |

Notes and limits
- Reminders ring only while the app is open in a browser tab (a background tab may be up to a minute late).
- Music needs an internet connection. If a video cannot be embedded, it is opened on YouTube instead.
- The server opens websites/Spotify on the computer running the app, only for requests from this computer. Set `ALEXA_OPEN_ON_PC=0` to disable that.
- While music is playing, Alexa only reacts to commands that start with "Alexa", so the song is not mistaken for a command.

## Personal context features (v2)

- **Per-person data:** pick a person in the top bar (or say "switch to Rahul"). Each person has their own database in `data/people/`, so notes, tasks, study history, orders, contacts and location never mix. "Main" is the original `data/assistant.db`.
- **Study plan from history:** "make my study plan" / the button on the Study page. Uses when you usually finish work, how many minutes you do per day, and which subjects lag. Complete tasks so it keeps learning.
- **Live location & maps:** allow location in the browser. "Open map", "how far is Phoenix Mall", "how far is this", "directions to ...", "save my location as home". Uses OpenStreetMap (Nominatim + OSRM), so it needs internet.
- **Rides:** "book an Uber to the airport" opens Uber/Ola with pickup = you and the drop-off filled in. You confirm and pay in the app (no public API lets another app press Book).
- **WhatsApp:** "save Rahul's number 98765 43210", then "open WhatsApp and call Rahul" or "message Rahul on WhatsApp saying I'm late". It opens the chat; you tap the call/send button (WhatsApp does not allow apps to start calls).
- **Orders:** "I ordered AirPods on Amazon arriving Friday", "track my Amazon order", "my AirPods order is out for delivery". Amazon/Flipkart have no public tracking API, so status is what you tell Alexa plus a shortcut to the retailer's orders page.
- **Serious alerts:** every minute the page checks for overdue/due-today tasks, food expiring, urgent visitor messages, high local alerts and deliveries due. Shown as a toast, a desktop notification (click the bell once to allow) and spoken aloud. Ask "anything urgent?" any time. The tab must stay open.

## Management additions (v3)

Every sidebar page can now manage what it creates, not just add it:

- **Inventory & expiry:** `−` / `+` quantity buttons, **Expiry** (change or clear the date; the expiry reminders are re-scheduled automatically) and **Remove** (also removes that item's pending reminders). Items expired or expiring within 3 days are flagged.
- **Study planner:** **Delete** for tasks, plus a new **Reminders** panel (add with a date/time, see pending reminders, cancel) so reminders no longer need the chat.
- **Home safety demo:** the warning now also fires when the heater / gas stove is switched **ON** while its light is already OFF (previously only when the light was switched off).
- **Arrival greeting:** pressing *Play greeting* always speaks it (no need to tick "Speak replies"). Family profiles can be removed.
- **Family voice notes:** your family profiles automatically appear as recipients/filters; voice notes can be deleted (the audio file is deleted too).
- **Visitor messages / Local alerts:** delete and dismiss buttons.

New endpoints: `PATCH/DELETE /api/inventory/<id>`, `DELETE /api/tasks/<id>`, `DELETE /api/reminders/<id>`, `DELETE /api/local-alerts/<id>`, `DELETE /api/visitor/messages/<id>`, `DELETE /api/family-messages/<id>`, and `{"action":"delete","id":…}` on `/api/family-profiles`.

Still intentionally not built (needs external services/hardware, so it stays honestly labelled as a demo): live courier tracking, a live official disaster/news feed, real smart-home switches, camera-based arrival recognition, push/SMS notifications.

## Local SQL storage rule (v3.1)

All app data lives in the local SQLite database (`data/assistant.db`, or `data/people/<name>.db`). Saying **"save this: …"** or **"store this …"** always writes to it: a note goes to memory, and an item with an expiry date goes to inventory. A bare "save this" makes the assistant ask what to store. Every chat exchange is also logged in the `chat_log` table. Password requests are still sent to the encrypted vault.
=======
# alexa-plus-
Voice-first personal AI assistant that runs locally. Manage study tasks, reminders, food expiry, music, maps and an encrypted password vault by talking. Includes per-person profiles, Gemini chat and an MCP server.
>>>>>>> 0499708c1bb86fc4c93af6bfe665812c14862450
