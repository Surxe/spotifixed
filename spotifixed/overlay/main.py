"""Wiring for the ``overlay`` command: settings to model to client to renderer.

The first statement below loads ``libgtk4-layer-shell`` and has to stay first: the
library must be in the process before ``libwayland-client`` is, which means before
anything imports ``gi``. That is also why the renderer is imported inside functions.
"""

from __future__ import annotations

import ctypes

try:
    ctypes.CDLL("libgtk4-layer-shell.so.0")
    _LAYER_SHELL_LOADED = True
except OSError:  # not installed; reported properly when the renderer is built
    _LAYER_SHELL_LOADED = False

from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from ..config import Settings  # noqa: E402
from ..errors import PlaylistPlayerError  # noqa: E402
from ..overlay_settings import OverlaySettings, salvage  # noqa: E402
from .client import DaemonClient  # noqa: E402
from .clock import now_epoch_ms  # noqa: E402
from .presenter import OverlayModel, OverlayView, sample_view  # noqa: E402
from .render.base import Placement, Renderer, Style  # noqa: E402


def placement_and_style(values: OverlaySettings) -> tuple[Placement, Style]:
    """What a renderer needs to draw with these values."""
    placement = Placement.from_settings(values.anchor, values.margin_x, values.margin_y)
    style = Style(
        font=values.font,
        font_size=values.font_size,
        color=values.color,
        outline_color=values.outline_color,
        max_width=values.max_width,
        fade_ms=values.fade_ms,
    )
    return placement, style


def build_renderer(settings: Settings) -> Renderer:
    """The renderer for these settings. Fails with one clear line if it cannot work here."""
    values = OverlaySettings.from_settings(settings)
    placement, style = placement_and_style(values)
    if not _LAYER_SHELL_LOADED:
        raise PlaylistPlayerError(
            "libgtk4-layer-shell is not installed (Debian: libgtk4-layer-shell0 and "
            "gir1.2-gtk4layershell-1.0)"
        )
    try:
        from .render.gtk_layer import GtkLayerRenderer
    except (ImportError, ValueError) as exc:
        raise PlaylistPlayerError(f"the GTK bindings are not available: {exc}") from exc
    return GtkLayerRenderer(placement, style, monitor=values.monitor)


def demo_view(settings: Settings) -> OverlayView:
    return sample_view(
        show_next=settings.overlay_lines == 2, underline_word=settings.overlay_underline_word
    )


class LiveSettings:
    """Applies the daemon's overlay settings document to a running overlay.

    The values this overlay was started with are its baseline; the document's overrides
    go on top, so a change made on the remote-control page takes effect at once and the
    baseline returns when the override is dropped. An override this version cannot use
    is skipped rather than fatal: the daemon may be newer than the overlay.
    """

    def __init__(self, base: OverlaySettings, model: OverlayModel, renderer: Renderer) -> None:
        self._base = base
        self._current = base
        self._model = model
        self._renderer = renderer

    def apply(self, document: dict[str, Any]) -> None:
        overrides, _dropped = salvage(document.get("overrides", {}))
        values = self._base.merged(overrides)
        if values != self._current:
            self._current = values
            self._model.configure(
                lead_ms=values.lead_ms,
                show_next=values.lines == 2,
                underline_word=values.underline_word,
                volume_indicator=values.volume_indicator,
            )
            self._renderer.reconfigure(*placement_and_style(values), values.monitor)
        until = document.get("preview_until_ms")
        self._model.set_preview_until(until if isinstance(until, int) else 0)


def connect(settings: Settings, renderer: Renderer) -> tuple[OverlayModel, DaemonClient]:
    """The model and the client that feeds it, set to wake ``renderer`` on every change."""
    model = OverlayModel(
        lead_ms=settings.overlay_lead_ms,
        show_next=settings.overlay_lines == 2,
        underline_word=settings.overlay_underline_word,
        volume_indicator=settings.overlay_volume_indicator,
    )
    live = LiveSettings(OverlaySettings.from_settings(settings), model, renderer)
    client = DaemonClient(
        settings.base_url,
        model,
        on_change=renderer.wake,
        on_overlay=live.apply,
        monitors=renderer.monitors,
    )
    renderer.on_monitors_changed(client.report_monitors)
    return model, client


def run(settings: Settings, *, demo: bool = False, snapshot: str | None = None) -> int:
    renderer = build_renderer(settings)
    if snapshot:
        renderer.snapshot(demo_view(settings), Path(snapshot))
        print(f"wrote {snapshot}")
        return 0
    if demo:
        view = demo_view(settings)
        renderer.run(lambda: view)
        return 0

    model, client = connect(settings, renderer)
    client.start()
    try:
        renderer.run(lambda: model.view(now_epoch_ms()))
    finally:
        client.stop()
    return 0
