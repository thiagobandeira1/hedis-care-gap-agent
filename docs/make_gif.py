"""Render ``docs/demo.gif``: the SPEC section 10 storyboard over synthetic personas.

Everything runs keyless: the API is started with ``CAREGAP_MODELS=fake``, so every verdict,
validator card and drafted plan shown is the deterministic output the committed persona goldens
were built from. Frames are captured from the real Streamlit console with Playwright when a
Chromium is available; otherwise the same storyboard is drawn from the API's JSON as text
cards (Pillow), and the log says which path was used.

Run from the repo root::

    uv run --with playwright --with pillow python -m playwright install chromium   # once
    uv run --with playwright --with pillow python docs/make_gif.py [--fallback]

Ports 8010 (API) and 8501 (UI) must be free; demo sqlite files land under gitignored ``data/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("make_gif")

REPO = Path(__file__).resolve().parents[1]
FRAMES = REPO / "docs" / "demo" / "frames"
OUT = REPO / "docs" / "demo.gif"
API = "http://127.0.0.1:8010"
UI = "http://127.0.0.1:8501"
TONY = "939eea26-a679-2564-5cf9-c0fd557beefc"
AS_OF = "2026-06-30"
WIDTH = 960
MAX_BYTES = 3 * 1024 * 1024
UV = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")

DEMO_ENV = {
    "CAREGAP_P6_MODE": "embedded",
    "CAREGAP_P6_DB_PATH": "data/p6.duckdb",
    "CAREGAP_MODELS": "fake",
    "CAREGAP_CHECKPOINT_PATH": "data/demo-checkpoints.sqlite",
    "CAREGAP_RUNSTORE_PATH": "data/demo-caregap.sqlite",
    "CAREGAP_API_HOST": "127.0.0.1",
    "CAREGAP_API_PORT": "8010",
    "STREAMLIT_SERVER_PORT": "8501",
    "STREAMLIT_SERVER_HEADLESS": "true",
    "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
}

BG = (250, 250, 252)
INK = (28, 30, 38)
MUTED = (100, 104, 118)
ACCENT = (33, 90, 160)
CHIP = {
    "gap_open": (204, 68, 51),
    "closed": (46, 140, 84),
    "excluded": (120, 110, 160),
    "not_eligible": (150, 150, 150),
    "needs_review": (214, 140, 20),
}

# --- process control --------------------------------------------------------------------


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC")}
    env.update(DEMO_ENV)
    return env


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        cwd=REPO,
        env=_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def _kill(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],  # noqa: S607
            check=False,
            capture_output=True,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def _get(url: str, timeout: float = 10.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback only
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, body: dict[str, Any]) -> Any:
    data = json.dumps(body).encode("utf-8")
    headers = {"content-type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310 - loopback
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - loopback only
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"POST {url} -> {exc.code}: {detail[:500]}") from exc


def _wait_http(url: str, seconds: float, label: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 - loopback only
                if resp.status == 200:
                    log.info("%s is up", label)
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(1)
    raise RuntimeError(f"{label} did not come up at {url} within {seconds:.0f}s")


# --- drive a run through the API ---------------------------------------------------------


def drive_run() -> dict[str, Any]:
    """Start a run for Tony, wait for the interrupt, and return everything the cards need."""
    panel = _get(f"{API}/v1/panel?as_of={AS_OF}&limit=50")
    gaps = _get(f"{API}/v1/patients/{TONY}/gaps?as_of={AS_OF}")
    accepted = _post(
        f"{API}/v1/runs",
        {
            "as_of": AS_OF,
            "patient_ids": [TONY],
            "options": {"validation_mode": "escalated", "approval_mode": "interrupt"},
        },
    )
    run_id = accepted["run_id"]
    log.info("run %s queued", run_id)
    deadline = time.monotonic() + 180
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            detail = _get(f"{API}/v1/runs/{run_id}/patients/{TONY}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:  # the row appears once the background loop reaches the patient
                raise
            time.sleep(1)
            continue
        status = detail["patient_run"].get("status")
        if detail.get("pending") is not None or status in {"completed", "rejected", "no_action"}:
            break
        time.sleep(1)
    else:
        raise RuntimeError(f"run {run_id} never reached the interrupt")
    log.info("patient run status=%s pending=%s", status, detail.get("pending") is not None)
    return {"panel": panel, "gaps": gaps, "run_id": run_id, "detail": detail}


def approve(run: dict[str, Any]) -> dict[str, Any]:
    pending = run["detail"].get("pending")
    if pending is None:
        return {"skipped": True}
    resolutions = [
        {
            "measure_id": item.get("measure_id"),
            "status": "open",
            "reason": "Demo reviewer confirmed the engine's finding on the evidence shown.",
        }
        for item in pending.get("review_items", [])
    ]
    body = {
        "decision_id": f"demo-{uuid.uuid4().hex[:12]}",
        "action": "approve",
        "review_resolutions": resolutions,
        "reviewer": "demo-reviewer",
        "note": "Approved in the demo GIF; fake models, synthetic persona.",
    }
    result: dict[str, Any] = _post(f"{API}/v1/runs/{run['run_id']}/patients/{TONY}/decision", body)
    log.info("decision recorded: kind=%s", result.get("kind"))
    return result


# --- drawing helpers ---------------------------------------------------------------------


def _font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = ["consola.ttf", "DejaVuSansMono.ttf"] if mono else ["segoeui.ttf", "DejaVuSans.ttf"]
    roots = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")]
    for root in roots:
        for name in names:
            path = root / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _card(height: int = 560) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (WIDTH, height), BG)
    return img, ImageDraw.Draw(img)


def _title_card(title: str, lines: list[str], footer: str = "") -> Image.Image:
    img, d = _card()
    d.rectangle([0, 0, WIDTH, 8], fill=ACCENT)
    d.text((48, 70), title, fill=INK, font=_font(40))
    y = 150
    for line in lines:
        for wrapped in textwrap.wrap(line, 78) or [""]:
            d.text((48, y), wrapped, fill=MUTED, font=_font(22))
            y += 34
        y += 10
    if footer:
        d.text((48, 500), footer, fill=ACCENT, font=_font(18))
    return img


def _caption(img: Image.Image, text: str) -> Image.Image:
    """Prepend a one-line caption bar so every frame says what it shows."""
    bar = 44
    out = Image.new("RGB", (img.width, img.height + bar), BG)
    d = ImageDraw.Draw(out)
    d.rectangle([0, 0, img.width, bar], fill=ACCENT)
    d.text((16, 10), text, fill=(255, 255, 255), font=_font(20))
    out.paste(img, (0, bar))
    return out


def _table_card(
    title: str, header: list[str], rows: list[list[str]], note: str = ""
) -> Image.Image:
    img, d = _card()
    d.rectangle([0, 0, WIDTH, 8], fill=ACCENT)
    d.text((40, 30), title, fill=INK, font=_font(28))
    mono = _font(17, mono=True)
    widths = [max(len(str(r[i])) for r in [header, *rows]) + 2 for i in range(len(header))]
    y = 90
    for i, row in enumerate([header, *rows]):
        x = 40
        for w, cell in zip(widths, row, strict=True):
            d.text((x, y), str(cell)[: w - 1], fill=INK if i else ACCENT, font=mono)
            x += w * 10
        y += 26
        if y > 500:
            d.text((40, y), "...", fill=MUTED, font=mono)
            break
    if note:
        d.text((40, 520), note, fill=MUTED, font=_font(16))
    return img


def _verdict_card(gaps: dict[str, Any]) -> Image.Image:
    img, d = _card()
    d.rectangle([0, 0, WIDTH, 8], fill=ACCENT)
    d.text(
        (40, 30),
        f"Patient {TONY[:8]}... at as_of {AS_OF}: engine verdicts",
        fill=INK,
        font=_font(26),
    )
    x, y = 40, 90
    for ev in gaps.get("evaluations", []):
        verdict = str(ev.get("verdict", "?"))
        label = f"{ev.get('measure_id')}  {verdict}"
        w = 16 + int(d.textlength(label, font=_font(18)))
        if x + w > WIDTH - 40:
            x, y = 40, y + 44
        d.rounded_rectangle([x, y, x + w, y + 32], radius=8, fill=CHIP.get(verdict, MUTED))
        d.text((x + 8, y + 5), label, fill=(255, 255, 255), font=_font(18))
        x += w + 12
    y += 60
    small = _font(16)
    for ev in gaps.get("evaluations", [])[:8]:
        reason = str(ev.get("reason") or ev.get("verdict_reason") or "")
        esc = ", ".join(str(e.get("code", e)) for e in ev.get("escalations", [])) or "-"
        line = (
            f"{ev.get('measure_id')}: den={ev.get('denominator')} "
            f"num={ev.get('numerator')} esc={esc} {reason}"
        )
        d.text((40, y), textwrap.shorten(line, 105), fill=MUTED, font=small)
        y += 26
    return img


def _approval_card(pending: dict[str, Any] | None) -> Image.Image:
    img, d = _card()
    d.rectangle([0, 0, WIDTH, 8], fill=ACCENT)
    d.text((40, 30), "Approval card (LangGraph interrupt)", fill=INK, font=_font(26))
    y = 90
    if pending is None:
        d.text((40, y), "No pending approval for this run.", fill=MUTED, font=_font(18))
        return img
    for gap in pending.get("open_gaps", []):
        d.text(
            (40, y),
            f"open gap: {gap.get('measure_id')}  priority={gap.get('priority')}",
            fill=INK,
            font=_font(18),
        )
        y += 28
    plan = pending.get("plan") or {}
    for action in plan.get("actions", [])[:4]:
        text = f"{action.get('kind')}: {action.get('detail') or action.get('text') or ''}"
        for wrapped in textwrap.wrap(text, 95)[:3]:
            d.text((60, y), wrapped, fill=MUTED, font=_font(16))
            y += 24
    y += 10
    for i, (label, color) in enumerate(
        [
            ("Approve", CHIP["closed"]),
            ("Edit & approve", ACCENT),
            ("Request revision", CHIP["needs_review"]),
            ("Reject", CHIP["gap_open"]),
        ]
    ):
        x = 40 + i * 220
        d.rounded_rectangle([x, 470, x + 200, 512], radius=8, fill=color)
        d.text((x + 16, 480), label, fill=(255, 255, 255), font=_font(18))
    d.text((40, 525), str(pending.get("demo_grade_notice", "")), fill=MUTED, font=_font(15))
    return img


def _readme_eval_card() -> Image.Image:
    text = (REPO / "README.md").read_text(encoding="utf-8").splitlines()
    rows: list[str] = []
    keep = False
    for line in text:
        if "EVAL-MEASURES:BEGIN" in line:
            keep = True
            continue
        if "EVAL-MEASURES:END" in line:
            break
        if keep and line.startswith("|") and not line.startswith("|---"):
            rows.append(line)
    cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
    header, body = (cells[0], cells[1:]) if cells else (["(no eval region)"], [])
    return _table_card(
        "README eval table (rewritten by `caregap report`, checked in CI)",
        header,
        body,
        note="Engine-only, blind gold labels on synthetic personas; agents measured separately.",
    )


# --- storyboard --------------------------------------------------------------------------


def storyboard_cards(
    run: dict[str, Any], outbox: list[dict[str, Any]]
) -> list[tuple[Image.Image, int]]:
    """Fallback path: text cards from the API's JSON."""
    panel_rows = [
        [
            str(i.get("patient_id", ""))[:20],
            str(i.get("open", i.get("gap_open", "")))[:6],
            str(i.get("needs_review", ""))[:6],
            str((i.get("last_run") or {}).get("status", ""))[:12],
        ]
        for i in run["panel"].get("items", [])
    ]
    detail = run["detail"]
    pending = detail.get("pending")
    trace_rows = [
        [
            str(t.get("node", "")),
            str(t.get("status", t.get("outcome", ""))),
            str(t.get("duration_ms", "")),
        ]
        for t in (detail.get("state") or {}).get("trace", [])
    ]
    outbox_rows = [
        [
            o.get("measure_id", ""),
            o.get("kind", ""),
            str(o.get("detail", ""))[:60],
            str(o.get("approval_ref", ""))[:14],
        ]
        for o in outbox
    ]
    return [
        (
            _table_card(
                f"Panel at as_of {AS_OF} (GET /v1/panel)",
                ["patient", "open", "review", "last run"],
                panel_rows,
            ),
            6,
        ),
        (_table_card(f"Run stepper for {run['run_id']}", ["node", "status", "ms"], trace_rows), 6),
        (_verdict_card(run["gaps"]), 8),
        (_approval_card(pending), 8),
        (
            _table_card(
                "Outbox & audit (GET /v1/outbox)",
                ["measure", "kind", "detail", "approval_ref"],
                outbox_rows,
                note="finalize is the only outbox writer; every entry carries the decision id.",
            ),
            7,
        ),
    ]


SCROLL_JS = """
(step) => {
  const sels = ['section[data-testid="stMain"]', 'section.main',
                '[data-testid="stAppViewContainer"]', '[data-testid="stAppViewBlockContainer"]'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el && el.scrollHeight > el.clientHeight + 10) { el.scrollBy(0, step); return el.scrollTop; }
  }
  window.scrollBy(0, step);
  return window.scrollY;
}
"""


def _scroll_shots(page: Any, prefix: str, caption: str, limit: int) -> list[Image.Image]:
    """Viewport screenshots while scrolling Streamlit's main pane (it scrolls inside a
    container, so a full-page screenshot would stop at the viewport)."""
    out: list[Image.Image] = []
    last_top = -1
    for i in range(limit):
        path = FRAMES / "shots" / f"{prefix}{i}.png"
        page.screenshot(path=str(path))
        with Image.open(path) as raw:
            img = raw.convert("RGB").resize((WIDTH, int(raw.height * WIDTH / raw.width)))
        out.append(_caption(img, caption))
        top = int(page.evaluate(SCROLL_JS, 620))
        if top == last_top:
            break
        last_top = top
        page.wait_for_timeout(900)
    return out


def capture_playwright(run: dict[str, Any]) -> list[tuple[Image.Image, int]]:
    """Screenshot the live Streamlit console; raises on any failure so the caller can fall back."""
    from playwright.sync_api import sync_playwright

    frames: list[tuple[Image.Image, int]] = []
    shots = FRAMES / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.goto(UI, wait_until="networkidle", timeout=60_000)
        page.wait_for_selector("text=Patients to run", timeout=90_000)
        sidebar = page.locator('[data-testid="stSidebar"]')

        page.get_by_label("Run id to follow").fill(run["run_id"])
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
        frames += [
            (f, 7)
            for f in _scroll_shots(
                page, "panel", "Panel: patients, latest run, and the per-node run stepper", 2
            )
        ]

        sidebar.get_by_text("Patient", exact=True).click()
        page.wait_for_selector("text=Engine verdicts", timeout=30_000)
        box = page.locator('[data-testid="stSelectbox"]').first
        box.click()
        page.keyboard.type(TONY)
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
        for name in ("Evidence", "Coverage"):
            try:
                page.get_by_text(name, exact=True).first.click(timeout=3000)
            except Exception:
                log.warning("could not expand %s", name)
        page.wait_for_timeout(1500)
        frames += [
            (f, 7)
            for f in _scroll_shots(
                page,
                "patient",
                f"Patient {TONY[:8]}...: verdict chips, evidence, coverage, approval card",
                6,
            )
        ]

        approve(run)
        sidebar.get_by_text("Outbox & audit", exact=True).click()
        page.wait_for_selector("text=Approval requests", timeout=30_000)
        page.wait_for_timeout(2000)
        frames += [
            (f, 7)
            for f in _scroll_shots(
                page,
                "outbox",
                "Outbox & audit: only finalize writes here, every entry carries its decision id",
                2,
            )
        ]
        browser.close()
    return frames


def assemble(frames: list[tuple[Image.Image, int]]) -> tuple[int, int]:
    FRAMES.mkdir(parents=True, exist_ok=True)
    width = WIDTH
    for attempt in range(3):
        images: list[Image.Image] = []
        durations: list[int] = []
        for i, (img, seconds) in enumerate(frames):
            resized = (
                img
                if img.width == width
                else img.resize((width, int(img.height * width / img.width)))
            )
            resized.save(FRAMES / f"{i:02d}.png")
            images.append(resized.quantize(colors=128 >> attempt, method=Image.Quantize.MEDIANCUT))
            durations.append(seconds * 1000)
        images[0].save(
            OUT, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True
        )
        size = OUT.stat().st_size
        log.info(
            "gif attempt %d: %d frames, %d bytes at width %d", attempt, len(images), size, width
        )
        if size <= MAX_BYTES:
            break
        width = int(width * 0.85)
    return len(frames), OUT.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fallback", action="store_true", help="skip Playwright; draw text cards")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if shutil.rmtree(FRAMES, ignore_errors=True) is None:
        FRAMES.mkdir(parents=True, exist_ok=True)
    for name in ("demo-checkpoints.sqlite", "demo-caregap.sqlite"):
        (REPO / "data" / name).unlink(missing_ok=True)
    api = ui = None
    try:
        api = _spawn([UV, "run", "caregap", "serve", "--host", "127.0.0.1", "--port", "8010"])
        _wait_http(f"{API}/healthz", 120, "API")
        run = drive_run()
        frames: list[tuple[Image.Image, int]] = [
            (
                _title_card(
                    "hedis-care-gap-agent",
                    [
                        "LangGraph care-gap closure over synthetic Synthea personas.",
                        "Deterministic HEDIS-aligned engine; LLM validator and drafter kept "
                        "inside what code can verify; a checkpointed interrupt puts a human "
                        "decision in front of the outbox.",
                        f"This run: patient {TONY[:8]}... at as_of {AS_OF}, "
                        "CAREGAP_MODELS=fake (no key).",
                    ],
                    footer="Every verdict shown is the committed persona golden under fake models.",
                ),
                6,
            )
        ]
        mode = "fallback"
        if not args.fallback:
            try:
                ui = _spawn([UV, "run", "caregap", "ui"])
                _wait_http(f"{UI}/_stcore/health", 120, "UI")
                frames += capture_playwright(run)
                mode = "playwright"
            except Exception:
                log.exception("Playwright capture failed; falling back to text cards")
                frames = frames[:1]
        if mode == "fallback":
            approve(run)
            outbox = _get(f"{API}/v1/outbox?run_id={run['run_id']}")
            frames += storyboard_cards(run, outbox)
        frames.append((_readme_eval_card(), 7))
        frames.append(
            (
                _title_card(
                    "demo-grade; HEDIS-aligned; not NCQA-certified",
                    [
                        "Synthetic data only: every patient is a Synthea persona; no real or "
                        "de-identified clinical data anywhere.",
                        "Rules are public-source approximations with tagged demo choices, "
                        "not NCQA specifications.",
                        "Regenerate: uv run --with playwright --with pillow "
                        "python docs/make_gif.py",
                    ],
                    footer="github.com/thiagobandeira1/hedis-care-gap-agent",
                ),
                7,
            )
        )
        count, size = assemble(frames)
        log.info("mode=%s frames=%d bytes=%d -> %s", mode, count, size, OUT)
        print(json.dumps({"mode": mode, "frames": count, "bytes": size, "gif": str(OUT)}))
        return 0
    finally:
        _kill(ui)
        _kill(api)


if __name__ == "__main__":
    sys.exit(main())
