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
