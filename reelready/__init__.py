import os

# Release images set this from the git tag; source checkouts report "dev".
__version__ = os.environ.get("REELREADY_VERSION") or "dev"
