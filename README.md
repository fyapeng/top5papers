# top5papers

Astro static site for tracking recent papers in the top five economics journals:

- American Economic Review (AER)
- Journal of Political Economy (JPE)
- Quarterly Journal of Economics (QJE)
- Review of Economic Studies (RES)
- Econometrica (ECTA)

The site reads the journal JSON files in the repository root and renders them at build time. The Python scraper keeps the JSON files updated, while Astro handles the frontend.

The site focuses on research articles and filters out non-research entries such as front matter, lectures, comments, replies, corrections, errata, annual reports, referee acknowledgments, and similar issue metadata.

## Local Development

```powershell
npm install
npm run dev
```

The local Astro server uses the `/top5papers/` base path:

```text
http://127.0.0.1:4321/top5papers/
```

## Build

```powershell
npm run build
```

The static output is written to `dist/`. Generated output, local Astro cache, and `node_modules/` are ignored by Git.

## Data Update

Journal data is stored in:

- `AER.json`
- `JPE.json`
- `QJE.json`
- `RES.json`
- `ECTA.json`

Run one journal update locally with:

```powershell
conda run -n codex python scraper.py AER
```

The existing GitHub Actions scraper workflow updates these JSON files. The Astro deployment workflow rebuilds the site when source files or JSON data change.

## Notes

Some journals do not expose complete metadata in RSS or email alerts. The scraper now extracts DOI-like identifiers from article links and uses Crossref as a metadata fallback for missing authors and abstracts. Email alerts are still useful as a signal that a new issue exists, but OUP alerts can truncate author lists with phrases such as "and others", so Crossref is the preferred source for author completion.

DOI, author, and abstract coverage should still be treated as source-dependent, especially for JPE abstracts.
