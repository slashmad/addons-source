# RiksarkivetMediaFetch

Third-party Gramps tool addon for Gramps 6.0.

## What it does

- Scans citation `Volume/Page` values for links like:
  - `https://sok.riksarkivet.se/bildvisning/C0037740_00168`
- Resolves the best downloadable image URL.
- Downloads media and stores it in the tree media base under:
  - `media/citations`
- Reuses existing media by checksum (optional).
- Attaches media to citations as normal `MediaRef` links.
- Supports optional login flow for protected pages.
- Supports progress + cancel for long batch runs.

## Credential handling

- Login credentials can be saved with Python `keyring`.
- No plaintext or DB metadata password fallback is used.
- If `keyring` is unavailable, credentials must be entered per run.

## Notes

- Primarily intended for public Riksarkivet records.
- Some records may require login/session and can fail with access errors.
