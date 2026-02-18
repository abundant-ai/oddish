FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x (ESLint requires ^18.18.0 || ^20.9.0 || >=21.1.0)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone repo at HEAD commit (with fix applied)
RUN git clone https://github.com/eslint/eslint.git src && \
    cd src && \
    (git fetch --depth 1 origin 96dedf22c9810a6611aa33016d793d82ba455a10 || git fetch --depth 1 origin "+refs/pull/18098/head:refs/remotes/origin/pr/18098") && \
    git checkout --detach FETCH_HEAD && \
    git submodule update --init --recursive

WORKDIR /app/src

# Install dependencies
RUN npm install

# If install/build steps touched tracked files (lockfiles, generated assets),
# reset them so bug.patch applies cleanly.
RUN git reset --hard

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

WORKDIR /app/src
