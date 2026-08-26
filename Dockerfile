FROM python:3.11-slim

WORKDIR /app

# System dependencies (aeneas links against classic libespeak, not espeak-ng;
# ffmpeg for audio).
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    curl \
    ffmpeg \
    espeak \
    libespeak-dev \
    python3-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
# aeneas' setup.py imports numpy + uses distutils.msvccompiler (removed from
# setuptools >=72). Install numpy first, pin setuptools < 72, and force stdlib
# distutils so the aeneas C extensions can build on Python 3.11.
ENV SETUPTOOLS_USE_DISTUTILS=stdlib
COPY requirements.txt .
RUN pip install --no-cache-dir "setuptools<72" "numpy<2" \
    && pip install --no-cache-dir -r requirements.txt

# spaCy German model (used by NER/NEL in Tools)
RUN python -m spacy download de_core_news_md

# Application code
COPY src/ ./src/

# Runtime directories (bind-mounted at runtime, but create for non-mount dev use)
RUN mkdir -p /app/status /app/logs

# Make Tools importable without editing sys.path in app code
ENV PYTHONPATH="/data/OpenParliamentTV-Tools:${PYTHONPATH}"

# Build provenance — the image carries no git checkout, so this is the only way
# it can name the commit it came from. Declared after COPY src/ deliberately:
# the value changes on every commit, and the layers below it are the cheap ones.
# Putting it any earlier would invalidate the apt/pip/spacy-model layers and turn
# a 10-second rebuild into a very long one on a Raspberry Pi.
ARG GIT_SHA=unknown
ENV CONDUCTOR_COMMIT=$GIT_SHA

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
