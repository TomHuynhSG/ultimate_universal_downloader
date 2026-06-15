class BaseExtractor:
    # Subclasses should define this list of supported domain strings
    URLS = []
    
    def __init__(self, url):
        self.url = url
        self.urls = []
        self.title = "Unknown Album"
    
    async def extract(self, session):
        """
        Extract direct links and populate self.urls.
        Uses the provided async curl_cffi session to bypass protections.
        """
        raise NotImplementedError()
