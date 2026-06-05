# Multi-Echo fMRI Posters at OHBM 2026

A searchable static index of OHBM 2026 posters (Bordeaux, France · June 14–18,
2026) — focused on **multi-echo fMRI**, but browsable across the whole meeting.
It lands on the multi-echo subset (**114** of ~3,217 posters) with a scope toggle
to reveal every poster, and a **Reports** menu linking the interactive tedana reports.

The page is generated into the repo's **`docs/`** (the GitHub Pages source), beside
the tedana reports. Each card shows the poster number, title, authors (lead author
highlighted), session days, and a link to the official OHBM app; matched posters also
carry "why it matched" tags. Every abstract is readable in place — matched abstracts
(with figures; tap one to view it full-screen) are inlined for an instant default
view, and the rest are lazy-loaded from `abstracts/<id>.html` on demand, keeping the
page light. The interface has live search (title / author / keyword), filters by
session day and match method, sort, an accent-color picker, and auto / light / dark
theme (persisted).

> Unofficial community resource. Not affiliated with or endorsed by OHBM.
> Data is pulled from the public OHBM 2026 conference app.

## Regenerate

`build_site.py` (Python standard library only — no dependencies) fetches all
poster abstracts from the OHBM conference API, enriches them, and writes the site
into `../docs` by default: `index.html`, `posters.json`, and the lazy-loaded
`abstracts/`.

```bash
cd web
python build_site.py --refetch   # fetch fresh data and rebuild the page
python build_site.py --no-fetch  # rebuild from the local cache (after a fetch)
```

The matched subset and output are configurable, so the same tool can build other
itineraries:

```bash
python build_site.py --query 'diffusion|DWI' --label diffusion \
    --output-file diffusion.html        # --output-dir is also available
```

### Matching logic
"multi-echo" alone is a generic MRI term (QSM, relaxometry, MRS), so the filter is
two-tiered to target multi-echo *fMRI*:

- **ME-fMRI-specific** (always qualify): `tedana`, `ME-ICA`, `ME-fMRI` / `ME-EPI`.
- **fMRI-gated** (only when the abstract also has fMRI/BOLD context): `multi-echo`,
  echo/optimal combination, `T2*`/`R2*` mapping, `TE-dependent`, `S0`.

Edit `multiecho_reasons()` in `build_site.py` and re-run `--no-fetch` to tune.

### External services
Fonts (Google Fonts) and icons (Lucide) load from CDN, and abstract figures are
served through the wsrv.nl image proxy for reliable caching.

## Files
| Path | Description |
|------|-------------|
| `web/build_site.py` | Fetch + tag + generate script (stdlib only). |
| `docs/index.html` | The generated site (all-poster metadata + matched abstracts inlined). |
| `docs/posters.json` | All ~3,217 posters with parsed fields. |
| `docs/abstracts/<id>.html` | Per-poster abstracts, lazy-loaded by the page on demand. |
