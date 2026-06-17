# Sincronizador Catálogo PDF ↔ Google Fotos

Esta es la versión simplificada para subir desde Android a GitHub.

## Archivos que debes subir a GitHub

Sube solo estos archivos sueltos:

- `app.py`
- `requirements.txt`
- `README.md`
- `secrets.example.toml`

No necesitas subir carpetas.

## Publicar en Streamlit Cloud

En Streamlit Community Cloud usa:

- Repository: tu repositorio de GitHub
- Branch: `main`
- Main file path: `app.py`

## Secretos en Streamlit

Configura estos secretos en la sección **Settings → Secrets** de tu app en Streamlit:

```toml
APP_PASSWORD = "cambia_esta_clave"
GOOGLE_CLIENT_ID = "tu_client_id"
GOOGLE_CLIENT_SECRET = "tu_client_secret"
GOOGLE_REDIRECT_URI = "https://tu-app.streamlit.app"
MASTER_ALBUM_ID = "opcional_si_ya_tienes_el_id"
MASTER_ALBUM_TITLE = "Catalogo Maestro Productos"
```

## Uso recomendado

1. Primero usa la pestaña **Prueba local**.
2. Sube un PDF real.
3. Sube varias imágenes que simulen el álbum.
4. Ajusta los controles hasta que el recorte funcione bien.
5. Luego conecta Google Fotos.
6. Crea el álbum maestro.
7. Comparte ese álbum manualmente una sola vez desde Google Fotos.
