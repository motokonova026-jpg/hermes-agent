# Instagram Profile Viewer Plugin

Adds `instagram_profile_view` for public Instagram profile research from a profile URL or `@handle`.

## Backend

Default backend: Apify actor API, because direct Instagram scraping from the VPS is fragile.

Required protected environment variable in the active profile, usually Willow:

```bash
APIFY_API_TOKEN=...
```

Optional overrides:

```bash
INSTAGRAM_PROFILE_APIFY_TOKEN=...          # profile-specific alternative to APIFY_API_TOKEN
INSTAGRAM_PROFILE_APIFY_ACTOR_ID=...       # default: apify/instagram-profile-scraper
INSTAGRAM_PROFILE_APIFY_INPUT_JSON='...'   # JSON template; supports {{username}}, {{profile_url}}, {{max_posts}}
```

## Privacy/safety contract

- Public profiles only.
- Do not store Instagram username/passwords in Hermes.
- If a profile is private/login-only/unavailable, return that limitation instead of pretending to see it.
- Tool output is structured for the model to summarize; it may include profile metadata and recent public posts if the backend returns them.
