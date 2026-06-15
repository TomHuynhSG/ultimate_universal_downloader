from backend.plugins.base import BaseExtractor

class ExampleExtractor(BaseExtractor):
    URLS = ['example.com']
    
    async def extract(self, session):
        self.title = "Example Download Album"
        
        # Simulate an HTTP request
        # response = await session.get(self.url)
        
        self.urls = [
            "https://example.com/image1.jpg",
            "https://example.com/image2.jpg"
        ]
        return self.urls
