import os
import json
import uuid
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import fitz  # PyMuPDF
import anthropic

app = FastAPI()

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
DOCS_DIR = DATA_DIR / "docs"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

# ── GIST PERSISTENCE ──────────────────────────────────────────────────────────
GIST_ID = os.environ.get("PODPIS_GIST_ID", "")
GIST_TOKEN = os.environ.get("PODPIS_GIST_TOKEN", "")
INDEX_FILE = "documents_index.json"


def gist_get(filename):
    if not GIST_ID or not GIST_TOKEN:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"token {GIST_TOKEN}", "User-Agent": "podpis-app"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
            if filename in d["files"]:
                return d["files"][filename]["content"]
    except Exception as e:
        print(f"Gist get error: {e}")
    return None


def gist_set(files):
    if not GIST_ID or not GIST_TOKEN:
        return
    try:
        import urllib.request
        payload = json.dumps({"files": {k: ({"content": v} if v is not None else None) for k, v in files.items()}}).encode()
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            data=payload, method="PATCH",
            headers={"Authorization": f"token {GIST_TOKEN}",
                     "Content-Type": "application/json", "User-Agent": "podpis-app"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print(f"Gist set error: {e}")


def load_index():
    content = gist_get(INDEX_FILE)
    if content:
        try:
            return json.loads(content)
        except Exception:
            return []
    return []


def save_index(index):
    gist_set({INDEX_FILE: json.dumps(index, ensure_ascii=False, indent=2)})


def load_doc(token):
    f = DOCS_DIR / f"{token}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    content = gist_get(f"doc_{token}.json")
    if content:
        data = json.loads(content)
        f.write_text(content, encoding="utf-8")
        return data
    return None


def save_doc(token, data, to_gist=True):
    f = DOCS_DIR / f"{token}.json"
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if to_gist:
        gist_set({f"doc_{token}.json": json.dumps(data, ensure_ascii=False, indent=2)})


# ── UPLOAD & ANALYZE PDF ─────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...)):
    token = str(uuid.uuid4())
    content = await file.read()

    pdf_path = DOCS_DIR / f"{token}.pdf"
    pdf_path.write_bytes(content)

    doc = fitz.open(stream=content, filetype="pdf")
    page_count = len(doc)

    sig_position = None

    # 1. Try to find a "dotted line" signature placeholder via text extraction
    for page_num in range(page_count):
        page = doc[page_num]
        text_dict = page.get_text("dict")
        page_rect = page.rect

        for block in text_dict.get("blocks", []):
            for line in block.get("lines", []):
                line_text = "".join(span["text"] for span in line["spans"])
                txt = line_text.strip()
                dot_count = txt.count(".")
                underscore_count = txt.count("_")
                if (dot_count > 10 or underscore_count > 10) and len(txt) > 10:
                    bbox = line["bbox"]
                    sig_position = {
                        "page": page_num,
                        "x": bbox[0],
                        "y": bbox[1] - 35,
                        "width": min(bbox[2] - bbox[0], 180),
                        "height": 30,
                        "page_width": page_rect.width,
                        "page_height": page_rect.height,
                    }

    # 2. Fallback: ask AI to find the position on the last page
    if sig_position is None:
        try:
            last_page = doc[page_count - 1]
            pix = last_page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img_bytes = pix.tobytes("png")
            import base64
            img_b64 = base64.b64encode(img_bytes).decode()

            client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
            resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                        {"type": "text", "text": (
                            "Na tomto obrazku PDF strany najdi miesto urcene na podpis "
                            "(zvycajne ciara s bodkami alebo podciarkovnikmi a meno pod nou). "
                            "Vrat IBA JSON s relativnymi suradnicami od 0 do 1: "
                            '{"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0} '
                            "kde x,y je lavy horny rohu OBLASTI NAD ciarou kam sa ma vlozit podpis "
                            "(width a height ako podiel sirky/vysky strany). "
                            "Bez akehokolvek dalsieho textu."
                        )}
                    ]
                }]
            )
            text_resp = resp.content[0].text.strip()
            text_resp = text_resp.replace("```json", "").replace("```", "").strip()
            rel = json.loads(text_resp)
            page_rect = last_page.rect
            sig_position = {
                "page": page_count - 1,
                "x": rel["x"] * page_rect.width,
                "y": rel["y"] * page_rect.height,
                "width": rel["width"] * page_rect.width,
                "height": rel["height"] * page_rect.height,
                "page_width": page_rect.width,
                "page_height": page_rect.height,
            }
        except Exception as e:
            print(f"AI position detection error: {e}")

    # 3. Last resort fallback
    if sig_position is None:
        last_page = doc[page_count - 1]
        page_rect = last_page.rect
        sig_position = {
            "page": page_count - 1,
            "x": page_rect.width * 0.5,
            "y": page_rect.height * 0.75,
            "width": 180,
            "height": 30,
            "page_width": page_rect.width,
            "page_height": page_rect.height,
        }

    doc.close()

    import datetime
    doc_data = {
        "token": token,
        "filename": file.filename,
        "page_count": page_count,
        "sig_position": sig_position,
        "signed": False,
        "signature": None,
        "created": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    save_doc(token, doc_data, to_gist=False)

    index = load_index()
    index.insert(0, {
        "token": token,
        "filename": file.filename,
        "created": doc_data["created"],
        "signed": False,
    })
    save_index(index[:50])

    return {"token": token, "sig_position": sig_position, "page_count": page_count}


# ── PREVIEW PAGE AS IMAGE ────────────────────────────────────────────────────

@app.get("/api/doc/{token}/page/{page_num}")
def get_page_image(token: str, page_num: int):
    pdf_path = DOCS_DIR / f"{token}.pdf"
    if not pdf_path.exists():
        b64 = gist_get(f"pdf_{token}.b64")
        if b64:
            import base64
            pdf_path.write_bytes(base64.b64decode(b64))
        else:
            raise HTTPException(404, "Dokument nenajdeny")

    doc = fitz.open(pdf_path)
    if page_num < 0 or page_num >= len(doc):
        raise HTTPException(404, "Strana nenajdena")
    page = doc[page_num]
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    img_path = DOCS_DIR / f"{token}_p{page_num}.png"
    pix.save(str(img_path))
    doc.close()
    return FileResponse(img_path, media_type="image/png")


# ── GET DOC INFO ─────────────────────────────────────────────────────────────

@app.get("/api/doc/{token}")
def get_doc(token: str):
    d = load_doc(token)
    if not d:
        raise HTTPException(404, "Dokument nenajdeny")
    return d


@app.get("/api/documents")
def list_documents():
    return load_index()


# ── UPDATE SIGNATURE POSITION ────────────────────────────────────────────────

@app.post("/api/doc/{token}/position")
async def update_position(token: str, request: Request):
    d = load_doc(token)
    if not d:
        raise HTTPException(404, "Dokument nenajdeny")
    body = await request.json()
    d["sig_position"] = body.get("sig_position", d["sig_position"])
    save_doc(token, d, to_gist=False)
    return {"ok": True}


# ── SUBMIT SIGNATURE ─────────────────────────────────────────────────────────

@app.post("/api/doc/{token}/sign")
async def submit_signature(token: str, request: Request):
    d = load_doc(token)
    if not d:
        raise HTTPException(404, "Dokument nenajdeny")

    body = await request.json()
    signature_data = body.get("signature", "")
    if not signature_data:
        raise HTTPException(400, "Chyba podpis")

    import datetime
    d["signature"] = signature_data
    d["signed"] = True
    d["signed_at"] = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

    pdf_path = DOCS_DIR / f"{token}.pdf"
    signed_path = DOCS_DIR / f"{token}_signed.pdf"

    import base64
    sig_b64 = signature_data.split(",")[-1] if "," in signature_data else signature_data
    sig_bytes = base64.b64decode(sig_b64)

    pdf_doc = fitz.open(pdf_path)
    pos = d["sig_position"]
    page = pdf_doc[pos["page"]]

    rect = fitz.Rect(pos["x"], pos["y"], pos["x"] + pos["width"], pos["y"] + pos["height"])
    page.insert_image(rect, stream=sig_bytes)

    pdf_doc.save(str(signed_path))
    pdf_doc.close()

    d["final_pdf"] = str(signed_path)
    save_doc(token, d, to_gist=False)

    async def _persist():
        try:
            pdf_b64 = base64.b64encode(signed_path.read_bytes()).decode()
            gist_set({
                f"signed_{token}.b64": pdf_b64,
                f"doc_{token}.json": json.dumps(d, ensure_ascii=False, indent=2),
            })
            index = load_index()
            for e in index:
                if e["token"] == token:
                    e["signed"] = True
                    break
            save_index(index)
        except Exception as e:
            print(f"Persist error: {e}")
    asyncio.create_task(_persist())

    return {"ok": True, "token": token}


# ── DOWNLOAD SIGNED PDF ──────────────────────────────────────────────────────

@app.get("/api/doc/{token}/download")
async def download_signed(token: str):
    d = load_doc(token)
    if not d:
        raise HTTPException(404, "Dokument nenajdeny")
    if not d.get("signed"):
        raise HTTPException(400, "Dokument este nie je podpisany")

    signed_path = DOCS_DIR / f"{token}_signed.pdf"
    if not signed_path.exists():
        b64 = gist_get(f"signed_{token}.b64")
        if b64:
            import base64
            signed_path.write_bytes(base64.b64decode(b64))
        else:
            pdf_path = DOCS_DIR / f"{token}.pdf"
            if not pdf_path.exists():
                pdf_b64 = gist_get(f"pdf_{token}.b64")
                if pdf_b64:
                    import base64
                    pdf_path.write_bytes(base64.b64decode(pdf_b64))
                else:
                    raise HTTPException(404, "Original PDF nenajdeny")

            if d.get("signature"):
                import base64
                sig_b64 = d["signature"].split(",")[-1] if "," in d["signature"] else d["signature"]
                sig_bytes = base64.b64decode(sig_b64)
                pdf_doc = fitz.open(pdf_path)
                pos = d["sig_position"]
                page = pdf_doc[pos["page"]]
                rect = fitz.Rect(pos["x"], pos["y"], pos["x"] + pos["width"], pos["y"] + pos["height"])
                page.insert_image(rect, stream=sig_bytes)
                pdf_doc.save(str(signed_path))
                pdf_doc.close()
            else:
                raise HTTPException(404, "Podpisany PDF nenajdeny")

    name = d.get("filename", "dokument").replace(".pdf", "")
    return FileResponse(signed_path, media_type="application/pdf", filename=f"{name}_podpisany.pdf")


# ── DELETE DOCUMENT ──────────────────────────────────────────────────────────

@app.delete("/api/doc/{token}")
async def delete_doc(token: str):
    for suffix in ["", "_signed"]:
        f = DOCS_DIR / f"{token}{suffix}.pdf"
        if f.exists():
            f.unlink()
    f = DOCS_DIR / f"{token}.json"
    if f.exists():
        f.unlink()
    for f in DOCS_DIR.glob(f"{token}_p*.png"):
        f.unlink()

    index = load_index()
    index = [e for e in index if e["token"] != token]
    save_index(index)

    async def _delete_gist():
        gist_set({
            f"doc_{token}.json": None,
            f"signed_{token}.b64": None,
            f"pdf_{token}.b64": None,
        })
    asyncio.create_task(_delete_gist())

    return {"ok": True}


# ── PERSIST PDF TO GIST (background) ─────────────────────────────────────────

@app.post("/api/doc/{token}/persist")
async def persist_pdf(token: str):
    pdf_path = DOCS_DIR / f"{token}.pdf"
    if not pdf_path.exists():
        raise HTTPException(404, "PDF nenajdeny")

    async def _save():
        try:
            import base64
            pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode()
            gist_set({f"pdf_{token}.b64": pdf_b64})
        except Exception as e:
            print(f"Persist PDF error: {e}")
    asyncio.create_task(_save())
    return {"ok": True}


# ── STATIC FILES ──────────────────────────────────────────────────────────

@app.get("/")
def index_page():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/sign/{token}")
def sign_page(token: str):
    return FileResponse(BASE / "static" / "sign.html")


@app.get("/ping")
def ping():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
