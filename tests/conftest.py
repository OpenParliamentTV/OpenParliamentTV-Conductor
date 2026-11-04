import os

# Keep tests independent of the developer's real config/secrets.env (e.g. a
# local AUTH_ENABLED=false would otherwise flip every auth test). Must run at
# import time: Settings reads SECRETS_ENV_FILE when the class is defined, and
# pytest imports conftest.py before any test module imports src.config.
os.environ.setdefault("SECRETS_ENV_FILE", "/nonexistent")
