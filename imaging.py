"""Image decoding/encoding for the sidecar capture loop.

MuMuPlayer's raw screencap format is non-standard (not plain RGBA).
This module handles:
    - Decoding PNG screencaps to BGR numpy arrays (for OpenCV processing)
    - Encoding BGR arrays back to JPEG (for the capture loop's jpeg mode)
"""

from __future__ import annotations

import struct
from typing import Any

import numpy as np


def decode_screencap_raw(png_bytes: bytes) -> np.ndarray:
    """Decode a PNG screencap from adb into a BGR numpy array.

    MuMuPlayer's screencap returns a standard PNG file. This decodes it
    to BGR (OpenCV's native format) for downstream processing.

    Parameters
    ----------
    png_bytes :
        Raw PNG bytes from ``adb exec-out screencap -p``.

    Returns
    -------
    BGR image array of shape (H, W, 3), dtype uint8.
    """
    try:
        import cv2
    except ImportError:
        raise ImportError("cv2 (opencv-python) is required for image decoding")

    # Decode PNG to BGR
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("Failed to decode PNG screencap")
    return bgr


def encode_jpeg(bgr: np.ndarray, quality: int = 80) -> bytes:
    """Encode a BGR numpy array as JPEG bytes.

    Parameters
    ----------
    bgr :
        BGR image array of shape (H, W, 3), dtype uint8.
    quality :
        JPEG quality (1–100).

    Returns
    -------
    JPEG-encoded bytes.
    """
    try:
        import cv2
    except ImportError:
        raise ImportError("cv2 (opencv-python) is required for image encoding")

    success, encoded = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not success:
        raise ValueError("Failed to encode JPEG")
    return encoded.tobytes()


def decode_jpeg(jpeg_bytes: bytes) -> np.ndarray:
    """Decode JPEG bytes to a BGR numpy array.

    Used by the sidecar client and mapping code to convert frame responses
    to OpenCV-compatible images.

    Parameters
    ----------
    jpeg_bytes :
        JPEG-encoded bytes.

    Returns
    -------
    BGR image array of shape (H, W, 3), dtype uint8.
    """
    try:
        import cv2
    except ImportError:
        raise ImportError("cv2 (opencv-python) is required for image decoding")

    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("Failed to decode JPEG frame")
    return bgr


def grayscale(frame: np.ndarray) -> np.ndarray:
    """Convert a BGR frame to single-channel grayscale.

    Parameters
    ----------
    frame :
        BGR image array (H, W, 3).

    Returns
    -------
    Grayscale array of shape (H, W), dtype uint8.
    """
    try:
        import cv2
    except ImportError:
        raise ImportError("cv2 (opencv-python) is required for image processing")

    if len(frame.shape) == 3 and frame.shape[2] == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame.copy()


def resize(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Resize a frame to the specified dimensions.

    Parameters
    ----------
    frame :
        Input image array.
    width, height :
        Target dimensions.

    Returns
    -------
    Resized image array.
    """
    try:
        import cv2
    except ImportError:
        raise ImportError("cv2 (opencv-python) is required for image processing")

    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
