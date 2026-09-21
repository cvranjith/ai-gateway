"""Text extraction for uploaded background-prep materials - see
deepsink_sessions.py's material upload route, the only caller.
Deliberately just these two formats for now, matching the user's own
scoping request ("just a text parser will do... maybe PDF parser") -
no PPTX/DOCX support yet, easy to add here later if wanted.
"""

import io

from services.errors import ServiceError


def extract_text(file_bytes, fmt):
    if fmt == "text":
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise ServiceError("could not read this file as UTF-8 text", 400)
    if fmt == "pdf":
        return _extract_pdf_text(file_bytes)
    raise ServiceError(f"unsupported material format '{fmt}' - only 'pdf' and 'text' are supported", 400)


def _extract_pdf_text(file_bytes):
    # Imported here, not at module load - pypdf is a real dependency
    # (requirements.txt), but keeping the import local means a gateway
    # that somehow hasn't picked it up yet fails only when a PDF is
    # actually uploaded, not on every startup.
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    except Exception as e:
        raise ServiceError(f"could not read this PDF: {e}", 400)

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            # One unreadable page shouldn't sink the whole document -
            # skip it and keep whatever else extracts cleanly.
            continue

    text = "\n\n".join(p for p in pages if p.strip())
    if not text.strip():
        raise ServiceError("no extractable text found in this PDF - it may be scanned/image-only", 422)
    return text
