import os
import shutil
import subprocess
from pathlib import Path

import yt_dlp


class MediaProcessor:
    @staticmethod
    def extract_info(url):
        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            return downloader.extract_info(url, download=False)

    @staticmethod
    def can_convert_to_gif():
        return shutil.which("ffmpeg") is not None

    @staticmethod
    def convert_to_gif(input_path, output_path):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg is not installed or available on PATH")

        input_path = Path(input_path)
        output_path = Path(output_path)
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-filter_complex",
            (
                "[0:v]split[a][b];"
                "[a]palettegen=stats_mode=diff[p];"
                "[b][p]paletteuse=dither=sierra2_4a"
            ),
            "-loop",
            "0",
            str(output_path),
        ]
        run_options = {
            "capture_output": True,
            "text": True,
            "timeout": 600,
        }
        if os.name == "nt":
            run_options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(command, **run_options)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("FFmpeg GIF conversion timed out") from exc
        if result.returncode:
            detail = (result.stderr or "").strip()[-500:]
            raise RuntimeError(f"FFmpeg GIF conversion failed: {detail}")
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError("FFmpeg produced an empty GIF")

    @staticmethod
    def download_video(url, output_path, headers=None):
        options = {
            "outtmpl": output_path,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "overwrites": True,
        }
        if headers:
            options["http_headers"] = headers
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])
