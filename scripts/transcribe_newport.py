#!/usr/bin/env python3
"""
City of Newport RI — vCon transcription tool.

For each vCon skeleton (built by fetch_feed.py) this produces a WTF transcript
(draft-howe-vcon-wtf-extension) and writes it into the vCon's analysis array.

Engines (``--engine``):

  deepgram (default)
      Downloads the recording, extracts a compact 16 kHz mono audio file, and
      uploads it to Deepgram's pre-recorded API. (Granicus' CDN blocks
      Deepgram's server IPs, so URL ingestion is not usable — we upload the
      audio bytes instead, which keeps the upload small.) Uses utterances +
      diarization, so each WTF segment is one utterance with word-level
      timestamps and a speaker label. vendor "deepgram".
      Requires DEEPGRAM_API_KEY in the environment.

  whisper
      Downloads the MP4, extracts 16 kHz mono audio, and transcribes locally
      with mlx-whisper. vendor "mlx-whisper". Runs offline, no API key.

Both engines probe for embedded closed captions first; if the recording carries
a caption track it is converted to WTF (vendor "granicus-captions") instead of
re-transcribing.

The WTF body shape is identical across engines so the dataset stays consistent.

Usage:
    export DEEPGRAM_API_KEY=...
    python transcribe_newport.py --all-pending                 # Deepgram
    python transcribe_newport.py --all-pending --model nova-2
    python transcribe_newport.py <vcon_file> --engine whisper --model large-v3
    python transcribe_newport.py --all-pending --dry-run
    python transcribe_newport.py --force <vcon_file>
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Whisper engine deps are imported lazily (see _load_whisper) so the Deepgram
# path has zero local-ML dependencies.
mlx_whisper = None
_MlxModelHolder = None
_mx = None
WhisperModel = None

REPO = Path(__file__).resolve().parent.parent
MEDIA_DIR = REPO / "media"

DEEPGRAM_ENDPOINT = "https://api.deepgram.com/v1/listen"

MLX_MODEL_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
}


# --------------------------------------------------------------------------- #
# vCon helpers
# --------------------------------------------------------------------------- #

def granicus_url(vcon: dict) -> Optional[str]:
    for d in vcon.get("dialog", []):
        url = d.get("url", "")
        if "granicus.com" in url:
            return url
    return None


def clip_id_of(vcon: dict) -> str:
    for d in vcon.get("dialog", []):
        cid = d.get("meta", {}).get("granicus_clip_id")
        if cid:
            return str(cid)
    m = re.search(r"clip_id=(\d+)", granicus_url(vcon) or "")
    return m.group(1) if m else "unknown"


def has_transcription(vcon: dict) -> bool:
    return any(a.get("type") == "wtf_transcription" for a in vcon.get("analysis", []))


# --------------------------------------------------------------------------- #
# Deepgram engine
# --------------------------------------------------------------------------- #

def deepgram_transcribe_file(audio_path: str, model: str, language: Optional[str]) -> dict:
    """Upload a local audio file to Deepgram's pre-recorded REST API.

    Uses the raw REST endpoint (stdlib only) to stay independent of SDK churn.
    We upload bytes rather than passing a URL because the Granicus CDN rejects
    Deepgram's server-side fetch (403).
    """
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set in the environment.")

    params = {
        "model": model,
        "smart_format": "true",
        "punctuate": "true",
        "utterances": "true",
        "diarize": "true",
        "paragraphs": "true",
    }
    if language:
        params["language"] = language
    endpoint = f"{DEEPGRAM_ENDPOINT}?{urllib.parse.urlencode(params)}"

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    req = urllib.request.Request(
        endpoint,
        data=audio_bytes,
        headers={
            "Authorization": f"Token {key}",
            "Content-Type": "audio/mpeg",
        },
        method="POST",
    )
    # Deepgram processes far faster than real time, but a 4-hour council
    # meeting still needs a generous ceiling.
    with urllib.request.urlopen(req, timeout=1800) as resp:
        return json.load(resp)


def deepgram_to_wtf(response: dict, model_requested: str, language: Optional[str]) -> dict:
    """Convert a Deepgram pre-recorded response to WTF.

    Each Deepgram utterance becomes one WTF segment (start/end/text/confidence,
    optional speaker) carrying word-level timestamps — the same shape the
    Whisper path emits, so analysis stays uniform across engines.
    """
    now = datetime.now(timezone.utc).isoformat()
    meta = response.get("metadata", {})
    results = response.get("results", {})
    channels = results.get("channels", [])
    alt = (channels[0].get("alternatives") or [{}])[0] if channels else {}

    full_text = alt.get("transcript", "")
    overall_conf = float(alt.get("confidence", 0.0) or 0.0)
    duration = float(meta.get("duration", 0.0) or 0.0)

    # model_info is keyed by model UUID -> {name, version, arch}
    model_label = model_requested
    mi = meta.get("model_info") or {}
    if isinstance(mi, dict) and mi:
        first = next(iter(mi.values()))
        if isinstance(first, dict):
            model_label = first.get("name", model_requested)

    def _word(w: dict) -> dict:
        return {
            "word": w.get("punctuated_word") or w.get("word", ""),
            "start": round(float(w.get("start", 0.0)), 3),
            "end": round(float(w.get("end", 0.0)), 3),
            "probability": round(float(w.get("confidence", 0.0)), 4),
        }

    segments = []
    speakers_seen = set()
    utterances = results.get("utterances") or []
    if utterances:
        for i, u in enumerate(utterances):
            seg = {
                "id": i,
                "start": round(float(u.get("start", 0.0)), 3),
                "end": round(float(u.get("end", 0.0)), 3),
                "text": (u.get("transcript") or "").strip(),
                "confidence": round(float(u.get("confidence", 0.0)), 4),
            }
            if u.get("speaker") is not None:
                seg["speaker"] = u["speaker"]
                speakers_seen.add(u["speaker"])
            words = [_word(w) for w in u.get("words", [])]
            if words:
                seg["words"] = words
            segments.append(seg)
    else:
        # No utterances (e.g. utterances disabled) — one segment from words.
        words_data = alt.get("words", [])
        words = [_word(w) for w in words_data]
        segments.append({
            "id": 0,
            "start": round(float(words_data[0].get("start", 0.0)), 3) if words_data else 0.0,
            "end": round(float(words_data[-1].get("end", 0.0)), 3) if words_data else 0.0,
            "text": full_text,
            "confidence": round(overall_conf, 4),
            **({"words": words} if words else {}),
        })

    if not full_text and segments:
        full_text = " ".join(s["text"] for s in segments).strip()
    if not duration and segments:
        duration = segments[-1]["end"]

    detected_lang = (channels[0].get("detected_language") if channels else None) or language or "en"

    return {
        "transcript": {
            "text": full_text,
            "language": detected_lang,
            "duration": round(duration, 3),
            "confidence": round(overall_conf, 4),
        },
        "segments": segments,
        "metadata": {
            "created_at": now,
            "processed_at": now,
            "provider": "deepgram",
            "model": model_label,
            "request_id": meta.get("request_id"),
            "audio": {"duration": round(duration, 3)},
            "options": {
                "language": detected_lang,
                "smart_format": True,
                "punctuate": True,
                "utterances": True,
                "diarize": True,
            },
        },
        "quality": {
            "average_confidence": round(overall_conf, 4),
            "speaker_count": len(speakers_seen) or None,
        },
    }


# --------------------------------------------------------------------------- #
# Whisper engine (mirrors ietf-meeting-vcons/scripts/whisper_transcribe.py)
# --------------------------------------------------------------------------- #

def _load_whisper() -> None:
    global mlx_whisper, _MlxModelHolder, _mx, WhisperModel
    if mlx_whisper is not None or WhisperModel is not None:
        return
    try:
        import mlx_whisper as _mw
        from mlx_whisper.transcribe import ModelHolder as _MH
        import mlx.core as _mxc
        mlx_whisper, _MlxModelHolder, _mx = _mw, _MH, _mxc
    except ImportError:
        try:
            from faster_whisper import WhisperModel as _WM
            WhisperModel = _WM
        except ImportError:
            raise RuntimeError(
                "The whisper engine needs mlx-whisper (Apple Silicon) or faster-whisper."
            )


def preload_mlx_model(model_size: str) -> None:
    if mlx_whisper is None or _MlxModelHolder is None:
        return
    model_path = MLX_MODEL_MAP.get(model_size, f"mlx-community/whisper-{model_size}-mlx")
    print(f"Pre-loading MLX model {model_path}...")
    _MlxModelHolder.get_model(model_path, _mx.float16)
    print("  Model loaded.")


def download_video(url: str, clip_id: str) -> str:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = MEDIA_DIR / f"{clip_id}.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  Using cached video: {dest.name} ({dest.stat().st_size // (1024*1024)} MB)")
        return str(dest)
    print(f"  Downloading video -> {dest.name}")
    req = urllib.request.Request(url, headers={"User-Agent": "vcon-dataset-newport/1.0"})
    tmp = dest.with_suffix(".mp4.part")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        tmp.rename(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)  # don't leave a partial file eating disk
        raise
    print(f"    {dest.stat().st_size // (1024*1024)} MB")
    return str(dest)


def extract_audio(video_path: str, clip_id: str) -> str:
    out = MEDIA_DIR / f"{clip_id}.mp3"
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", video_path,
         "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)],
        check=True,
    )
    return str(out)


def transcribe_with_whisper(audio_path: str, model_size: str, language: Optional[str]) -> tuple:
    if mlx_whisper is not None:
        model_path = MLX_MODEL_MAP.get(model_size, f"mlx-community/whisper-{model_size}-mlx")
        kwargs = {"word_timestamps": True, "verbose": False}
        if language:
            kwargs["language"] = language
        result = mlx_whisper.transcribe(audio_path, path_or_hf_repo=model_path, **kwargs)
        if _mx is not None:
            _mx.eval()

        class _Info:
            pass

        info = _Info()
        info.language = result.get("language", "en")
        info.duration = result.get("duration", 0.0)

        class _Word:
            def __init__(self, w):
                self.word = w.get("word", "")
                self.start = w.get("start", 0.0)
                self.end = w.get("end", 0.0)
                self.probability = w.get("probability", 1.0)

        class _Segment:
            def __init__(self, s):
                self.start = s.get("start", 0.0)
                self.end = s.get("end", 0.0)
                self.text = s.get("text", "")
                self.avg_logprob = s.get("avg_logprob", -0.5)
                self.words = [_Word(w) for w in s.get("words", [])]

        return [_Segment(s) for s in result.get("segments", [])], info

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        audio_path, language=language, word_timestamps=True, beam_size=5, vad_filter=True
    )
    return list(segments), info


def transcript_to_wtf(segments: list, info, model_size: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    full_text = " ".join(seg.text.strip() for seg in segments)
    wtf_segments = []
    confidence_scores = []
    for i, seg in enumerate(segments):
        raw_logprob = getattr(seg, "avg_logprob", -0.5)
        confidence = min(1.0, max(0.0, 1.0 + raw_logprob))
        confidence_scores.append(confidence)
        words = []
        if getattr(seg, "words", None):
            for w in seg.words:
                words.append({
                    "word": w.word,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                    "probability": round(w.probability, 4),
                })
        wtf_seg = {
            "id": i,
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "confidence": round(confidence, 4),
        }
        if words:
            wtf_seg["words"] = words
        wtf_segments.append(wtf_seg)

    avg = sum(confidence_scores) / len(confidence_scores) if confidence_scores else 0.0
    lang = getattr(info, "language", "en")
    duration = getattr(info, "duration", 0.0) or 0.0
    if not duration and wtf_segments:
        duration = wtf_segments[-1]["end"]
    provider = "mlx-whisper" if mlx_whisper is not None else "whisper"
    return {
        "transcript": {
            "text": full_text,
            "language": lang,
            "duration": round(duration, 3),
            "confidence": round(avg, 4),
        },
        "segments": wtf_segments,
        "metadata": {
            "created_at": now,
            "processed_at": now,
            "provider": provider,
            "model": model_size,
            "audio": {"duration": round(duration, 3)},
            "options": {"language": lang, "word_timestamps": True},
        },
        "quality": {"average_confidence": round(avg, 4)},
    }


# --------------------------------------------------------------------------- #
# Embedded captions -> WTF
# --------------------------------------------------------------------------- #

def extract_captions(video_path: str, work_dir: Path) -> Optional[str]:
    """Return SRT text if the local video carries captions (dedicated subtitle
    stream, or embedded CEA-608), else None."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s",
         "-show_entries", "stream=index", "-of", "csv=p=0", video_path],
        capture_output=True, text=True,
    )
    srt = work_dir / "captions.srt"
    if probe.stdout.strip():
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", video_path, "-map", "0:s:0", str(srt)],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and srt.exists() and srt.stat().st_size > 0:
            return srt.read_text(encoding="utf-8", errors="replace")

    cc = work_dir / "cc.srt"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"movie={video_path}[out+subcc]", "-map", "0:1", str(cc)],
        capture_output=True, text=True,
    )
    if cc.exists() and cc.stat().st_size > 0:
        text = cc.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    return None


_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")


def _ts_to_sec(ts: str) -> float:
    m = _TS.match(ts)
    if not m:
        return 0.0
    h, mn, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mn * 60 + s + ms / 1000.0


def srt_to_wtf(srt_text: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    segments = []
    for block in re.split(r"\n\s*\n", srt_text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_s, _, end_s = lines[0].partition("-->")
        text = " ".join(l.strip() for l in lines[1:]).strip()
        if not text:
            continue
        segments.append({
            "id": len(segments),
            "start": round(_ts_to_sec(start_s.strip()), 3),
            "end": round(_ts_to_sec(end_s.strip()), 3),
            "text": text,
        })
    full_text = " ".join(s["text"] for s in segments)
    duration = segments[-1]["end"] if segments else 0.0
    return {
        "transcript": {"text": full_text, "language": "en", "duration": round(duration, 3)},
        "segments": segments,
        "metadata": {
            "created_at": now,
            "processed_at": now,
            "provider": "granicus-captions",
            "model": "embedded-closed-captions",
            "audio": {"duration": round(duration, 3)},
            "options": {"language": "en", "word_timestamps": False},
        },
        "quality": {"source": "broadcast_closed_captions"},
    }


# --------------------------------------------------------------------------- #
# vCon update + orchestration
# --------------------------------------------------------------------------- #

def update_vcon(vcon_path: str, wtf: dict, vendor: str) -> None:
    with open(vcon_path, encoding="utf-8") as f:
        vcon = json.load(f)

    vcon["analysis"] = [a for a in vcon.get("analysis", []) if a.get("type") != "wtf_transcription"]
    vcon["analysis"].append({
        "type": "wtf_transcription",
        "dialog": 0,
        "vendor": vendor,
        "encoding": "json",
        "body": json.dumps(wtf),
    })
    dur = wtf.get("transcript", {}).get("duration")
    if dur and vcon.get("dialog") and not vcon["dialog"][0].get("duration"):
        vcon["dialog"][0]["duration"] = dur

    vcon["updated_at"] = datetime.now(timezone.utc).isoformat()
    vcon.setdefault("extensions", [])
    if "wtf_transcription" not in vcon["extensions"]:
        vcon["extensions"].append("wtf_transcription")

    with open(vcon_path, "w", encoding="utf-8") as f:
        json.dump(vcon, f, indent=2, ensure_ascii=False)


def prune_media(clip_id: str) -> None:
    """Delete the cached video/audio for a clip (re-downloadable, gitignored).
    Used to keep peak disk usage to a single meeting during large batches."""
    freed = 0
    for ext in (".mp4", ".mp3", ".mp4.part"):
        p = MEDIA_DIR / f"{clip_id}{ext}"
        if p.exists():
            freed += p.stat().st_size
            p.unlink()
    if freed:
        print(f"  Pruned cached media ({freed // (1024*1024)} MB freed)")


def transcribe_vcon(vcon_path: str, engine: str, model: str, language: Optional[str],
                    force: bool, prune: bool = False) -> bool:
    print(f"Processing: {Path(vcon_path).name}")
    with open(vcon_path, encoding="utf-8") as f:
        vcon = json.load(f)

    if has_transcription(vcon) and not force:
        print("  Already transcribed, skipping.")
        return False

    url = granicus_url(vcon)
    if not url:
        print("  No Granicus URL in dialog, skipping.")
        return False
    clip_id = clip_id_of(vcon)

    # Both engines: fetch the recording and try embedded captions first.
    import tempfile
    video_path = download_video(url, clip_id)
    with tempfile.TemporaryDirectory() as work:
        srt = extract_captions(video_path, Path(work))
    if srt:
        print("  Embedded captions found -> WTF (granicus-captions)")
        update_vcon(vcon_path, srt_to_wtf(srt), vendor="granicus-captions")
        print("  Done! (captions)")
        if prune:
            prune_media(clip_id)
        return True

    audio_path = extract_audio(video_path, clip_id)

    if engine == "deepgram":
        print(f"  Uploading audio to Deepgram ({model})...")
        resp = deepgram_transcribe_file(audio_path, model, language)
        wtf = deepgram_to_wtf(resp, model, language)
        update_vcon(vcon_path, wtf, vendor="deepgram")
        spk = wtf["quality"].get("speaker_count")
        print(f"  Done! model: {wtf['metadata']['model']}, segments: {len(wtf['segments'])}, "
              f"speakers: {spk}, confidence: {wtf['transcript']['confidence']:.3f}")
        if prune:
            prune_media(clip_id)
        return True

    print("  No embedded captions -> transcribing with Whisper")
    print(f"  Transcribing ({model})...")
    segments, info = transcribe_with_whisper(audio_path, model, language)
    wtf = transcript_to_wtf(segments, info, model)
    vendor = "mlx-whisper" if mlx_whisper is not None else "whisper"
    update_vcon(vcon_path, wtf, vendor=vendor)
    print(f"  Done! Language: {wtf['transcript']['language']}, "
          f"segments: {len(wtf['segments'])}, confidence: {wtf['quality']['average_confidence']:.3f}")
    if prune:
        prune_media(clip_id)
    return True


def find_pending() -> list:
    pending = []
    for path in sorted(REPO.glob("*/*.vcon.json")):
        if "media" in path.parts:
            continue
        with open(path, encoding="utf-8") as f:
            vcon = json.load(f)
        if not has_transcription(vcon):
            pending.append(path)
    return pending


# Sensible default model per engine.
DEFAULT_MODEL = {"deepgram": "nova-3", "whisper": "large-v3"}


def main():
    ap = argparse.ArgumentParser(description="Transcribe Newport RI meeting vCons (Deepgram or local Whisper)")
    ap.add_argument("vcon_file", nargs="?", help="Path to a specific vCon file")
    ap.add_argument("--all-pending", "--all", action="store_true", dest="all_pending",
                    help="Transcribe every vCon lacking a transcript")
    ap.add_argument("--engine", choices=["deepgram", "whisper"], default="deepgram",
                    help="Transcription engine (default: deepgram)")
    ap.add_argument("--model", default=None,
                    help="Model name. Default: nova-3 (deepgram) / large-v3 (whisper)")
    ap.add_argument("--language", default="en", help="Language code (default: en; '' = auto-detect)")
    ap.add_argument("--force", action="store_true", help="Re-transcribe even if already done")
    ap.add_argument("--prune-media", action="store_true",
                    help="Delete each meeting's cached video/audio after its transcript is "
                         "written (keeps peak disk to a single meeting during large batches)")
    ap.add_argument("--dry-run", action="store_true", help="List files without transcribing")
    args = ap.parse_args()

    model = args.model or DEFAULT_MODEL[args.engine]
    language = args.language or None

    if args.vcon_file:
        files = [Path(args.vcon_file)]
    elif args.all_pending:
        files = find_pending()
        if not files:
            print("No pending vCons (all have transcripts).")
            return
    else:
        ap.print_help()
        sys.exit(1)

    print(f"Engine: {args.engine}  model: {model}")
    print(f"Found {len(files)} vCon file(s) to process")
    if args.dry_run:
        for f in files:
            print(f"  {f}")
        return

    if args.engine == "deepgram":
        if not os.environ.get("DEEPGRAM_API_KEY"):
            print("Error: DEEPGRAM_API_KEY is not set. export DEEPGRAM_API_KEY=... and retry.")
            sys.exit(1)
    else:
        _load_whisper()
        preload_mlx_model(model)

    ok, errors = 0, []
    for path in files:
        try:
            if transcribe_vcon(str(path), args.engine, model, language, args.force, args.prune_media):
                ok += 1
        except Exception as e:
            errors.append((str(path), str(e)))
            print(f"  Error: {e}")

    if errors:
        print("\nErrors:")
        for p, e in errors:
            print(f"  {p}: {e}")
    print(f"\nCompleted: {ok}/{len(files)} transcribed")


if __name__ == "__main__":
    main()
