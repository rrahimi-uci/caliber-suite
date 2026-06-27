# Apache AGE Viewer — the web console for the AGE graph DB. There is no
# published image, so we build from source (pinned to a commit for repro).
#
# Layout mirrors the upstream Dockerfile (`npm run setup` installs root +
# frontend + backend, `npm run start` runs the React UI on :3000 and the Express
# API on :3001 in the same container). `sass` is pure-JS, but we add the usual
# toolchain so any transitive node-gyp dep still builds.
#
# Base is node:16 (NOT upstream's stale node:14): the backend's transitive
# winston/colorspace deps now use `||=` (needs Node 15+), and pg@8 wants ≥16.
# Node 16 is also the ceiling — Node 17+ breaks react-scripts 4's webpack via
# the OpenSSL provider change. So 16 is the one version that satisfies both.
FROM node:16-alpine

# Pinned to apache/age-viewer master @ 8a7f0be (verified buildable). Bump
# deliberately; a moving HEAD would make this image non-reproducible.
ARG AGE_VIEWER_REF=8a7f0be2513e2aa4b2caf3d9833f4e2707f0001d

RUN apk add --no-cache git python3 make g++ \
    && npm install -g pm2 npm-run-all

WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/apache/age-viewer.git \
    && git fetch --depth 1 origin "$AGE_VIEWER_REF" \
    && git checkout FETCH_HEAD

# Pre-fill the connect form with THIS suite's Postgres/AGE coordinates and
# auto-connect on mount, so the graph console needs zero manual entry: the
# user lands already connected with the knowledge_graph graph selected. (We patch the
# source rather than the UI because the connect defaults are hardcoded as
# `FormInitialValue`, and the dev server compiles from source at runtime.)
RUN F=frontend/src/components/frame/presentations/ServerConnectFrame.jsx \
    && sed -i \
      -e "s/  database: '',/  database: 'caliber',/" \
      -e "s/  host: '',/  host: 'postgres',/" \
      -e "s/  password: '',/  password: 'caliber',/" \
      -e "s/  port: null,/  port: 5432,/" \
      -e "s/  user: '',/  user: 'caliber',/" \
      -e "s|  return (|  React.useEffect(() => { connectToDatabase(FormInitialValue); }, []); return (|" \
      "$F" \
    && echo "--- patched connect defaults ---" \
    && grep -nE "caliber|host: 'postgres'|port: 5432|useEffect" "$F"

RUN npm run setup

# Upstream's backend .babelrc enables @babel/plugin-transform-runtime, whose
# emitted code requires @babel/runtime helpers — but upstream never declares
# @babel/runtime, and ships no lockfile, so a fresh install omits it. Add it
# explicitly (within the @babel/core ^7.12 line) so babel-node can resolve them.
RUN cd backend && npm install @babel/runtime@^7.12.13

# Upstream ships per-PG-major metadata SQL only through PG15, but our Postgres
# is 16 (without this, the graph "meta" panel 500s: "SQL does not exist").
# The query touches only pg_class/pg_namespace/ag_graph/ag_label — catalogs
# stable across 15→16 — so the PG15 file is correct for 16.
RUN cp -r backend/sql/15 backend/sql/16

# CRA dev-server hygiene. Do NOT set PORT/HOST here: the frontend defaults to
# 3000 and the backend to 3001 (the frontend proxies API calls to it), and a
# global PORT would make both bind the same port. We only skip the host check
# (avoids the MLflow-style "Invalid Host header" trap when reached via the
# mapped host port) and stop CRA from trying to open a browser.
ENV BROWSER=none \
    DANGEROUSLY_DISABLE_HOST_CHECK=true \
    DISABLE_ESLINT_PLUGIN=true \
    ESLINT_NO_DEV_ERRORS=true

EXPOSE 3000
CMD ["npm", "run", "start"]
