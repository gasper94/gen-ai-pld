# PLD laydown harness.
#
# The container is a CLIENT, not a self-contained system. Three things stay
# outside it and are supplied at run time: the text model (QWEN_BASE_URL), the
# vision model step 0 scores references with (REFMATCH_BASE_URL), and fal.ai
# (FAL_KEY). Nothing here downloads a model or holds a credential.
#
# Build:  docker build -t pld-harness .
# Run:    see docker-entrypoint.sh, or the "Usage" section of DOCKER.md
FROM python:3.10-slim

# ImageMagick is not optional: match_reference.py builds its result sheet with
# `magick`/`montage` and grade_flats.py measures trim boxes with
# `magick identify`, all under check=True. fonts-liberation supplies the Arial
# stand-in below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        imagemagick \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# The source photos are ~45MP (5464x8192). ImageMagick 7 on trixie already
# allows 1GiB/256MP, which clears that comfortably - but `python:3.10-slim` is a
# moving tag that shipped ImageMagick 6 on bookworm not long ago, and IM6's
# Debian policy caps memory at 256MiB, which pushes every operation on an image
# this size into the disk-backed cache and makes a run crawl for no visible
# reason. Raise it wherever it is, rather than pinning the fix to one version.
RUN set -eux; \
    for p in /etc/ImageMagick-*/policy.xml; do \
        [ -f "$p" ] || continue; \
        sed -i 's/name="memory" value="[^"]*"/name="memory" value="2GiB"/;  \
                s/name="map" value="[^"]*"/name="map" value="4GiB"/;        \
                s/name="area" value="[^"]*"/name="area" value="1GP"/;       \
                s/name="disk" value="[^"]*"/name="disk" value="8GiB"/' "$p"; \
    done

WORKDIR /app

# The venv lives at the path find_python() in harness.py already looks for
# (HERE/.venv/bin/python), so the harness hands the agent an interpreter that
# has numpy/PIL/scipy without any source change. task/SKILL.md also tells the
# agent to run tools with `../.venv/bin/python`, and that resolves here too.
RUN python -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY requirements.txt ./
RUN /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt

# Two macOS-only binaries the tools shell out to. Shimming them keeps the
# harness source byte-identical to the copy that still runs natively on macOS;
# see the header comment in each file for what they do and why.
COPY docker/sips /usr/local/bin/sips
COPY docker/magick /usr/local/bin/magick.im6
RUN set -eux; \
    chmod +x /usr/local/bin/sips /usr/local/bin/magick.im6; \
    if command -v magick >/dev/null 2>&1; then \
        rm /usr/local/bin/magick.im6; \
    else \
        mv /usr/local/bin/magick.im6 /usr/local/bin/magick; \
    fi; \
    magick -version >/dev/null

# grade_flats.py and match_reference.py pass this exact path to ImageMagick's
# -font under check=True, so on Linux the annotated sheets would fail to build.
# Liberation Sans is metric-compatible with Arial, so putting it where the
# tools already look is a smaller change than editing three call sites.
RUN mkdir -p /System/Library/Fonts/Supplemental \
    && ln -s /usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf \
             /System/Library/Fonts/Supplemental/Arial.ttf

COPY harness.py ./
COPY tools/ tools/
COPY task/ task/
COPY profiles/ profiles/
COPY library_reference/ library_reference/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# task/SKILL.md hardcodes the developer's absolute workspace path in two
# instructions it gives the agent. Rewriting it in the image beats patching the
# file in git, which would break the native macOS run this project still uses.
RUN sed -i 's#/Users/ulmarti/Desktop/PLD_Harness#/app#g' task/SKILL.md \
    && ! grep -q '/Users/' task/SKILL.md

# Created empty so a run with no volumes mounted still works; each is a mount
# point in the documented invocation.
RUN mkdir -p /app/inputs /app/runs /app/.cache /in /out

# Unbuffered so `docker logs` shows the agent's turns as they happen rather
# than in one block when the run ends.
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
