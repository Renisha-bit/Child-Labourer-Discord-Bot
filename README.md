# Child Laborer — Discord Music Bot

A custom Discord bot built with Python for a music-focused community server.

The bot handles server utilities, reaction roles, moderation, announcements, and automatic YouTube upload notifications. Music playback will be added later.

---

## ✦ Features

### Reaction Roles

Users can react to permanent role messages to assign or remove their own roles.

**Music Roles**

* 🎧 Listener
* 🎸 Musician
* 🎛️ Producer
* 🎤 Vocalist
* ✍️ Songwriter
* 📀 DJ

**Notification Roles**

* 🔔 Upload Ping
* 📢 VC Ping

**Game Roles**

* ⚔️ Valorant
* ⛏️ Minecraft

**Color Roles**

* 🔴 Red
* 🟠 Orange
* 🟡 Yellow
* 🟢 Green
* 🔵 Blue
* 🟣 Purple
* 🩷 Pink
* ⚪ White
* ⚫ Black

---

### Automatic Announcements

The bot can send announcements to a selected Discord channel using:

```text
.announce
```

The bot asks for:

1. The Discord channel ID
2. The announcement message

---

### Word Filter

The bot automatically detects prohibited words and deletes the message.

The user is then mentioned with a reminder to keep the conversation respectful.

---

### Welcome Messages

When a new member joins, the bot sends a welcome message in the configured welcome channel.

---

### YouTube Upload Notifications

The bot checks a configured YouTube channel for new uploads.

When a new upload is detected, it automatically sends a notification to the configured Discord channel.

Example:

```text
@Upload Ping
New upload: Song Name
https://www.youtube.com/watch?v=XXXXXXXXXXX
```

The video title and link are detected automatically, so they do not need to be manually changed for every upload.

---

## Planned Features

Music playback is planned for a later version.

Planned commands include:

```text
.play
.pause
.resume
.skip
.stop
.queue
.nowplaying
```

The goal is to allow members to listen to the singer's music together in a Discord voice channel while directing listeners to the original YouTube uploads.

---

## Requirements

* Python 3
* Discord account
* Discord Bot Application
* `discord.py`
* `python-dotenv`
* `aiohttp`

---

## Installation

Install the required packages:

```powershell
py -m pip install discord.py python-dotenv aiohttp
```

---

## Environment Variables

Create a `.env` file in the same folder as the bot.

Add:

```env
DISCORD_TOKEN=YOUR_BOT_TOKEN
```

**Never share your bot token or commit your ****`.env`**** file to GitHub.**

---

## Configuration

Several values need to be configured inside the Python file.

### Reaction Role Channel

```python
REACTION_ROLE_CHANNEL_ID = 123456789012345678
```

Replace the number with the ID of the Discord channel where the reaction-role messages should be created.

### YouTube Channel

```python
YOUTUBE_CHANNEL_ID = "UCxxxxxxxxxxxxxxxxxxxx"
```

Replace this with the ID of the YouTube channel the bot should monitor.

### Upload Notification Channel

```python
UPLOAD_NOTIFICATION_CHANNEL_ID = 123456789012345678
```

Replace this with the Discord channel ID where new-upload notifications should be sent.

---

## Running the Bot

Open the project folder in VS Code and run:

```powershell
py main.py
```

Keep the terminal running while you want the bot online.

To stop the bot:

```text
Ctrl + C
```

---

## Commands

| Command        | Description                                 |
| -------------- | ------------------------------------------- |
| `.ping`       | Checks whether the bot is responding        |
| `.announce`    | Sends an announcement to a selected channel |
| `.setup_roles` | Creates the reaction-role messages          |

---

## Required Discord Permissions

The bot needs permissions appropriate to the features being used.

Current important permissions include:

* View Channels
* Send Messages
* Read Message History
* Add Reactions
* Manage Roles
* Manage Messages

The bot's highest role must be **above all roles that it needs to assign**.

## Project Structure

```text
Discord Bot/
│
├── main.py
├── .env
├── discord.log
└── README.md
```

---

## Important

### Keep your token private

Never upload this:

```env
DISCORD_TOKEN=your_actual_token
```

to GitHub or send it publicly.

If the token is accidentally exposed, regenerate it through the Discord Developer Portal.

### Reaction Roles

Run:

```text
.setup_roles
```

only when you actually want to create the reaction-role messages. Running it repeatedly will create additional copies.

---

## Project Status

**Current**

* [x] Bot setup
* [x] .ping
* [x] Welcome messages
* [x] Word filter
* [x] .announce
* [x] Reaction roles
* [x] YouTube upload checker

**Planned**

* [ ] Music playback
* [ ] Queue system
* [ ] Pause / resume
* [ ] Skip / stop
* [ ] Now playing
* [ ] Additional music commands
* [ ] Instagram upload notifications

---

## Purpose

This bot is being developed specifically for a music community, with the goal of making the server more interactive while helping members discover and listen to the artist's music.

Built with Python + Discord.py.

**Child Laborer™ — because apparently managing a Discord server wasn't enough.**
