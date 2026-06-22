from __future__ import annotations

import json
import mimetypes
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import fitz  # PyMuPDF
import imagehash
import numpy as np
import pandas as pd
import requests
import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from PIL import Image, ImageOps

# ============================================================
# CONFIGURACION GENERAL
# ============================================================

st.set_page_config(
    page_title="Sincronizador Catálogo ↔ Google Fotos",
    page_icon="📸",
    layout="wide",
)

APP_DIR = Path(".")
DATA_DIR = APP_DIR / "data"
RUNS_DIR = APP_DIR / "runs"
STATE_FILE = DATA_DIR / "state.json"
TOKEN_FILE = DATA_DIR / "google_token_web.json"
DATA_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
]
API_ROOT = "https://photoslibrary.googleapis.com/v1"
UPLOAD_URL = f"{API_ROOT}/uploads"


# ============================================================
# UTILIDADES
# ============================================================

def secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Lee secretos de Streamlit Cloud sin romper la app si todavía no existen."""
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> Dict:
    state = load_json(STATE_FILE, {"album_id": None, "album_title": "Catalogo Maestro Productos", "album_product_url": None})
    # Permite guardar datos fijos en Streamlit Secrets.
    if secret("MASTER_ALBUM_ID"):
        state["album_id"] = secret("MASTER_ALBUM_ID")
    if secret("MASTER_ALBUM_TITLE"):
        state["album_title"] = secret("MASTER_ALBUM_TITLE")
    if secret("MASTER_ALBUM_PRODUCT_URL"):
        state["album_product_url"] = secret("MASTER_ALBUM_PRODUCT_URL")
    return state


def save_state(state: Dict) -> None:
    save_json(STATE_FILE, state)


def require_private_access() -> None:
    configured_password = secret("APP_PASSWORD")
    if not configured_password:
        st.warning(
            "APP_PASSWORD no está configurada en Streamlit Secrets. "
            "La app funciona, pero antes de usar datos reales deberías poner una clave privada."
        )
        return

    if st.session_state.get("authenticated"):
        return

    st.title("🔐 Acceso privado")
    password = st.text_input("Clave de acceso", type="password")
    if st.button("Entrar"):
        if password == configured_password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Clave incorrecta.")
    st.stop()


def new_run_dir(prefix: str = "run") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = RUNS_DIR / f"{prefix}_{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_uploaded_file(uploaded_file, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(uploaded_file.name).name.replace(" ", "_")
    dest = dest_dir / safe_name
    dest.write_bytes(uploaded_file.getbuffer())
    return dest


def create_zip(paths: Iterable[Path], zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in paths:
            if path.exists():
                zf.write(path, arcname=path.name)
    return zip_path


def show_image_grid(paths: Sequence[Path], title: str, max_items: int = 24) -> None:
    st.subheader(title)
    if not paths:
        st.info("No hay imágenes para mostrar.")
        return
    cols = st.columns(4)
    for idx, path in enumerate(paths[:max_items]):
        with cols[idx % 4]:
            try:
                st.image(str(path), caption=path.name, use_container_width=True)
            except Exception:
                st.caption(path.name)
    if len(paths) > max_items:
        st.caption(f"Mostrando {max_items} de {len(paths)} imágenes.")


def dataframe_download(df: pd.DataFrame, label: str, filename: str) -> None:
    st.download_button(label, df.to_csv(index=False).encode("utf-8"), filename, "text/csv")


# ============================================================
# RECORTE DE PDF
# ============================================================

@dataclass
class CropResult:
    path: Path
    page: int
    box: Tuple[int, int, int, int]
    method: str


def _bbox_iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    x1, y1 = max(ax1, bx1), max(ay1, by1)
    x2, y2 = min(ax2, bx2), min(ay2, by2)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / max(area_a + area_b - inter, 1)


def _dedupe_boxes(boxes: List[Tuple[int, int, int, int]], iou_threshold: float = 0.65) -> List[Tuple[int, int, int, int]]:
    boxes = sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    kept: List[Tuple[int, int, int, int]] = []
    for box in boxes:
        if all(_bbox_iou(box, old) < iou_threshold for old in kept):
            kept.append(box)
    return kept


def _detect_black_border_crops( image: Image.Image, min_area_ratio: float, max_area_ratio: float, border_darkness: int, margin_px: int, ) -> List[Tuple[int, int, int, int]]:
    arr = np.array(image)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape[:2]
    page_area = height * width

    # Detecta zonas oscuras, especialmente bordes negros.
    _, binary = cv2.threshold(gray, border_darkness, 255, cv2.THRESH_BINARY_INV)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: List[Tuple[int, int, int, int]] = []

    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if area < page_area * min_area_ratio or area > page_area * max_area_ratio:
            continue
        aspect = w / max(h, 1)
        if aspect < 0.20 or aspect > 5.0:
            continue
        if w > width * 0.96 and h > height * 0.96:
            continue

        x1 = max(0, x - margin_px)
        y1 = max(0, y - margin_px)
        x2 = min(width, x + w + margin_px)
        y2 = min(height, y + h + margin_px)
        boxes.append((x1, y1, x2, y2))

    boxes = _dedupe_boxes(boxes)
    return sorted(boxes, key=lambda b: (b[1] // 50, b[0]))


def _extract_embedded_images(pdf_path: Path, output_dir: Path, min_width: int = 120, min_height: int = 120) -> List[CropResult]:
    doc = fitz.open(str(pdf_path))
    results: List[CropResult] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    for page_index in range(len(doc)):
        page = doc[page_index]
        for image_index, img_info in enumerate(page.get_images(full=True), start=1):
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image.get("image")
            if not image_bytes:
                continue
            ext = base_image.get("ext", "jpg").lower()
            if ext not in {"jpg", "jpeg", "png", "webp"}:
                ext = "jpg"
            out_path = output_dir / f"page_{page_index + 1:03d}_embedded_{image_index:03d}.{ext}"
            out_path.write_bytes(image_bytes)
            try:
                with Image.open(out_path) as im:
                    im = ImageOps.exif_transpose(im)
                    if im.width < min_width or im.height < min_height:
                        out_path.unlink(missing_ok=True)
                        continue
                    jpg_path = out_path.with_suffix(".jpg")
                    im.convert("RGB").save(jpg_path, "JPEG", quality=92)
                    if jpg_path != out_path:
                        out_path.unlink(missing_ok=True)
                    out_path = jpg_path
            except Exception:
                out_path.unlink(missing_ok=True)
                continue
            results.append(CropResult(out_path, page_index + 1, (0, 0, 0, 0), "embedded"))
    return results


def extract_product_images( pdf_path: Path, output_dir: Path, dpi: int = 180, min_area_ratio: float = 0.004, max_area_ratio: float = 0.70, border_darkness: int = 80, margin_px: int = 8, use_embedded_fallback: bool = True, ) -> List[CropResult]:
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    results: List[CropResult] = []
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    for page_index in range(len(doc)):
        page = doc[page_index]
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        pil_page = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        boxes = _detect_black_border_crops(
            pil_page,
            min_area_ratio=min_area_ratio,
            max_area_ratio=max_area_ratio,
            border_darkness=border_darkness,
            margin_px=margin_px,
        )
        for crop_index, box in enumerate(boxes, start=1):
            crop = pil_page.crop(box).convert("RGB")
            out_path = output_dir / f"page_{page_index + 1:03d}_product_{crop_index:03d}.jpg"
            crop.save(out_path, "JPEG", quality=92)
            results.append(CropResult(out_path, page_index + 1, box, "border"))

    if not results and use_embedded_fallback:
        results = _extract_embedded_images(pdf_path, output_dir)
    return results


# ============================================================
# COMPARACION VISUAL
# ============================================================

@dataclass
class ImageFingerprint:
    path: str
    name: str
    phash: str
    dhash: str
    whash: str
    width: int
    height: int
    source_id: Optional[str] = None
    product_url: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def _open_normalized(path: Path) -> Image.Image:
    img = Image.open(path).convert("RGB")
    return ImageOps.exif_transpose(img)


def fingerprint_image(path: Path, source_id: Optional[str] = None, product_url: Optional[str] = None) -> ImageFingerprint:
    img = _open_normalized(path)
    img.thumbnail((768, 768))
    return ImageFingerprint(
        path=str(path),
        name=path.name,
        phash=str(imagehash.phash(img, hash_size=16)),
        dhash=str(imagehash.dhash(img, hash_size=16)),
        whash=str(imagehash.whash(img, hash_size=8)),
        width=img.width,
        height=img.height,
        source_id=source_id,
        product_url=product_url,
    )


def fingerprint_many(paths: Sequence[Path]) -> List[ImageFingerprint]:
    fps: List[ImageFingerprint] = []
    for path in paths:
        try:
            fps.append(fingerprint_image(path))
        except Exception:
            pass
    return fps


def _hash_distance(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def visual_distance(a: ImageFingerprint, b: ImageFingerprint) -> float:
    ph = _hash_distance(a.phash, b.phash)
    dh = _hash_distance(a.dhash, b.dhash)
    wh = _hash_distance(a.whash, b.whash)
    return float((0.60 * ph) + (0.30 * dh) + (0.10 * wh))


def build_distance_table(pdf_items: Sequence[ImageFingerprint], album_items: Sequence[ImageFingerprint]) -> pd.DataFrame:
    rows = []
    for i, pdf_item in enumerate(pdf_items):
        for j, album_item in enumerate(album_items):
            rows.append({
                "pdf_index": i,
                "album_index": j,
                "pdf_name": pdf_item.name,
                "album_name": album_item.name,
                "distance": visual_distance(pdf_item, album_item),
                "album_media_id": album_item.source_id,
                "album_product_url": album_item.product_url,
            })
    if not rows:
        return pd.DataFrame(columns=["pdf_index", "album_index", "pdf_name", "album_name", "distance"])
    return pd.DataFrame(rows).sort_values("distance", ascending=True).reset_index(drop=True)


def compare_sets( pdf_items: Sequence[ImageFingerprint], album_items: Sequence[ImageFingerprint], match_threshold: float = 18.0, doubtful_threshold: float = 30.0, ) -> Dict[str, object]:
    distance_table = build_distance_table(pdf_items, album_items)
    matched_pairs = []
    used_pdf = set()
    used_album = set()

    for _, row in distance_table.iterrows():
        if row["distance"] > match_threshold:
            break
        pi, ai = int(row["pdf_index"]), int(row["album_index"])
        if pi in used_pdf or ai in used_album:
            continue
        matched_pairs.append(row.to_dict())
        used_pdf.add(pi)
        used_album.add(ai)

    missing_pdf_indices = [i for i in range(len(pdf_items)) if i not in used_pdf]
    extra_album_indices = [i for i in range(len(album_items)) if i not in used_album]

    doubtful_pairs = []
    for pi in missing_pdf_indices:
        candidates = distance_table[
            (distance_table["pdf_index"] == pi) & (distance_table["album_index"].isin(extra_album_indices))
        ]
        if candidates.empty:
            continue
        best = candidates.iloc[0]
        if match_threshold < best["distance"] <= doubtful_threshold:
            doubtful_pairs.append(best.to_dict())

    doubtful_pdf = {int(x["pdf_index"]) for x in doubtful_pairs}
    doubtful_album = {int(x["album_index"]) for x in doubtful_pairs}

    new_pdf_items = [pdf_items[i] for i in missing_pdf_indices if i not in doubtful_pdf]
    old_album_items = [album_items[i] for i in extra_album_indices if i not in doubtful_album]

    return {
        "matched_pairs": matched_pairs,
        "doubtful_pairs": doubtful_pairs,
        "new_pdf_items": new_pdf_items,
        "old_album_items": old_album_items,
        "distance_table": distance_table,
        "summary": {
            "pdf_total": len(pdf_items),
            "album_total": len(album_items),
            "matched": len(matched_pairs),
            "new_for_album": len(new_pdf_items),
            "old_in_album": len(old_album_items),
            "doubtful": len(doubtful_pairs),
        },
    }


def items_to_dataframe(items: Sequence[ImageFingerprint]) -> pd.DataFrame:
    rows = [item.to_dict() for item in items]
    if not rows:
        return pd.DataFrame(columns=["name", "path", "source_id", "product_url"])
    return pd.DataFrame(rows)


# ============================================================
# GOOGLE PHOTOS API
# ============================================================

class GooglePhotosError(RuntimeError):
    pass


def get_redirect_uri() -> str:
    redirect_uri = secret("GOOGLE_REDIRECT_URI") or secret("APP_URL")
    if not redirect_uri:
        raise GooglePhotosError(
            "Falta configurar GOOGLE_REDIRECT_URI en Streamlit Secrets. "
            "Debe ser la URL pública de tu app, por ejemplo https://tu-app.streamlit.app"
        )
    return redirect_uri.rstrip("/")


def get_client_config() -> Dict:
    client_id = secret("GOOGLE_CLIENT_ID")
    client_secret = secret("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise GooglePhotosError("Faltan GOOGLE_CLIENT_ID y/o GOOGLE_CLIENT_SECRET en Streamlit Secrets.")
    redirect_uri = get_redirect_uri()
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


def build_flow() -> Flow:
    redirect_uri = get_redirect_uri()
    return Flow.from_client_config(get_client_config(), scopes=SCOPES, redirect_uri=redirect_uri)


def get_authorization_url() -> str:
    flow = build_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    st.session_state["google_oauth_state"] = state
    return authorization_url


def save_credentials(creds: Credentials) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    st.session_state["google_token_json"] = creds.to_json()


def load_credentials() -> Optional[Credentials]:
    """ Carga credenciales de Google Fotos. Prioridad: 1. Token guardado por la app después de OAuth normal. 2. GOOGLE_REFRESH_TOKEN guardado en Streamlit Secrets. El segundo método evita problemas de redirección OAuth en Streamlit Cloud. """
    token_json = st.session_state.get("google_token_json")
    if not token_json and TOKEN_FILE.exists():
        token_json = TOKEN_FILE.read_text(encoding="utf-8")
    if token_json:
        try:
            info = json.loads(token_json)
            return Credentials.from_authorized_user_info(info, SCOPES)
        except Exception:
            pass

    refresh_token = secret("GOOGLE_REFRESH_TOKEN")
    client_id = secret("GOOGLE_CLIENT_ID")
    client_secret = secret("GOOGLE_CLIENT_SECRET")
    if refresh_token and client_id and client_secret:
        try:
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=SCOPES,
            )
            creds.refresh(Request())
            save_credentials(creds)
            return creds
        except Exception:
            return None

    return None


def handle_oauth_callback() -> bool:
    params = st.query_params
    code = params.get("code")
    if not code:
        return False
    try:
        flow = build_flow()
        flow.fetch_token(code=code)
        save_credentials(flow.credentials)
        st.query_params.clear()
        return True
    except Exception as exc:
        raise GooglePhotosError(f"No pude completar la autorización con Google: {exc}") from exc


def is_google_connected() -> bool:
    creds = load_credentials()
    if not creds:
        return False
    if creds.valid:
        return True
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
            return True
        except Exception:
            return False
    return False


def get_credentials() -> Credentials:
    creds = load_credentials()
    if not creds:
        raise GooglePhotosError("Google Fotos todavía no está conectado. Autoriza primero la cuenta de Google.")
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
        return creds
    raise GooglePhotosError("La autorización de Google expiró. Vuelve a conectar Google Fotos.")


def disconnect_google() -> None:
    st.session_state.pop("google_token_json", None)
    TOKEN_FILE.unlink(missing_ok=True)


def _headers(creds: Credentials, content_type: str = "application/json") -> Dict[str, str]:
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": content_type}


def _check_response(response: requests.Response) -> Dict:
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise GooglePhotosError(f"Error Google Photos {response.status_code}: {detail}")
    if not response.text:
        return {}
    try:
        return response.json()
    except Exception:
        return {"text": response.text}


def create_album(creds: Credentials, title: str) -> Dict:
    response = requests.post(
        f"{API_ROOT}/albums",
        headers=_headers(creds),
        json={"album": {"title": title}},
        timeout=60,
    )
    return _check_response(response)


def list_albums(creds: Credentials, page_size: int = 50) -> List[Dict]:
    albums: List[Dict] = []
    page_token: Optional[str] = None
    while True:
        params = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        response = requests.get(f"{API_ROOT}/albums", headers=_headers(creds), params=params, timeout=60)
        data = _check_response(response)
        albums.extend(data.get("albums", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return albums


def find_album_by_title(creds: Credentials, title: str) -> Optional[Dict]:
    for album in list_albums(creds):
        if album.get("title") == title:
            return album
    return None


def find_or_create_album(creds: Credentials, title: str) -> Dict:
    existing = find_album_by_title(creds, title)
    if existing:
        return existing
    return create_album(creds, title)


def upload_bytes(creds: Credentials, image_path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(str(image_path))
    mime_type = mime_type or "image/jpeg"
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-type": "application/octet-stream",
        "X-Goog-Upload-Content-Type": mime_type,
        "X-Goog-Upload-File-Name": image_path.name,
        "X-Goog-Upload-Protocol": "raw",
    }
    response = requests.post(UPLOAD_URL, headers=headers, data=image_path.read_bytes(), timeout=180)
    if response.status_code >= 400:
        raise GooglePhotosError(f"Error subiendo imagen {response.status_code}: {response.text}")
    return response.text


def batch_create_media_items(creds: Credentials, upload_tokens: List[str], filenames: List[str], album_id: Optional[str] = None) -> List[Dict]:
    body: Dict[str, object] = {
        "newMediaItems": [
            {
                "description": f"Producto sincronizado desde PDF: {filename}",
                "simpleMediaItem": {"uploadToken": token, "fileName": filename},
            }
            for token, filename in zip(upload_tokens, filenames)
        ]
    }
    if album_id:
        body["albumId"] = album_id
    response = requests.post(f"{API_ROOT}/mediaItems:batchCreate", headers=_headers(creds), json=body, timeout=180)
    data = _check_response(response)
    return data.get("newMediaItemResults", [])


def upload_images_to_album(creds: Credentials, image_paths: Iterable[Path], album_id: str, batch_size: int = 25) -> List[Dict]:
    results: List[Dict] = []
    tokens: List[str] = []
    names: List[str] = []
    for image_path in image_paths:
        tokens.append(upload_bytes(creds, image_path))
        names.append(image_path.name)
        if len(tokens) >= batch_size:
            results.extend(batch_create_media_items(creds, tokens, names, album_id))
            tokens, names = [], []
    if tokens:
        results.extend(batch_create_media_items(creds, tokens, names, album_id))
    return results


def search_media_items_in_album(creds: Credentials, album_id: str, page_size: int = 100) -> List[Dict]:
    items: List[Dict] = []
    page_token: Optional[str] = None
    while True:
        body = {"albumId": album_id, "pageSize": page_size}
        if page_token:
            body["pageToken"] = page_token
        response = requests.post(f"{API_ROOT}/mediaItems:search", headers=_headers(creds), json=body, timeout=60)
        data = _check_response(response)
        items.extend(data.get("mediaItems", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def download_media_item(creds: Credentials, media_item: Dict, output_dir: Path) -> Optional[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base_url = media_item.get("baseUrl")
    filename = media_item.get("filename") or f"{media_item.get('id', 'media')}.jpg"
    if not base_url:
        return None
    filename = Path(filename).name.replace(" ", "_")
    out_path = output_dir / filename
    response = requests.get(base_url + "=d", headers={"Authorization": f"Bearer {creds.token}"}, timeout=120)
    if response.status_code >= 400:
        return None
    out_path.write_bytes(response.content)
    try:
        with Image.open(out_path) as im:
            jpg_path = out_path.with_suffix(".jpg")
            ImageOps.exif_transpose(im).convert("RGB").save(jpg_path, "JPEG", quality=92)
            if jpg_path != out_path:
                out_path.unlink(missing_ok=True)
            return jpg_path
    except Exception:
        out_path.unlink(missing_ok=True)
        return None


def download_album_media(creds: Credentials, album_id: str, output_dir: Path) -> List[Dict]:
    downloaded: List[Dict] = []
    items = search_media_items_in_album(creds, album_id)
    for item in items:
        path = download_media_item(creds, item, output_dir)
        if path:
            downloaded.append({"mediaItem": item, "path": str(path)})
    return downloaded


def batch_remove_media_items(creds: Credentials, album_id: str, media_item_ids: Sequence[str]) -> Dict:
    ids = [x for x in media_item_ids if x]
    if not ids:
        return {}
    response = requests.post(
        f"{API_ROOT}/albums/{album_id}:batchRemoveMediaItems",
        headers=_headers(creds),
        json={"mediaItemIds": ids},
        timeout=120,
    )
    return _check_response(response)


# ============================================================
# INTERFAZ STREAMLIT
# ============================================================

def get_settings_from_sidebar() -> Dict:
    st.sidebar.header("Ajustes")
    st.sidebar.caption("Empieza con estos valores. Luego se ajustan según tus PDFs reales.")
    dpi = st.sidebar.slider("Calidad de lectura PDF (DPI)", 120, 260, 180, 10)
    border_darkness = st.sidebar.slider("Qué tan oscuro debe ser el borde", 20, 140, 80, 5)
    min_area_ratio = st.sidebar.slider("Tamaño mínimo del recorte", 0.001, 0.030, 0.004, 0.001, format="%.3f")
    max_area_ratio = st.sidebar.slider("Tamaño máximo del recorte", 0.10, 0.95, 0.70, 0.05)
    margin_px = st.sidebar.slider("Margen alrededor del recorte", 0, 40, 8, 1)
    match_threshold = st.sidebar.slider("Umbral coincidencia segura", 4.0, 40.0, 18.0, 1.0)
    doubtful_threshold = st.sidebar.slider("Umbral revisión dudosa", 10.0, 70.0, 30.0, 1.0)
    return {
        "dpi": dpi,
        "border_darkness": border_darkness,
        "min_area_ratio": min_area_ratio,
        "max_area_ratio": max_area_ratio,
        "margin_px": margin_px,
        "match_threshold": match_threshold,
        "doubtful_threshold": doubtful_threshold,
    }


def extract_from_pdf_ui(pdf_file, run_dir: Path, prefix: str, settings: Dict) -> List[Path]:
    pdf_path = save_uploaded_file(pdf_file, run_dir)
    output_dir = run_dir / prefix
    results = extract_product_images(
        pdf_path=pdf_path,
        output_dir=output_dir,
        dpi=settings["dpi"],
        min_area_ratio=settings["min_area_ratio"],
        max_area_ratio=settings["max_area_ratio"],
        border_darkness=settings["border_darkness"],
        margin_px=settings["margin_px"],
        use_embedded_fallback=True,
    )
    return [r.path for r in results]


def summary_cards(summary: Dict) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Productos PDF", summary.get("pdf_total", 0))
    c2.metric("Productos álbum", summary.get("album_total", 0))
    c3.metric("Coincidencias", summary.get("matched", 0))
    c4.metric("Faltan subir", summary.get("new_for_album", 0))
    c5.metric("Posibles agotados", summary.get("old_in_album", 0))
    if summary.get("doubtful", 0):
        st.warning(f"Hay {summary['doubtful']} coincidencias dudosas para revisar manualmente.")


def local_test_tab(settings: Dict) -> None:
    st.header("1) Prueba local")
    st.write("Primero valida aquí que el PDF se recorta bien. Esta parte no usa Google Fotos.")

    pdf_file = st.file_uploader("Sube el PDF del catálogo actual", type=["pdf"], key="local_pdf")
    album_files = st.file_uploader(
        "Sube varias imágenes que simulen el álbum actual",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key="local_album_images",
    )

    if st.button("Analizar prueba local", type="primary", disabled=not pdf_file or not album_files):
        run_dir = new_run_dir("local")
        with st.spinner("Recortando imágenes del PDF..."):
            pdf_paths = extract_from_pdf_ui(pdf_file, run_dir, "pdf_productos", settings)
        album_dir = run_dir / "album_simulado"
        album_paths = [save_uploaded_file(file, album_dir) for file in album_files]
        with st.spinner("Comparando visualmente..."):
            pdf_items = fingerprint_many(pdf_paths)
            album_items = fingerprint_many(album_paths)
            result = compare_sets(pdf_items, album_items, settings["match_threshold"], settings["doubtful_threshold"])

        st.success("Análisis terminado.")
        summary_cards(result["summary"])
        new_items: List[ImageFingerprint] = result["new_pdf_items"]
        old_items: List[ImageFingerprint] = result["old_album_items"]
        doubtful = pd.DataFrame(result["doubtful_pairs"])

        col_a, col_b = st.columns(2)
        with col_a:
            missing_paths = [Path(item.path) for item in new_items]
            show_image_grid(missing_paths, "Faltan en el álbum: listos para subir", max_items=30)
            if missing_paths:
                zip_path = create_zip(missing_paths, run_dir / "imagenes_faltantes_para_subir.zip")
                st.download_button("Descargar ZIP con imágenes faltantes", zip_path.read_bytes(), "imagenes_faltantes_para_subir.zip", "application/zip")
        with col_b:
            old_paths = [Path(item.path) for item in old_items]
            show_image_grid(old_paths, "Sobran en el álbum: posibles agotados", max_items=30)

        report_df = pd.DataFrame({
            "tipo": ["faltante_para_subir"] * len(new_items) + ["posible_agotado"] * len(old_items),
            "archivo": [item.name for item in new_items] + [item.name for item in old_items],
            "ruta": [item.path for item in new_items] + [item.path for item in old_items],
        })
        st.subheader("Reporte")
        st.dataframe(report_df, use_container_width=True)
        dataframe_download(report_df, "Descargar reporte CSV", "reporte_catalogo.csv")

        if not doubtful.empty:
            st.subheader("Coincidencias dudosas")
            st.dataframe(doubtful[["pdf_name", "album_name", "distance"]], use_container_width=True)


def google_connection_box() -> None:
    st.subheader("Conexión con Google Fotos")
    try:
        if handle_oauth_callback():
            st.success("Google Fotos conectado correctamente.")
    except Exception as exc:
        st.error(str(exc))

    connected = is_google_connected()
    if connected:
        st.success("Google Fotos está conectado.")
        if st.button("Desconectar Google Fotos"):
            disconnect_google()
            st.rerun()
    else:
        if secret("GOOGLE_REFRESH_TOKEN"):
            st.warning("Hay GOOGLE_REFRESH_TOKEN configurado, pero no pude conectarme. Revisa que el refresh token, client_id y client_secret pertenezcan al mismo proyecto de Google Cloud.")
        else:
            st.info("Google Fotos todavía no está conectado. Puedes usar el enlace de autorización normal o configurar GOOGLE_REFRESH_TOKEN en Streamlit Secrets para evitar problemas de redirección.")
        if st.button("Generar enlace de autorización Google"):
            try:
                auth_url = get_authorization_url()
                st.link_button("Autorizar Google Fotos", auth_url)
                st.caption("Después de autorizar, Google volverá automáticamente a esta app.")
            except Exception as exc:
                st.error(str(exc))


def google_photos_tab(settings: Dict) -> None:
    st.header("2) Google Fotos")
    st.write("Esta parte crea y actualiza el álbum maestro creado por la app.")
    google_connection_box()

    state = load_state()
    st.divider()
    st.subheader("Álbum maestro")
    album_title = st.text_input("Nombre del álbum maestro", value=state.get("album_title") or "Catalogo Maestro Productos")
    manual_album_id = st.text_input(
        "ID del álbum maestro si ya lo tienes",
        value=state.get("album_id") or "",
        help="Si la app ya creó el álbum antes, pega aquí el ID o guárdalo en Streamlit Secrets como MASTER_ALBUM_ID.",
    ).strip()
    if manual_album_id:
        state["album_id"] = manual_album_id
        state["album_title"] = album_title
        save_state(state)

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Crear o buscar álbum maestro", disabled=not is_google_connected()):
            try:
                with st.spinner("Buscando o creando álbum maestro..."):
                    creds = get_credentials()
                    album = find_or_create_album(creds, album_title)
                state["album_id"] = album.get("id")
                state["album_title"] = album_title
                state["album_product_url"] = album.get("productUrl")
                save_state(state)
                st.success(f"Álbum listo: {album.get('title')}")
                st.code(album.get("id") or "")
                st.info("Copia este ID y guárdalo en Streamlit Secrets como MASTER_ALBUM_ID.")
                if album.get("productUrl"):
                    st.link_button("Abrir álbum en Google Fotos", album["productUrl"])
            except Exception as exc:
                st.error(str(exc))
    with c2:
        st.write("**Álbum actual:**")
        st.code(state.get("album_id") or "Todavía no hay álbum guardado")
        if state.get("album_product_url"):
            st.link_button("Abrir álbum guardado", state["album_product_url"])

    st.divider()
    st.subheader("A. Carga inicial del catálogo")
    initial_pdf = st.file_uploader("Sube el primer PDF para llenar el álbum maestro", type=["pdf"], key="initial_pdf")
    if st.button("Recortar PDF y subir TODO al álbum maestro", disabled=not initial_pdf or not state.get("album_id") or not is_google_connected()):
        run_dir = new_run_dir("google_inicial")
        try:
            with st.spinner("Recortando imágenes del PDF inicial..."):
                pdf_paths = extract_from_pdf_ui(initial_pdf, run_dir, "pdf_productos", settings)
            st.write(f"Imágenes detectadas: {len(pdf_paths)}")
            show_image_grid(pdf_paths, "Vista previa de lo que se subirá", max_items=18)
            with st.spinner("Subiendo imágenes a Google Fotos..."):
                creds = get_credentials()
                results = upload_images_to_album(creds, pdf_paths, state["album_id"])
            st.success(f"Subida completada. Respuestas recibidas: {len(results)}")
        except Exception as exc:
            st.error(str(exc))

    st.divider()
    st.subheader("B. Actualizar álbum con un nuevo PDF")
    update_pdf = st.file_uploader("Sube el nuevo PDF del catálogo", type=["pdf"], key="update_pdf")
    if st.button("Comparar nuevo PDF contra álbum maestro", disabled=not update_pdf or not state.get("album_id") or not is_google_connected()):
        run_dir = new_run_dir("google_update")
        try:
            with st.spinner("Recortando imágenes del nuevo PDF..."):
                pdf_paths = extract_from_pdf_ui(update_pdf, run_dir, "pdf_productos", settings)
            with st.spinner("Descargando imágenes actuales del álbum maestro..."):
                creds = get_credentials()
                downloaded = download_album_media(creds, state["album_id"], run_dir / "album_actual")
            with st.spinner("Comparando PDF contra álbum..."):
                pdf_items = fingerprint_many(pdf_paths)
                album_items: List[ImageFingerprint] = []
                for entry in downloaded:
                    media = entry["mediaItem"]
                    fp = fingerprint_image(Path(entry["path"]), source_id=media.get("id"), product_url=media.get("productUrl"))
                    album_items.append(fp)
                result = compare_sets(pdf_items, album_items, settings["match_threshold"], settings["doubtful_threshold"])
            st.session_state["last_google_result"] = result
            st.session_state["last_google_run_dir"] = str(run_dir)
            st.session_state["last_album_id"] = state["album_id"]
            st.success("Comparación terminada.")
        except Exception as exc:
            st.error(str(exc))

    result = st.session_state.get("last_google_result")
    if result:
        summary_cards(result["summary"])
        new_items: List[ImageFingerprint] = result["new_pdf_items"]
        old_items: List[ImageFingerprint] = result["old_album_items"]
        doubtful = pd.DataFrame(result["doubtful_pairs"])

        c1, c2 = st.columns(2)
        with c1:
            show_image_grid([Path(item.path) for item in new_items], "Faltantes que la app puede subir", max_items=24)
            if st.button("Subir faltantes al álbum maestro", disabled=not new_items or not is_google_connected()):
                try:
                    with st.spinner("Subiendo faltantes..."):
                        creds = get_credentials()
                        upload_images_to_album(creds, [Path(item.path) for item in new_items], state["album_id"])
                    st.success("Faltantes subidos al álbum maestro.")
                except Exception as exc:
                    st.error(str(exc))
        with c2:
            show_image_grid([Path(item.path) for item in old_items], "Posibles agotados que la app puede retirar", max_items=24)
            st.warning("Revisa bien antes de retirar. La app los quita del álbum maestro.")
            old_df = items_to_dataframe(old_items)
            if not old_df.empty:
                st.dataframe(old_df[["name", "source_id", "product_url"]], use_container_width=True)
            if st.button("Retirar posibles agotados del álbum", disabled=not old_items or not is_google_connected()):
                try:
                    ids = [item.source_id for item in old_items if item.source_id]
                    with st.spinner("Retirando del álbum maestro..."):
                        creds = get_credentials()
                        batch_remove_media_items(creds, state["album_id"], ids)
                    st.success("Elementos retirados del álbum maestro.")
                except Exception as exc:
                    st.error(str(exc))

        report_df = pd.DataFrame({
            "tipo": ["faltante_para_subir"] * len(new_items) + ["posible_agotado"] * len(old_items),
            "archivo": [item.name for item in new_items] + [item.name for item in old_items],
            "ruta": [item.path for item in new_items] + [item.path for item in old_items],
            "google_media_id": [""] * len(new_items) + [item.source_id or "" for item in old_items],
            "google_url": [""] * len(new_items) + [item.product_url or "" for item in old_items],
        })
        st.subheader("Reporte de actualización")
        st.dataframe(report_df, use_container_width=True)
        dataframe_download(report_df, "Descargar reporte CSV", "reporte_actualizacion_google_fotos.csv")
        if new_items:
            zip_path = create_zip([Path(item.path) for item in new_items], Path(st.session_state["last_google_run_dir"]) / "faltantes.zip")
            st.download_button("Descargar ZIP de faltantes por seguridad", zip_path.read_bytes(), "faltantes.zip", "application/zip")
        if not doubtful.empty:
            st.subheader("Coincidencias dudosas")
            columns = [c for c in ["pdf_name", "album_name", "distance", "album_media_id", "album_product_url"] if c in doubtful.columns]
            st.dataframe(doubtful[columns], use_container_width=True)


def help_tab() -> None:
    st.header("3) Ayuda")
    st.markdown(
        """ ### Esta es la versión fácil para subir desde Android Solo necesitas subir archivos sueltos a GitHub. No necesitas subir carpetas. Archivos necesarios: - `app.py` - `requirements.txt` - `README.md` - `secrets.example.toml` ### Orden recomendado 1. Sube esos archivos a GitHub. 2. Publica la app en Streamlit Community Cloud usando `app.py` como archivo principal. 3. Configura los secretos de Streamlit. 4. Primero prueba la pestaña **Prueba local** con un PDF real. 5. Después conecta Google Fotos y crea el álbum maestro. ### Secretos que debes configurar en Streamlit ```toml APP_PASSWORD = "tu_clave_privada" GOOGLE_CLIENT_ID = "tu_client_id" GOOGLE_CLIENT_SECRET = "tu_client_secret" GOOGLE_REDIRECT_URI = "https://tu-app.streamlit.app" GOOGLE_REFRESH_TOKEN = "opcional_recomendado_si_el_regreso_de_google_falla" MASTER_ALBUM_ID = "opcional_si_ya_tienes_el_id" ``` ### Importante No subas contraseñas, claves de Google ni tokens a GitHub. """
    )


def main() -> None:
    require_private_access()
    st.title("📸 Sincronizador Catálogo PDF ↔ Google Fotos")
    st.caption("App web para usar desde Android, Windows o cualquier navegador.")

    settings = get_settings_from_sidebar()
    tab1, tab2, tab3 = st.tabs(["Prueba local", "Google Fotos", "Ayuda"])
    with tab1:
        local_test_tab(settings)
    with tab2:
        google_photos_tab(settings)
    with tab3:
        help_tab()


if __name__ == "__main__":
    main()
