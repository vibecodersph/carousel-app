# The `buloy` branch

This is Viron's working branch. `main` is the team's.

The publishers his pipelines run are kept here and can differ from `main`: `buffer_social.py` (TikTok, Instagram Reels, X and YouTube through Buffer), `facebook_publish.py`, `instagram_publish.py`, `tiktok_publish.py` and `tiktok_draft.py`.

## On a new machine

```bash
git clone https://github.com/vibecodersph/carousel-app.git ~/projects/carousel-app
cd ~/projects/carousel-app
git checkout buloy
```

Keys go in a local `.env`, which git ignores. Each publisher's docstring names the keys it reads.

## Working rules

- Work on `buloy`. Commit only the files you changed, by path, and push to `origin/buloy`.
- Do not push to `main`, and do not merge between `main` and `buloy`, without Viron.
- This repository is public. No keys, tokens, account ids, private captions or unreleased media in a commit.
- A test never publishes: use `--dry-run`, or `--when draft` with Buffer.
