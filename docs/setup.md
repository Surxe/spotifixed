# Setting up spotifixed

Everything needed to install spotifixed, fill its cache from a Spotify playlist and
configure it. For what it does once it is running, see [Using spotifixed](usage.md); the
front page is the [README](../README.md).

## How it fits together

| Command | What it does | Needs |
|---------|--------------|-------|
| `sync` | Fetches track list, audio, lyrics and cover art into the cache | network, yt-dlp, ffmpeg |
| `align` | Optional: times each word of the synced lyrics against the audio, and hears clean edits | PyTorch, Demucs and Whisper (`requirements-align.txt`), ffmpeg; a GPU, in practice |
| `status` | Shows what is cached for a playlist | |
| `pin` | Chooses the audio for a track by hand | |
| `serve` | The daemon: owns mpv, publishes playback state, serves the remote-control page | mpv |
| `ctl` | Sends play, pause, next and so on to the daemon, for a keyboard shortcut | a running `serve` |
| `overlay` | Draws the current lyric line above other windows; hidden while paused | a running `serve`, GTK 4 layer-shell |
| `shortcuts` | Holds the global volume shortcuts and sends each press to the daemon | a running `serve`, python3-gi, a desktop with the global shortcuts portal |
| `start` | The whole application at once: `serve`, `overlay` and `shortcuts` beside it, and the page opened in the browser | what those need |

`sync` is the only command that touches the network. `serve`, the page and the overlay
work from the cache: the page, the overlay and the shortcuts are plain clients of the
daemon's HTTP API on 127.0.0.1, and none of them reads the cache or talks to mpv.

## Installing

Programs (Debian and Ubuntu names; other distributions have the same packages):

```bash
sudo apt-get install python3-venv ffmpeg mpv                       # required
sudo apt-get install mpv-mpris                                     # optional: media keys
sudo apt-get install libgtk4-layer-shell0 gir1.2-gtk4layershell-1.0 python3-gi  # optional: overlay
sudo apt-get install python3-gi                                    # optional: volume shortcuts
```

Then, in a clone of this repo:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
scripts/prereq-check.sh
```

`--system-site-packages` lets the virtualenv see the system's GTK bindings, which pip
cannot install; leave it off if you do not want the overlay. yt-dlp is installed in the
virtualenv, not from a distribution package, because it has to be updated whenever
YouTube changes. Having `deno` or `node` on the PATH makes its extraction more reliable.

Run commands as `.venv/bin/python -m spotifixed <command>` from the repo. Only
`sync` needs the virtualenv at all: `serve`, `ctl` and `overlay` use nothing but the
standard library (and, for the overlay, the system GTK bindings), so they also run with
the system `python3`.

## The one secret

Fetching a playlist's track list is a paid Apify Actor run of about $0.01 and needs an
Apify API token in the `APIFY_TOKEN` environment variable (or in a gitignored `.env`).
See `secrets.env.example` for the two ways to supply it. The token is read only by
`sync`, and only when a playlist's track list is not cached yet.

The request is sent once and never retried automatically. The response is kept verbatim
under `cache/playlists/<id>/`, and every later `sync` works from that copy with no token
and no request; it is fetched again only with `sync --refresh-tracklist`. An exit status
of 2 means the source returned fewer tracks than the playlist holds.

## Syncing a playlist

```bash
.venv/bin/python -m spotifixed sync <playlist-url>     # everything, resumable
.venv/bin/python -m spotifixed sync <id> --limit 10    # the first ten tracks
.venv/bin/python -m spotifixed sync <id> --only lyrics # one stage: tracklist, audio, lyrics or cover
.venv/bin/python -m spotifixed sync <id> --dry-run     # what would be fetched
.venv/bin/python -m spotifixed status <id>             # per-track table and totals
```

Per track, in this order: audio (yt-dlp), then lyrics (LRCLIB), then cover art. The
order matters: the lyrics record is chosen by the measured length of the audio that was
actually downloaded, which is what keeps the overlay in time.

`sync` can be interrupted at any point and run again; what is on disk is what is done.
What a source does not have is remembered in `cache/misses.json` and not asked for
again for `MISS_RETRY_DAYS` (or until `--retry-misses`). A stage that fails outright
(network trouble, yt-dlp broken by a YouTube change) is not remembered and is simply
retried by the next `sync`. When downloads start failing, update yt-dlp first:

```bash
.venv/bin/pip install -U yt-dlp
```

The audio search wants the album recording. A candidate labelled as another version is
held below the line for downloading by itself: live, remix, cover, karaoke, instrumental,
TV mix, backing track and the like in its title, a karaoke or instrumentals channel, or a
credit such as "originally performed by" or "made famous by" (a name after such a credit
does not count as the artist's). A label the Spotify title carries itself is not held
against it.

Karaoke uploads are not always labelled, so `align` (see Word timing) also measures how
much of each track's audio has a voice in it. When a track with synced lyrics turns out
to have next to no singing, `align` says so, and the next `sync` replaces the audio with
the best candidate not yet tried; what was rejected is listed in the track's
`rejected_audio.json`. After three such versions, or when no other version is left, the
audio it has is kept. A pinned track's audio is never replaced this way.

Clean edits are replaced the same way. A clean edit is usually labelled as nothing at
all, often on the artist's own channel, so `align` listens for it (see Word timing), and
the next `sync` turns down audio in which too few of the lyrics' strong words were heard
(`reason` "clean edit" in `rejected_audio.json`). For such a track it also searches for
the song with "explicit" added. For a track Spotify marks explicit, a candidate titled
"clean", "edited", "censored" or "radio edit" counts as another version from the first
search on, and one titled "explicit", "dirty" or "uncensored" gains a little. Audio heard
saying no fewer of the words than a version already turned down is kept: in such a song
the words are hard to hear (few of them, or a voice the recognizer misses), not cut.
`SPOTIFIXED_AUDIO_PREFER_EXPLICIT=false` keeps clean edits.

A track whose audio search finds nothing trustworthy is flagged `review` in `status`,
with its best candidates listed in `cache/misses.json`. Choose one by hand, then sync
that track again:

```bash
.venv/bin/python -m spotifixed pin <track-id> <url>    # or: pin <track-id> clear
.venv/bin/python -m spotifixed sync <id> --track <track-id>
```

Audio is downloaded for personal, local playback and never redistributed; the boundary
the tool draws is set out under
[What it is and is not](../README.md#what-it-is-and-is-not), and the terms of the
services it fetches from are yours to follow.

## Word timing

Optional, and only of use with the **Underline word** setting. Lyric files time lines,
not words, so by default the underlined word is an estimate from syllable counts. `align`
replaces the estimate with what is heard in the audio:

```bash
.venv/bin/pip install -r requirements-align.txt --extra-index-url https://download.pytorch.org/whl/cu128
.venv/bin/python -m spotifixed align                 # every cached playlist, resumable
.venv/bin/python -m spotifixed align <id> --limit 10 # the first ten tracks of one
.venv/bin/python -m spotifixed align --track <id>    # one track; --force to do it again
```

For each track with synced lyrics and audio it separates the voice from the music
(Demucs), has a speech model say which letters it hears in every 20 ms of that voice (MMS
forced alignment), and places each line's words inside a window around that line's own
timestamp. The result is `words.json` beside the track's lyrics; the daemon sends it with
the lyrics, and the overlay and the page use it. Nothing else needs these packages, and a
track that has not been aligned simply keeps the estimate.

It works line by line. A line the model heard too faintly to trust (a processed or
buried voice, usually) is left out and keeps the estimate, so one song can have both;
`align` prints how many lines it placed. Word times are in the audio's own time, so the
track's offset does not move them, though a large change of offset moves where lines are
looked for and the track is aligned again on the next run. New audio for a track (a pin)
discards its word times. About five seconds a track on a recent GPU.

It also records the share of the track in which a voice is heard (`voice_share` in
`words.json`). Under 0.15 the audio is taken to have no singing, a karaoke backing or an
instrumental under the song's name, and `align` prints "no singing heard"; see Syncing a
playlist for what `sync` does about it. Songs with vocals measure about 0.4 and up.

For lyrics with at least three strong words ("fuck", "shit", "bitch" and the like, also
as "f**k"), it has Whisper write down what the separated voice says and records how many
of them it heard (`strong_words` in `words.json`). A clean edit mutes, reverses or
replaces them, so it is heard saying almost none; the explicit recording, most of them.
Under 0.55 of them the audio is taken to be a clean edit, and `align` prints "sounds like
a clean edit"; see Syncing a playlist for what `sync` does about it. Measured over a
500-track playlist: clean edits come out under 0.2, explicit recordings at 0.6 and over.
This adds about ten seconds to such a track, and Whisper downloads about 1.6 GB on first
use. `SPOTIFIXED_AUDIO_PREFER_EXPLICIT=false` leaves it out.

## Where things are kept

| Path | Holds |
|------|-------|
| `cache/` | Everything fetched: track lists, audio, lyrics, covers. Safe to delete; `sync` rebuilds it. |
| `state/` | What you changed by hand: lyric offsets, audio pins, and overlay settings set from the page (with whether the overlay is switched on). Not rebuilt. |
| `state/session.json` | Each shuffled playlist's order, and what the daemon resumes: the playlist, song, position and volume, with the settings made on the page (fade in, mono, repeat, which tracks are included). Rewritten as playback changes; deleting it just starts fresh. |

Both are gitignored and sit in the repo by default; `SPOTIFIXED_CACHE_DIR` and
`SPOTIFIXED_STATE_DIR` move them. Files get ordinary permissions, whatever your
umask allows.

This repo ships no installer and does nothing about user accounts. To run the daemon
and overlay from somewhere other than a working clone (as a different account than the
one that syncs, say), copy the `spotifixed/` directory there, run it with the
system `python3 -m spotifixed`, and point the two directory settings at the shared
cache. Sharing a cache between accounts is ordinary Unix: a group-owned, setgid
directory and a umask of 002 for whoever writes to it.

## Configuration

Every setting can be given as a CLI flag, an environment variable, or a line in a `.env`
file in the working directory, in that order of precedence. `APIFY_TOKEN` is the
exception: supply it through the environment or `.env` only. The generated list below
shows a flag for it because the generator documents one for every option; that flag is
deliberately not registered, so a token cannot end up on a command line.

<!-- BEGIN_GENERATED_OPTIONS -->
#### Storage

* **SPOTIFIXED_CACHE_DIR** - Directory for everything fetched: tracklists, audio, lyrics, cover art. A relative path resolves against the repo root, not the working directory.
  - Default: `"cache"`
  - Command line: `--cache-dir`

* **SPOTIFIXED_STATE_DIR** - Directory for hand-authored per-track values (timing offsets, audio pins). Kept apart from the cache so wiping the cache never loses a correction.
  - Default: `"state"`
  - Command line: `--state-dir`


#### Tracklist (Apify)

* **APIFY_TOKEN** - Apify API token. Environment only, never a CLI argument and never a committed file. Needed solely to fetch a playlist's track list that is not cached yet; see secrets.env.example for where the real value lives.
  - Default: None
  - Command line: `--apify-token`

* **SPOTIFIXED_TRACKLIST_ACTOR** - Apify Actor that returns a playlist's tracks, as user~name. The response shape is Actor-specific, so a new Actor also needs a parser.
  - Default: `"s-r~free-spotify-playlist-intelligence"`
  - Command line: `--tracklist-actor`

* **SPOTIFIXED_APIFY_MAX_CHARGE_USD** - Charge cap sent with each Actor run, in US dollars.
  - Default: `"0.05"`
  - Command line: `--apify-max-charge-usd`

* **SPOTIFIXED_APIFY_BASE_URL** - Apify API origin. Tests point this at a local stub.
  - Default: `"https://api.apify.com"`
  - Command line: `--apify-base-url`


#### Sync

* **SPOTIFIXED_LRCLIB_BASE_URL** - LRCLIB origin, the lyrics source.
  - Default: `"https://lrclib.net"`
  - Command line: `--lrclib-base-url`

* **SPOTIFIXED_AUDIO_DURATION_TOLERANCE_S** - An audio candidate within this many seconds of Spotify's duration gets full marks for duration.
  - Default: `"3.0"`
  - Command line: `--audio-duration-tolerance-s`

* **SPOTIFIXED_AUDIO_DURATION_REJECT_S** - An audio candidate further than this from Spotify's duration is rejected.
  - Default: `"7.0"`
  - Command line: `--audio-duration-reject-s`

* **SPOTIFIXED_AUDIO_PREFER_EXPLICIT** - Want the explicit recording of a song, not its clean edit. align listens for the lyrics' strong words (with Whisper, from requirements-align.txt), and sync replaces audio in which they were not heard. Off: clean edits are kept, and align does not load Whisper.
  - Default: `"true"`
  - Command line: `--audio-prefer-explicit`

* **SPOTIFIXED_LYRICS_DURATION_TOLERANCE_S** - A lyrics record further than this from the audio's real duration is still kept, but flagged as low timing confidence.
  - Default: `"2.0"`
  - Command line: `--lyrics-duration-tolerance-s`

* **SPOTIFIXED_SYNC_WORKERS** - Tracks processed in parallel. Keep low to stay polite to the sources.
  - Default: `"2"`
  - Command line: `--sync-workers`

* **SPOTIFIXED_MISS_RETRY_DAYS** - A recorded miss (no lyrics, no audio match) is retried after this many days.
  - Default: `"14"`
  - Command line: `--miss-retry-days`


#### Daemon

* **SPOTIFIXED_HOST** - Address the daemon binds. Anything other than loopback is refused.
  - Default: `"127.0.0.1"`
  - Command line: `--host`

* **SPOTIFIXED_PORT** - Port for the daemon's HTTP API and remote-control page.
  - Default: `"8765"`
  - Command line: `--port`

* **SPOTIFIXED_MPV_BIN** - mpv executable.
  - Default: `"mpv"`
  - Command line: `--mpv-bin`

* **SPOTIFIXED_MPV_AUDIO_OUTPUT** - mpv --ao value. Empty lets mpv choose; tests use `null` for silence.
  - Default: `""` (empty)
  - Command line: `--mpv-audio-output`

* **SPOTIFIXED_MPV_MPRIS_PLUGIN** - Path to mpv's MPRIS plugin (Debian: /usr/lib/mpv-mpris/mpris.so) so desktop media keys control playback. Empty disables it.
  - Default: `""` (empty)
  - Command line: `--mpv-mpris-plugin`


#### Overlay

* **SPOTIFIXED_OVERLAY_MONITOR** - Output connector to show the overlay on, e.g. DP-2. Empty: compositor's choice.
  - Default: `""` (empty)
  - Command line: `--overlay-monitor`

* **SPOTIFIXED_OVERLAY_ANCHOR** - Screen edge or corner the overlay is attached to.
  - Default: `"bottom"`
  - Command line: `--overlay-anchor`

* **SPOTIFIXED_OVERLAY_MARGIN_X** - Horizontal distance in pixels from the anchored edge.
  - Default: `"0"`
  - Command line: `--overlay-margin-x`

* **SPOTIFIXED_OVERLAY_MARGIN_Y** - Vertical distance in pixels from the anchored edge.
  - Default: `"120"`
  - Command line: `--overlay-margin-y`

* **SPOTIFIXED_OVERLAY_MAX_WIDTH** - Maximum line width in pixels; longer lines wrap.
  - Default: `"1200"`
  - Command line: `--overlay-max-width`

* **SPOTIFIXED_OVERLAY_FONT** - Font family and style.
  - Default: `"Sans Bold"`
  - Command line: `--overlay-font`

* **SPOTIFIXED_OVERLAY_FONT_SIZE** - Font size in points.
  - Default: `"32"`
  - Command line: `--overlay-font-size`

* **SPOTIFIXED_OVERLAY_COLOR** - Text colour.
  - Default: `"#FFFFFF"`
  - Command line: `--overlay-color`

* **SPOTIFIXED_OVERLAY_OUTLINE_COLOR** - Outline colour, for contrast against any background.
  - Default: `"#000000"`
  - Command line: `--overlay-outline-color`

* **SPOTIFIXED_OVERLAY_LINES** - 1 shows the current line; 2 adds the next line, dimmed.
  - Default: `"1"`
  - Command line: `--overlay-lines`

* **SPOTIFIXED_OVERLAY_LEAD_MS** - Show each line this many milliseconds before its timestamp.
  - Default: `"150"`
  - Command line: `--overlay-lead-ms`

* **SPOTIFIXED_OVERLAY_FADE_MS** - Fade duration when a line appears or disappears.
  - Default: `"120"`
  - Command line: `--overlay-fade-ms`

* **SPOTIFIXED_OVERLAY_UNDERLINE_WORD** - Underline the word being sung, in the overlay and on the page. The word is estimated from the line timing, so it needs the track's offset to be right.
  - Default: `"false"`
  - Command line: `--overlay-underline-word`

* **SPOTIFIXED_OVERLAY_VOLUME_INDICATOR** - Show spotifixed's volume in the overlay for a moment whenever it changes, above the lyric line, even while paused, and the level rising during the fade-in on resume.
  - Default: `"true"`
  - Command line: `--overlay-volume-indicator`


#### Shortcuts

* **SPOTIFIXED_SHORTCUT_VOLUME_UP** - Global shortcut that raises spotifixed's own volume, in the XDG shortcuts format (modifiers CTRL, ALT, SHIFT, NUM, LOGO joined by + to an xkb key name, e.g. CTRL+XF86AudioRaiseVolume or LOGO+Up). It is the trigger suggested to the desktop's global shortcuts portal, which may let the user change it. Empty disables it. Play, pause, next and previous are the keyboard's media keys (SPOTIFIXED_MPV_MPRIS_PLUGIN), not settings here.
  - Default: `"CTRL+XF86AudioRaiseVolume"`
  - Command line: `--shortcut-volume-up`

* **SPOTIFIXED_SHORTCUT_VOLUME_DOWN** - Global shortcut that lowers spotifixed's own volume, in the same format as SPOTIFIXED_SHORTCUT_VOLUME_UP. Empty disables it.
  - Default: `"CTRL+XF86AudioLowerVolume"`
  - Command line: `--shortcut-volume-down`

* **SPOTIFIXED_SHORTCUT_SKIP_DISABLE** - Global shortcut that skips the song that is on and turns it off, so it is not played again until it is turned back on in the page's track list. In the same format as SPOTIFIXED_SHORTCUT_VOLUME_UP. Empty disables it.
  - Default: `"LOGO+XF86AudioNext"`
  - Command line: `--shortcut-skip-disable`

* **SPOTIFIXED_SHORTCUT_VOLUME_STEP** - How much one press of a volume shortcut changes the volume, out of 100.
  - Default: `"5"`
  - Command line: `--shortcut-volume-step`


<!-- END_GENERATED_OPTIONS -->
