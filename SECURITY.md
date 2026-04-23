# Security Policy

## Reporting a vulnerability

Please do **not** file a public GitHub issue for security problems.

Use GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/jtspetersen/abrp-dashboard/security/advisories/new)

Expect a response within 7 days. I'm a single maintainer on a hobby project — I'll acknowledge promptly but may need a couple of weeks to investigate and ship a fix depending on severity.

## What's in scope

- Server-side execution of uploaded `.xlsx` files (zip-bombs, XLM macro paths, dependency CVEs in `openpyxl` / `pandas`).
- Injection through user-controlled fields that flow into HTML rendering (pydeck tooltips, Plotly hovers, `st.caption`).
- Leaked secrets in the codebase — tokens, API keys, credentials. (Short version: there aren't any, but if you find one it's a bug.)
- Dependency supply-chain issues that materially affect runtime behavior.

## What's out of scope

- Streamlit's own defaults (file-upload size, session handling, WebSocket behavior). Report those upstream at [streamlit/streamlit](https://github.com/streamlit/streamlit).
- Rate-limiting or abuse of Photon / OSRM / open-elevation from your instance — those are fair-use public services and their operators' problems.
- Anything requiring physical access to a user's machine (this is a local-install app).
