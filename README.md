# City of Newport, RI — Public Meeting vCons

This repository contains [vCon](https://datatracker.ietf.org/doc/draft-ietf-vcon-vcon-core/)
(Virtual Conversation Container) files built from the City of Newport, Rhode
Island's archived public meeting videos.

Each vCon captures one meeting: its metadata, the public video recording, a
lawful-basis record, and a full transcript in
[WTF (World Transcription Format)](https://datatracker.ietf.org/doc/draft-howe-vcon-wtf-extension/)
with word-level timestamps.

## Data source

All source material is public record, published by the City of Newport on its
**Granicus** portal: <https://cityofnewport.granicus.com>.

- **Enumeration:** the public RSS feed
  `ViewPublisherRSS.php?view_id=1&mode=vpodcast` lists every archived meeting
  with a title, date, and Granicus `clip_id`.
- **Video:** `DownloadFile.php?view_id=1&clip_id={id}` redirects to a direct
  MP4 on the Granicus CDN.
- **Transcripts:** Newport's recordings carry no embedded closed captions, so
  audio is transcribed by **Deepgram** (default) or local **Whisper**. When a
  meeting *does* carry captions, they are used directly instead.

## Repository structure

```
vcon-dataset-city-of-newport-ri/
├── citycouncil/        # City Council regular & special meetings
│   └── newport_citycouncil_2026-06-03_1002.vcon.json
├── scripts/
│   ├── fetch_feed.py           # Granicus feed -> vCon skeletons
│   ├── transcribe_newport.py   # download + captions/Whisper -> WTF transcript
│   └── requirements.txt
└── media/              # downloaded video/audio cache (gitignored)
```

Other meeting bodies in the feed (Planning Board, Zoning Board of Review,
Historic District Commission, etc.) can be added with the same tooling.

## File naming

`newport_{body}_{YYYY-MM-DD}_{clip_id}.vcon.json`

- `body` — meeting body slug (`citycouncil`, `planningboard`, `zoningboard`, `hdc`, …)
- `YYYY-MM-DD` — meeting date (parsed from the title, falling back to the feed date)
- `clip_id` — Granicus clip identifier

## vCon structure

Each file follows
[draft-ietf-vcon-vcon-core](https://datatracker.ietf.org/doc/draft-ietf-vcon-vcon-core/)
(`"vcon": "0.4.0"`):

- `parties` — the City of Newport (convener) and public attendees
- `dialog[0]` — `recording`, `video/mp4`, the Granicus download URL + duration
- `attachments` —
  - `meeting_metadata` (clip id, body, date, source portal)
  - `lawful_basis` — processing under the
    [Rhode Island Open Meetings Act](http://webserver.rilegislature.gov/Statutes/TITLE42/42-46/INDEX.htm)
    (recordings of open public meetings are public record)
- `analysis[0]` — `wtf_transcription` (vendor `mlx-whisper`, or
  `granicus-captions` when embedded captions exist); `body` is the WTF object
  as a JSON string with `encoding: "json"`

## Regenerating / extending the dataset

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt        # plus: brew install ffmpeg

# 1. Build skeletons for the most recent N meetings of a body
python scripts/fetch_feed.py --body citycouncil --limit 5
#    other useful flags: --all, --list-bodies, --dry-run

# 2a. Transcribe with Deepgram (default; fetches media by URL, no download)
export DEEPGRAM_API_KEY=...
python scripts/transcribe_newport.py --all-pending            # model nova-3
#    --force re-transcribes vCons that already have a transcript

# 2b. Or transcribe locally with Whisper
python scripts/transcribe_newport.py --all-pending --engine whisper --model large-v3
```

The Deepgram engine uses utterances + diarization, so each WTF segment is one
utterance with word-level timestamps and a speaker label. Downloaded
video/audio (whisper engine only) is cached under `media/` and is not committed.

## License

Code and metadata are released under the BSD 3-Clause License (see `LICENSE`).
The underlying meeting recordings are public records of the City of Newport, RI.
