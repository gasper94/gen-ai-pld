"""Shared helpers for the laydown tools.

Everything here is deterministic and cheap. The billed work is in generate.py.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"
RUNS = ROOT / "runs"

# nano-banana-pro/edit at 4K is $0.30/image; 1K and 2K are both $0.15. Published
# rates - fal exposes no billing API, so a reported cost is never a receipt.
ENDPOINT = "fal-ai/nano-banana-pro/edit"
PRICE_4K = 0.30


def session_run_dir() -> Path:
    """The one run folder for this session.

    run.sh stamps LAYDOWN_SESSION once and exports it, so every call resolves to
    the same folder. That is what makes the image budget hold: the cap counts
    images in a folder, so a second folder would be a second budget, and the
    agent creates folders by calling prepare.py.
    """
    sid = os.environ.get("LAYDOWN_SESSION")
    return RUNS / (sid or time.strftime("%Y%m%d_%H%M%S"))


def fix_ca_bundle() -> None:
    """certifi's Mozilla-only bundle rejects the corporate proxy's certificate;
    the Homebrew OpenSSL store includes it."""
    bundle = "/opt/homebrew/etc/openssl@3/cert.pem"
    if os.path.exists(bundle):
        os.environ.setdefault("SSL_CERT_FILE", bundle)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)


def load_fal_key() -> str:
    key = os.environ.get("FAL_KEY")
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        m = re.search(r'^\s*FAL_KEY\s*=\s*["\']?([^"\'\s]+)', env.read_text(), re.M)
        if m:
            os.environ["FAL_KEY"] = m.group(1)
            return m.group(1)
    sys.exit("FAL_KEY not set and none found in .env")


def log(run_dir: Path, what: str, cents: float = 0.0) -> str:
    """Append one line to <run>/steps.log and echo it.

    The running total is read back off the last line, so separate processes keep
    one continuous tally per run folder.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    p = run_dir / "steps.log"
    n, total = 0, 0.0
    if p.exists():
        for line in p.read_text().splitlines():
            m = re.search(r"total\s+([0-9.]+)c\s*$", line)
            if m:
                n, total = n + 1, float(m.group(1))
    total += cents
    line = (f"[{n + 1}] {time.strftime('%H:%M:%S')}  {what:<46} "
            f"{cents:6.1f}c   total {total:.1f}c")
    with p.open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)
    return line


def garment_mask(path: Path, long_side: int = 1024):
    """Boolean mask of the garment, largest connected blob only.

    The plate is light and the garment darker, so a threshold below the border
    median separates them. Two details matter and both were paid for on this
    project: the plate is NOT white (it sweeps 228-252), so the threshold is
    taken from the image's own border rather than assumed; and without the
    largest-blob step a single speck of dust moves the bbox by 80% while every
    aggregate number stays plausible.

    Returns (mask, working_size). Downscaled for speed - every metric here is a
    shape statistic, and none of them changes under a clean 1024px resample.
    """
    im = Image.open(path).convert("L")
    im.thumbnail((long_side, long_side), Image.LANCZOS)
    a = np.asarray(im, dtype=np.float32)

    b = 8
    border = np.concatenate([a[:b].ravel(), a[-b:].ravel(),
                             a[:, :b].ravel(), a[:, -b:].ravel()])
    plate = float(np.median(border))
    mask = a < plate - 18.0

    mask = ndimage.binary_opening(mask, np.ones((3, 3)))
    mask = ndimage.binary_fill_holes(mask)
    lab, n = ndimage.label(mask)
    if n == 0:
        return np.zeros_like(mask, dtype=bool), a.shape
    sizes = ndimage.sum(mask, lab, range(1, n + 1))
    keep = int(np.argmax(sizes)) + 1
    return lab == keep, a.shape


def speck_count(path: Path, min_px: int = 24) -> int:
    """Blobs other than the garment, above min_px. Background dirt and hairlines."""
    im = Image.open(path).convert("L")
    im.thumbnail((1024, 1024), Image.LANCZOS)
    a = np.asarray(im, dtype=np.float32)
    b = 8
    border = np.concatenate([a[:b].ravel(), a[-b:].ravel(),
                             a[:, :b].ravel(), a[:, -b:].ravel()])
    mask = a < float(np.median(border)) - 18.0
    mask = ndimage.binary_opening(mask, np.ones((2, 2)))
    lab, n = ndimage.label(mask)
    if n <= 1:
        return 0
    sizes = ndimage.sum(mask, lab, range(1, n + 1))
    biggest = np.max(sizes)
    return int(((sizes >= min_px) & (sizes < biggest)).sum())


def rigid_dims(mask: np.ndarray) -> dict:
    """Dimensions that survive a legitimate re-lay of the garment.

    Overall bbox width is NOT one of them: closing a pair of splayed legs, or
    folding straps in, narrows the bbox on purpose. This project's own source
    measures 60.7% of frame width against a library range of 40.3-52.4%, so
    penalising that narrowing would penalise the fix.

    What genuinely cannot change is how long the garment is and how wide its top
    band is - a waistband or a bra band is rigid however the rest is arranged.
    Those two catch a garment squeezed narrower or stretched longer, which is a
    lie about the product, while letting the re-lay happen.
    """
    ys, xs = np.nonzero(mask)
    if len(xs) < 10:
        return {"length": 0.0, "top_width": 0.0, "solidity": 0.0}
    y0, y1 = ys.min(), ys.max()
    length = float(y1 - y0 + 1)

    # Top band: the widest run across the top eighth of the garment.
    band = mask[y0:y0 + max(1, int(length * 0.125))]
    per_row = band.sum(axis=1)
    top_width = float(per_row.max()) if per_row.size else 0.0

    # Solidity: how much of its own convex hull the silhouette fills. Drops when
    # limbs splay, rises when they close - a lay-quality signal, not a gate.
    area = float(mask.sum())
    try:
        from scipy.spatial import ConvexHull
        pts = np.column_stack((xs, ys))
        step = max(1, len(pts) // 4000)
        hull = float(ConvexHull(pts[::step]).volume)
        solidity = area / hull if hull else 0.0
    except Exception:
        solidity = 0.0

    return {"length": length, "top_width": top_width, "solidity": solidity}


def clipped(mask: np.ndarray, margin_px: int = 2) -> str:
    """Which frame edges the garment touches.

    The one framing fault a retoucher cannot repair: pixels that were never
    captured. Everything else about position is a transform on a layer.
    """
    H, W = mask.shape
    hit = []
    if mask[:margin_px + 1].any():
        hit.append("top")
    if mask[-(margin_px + 1):].any():
        hit.append("bottom")
    if mask[:, :margin_px + 1].any():
        hit.append("left")
    if mask[:, -(margin_px + 1):].any():
        hit.append("right")
    return ",".join(hit)


def soft_alpha(path: Path, feather: float = 1.2) -> np.ndarray:
    """Soft alpha for a cutout, at the image's own resolution.

    A binary mask cuts a hard, aliased edge that reads as a paste-up. This ramps
    alpha over the plate-to-garment transition instead, so the edge keeps its
    natural softness, then restricts to the garment blob so background specks do
    not come along.
    """
    im = Image.open(path).convert("L")
    a = np.asarray(im, dtype=np.float32)
    b = 8
    border = np.concatenate([a[:b].ravel(), a[-b:].ravel(),
                             a[:, :b].ravel(), a[:, -b:].ravel()])
    plate = float(np.median(border))

    # Ramp alpha across the FULL plate-to-garment contrast, not a fixed slice.
    # A fixed 16-level ramp captured only the top sliver of an edge that falls
    # from 247 to about 60, so genuinely half-covered pixels came out fully
    # opaque and carried plate colour into the cutout as a light fringe.
    core = ndimage.binary_erosion(a < plate - 25.0, np.ones((7, 7)))
    fg = float(np.percentile(a[core], 40)) if core.sum() > 200 else plate - 60.0
    span = max(plate - fg, 30.0)          # a pale garment needs a floor here
    alpha = np.clip((plate - a) / span, 0.0, 1.0)

    solid = alpha > 0.5
    solid = ndimage.binary_fill_holes(ndimage.binary_opening(solid, np.ones((3, 3))))
    lab, n = ndimage.label(solid)
    if n:
        sizes = ndimage.sum(solid, lab, range(1, n + 1))
        keep = lab == int(np.argmax(sizes)) + 1
        # Dilate before masking so the soft ramp outside the solid core survives.
        alpha *= ndimage.binary_dilation(keep, np.ones((9, 9)))
        # The interior must be fully opaque. Normalising the ramp against a
        # representative garment tone leaves lighter areas of the garment at
        # alpha ~0.85, which lets the background bleed through the middle of the
        # product. Only the boundary band should ever be partial.
        alpha = np.maximum(alpha, ndimage.binary_erosion(
            keep, np.ones((5, 5))).astype(np.float32))
    if feather:
        alpha = ndimage.gaussian_filter(alpha, feather)
    return np.clip(alpha, 0.0, 1.0)


def resize_mask(mask: np.ndarray, shape) -> np.ndarray:
    """Nearest-neighbour resample of a boolean mask to a common canvas."""
    src = Image.fromarray((mask * 255).astype(np.uint8))
    return np.asarray(src.resize((shape[1], shape[0]), Image.NEAREST)) > 127


def iou(a: np.ndarray, b: np.ndarray) -> float:
    u = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / u) if u else 0.0


def shape_stats(mask: np.ndarray) -> dict:
    """Tilt, centroid offset and mirror symmetry of one silhouette."""
    ys, xs = np.nonzero(mask)
    if len(xs) < 10:
        return {"tilt": 0.0, "cx": 0.0, "cy": 0.0, "symmetry": 0.0}
    H, W = mask.shape

    # Tilt: how far the silhouette's principal axis leans off the NEAREST frame
    # axis, not off vertical.
    #
    # Measuring off vertical only works for garments taller than they are wide.
    # Every bra in this project's library is wider than tall, so its major axis
    # is horizontal and the off-vertical figure reads +-88 degrees on a perfectly
    # level laydown - which would reject 100% of them at a 3-degree bar. Folding
    # to the nearest axis also makes the number stable for near-square garments
    # (several bras measure aspect 0.98-1.03), where the major axis can flip
    # between horizontal and vertical on a pixel.
    x, y = xs - xs.mean(), ys - ys.mean()
    cov = np.cov(np.vstack([x, y]))
    vals, vecs = np.linalg.eigh(cov)
    vx, vy = vecs[:, int(np.argmax(vals))]
    axis = float(np.degrees(np.arctan2(vx, vy)))
    if axis > 90:
        axis -= 180
    if axis < -90:
        axis += 180
    tilt = ((axis + 45) % 90) - 45      # lean from whichever axis is nearer

    # Symmetry: IoU against the horizontal mirror of the silhouette's own bbox,
    # so a garment that is symmetric but off-centre still scores well - centring
    # is measured separately below.
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = mask[y0:y1, x0:x1]
    sym = iou(crop, crop[:, ::-1])

    return {"tilt": tilt, "axis": axis,
            "cx": float((xs.mean() - W / 2) / W * 100),
            "cy": float((ys.mean() - H / 2) / H * 100),
            "symmetry": float(sym)}


def garment_rgb(path: Path, mask: np.ndarray) -> np.ndarray:
    """Mean RGB inside the mask, at the mask's working resolution."""
    im = Image.open(path).convert("RGB")
    im = im.resize((mask.shape[1], mask.shape[0]), Image.LANCZOS)
    a = np.asarray(im, dtype=np.float32)
    if not mask.any():
        return np.zeros(3, dtype=np.float32)
    return a[mask].mean(axis=0)


# A numeric wrinkle metric was tried and removed. Creases are broad, soft,
# oriented ridges; an isotropic band-pass at every scale from sigma 3-12 to
# 2-60 ranked the visibly SMOOTHEST candidate of a real run highest, because it
# was measuring the garment's form shading rather than its creases. Wrinkles
# stay a vision check until something is built that ranks the right way round.


def seam_energy(path: Path, mask: np.ndarray) -> float:
    """Laplacian energy well inside the silhouette.

    Run against the source's own value this reads as invented detail: a model
    that hallucinates topstitching drives it up, one that renders cloudy paper
    instead of knit drives it down. Eroded hard so the silhouette edge itself
    never contributes.
    """
    im = Image.open(path).convert("L")
    im = im.resize((mask.shape[1], mask.shape[0]), Image.LANCZOS)
    a = np.asarray(im, dtype=np.float32)
    inner = ndimage.binary_erosion(mask, np.ones((9, 9)))
    if inner.sum() < 100:
        inner = mask
    if not inner.any():
        return 0.0          # .std() of an empty slice is nan, which reads as a
                            # number in the table and is not one
    e = ndimage.laplace(ndimage.gaussian_filter(a, 1))
    return float(e[inner].std())
