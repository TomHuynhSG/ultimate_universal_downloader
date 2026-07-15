import inspect


class BaseExtractor:
    URLS = []

    def __init__(self, url, progress_callback=None):
        self.url = url
        self.urls = []
        self.title = "Unknown Album"
        self.thumbnail = None
        self.progress_callback = progress_callback

    async def report_progress(self, **payload):
        if not self.progress_callback:
            return
        result = self.progress_callback(payload)
        if inspect.isawaitable(result):
            await result

    async def extract(self, session):
        raise NotImplementedError()
