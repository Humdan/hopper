"""Media job types: hear, see and make voice, images and video.

Built-in job types that need no agent, only an OpenRouter key and ffmpeg:

    transcribe   audio / voice note / video soundtrack -> text
    speak        text -> voice note (.ogg, plays inline in Telegram and WhatsApp)
    describe     image or video (+ optional question) -> text
    image        prompt (+ optional input images to edit) -> image file(s)
    video        prompt -> video   (not available: needs a video provider; refused clearly)

Queue one with `hop add --type image "a red fox at dusk"` (or the `type` argument of the
MCP hop_add). The job's meta carries {"type": ..., "input": {...}} and it requires the
capability "media.<type>", so `hop work --builtin media` (or any worker advertising that
capability) picks it up. The same functions run inline in `hop relay`, e.g. to turn an
incoming voice note into text before the agent sees it.

Models are configurable:

    [media]
    openrouter_key = "env:OPENROUTER_API_KEY"
    transcribe_model = "google/gemini-3.8-flash"
    describe_model = "google/gemini-3.8-flash"
    speak_model = "openai/gpt-audio-mini"
    speak_voice = "alloy"
    image_model = "openai/gpt-5-image-mini"
    out_dir = "~/.local/share/hopper/media"
    input_dirs = ["~/.local/share/hopper"]   # files a job may read; anything else is refused

Jobs can come from remote producers, so a job naming a file can only read under
input_dirs (by default Hopper's own data folder, where channel attachments and generated
media land). Otherwise a job could send any file on the machine to a cloud model.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from .config import Config, resolve_secret
from .util import HopperError, expand

TYPES = ("transcribe", "speak", "describe", "image", "video")
URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULTS = {
    "transcribe_model": "google/gemini-3.8-flash",
    "describe_model": "google/gemini-3.8-flash",
    "speak_model": "openai/gpt-audio-mini",
    "speak_voice": "alloy",
    "image_model": "openai/gpt-5-image-mini",
    "out_dir": "~/.local/share/hopper/media",
    "input_dirs": ["~/.local/share/hopper"],
}
VIDEO_INLINE_MAX = 18 * 1024 * 1024     # bigger videos are described from frames + soundtrack


class Media:
    def __init__(self, cfg: Config):
        self.conf = {**DEFAULTS, **(getattr(cfg, "media", None) or {})}
        self.out_dir = expand(self.conf["out_dir"])

    # -- plumbing -------------------------------------------------------------
    def _key(self) -> str:
        key = self.conf.get("openrouter_key")
        key = resolve_secret(key) if key else os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise HopperError("media needs an OpenRouter key: [media] openrouter_key or $OPENROUTER_API_KEY")
        return key

    def _post(self, body: dict, stream: bool = False, timeout: float = 180):
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={
            "Authorization": f"Bearer {self._key()}", "Content-Type": "application/json",
            "X-Title": "hopper"})
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                reason = json.load(exc).get("error", {}).get("message", "")
            except Exception:
                reason = ""
            raise HopperError(f"{body.get('model')}: HTTP {exc.code} {reason}".strip()) from None
        except urllib.error.URLError as exc:
            raise HopperError(f"openrouter unreachable: {exc.reason}") from None
        with r:
            if not stream:
                return json.load(r)
            return [json.loads(line[6:]) for line in r.read().decode().splitlines()
                    if line.startswith("data: ") and line[6:].strip() not in ("", "[DONE]")]

    def _out(self, ext: str) -> str:
        os.makedirs(self.out_dir, exist_ok=True)
        return os.path.join(self.out_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.{ext}")

    @staticmethod
    def _ffmpeg(*args: str) -> None:
        if not shutil.which("ffmpeg"):
            raise HopperError("ffmpeg is needed for audio and video (apt install ffmpeg)")
        proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise HopperError(f"ffmpeg failed: {proc.stderr.strip()[:300]}")

    @staticmethod
    def _data_url(path: str) -> str:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            return f"data:{mime};base64,{base64.b64encode(fh.read()).decode()}"

    @staticmethod
    def _text(resp: dict) -> str:
        return ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    def _need(self, path: str) -> str:
        path = os.path.realpath(expand(path or ""))
        roots = [os.path.realpath(expand(d)) for d in self.conf["input_dirs"]]
        if not any(os.path.commonpath([path, r]) == r for r in roots):
            raise HopperError(f"{path} is outside [media] input_dirs ({', '.join(roots)})")
        if not os.path.isfile(path):
            raise HopperError(f"no such file: {path}")
        return path

    # -- job types ------------------------------------------------------------
    def transcribe(self, path: str) -> dict:
        path = self._need(path)
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = os.path.join(tmp, "a.mp3")
            self._ffmpeg("-i", path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", mp3)
            with open(mp3, "rb") as fh:
                data = base64.b64encode(fh.read()).decode()
        resp = self._post({"model": self.conf["transcribe_model"], "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Transcribe this audio verbatim, in its original language. "
                                     "Output only the transcript; if there is no speech, output [no speech]."},
            {"type": "input_audio", "input_audio": {"data": data, "format": "mp3"}}]}]})
        text = self._text(resp).strip()
        return {"summary": text, "text": text, "artifacts": []}

    def speak(self, text: str, voice: str | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise HopperError("speak needs text")
        chunks = self._post({"model": self.conf["speak_model"], "modalities": ["text", "audio"],
                             "audio": {"voice": voice or self.conf["speak_voice"], "format": "pcm16"},
                             "stream": True,
                             "messages": [{"role": "system", "content": "Read the user's text aloud exactly "
                                           "as written. Do not add, answer or comment."},
                                          {"role": "user", "content": text}]}, stream=True)
        pcm = b"".join(base64.b64decode(d["audio"]["data"])
                       for c in chunks for d in [((c.get("choices") or [{}])[0].get("delta") or {})]
                       if (d.get("audio") or {}).get("data"))
        if not pcm:
            raise HopperError("the speech model returned no audio")
        out = self._out("ogg")
        with tempfile.NamedTemporaryFile(suffix=".pcm") as raw:
            raw.write(pcm)
            raw.flush()
            # gpt-audio streams 24 kHz mono 16-bit PCM; Opus in Ogg is what chat apps play as a voice note
            self._ffmpeg("-f", "s16le", "-ar", "24000", "-ac", "1", "-i", raw.name,
                         "-c:a", "libopus", "-b:a", "32k", out)
        return {"summary": f"Voice note ({len(pcm) / 48000:.0f}s)", "artifacts": [out]}

    def describe(self, path: str, question: str = "") -> dict:
        path = self._need(path)
        ask = question.strip() or "Describe this in detail: what it shows, any text in it, and anything notable."
        mime = mimetypes.guess_type(path)[0] or ""
        if mime.startswith("video/"):
            return self._describe_video(path, ask)
        if not mime.startswith("image/"):
            raise HopperError(f"describe takes an image or a video (got {mime or 'unknown type'})")
        resp = self._post({"model": self.conf["describe_model"], "messages": [{"role": "user", "content": [
            {"type": "text", "text": ask}, {"type": "image_url", "image_url": {"url": self._data_url(path)}}]}]})
        text = self._text(resp).strip()
        return {"summary": text, "text": text, "artifacts": []}

    def _describe_video(self, path: str, ask: str) -> dict:
        if os.path.getsize(path) <= VIDEO_INLINE_MAX:
            content = [{"type": "text", "text": ask},
                       {"type": "video_url", "video_url": {"url": self._data_url(path)}}]
        else:
            # Too big to send whole: 8 evenly spaced frames plus the soundtrack's transcript.
            with tempfile.TemporaryDirectory() as tmp:
                dur = _duration(path)
                self._ffmpeg("-i", path, "-vf", f"fps={8 / max(dur, 1):.4f},scale=768:-2", "-frames:v", "8",
                             os.path.join(tmp, "f%02d.jpg"))
                frames = sorted(os.path.join(tmp, f) for f in os.listdir(tmp))
                try:
                    heard = self.transcribe(path)["text"]
                except HopperError:
                    heard = "[no soundtrack]"
                content = [{"type": "text", "text": f"{ask}\n\nThese are {len(frames)} frames in order from a "
                                                    f"{dur:.0f}s video. Its soundtrack says: {heard}"}]
                content += [{"type": "image_url", "image_url": {"url": self._data_url(f)}} for f in frames]
        resp = self._post({"model": self.conf["describe_model"], "messages": [{"role": "user", "content": content}]})
        text = self._text(resp).strip()
        return {"summary": text, "text": text, "artifacts": []}

    def image(self, prompt: str, inputs=()) -> dict:
        prompt = (prompt or "").strip()
        if not prompt:
            raise HopperError("image needs a prompt")
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": self._data_url(self._need(p))}} for p in inputs or ()]
        resp = self._post({"model": self.conf["image_model"], "modalities": ["image", "text"],
                           "messages": [{"role": "user", "content": content}]}, timeout=300)
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        paths = []
        for img in msg.get("images") or []:
            url = (img.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                header, _, b64 = url.partition(",")
                ext = (mimetypes.guess_extension(header[5:].split(";")[0]) or ".png").lstrip(".")
                out = self._out(ext)
                with open(out, "wb") as fh:
                    fh.write(base64.b64decode(b64))
                paths.append(out)
        if not paths:
            raise HopperError(f"the image model returned no image: {(msg.get('content') or '')[:200]}")
        return {"summary": (msg.get("content") or "Here's the image.").strip(), "artifacts": paths}

    def video(self, prompt: str) -> dict:
        raise HopperError("video generation isn't set up: no video provider is configured")

    # -- dispatch -------------------------------------------------------------
    def run(self, kind: str, inp: dict) -> dict:
        """Run one job type. Returns {"summary", "artifacts", ...}."""
        if kind == "transcribe":
            return self.transcribe(inp.get("file", ""))
        if kind == "speak":
            return self.speak(inp.get("text", ""), inp.get("voice"))
        if kind == "describe":
            return self.describe(inp.get("file", ""), inp.get("question", ""))
        if kind == "image":
            return self.image(inp.get("prompt", ""), inp.get("files") or ())
        if kind == "video":
            return self.video(inp.get("prompt", ""))
        raise HopperError(f"unknown media job type {kind!r}; known: {', '.join(TYPES)}")


def _duration(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", path], capture_output=True, text=True).stdout
        return float(out.strip())
    except (ValueError, OSError):
        return 60.0


def job_input(kind: str, title: str, body: str, files=()) -> dict:
    """Build a job's input from CLI-style arguments: the title is the prompt or text,
    files are what to transcribe/describe/edit."""
    files = [os.path.abspath(expand(f)) for f in files or ()]
    if kind in ("transcribe", "describe") and not files:
        raise HopperError(f"a {kind} job needs --file")
    if kind == "transcribe":
        return {"file": files[0]}
    if kind == "describe":
        return {"file": files[0], "question": body or ""}
    if kind == "speak":
        return {"text": body or title}
    if kind == "image":
        return {"prompt": body or title, "files": files}
    if kind == "video":
        return {"prompt": body or title}
    raise HopperError(f"unknown job type {kind!r}; known: {', '.join(TYPES)}")
