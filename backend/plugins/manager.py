import os
import importlib.util
import inspect
from backend.plugins.base import BaseExtractor

class PluginManager:
    def __init__(self, plugins_dir="plugins"):
        self.plugins_dir = plugins_dir
        self.registry = []

    def load_plugins(self):
        self.registry = []
        plugins_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), self.plugins_dir)
        
        if not os.path.exists(plugins_path):
            os.makedirs(plugins_path)

        for filename in os.listdir(plugins_path):
            if filename.endswith(".py"):
                module_name = filename[:-3]
                file_path = os.path.join(plugins_path, filename)
                
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, BaseExtractor) and obj is not BaseExtractor:
                        self.registry.append(obj)
                        print(f"Loaded plugin: {obj.__name__} supporting {obj.URLS}")

    def get_extractor(self, url):
        for plugin_class in self.registry:
            for domain in plugin_class.URLS:
                if domain in url:
                    return plugin_class(url)
        return None
