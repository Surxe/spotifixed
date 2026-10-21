<p align="center">
  <img src="docs/media/overlay-demo.gif" alt="A lyric line drawn over a fullscreen game, with the word being sung underlined" width="720">
</p>

# <img src="spotifixed/server/static/icon.svg" alt="" width="40" align="top"> spotifixed

**Lyrics on top of anything.** spotifixed plays your Spotify playlists and draws the line
being sung right over your game, video or whatever else is fullscreen. It stays out of the
way, costs next to nothing to run, and looks the way you set it.

## A light overlay

The overlay is one line of text on the screen's top layer, above fullscreen windows. It
never takes focus, clicks pass straight through it, and it only exists while there is a
line to show:

- **Gone when it is not needed.** Paused, between lines, or on a track without synced
  lyrics, the overlay is removed from the screen, not just made transparent.
- **Off means off.** Switch it off on the page and the overlay program closes, using no
  memory or processor time until it is switched back on.
- **Nothing heavy underneath.** The player and the overlay need nothing beyond Python and
  your desktop's own GTK; there is no Electron app and no browser engine. Music plays from files on your own disk, so nothing streams,
  and the page only redraws while something on it is moving.
- **No AI at runtime.** Lyrics come from synced lyric files, timed to the exact audio
  that was downloaded.

## Make it yours

<p align="center">
  <img src="docs/media/overlay-settings.png" alt="The page with its Settings panel open: playback, tracks, page and overlay settings" width="720">
</p>

Open **Settings** on the page and move a control: the overlay changes as you do, and the
change is saved. **Show a sample line** puts a line on screen so you can arrange it with
nothing playing.

- **Where:** any edge, corner or the center of the screen, with margins, on the monitor
  you choose.
- **How it looks:** font, size, text colour and an outline colour that keeps it readable
  over any background.
- **What it shows:** the current line alone, or the next line too, dimmed.
- **When:** how early each line appears, and how quickly it fades in and out.
- **Underline word:** underlines the word being sung, as in the clip above.

Every setting can be put back one at a time, or all at once with **Reset to configured
values**. The same panel keeps the page itself light: turn off **Show lyrics** or **Show
time and position** and the page stops redrawing them.

## And a player to go with it

A small remote-control page in your browser runs the music: pick a playlist, click a
track, and the lyrics follow along. It remembers the song and position you left off at,
can shuffle a playlist and keep that order, fade the music in when you resume, play the
same sound in both ears, and leave out tracks without lyrics or songs you turn off. Media
keys work from inside a fullscreen game, without the page open; Ctrl+Volume Up and Down
turn spotifixed alone up or down, and Meta+Next skips a song and turns it off (these keys
can be changed).

## Get started

spotifixed runs on Linux. The overlay needs a Wayland desktop that supports the
layer-shell protocol, such as KDE Plasma or Sway (not GNOME).

1. **Install** a few system packages and the Python environment.
2. **Sync** a playlist once: its audio, lyrics and cover art are saved to disk.
3. **Start** it: `.venv/bin/python -m spotifixed start`, or put that behind a desktop
   launcher.

[Setting up spotifixed](docs/setup.md) walks through all three, step by step.

## Documentation

| Guide | Covers |
|-------|--------|
| [Setting up](docs/setup.md) | Installing, syncing a playlist, word timing, where files live, and every setting |
| [Using](docs/usage.md) | The page and its settings, keyboard and media keys, and the overlay in detail |
| [Developing](docs/development.md) | Tests, lint and the generated docs |

## What it is and is not

spotifixed is a personal-use tool. It downloads a playlist's audio, lyrics and
cover art into a local cache for the one person who runs it, plays them back locally
through mpv, and shows the current lyric line in an overlay. Nothing is re-encoded,
re-uploaded, shared or redistributed; the daemon listens on loopback only, and no
endpoint sends or downloads a file.

This repository ships none of that content. `cache/` and `state/` are gitignored, and
the tests use synthetic data only, so the code here holds no audio, lyrics, track lists
or cover art. The code is under the MIT License (see `LICENSE`); the audio, lyrics and
cover art `sync` can fetch are third-party works that stay under their owners' rights
and the terms of the services they come from.

`sync` reads a playlist's track list from a paid Apify Actor, its lyrics from LRCLIB,
and its audio through yt-dlp. Use spotifixed only for content you are allowed to
keep for personal playback, and follow the terms of the services you use it with.
