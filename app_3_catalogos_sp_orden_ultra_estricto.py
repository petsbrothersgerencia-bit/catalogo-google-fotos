# -*- coding: utf-8 -*-
"""
FINAL: 3 CATÁLOGOS + SP SIN PRECIO
Autor: ChatGPT para Pets Brothers

Qué conserva esta versión:
- Tres catálogos: 4PETS BROTHERS, P3TS BROTHERS y SP.
- Carga de PDF corregida para Android: el botón Browse files no filtra por tipo antes de recibir el archivo.
- Extracción de códigos de producto desde texto seleccionable del PDF.
- Asociación de cada código con la imagen que está encima.
- Comparación por código, no solo por parecido visual.
- Reconstrucción del álbum sin cambiar el enlace compartido.
- Actualización: subir nuevos, retirar agotados y reemplazar existentes para actualizar precio/imagen.
- Mantiene orden del PDF: página -> arriba-abajo -> izquierda-derecha, usando la posición de los códigos como referencia principal.

Secrets esperados en Streamlit:
APP_PASSWORD = "tu_clave"
GOOGLE_CLIENT_ID = "..."
GOOGLE_CLIENT_SECRET = "..."
GOOGLE_REFRESH_TOKEN = "..."
ALBUM_4PETS_ID = "..."
ALBUM_4PETS_TITLE = "4PETS BROTHERS"
ALBUM_P3TS_ID = "..."
ALBUM_P3TS_TITLE = "P3TS BROTHERS"
ALBUM_SP_ID = "..."
ALBUM_SP_TITLE = "SP"
"""

import io
import re
import time
import json
import math
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Iterable

import requests
import streamlit as st
from PIL import Image, ImageOps, ImageDraw

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

APP_VERSION = "FINAL_3_CATALOGOS_SP_SIN_PRECIO_ORDEN_ULTRA_ESTRICTO_2026_06_26"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
PHOTOS_API = "https://photoslibrary.googleapis.com/v1"

# Modo ultra estricto para intentar conservar el orden visual en Google Fotos.
# Es más lento, pero reduce mezclas cuando Google procesa muchas imágenes.
GOOGLE_CREATE_DELAY_SECONDS = 1.25
GOOGLE_REBUILD_SETTLE_SECONDS = 12

CATALOGS = {
    "4PETS": {
        "label": "4PETS BROTHERS",
        "album_id_secret": "ALBUM_4PETS_ID",
        "album_title_secret": "ALBUM_4PETS_TITLE",
        "filename_prefix": "4PETS",
        "mode": "normal",
    },
    "P3TS": {
        "label": "P3TS BROTHERS",
        "album_id_secret": "ALBUM_P3TS_ID",
        "album_title_secret": "ALBUM_P3TS_TITLE",
        "filename_prefix": "P3TS",
        "mode": "normal",
    },
    "SP": {
        "label": "SP",
        "album_id_secret": "ALBUM_SP_ID",
        "album_title_secret": "ALBUM_SP_TITLE",
        "filename_prefix": "SP",
        # En SP se conserva imagen, código, descripción y QR, pero se tapa el precio.
        "mode": "hide_price",
    },
}

# Códigos como CEP32, D05055, BOL34, CAM1, GBOL1.
# Reglas: letras/números, sin espacios, entre 3 y 14 caracteres, al menos una letra y un número.
CODE_RE = re.compile(r"\b(?=[A-Z0-9]{3,14}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]+\b")
# Precio colombiano visible en el PDF, por ejemplo $6,900, $ 6.900 o 6900.
PRICE_TEXT_RE = re.compile(r"^\$?\s*\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?$|^\$\s*\d{3,}$")


@dataclass
class TextItem:
    text: str
    code: str
    rect: fitz.Rect
    page_number: int


@dataclass
class ProductCrop:
    code: str
    page_number: int
    order_on_page: int
    bbox_points: Tuple[float, float, float, float]
    image_bytes: bytes
    image_hash: str
    filename: str


def safe_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
        if value is None:
            return default
        return str(value).strip()
    except Exception:
        return default


def normalize_code(value: str) -> str:
    value = (value or "").upper().strip()
    value = re.sub(r"[^A-Z0-9]", "", value)
    return value


def looks_like_code(value: str) -> bool:
    code = normalize_code(value)
    return bool(CODE_RE.fullmatch(code))


def sanitize_filename_part(value: str) -> str:
    value = normalize_code(value)
    value = value or "SINCODIGO"
    return re.sub(r"[^A-Z0-9_-]", "", value)


def image_average_hash(image_bytes: bytes, size: int = 8) -> str:
    """Hash visual simple, sin depender de imagehash."""
    with Image.open(io.BytesIO(image_bytes)) as im:
        im = ImageOps.exif_transpose(im).convert("L").resize((size, size))
        pixels = list(im.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for p in pixels:
            bits = (bits << 1) | int(p >= avg)
        return f"{bits:0{size * size // 4}x}"


def hamming_hex(a: str, b: str) -> Optional[int]:
    try:
        ia = int(a, 16)
        ib = int(b, 16)
        return (ia ^ ib).bit_count()
    except Exception:
        return None


def rect_iou(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = a & b
    if inter.is_empty:
        return 0.0
    inter_area = inter.get_area()
    union_area = a.get_area() + b.get_area() - inter_area
    if union_area <= 0:
        return 0.0
    return inter_area / union_area


def dedupe_rects(rects: List[fitz.Rect], iou_threshold: float = 0.85) -> List[fitz.Rect]:
    result: List[fitz.Rect] = []
    for rect in sorted(rects, key=lambda r: (round(r.y0, 1), round(r.x0, 1), -r.get_area())):
        if any(rect_iou(rect, existing) >= iou_threshold for existing in result):
            continue
        result.append(rect)
    return result


def get_pdf_words(page) -> List[Tuple[float, float, float, float, str]]:
    words = page.get_text("words") or []
    clean_words = []
    for w in words:
        if len(w) >= 5:
            text = str(w[4]).strip()
            if text:
                clean_words.append((float(w[0]), float(w[1]), float(w[2]), float(w[3]), text))
    return clean_words


def extract_code_items(page, page_number: int) -> List[TextItem]:
    items: List[TextItem] = []
    for x0, y0, x1, y1, text in get_pdf_words(page):
        # Algunos PDFs parten texto; por eso también revisamos matches dentro de cada palabra.
        upper = text.upper().strip()
        for match in CODE_RE.finditer(upper):
            code = normalize_code(match.group(0))
            if not code:
                continue
            # Evitar que 4PETS/P3TS del encabezado se vuelvan producto.
            if code in {"4PETS", "P3TS"}:
                continue
            items.append(TextItem(text=text, code=code, rect=fitz.Rect(x0, y0, x1, y1), page_number=page_number))
    return items


def get_image_rects(page) -> List[fitz.Rect]:
    rects: List[fitz.Rect] = []
    page_rect = page.rect
    page_area = page_rect.get_area()

    # Método 1: imágenes embebidas según PyMuPDF.
    try:
        for info in page.get_image_info(xrefs=True):
            bbox = info.get("bbox")
            if not bbox:
                continue
            rect = fitz.Rect(bbox)
            rects.append(rect)
    except Exception:
        pass

    # Método 2: bloques de imagen dentro del diccionario de texto.
    try:
        page_dict = page.get_text("dict") or {}
        for block in page_dict.get("blocks", []):
            if block.get("type") == 1 and "bbox" in block:
                rects.append(fitz.Rect(block["bbox"]))
    except Exception:
        pass

    filtered: List[fitz.Rect] = []
    for rect in rects:
        if rect.is_empty:
            continue
        width = rect.width
        height = rect.height
        area = rect.get_area()
        if width < 25 or height < 25:
            continue
        if area < 900:
            continue
        # Evita capturar un fondo de página completa como si fuera producto.
        if page_area > 0 and area / page_area > 0.80:
            continue
        filtered.append(rect)

    return dedupe_rects(filtered)


def find_code_below_image(image_rect: fitz.Rect, codes: List[TextItem], used_code_indexes: set) -> Optional[Tuple[int, TextItem]]:
    candidates: List[Tuple[float, int, TextItem]] = []
    img_center_x = (image_rect.x0 + image_rect.x1) / 2
    max_below = max(65.0, image_rect.height * 0.45)

    for idx, item in enumerate(codes):
        if idx in used_code_indexes:
            continue
        code_rect = item.rect
        if code_rect.y0 < image_rect.y1 - 4:
            continue
        vertical_gap = code_rect.y0 - image_rect.y1
        if vertical_gap > max_below:
            continue
        code_center_x = (code_rect.x0 + code_rect.x1) / 2
        center_dist = abs(code_center_x - img_center_x)
        horizontal_overlap = max(0.0, min(image_rect.x1, code_rect.x1) - max(image_rect.x0, code_rect.x0))
        center_allowed = max(image_rect.width * 0.65, 45.0)
        if horizontal_overlap <= 0 and center_dist > center_allowed:
            continue
        score = vertical_gap + center_dist * 0.025
        candidates.append((score, idx, item))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][2]


def find_image_above_code(code_item: TextItem, image_rects: List[fitz.Rect], used_image_indexes: set) -> Optional[Tuple[int, fitz.Rect]]:
    """Busca la imagen que está encima de un código.

    Esta función corrige el orden del catálogo: primero se ordenan los códigos por su
    posición visual en el PDF y después se busca la imagen asociada a cada código.
    Así la vista previa y la subida quedan en el mismo orden del PDF, aunque el PDF
    entregue internamente las imágenes en un orden raro.
    """
    candidates: List[Tuple[float, int, fitz.Rect]] = []
    code_rect = code_item.rect
    code_center_x = (code_rect.x0 + code_rect.x1) / 2

    for idx, image_rect in enumerate(image_rects):
        if idx in used_image_indexes:
            continue
        if image_rect.is_empty:
            continue

        # La imagen del producto debe estar encima del código o apenas tocándolo.
        # Esto evita escoger QR u otras imágenes que estén abajo del bloque de texto.
        if image_rect.y0 > code_rect.y0:
            continue
        vertical_gap = code_rect.y0 - image_rect.y1
        if vertical_gap < -8:
            continue
        max_gap = max(90.0, image_rect.height * 0.65)
        if vertical_gap > max_gap:
            continue

        image_center_x = (image_rect.x0 + image_rect.x1) / 2
        center_dist = abs(code_center_x - image_center_x)
        horizontal_overlap = max(0.0, min(image_rect.x1, code_rect.x1) - max(image_rect.x0, code_rect.x0))
        center_allowed = max(image_rect.width * 0.70, 55.0)
        if horizontal_overlap <= 0 and center_dist > center_allowed:
            continue

        # Preferimos la imagen más cercana encima del código y mejor alineada.
        # Un pequeño bono por tamaño ayuda a preferir la foto principal sobre adornos.
        area_bonus = min(image_rect.get_area() / 100000.0, 3.0)
        score = max(vertical_gap, 0) + center_dist * 0.035 - area_bonus
        candidates.append((score, idx, image_rect))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][2]


def same_text_line(a: fitz.Rect, b: fitz.Rect) -> bool:
    """Devuelve True si dos palabras están en la misma línea visual."""
    ac = (a.y0 + a.y1) / 2
    bc = (b.y0 + b.y1) / 2
    tolerance = max(a.height, b.height, 6.0) * 0.75
    return abs(ac - bc) <= tolerance


def is_money_word(text: str) -> bool:
    clean = (text or "").strip().replace(" ", "")
    if "$" in clean and any(ch.isdigit() for ch in clean):
        return True
    return bool(PRICE_TEXT_RE.match(clean))


def find_price_word_rects(words: List[Tuple[float, float, float, float, str]], crop_rect: fitz.Rect, code_rect: fitz.Rect) -> List[fitz.Rect]:
    """Encuentra la zona del precio usando el texto real del PDF.

    No depende del ancho del recorte. Busca palabras con $ y, si el PDF separa el $ del número,
    también tapa el número que esté en la misma línea.
    """
    entries: List[Tuple[fitz.Rect, str]] = []
    for x0, y0, x1, y1, text in words:
        rect = fitz.Rect(x0, y0, x1, y1)
        center = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
        if crop_rect.contains(center):
            entries.append((rect, str(text).strip()))

    price_rects: List[fitz.Rect] = []
    for idx, (rect, text) in enumerate(entries):
        clean = text.replace(" ", "")
        # Evita borrar números dentro de la descripción: el precio normalmente está debajo del código.
        if rect.y0 < code_rect.y1 - 2:
            continue

        # Caso normal: la palabra contiene $6,900 o $.
        if "$" in clean:
            line_rect = fitz.Rect(rect)
            for other_rect, other_text in entries:
                if other_rect == rect:
                    continue
                if other_rect.y0 < code_rect.y1 - 2:
                    continue
                if same_text_line(rect, other_rect):
                    other_clean = other_text.replace(" ", "")
                    # Incluye solo la parte numérica del precio, no palabras como Pres:.
                    if is_money_word(other_clean) or (other_clean.replace(",", "").replace(".", "").isdigit() and other_rect.x0 >= rect.x0 - 2):
                        line_rect |= other_rect
            price_rects.append(line_rect)
            continue

        # Caso alternativo: algunos PDFs no incluyen $ en la palabra, pero dejan números tipo 6,900.
        if is_money_word(clean):
            # Solo tomarlo si en la misma línea o muy cerca existe un símbolo $ o la palabra Pres.
            nearby_dollar = False
            nearby_pres = False
            for other_rect, other_text in entries:
                if other_rect.y0 < code_rect.y1 - 2:
                    continue
                if same_text_line(rect, other_rect) or abs(other_rect.y1 - rect.y0) < max(rect.height, 8.0) * 1.8:
                    o = other_text.strip().lower()
                    if "$" in o:
                        nearby_dollar = True
                    if o.startswith("pres") or "precio" in o:
                        nearby_pres = True
            if nearby_dollar or nearby_pres:
                price_rects.append(fitz.Rect(rect))

    # Une rectángulos muy cercanos en la misma línea para tapar bien el precio completo.
    merged: List[fitz.Rect] = []
    for rect in sorted(price_rects, key=lambda r: (r.y0, r.x0)):
        placed = False
        for i, existing in enumerate(merged):
            if same_text_line(rect, existing) and abs(rect.x0 - existing.x1) < 35:
                merged[i] = existing | rect
                placed = True
                break
        if not placed:
            merged.append(rect)
    return merged


def cover_price_in_image(im: Image.Image, price_rects: List[fitz.Rect], crop_rect: fitz.Rect, original_render_size: Tuple[int, int], dpi: int) -> Image.Image:
    """Tapa el precio con blanco en la imagen ya renderizada del producto."""
    if not price_rects:
        return im
    zoom = dpi / 72.0
    render_w, render_h = original_render_size
    scale_x = im.width / max(render_w, 1)
    scale_y = im.height / max(render_h, 1)
    draw = ImageDraw.Draw(im)
    for rect in price_rects:
        x0 = int((rect.x0 - crop_rect.x0) * zoom * scale_x) - 8
        y0 = int((rect.y0 - crop_rect.y0) * zoom * scale_y) - 6
        x1 = int((rect.x1 - crop_rect.x0) * zoom * scale_x) + 10
        y1 = int((rect.y1 - crop_rect.y0) * zoom * scale_y) + 8
        x0 = max(0, min(im.width, x0))
        y0 = max(0, min(im.height, y0))
        x1 = max(0, min(im.width, x1))
        y1 = max(0, min(im.height, y1))
        if x1 > x0 and y1 > y0:
            draw.rectangle([x0, y0, x1, y1], fill="white")
    return im


def crop_product_from_page(page, image_rect: fitz.Rect, code_item: TextItem, dpi: int = 180, hide_price: bool = False) -> bytes:
    """Recorta la imagen y el texto relacionado debajo.

    En modo SP (hide_price=True), mantiene código, descripción y QR, pero tapa solo el precio
    usando la posición real del texto en el PDF.
    """
    words = get_pdf_words(page)
    crop_rect = fitz.Rect(image_rect)
    code_rect = fitz.Rect(code_item.rect)
    crop_rect |= code_rect

    img_width = max(image_rect.width, 1.0)
    x_min_allowed = image_rect.x0 - img_width * 0.20
    x_max_allowed = image_rect.x1 + img_width * 0.20
    y_min_allowed = image_rect.y0 - 6
    y_max_allowed = code_rect.y1 + max(45.0, image_rect.height * 0.20)

    for x0, y0, x1, y1, text in words:
        word_rect = fitz.Rect(x0, y0, x1, y1)
        center_x = (x0 + x1) / 2
        if x_min_allowed <= center_x <= x_max_allowed and y_min_allowed <= y0 <= y_max_allowed:
            crop_rect |= word_rect

    # Un poco de margen para no cortar bordes o texto.
    pad_x = max(4.0, image_rect.width * 0.035)
    pad_y = max(4.0, image_rect.height * 0.030)
    crop_rect = fitz.Rect(
        max(page.rect.x0, crop_rect.x0 - pad_x),
        max(page.rect.y0, crop_rect.y0 - pad_y),
        min(page.rect.x1, crop_rect.x1 + pad_x),
        min(page.rect.y1, crop_rect.y1 + pad_y),
    )

    price_rects = find_price_word_rects(words, crop_rect, code_rect) if hide_price else []

    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    pix = page.get_pixmap(matrix=matrix, clip=crop_rect, alpha=False)
    original_render_size = (pix.width, pix.height)
    png_bytes = pix.tobytes("png")

    with Image.open(io.BytesIO(png_bytes)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        # Limita tamaño para que Google Fotos y Streamlit trabajen más rápido.
        max_side = 1800
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        if hide_price:
            im = cover_price_in_image(im, price_rects, crop_rect, original_render_size, dpi)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=92, optimize=True)
        return out.getvalue()

def extract_products_from_pdf(pdf_bytes: bytes, catalog_key: str, dpi: int = 180) -> Tuple[List[ProductCrop], List[str]]:
    if fitz is None:
        raise RuntimeError("Falta PyMuPDF. En requirements.txt debe existir: PyMuPDF")
    if not pdf_bytes or not pdf_bytes[:5] == b"%PDF-":
        raise ValueError("El archivo no parece ser un PDF válido.")

    warnings: List[str] = []
    products: List[ProductCrop] = []
    seen_codes: Dict[str, int] = {}
    prefix = CATALOGS.get(catalog_key, {}).get("filename_prefix", catalog_key)
    hide_price = CATALOGS.get(catalog_key, {}).get("mode") == "hide_price"

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_index in range(len(doc)):
            page = doc[page_index]
            page_number = page_index + 1
            image_rects = get_image_rects(page)
            image_rects = sorted(image_rects, key=lambda r: (round(r.y0, 1), round(r.x0, 1)))
            code_items = extract_code_items(page, page_number)
            code_items = sorted(code_items, key=lambda item: (round(item.rect.y0, 1), round(item.rect.x0, 1)))

            if not image_rects:
                warnings.append(f"Página {page_number}: no encontré imágenes embebidas para recortar.")
                continue
            if not code_items:
                warnings.append(f"Página {page_number}: no encontré códigos de producto seleccionables.")
                continue

            # Corrección de orden:
            # Antes se recorrían primero las imágenes internas del PDF. Algunos PDFs entregan
            # esas imágenes en orden extraño. Ahora recorremos primero los códigos, que sí
            # siguen el orden visual del catálogo: página -> arriba-abajo -> izquierda-derecha.
            used_images = set()
            used_codes = set()
            order_on_page = 0
            for code_idx, code_item in enumerate(code_items):
                code = normalize_code(code_item.code)
                if not code:
                    continue
                matched_image = find_image_above_code(code_item, image_rects, used_images)
                if not matched_image:
                    continue
                image_idx, image_rect = matched_image
                used_codes.add(code_idx)
                used_images.add(image_idx)

                if code in seen_codes:
                    warnings.append(
                        f"Código duplicado {code}: ya apareció antes. Se conserva la primera aparición y se omite esta."
                    )
                    continue
                seen_codes[code] = 1
                try:
                    image_bytes = crop_product_from_page(page, image_rect, code_item, dpi=dpi, hide_price=hide_price)
                    image_hash = image_average_hash(image_bytes)
                    global_order = len(products) + 1
                    filename = f"{prefix}_{global_order:04d}_{sanitize_filename_part(code)}.jpg"
                    product_bbox = tuple(float(v) for v in image_rect)
                    products.append(
                        ProductCrop(
                            code=code,
                            page_number=page_number,
                            order_on_page=order_on_page,
                            bbox_points=product_bbox,
                            image_bytes=image_bytes,
                            image_hash=image_hash,
                            filename=filename,
                        )
                    )
                    order_on_page += 1
                except Exception as exc:
                    warnings.append(f"Página {page_number}, código {code}: no pude recortar la imagen. Error: {exc}")

            unmatched_codes = [item.code for idx, item in enumerate(code_items) if idx not in used_codes]
            # No mostramos todos para no llenar pantalla; solo diagnóstico útil.
            if len(unmatched_codes) > 12:
                warnings.append(
                    f"Página {page_number}: {len(unmatched_codes)} códigos no fueron asociados a una imagen. "
                    "Puede ser encabezado, tabla o texto fuera de productos."
                )

    products.sort(key=lambda p: (p.page_number, p.order_on_page))
    if not products:
        warnings.append(
            "No se pudo asociar ningún código con imagen. Revisa que los códigos estén debajo de cada imagen y que el PDF no sea una sola imagen escaneada."
        )
    return products, warnings


def get_access_token() -> str:
    client_id = safe_secret("GOOGLE_CLIENT_ID")
    client_secret = safe_secret("GOOGLE_CLIENT_SECRET")
    refresh_token = safe_secret("GOOGLE_REFRESH_TOKEN")
    missing = [name for name, val in [
        ("GOOGLE_CLIENT_ID", client_id),
        ("GOOGLE_CLIENT_SECRET", client_secret),
        ("GOOGLE_REFRESH_TOKEN", refresh_token),
    ] if not val]
    if missing:
        raise RuntimeError("Faltan Secrets de Google: " + ", ".join(missing))

    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=45,
    )
    if response.status_code != 200:
        raise RuntimeError(f"No pude renovar el token de Google. {response.status_code}: {response.text[:400]}")
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("Google no devolvió access_token.")
    return token


def google_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def google_json_request(method: str, url: str, token: str, **kwargs) -> dict:
    headers = kwargs.pop("headers", {}) or {}
    headers.update(google_headers(token))
    response = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    if response.status_code >= 300:
        raise RuntimeError(f"Error Google Photos {response.status_code}: {response.text[:700]}")
    if not response.text:
        return {}
    try:
        return response.json()
    except Exception:
        return {}


def get_album(album_id: str, token: str) -> dict:
    return google_json_request("GET", f"{PHOTOS_API}/albums/{album_id}", token)


def create_google_album(title: str, token: str) -> dict:
    """Crea un álbum desde la app para que luego la app pueda administrarlo mejor."""
    payload = {"album": {"title": title}}
    return google_json_request("POST", f"{PHOTOS_API}/albums", token, json=payload)


def list_album_media(album_id: str, token: str) -> List[dict]:
    items: List[dict] = []
    page_token = None
    while True:
        payload = {"albumId": album_id, "pageSize": 100}
        if page_token:
            payload["pageToken"] = page_token
        data = google_json_request("POST", f"{PHOTOS_API}/mediaItems:search", token, json=payload)
        items.extend(data.get("mediaItems", []) or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def parse_code_from_media_item(item: dict) -> Optional[str]:
    description = item.get("description") or ""
    match = re.search(r"APP_PRODUCT_CODE\s*=\s*([A-Z0-9]{3,14})", description.upper())
    if match:
        return normalize_code(match.group(1))

    filename = (item.get("filename") or "").upper()
    filename_no_ext = re.sub(r"\.[A-Z0-9]+$", "", filename)
    tokens = re.split(r"[^A-Z0-9]+", filename_no_ext)
    ignored = {"4PETS", "P3TS", "BROTHERS", "CATALOGO", "PRODUCTO", "IMG", "IMAGE", "FOTO"}
    candidates = [normalize_code(t) for t in tokens if normalize_code(t) and normalize_code(t) not in ignored]
    candidates = [c for c in candidates if looks_like_code(c)]
    if candidates:
        # Normalmente el código va al final: 4PETS_CEP32.jpg.
        return candidates[-1]
    return None


def parse_hash_from_media_item(item: dict) -> Optional[str]:
    description = item.get("description") or ""
    match = re.search(r"IMAGE_HASH\s*=\s*([0-9A-Fa-f]+)", description)
    if match:
        return match.group(1).lower()
    return None


def media_by_code(items: List[dict]) -> Tuple[Dict[str, List[dict]], List[dict]]:
    by_code: Dict[str, List[dict]] = {}
    without_code: List[dict] = []
    for item in items:
        code = parse_code_from_media_item(item)
        if code:
            by_code.setdefault(code, []).append(item)
        else:
            without_code.append(item)
    return by_code, without_code


def remove_media_from_album(album_id: str, media_ids: List[str], token: str, progress_label: str = "Retirando imágenes") -> int:
    if not media_ids:
        return 0
    total = 0
    progress = st.progress(0, text=progress_label)
    chunks = [media_ids[i : i + 50] for i in range(0, len(media_ids), 50)]
    for idx, chunk in enumerate(chunks, start=1):
        google_json_request(
            "POST",
            f"{PHOTOS_API}/albums/{album_id}:batchRemoveMediaItems",
            token,
            json={"mediaItemIds": chunk},
        )
        total += len(chunk)
        progress.progress(idx / len(chunks), text=f"{progress_label}: {total}/{len(media_ids)}")
        time.sleep(0.15)
    progress.empty()
    return total


def upload_raw_image(product: ProductCrop, token: str) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "X-Goog-Upload-File-Name": product.filename,
        "X-Goog-Upload-Protocol": "raw",
    }
    response = requests.post(f"{PHOTOS_API}/uploads", headers=headers, data=product.image_bytes, timeout=90)
    if response.status_code >= 300:
        raise RuntimeError(f"Error subiendo {product.filename}: {response.status_code} {response.text[:400]}")
    upload_token = response.text.strip()
    if not upload_token:
        raise RuntimeError(f"Google no devolvió upload token para {product.filename}")
    return upload_token


def product_global_order(product: ProductCrop) -> str:
    """Extrae el número global del nombre, por ejemplo SP_0001_CODIGO.jpg."""
    match = re.search(r"_(\d{4,6})_", product.filename or "")
    if match:
        return match.group(1)
    return ""


def product_description(product: ProductCrop, catalog_key: str, catalog_title: str) -> str:
    return "\n".join(
        [
            f"APP_PRODUCT_CODE={product.code}",
            f"CATALOG_KEY={catalog_key}",
            f"CATALOG_TITLE={catalog_title}",
            f"ORDER_GLOBAL={product_global_order(product)}",
            f"PAGE={product.page_number}",
            f"ORDER_ON_PAGE={product.order_on_page}",
            f"IMAGE_HASH={product.image_hash}",
            f"APP_VERSION={APP_VERSION}",
        ]
    )


def batch_create_media(album_id: str, products_with_tokens: List[Tuple[ProductCrop, str]], catalog_key: str, catalog_title: str, token: str) -> int:
    """Crea elementos en Google Fotos respetando el orden recibido.

    Para evitar que Google Fotos mezcle el orden, esta versión se usa en modo estricto:
    normalmente recibe 1 producto por llamada desde upload_products_to_album().
    Además envía albumPosition LAST_IN_ALBUM.
    """
    if not products_with_tokens:
        return 0

    new_media_items = []
    for product, upload_token in products_with_tokens:
        new_media_items.append(
            {
                "description": product_description(product, catalog_key, catalog_title),
                "simpleMediaItem": {
                    "fileName": product.filename,
                    "uploadToken": upload_token,
                },
            }
        )

    payload = {
        "albumId": album_id,
        "newMediaItems": new_media_items,
        "albumPosition": {"position": "LAST_IN_ALBUM"},
    }

    data = google_json_request("POST", f"{PHOTOS_API}/mediaItems:batchCreate", token, json=payload)
    results = data.get("newMediaItemResults", []) or []
    if not results:
        names = ", ".join([p.filename for p, _ in products_with_tokens])
        raise RuntimeError(f"Google no confirmó la creación de: {names}")

    total_created = 0
    failures = []
    for (product, _upload_token), result in zip(products_with_tokens, results):
        status = result.get("status", {}) or {}
        code = int(status.get("code", 0) or 0)
        if code == 0 and result.get("mediaItem"):
            total_created += 1
        else:
            message = status.get("message") or "sin mensaje de Google"
            failures.append(f"{product.filename}: {message}")

    if failures:
        raise RuntimeError("Google no creó correctamente estas imágenes: " + " | ".join(failures[:5]))

    # Pausa intencional para darle tiempo a Google Fotos a ubicar el elemento al final.
    time.sleep(GOOGLE_CREATE_DELAY_SECONDS)
    return total_created


def upload_products_to_album(album_id: str, products: List[ProductCrop], catalog_key: str, catalog_title: str, token: str, label: str = "Subiendo imágenes") -> int:
    """Sube y crea cada imagen una por una para conservar el orden del PDF en el álbum.

    MODO ULTRA ESTRICTO:
    1. subir bytes del producto 0001
    2. crear elemento 0001 al final del álbum usando albumPosition LAST_IN_ALBUM
    3. esperar unos segundos
    4. repetir con 0002, 0003, 0004...

    Es más lento, pero es lo máximo que podemos hacer dentro de Google Fotos.
    """
    if not products:
        return 0

    # Seguridad adicional: respeta el nombre 0001, 0002, 0003...
    products_sorted = sorted(products, key=lambda p: (product_global_order(p) or "999999", p.page_number, p.order_on_page, p.code))

    created_total = 0
    progress = st.progress(0, text=label)
    for idx, product in enumerate(products_sorted, start=1):
        progress.progress((idx - 1) / len(products_sorted), text=f"{label}: preparando {idx}/{len(products_sorted)} — {product.filename}")
        upload_token = upload_raw_image(product, token)
        created = batch_create_media(album_id, [(product, upload_token)], catalog_key, catalog_title, token)
        created_total += created
        progress.progress(idx / len(products_sorted), text=f"{label}: creado {idx}/{len(products_sorted)} — {product.filename}")
        # Pausa adicional corta entre productos para evitar que Google Fotos procese varios casi al mismo tiempo.
        time.sleep(0.35)

    progress.empty()
    return created_total


def render_product_preview(products: List[ProductCrop], limit: int = 12):
    if not products:
        return
    st.caption(f"Vista previa de los primeros {min(limit, len(products))} productos detectados.")
    cols = st.columns(3)
    for idx, product in enumerate(products[:limit]):
        with cols[idx % 3]:
            st.image(product.image_bytes, caption=f"{product.code} — pág. {product.page_number}", use_container_width=True)


def upload_pdf_widget(label: str, key: str) -> Optional[Tuple[bytes, str]]:
    """Cargador robusto de PDF con memoria.

    Corrección puntual para la pestaña de reconstrucción:
    - conserva el PDF en st.session_state después de seleccionarlo;
    - acepta PDF por firma interna %PDF aunque Android/Chrome lo entregue con tipo raro;
    - el botón principal Browse files ya NO filtra por tipo antes de recibir el archivo;
    - incluye un cargador alternativo por si Android/Chrome no entrega el archivo.
    """
    data_key = f"{key}_stored_pdf_bytes"
    name_key = f"{key}_stored_pdf_name"
    size_key = f"{key}_stored_pdf_size"
    hash_key = f"{key}_stored_pdf_hash"

    def persist_uploaded_pdf(uploaded) -> bool:
        if uploaded is None:
            return False
        try:
            data = uploaded.getvalue()
            name = uploaded.name or "catalogo.pdf"
        except Exception as exc:
            st.error(f"No pude leer el archivo seleccionado: {exc}")
            return False

        if not data:
            st.error("El PDF llegó vacío. Intenta seleccionarlo desde Descargas/Mis archivos, no desde Recientes.")
            return False

        is_pdf_name = name.lower().endswith(".pdf")
        is_pdf_signature = data[:5] == b"%PDF-"
        if not is_pdf_name and not is_pdf_signature:
            st.error("El archivo cargado no parece PDF. Renómbralo como catalogo.pdf o selecciona el archivo correcto.")
            return False

        st.session_state[data_key] = data
        st.session_state[name_key] = name
        st.session_state[size_key] = len(data)
        st.session_state[hash_key] = hashlib.sha256(data).hexdigest()
        return True

    uploaded = st.file_uploader(
        label,
        type=None,
        accept_multiple_files=False,
        key=f"{key}_pdf_picker",
        help="Selecciona el PDF desde Archivos/Mis archivos/Descargas. Este botón no filtra por tipo para evitar el error rojo en Android.",
    )
    persist_uploaded_pdf(uploaded)

    if st.session_state.get(data_key) is None:
        with st.expander("Si el PDF no carga, abre este cargador alternativo"):
            st.caption("Este segundo cargador no filtra por tipo de archivo. Úsalo si Android/Chrome pone el botón rojo y luego gris.")
            uploaded_alt = st.file_uploader(
                "Cargador alternativo del mismo PDF",
                type=None,
                accept_multiple_files=False,
                key=f"{key}_raw_picker",
            )
            persist_uploaded_pdf(uploaded_alt)

    stored_data = st.session_state.get(data_key)
    stored_name = st.session_state.get(name_key, "catalogo.pdf")
    stored_size = st.session_state.get(size_key, len(stored_data) if stored_data else 0)

    if stored_data:
        st.success(f"PDF cargado correctamente: {stored_name} ({stored_size / (1024 * 1024):.2f} MB)")
        st.caption("PDF listo para procesar. Cargado desde el botón Browse files corregido para Android.")
        if st.button("Limpiar PDF cargado", key=f"{key}_clear"):
            for k in (data_key, name_key, size_key, hash_key):
                st.session_state.pop(k, None)
            st.rerun()
        return stored_data, stored_name

    st.caption("Todavía no hay PDF cargado en esta sección.")
    return None

def password_gate() -> bool:
    configured_password = safe_secret("APP_PASSWORD")
    if not configured_password:
        st.warning("APP_PASSWORD no está configurado en Streamlit Secrets. La app queda sin clave interna.")
        return True
    if st.session_state.get("authenticated") is True:
        return True
    st.subheader("Ingreso")
    password = st.text_input("Clave de la app", type="password")
    if st.button("Entrar"):
        if password == configured_password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Clave incorrecta.")
    return False


def products_to_rows(products: List[ProductCrop], limit: Optional[int] = None) -> List[dict]:
    rows = []
    source = products if limit is None else products[:limit]
    for p in source:
        rows.append(
            {
                "codigo": p.code,
                "pagina": p.page_number,
                "orden": p.order_on_page + 1,
                "archivo": p.filename,
                "hash": p.image_hash,
            }
        )
    return rows


def codes_preview(codes: Iterable[str], max_items: int = 80) -> str:
    codes = sorted(list(codes))
    if not codes:
        return ""
    shown = codes[:max_items]
    suffix = "" if len(codes) <= max_items else f" ... y {len(codes) - max_items} más"
    return ", ".join(shown) + suffix


def analyze_pdf_with_ui(pdf_bytes: bytes, catalog_key: str, dpi: int, session_key: str) -> Optional[List[ProductCrop]]:
    with st.spinner("Leyendo PDF, códigos e imágenes..."):
        try:
            products, warnings = extract_products_from_pdf(pdf_bytes, catalog_key=catalog_key, dpi=dpi)
        except Exception as exc:
            st.error(f"No pude analizar el PDF: {exc}")
            return None
    st.session_state[session_key] = products
    if warnings:
        with st.expander("Advertencias de lectura del PDF"):
            for warning in warnings[:80]:
                st.warning(warning)
            if len(warnings) > 80:
                st.caption(f"Hay {len(warnings) - 80} advertencias adicionales.")
    if products:
        st.success(f"Productos detectados con código: {len(products)}")
        st.dataframe(products_to_rows(products, limit=200), use_container_width=True, hide_index=True)
        render_product_preview(products, limit=9)
    else:
        st.error("No se detectaron productos con código e imagen.")
    return products


def app():
    st.set_page_config(page_title="Catálogo Google Fotos", page_icon="🐾", layout="wide")
    st.title("FINAL: 3 CATÁLOGOS + SP SIN PRECIO + ORDEN ULTRA ESTRICTO")
    st.caption(f"Versión interna: {APP_VERSION}")

    if not password_gate():
        return

    if fitz is None:
        st.error("Falta PyMuPDF. En requirements.txt agrega: PyMuPDF")
        return

    st.sidebar.header("Catálogo")
    catalog_key = st.sidebar.radio(
        "Selecciona el catálogo que vas a trabajar",
        list(CATALOGS.keys()),
        format_func=lambda key: CATALOGS[key]["label"],
        key="catalog_key_radio",
    )
    catalog_cfg = CATALOGS[catalog_key]
    default_title = catalog_cfg["label"]
    catalog_title = safe_secret(catalog_cfg["album_title_secret"], default_title) or default_title
    album_id = safe_secret(catalog_cfg["album_id_secret"])
    # Permite usar en esta sesión un álbum recién creado por la app antes de copiarlo a Secrets.
    session_album_id = st.session_state.get(f"created_album_id_{catalog_key}", "")
    if not album_id and session_album_id:
        album_id = session_album_id

    st.sidebar.markdown("---")
    st.sidebar.write("**Catálogo actual:**", catalog_title)
    if catalog_cfg.get("mode") == "hide_price":
        st.sidebar.info("Modo SP: tapa solo el precio y conserva código, descripción y QR.")
    st.sidebar.write("**Secret ID:**", catalog_cfg["album_id_secret"])
    if album_id:
        st.sidebar.success("Álbum configurado")
        st.sidebar.code(album_id[:12] + "..." + album_id[-8:])
    else:
        st.sidebar.error("Falta el ID del álbum en Secrets")

    dpi = st.sidebar.slider("Calidad de recorte PDF", min_value=130, max_value=240, value=180, step=10)

    if st.sidebar.button("Probar conexión Google Fotos"):
        try:
            token = get_access_token()
            if not album_id:
                st.sidebar.error("No hay album_id configurado.")
            else:
                album = get_album(album_id, token)
                st.sidebar.success("Google Fotos conectado")
                st.sidebar.write(album.get("title", "Álbum sin título"))
        except Exception as exc:
            st.sidebar.error(str(exc))

    if not album_id:
        st.error(
            f"Falta configurar {catalog_cfg['album_id_secret']} en Streamlit Secrets. "
            "Sin ese ID no se puede revisar ni reconstruir este catálogo."
        )
        st.info(
            "Lo recomendado es que el álbum lo cree la app. Así la app podrá administrarlo mejor. "
            "Después de crearlo, copia el ID en Streamlit Secrets y reinicia la app."
        )
        if st.button(f"Crear álbum {catalog_title} desde la app", key=f"create_album_{catalog_key}"):
            try:
                token = get_access_token()
                album = create_google_album(catalog_title, token)
                new_album_id = album.get("id", "")
                st.session_state[f"created_album_id_{catalog_key}"] = new_album_id
                st.success(f"Álbum creado: {album.get('title', catalog_title)}")
                st.write("Copia este ID y pégalo en Streamlit Secrets:")
                st.code(f'{catalog_cfg["album_id_secret"]} = "{new_album_id}"')
                st.code(f'{catalog_cfg["album_title_secret"]} = "{catalog_title}"')
                st.warning("Después de guardar Secrets, haz Reboot app para que quede permanente.")
                if album.get("productUrl"):
                    st.link_button("Abrir álbum en Google Fotos", album["productUrl"])
                album_id = new_album_id
            except Exception as exc:
                st.error(f"No pude crear el álbum: {exc}")

    tab_test, tab_update, tab_rebuild, tab_diag = st.tabs(
        [
            "A. Prueba local PDF",
            "B. Revisar / actualizar por código",
            "C. Reconstruir álbum desde PDF completo",
            "D. Diagnóstico del álbum",
        ]
    )

    with tab_test:
        st.subheader("A. Prueba local de lectura del PDF")
        st.write(
            "Usa esta pestaña para confirmar que la app sí carga el PDF y detecta códigos debajo de las imágenes. "
            "Aquí no se sube nada a Google Fotos."
        )
        loaded = upload_pdf_widget("Cargar PDF para prueba local", key=f"test_pdf_{catalog_key}")
        if loaded:
            pdf_bytes, pdf_name = loaded
            if st.button("Analizar PDF localmente", key=f"analyze_test_{catalog_key}"):
                analyze_pdf_with_ui(pdf_bytes, catalog_key, dpi, session_key=f"test_products_{catalog_key}")

    with tab_update:
        st.subheader("B. Revisar y actualizar el álbum por código")
        st.write(
            "Esta sección compara los códigos del PDF contra los códigos guardados en Google Fotos. "
            "Así evita falsos nuevos o falsos agotados cuando cambia el precio o el diseño."
        )
        loaded = upload_pdf_widget("Cargar PDF nuevo del catálogo", key=f"update_pdf_{catalog_key}")
        if loaded:
            pdf_bytes, pdf_name = loaded
            if st.button("Analizar PDF y comparar con el álbum", key=f"compare_{catalog_key}", disabled=not bool(album_id)):
                products = analyze_pdf_with_ui(pdf_bytes, catalog_key, dpi, session_key=f"update_products_{catalog_key}")
                if products:
                    try:
                        token = get_access_token()
                        with st.spinner("Leyendo álbum de Google Fotos..."):
                            album_items = list_album_media(album_id, token)
                        by_code, without_code = media_by_code(album_items)
                        pdf_by_code = {p.code: p for p in products}
                        pdf_codes = set(pdf_by_code.keys())
                        album_codes = set(by_code.keys())

                        new_codes = sorted(pdf_codes - album_codes)
                        active_codes = sorted(pdf_codes & album_codes)
                        exhausted_codes = sorted(album_codes - pdf_codes)

                        st.session_state[f"analysis_{catalog_key}"] = {
                            "products": products,
                            "pdf_by_code": pdf_by_code,
                            "album_items": album_items,
                            "by_code": by_code,
                            "without_code": without_code,
                            "new_codes": new_codes,
                            "active_codes": active_codes,
                            "exhausted_codes": exhausted_codes,
                        }
                        st.success("Comparación terminada.")
                    except Exception as exc:
                        st.error(f"No pude comparar contra Google Fotos: {exc}")

        analysis = st.session_state.get(f"analysis_{catalog_key}")
        if analysis:
            new_codes = analysis["new_codes"]
            active_codes = analysis["active_codes"]
            exhausted_codes = analysis["exhausted_codes"]
            without_code = analysis["without_code"]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Nuevos reales", len(new_codes))
            c2.metric("Siguen activos", len(active_codes))
            c3.metric("Posibles agotados", len(exhausted_codes))
            c4.metric("En álbum sin código", len(without_code))

            with st.expander("Ver códigos nuevos"):
                st.write(codes_preview(new_codes) or "No hay nuevos.")
            with st.expander("Ver códigos posibles agotados"):
                st.write(codes_preview(exhausted_codes) or "No hay posibles agotados.")
            if without_code:
                st.warning(
                    "Hay imágenes en el álbum que no tienen código guardado. Para limpiar eso, usa la sección C: Reconstruir álbum."
                )

            st.markdown("### Ejecutar cambios en Google Fotos")
            do_new = st.checkbox("Subir productos nuevos", value=True, key=f"do_new_{catalog_key}")
            do_exhausted = st.checkbox(
                "Retirar del álbum los posibles agotados",
                value=False,
                key=f"do_exhausted_{catalog_key}",
                help="Solo retira del álbum las imágenes creadas/visibles para la app. No borra el álbum ni cambia el enlace.",
            )
            do_replace_active = st.checkbox(
                "Reemplazar productos que siguen activos para actualizar precio/imagen",
                value=False,
                key=f"do_replace_active_{catalog_key}",
                help="Úsalo cuando el PDF trae precios actualizados. Retira la imagen vieja del código y sube la nueva del PDF.",
            )
            confirm_changes = st.checkbox(
                "Confirmo que quiero aplicar estos cambios al álbum seleccionado",
                value=False,
                key=f"confirm_update_{catalog_key}",
            )

            if st.button("Aplicar actualización por código", key=f"apply_update_{catalog_key}", disabled=not confirm_changes):
                try:
                    token = get_access_token()
                    pdf_by_code: Dict[str, ProductCrop] = analysis["pdf_by_code"]
                    by_code: Dict[str, List[dict]] = analysis["by_code"]
                    removed_count = 0
                    uploaded_count = 0

                    if do_exhausted and exhausted_codes:
                        ids_to_remove = []
                        for code in exhausted_codes:
                            ids_to_remove.extend([item["id"] for item in by_code.get(code, []) if item.get("id")])
                        removed_count += remove_media_from_album(album_id, ids_to_remove, token, "Retirando agotados")

                    if do_replace_active and active_codes:
                        ids_to_remove = []
                        for code in active_codes:
                            ids_to_remove.extend([item["id"] for item in by_code.get(code, []) if item.get("id")])
                        removed_count += remove_media_from_album(album_id, ids_to_remove, token, "Retirando versiones anteriores")
                        products_to_upload = [pdf_by_code[code] for code in active_codes if code in pdf_by_code]
                        uploaded_count += upload_products_to_album(
                            album_id,
                            products_to_upload,
                            catalog_key,
                            catalog_title,
                            token,
                            label="Subiendo versiones actualizadas",
                        )

                    if do_new and new_codes:
                        products_to_upload = [pdf_by_code[code] for code in new_codes if code in pdf_by_code]
                        uploaded_count += upload_products_to_album(
                            album_id,
                            products_to_upload,
                            catalog_key,
                            catalog_title,
                            token,
                            label="Subiendo nuevos",
                        )

                    st.success(f"Actualización terminada. Retiradas: {removed_count}. Subidas: {uploaded_count}.")
                    st.info("Vuelve a analizar para verificar el resultado actualizado.")
                except Exception as exc:
                    st.error(f"No pude aplicar la actualización: {exc}")

    with tab_rebuild:
        st.subheader("C. Reconstruir álbum desde PDF completo")
        st.warning(
            "Esta opción conserva el mismo álbum y el mismo enlace compartido, pero retira del álbum las imágenes que la app puede ver/manejar "
            "y sube de nuevo todo el PDF con códigos. Es la opción correcta para reemplazar imágenes viejas que no tenían código."
        )
        st.info("Botón corregido: este cargador acepta el archivo primero y luego verifica si es PDF. Así evitamos que Android/Chrome lo rechace antes de cargar.")
        loaded = upload_pdf_widget("Cargar PDF completo para reconstruir el álbum", key=f"rebuild_pdf_{catalog_key}")
        if loaded:
            pdf_bytes, pdf_name = loaded
            if st.button("Analizar PDF para reconstrucción", key=f"analyze_rebuild_{catalog_key}"):
                analyze_pdf_with_ui(pdf_bytes, catalog_key, dpi, session_key=f"rebuild_products_{catalog_key}")

        products = st.session_state.get(f"rebuild_products_{catalog_key}")
        if products:
            st.info(f"Listo para reconstruir {catalog_title} con {len(products)} productos detectados.")
            confirm_rebuild_1 = st.checkbox(
                "Entiendo que se retirarán del álbum las imágenes antiguas visibles para la app",
                value=False,
                key=f"confirm_rebuild_1_{catalog_key}",
            )
            confirm_rebuild_2 = st.checkbox(
                "Entiendo que el álbum se conserva y el enlace compartido no cambia",
                value=False,
                key=f"confirm_rebuild_2_{catalog_key}",
            )
            if st.button(
                f"Reconstruir álbum {catalog_title} desde PDF completo",
                key=f"run_rebuild_{catalog_key}",
                disabled=not (confirm_rebuild_1 and confirm_rebuild_2 and bool(album_id)),
            ):
                try:
                    token = get_access_token()
                    with st.spinner("Leyendo elementos actuales del álbum..."):
                        album_items = list_album_media(album_id, token)
                    ids_to_remove = [item["id"] for item in album_items if item.get("id")]
                    removed = remove_media_from_album(album_id, ids_to_remove, token, "Limpiando álbum")

                    if ids_to_remove:
                        wait_box = st.empty()
                        wait_bar = st.progress(0, text="Esperando que Google Fotos termine de limpiar el álbum...")
                        for second in range(GOOGLE_REBUILD_SETTLE_SECONDS):
                            wait_bar.progress(
                                (second + 1) / GOOGLE_REBUILD_SETTLE_SECONDS,
                                text=f"Esperando limpieza de Google Fotos: {second + 1}/{GOOGLE_REBUILD_SETTLE_SECONDS} segundos",
                            )
                            time.sleep(1)
                        wait_bar.empty()
                        wait_box.empty()

                    created = upload_products_to_album(
                        album_id,
                        products,
                        catalog_key,
                        catalog_title,
                        token,
                        label="Subiendo catálogo completo en orden ultra estricto",
                    )
                    st.success(
                        f"Reconstrucción terminada. Imágenes retiradas del álbum: {removed}. Imágenes subidas: {created}."
                    )
                    st.info(
                        "Si había fotos o videos subidos manualmente directamente en Google Fotos, pueden seguir en el álbum porque la app quizá no puede verlos."
                    )
                except Exception as exc:
                    st.error(f"No pude reconstruir el álbum: {exc}")

    with tab_diag:
        st.subheader("D. Diagnóstico del álbum")
        st.write("Sirve para confirmar si el álbum actual tiene códigos guardados por la app.")
        if st.button("Leer diagnóstico del álbum", key=f"diag_{catalog_key}", disabled=not bool(album_id)):
            try:
                token = get_access_token()
                album = get_album(album_id, token)
                items = list_album_media(album_id, token)
                by_code, without_code = media_by_code(items)
                st.success("Álbum leído correctamente")
                st.write("**Título Google Fotos:**", album.get("title", ""))
                st.write("**ID del álbum:**")
                st.code(album_id)
                c1, c2, c3 = st.columns(3)
                c1.metric("Elementos visibles para la app", len(items))
                c2.metric("Códigos detectados", len(by_code))
                c3.metric("Elementos sin código", len(without_code))
                rows = []
                for code, media_items in sorted(by_code.items()):
                    hashes = [parse_hash_from_media_item(item) for item in media_items]
                    rows.append(
                        {
                            "codigo": code,
                            "cantidad_en_album": len(media_items),
                            "archivo_1": media_items[0].get("filename", ""),
                            "hash_1": hashes[0] or "",
                        }
                    )
                if rows:
                    st.dataframe(rows, use_container_width=True, hide_index=True)
                if without_code:
                    with st.expander("Elementos visibles sin código"):
                        st.dataframe(
                            [
                                {"archivo": item.get("filename", ""), "id": item.get("id", "")[:12] + "..."}
                                for item in without_code[:200]
                            ],
                            use_container_width=True,
                            hide_index=True,
                        )
            except Exception as exc:
                st.error(f"No pude leer el diagnóstico: {exc}")

    st.markdown("---")
    st.caption(
        "Regla de trabajo: las fotos de productos que la app debe controlar deben entrar por la app. "
        "Fotos o videos subidos manualmente a Google Fotos pueden quedar fuera del control automático."
    )


if __name__ == "__main__":
    app()
