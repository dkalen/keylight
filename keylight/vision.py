"""Face and background measurement from a frame (needs numpy + opencv).

Everything is measured in linear light (sRGB decoded), so ratios are in stops.
Imported lazily: the rest of keylight works without these dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

from .auto import Measurement, srgb_to_linear
from .core import AutoConfig

MODEL_NAME = "face_detection_yunet_2023mar.onnx"


def default_model_path() -> Path:
    """Beside the package (installed: <lib>/models; checkout: <repo>/models),
    else the installer's default location."""
    candidates = [Path(__file__).resolve().parent.parent / "models" / MODEL_NAME,
                  Path(os.environ.get("KEYLIGHT_PREFIX", "~/.local")).expanduser()
                  / "share" / "keylight" / "models" / MODEL_NAME]
    return next((c for c in candidates if c.exists()), candidates[0])


class Vision:
    def __init__(self, cfg: AutoConfig):
        self.cfg = cfg
        model = Path(cfg.model).expanduser() if cfg.model else default_model_path()
        if not model.exists():
            raise FileNotFoundError(f"face model not found: {model} (re-run the installer with --with-auto)")
        try:   # OpenCV 5 warns about DNN targets on every load; it's noise in the journal
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        except AttributeError:
            pass
        self.detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), cfg.min_face_score, 0.3, 50)
        lut = np.array([srgb_to_linear(i / 255) for i in range(256)], dtype=np.float32)
        self.lut = lut

    def decode(self, jpeg: bytes) -> np.ndarray:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("could not decode frame")
        return img

    def linear_luma(self, img: np.ndarray) -> np.ndarray:
        lin = self.lut[img]                                   # BGR, linear
        return lin[..., 0] * 0.0722 + lin[..., 1] * 0.7152 + lin[..., 2] * 0.2126

    def key_side_box(self, face: tuple[float, float, float, float], width: int) -> tuple[int, int, int, int]:
        """Inner 60% of the face box, then the half on the Light's side."""
        x, y, w, h = face
        ix, iy, iw, ih = x + 0.2 * w, y + 0.2 * h, 0.6 * w, 0.6 * h
        # Unmirrored, the subject's left appears on the image's right.
        image_right = (self.cfg.key_side == "left") != self.cfg.mirrored
        if image_right:
            ix += iw / 2
        iw /= 2
        x0, y0 = max(0, int(ix)), max(0, int(iy))
        return x0, y0, max(1, min(width - x0, int(iw))), max(1, int(ih))

    def measure(self, img: np.ndarray) -> Measurement:
        h, w = img.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(img)
        luma = self.linear_luma(img)
        bg = self._background(luma, None)
        if faces is None or len(faces) == 0:
            return Measurement(face_lin=None, bg_lin=bg)
        best = max(faces, key=lambda f: f[2] * f[3])
        face = tuple(float(v) for v in best[:4])
        bx, by, bw, bh = self.key_side_box(face, w)
        region = luma[by:by + bh, bx:bx + bw]
        face_lin = float(region.mean()) if region.size else None
        return Measurement(face_lin=face_lin, bg_lin=self._background(luma, face),
                           face_box=face, key_box=(bx, by, bw, bh), score=float(best[-1]))

    def _background(self, luma: np.ndarray, face) -> float | None:
        h, w = luma.shape
        roi = self.cfg.background_roi
        if roi:
            x, y, rw, rh = int(roi[0] * w), int(roi[1] * h), max(1, int(roi[2] * w)), max(1, int(roi[3] * h))
            region = luma[y:y + rh, x:x + rw]
            return float(region.mean()) if region.size else None
        if face is None:
            return None
        mask = np.ones_like(luma, dtype=bool)
        fx, fy, fw, fh = face
        # exclude face, hair and shoulders: a box twice the face size, extended down
        x0, x1 = int(max(0, fx - fw / 2)), int(min(w, fx + fw * 1.5))
        mask[int(max(0, fy - fh / 2)):, x0:x1] = False
        values = luma[mask]
        return float(np.percentile(values, 90)) if values.size else None

    def annotate(self, img: np.ndarray, m: Measurement, path: Path) -> None:
        out = img.copy()
        h, w = out.shape[:2]
        if m.face_box:
            x, y, fw, fh = (int(v) for v in m.face_box)
            cv2.rectangle(out, (x, y), (x + fw, y + fh), (0, 255, 0), 1)
        if m.key_box:
            x, y, kw, kh = m.key_box
            cv2.rectangle(out, (x, y), (x + kw, y + kh), (0, 200, 255), 2)
        roi = self.cfg.background_roi
        if roi:
            cv2.rectangle(out, (int(roi[0] * w), int(roi[1] * h)),
                          (int((roi[0] + roi[2]) * w), int((roi[1] + roi[3]) * h)), (255, 128, 0), 2)
        # 10% grid with labels, to read off background_roi coordinates
        for i in range(1, 10):
            cv2.line(out, (int(w * i / 10), 0), (int(w * i / 10), h), (90, 90, 90), 1)
            cv2.line(out, (0, int(h * i / 10)), (w, int(h * i / 10)), (90, 90, 90), 1)
            cv2.putText(out, f".{i}", (int(w * i / 10) + 2, 12), cv2.FONT_HERSHEY_PLAIN, 0.8, (255, 255, 255), 1)
            cv2.putText(out, f".{i}", (2, int(h * i / 10) - 2), cv2.FONT_HERSHEY_PLAIN, 0.8, (255, 255, 255), 1)
        cv2.imwrite(str(path), out)
