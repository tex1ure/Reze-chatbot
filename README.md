# Reze / Makima Bot

a feature-packed discord bot built with discord.py, motor (mongodb), and flask. handles natural AI chat across multiple providers, simulated human typing behavior, meme generators, interactive mini-games, an e-family system with visual tree rendering, custom anime action emotes, and a web admin dashboard.

---

## Features Overview

### Multi-Provider AI Engine (`ai_handler.py`)
the bot doesn't rely on just one AI provider. it includes built-in failovers and key rotators:
* **Google GenAI**: supports gemini and gemma models (`gemma-4-31b-it`) with multi-key rotation.
* **Groq API**: fallback to `llama-3.3-70b-versatile` with key rotation.
* **Cerebras API**: ultra-fast inference fallback using `llama-3.3-70b`.
* **SiliconFlow / SiliconCloud**: fallback support for models like `DeepSeek-V3` and `Qwen2.5-72B`.
* **Google Search Integration**: performs live web searches to answer real-time questions.
* **Smart Memory Compression**: automatically summarizes old channel history into MongoDB documents when conversation length exceeds threshold.
* **Dynamic Mood System**: shifts between `NORMAL`, `YAPPING`, `LEWD`, and `BORED` modes.
* **Absence & Return Detection**: remembers when users return after days away and brings it up.
* **Grudge Tracking**: keeps track of annoying user behavior to apply temporary cool-downs or roasts.

### Simulated Human Behavior
* **Typing delays**: realistic typing speeds and hesitation pauses before replying.
* **Organic typos**: introduces typos (normal and drunk mode chances) and sends follow-up corrections.
* **Message edits & deletions**: randomly edits or deletes recent messages.
* **Left-on-read reactions**: places subtle reactions when ignoring or taking time to respond.
* **Eavesdropping & wrong-chat texts**: randomly chimes in or sends fake wrong-chat snippets.
* **Dynamic status schedules**: updates Discord status based on an IST hourly schedule.

---

## Command Reference (Prefix: `$`)

### General & Lookup
* `$anime [query]` - search MyAnimeList for anime info.
* `$manga [query]` - search MyAnimeList for manga info.
* `$movie [query]` / `$show [query]` - fetch IMDb/OMDb details.
* `$avatar [@user]` / `$av` - grab a user's avatar.
* `$translate [lang] [text]` / `$tl` - translate text or replied messages.
* `$weather [city]` - check real-time weather.
* `$ping` - check bot response latency.
* `$uptime` - view bot uptime.
* `$server` - display server info.
* `$poll [question] | [opt1] | [opt2]` - create interactive reaction polls.

### Interactive Games & Fun
* `$akinator` - play Akinator directly in discord chat using buttons.
* `$choose [opt1 | opt2]` - let the bot pick between choices.
* `$quote [@user] [text]` - generate a cinematic quote card image.
* `$truth` / `$dare` - play Truth or Dare powered by Llama 3.3.
* `$wyr` / `$wouldyourather` - play Would You Rather polls.
* `$gay [@user]` / `$lesbian [@user]` - check fun percentage ratings.
* `$waifu` / `$husbando` - fetch random anime characters with voting.
* `$mkkf` / `$mkkm` - play Marry, Kiss, Kill (female/male).
* `$villain` - battle two iconic anime villains.
* `$cat` / `$dog` - get random animal pictures.
* `$confess [text]` - send an anonymous confession (DM only).

### Mini-Games (`games.py`)
* **Russian Roulette**: spin the cylinder and pull the trigger with interactive buttons.
* **Blackjack**: hit or stand in a classic card game vs the bot.
* **Rock-Paper-Scissors**: play solo vs bot or challenge server members in multiplayer.
* **Tic-Tac-Toe**: 2-player interactive grid battle.
* **Anime Trivia**: answer questions fetched from OpenTDB.
* **Word Scramble**: unscramble word prompts against the clock.

### Meme Generator Engine
* `$wanted [@user]` / `$bounty` - create a One Piece wanted poster.
* `$jail [@user]` - put a user behind photorealistic jail bars.
* `$rip [@user] [reason]` - generate a gravestone meme.
* `$gandhi [text]` / `$fakequote` - generate a fake quote banner on a Gandhi background.
* `$simp [@user]` - issue a Certified Simp Card.
* `$wasted [@user]` - apply a GTA sepia red Wasted overlay.
* `$trashcan [@user]` - drop an avatar into the trashcan.
* `$impersonate [@user] [text]` - mimic someone using temporary webhooks.
* `$ship [@user]` - calculate love compatibility and generate a ship card image.

### Action Emotes (`$[action] [@user]`)
* **Affection**: `pat`, `hug`, `kiss`, `cuddle`, `handhold`, `feed`, `tickle`, `highfive`, `cheer`, `wink`, `smile`, `happy`, `blowkiss`
* **Expression**: `blush`, `cry`, `cringe`, `confused`, `facepalm`, `bored`, `tired`, `sleep`, `sad`, `scream`, `panic`, `yawn`, `surprised`, `chase`, `run`, `handshake`, `tailwhip`, `nope`
* **Playful**: `poke`, `nom`, `lick`, `bleh`, `wave`, `dance`, `smug`, `shrug`, `nod`, `thumbsup`, `lurk`, `think`, `stare`
* **Chaos**: `slap`, `punch`, `kick`, `yeet`, `bite`, `bonk`, `threaten`, `angry`, `baka`

### E-Family System
* `$marry [@user]` - propose marriage to another member.
* `$adopt [@user]` - adopt a member as your child.
* `$divorce` - divorce your current spouse.
* `$disown [@user]` - disown a child.
* `$abandon [@user]` - abandon a parent.
* `$runaway` - leave home and remove parents.
* `$disownall` - disown all children.
* `$family [@user]` - generate and view an image of a user's family tree graph.

### Server Config (Slash Commands)
* `/setup` - view current server configuration.
* `/setrole [type] [role]` - assign male, female, hinglish, or lewd roles.
* `/setchannel [type] [channel]` - assign target, story, nsfw, or confession channels.
* `/resetconfig` - reset server config back to default settings.
* `/nsfw` - toggle NSFW mode (DM only).

---

## Web Dashboard (`dashboard.py`)

includes a built-in Flask REST API for remote management:
* authenticated session access using `DASHBOARD_PASSWORD`.
* update live config values (`rate_limit_count`, `temperature`, `model`, probabilities) without bot restarts.
* sync and monitor MongoDB user relationship data and channel memories.

---

## File Structure

```
.
├── main.py              # entry point, commands, event listeners, family tree image renderer
├── ai_handler.py        # multi-provider AI logic (Gemini, Groq, Cerebras, SiliconFlow) & search
├── bot_config.py        # thread-safe mutable configuration manager
├── db.py                # async MongoDB manager (Motor)
├── games.py             # mini-game UI views & logic
├── dashboard.py         # Flask REST API blueprint
├── keep_alive.py        # lightweight Flask ping server
├── requirements.txt     # python packages
├── Dockerfile           # container build script
├── compose.yaml         # docker compose definition
├── Procfile             # PaaS deployment directive
├── README.Docker.md     # docker setup instructions
├── LICENSE              # custom non-commercial software license
└── assets/              # meme overlays, reaction images, and custom cards
```

---

## Environment Setup

Copy `.env.example` to `.env` and fill in credentials:

```env
DISCORD_TOKEN=your_discord_bot_token
APPLICATION_ID=your_application_id

# AI Providers (comma-separated for key rotation)
GEMINI_API_KEY=your_gemini_key_1,your_gemini_key_2
GROQ_API_KEY=your_groq_key
CEREBRAS_API_KEY=your_cerebras_key
SILICON_API=your_siliconflow_key

# Database
MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/?appName=RezeBot

# Dashboard & External APIs
DASHBOARD_PASSWORD=your_dashboard_password
OMDB_API_KEY=your_omdb_key
```

---

## Local Development

```bash
# clone repository
git clone https://github.com/your-username/Makima-chatbot.git
cd Makima-chatbot

# create virtual environment
python -m venv venv
source venv/bin/activate  # venv\Scripts\activate on Windows

# install packages
pip install -r requirements.txt

# run bot
python main.py
```

---

## Docker Deployment

```bash
# launch container
docker compose up -d --build

# check logs
docker compose logs -f
```

For full container setup instructions, check [README.Docker.md](file:///b:/Discord%20bots/Makima-chatbot/README.Docker.md).

---

## Config Overview (`bot_config.py`)

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `rate_limit_count` | `5` | max messages allowed per window |
| `rate_limit_window` | `40` | rate limit window in seconds |
| `temperature` | `0.9` | AI sampling temperature |
| `model` | `gemma-4-31b-it` | primary model identifier |
| `memory_compress_threshold` | `20` | message count trigger for context compression |
| `left_on_read_react_chance` | `0.30` | chance of dropping reaction when lagging |
| `typo_chance_normal` | `0.05` | typo probability in normal mode |
| `unprompted_enabled` | `True` | allow bot to send random messages in quiet channels |

---

## License

This project is licensed under a custom non-commercial license. You are free to use, modify, and host this bot for personal or educational use. Commercial use or selling access to this bot without permission is prohibited. See the [LICENSE](file:///b:/Discord%20bots/Makima-chatbot/LICENSE) file for details.

---

## Disclaimer

Check images in `assets/memes/` before hosting publicly. Follow Discord's TOS and AI provider usage policies.
