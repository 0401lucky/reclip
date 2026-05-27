import os
import uuid
import glob
import json
import subprocess
import tempfile
import threading
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}


def write_cookies_file(cookies):
    if not isinstance(cookies, str):
        cookies = ""
    cookies = (cookies or "").strip()
    if not cookies:
        return None

    fd, path = tempfile.mkstemp(prefix="reclip-cookies-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(cookies)
        if not cookies.endswith("\n"):
            f.write("\n")
    return path


def remove_temp_file(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def ytdlp_error(stderr, cookies_supplied=False):
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    raw = lines[-1] if lines else "yt-dlp failed"
    text = (stderr or raw).lower()

    if any(term in text for term in ("netscape format", "invalid cookie", "cookie file")):
        return "The supplied cookies could not be read. Paste Netscape-format cookies.txt content exported from your browser and try again."

    if "no video could be found in this tweet" in text:
        if cookies_supplied:
            return "Cookies were supplied, but Twitter/X still did not expose a video. Export fresh Netscape-format cookies.txt from a logged-in browser and try again."
        return "Twitter/X may require login cookies for this tweet. Paste Netscape-format cookies.txt from a logged-in browser and try again."

    twitter_blocked = ("twitter" in text or "x.com" in text) and any(
        term in text
        for term in ("login", "cookie", "sensitive", "nsfw", "guest", "403", "401", "unauthorized", "forbidden")
    )
    login_required = any(
        term in text
        for term in ("cookie", "cookies", "login", "log in", "sign in", "authentication", "private", "age-restricted")
    )

    if twitter_blocked or login_required:
        if cookies_supplied:
            return "Cookies were supplied, but the platform still blocked access. Export fresh Netscape-format cookies.txt from a logged-in browser and try again."
        return "This video may require login cookies. Paste Netscape-format cookies.txt and try again."

    return raw


def run_download(job_id, url, format_choice, format_id, cookies):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    cookies_path = None

    try:
        cookies_path = write_cookies_file(cookies)
        cmd = ["yt-dlp", "--no-playlist", "-o", out_template]
        if cookies_path:
            cmd += ["--cookies", cookies_path]

        if format_choice == "audio":
            cmd += ["-x", "--audio-format", "mp3"]
        elif format_id:
            cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
        else:
            cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = ytdlp_error(result.stderr, bool(cookies_path))
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        remove_temp_file(cookies_path)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    cookies = data.get("cookies", "")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cookies_path = None
    try:
        cookies_path = write_cookies_file(cookies)
        cmd = ["yt-dlp", "--no-playlist", "-j"]
        if cookies_path:
            cmd += ["--cookies", cookies_path]
        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": ytdlp_error(result.stderr, bool(cookies_path))}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        remove_temp_file(cookies_path)


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    cookies = data.get("cookies", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, cookies))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
