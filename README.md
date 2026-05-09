# Video Publish Automation

This script lets you pick one video from a configured folder, copy it to a mounted SMB destination, upload it to YouTube as `private` / `unlisted` / `public`, optionally add it to a playlist, and then delete the original only after both destinations succeed.

## What It Does

- Lists videos from a configurable local folder and asks you to pick one.
- Copies the selected video to a configurable mounted SMB destination.
- Uploads the same video to YouTube with configurable privacy, metadata, and playlist.
- Shows live progress for both the SMB copy and the YouTube resumable upload.
- Deletes the original only after both destinations succeed, if enabled.

## Setup

1. Install dependencies:

   ```bash
   cd video_publish
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy the config and edit paths:

   ```bash
   cp config.example.toml config.toml
   ```

3. Mount your SMB share in Finder or with macOS first, then set `paths.destination_dir` to the mounted path, usually something under `/Volumes/...`.

4. In Google Cloud Console:

   - Enable **YouTube Data API v3**.
   - Create an OAuth client for a **Desktop app**.
   - Download the client JSON as `client_secret.json` into this folder, or update `youtube.client_secrets_file`.
   - The script requests YouTube upload access plus playlist-edit access so it can add the uploaded video to your configured playlist.

5. Turn off dry run when ready:

   ```toml
   [behavior]
   dry_run = false
   ```

## Run

```bash
cd video_publish
. .venv/bin/activate
python video_publish.py --config config.toml
```

The first YouTube upload opens a browser for OAuth consent. After that, `token.json` is reused.

## Notes

- SMB is intentionally handled as a mounted filesystem path. This avoids storing SMB passwords in the script and gives normal macOS keychain/reconnect behavior.
- The copy and YouTube upload run at the same time. The source video is deleted only if both succeed and `delete_source_after_success = true`.
- If YouTube says an uploaded video is forced private, that can be caused by Google API project verification limits.
- `config.toml`, `client_secret.json`, and `token.json` are intentionally ignored by git.
