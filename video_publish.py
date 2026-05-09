#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import mimetypes
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from tqdm import tqdm


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
CHUNK_SIZE = 8 * 1024 * 1024


class ConfigError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    required = [
        ("paths", "source_dir"),
        ("paths", "destination_dir"),
        ("youtube", "client_secrets_file"),
        ("youtube", "token_file"),
    ]
    for section, key in required:
        if not config.get(section, {}).get(key):
            raise ConfigError(f"Missing config value: {section}.{key}")

    privacy = config["youtube"].get("privacy_status", "private")
    if privacy not in {"private", "unlisted", "public"}:
        raise ConfigError("youtube.privacy_status must be private, unlisted, or public")

    conflict = config.get("behavior", {}).get("on_destination_conflict", "timestamp")
    if conflict not in {"fail", "timestamp"}:
        raise ConfigError("behavior.on_destination_conflict must be fail or timestamp")


def list_videos(source_dir: Path, extensions: list[str]) -> list[Path]:
    normalized = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}
    videos = [
        path
        for path in source_dir.iterdir()
        if path.is_file() and path.suffix.lower() in normalized
    ]
    return sorted(videos, key=lambda p: p.stat().st_mtime, reverse=True)


def pick_video(videos: list[Path]) -> Path:
    print("\nPick a video to process:\n")
    for index, video in enumerate(videos, start=1):
        size_mb = video.stat().st_size / 1024 / 1024
        modified = dt.datetime.fromtimestamp(video.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{index:2d}. {video.name}  ({size_mb:.1f} MB, modified {modified})")

    while True:
        choice = input("\nEnter number, or q to quit: ").strip().lower()
        if choice in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if choice.isdigit() and 1 <= int(choice) <= len(videos):
            return videos[int(choice) - 1]
        print("Invalid choice.")


def resolve_destination(source: Path, destination_dir: Path, conflict_mode: str) -> Path:
    destination = destination_dir / source.name
    if not destination.exists():
        return destination
    if destination.stat().st_size == source.stat().st_size:
        return destination
    if conflict_mode == "fail":
        raise FileExistsError(f"Destination file already exists: {destination}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return destination.with_name(f"{destination.stem}-{stamp}{destination.suffix}")


def copy_with_progress(source: Path, destination: Path, dry_run: bool) -> Path:
    if dry_run:
        print(f"[dry-run] Would copy to SMB destination: {destination}")
        return destination

    if destination.exists() and destination.stat().st_size == source.stat().st_size:
        print(f"SMB destination already exists with matching size, skipping copy: {destination}")
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    total = source.stat().st_size
    tmp_destination = destination.with_name(f"{destination.name}.partial")

    with source.open("rb") as src, tmp_destination.open("wb") as dst:
        with tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            desc="SMB copy",
            position=0,
            leave=True,
        ) as bar:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
                bar.update(len(chunk))

    tmp_destination.replace(destination)
    return destination


def youtube_service(client_secrets_file: Path, token_file: Path, dry_run: bool):
    if dry_run:
        return None

    credentials = None
    if token_file.exists():
        credentials = Credentials.from_authorized_user_info(json.loads(token_file.read_text()), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), SCOPES)
        credentials = flow.run_local_server(
            port=0,
            prompt="consent select_account",
            access_type="offline",
        )

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json())
    return build("youtube", "v3", credentials=credentials)


def validate_youtube_destination(service, playlist_id: str | None) -> None:
    if not playlist_id:
        return

    channels = service.channels().list(part="snippet", mine=True).execute().get("items", [])
    channel_ids = {channel["id"] for channel in channels}
    channel_names = ", ".join(channel["snippet"].get("title", channel["id"]) for channel in channels)

    playlists = service.playlists().list(part="snippet", id=playlist_id).execute().get("items", [])
    if not playlists:
        raise ConfigError(f"YouTube playlist not found or not visible to this OAuth account: {playlist_id}")

    playlist = playlists[0]
    owner_channel_id = playlist["snippet"].get("channelId")
    owner_title = playlist["snippet"].get("channelTitle", owner_channel_id)
    if owner_channel_id not in channel_ids:
        raise ConfigError(
            "Configured playlist belongs to a different YouTube channel. "
            f"Authenticated channel(s): {channel_names or 'none'}; "
            f"playlist owner: {owner_title} ({owner_channel_id}). "
            "Delete token.json and authorize the Google/YouTube channel that owns the playlist, "
            "or use a playlist owned by the authenticated channel."
        )


def upload_to_youtube(source: Path, config: dict[str, Any], dry_run: bool) -> str | None:
    yt_config = config["youtube"]
    privacy = yt_config.get("privacy_status", "private")
    title = yt_config.get("title") or source.stem
    description = yt_config.get("description", "")
    tags = yt_config.get("tags", [])
    category_id = yt_config.get("category_id", "22")

    if dry_run:
        print(f"[dry-run] Would upload to YouTube: title={title!r}, privacy={privacy!r}")
        if yt_config.get("playlist_id"):
            print(f"[dry-run] Would add uploaded video to playlist: {yt_config['playlist_id']}")
        return "dry-run-video-id"

    service = youtube_service(
        Path(yt_config["client_secrets_file"]).expanduser(),
        Path(yt_config["token_file"]).expanduser(),
        dry_run=False,
    )
    validate_youtube_destination(service, yt_config.get("playlist_id"))
    mimetype = mimetypes.guess_type(source.name)[0] or "video/*"
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(source), mimetype=mimetype, chunksize=CHUNK_SIZE, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    with tqdm(total=100, unit="%", desc="YouTube upload", position=1, leave=True) as bar:
        last_percent = 0
        while response is None:
            status, response = request.next_chunk()
            if status:
                percent = int(status.progress() * 100)
                bar.update(max(0, percent - last_percent))
                last_percent = percent
        bar.update(100 - last_percent)

    video_id = response["id"]
    playlist_id = yt_config.get("playlist_id")
    if playlist_id:
        service.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id,
                    },
                }
            },
        ).execute()
        print(f"Added to playlist: {playlist_id}")

    return video_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Move a picked video to SMB and upload it to YouTube.")
    parser.add_argument("--config", default="config.toml", help="Path to TOML config file.")
    args = parser.parse_args()

    try:
        config_path = Path(args.config).expanduser().resolve()
        config = load_config(config_path)
        paths = config["paths"]
        behavior = config.get("behavior", {})
        dry_run = bool(behavior.get("dry_run", True))

        source_dir = Path(paths["source_dir"]).expanduser()
        destination_dir = Path(paths["destination_dir"]).expanduser()
        extensions = paths.get("extensions", [".mp4", ".mov", ".m4v", ".mkv"])

        if not source_dir.is_dir():
            raise ConfigError(f"Source directory does not exist: {source_dir}")
        if not dry_run and not destination_dir.exists():
            raise ConfigError(f"Destination directory does not exist or SMB share is not mounted: {destination_dir}")

        videos = list_videos(source_dir, extensions)
        if not videos:
            print(f"No videos found in {source_dir}")
            return 0

        selected = pick_video(videos)
        destination = resolve_destination(
            selected,
            destination_dir,
            behavior.get("on_destination_conflict", "timestamp"),
        )

        print(f"\nSelected: {selected}")
        print(f"SMB destination: {destination}")
        print(f"Dry run: {dry_run}\n")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            copy_future = executor.submit(copy_with_progress, selected, destination, dry_run)
            upload_future = executor.submit(upload_to_youtube, selected, config, dry_run)
            copied_to = copy_future.result()
            video_id = upload_future.result()

        if behavior.get("delete_source_after_success", True) and not dry_run:
            selected.unlink()
            print(f"Deleted source after successful copy and upload: {selected}")

        print("\nDone.")
        print(f"Copied to: {copied_to}")
        print(f"YouTube video id: {video_id}")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except (ConfigError, FileExistsError, HttpError, OSError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
