// The remote-control page. A pure client of the daemon's API: it never plays audio and
// never reads files. Playback state arrives on one event stream; between snapshots the
// position is extrapolated locally (see clock.js), so nothing here polls. What moves on
// its own while a song plays (the time, the slider, the lyric bar) is drawn by a loop that
// runs only while something it draws is showing and moving; see schedule().

import {
  alignedWord, currentIndex, currentWord, formatTime, lineProgress, positionAt, wordEnds, wordRanges,
} from "./clock.js";
import { initOverlayPanel } from "./overlay.js";
import { initKeyboardList, initPlaybackSettings, initSettingsPane, initSwitches } from "./settings.js";

const API = "/api/v1";
// Show a line slightly before its timestamp; it reads better than exactly on it.
const LEAD_MS = 150;
const SEEK_STEP_S = 5;

const $ = (id) => document.getElementById(id);
const els = {
  playlists: $("playlists"), shuffle: $("shuffle"), unshuffle: $("unshuffle"),
  coverage: $("coverage"),
  connection: $("connection"), tracks: $("tracks"), lyrics: $("lyrics"),
  lyricsNote: $("lyrics-note"), offset: $("offset"), offsetReset: $("offset-reset"),
  cover: $("cover"), title: $("title"), artists: $("artists"), toggle: $("toggle"),
  prev: $("prev"), next: $("next"), stop: $("stop"), seek: $("seek"),
  elapsed: $("elapsed"), duration: $("duration"), volume: $("volume"),
};

let state = null; // the latest PlaybackState snapshot
let playlist = null; // the playlist shown in the track list
let tracksById = new Map();
let lyrics = { trackId: null, kind: "missing", times: [], ends: [], words: [], rows: [] };
let shownLine = -2;
let shownWord = null; // the underlined word's element, when there is one
let underlineWord = false; // the overlay's "underline word" setting, which the page follows
let seeking = false;
let settingVolume = false;
// What the page settings let it draw; both on until the daemon says otherwise.
const view = { lyrics: true, position: true };
let loop = null; // the drawing loop while it runs: { kind: "frame" | "timer", id }
let shownElapsed = null; // what #elapsed and #seek were last set to, to skip repeats
let shownSeek = null;

async function getJson(path) {
  const response = await fetch(API + path);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function post(path, body = {}) {
  const response = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    console.warn(`${path}: ${detail.error ?? response.status}`);
  }
  return response.ok;
}

// Like post(), but hands back the answer: { ok, data } or { ok: false, error }.
async function postJson(path, body = {}) {
  try {
    const response = await fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (response.ok) return { ok: true, data };
    return { ok: false, error: data.error ?? `HTTP ${response.status}` };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

const overlayPanel = initOverlayPanel({ getJson, postJson });
initSettingsPane([
  initPlaybackSettings({ getJson, postJson }),
  initSwitches({
    getJson, postJson, noteId: "playback-note",
    ids: { mono: "mono", loop: "loop" },
    changed: () => {},
  }),
  initSwitches({
    getJson, postJson, noteId: "track-note",
    ids: { include_without_lyrics: "include-without-lyrics", include_without_audio: "include-without-audio" },
    changed: () => playlist && showPlaylist(playlist.id),
  }),
  initSwitches({
    getJson, postJson, noteId: "page-note",
    ids: { show_lyrics: "show-lyrics", show_position: "show-position" },
    changed: applyView,
  }),
  initKeyboardList({ getJson }),
  overlayPanel,
]);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ------------------------------- playlists ------------------------------- //

async function loadPlaylists() {
  // The session holds the page settings, so it is asked for first: lyrics that are
  // switched off are then never fetched.
  const saved = await getJson("/session").catch(() => null);
  if (saved) applyView(saved.settings);
  const { playlists } = await getJson("/playlists");
  els.playlists.replaceChildren(
    ...playlists.map((p) => {
      const option = element("option", "", `${p.name} (${p.coverage.audio}/${p.coverage.tracks})`);
      option.value = p.id;
      return option;
    }),
  );
  if (!playlists.length) {
    els.coverage.textContent = "No playlists are cached yet; run the sync command first.";
    return;
  }
  // The playlist the daemon last played (it resumes it on start), unless one is playing.
  const playing = state?.playlist_id ?? saved?.playlist_id;
  if (playing && playlists.some((p) => p.id === playing)) els.playlists.value = playing;
  await showPlaylist(els.playlists.value);
}

// Shuffle draws a new order for the playlist shown and the daemon keeps it: the list
// below is redrawn in that order, and the queue follows it at once if it is playing.
async function reorder(action) {
  if (!playlist) return;
  const result = await postJson(`/playlists/${playlist.id}/${action}`);
  if (result.ok) await showPlaylist(playlist.id);
  else els.coverage.textContent = result.error;
}
els.shuffle.addEventListener("click", () => reorder("shuffle"));
els.unshuffle.addEventListener("click", () => reorder("unshuffle"));

async function showPlaylist(id) {
  playlist = await getJson(`/playlists/${id}`);
  tracksById = new Map(playlist.tracks.map((t) => [t.id, t]));
  const c = playlist.coverage;
  els.coverage.textContent =
    `${c.tracks} tracks, ${c.audio} with audio, ${c.synced} with synced lyrics` +
    (playlist.hidden ? `, ${playlist.hidden} left out by settings` : "") +
    (playlist.disabled ? `, ${playlist.disabled} turned off` : "");
  els.tracks.replaceChildren(...playlist.tracks.map(trackRow));
  els.unshuffle.hidden = !playlist.shuffled;
  markCurrentTrack();
  renderNowPlaying();
}

// A track's lyric offset as the table shows it: signed milliseconds, marked when set.
function offsetLabel(ms) {
  return `${ms > 0 ? "+" : ""}${ms} ms`;
}

// Show a track's offset in its row, and keep the playlist's copy of it in step.
function setRowOffset(trackId, ms) {
  const track = tracksById.get(trackId);
  if (track) track.offset_ms = ms;
  const cell = els.tracks.querySelector(`li[data-id="${trackId}"] .offset`);
  if (!cell) return;
  cell.textContent = offsetLabel(ms);
  cell.classList.toggle("set", ms !== 0);
}

// A song that is turned off is skipped when the playlist plays, in every playlist it is
// in. It stays listed, dimmed, so that it can be turned back on; clicking it still plays it.
async function setDisabled(trackId, disabled) {
  const result = await postJson(`/tracks/${trackId}/disabled`, { disabled });
  // On success the list is redrawn by the event that follows; on failure the box goes back.
  if (result.ok) return;
  if (playlist) await showPlaylist(playlist.id);
  els.coverage.textContent = result.error;
}

function trackRow(track, index) {
  const row = element("li", track.has_audio ? "" : "unplayable");
  row.classList.toggle("off", track.disabled);
  row.dataset.id = track.id;
  const on = element("input", "on");
  on.type = "checkbox";
  on.checked = !track.disabled;
  on.disabled = !track.has_audio;
  on.title = track.disabled
    ? "Turned off: skipped when the playlist plays. Tick to hear it again."
    : "Untick to skip this song from now on, in every playlist.";
  on.setAttribute("aria-label", `Play ${track.title} with the playlist`);
  on.addEventListener("click", (event) => event.stopPropagation());
  on.addEventListener("change", () => setDisabled(track.id, !on.checked));
  const name = element("span", "name");
  name.append(track.title, element("span", "by", `  ${track.artists.join(", ")}`));
  const badges = element("span", "badges");
  if (!track.has_audio) badges.append(element("span", "badge bad", "no audio"));
  if (track.lyrics_kind !== "synced") {
    badges.append(element("span", "badge warn", "no synced lyrics"));
  } else if (track.timing_confidence === "low") {
    badges.append(element("span", "badge warn", "timing?"));
  }
  const offset = element("span", "offset", offsetLabel(track.offset_ms));
  offset.classList.toggle("set", track.offset_ms !== 0);
  offset.title = "Lyric offset for this track";
  row.append(
    on,
    element("span", "index", String(index + 1)),
    name,
    badges,
    offset,
    element("span", "length", formatTime(track.duration_ms / 1000)),
  );
  if (track.has_audio) {
    row.addEventListener("click", () =>
      post("/player/load", { playlist_id: playlist.id, start_track_id: track.id }),
    );
  }
  return row;
}

function markCurrentTrack() {
  for (const row of els.tracks.querySelectorAll("li.current")) row.classList.remove("current");
  if (!state?.track_id || state.playlist_id !== playlist?.id) return;
  const row = els.tracks.querySelector(`li[data-id="${state.track_id}"]`);
  if (!row) return;
  row.classList.add("current");
  row.scrollIntoView({ block: "nearest" });
}

// --------------------------------- lyrics -------------------------------- //

async function loadLyrics(trackId) {
  lyrics = { trackId, kind: "missing", times: [], ends: [], words: [], rows: [] };
  shownLine = -2;
  shownWord = null;
  els.lyrics.replaceChildren();
  els.lyrics.className = "";
  els.lyricsNote.textContent = "";
  if (!trackId || !view.lyrics) return;
  const data = await getJson(`/tracks/${trackId}/lyrics`).catch(() => null);
  // The track changed, or lyrics were switched off, while fetching.
  if (!data || state?.track_id !== trackId || !view.lyrics) return;
  lyrics.kind = data.kind;
  if (data.kind === "synced") {
    lyrics.times = data.lines.map((line) => line.t_ms);
    lyrics.ends = wordEnds(data.lines);
    lyrics.words = data.lines.map((line) => line.words ?? null);
    // A line with no words marks a pause in the singing; it gets no progress bar.
    lyrics.rows = data.lines.map((line) => lyricRow(line.text));
    els.lyrics.replaceChildren(...lyrics.rows);
    els.lyricsNote.textContent =
      data.timing_confidence === "low" ? "Timing may be off; nudge the offset." : "";
  } else if (data.kind === "plain") {
    els.lyrics.className = "plain";
    els.lyrics.replaceChildren(...data.plain.split("\n").map((text) => element("li", "", text)));
    els.lyricsNote.textContent = "Lyrics are not time-synced for this track.";
  } else {
    els.lyricsNote.textContent =
      data.kind === "instrumental" ? "Instrumental." : "No lyrics for this track.";
  }
  schedule(); // synced lines want drawing every frame; anything else does not
}

// One synced line, each word in an element of its own so that one can be underlined.
function lyricRow(text) {
  const row = element("li", text.trim() ? "" : "pause");
  let at = 0;
  for (const [start, end] of wordRanges(text)) {
    row.append(text.slice(at, start), element("span", "", text.slice(start, end)));
    at = end;
  }
  row.append(text.slice(at));
  return row;
}

function showLine(index) {
  if (index === shownLine) return;
  shownLine = index;
  lyrics.rows.forEach((row, i) => {
    row.classList.toggle("current", i === index);
    row.classList.toggle("past", i < index);
  });
  lyrics.rows[Math.max(index, 0)]?.scrollIntoView({ block: "center", behavior: "smooth" });
}

// The bar under the current line: how far playback is from that line's start to the
// next line's. It is what makes an offset readable: a bar that is already part-filled
// when the words begin means the lyrics are running ahead of the song.
function showProgress(index, positionS) {
  const row = lyrics.rows[index];
  if (!row || row.classList.contains("pause")) return;
  const durationMs = state.duration_s ? state.duration_s * 1000 : null;
  const progress = lineProgress(
    lyrics.times, index, positionS * 1000, LEAD_MS, state.offset_ms, durationMs,
  );
  const value = progress === null ? "" : progress.toFixed(4);
  if (row.dataset.progress === value) return; // paused, or between frames: nothing to redraw
  row.dataset.progress = value;
  row.classList.toggle("timed", progress !== null);
  row.style.setProperty("--progress", value || "0");
}

// The underline under the word being sung. A line that was aligned to the audio carries
// the time of each word, in the audio's own time. Any other line gets an estimate from
// the line's timing (see clock.js), on the same shifted clock as the line and its bar.
// Off unless the overlay setting turns it on; no word is underlined while a line trails
// off.
function showWord(index, positionS) {
  let word = -1;
  if (underlineWord && index >= 0 && lyrics.words[index]) {
    word = alignedWord(lyrics.words[index], positionS * 1000);
  } else if (underlineWord && index >= 0) {
    const elapsed = positionS * 1000 + LEAD_MS + state.offset_ms - lyrics.times[index];
    word = currentWord(lyrics.ends[index], elapsed);
  }
  const span = word >= 0 ? lyrics.rows[index].children[word] : null;
  if (span === shownWord) return;
  shownWord?.classList.remove("sung");
  span?.classList.add("sung");
  shownWord = span ?? null;
}

// ---------------------------------- state --------------------------------- //

// Title, artists and cover for the current track. Called when the track changes and
// again when a playlist finishes loading, since the first state event usually arrives
// before the track list that holds the titles.
function renderNowPlaying() {
  const track = tracksById.get(state?.track_id);
  els.title.textContent = track ? track.title : state?.track_id ?? "Nothing playing";
  els.artists.textContent = track ? track.artists.join(", ") : state?.error ?? "";
  const showCover = Boolean(track?.has_cover);
  els.cover.hidden = !showCover;
  const source = showCover ? `${API}/tracks/${track.id}/cover` : "";
  if (els.cover.getAttribute("src") !== source) els.cover.setAttribute("src", source);
}

function applyState(next) {
  if (state && next.version <= state.version) return; // stale or repeated event
  const trackChanged = next.track_id !== state?.track_id;
  state = next;

  const active = state.status === "playing" || state.status === "loading";
  els.toggle.textContent = active ? "Pause" : "Play";
  els.offset.textContent = offsetLabel(state.offset_ms);
  if (state.track_id && tracksById.get(state.track_id)?.offset_ms !== state.offset_ms) {
    setRowOffset(state.track_id, state.offset_ms);
  }
  els.duration.textContent = formatTime(state.duration_s ?? 0);
  els.seek.max = String(state.duration_s ?? 1);
  if (!settingVolume) els.volume.value = String(state.volume);
  renderNowPlaying();

  if (trackChanged) {
    markCurrentTrack();
    loadLyrics(state.track_id);
  }
  if (state.playlist_id && playlist && state.playlist_id !== playlist.id) {
    els.playlists.value = state.playlist_id;
    showPlaylist(state.playlist_id);
  }
  schedule();
}

// The page settings: what is switched off is hidden, and not drawn or fetched either.
function applyView(settings) {
  const hadLyrics = view.lyrics;
  view.lyrics = settings.show_lyrics !== false;
  view.position = settings.show_position !== false;
  document.body.classList.toggle("no-lyrics", !view.lyrics);
  document.body.classList.toggle("no-position", !view.position);
  shownElapsed = shownSeek = null; // draw them afresh if they are showing again
  // Lyrics switched off are dropped; switched back on, the current track's are fetched.
  if (view.lyrics !== hadLyrics) loadLyrics(view.lyrics ? state?.track_id ?? null : null);
  schedule();
}

// Everything that follows the position, drawn once for now. Nothing is written that
// would not change what is on screen.
function draw() {
  if (!state) return;
  const position = positionAt(state, Date.now());
  if (view.position) {
    const seek = Math.round(position * 10) / 10; // the slider's own step
    if (!seeking && seek !== shownSeek) {
      els.seek.value = String(seek);
      shownSeek = seek;
    }
    const elapsed = formatTime(position);
    if (elapsed !== shownElapsed) {
      els.elapsed.textContent = elapsed;
      shownElapsed = elapsed;
    }
  }
  if (view.lyrics && lyrics.kind === "synced" && lyrics.trackId === state.track_id) {
    const index = currentIndex(lyrics.times, position * 1000, LEAD_MS, state.offset_ms);
    showLine(index);
    showProgress(index, position);
    showWord(index, position);
  }
}

// How often the page has to draw: every frame while synced lyrics are showing (the bar
// and the underline move smoothly), once a second for the time alone, and not at all
// while nothing is playing or nothing that moves is showing.
function loopKind() {
  if (!state || state.paused || state.status !== "playing") return null;
  if (view.lyrics && lyrics.kind === "synced" && lyrics.trackId === state.track_id) return "frame";
  return view.position ? "timer" : null;
}

// Milliseconds until the time shown next changes: the next whole second of the song.
function untilNextSecond() {
  const position = positionAt(state, Date.now());
  const left = (Math.floor(position) + 1 - position) / (state.speed || 1);
  return Math.max(20, Math.ceil(left * 1000) + 5);
}

// Draw now, then start, keep or stop the loop to suit. Called whenever what it depends
// on changes: a state event, a page setting, lyrics loaded.
function schedule() {
  draw();
  const kind = loopKind();
  if ((loop?.kind ?? null) === kind) return;
  if (loop?.kind === "frame") cancelAnimationFrame(loop.id);
  else if (loop) clearTimeout(loop.id);
  loop = null;
  if (!kind) return;
  const next = (step) =>
    kind === "frame" ? requestAnimationFrame(step) : setTimeout(step, untilNextSecond());
  const step = () => {
    draw();
    loop = { kind, id: next(step) };
  };
  loop = { kind, id: next(step) };
}

function connect() {
  const events = new EventSource(`${API}/events`);
  events.addEventListener("state", (event) => applyState(JSON.parse(event.data)));
  // A song was turned off or back on, here, in another tab or by the keyboard shortcut.
  events.addEventListener("tracks", () => playlist && showPlaylist(playlist.id));
  events.addEventListener("overlay", (event) => {
    const overlay = JSON.parse(event.data);
    underlineWord = Boolean(overlay.settings?.underline_word);
    draw(); // the underline follows at once, even while paused
    overlayPanel.apply(overlay);
  });
  events.onopen = () => {
    els.connection.textContent = "connected";
    els.connection.className = "pill on";
  };
  events.onerror = () => {
    // EventSource reconnects by itself; forget the old version so a restarted daemon,
    // whose versions start again from 1, is not mistaken for stale events.
    els.connection.textContent = "offline";
    els.connection.className = "pill off";
    if (state) state = { ...state, version: 0 };
    overlayPanel.disconnected();
  };
}

// -------------------------------- controls -------------------------------- //

els.playlists.addEventListener("change", () => showPlaylist(els.playlists.value));
els.toggle.addEventListener("click", () => {
  if (!state?.queue_length && playlist) {
    post("/player/load", { playlist_id: playlist.id });
  } else {
    post("/player/toggle");
  }
});
els.prev.addEventListener("click", () => post("/player/prev"));
els.next.addEventListener("click", () => post("/player/next"));
els.stop.addEventListener("click", () => post("/player/stop"));

els.seek.addEventListener("input", () => {
  seeking = true;
  shownSeek = null; // whatever the daemon says next is drawn, even if it was shown before
});
els.seek.addEventListener("change", async () => {
  await post("/player/seek", { position_s: Number(els.seek.value) });
  seeking = false;
});
els.volume.addEventListener("input", () => { settingVolume = true; });
els.volume.addEventListener("change", async () => {
  await post("/player/volume", { volume: Number(els.volume.value) });
  settingVolume = false;
});

async function setOffset(value) {
  if (!state?.track_id) return;
  const trackId = state.track_id;
  const result = await postJson(`/tracks/${trackId}/offset`, { offset_ms: value });
  // The daemon clamps the value; show what it kept, even if the track is not playing.
  if (result.ok) setRowOffset(trackId, result.data.offset_ms);
  else els.lyricsNote.textContent = result.error;
}
for (const button of document.querySelectorAll("[data-nudge]")) {
  button.addEventListener("click", () =>
    setOffset((state?.offset_ms ?? 0) + Number(button.dataset.nudge)),
  );
}
els.offsetReset.addEventListener("click", () => setOffset(0));

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  // Keys typed into a control belong to that control, not to the transport.
  const target = event.target;
  if (target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement) return;
  if (target instanceof HTMLInputElement && target.type !== "range") return;
  if (target instanceof Element && target.closest("#settings-pane")) return;
  const actions = {
    " ": () => els.toggle.click(),
    ArrowLeft: () => post("/player/seek", { delta_s: -SEEK_STEP_S }),
    ArrowRight: () => post("/player/seek", { delta_s: SEEK_STEP_S }),
    ArrowUp: () => post("/player/prev"),
    ArrowDown: () => post("/player/next"),
  };
  const action = actions[event.key];
  if (!action) return;
  event.preventDefault();
  action();
});

connect();
loadPlaylists().catch((error) => { els.coverage.textContent = String(error); });
