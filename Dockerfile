# Reproducible image for the core of the pipeline (chat + agentic dataset
# construction, intent classification, "as-is" adoption verification).
# It does NOT include the ~100 optional analysis scripts of the README (step 15+),
# nor the static-quality tools (semgrep) or plagiarism tools (dolos, Node.js)
# - out of scope for this submission.
#
# Build:
#   docker build -t vibe-coding-pipeline .
#
# Usage (credentials are NEVER included in the image - they are mounted at
# runtime; without them, discovery still works but is limited to 60
# requests/hour and has no code search):
#   docker run --rm -it \
#     -v "$(pwd)/pipeline/tokens.txt:/app/pipeline/tokens.txt:ro" \
#     -v "$(pwd)/pipeline/4_01_chat_intent_classification/.openai_key:/app/pipeline/4_01_chat_intent_classification/.openai_key:ro" \
#     -v "$(pwd)/pipeline/4_02_agentic_intent_classification/.openai_key:/app/pipeline/4_02_agentic_intent_classification/.openai_key:ro" \
#     -v "$(pwd)/output:/app/pipeline_output" \
#     vibe-coding-pipeline bash
#
# Inside the container, the scripts are launched exactly as locally
# (e.g. ./pipeline/run_agentic_pipeline.sh), with the working directory
# already set to the project root.

FROM python:3.13-slim

# Minimal system dependencies: git (not strictly needed by the scripts,
# but useful if the final package includes .git), build-essential
# for any Python packages compiled from source.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pipeline/requirements.txt pipeline/requirements.txt
RUN pip install --no-cache-dir -r pipeline/requirements.txt

# Chromium + required system libraries (fonts, codecs, etc.) for the
# ChatGPT/Gemini fetch via Playwright - --with-deps installs them from apt
# automatically, version always consistent with the playwright package
# just installed above.
RUN playwright install --with-deps chromium

# Pipeline code (Python/shell scripts) - NOT the generated data
# (out_ago2026/, out_vibe_coding/, etc.: too large and not part of
# the source code) nor the credentials (tokens.txt, .openai_key - NEVER
# included in the image, they are mounted at runtime, see above).
COPY pipeline/ pipeline/
COPY run_all.sh run_all.sh
RUN chmod +x run_all.sh

# Removes from the image any credential/data possibly copied
# by mistake by the COPY steps above (defense in depth - the .dockerignore
# should already exclude them, this is an explicit second layer).
RUN find . -iname "tokens.txt" -delete \
    && find . -iname ".openai_key" -delete \
    && rm -rf pipeline/out_ago2026 pipeline/out_vibe_coding pipeline/out \
              pipeline/out_unified pipeline/1_07_agentic_scraper/data

CMD ["bash"]
