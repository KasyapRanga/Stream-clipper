---
description: Clip a stream while it is live (waits for the channel to go live, records, makes clips as highlights happen)
argument-hint: <channel URL, e.g. https://www.twitch.tv/name>
allowed-tools: Bash(.venv/Scripts/python:*), Bash(.venv/Scripts/python.exe:*), Read
---

Start live clipping for: $ARGUMENTS

1. If no channel or stream URL was given above, ask for one (Twitch channel, YouTube channel `/live` or stream link, Kick channel).

2. Start the live agent IN THE BACKGROUND (it runs for the whole stream, often hours):
   `.venv/Scripts/python -m clipper live "<URL>" --agent auto --publish`
   - It waits until the channel is live, records it in 30-second pieces, and watches chat and audio for highlights.
   - With `--agent auto` it hands batches of moments to an agent to pick, trim and title, in the order of `agents.priority` in config.yaml (default: Gemini CLI, which is Antigravity's agent, first; Claude Code if Gemini is missing or fails; signal-based picks as the last resort). Every agent's picks go through the same checks, so the clips come out the same way.
   - Use `--agent none` instead if the user wants it fully automatic on signals only (no Claude usage during the stream).
   - Add `--layout split|blur|crop` or `--no-music` if the user asked for them.
   - `--publish` posts every clip automatically to YouTube Shorts / Instagram Reels (one post per platform every 30 minutes; clips wait in a queue so every highlight gets posted). Leave it out only if the user does not want posting. First run `.venv/Scripts/python -m clipper publish status`: the platforms must be connected (`publish setup youtube|instagram`, which the user runs themselves because it asks for their sign-in) and set `enabled: true` under `publish` in config.yaml. Never ask for, read or print passwords, app secrets or tokens.

3. Posting does not stop when the stream ends. If `publish status` says the background poster is "not installed", run `.venv/Scripts/python -m clipper publish install` once (it creates a Windows task that keeps posting queued clips every 30 minutes, stream or no stream).

4. Tell the user it is running, that clips appear in `output/live-<channel>-<date>/` a minute or two after each moment, and that they can stop it any time with Ctrl+C in that terminal (it finishes the clips already found and saves the full recording to `work/<session>/recording.mp4`).

5. When the user asks how it is going, also run `.venv/Scripts/python -m clipper publish status` if `--publish` is on (what was posted, links, what is queued), read `work/<session>/live/status.json` (recorded time, candidates, finished clips, moments waiting for Claude) and report it briefly. The newest session name is in `work/last_live_session.json` after a run, or is the newest `work/live-*` folder.

Do not run the live command in the foreground: it would block this conversation for the whole stream.
