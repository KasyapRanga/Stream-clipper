# stream-clipper

Paste a gaming stream link and get vertical highlight clips with captions and background music.

```
stream link ─► analyze ─► candidates.md ─► agent picks & trims ─► render ─► output/*.mp4
               (chat + audio + replay           (writes selection.json,          (9:16, karaoke
                signals, Whisper on GPU)          no API keys)                     captions, ducked music)
```

Works with Twitch VODs, YouTube videos and VODs, Kick VODs (anything yt-dlp supports) and local video files.

## Setup (Windows, once)

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

This creates `.venv`, installs the Python packages plus the CUDA libraries for Whisper, installs ffmpeg with winget if needed, and runs `clipper doctor`. Reopen the terminal if ffmpeg was just installed.

Optional: put [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader/releases) on PATH for the most reliable Twitch chat download. Otherwise `chat-downloader` is used.

## Use it from Claude Code

Open a terminal in this folder, start `claude`, and type:

```
/make-clips https://www.twitch.tv/videos/1234567890
```

Claude Code runs the analysis, picks and trims the best moments, writes the titles, and renders. It uses your Claude login, so no API key is needed.

Fully automatic, without an interactive session:

```powershell
.venv\Scripts\python -m clipper run "<url>" --agent claude
```

## Live mode: clips while the stream is still going

```powershell
.venv\Scripts\python -m clipper live https://www.twitch.tv/<channel> --agent claude
```

or in Claude Code: `/live-clips https://www.twitch.tv/<channel>` (runs it in the background).

- Start it before the stream: it checks every 2 minutes until the channel goes live.
- It records the stream in 30-second pieces (copied, no quality loss), reads Twitch chat live (anonymous, read-only; YouTube chat via chat-downloader), and scores audio + chat as it arrives.
- Each highlight is cut, transcribed and rendered about a minute or two after it happens (once the reaction has played out). `--agent claude` lets Claude Code judge batches of moments (3 at a time, or every 5 minutes); `--agent none` clips everything above `live.min_score` without using Claude.
- Survives dropped connections, stops by itself when the stream ends (or Ctrl+C), finishes the clips it already found, and saves the whole stream as `work/<session>/recording.mp4` for a later full `analyze` pass.
- Clips go to `output/live-<channel>-<date>/`; progress is in `work/<session>/live/status.json`.
- Disk: about 2–3 GB per hour of 1080p60. Settings: `config.yaml → live`.
- Test it without a stream: `python -m clipper live my-recording.mp4 --speed 4` plays a file as if it were live.

Only clip streams you have the rights to (your own, or creators whose clipping programme you are in).

## Use it from Antigravity

Antigravity gets the same two commands as Claude Code. Open this folder in Antigravity and, in the agent panel, type:

```
/make-clips https://www.twitch.tv/videos/1234567890
/live-clips https://www.twitch.tv/<channel>
```

`/make-clips` works exactly like in Claude Code: the agent runs the analysis, picks and trims every real highlight, writes the titles and renders. The workflows mark their python steps with `// turbo`, so Antigravity runs them without asking each time (your terminal settings in Antigravity still apply).

For the hands-off parts (fully automatic `run`, and `live` judging batches while you stream), a background program has to call an agent by itself. Antigravity's built-in agent can't be called that way, so these use **Gemini CLI**, Google's command-line agent, with your Google sign-in (no API key):

```powershell
npm install -g @google/gemini-cli     # once (needs Node.js); then run `gemini` once and sign in with Google
.venv\Scripts\python -m clipper run "<url>" --agent gemini
.venv\Scripts\python -m clipper live https://www.twitch.tv/<channel> --agent gemini
```

Gemini gets the same rules as Claude (steps 4–6 of `make-clips`) and only returns the selection; the tool writes the file itself, so Gemini needs no file or shell permissions. If Gemini CLI is missing or fails, the tool falls back to signal-based picks. Pick a model with `agents.gemini_model` in `config.yaml` (empty = Gemini CLI's default).

### Who picks the clips (Antigravity first)

`live` and `run` use `--agent auto` by default: the agents in `config.yaml → agents.priority` are tried in order, and the default order is **Gemini CLI first (Antigravity's agent), then Claude Code**, then signal-based picks if neither works. Both agents get the same rules (steps 4–6 of `make-clips`), the same candidates, and their answers pass through the same checks (known moments only, 15–60 s, inside the recorded window, short upper-case hook title, valid music mood), so the clips come out the same way whichever agent picked them. Force one with `--agent gemini` or `--agent claude`.

Keeping the two in sync: the Claude commands in `claude-setup/` are the source. After changing one, run `.venv\Scripts\python tools\sync_antigravity.py` to regenerate `.agent/workflows/`.

## Use it from the terminal

```powershell
.venv\Scripts\python -m clipper run "<url>"                 # fully automatic, signals only
.venv\Scripts\python -m clipper analyze "<url>"             # step 1: find candidates
#   ...edit work\<id>\selection.json, or let the agent write it...
.venv\Scripts\python -m clipper render <id>                 # step 2: render
.venv\Scripts\python -m clipper render <id> --layout split --only c03
.venv\Scripts\python -m clipper music fetch                 # pre-download the free music
.venv\Scripts\python -m clipper doctor                      # check ffmpeg / CUDA / packages
```

## Auto-posting to YouTube Shorts and Instagram Reels

Add `--publish` and every finished clip is posted, also during a live stream:

```powershell
.venv\Scripts\python -m clipper live https://www.twitch.tv/<channel> --agent claude --publish
.venv\Scripts\python -m clipper run "<VOD or file>" --publish
.venv\Scripts\python -m clipper publish post output\<id>\03_clip.mp4      # post one clip now
.venv\Scripts\python -m clipper publish status                           # what was posted (links), queue, limits
```

Captions: the clip title, "Clipped live from <stream link>", your hashtags and the music credit (required by the CC BY licence) are filled in automatically. YouTube gets `#shorts`, category Gaming and "not made for kids".

Posting rhythm (`config.yaml → publish`): every clip is posted, oldest first, with at least 30 minutes between two posts on the same platform. When highlights come faster, they wait in the queue (`work/publish/queue.json`) and keep going out every 30 minutes, also after the stream ends and across restarts. No approval needed (`mode: auto`); set `mode: review` if you ever want to approve clips first. A failed upload is retried (5, 10, 15 … up to 60 minutes apart) until it goes through, so no highlight is dropped. Only one program posts at a time, so a live run and a `publish run` window never double-post.

**Posting keeps going after the stream.** Run this once:

```powershell
.venv\Scripts\python -m clipper publish install
```

**Posted clips are deleted from the PC** once they are live on every enabled platform (`publish.delete_after_post: true`): the video, its notes and its working folder. The links are kept in `work\publish\posted.csv`. Set it to `false` to keep them.

It adds a small Windows background task (`stream-clipper-poster`, no window) that starts when you log in and posts whatever is queued every 30 minutes, whether or not you are live. The live command fills the queue; the background poster empties it. `publish status` shows whether it is running, how many clips are queued and when each platform posts next; its log is `work\publish\poster.log`. Remove it with `publish uninstall`. The PC has to be on (and not asleep) for posts to go out. Without the task, the live window keeps posting after the stream as long as you leave it open. When the stream ends, the queue keeps posting with the same spacing (Ctrl+C to stop; `publish run` continues later).

Platform limits: YouTube's API allows 100 uploads per day per project; Instagram allows up to 100 API posts per 24 hours (the tool checks Instagram's live count before each post).

### Connect YouTube (once, about 10 minutes)

1. Go to console.cloud.google.com, create a project (any name).
2. APIs & Services → Library → **YouTube Data API v3** → Enable.
3. OAuth consent screen (Google Auth Platform): user type **External**, fill in an app name and your email, add your own Google account as a test user. Then set the app to **In production** ("Publish app"). In testing mode Google makes the sign-in expire every 7 days. The "unverified app" warning you'll see when signing in is expected for your own app: Advanced → continue.
4. Clients → Create client → **Desktop app** → download the JSON and save it as `secrets\client_secret.json` in this folder.
5. Run `.venv\Scripts\python -m clipper publish setup youtube --client-secret secrets\client_secret.json` and sign in with the account that owns your channel.
6. In `config.yaml`, set `publish → youtube → enabled: true`.

### Connect Instagram (once, about 15 minutes)

Instagram only allows API posting from professional accounts linked to a Facebook Page.

1. In Instagram: Settings → Account type and tools → switch to a **Creator** (or Business) account.
2. Link it to a Facebook Page (create a Page for your gaming channel if you don't have one): Meta Accounts Center or the Page's settings → Linked accounts → Instagram.
3. At developers.facebook.com → My Apps → Create app. Choose the **Business** type / the use case for managing Instagram content with Facebook Login. As the app's admin you can post to your own account without Meta's App Review.
4. Open **Graph API Explorer**, pick your app, choose "User Token", add the permissions `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement` (add `business_management` if your Page is in a Business portfolio), then Generate Access Token and copy it.
5. Run `.venv\Scripts\python -m clipper publish setup instagram` and paste the App ID, App Secret (App settings → Basic) and that token. Secret and token are typed hidden; the tool swaps the token for a 60-day one and finds your Instagram account.
6. In `config.yaml`, set `publish → instagram → enabled: true`.

The Instagram token lasts about 60 days: `publish status` shows the date; repeat steps 4–5 before then.

Your sign-ins are stored in `secrets\` (ignored by git). Keep that folder private and never share it.

## How highlights are found

| Signal | What it measures |
|---|---|
| Chat rate | Messages per second compared with the stream's own baseline, shifted back ~6 s for reaction delay |
| Chat keywords | POG / KEKW / LUL / "clip it" floods (list in `config.yaml`) |
| Audio | Loudness spikes compared with the surrounding minute (screams, explosions) |
| Replay heatmap | YouTube "most replayed", when available |

The top peaks are transcribed with faster-whisper (word timestamps) and snapped to sentence boundaries. Only the audio track is downloaded for analysis; video is fetched just for the chosen sections.

## Captions, titles and language

Everything a viewer reads is English:
- **Captions** appear only when the speech in a clip is clearly English (Whisper hears English and most words are real English words). Clips where you speak Telugu, Hindi or a mix get **no captions** rather than garbled ones. Settings: `render.captions.english_only` and `min_english_ratio`.
- **Titles and post text** are always English. The agent is told to describe the moment in English (never copy or transliterate Telugu), and every title is checked: other scripts and romanized filler ("enti ra", "kya hai") are removed, and if too little English is left the clip gets an English hook that fits its mood ("THIS WAS TOO FUNNY", "WHAT A PLAY").

## How many clips

There is no fixed number. Long streams get more candidates (about one per 4 minutes, up to 60), and the agent makes a clip for every real highlight. In signals-only mode (`run` without `--agent`), every candidate scoring at least `highlights.auto_clip_score` becomes a clip.

## Layouts and facecam

With the default `layout: auto`, every clip is checked for the streamer's webcam:

| What's in the clip | Layout used |
|---|---|
| Webcam overlay on gameplay | **split**: webcam panel on top (zoomed on the face, border trimmed), gameplay below, white divider. The classic gaming Shorts/Reels format. |
| Full-screen camera (just chatting) | **crop**: vertical crop centred on the face |
| No face | **blur**: gameplay centred over a blurred copy of itself |

Framing: the webcam panel is sized to your face (face fills about 40% of the panel, `facecam.face_height_share`) and follows you smoothly through the clip when you lean in or sit back (`facecam.track`, `track_deadzone`). The overlay's frame/border is trimmed off.

How detection works: frames are sampled across the clip, faces are detected (OpenCV YuNet, with Haar as a fallback), and the face that stays in one place is taken as the webcam (faces inside the game move or vanish). The webcam box is then found as the rectangle around that face whose border stays fixed while the gameplay changes. The result is cached per clip in `work/<id>/clips/<clip>/facecam.json`.

Check detection on any video file:

```powershell
.venv\Scripts\python -m clipper facecam path\to\video.mp4
```

It prints the box and saves `<video>_facecam.png` with the webcam outlined. If detection gets your setup wrong, it also prints the box as fractions: put those in `config.yaml → render.facecam` and set `mode: manual`.

Tuning in `config.yaml → render.facecam`: `position` (top/bottom), `height_share` (panel size), `zoom` (tighter on the face), `game_zoom`, `divider`. Force a layout with `render <id> --layout split|blur|crop`.

## Music and voices

Voices come first: the stream audio is levelled so quiet voices (friends on Discord, teammates) are lifted to the streamer's level, then normalised. Music sits about 15 dB under the voices and drops further whenever anyone talks, using the transcript plus a voice-energy detector.

Music is matched to the clip's mood: `funny` (fails, banter), `hype` (wins, clutches), `chill`, `tense` (only real suspense), or `none` for talk-heavy clips. If no track of that mood is available, the clip gets no music rather than a random one. Free Kevin MacLeod tracks (CC BY 4.0) are downloaded into `music/` on first use; add your own in `music/<mood>/`. **Each clip's `.txt` holds the credit line, which the licence requires in the post description.**

## Output

`output/<id>/NN_title.mp4` (1080x1920, H.264 via NVENC with x264 fallback) plus `NN_title.txt` with the title, source timestamps, reason, and music credit.

## Limits

- Live streams: run on the VOD after the stream ends.
- Subscriber-only VODs: set `download.cookies_from_browser: chrome` in `config.yaml`.
- Chat replay sites change often. If the chat download fails, the tool continues with audio signals only.
