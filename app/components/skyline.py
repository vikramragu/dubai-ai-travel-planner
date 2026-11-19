"""The day/night Dubai skyline behind the app (architecture.md §11.2).

An illustrated skyline drawn for this project (no photo, no licence): Burj Khalifa, Burj Al
Arab, the Emirates Towers, the Dubai Frame, the Museum of the Future and generic towers over
the water. One scene; every colour is a CSS variable with a day and a night value, so switching
themes cross-fades the same picture: the sky darkens, the sun sets as the moon rises, stars and
window lights come on.

It follows Streamlit's own light/dark setting: a small script watches the app's background
brightness and sets ``data-dtp-theme`` on the page (instant, no rerun); the server-side
``st.context.theme.type`` is the starting value. Content sits on translucent cards so it stays
readable over the picture.
"""

from __future__ import annotations

import base64
import random

import streamlit as st

W, H = 1600, 520  # the skyline strip; the sky above is a full-page gradient
HORIZON = 430  # y of the waterline


def _windows(x: float, top: float, width: float, bottom: float, seed: int) -> str:
    """Window lights on a tower face (lit at night; some stay dark, like a real tower)."""
    rnd = random.Random(seed)
    out = []
    y = top + 10
    while y < bottom - 8:
        cx = x + 5
        while cx < x + width - 6:
            if rnd.random() < 0.55:
                out.append(f'<rect class="w" x="{cx:.1f}" y="{y:.1f}" width="3" height="4"/>')
            cx += 8
        y += 11
    return "".join(out)


def _tower(x: float, width: float, height: float, seed: int, cls: str = "b2") -> str:
    top = HORIZON - height
    return f'<rect class="{cls}" x="{x}" y="{top}" width="{width}" height="{height}"/>' + _windows(
        x, top, width, HORIZON, seed
    )


def sky_svg() -> str:
    """The open sky over the city: sun, moon (a masked crescent) and stars."""
    rnd = random.Random(7)
    stars = "".join(
        f'<circle class="star" cx="{rnd.uniform(0, 1600):.0f}" cy="{rnd.uniform(0, 520):.0f}" '
        f'r="{rnd.choice((0.8, 1.0, 1.3, 1.6))}"/>'
        for _ in range(70)
    )
    return (
        """
<svg class="dtp-sky dtp-heavens" viewBox="0 0 1600 900" preserveAspectRatio="xMidYMin slice"
     xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <defs><mask id="dtp-crescent"><rect width="1600" height="900" fill="white"/>
    <circle cx="1268" cy="150" r="40" fill="black"/></mask></defs>
  <g class="stars">"""
        + stars
        + """</g>
  <circle class="sun" cx="800" cy="200" r="58"/>
  <circle class="moon" cx="1250" cy="165" r="44" mask="url(#dtp-crescent)"/>
</svg>"""
    )


def skyline_svg() -> str:
    parts: list[str] = []
    # far towers (lighter, behind)
    for i, (x, w, h) in enumerate(
        [
            (40, 46, 130),
            (100, 38, 180),
            (150, 52, 145),
            (300, 40, 195),
            (520, 46, 160),
            (980, 42, 205),
            (1040, 56, 150),
            (1250, 44, 220),
            (1310, 38, 170),
            (1480, 50, 145),
        ]
    ):
        parts.append(f'<rect class="b1" x="{x}" y="{HORIZON - h}" width="{w}" height="{h}"/>')
        parts.append(_windows(x, HORIZON - h, w, HORIZON, 100 + i))
    # Burj Al Arab (sail) on the left, on its island
    parts.append(
        '<path class="b3" d="M215 430 L215 205 Q300 255 318 430 Z"/>'
        '<path class="b2" d="M209 430 L209 190 L215 190 L215 430 Z"/>'
        '<rect class="b2" x="204" y="182" width="16" height="10"/>'
        '<path class="w" d="M232 300 Q270 320 282 360 L278 362 Q266 326 232 306 Z"/>'
    )
    # near towers
    for i, (x, w, h) in enumerate(
        [
            (360, 58, 210),
            (430, 44, 250),
            (600, 64, 185),
            (680, 50, 235),
            (1110, 60, 250),
            (1180, 48, 195),
            (1380, 62, 235),
            (1450, 40, 185),
        ]
    ):
        parts.append(_tower(x, w, h, 200 + i))
    # Emirates Towers: twin triangular-topped towers
    parts.append(
        '<path class="b2" d="M760 430 L760 170 L790 120 L790 430 Z"/>'
        '<path class="b3" d="M800 430 L800 200 L826 160 L826 430 Z"/>'
        + _windows(762, 180, 26, 430, 301)
        + _windows(802, 210, 22, 430, 302)
    )
    # Burj Khalifa: stepped needle, the tallest thing in the picture
    parts.append(
        '<path class="b4" d="M880 430 L880 330 L892 330 L892 250 L902 250 L902 170 L910 170 '
        "L910 100 L916 100 L916 45 L920 8 L924 45 L924 100 L930 100 L930 170 L938 170 "
        'L938 250 L948 250 L948 330 L960 330 L960 430 Z"/>'
        + _windows(884, 335, 72, 430, 401)
        + _windows(895, 255, 50, 330, 402)
        + _windows(904, 175, 32, 250, 403)
        + '<circle class="beacon" cx="920" cy="10" r="3"/>'
    )
    # Museum of the Future: a torus
    parts.append(
        '<ellipse class="b3" cx="1035" cy="395" rx="34" ry="30"/>'
        '<ellipse class="sky-hole" cx="1035" cy="395" rx="15" ry="12"/>'
    )
    # Dubai Frame
    parts.append(
        '<path class="b2" d="M1520 430 L1520 300 L1580 300 L1580 430 L1566 430 L1566 314 '
        'L1534 314 L1534 430 Z"/>'
    )
    city = "".join(parts)
    return f"""
<svg class="dtp-sky dtp-city" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMax slice"
     xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <g class="city">{city}</g>
  <rect class="water" x="0" y="{HORIZON}" width="{W}" height="{H - HORIZON}"/>
  <g class="reflection" transform="translate(0 {2 * HORIZON}) scale(1 -1)">{city}</g>
  <rect class="water-shine" x="0" y="{HORIZON}" width="{W}" height="{H - HORIZON}"/>
</svg>"""


CSS = """
<style>
:root, :root[data-dtp-theme="day"] {
  --sky-top: #7cc4f2; --sky-mid: #bfe3f7; --sky-low: #fde9c9;
  --sun: #ffd76a; --sun-o: 1; --moon-o: 0; --star-o: 0; --win-o: 0;
  --b1: #9db6cc; --b2: #6f8ba6; --b3: #5d7a96; --b4: #4f6d8a;
  --water: #5aa9d6; --win: #ffe9a8;
  --card: rgba(255,255,255,0.38); --card-line: rgba(255,255,255,0.5);
  --field: rgba(255,255,255,0.62); --soft-text: #2f3a4d;
}
:root[data-dtp-theme="night"] {
  --sky-top: #070b1f; --sky-mid: #14183a; --sky-low: #3a2a55;
  --sun: #ff9a4a; --sun-o: 0; --moon-o: 1; --star-o: 1; --win-o: 0.95;
  --b1: #1b2340; --b2: #141a32; --b3: #10152b; --b4: #0c1124;
  --water: #0b1430; --win: #ffd27a;
  --card: rgba(10,13,30,0.42); --card-line: rgba(255,255,255,0.10);
  --field: rgba(20,24,48,0.55); --soft-text: #c9d1e3;
}
#dtp-bg { position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background: linear-gradient(180deg, var(--sky-top), var(--sky-mid) 55%, var(--sky-low));
  transition: background 1.6s ease; }
#dtp-bg .dtp-city { position: absolute; left: 0; bottom: 0; width: 100%; height: 46vh; }
#dtp-bg .dtp-heavens { position: absolute; left: 0; top: 0; width: 100%; height: 70vh; }
#dtp-bg * { transition: fill 1.6s ease, opacity 1.6s ease, transform 1.6s ease; }
.dtp-sky .b1 { fill: var(--b1); } .dtp-sky .b2 { fill: var(--b2); }
.dtp-sky .b3 { fill: var(--b3); } .dtp-sky .b4 { fill: var(--b4); }
.dtp-sky .w { fill: var(--win); opacity: var(--win-o); }
.dtp-sky .beacon { fill: #ff4d4d; opacity: var(--win-o); }
.dtp-sky .sun { fill: var(--sun); opacity: var(--sun-o);
  transform: translateY(calc((1 - var(--sun-o)) * 260px)); }
.dtp-sky .moon { opacity: var(--moon-o); fill: #f4f1de;
  transform: translateY(calc((1 - var(--moon-o)) * 160px)); }
.dtp-sky .sky-hole { fill: var(--sky-mid); }
.dtp-sky .star { fill: #fff; opacity: var(--star-o); }
.dtp-sky .water { fill: var(--water); }
.dtp-sky .reflection { opacity: 0.18; }
.dtp-sky .water-shine { fill: var(--water); opacity: 0.55; }
/* the app over the picture */
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
  background: transparent !important; }
[data-testid="stMainBlockContainer"] { position: relative; z-index: 1; background: var(--card);
  border: 1px solid var(--card-line); border-radius: 18px;
  margin-top: 2.5rem; margin-bottom: 2rem;
  backdrop-filter: blur(5px) saturate(1.2); -webkit-backdrop-filter: blur(5px) saturate(1.2);
  box-shadow: 0 10px 40px rgba(0,0,0,0.12); }
/* "glass" fields, cards and buttons: the skyline shows through them too */
[data-testid="stMainBlockContainer"] :is([data-baseweb="input"], [data-baseweb="base-input"],
  [data-baseweb="select"] > div, [data-baseweb="textarea"], [data-testid="stNumberInputContainer"],
  input, textarea, button[kind="secondary"], button[kind="secondaryFormSubmit"],
  [data-testid="stExpander"] details, [data-testid="stVerticalBlockBorderWrapper"]) {
  background-color: var(--field) !important; }
[data-testid="stMainBlockContainer"] :is(input, textarea) {
  background-color: transparent !important; }
/* Streamlit's grey "secondary" text is too faint over the picture: give it solid contrast */
[data-testid="stMainBlockContainer"] :is([data-testid="stCaptionContainer"],
  [data-testid="stCaptionContainer"] *, [data-testid="stExpander"] summary,
  [data-testid="stExpander"] summary *, [data-testid="stMetricLabel"] *,
  [data-testid="stWidgetLabel"] *, [data-testid="stFileUploaderDropzoneInstructions"] *) {
  color: var(--soft-text) !important; opacity: 1 !important; }
[data-testid="stSidebar"] { z-index: 2; }
</style>
"""

SCRIPT = """
<script>
(function () {
  if (window.__dtpSkyline) return; window.__dtpSkyline = true;
  function luminance(css) {
    var m = css && css.match(/\\d+(\\.\\d+)?/g); if (!m) return null;
    return (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]) / 255;
  }
  function detect() {
    // We make the app background transparent (to show the skyline), so read the theme from
    // Streamlit's text colour instead: light text = dark theme.
    var app = document.querySelector('.stApp');
    var l = app ? luminance(getComputedStyle(app).color) : null;
    if (l !== null) document.documentElement.dataset.dtpTheme = l > 0.5 ? 'night' : 'day';
  }
  detect();
  new MutationObserver(detect).observe(document.documentElement,
    { attributes: true, childList: true, subtree: true, attributeFilter: ['class', 'style'] });
  setInterval(detect, 1000);
})();
</script>
"""


def render_skyline() -> None:
    """Draw the background (call once near the top of every page).

    ``st.html`` sanitises its HTML (``<svg>`` is dropped, and so is a script that contains
    markup) and React re-renders its block, so our script carries the drawing base64-encoded
    and attaches it to ``<body>``, outside the part of the page Streamlit manages, once per
    browser tab."""
    start = "night" if st.context.theme.type == "dark" else "day"
    # base64: the script then contains no markup the sanitiser could act on
    drawing = base64.b64encode((sky_svg() + skyline_svg()).encode()).decode()
    st.html(
        f"{CSS}<script>(function () {{"
        "  var root = document.documentElement;"
        f"  root.dataset.dtpTheme = root.dataset.dtpTheme || '{start}';"
        "  if (document.getElementById('dtp-bg')) return;"
        "  var bg = document.createElement('div');"
        "  bg.id = 'dtp-bg';"
        f"  bg.innerHTML = atob('{drawing}');"
        "  document.body.prepend(bg);"
        f"}})();</script>{SCRIPT}",
        unsafe_allow_javascript=True,
    )
