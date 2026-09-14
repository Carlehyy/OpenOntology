ARG BROWSER_BASE_IMAGE=chromedp/headless-shell:latest
FROM ${BROWSER_BASE_IMAGE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends fontconfig fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# The headless-shell base image has no usable Chromium SUID sandbox.  Run the
# container itself as a fixed unprivileged identity and keep the explicit
# --no-sandbox flag in Compose; the container boundary remains the isolation
# layer until a sandbox-enabled browser image is adopted and staged.
RUN groupadd --gid 10001 browser \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/browser --shell /usr/sbin/nologin browser \
    && chown -R 10001:10001 /home/browser
ENV HOME=/home/browser \
    XDG_CACHE_HOME=/tmp
USER 10001:10001
