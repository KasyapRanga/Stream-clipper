"""Automatic facecam detection.

Finds the streamer's webcam overlay in a clip:
1. Sample frames and detect faces (OpenCV, bundled models, no downloads).
2. Keep the face that stays in the same place across frames (the webcam; faces inside the
   game move around or vanish).
3. Average edge maps across the frames: gameplay edges blur away, the overlay's fixed border
   stays. Scan outward from the face to find that border. If none is found, estimate the
   webcam box from the face size.

Returns a dict in source-pixel coordinates:
  {"kind": "overlay" | "fullcam", "cam": [x, y, w, h], "face": [x, y, w, h], "src": [W, H], ...}
or None when no stable face is present (pure gameplay).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .util import log, read_json, write_json

ANALYSIS_W = 1280  # detection width: small streamer webcams have faces of ~40-80 px at 1080p


def _sample_frames(video: Path, n: int):
    import cv2
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = []
    idxs = np.linspace(total * 0.03, max(0, total * 0.97 - 1), n).astype(int) if total else range(n)
    for i in idxs:
        if total:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, frame = cap.read()
        if not ok:
            continue
        scale = ANALYSIS_W / frame.shape[1]
        small = cv2.resize(frame, (ANALYSIS_W, int(round(frame.shape[0] * scale))))
        frames.append(small)
    cap.release()
    return frames, src_w, src_h


YUNET_URLS = [
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx",
    "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
]
_DETECTOR = None


def _yunet(cfg: dict):
    """OpenCV's YuNet DNN face detector (better with headsets, side angles, dim rooms).
    The ~230 KB model is downloaded once into models/. Returns None if unavailable."""
    import cv2
    from .util import ROOT
    if not hasattr(cv2, "FaceDetectorYN"):
        return None
    path = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
    if not path.exists():
        import requests
        for url in YUNET_URLS:
            try:
                r = requests.get(url, timeout=30)
                if r.status_code == 200 and len(r.content) > 100_000 and r.content[:1] != b"v":
                    path.parent.mkdir(exist_ok=True)
                    path.write_bytes(r.content)
                    break
            except Exception:
                continue
        else:
            log("   face detector: could not download YuNet (weaker built-in detector will be used). "
                f"You can save it manually to {path} from {YUNET_URLS[0]}")
            return None
    try:
        return cv2.FaceDetectorYN.create(str(path), "", (320, 320), 0.6, 0.3, 50)
    except Exception:
        return None


def _detector(cfg: dict):
    global _DETECTOR
    if _DETECTOR is None:
        import cv2
        yn = _yunet(cfg) if cfg["render"]["facecam"].get("detector", "auto") != "haar" else None
        if yn is not None:
            _DETECTOR = ("yunet", yn)
        else:
            base = cv2.data.haarcascades
            cascades = [cv2.CascadeClassifier(base + n) for n in
                        ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml")]
            if any(c.empty() for c in cascades):
                raise ImportError("OpenCV face models missing (use opencv-python-headless 4.x)")
            _DETECTOR = ("haar", cascades)
        log(f"   face detector: {_DETECTOR[0]}")
    return _DETECTOR


def _detect_faces(frame, cfg: dict) -> list[tuple[int, int, int, int]]:
    import cv2
    kind, det = _detector(cfg)
    if kind == "yunet":
        h, w = frame.shape[:2]
        det.setInputSize((w, h))
        _, faces = det.detect(frame)
        return [tuple(int(v) for v in f[:4]) for f in (faces if faces is not None else [])]
    eq = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    found = []
    for i, c in enumerate(det):
        boxes = c.detectMultiScale(eq, scaleFactor=1.08, minNeighbors=5 if i == 0 else 7,
                                   minSize=(20, 20))
        found += [tuple(int(v) for v in b) for b in boxes]
        if i == 0 and len(found):
            break  # frontal hit is enough; profile is a fallback
    return found


def _stable_face(per_frame: list[list[tuple]], frame_w: int):
    """Cluster face centres across frames; return the median box of the most persistent one."""
    pts = [(fi, b) for fi, boxes in enumerate(per_frame) for b in boxes]
    if not pts:
        return None, 0
    tol = frame_w * 0.06
    clusters: list[list[tuple]] = []
    for fi, b in pts:
        cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
        for cl in clusters:
            mx = np.median([x[0] + x[2] / 2 for _, x in cl])
            my = np.median([x[1] + x[3] / 2 for _, x in cl])
            if abs(cx - mx) < tol and abs(cy - my) < tol:
                cl.append((fi, b))
                break
        else:
            clusters.append([(fi, b)])
    best = max(clusters, key=lambda cl: len({fi for fi, _ in cl}))
    frames_hit = len({fi for fi, _ in best})
    box = np.median(np.array([b for _, b in best], dtype=float), axis=0)
    return box, frames_hit


def _side_candidates(strength, lo: int, hi: int, frame_edge: int | None) -> list[tuple[int, float]]:
    """Positions in [lo, hi) whose edge strength is a local peak, plus the frame edge if in reach."""
    lo, hi = max(lo, 1), min(hi, len(strength) - 1)
    c = [(i, float(strength[i])) for i in range(lo, hi)
         if strength[i] > 0.3 and strength[i] >= strength[i - 1] and strength[i] >= strength[i + 1]]
    c = sorted(c, key=lambda t: -t[1])[:7]
    if frame_edge is not None:
        c.append((frame_edge, -1.0))  # -1 marks "frame edge"
    return c


FRAME_EDGE_SCORE = 0.5


def _find_rectangle(E: np.ndarray, face, reach: float):
    """Best rectangle around the face whose whole perimeter sits on persistent edges."""
    import cv2
    H, W = E.shape
    Ed = cv2.dilate(E.astype(np.float32), np.ones((3, 3), np.uint8))
    colc = np.vstack([np.zeros((1, W)), np.cumsum(Ed, axis=0)])
    rowc = np.hstack([np.zeros((H, 1)), np.cumsum(Ed, axis=1)])
    fx, fy, fw, fh = face
    ys0, ys1 = int(max(0, fy - 0.5 * fh)), int(min(H, fy + 1.5 * fh))
    xs0, xs1 = int(max(0, fx - 0.5 * fw)), int(min(W, fx + 1.5 * fw))
    col_s = (colc[ys1] - colc[ys0]) / max(1, ys1 - ys0)
    row_s = (rowc[:, xs1] - rowc[:, xs0]) / max(1, xs1 - xs0)
    rx, ry = reach * fw, reach * fh
    # Borders can sit right next to the face (streamer low in frame, chin near the bottom edge).
    L = _side_candidates(col_s, int(fx - rx), int(fx - 0.05 * fw), 0 if fx - rx <= 0 else None)
    R = _side_candidates(col_s, int(fx + 1.05 * fw), int(fx + fw + rx), W - 1 if fx + fw + rx >= W - 1 else None)
    T = _side_candidates(row_s, int(fy - ry), int(fy - 0.05 * fh), 0 if fy - ry <= 0 else None)
    B = _side_candidates(row_s, int(fy + 1.0 * fh), int(fy + fh + ry), H - 1 if fy + fh + ry >= H - 1 else None)

    def side(pos, a, b, vertical, tag):
        if tag < 0:
            return float(FRAME_EDGE_SCORE)  # a webcam pushed into a corner uses the frame edge as its border
        if b <= a:
            return 0.0
        return ((colc[b, pos] - colc[a, pos]) if vertical else (rowc[pos, b] - rowc[pos, a])) / (b - a)

    best, best_score = None, 0.0
    for l, lt in L:
        for r, rt in R:
            for t, tt in T:
                for b, bt in B:
                    w, h = r - l, b - t
                    if h <= 0 or not (0.9 <= w / h <= 2.4) or w * h > 0.45 * W * H:
                        continue
                    sc = (side(l, t, b, True, lt) + side(r, t, b, True, rt)
                          + side(t, l, r, False, tt) + side(b, l, r, False, bt)) / 4
                    # near-ties go to the larger box: an inner edge (a door frame, the chair) can
                    # look as strong as the real webcam border
                    if sc > best_score + 0.03 or (sc > best_score - 0.03 and best and w * h > (best[2] - best[0]) * (best[3] - best[1])):
                        if sc > best_score - 0.03:
                            best, best_score = (l, t, r, b), max(sc, best_score) if best else sc
    return (best, best_score) if best_score >= 0.5 else (None, best_score)


def detect(video: Path, cfg: dict) -> dict | None:
    import cv2
    fc = cfg["render"]["facecam"]
    frames, src_w, src_h = _sample_frames(video, int(fc.get("samples", 24)))
    if len(frames) < 4:
        return None
    H, W = frames[0].shape[:2]
    per_frame = [_detect_faces(f, cfg) for f in frames]
    face, hits = _stable_face(per_frame, W)
    if face is None or hits < max(3, int(len(frames) * float(fc.get("min_presence", 0.3)))):
        return None
    fx, fy, fw, fh = face
    s = src_w / W  # back to source pixels

    # Big face = the whole frame is the camera (just-chatting segment).
    if fw > W * 0.13:
        return {"kind": "fullcam", "face": [round(v * s) for v in face], "cam": [0, 0, src_w, src_h],
                "src": [src_w, src_h], "presence": hits / len(frames)}

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    E = np.mean([cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 60, 160) / 255.0 for g in grays], axis=0)
    # Motion cue: gameplay changes between frames, a webcam's room mostly doesn't, so the
    # per-pixel change map has a step at the webcam border even where colours are similar.
    motion = np.std(np.stack(grays).astype(np.float32), axis=0)
    motion = cv2.GaussianBlur(motion, (5, 5), 0)
    gm = np.hypot(cv2.Sobel(motion, cv2.CV_32F, 1, 0, ksize=3), cv2.Sobel(motion, cv2.CV_32F, 0, 1, ksize=3))
    gm = np.clip(gm / (np.percentile(gm, 99.5) + 1e-6), 0, 1)
    E = np.maximum(E, gm)
    rect, score = _find_rectangle(E, face, float(fc.get("max_reach", 4.0)))
    if rect:
        left, top, right, bottom = rect
        left, top = left + 1, top + 1  # step inside the border line
        right, bottom = right - 1, bottom - 1
        found_border = True
    else:
        # No clear frame: estimate a typical 4:3 webcam around the face, kept inside the frame.
        cw_, ch_ = min(fw * 3.2, W), min(fw * 3.2 * 0.75, H)
        left = min(max(0.0, fx + fw / 2 - cw_ / 2), W - cw_)
        top = min(max(0.0, fy + fh * 0.6 - ch_ / 2), H - ch_)
        right, bottom = left + cw_, top + ch_
        found_border = False
    cw, ch = right - left, bottom - top
    # A camera that fills most of the frame is a full-screen camera segment.
    if not found_border and cw * ch > W * H * 0.35:
        return {"kind": "fullcam", "face": [round(v * s) for v in face], "cam": [0, 0, src_w, src_h],
                "src": [src_w, src_h], "presence": round(hits / len(frames), 2)}

    return {
        "kind": "overlay",
        "cam": [round(left * s), round(top * s), round(cw * s), round(ch * s)],
        "face": [round(v * s) for v in face],
        "src": [src_w, src_h],
        "presence": round(hits / len(frames), 2),
        "border_found": found_border,
    }


CACHE_VERSION = 3  # bumped when detection improves, so old boxes are re-detected


def detect_cached(video: Path, out_json: Path, cfg: dict) -> dict | None:
    if out_json.exists():
        data = read_json(out_json)
        if data.get("v") == CACHE_VERSION:
            return data.get("result")
    try:
        res = detect(video, cfg)
    except ImportError as e:
        log(f"   facecam detection skipped: {e}")
        res = None
    if res:
        res["cache_v"] = CACHE_VERSION
    write_json(out_json, {"v": CACHE_VERSION, "result": res})
    if res:
        x, y, w, h = res["cam"]
        log(f"   facecam: {res['kind']} at x={x} y={y} {w}x{h} (face seen in {res['presence']:.0%} of frames)")
    else:
        log("   facecam: none detected in this clip")
    return res


def manual(cfg: dict, src_w: int, src_h: int) -> dict:
    f = cfg["render"]["facecam"]
    cam = [round(src_w * f["x"]), round(src_h * f["y"]), round(src_w * f["w"]), round(src_h * f["h"])]
    return {"kind": "overlay", "cam": cam, "face": None, "src": [src_w, src_h], "manual": True}


def preview(video: Path, cfg: dict, out_png: Path) -> dict | None:
    """Detect the webcam in a video and save a frame with the box drawn, for checking."""
    import cv2
    res = detect(video, cfg)
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
    ok, frame = cap.read()
    cap.release()
    if ok:
        if res:
            x, y, w, h = res["cam"]
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 4)
            if res.get("face"):
                fx, fy, fw, fh = res["face"]
                cv2.rectangle(frame, (fx, fy), (fx + fw, fy + fh), (0, 200, 255), 3)
        cv2.imwrite(str(out_png), frame)
    return res


# ------------------------------------------------------------------------------------ face tracking
def track_face(video: Path, region: list[float], cfg: dict, out_json: Path | None = None,
               step: float = 0.5) -> list[dict]:
    """Follow the face inside `region` (the webcam box, or the whole frame) through the clip.

    Returns keyframes [{t, cx, cy, fh}] in source pixels, smoothed: a median filter removes detector
    jitter and a dead-zone keeps the framing still until the streamer really moves (leaning in,
    sitting back), so the camera follows without wobbling."""
    import cv2
    if out_json and out_json.exists():
        try:
            data = read_json(out_json)
            if data.get("v") == 2 and data.get("region") == [round(v) for v in region]:
                return data["track"]
        except Exception:
            pass
    rx, ry, rw, rh = [int(round(v)) for v in region]
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    every = max(1, int(round(fps * step)))
    raw: list[tuple[float, float, float, float] | None] = []
    times: list[float] = []
    prev = None
    i = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % every == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            crop = frame[max(0, ry): ry + rh, max(0, rx): rx + rw]
            hit = None
            if crop.size:
                scale = max(1.0, 640 / crop.shape[1])          # small webcams: enlarge so faces are findable
                img = cv2.resize(crop, None, fx=scale, fy=scale) if scale > 1 else crop
                faces = _detect_faces(img, cfg)
                if faces:
                    cands = [((x + w / 2) / scale + rx, (y + h * 0.45) / scale + ry, h / scale)
                             for x, y, w, h in faces]
                    hit = (min(cands, key=lambda c: (c[0] - prev[0]) ** 2 + (c[1] - prev[1]) ** 2)
                           if prev else max(cands, key=lambda c: c[2]))
                    prev = hit
            raw.append(hit)
            times.append(i / fps)
        i += 1
        if total and i >= total:
            break
    cap.release()
    found = [r for r in raw if r]
    if len(found) < max(2, 0.25 * len(raw)):
        track: list[dict] = []
    else:
        # fill gaps with the nearest detection
        filled, last = [], found[0]
        for r in raw:
            last = r or last
            filled.append(last)
        arr = np.array(filled, dtype=float)
        k = 5  # median over ~2.5 s
        med = np.array([np.median(arr[max(0, j - k // 2): j + k // 2 + 1], axis=0) for j in range(len(arr))])
        fh_med = float(np.median(med[:, 2]))
        dead = float(cfg["render"]["facecam"].get("track_deadzone", 0.06)) * rw
        hold = med[0].copy()
        out = []
        for t, m in zip(times, med):
            if abs(m[0] - hold[0]) > dead or abs(m[1] - hold[1]) > dead * 0.8:
                hold = hold + (m - hold) * 0.5   # ease toward the new position
            out.append({"t": round(t, 2), "cx": round(float(hold[0]), 1), "cy": round(float(hold[1]), 1),
                        "fh": round(fh_med, 1)})
        track = out
    if out_json:
        write_json(out_json, {"v": 2, "region": [round(v) for v in region], "track": track})
    return track


def crop_window(track: list[dict], box: list[float], aspect: float, out_w: int, cfg: dict,
                fallback_center: tuple[float, float] | None) -> tuple[int, int, str, str]:
    """Crop size from the face size (face fills ~`face_height_share` of the panel), and x/y
    expressions that follow the tracked face over time (piecewise linear between keyframes)."""
    fc = cfg["render"]["facecam"]
    bx, by, bw, bh = box
    # largest crop of this aspect inside the box
    max_w, max_h = (bh * aspect, bh) if bw / bh > aspect else (bw, bw / aspect)
    if track:
        fh = track[0]["fh"]
        want_h = fh / float(fc.get("face_height_share", 0.42))
        min_h = max(max_h / float(fc.get("max_zoom", 2.2)), out_w / aspect / float(fc.get("max_upscale", 4.5)))
        h = min(max_h, max(min_h, want_h))
        # If the face sits near the webcam's edge (e.g. low in frame), zoom in a little more so it can
        # still be placed at `face_y`, but never past the upscale limit (that would only blur it).
        head_ = float(fc.get("face_y", 0.42))
        cys = sorted(k["cy"] for k in track)
        cy_med = cys[len(cys) // 2]
        fit = min((by + bh - cy_med) / max(0.05, 1 - head_), (cy_med - by) / max(0.05, head_))
        if fit < h:
            h = max(min_h, fit)
    else:
        h = max_h / float(fc.get("zoom", 1.0))
    w = h * aspect
    w, h = int(w) // 2 * 2, int(h) // 2 * 2
    head = float(fc.get("face_y", 0.42))  # face centre this far down the panel (room above the head)

    def clampx(cx):
        return min(max(cx - w / 2, bx), bx + bw - w)

    def clampy(cy):
        return min(max(cy - h * head, by), by + bh - h)

    if not track:
        cx, cy = fallback_center or (bx + bw / 2, by + bh / 2)
        return w, h, str(int(clampx(cx))), str(int(clampy(cy)))
    keys = track[:: max(1, len(track) // 90)]  # keep the expression short (<= ~90 keyframes)
    xs = [clampx(k["cx"]) for k in keys]
    ys = [clampy(k["cy"]) for k in keys]
    ts = [k["t"] for k in keys]

    def expr(vals):
        parts = [f"{vals[0]:.1f}"]
        for j in range(1, len(vals)):
            d = vals[j] - vals[j - 1]
            if abs(d) < 0.5:
                continue
            t0, t1 = ts[j - 1], ts[j]
            parts.append(f"{d:+.1f}*clip((t-{t0:.2f})/{max(0.05, t1 - t0):.2f},0,1)")
        return "".join(parts)
    return w, h, expr(xs), expr(ys)
