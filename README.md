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

## Access

The dataset is published in two places. Both hold the same files with the same
layout; pick whichever suits the job.

**Git** — clone the whole dataset, with history:

```bash
git clone https://github.com/vcon-dev/vcon-dataset-city-of-newport-ri.git
```

**S3** — anonymous read, no AWS account or credentials required. Useful for
pulling a single meeting, or for streaming the set without a full clone:

```
s3://vcon-dataset-city-of-newport-ri/{body}/{filename}.vcon.json
https://vcon-dataset-city-of-newport-ri.s3.amazonaws.com/{body}/{filename}.vcon.json
```

```bash
# one meeting, over plain HTTPS
curl -O "https://vcon-dataset-city-of-newport-ri.s3.amazonaws.com/citycouncil/newport_citycouncil_2026-08-26_1025.vcon.json"

# browse or mirror with the AWS CLI (--no-sign-request = no credentials)
aws s3 ls s3://vcon-dataset-city-of-newport-ri/ --recursive --no-sign-request
aws s3 sync s3://vcon-dataset-city-of-newport-ri/ ./newport-vcons --no-sign-request
```

The bucket is read-only to the public: `s3:GetObject` and `s3:ListBucket` are
granted anonymously, writes are denied.

## Repository structure

One directory per meeting body:

```
vcon-dataset-city-of-newport-ri/
├── citycouncil/        # City Council regular, special & joint workshops
│   └── newport_citycouncil_2026-08-26_1025.vcon.json
├── planningboard/      # Planning Board
├── zoningboard/        # Zoning Board of Review
├── hdc/                # Historic District Commission
├── waterfront/         # Waterfront Commission
├── ...                 # one-off bodies (beach commission, stormwater, etc.)
├── scripts/
│   ├── fetch_feed.py           # Granicus feed -> vCon skeletons
│   ├── transcribe_newport.py   # download + captions/Deepgram -> WTF transcript
│   └── requirements.txt
└── media/              # downloaded video/audio cache (gitignored)
```

Any other body appearing in the Granicus feed is picked up automatically by
`fetch_feed.py --all`, which creates its directory on first sight.

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
