"""The renderer contract, and the toolkit-independent parts of placing and styling.

A renderer owns the main loop. It asks ``view_source`` what to show: whenever
:meth:`Renderer.wake` is called (from any thread), and on a short timer while the view
says it is ``ticking``. It decides nothing about content. Where and how it draws can be
changed while it runs, with :meth:`Renderer.reconfigure`.

``Placement`` and ``Style`` turn the settings into what a renderer needs, here rather
than in a renderer so the rules are tested without a display.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ...errors import ConfigError
from ..presenter import OverlayView

ViewSource = Callable[[], OverlayView]

_EDGES = ("top", "bottom", "left", "right")
_ANCHORS: dict[str, tuple[str, ...]] = {
    "top": ("top",),
    "bottom": ("bottom",),
    "left": ("left",),
    "right": ("right",),
    "top-left": ("top", "left"),
    "top-right": ("top", "right"),
    "bottom-left": ("bottom", "left"),
    "bottom-right": ("bottom", "right"),
    "center": (),
}
# A colour ends up inside a stylesheet, so only plain forms are accepted.
_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|[a-zA-Z]{3,20}")


@dataclass(frozen=True, slots=True)
class Placement:
    """Where the overlay sits on its monitor.

    ``edges`` are the screen edges it is attached to, with ``margins`` giving the
    distance from each. On an axis with no attached edge the compositor centres it, and
    ``shift_x`` / ``shift_y`` move it off centre (right and down are positive).
    """

    edges: frozenset[str]
    margins: dict[str, int]
    shift_x: int = 0
    shift_y: int = 0

    @classmethod
    def from_settings(cls, anchor: str, margin_x: int, margin_y: int) -> Placement:
        if anchor not in _ANCHORS:
            raise ConfigError(
                f"SPOTIFIXED_OVERLAY_ANCHOR must be one of {', '.join(_ANCHORS)}; got {anchor!r}"
            )
        edges = frozenset(_ANCHORS[anchor])
        margins = {edge: 0 for edge in _EDGES}
        horizontal = edges & {"left", "right"}
        vertical = edges & {"top", "bottom"}
        for edge in horizontal:
            margins[edge] = margin_x
        for edge in vertical:
            margins[edge] = margin_y
        return cls(
            edges=edges,
            margins=margins,
            shift_x=0 if horizontal else margin_x,
            shift_y=0 if vertical else margin_y,
        )


@dataclass(frozen=True, slots=True)
class Style:
    font: str  # a Pango description without the size, e.g. "Sans Bold"
    font_size: int  # points
    color: str
    outline_color: str
    max_width: int  # pixels; longer lines wrap
    fade_ms: int

    def __post_init__(self) -> None:
        for name, value in (("COLOR", self.color), ("OUTLINE_COLOR", self.outline_color)):
            if not _COLOR.fullmatch(value):
                raise ConfigError(
                    f"SPOTIFIXED_OVERLAY_{name} must be a colour like #RRGGBB; got {value!r}"
                )
        if self.font_size < 1 or self.max_width < 1:
            raise ConfigError("overlay font size and maximum width must be positive")


class Renderer(Protocol):
    def run(self, view_source: ViewSource) -> None:
        """Show views from ``view_source`` until :meth:`stop`. Blocks."""
        ...

    def wake(self) -> None:
        """Something changed; ask ``view_source`` again now. Safe from any thread."""
        ...

    def reconfigure(self, placement: Placement, style: Style, monitor: str) -> None:
        """Move and restyle the overlay without restarting it. Safe from any thread."""
        ...

    def stop(self) -> None:
        """End :meth:`run`. Safe from any thread."""
        ...

    def monitors(self) -> list[dict[str, str]]:
        """The monitors on this display as ``{connector, description}``. Safe from any thread."""
        ...

    def on_monitors_changed(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` (on the renderer's thread) when a monitor comes or goes."""
        ...

    def snapshot(self, view: OverlayView, path: Path) -> None:
        """Draw ``view`` once, save it as a PNG at ``path``, and return."""
        ...

    def describe(self) -> dict[str, Any]:
        """What is on screen right now, for diagnostics and tests."""
        ...
