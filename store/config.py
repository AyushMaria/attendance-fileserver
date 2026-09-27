"""
Reads this store's settings from store_config.ini.

Why this exists: every store runs the same code but has its own device IP,
folders and tokens. Keeping those in a separate file that git ignores means
`git pull` can update the code at every store without touching - or
conflicting with - any store's settings, and the tokens never reach GitHub.

    from config import settings
    ip = settings.get("device", "ip")

First-time setup at a store: copy store_config.example.ini to
store_config.ini in this folder and fill it in.
"""

import configparser
import os
import sys
from datetime import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_NAME = "store_config.example.ini"

# An environment variable can point somewhere else - handy for testing, or
# for keeping the settings file outside the code folder entirely.
CONFIG_PATH = Path(os.environ.get("ATTENDANCE_CONFIG", HERE / "store_config.ini"))


class Settings:
    def __init__(self, path):
        self.path = Path(path)
        # interpolation off: tokens and passwords may legitimately contain %
        self._parser = configparser.ConfigParser(interpolation=None)
        self.loaded = False
        if self.path.exists():
            # utf-8-sig tolerates the invisible marker Notepad sometimes adds
            with open(self.path, encoding="utf-8-sig") as fh:
                self._parser.read_file(fh)
            self.loaded = True

    def require(self):
        """Stop with a clear message if this store has not been set up yet."""
        if not self.loaded:
            print(f"Settings file not found: {self.path}")
            print(f"Copy {EXAMPLE_NAME} to store_config.ini in {HERE}")
            print("and fill in this store's values, then run again.")
            sys.exit(1)
        return self

    def get(self, section, key, default=""):
        value = self._parser.get(section, key, fallback=None)
        if value is None:
            return default
        value = value.strip()
        # tolerate values pasted with surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value if value != "" else default

    def getint(self, section, key, default=0):
        raw = self.get(section, key, "")
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            self._bad(section, key, raw, "a whole number")

    def getfloat(self, section, key, default=0.0):
        raw = self.get(section, key, "")
        if raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            self._bad(section, key, raw, "a number")

    def getbool(self, section, key, default=False):
        raw = self.get(section, key, "").lower()
        if raw == "":
            return default
        if raw in ("1", "yes", "true", "on"):
            return True
        if raw in ("0", "no", "false", "off"):
            return False
        self._bad(section, key, raw, "yes or no")

    def getlist(self, section, key, default=None):
        raw = self.get(section, key, "")
        if raw == "":
            return list(default or [])
        return [item.strip() for item in raw.split(",") if item.strip()]

    def gettime(self, section, key, default="09:00"):
        raw = self.get(section, key, default)
        try:
            hh, mm = (int(part) for part in raw.split(":"))
            return time(hh, mm)
        except ValueError:
            self._bad(section, key, raw, "a time like 09:00")

    def getpath(self, section, key, default):
        """A folder path. Relative paths are taken relative to this folder,
        not to wherever the script happened to be started from - so a task
        with the wrong 'Start in' setting still writes to the right place."""
        path = Path(self.get(section, key, default))
        return path if path.is_absolute() else (HERE / path)

    def _bad(self, section, key, raw, expected):
        print(f"In {self.path.name}, [{section}] {key} = {raw!r} should be {expected}.")
        sys.exit(1)


settings = Settings(CONFIG_PATH)
