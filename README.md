# Klenty SEO Pulse

GA4 + Search Console dashboard, refreshed automatically every 3 hours by GitHub Actions and published with GitHub Pages.

- `snapshot.py` pulls the data and writes `site/data/*.json`
- `site/index.html` is the dashboard
- `.github/workflows/refresh.yml` runs the refresh on a schedule (or on demand: Actions → Refresh SEO Pulse → Run workflow)

Secrets needed (Settings → Secrets and variables → Actions):
- `GOOGLE_CREDENTIALS`: the service account's JSON key
- `GA4_PROPERTY_ID`: the GA4 property's numeric ID
