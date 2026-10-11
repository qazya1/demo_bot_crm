# qr_utils.py
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional

import qrcode
from qrcode.constants import ERROR_CORRECT_H
from PIL import Image


@dataclass(frozen=True)
class QROptions:
    box_size: int = 10
    border: int = 3
    error_correction: int = ERROR_CORRECT_H
    resize_to: int = 512


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL пустой")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("URL должен начинаться с http:// или https://")
    return url


def _hash_name(url: str, *, namespace: str = "", ext: str = ".png") -> str:
    """
    Стабильное имя файла по ссылке.
    namespace можно использовать как "referrals", "payments", etc.
    """
    base = f"{namespace}|{url}" if namespace else url
    h = hashlib.md5(base.encode("utf-8")).hexdigest()
    return f"{h}{ext}"


def _load_logo(logo_path: str) -> Image.Image:
    if not os.path.exists(logo_path):
        raise FileNotFoundError(f"Логотип не найден: {logo_path}")
    return Image.open(logo_path).convert("RGBA")


def _paste_logo_center(
    qr_img_rgb: Image.Image,
    logo_rgba: Image.Image,
    *,
    logo_scale: float = 0.22,
    add_white_pad: bool = True,
    pad_scale: float = 1.18,
) -> Image.Image:
    qr = qr_img_rgb.convert("RGBA")
    w, h = qr.size
    target = max(1, int(w * float(logo_scale)))

    logo = logo_rgba.copy()
    logo.thumbnail((target, target), resample=Image.LANCZOS)

    lw, lh = logo.size
    x = (w - lw) // 2
    y = (h - lh) // 2

    if add_white_pad:
        pad_w = int(lw * pad_scale)
        pad_h = int(lh * pad_scale)
        pad_x = (w - pad_w) // 2
        pad_y = (h - pad_h) // 2
        pad = Image.new("RGBA", (pad_w, pad_h), (255, 255, 255, 255))
        qr.alpha_composite(pad, (pad_x, pad_y))

    qr.alpha_composite(logo, (x, y))
    return qr.convert("RGB")


def generate_qr_file_hashed(
    url: str,
    *,
    out_dir: str = "images/qrs",
    namespace: str = "",
    logo_path: Optional[str] = None,
    options: Optional[QROptions] = None,
    logo_scale: float = 0.22,
    overwrite: bool = False,
) -> str:
    """
    Генерирует (или возвращает существующий) QR PNG-файл.
    Имя файла = sha256(url) (+ namespace).

    Возвращает полный путь: out_dir/<hash>.png
    """
    url = _validate_url(url)
    filename = _hash_name(url, namespace=namespace, ext=".png")
    out_path = os.path.join(out_dir, filename)

    if not overwrite and os.path.exists(out_path):
        return out_path

    opt = options or QROptions()

    qr = qrcode.QRCode(
        version=None,
        error_correction=opt.error_correction,
        box_size=opt.box_size,
        border=opt.border,
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    if opt.resize_to:
        img = img.resize((opt.resize_to, opt.resize_to), resample=Image.NEAREST)

    if logo_path:
        logo = _load_logo(logo_path)
        img = _paste_logo_center(img, logo, logo_scale=logo_scale, add_white_pad=True)

    os.makedirs(out_dir, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return out_path