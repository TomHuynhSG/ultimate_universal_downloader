import importlib.util
import inspect
import logging
import os
from urllib.parse import urlparse

from backend.plugins.base import BaseExtractor


logger = logging.getLogger(__name__)


class PluginManager:
    def __init__(self, plugins_dir="plugins"):
        self.plugins_dir = plugins_dir
        self.registry = []

    def load_plugins(self):
        self.registry = []
        plugins_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), self.plugins_dir
        )

        if not os.path.exists(plugins_path):
            os.makedirs(plugins_path)

        for filename in sorted(os.listdir(plugins_path)):
            if not filename.endswith(".py"):
                continue
            module_name = filename[:-3]
            file_path = os.path.join(plugins_path, filename)

            try:
                spec = importlib.util.spec_from_file_location(f"uud_plugin_{module_name}", file_path)
                if not spec or not spec.loader:
                    raise ImportError(f"Unable to create module spec for {file_path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                logger.exception("Failed to load plugin %s", file_path)
                continue

            for _name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseExtractor) and obj is not BaseExtractor:
                    self.registry.append(obj)
                    logger.info("Loaded plugin: %s supporting %s", obj.__name__, obj.URLS)

    @staticmethod
    def _matches(url, rule):
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if not hostname or parsed.scheme not in {"http", "https"}:
            return False
        rule_host = rule.split("/", 1)[0].lower().lstrip(".")
        if hostname != rule_host and not hostname.endswith(f".{rule_host}"):
            return False
        if "/" in rule:
            required_path = "/" + rule.split("/", 1)[1]
            return parsed.path.startswith(required_path)
        return True

    def get_extractor(self, url, progress_callback=None):
        for plugin_class in self.registry:
            for domain in plugin_class.URLS:
                if self._matches(url, domain):
                    return plugin_class(url, progress_callback=progress_callback)
        return None
