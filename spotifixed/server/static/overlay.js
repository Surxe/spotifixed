// The overlay section of the settings pane. It edits the daemon's overlay settings
// document, which a running overlay follows live, so moving a slider moves the lyric
// line on screen. The pane itself (opening, closing) belongs to settings.js.
//
// The daemon owns every rule about what a setting may be and sends them with the
// document (`rules`); this file only decides how each kind of setting is drawn. Values
// set here are overrides on top of the configured ones. The daemon saves each one as it
// is made, so there is nothing to save by hand; the panel shows which settings are
// overridden and can put them back.

// How each setting is shown. `slider` is the comfortable range for dragging; the number
// box beside it accepts the whole range the daemon allows. A margin's slider depends on
// the anchor (see marginSlider), so `slider` there is only its widest form.
export const FIELDS = [
  { key: "anchor", label: "Anchor" },
  { key: "margin_x", label: "Margin X", unit: "px", slider: [-1000, 1000], edges: ["left", "right"] },
  { key: "margin_y", label: "Margin Y", unit: "px", slider: [-600, 600], edges: ["top", "bottom"] },
  { key: "monitor", label: "Monitor", monitors: true },
  { key: "font_size", label: "Font size", unit: "pt", slider: [8, 96] },
  { key: "max_width", label: "Max width", unit: "px", slider: [200, 3000] },
  { key: "font", label: "Font", placeholder: "e.g. Sans Bold" },
  { key: "color", label: "Text colour" },
  { key: "outline_color", label: "Outline colour" },
  { key: "lines", label: "Lines" },
  { key: "lead_ms", label: "Lead", unit: "ms", slider: [-500, 1000] },
  { key: "fade_ms", label: "Fade", unit: "ms", slider: [0, 1000] },
  { key: "underline_word", label: "Underline word", unit: "estimated" },
  { key: "volume_indicator", label: "Show volume changes" },
];

const SEND_EVERY_MS = 60;
const PREVIEW_S = 8;
const PREVIEW_RENEW_MS = 3000;

// What to send for the text a control holds, or undefined when it is not sendable yet
// (a half-typed number, say). The daemon has the final word; this only avoids sending
// what it would certainly refuse.
export function parseValue(rule, raw) {
  if (rule.kind === "int") {
    if (!/^-?\d+$/.test(String(raw).trim())) return undefined;
    const value = Number(raw);
    return value >= rule.min && value <= rule.max ? value : undefined;
  }
  if (rule.kind === "bool") return typeof raw === "boolean" ? raw : undefined;
  if (rule.kind === "choice") return rule.choices.includes(raw) ? raw : undefined;
  if (rule.kind === "color") {
    return /^(#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})|[a-z]{3,20})$/i.test(raw) ? raw : undefined;
  }
  return String(raw);
}

// The entries of the Monitor list: automatic, then each monitor the running overlay
// reported, then the chosen one if it is not among them (unplugged, or the overlay is not
// running to say). With no overlay running there is nothing to list, and the last entry
// says so.
export function monitorChoices(monitors, current) {
  const choices = [{ value: "", text: "Automatic (the screen in use)" }];
  for (const { connector, description } of monitors ?? []) {
    choices.push({ value: connector, text: description ? `${description} (${connector})` : connector });
  }
  const listed = choices.some((choice) => choice.value === current);
  if (!listed) {
    const missing = monitors?.length ? " (not connected)" : "";
    choices.push({ value: current, text: `${current}${missing}` });
  }
  if (!monitors?.length) {
    choices.push({ value: "", text: "Your monitors are listed while the overlay is running", disabled: true });
  }
  return choices;
}

// The slider range for a margin under an anchor. Along an axis the overlay is attached
// on, the margin is a distance in from that edge, and a negative one would slide the
// line off the screen, so the slider starts at 0. On an axis it is centred on, the
// margin is an offset either way.
export function marginSlider(field, anchor) {
  const [low, high] = field.slider;
  const attached = field.edges.some((edge) => String(anchor).split("-").includes(edge));
  return attached ? [0, high] : [low, high];
}

// The value a colour picker can show for a colour setting: it only understands #rrggbb.
export function pickerColor(value) {
  const text = String(value);
  if (/^#[0-9a-f]{6}$/i.test(text)) return text.toLowerCase();
  if (/^#[0-9a-f]{8}$/i.test(text)) return text.slice(0, 7).toLowerCase();
  if (/^#[0-9a-f]{3}$/i.test(text)) {
    return `#${[...text.slice(1)].map((c) => c + c).join("")}`.toLowerCase();
  }
  return null;
}

function node(tag, attributes = {}, ...children) {
  const made = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "class") made.className = value;
    else if (name === "text") made.textContent = value;
    else made.setAttribute(name, value);
  }
  made.append(...children);
  return made;
}

export function initOverlayPanel({ getJson, postJson }) {
  const $ = (id) => document.getElementById(id);
  const els = {
    status: $("overlay-status"), preview: $("overlay-preview"), form: $("overlay-form"),
    reset: $("overlay-reset"), enabled: $("overlay-enabled"),
    note: $("overlay-note"), problem: $("overlay-problem"),
  };
  const rows = new Map(); // key -> { row, inputs, drop }
  let rules = null;
  let doc = null;
  let pending = {};
  let inFlight = {};
  let sendTimer = 0;
  let sending = false;
  let previewTimer = 0;

  // ------------------------------- sending ------------------------------- //

  function queue(key, value) {
    pending[key] = value;
    if (!sendTimer) sendTimer = setTimeout(flush, SEND_EVERY_MS);
  }

  async function flush() {
    sendTimer = 0;
    if (sending || !Object.keys(pending).length) return;
    inFlight = pending;
    pending = {};
    sending = true;
    const result = await postJson("/overlay", inFlight);
    sending = false;
    inFlight = {};
    if (!result.ok) {
      els.note.textContent = result.error;
      render(); // put the controls back to what the daemon actually holds
    } else {
      els.note.textContent = "";
      apply(result.data);
    }
    if (Object.keys(pending).length) sendTimer = setTimeout(flush, SEND_EVERY_MS);
  }

  function changed(key, raw) {
    const value = parseValue(rules[key], raw);
    if (value === undefined) return;
    for (const input of rows.get(key).inputs) {
      if (input !== document.activeElement) setInput(input, value);
    }
    queue(key, value);
  }

  // ------------------------------- building ------------------------------ //

  function setInput(input, value) {
    if (input.type === "checkbox") {
      input.checked = Boolean(value);
    } else if (input.type === "color") {
      const shown = pickerColor(value);
      if (shown) input.value = shown;
    } else {
      input.value = String(value);
    }
  }

  function controlsFor(field, rule) {
    const id = `overlay-field-${field.key}`;
    if (field.monitors) {
      // Filled in by render, which knows the monitors.
      const select = node("select", { id });
      select.addEventListener("change", () => changed(field.key, select.value));
      return [select];
    }
    if (rule.kind === "choice" || field.key === "lines") {
      const choices = rule.kind === "choice" ? rule.choices : ["1", "2"];
      const select = node("select", { id }, ...choices.map((c) => node("option", { value: c, text: c })));
      select.addEventListener("change", () => changed(field.key, select.value));
      return [select];
    }
    if (rule.kind === "bool") {
      const box = node("input", { id, type: "checkbox" });
      box.addEventListener("change", () => changed(field.key, box.checked));
      return [box];
    }
    if (rule.kind === "int") {
      const [low, high] = field.slider ?? [rule.min, rule.max];
      const slider = node("input", { type: "range", min: low, max: high, step: 1, "aria-label": field.label });
      const box = node("input", { id, type: "number", min: rule.min, max: rule.max, step: 1 });
      for (const input of [slider, box]) {
        input.addEventListener("input", () => changed(field.key, input.value));
      }
      return [slider, box];
    }
    if (rule.kind === "color") {
      const picker = node("input", { type: "color", "aria-label": field.label });
      const box = node("input", { id, type: "text", spellcheck: "false", size: 10 });
      picker.addEventListener("input", () => changed(field.key, picker.value));
      box.addEventListener("change", () => changed(field.key, box.value.trim()));
      return [picker, box];
    }
    const box = node("input", { id, type: "text", spellcheck: "false", placeholder: field.placeholder ?? "" });
    box.addEventListener("change", () => changed(field.key, box.value.trim()));
    return [box];
  }

  function build() {
    els.form.replaceChildren();
    rows.clear();
    for (const field of FIELDS) {
      const rule = rules[field.key];
      if (!rule) continue; // a setting this daemon does not have
      const inputs = controlsFor(field, rule);
      const drop = node("button", {
        type: "button", class: "drop", title: "Back to the configured value", text: "x",
      });
      drop.addEventListener("click", () => queue(field.key, null));
      const row = node(
        "div",
        { class: "row" },
        node("label", { for: `overlay-field-${field.key}`, text: field.label }),
        node("span", { class: "controls" }, ...inputs),
        node("span", { class: "unit muted", text: field.unit ?? "" }),
        drop,
      );
      rows.set(field.key, { row, inputs, drop });
      els.form.append(row);
    }
  }

  // ------------------------------- showing ------------------------------- //

  function fillMonitors(select, choices) {
    const wanted = JSON.stringify(choices);
    if (select.dataset.choices === wanted) return; // leave an open list alone
    select.dataset.choices = wanted;
    const value = select.value;
    select.replaceChildren(...choices.map((choice) => {
      const option = node("option", { value: choice.value, text: choice.text });
      option.disabled = Boolean(choice.disabled);
      return option;
    }));
    select.value = value;
  }

  function render() {
    if (!doc || !rules) return;
    for (const [key, { row, inputs, drop }] of rows) {
      const overridden = key in doc.overrides;
      row.classList.toggle("overridden", overridden);
      drop.hidden = !overridden;
      const configured = doc.defaults[key] === "" ? "(empty)" : doc.defaults[key];
      row.title = overridden ? `Configured value: ${configured}` : "";
      const field = FIELDS.find((candidate) => candidate.key === key);
      if (field.monitors) {
        const current = key in pending ? pending[key] : key in inFlight ? inFlight[key] : doc.settings[key];
        fillMonitors(inputs[0], monitorChoices(doc.monitors, current ?? doc.defaults[key]));
      }
      if (field.edges) {
        const [low, high] = marginSlider(field, doc.settings.anchor);
        inputs[0].min = String(low);
        inputs[0].max = String(high);
      }
      // A value on its way to the daemon is newer than the document in hand.
      if (key in pending || key in inFlight) continue;
      for (const input of inputs) {
        if (input !== document.activeElement) setInput(input, doc.settings[key]);
      }
    }
    const running = doc.clients > 0;
    const enabled = doc.enabled !== false;
    els.status.textContent = !enabled ? "overlay off" : running ? "overlay running" : "overlay not running";
    els.status.className = `pill ${running ? "on" : "off"}`;
    // Only an overlay that spotifixed started itself can be switched from here.
    els.enabled.checked = enabled;
    els.enabled.disabled = !doc.managed;
    els.enabled.parentElement.title = doc.managed
      ? ""
      : "The overlay is run separately here, so it cannot be switched on or off from this page";
    els.problem.textContent = doc.problem ?? "";
    els.preview.disabled = !enabled;
    if (!enabled && els.preview.checked) setPreview(false);
    els.reset.disabled = !Object.keys(doc.overrides).length;
  }

  function apply(next) {
    if (doc && next.version < doc.version) return; // stale
    doc = next;
    render();
  }

  // -------------------------------- preview ------------------------------ //

  function setPreview(on) {
    clearInterval(previewTimer);
    previewTimer = 0;
    els.preview.checked = on;
    if (on) {
      // The daemon ends a preview by itself; keep asking while the box stays ticked, so
      // a closed tab cannot leave the sample line on screen.
      const renew = () => postJson("/overlay/preview", { seconds: PREVIEW_S });
      renew();
      previewTimer = setInterval(renew, PREVIEW_RENEW_MS);
    } else {
      postJson("/overlay/preview", { seconds: 0 });
    }
  }

  // -------------------------------- wiring ------------------------------- //

  // The settings pane was opened: fetch the rules the first time, and draw the controls.
  async function opened() {
    if (rules) return;
    try {
      const described = await getJson("/overlay");
      rules = described.rules;
      build();
      apply(described);
    } catch (error) {
      els.note.textContent = String(error);
    }
  }

  // The settings pane was closed: a sample line must not stay up with nobody looking.
  function closed() {
    if (els.preview.checked) setPreview(false);
  }

  els.preview.addEventListener("change", () => setPreview(els.preview.checked));
  els.enabled.addEventListener("change", async () => {
    const result = await postJson("/overlay/enabled", { enabled: els.enabled.checked });
    els.note.textContent = result.ok ? "" : result.error;
    if (result.ok) apply(result.data);
    else render(); // put the switch back to what the daemon actually holds
  });
  els.form.addEventListener("submit", (event) => event.preventDefault());
  els.reset.addEventListener("click", async () => {
    pending = {};
    const result = await postJson("/overlay/reset", {});
    if (result.ok) apply(result.data);
  });

  return {
    apply,
    opened,
    closed,
    disconnected() {
      // A restarted daemon counts versions from 1 again; do not mistake it for stale.
      if (doc) doc = { ...doc, version: 0 };
    },
  };
}
