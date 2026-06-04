# Multi-Echo fMRI Posters at OHBM 2026

A searchable static index of every **multi-echo fMRI** poster at the OHBM 2026
Annual Meeting (Bordeaux, France · June 14–18, 2026), for the multi-echo community.

**`index.html`** is a single self-contained page — open it directly or serve it
via GitHub Pages.

Currently **114** posters match out of 3,219 total. Each card shows the poster
number, title, authors (lead author highlighted), a collapsible abstract (with its
figures — tap a figure to view it full-screen), the poster's session days, "why it
matched" tags, and a link to the official OHBM app. The interface has live search
(title / author / keyword), filters by session day and match method, sort, an
accent-color picker, and auto / light / dark theme (persisted).

> Unofficial community resource. Not affiliated with or endorsed by OHBM.
> Data is pulled from the public OHBM 2026 conference app.

## Regenerate

`build_site.py` (Python standard library only — no dependencies) fetches all
poster abstracts from the OHBM conference API, filters them for multi-echo fMRI
content, enriches the matches, and writes `index.html` + `posters.json` here.

```bash
cd web
python build_site.py --refetch   # fetch fresh data and rebuild the page
python build_site.py --no-fetch  # rebuild from the local cache (after a fetch)
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
| File | Description |
|------|-------------|
| `index.html` | The self-contained site (poster data inlined). |
| `posters.json` | The 114 matched posters with parsed fields. |
| `build_site.py` | Fetch + filter + generate script (stdlib only). |
