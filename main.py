import os
import shutil
import time
import uuid
import threading

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_OUTPUT_DIR = "output"
os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
app.mount("/output", StaticFiles(directory=BASE_OUTPUT_DIR), name="output")

MAX_SECONDS = 600                 # 10 דקות
MAX_UPLOAD_MB = 25                # מגבלת גודל קובץ מועלה
JOB_TTL_SECONDS = 60 * 60         # מחיקת עבודות ישנות אחרי שעה
ALLOWED_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".webm", ".mp4"}

# --- Spleeter נטען פעם אחת בלבד (חוסך זיכרון וזמן בכל בקשה) ---
_separator = None
_sep_lock = threading.Lock()


def get_separator():
    global _separator
    with _sep_lock:
        if _separator is None:
            from spleeter.separator import Separator
            _separator = Separator("spleeter:2stems", multiprocess=False)
        return _separator


def cleanup_old_jobs():
    now = time.time()
    for name in os.listdir(BASE_OUTPUT_DIR):
        path = os.path.join(BASE_OUTPUT_DIR, name)
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > JOB_TTL_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def new_job():
    cleanup_old_jobs()
    job_id = uuid.uuid4().hex[:8]
    job_dir = os.path.join(BASE_OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    return job_id, job_dir


def separate(job_id: str, job_dir: str, audio_path: str):
    """מפריד לשני ערוצים ומחזיר קישורים. codec='mp3' - אחרת Spleeter שומר WAV."""
    try:
        sep = get_separator()
        sep.separate_to_file(
            audio_path,
            job_dir,
            codec="mp3",
            bitrate="192k",
            duration=MAX_SECONDS,
            filename_format="{instrument}.{codec}",
        )
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"שגיאה בהפרדת השיר: {e}")

    try:
        os.remove(audio_path)  # אין צורך בקובץ המקור אחרי ההפרדה
    except OSError:
        pass

    return {
        "status": "success",
        "accompaniment": f"/output/{job_id}/accompaniment.mp3",
        "vocals": f"/output/{job_id}/vocals.mp3",
    }


@app.get("/")
def home():
    return {"status": "ok", "message": "Karaoke Backend is live"}


# ---------- דרך 1 (המומלצת): העלאת קובץ ----------
@app.post("/upload")
def upload_file(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="סוג קובץ לא נתמך. העלה MP3 / WAV / M4A")

    job_id, job_dir = new_job()
    audio_path = os.path.join(job_dir, f"source{ext}")

    size = 0
    with open(audio_path, "wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                out.close()
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(status_code=413, detail=f"הקובץ גדול מ-{MAX_UPLOAD_MB}MB")
            out.write(chunk)

    return separate(job_id, job_dir, audio_path)


# ---------- דרך 2: קישור יוטיוב (עובד רק עם עוגיות או מחוץ ל-Render) ----------
class ProcessRequest(BaseModel):
    url: str


def get_cookie_file(job_dir: str):
    """אם הוגדר משתנה סביבה YT_COOKIES (תוכן קובץ cookies.txt) - נשתמש בו."""
    cookies = os.environ.get("YT_COOKIES", "").strip()
    if not cookies:
        return None
    path = os.path.join(job_dir, "cookies.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cookies.replace("\\n", "\n") + "\n")
    return path


@app.post("/process")
def process_video(req: ProcessRequest):
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="חסר קישור")

    if "list=" in url or "start_radio=" in url:
        url = url.split("&list=")[0].split("?list=")[0].split("&start_radio=")[0]

    job_id, job_dir = new_job()
    base = os.path.join(job_dir, "source")

    ydl_opts = {
        "format": "ba/b",
        "outtmpl": f"{base}.%(ext)s",
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <= {MAX_SECONDS}"),
        "quiet": True,
        "no_warnings": True,
    }
    cookie_file = get_cookie_file(job_dir)
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        msg = str(e)
        if "403" in msg or "Sign in" in msg or "bot" in msg.lower():
            raise HTTPException(
                status_code=502,
                detail="יוטיוב חוסם את השרת. השתמש באפשרות 'העלאת קובץ' במקום קישור.",
            )
        raise HTTPException(status_code=500, detail=f"שגיאת יוטיוב: {msg}")

    audio_path = f"{base}.mp3"
    if not os.path.exists(audio_path):
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="השיר ארוך מ-10 דקות או שההורדה נכשלה")

    return separate(job_id, job_dir, audio_path)
