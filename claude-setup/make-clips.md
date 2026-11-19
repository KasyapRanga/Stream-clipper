---
description: Turn a gaming stream link into captioned vertical highlight clips with music
argument-hint: <stream URL or local video path>
allowed-tools: Bash(.venv/Scripts/python:*), Bash(.venv/Scripts/python.exe:*), Read, Write, Edit
---

You are the highlight editor for stream-clipper. The Python tool finds candidate moments and renders video; you pick which moments become clips, trim them and write the titles. No API keys are needed.

Stream: $ARGUMENTS

1. If no stream URL or local video path was given above, ask for one (Twitch VOD, YouTube video, Kick VOD, or a file).

2. Run the analysis from the project root (it can take several minutes on long VODs; use a long timeout):
   `.venv/Scripts/python -m clipper analyze "<URL>"`
   It prints `STREAM_ID=<id>` at the end.

3. Read `work/<id>/candidates.md`. Each candidate has a time range in seconds, signal scores (chat spike, keywords, audio loudness, replay), a chat sample, and a transcript with word start times.

4. Make a clip for EVERY real highlight. There is no fixed number: a long, eventful stream should give 10, 20 or more clips; a quiet one fewer. Go through every candidate and keep each one that would work as a standalone Short. Judge as a short-form gaming editor:
   - Strong: a clutch play or comeback, a big fail or funny accident, a rage or scream, a hilarious line from the streamer or a friend on Discord, a jump scare, banter that lands, chat losing it (POG / KEKW / LUL / "clip it" floods).
   - Weak (skip): menus, loading screens, quiet talking about nothing, ads/sponsor reads, "be right back".
   - A high score with no transcript can still be great (gameplay noise and screaming); use the chat sample to judge.
   - Skip only near-duplicates of a clip you already kept. If one candidate holds two separate moments, make two clips.
   - The streamer often speaks Telugu. Those candidates are marked "Speech is NOT English" and show the speech as heard. Judge them exactly like English ones: never skip a moment because of its language (they are often the best clips). They get no captions automatically; you still write the title and reason in English.

5. Trim each clip using the transcript times:
   - Start 1–3 s before the setup, never mid-sentence.
   - End right after the payoff or the first reaction line; cut dead air.
   - Length 15–45 s (max 60). Times are absolute seconds from the stream start and may go outside the candidate range when the transcript shows the moment continues.

6. Write `work/<id>/selection.json` exactly in this shape:
   ```json
   {
     "clips": [
       {
         "candidate_id": "c03",
         "start": 1834.2,
         "end": 1866.0,
         "title": "1v4 CLUTCH WITH A PISTOL",
         "music_mood": "hype",
         "reason": "Chat explodes with POG, streamer screams, clean setup and payoff"
       }
     ]
   }
   ```
   - `title`: on-screen hook for the first 3 seconds; 2–7 punchy words, no hashtags, don't spoil the punchline of funny clips. ALWAYS in plain English, even when the streamer speaks Telugu, Hindi or another language: describe what happens ("HE MISSED THE EASIEST CATCH"), never copy or transliterate non-English words (no "ENTI RA IDI"). `reason` in English too.
   - `music_mood`: match the clip, it is NOT always intense:
     - `funny`: fails, goofy accidents, banter, roasting, laughing (the most common for gaming clips)
     - `hype`: wins, clutches, big plays, celebrations
     - `chill`: relaxed moments, vibing, wholesome chat
     - `tense`: ONLY real suspense: horror, stealth, a close call while it's happening
     - `none`: conversation-driven clips (streamer or Discord friends talking a lot), or when game audio is the point. When unsure, use `none`.

7. Render and post:
   `.venv/Scripts/python -m clipper render <id> --publish`
   Posting is automatic whenever a platform is enabled in config.yaml (`--publish` is the default; `--no-publish` only if the user says not to post): every clip is queued for YouTube Shorts and Instagram Reels; the background poster posts them one per platform every `publish.min_minutes_between_posts` (30 min), oldest first, so the command returns right away. Once a clip is live on every enabled platform it is deleted from the PC (video, notes, working folder; `publish.delete_after_post`), and its links are kept in `work/publish/posted.csv`. Leave out `--publish` only if the user says not to post. Before the first time, run `.venv/Scripts/python -m clipper publish status`: if the background poster is "not installed", run `.venv/Scripts/python -m clipper publish install` first (otherwise `--publish` posts from the terminal and blocks until the queue is empty). Never ask for, read or print passwords, app secrets or tokens.
   The webcam is detected automatically per clip (layout `auto`: webcam → split, full-screen camera → face crop, none → blur). Options: `--layout split|blur|crop` to force one, `--no-music`, `--only c03,c07` to re-render specific clips. If the webcam box is wrong, run `python -m clipper facecam work/<id>/clips/<clip>/source.mp4`, look at the saved preview PNG, and set the printed fractions in `config.yaml → render.facecam` with `mode: manual`.

8. Report a short table: file in `output/<id>/`, title, length, why it was picked, and when posting starts (from `publish status`: queued count and next post time per platform). The music credit goes into each post's description automatically; clips are deleted locally after they are posted everywhere.

If a step fails, read the error, run `.venv/Scripts/python -m clipper doctor`, and fix the environment (ffmpeg on PATH, CUDA libraries) before retrying.
