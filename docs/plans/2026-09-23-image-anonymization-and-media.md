# Anonymization and media metadata in the published image

## Goal

The published image anonymizes (`anonymize=true`, German and English) and reads audio and
video metadata (`media_conversion_policy=metadata`) without an operator installing anything.
Both answer today with 503 and `failed` there, because the image carries neither the PII
extra with a language model nor `ffprobe`.

## Decisions

Taken by the user on 2026-09-23:

1. spaCy's `md` models for de and en, in the standard image. Measured from the 3.8 model
   cards: named-entity F1 83.8 (de) and 84.7 (en) against 84.9 and 85.5 for `lg`, at 42 and
   31 MiB against 541 and 382 MiB. Each of the two conversion workers loads its own model.
2. `ffprobe` in the image.
3. One release with everything since 3.0.0: the SSRF hardening, the /docs rework and this.

Engineering decisions, argued here:

- **The models are dependencies of the `pii` extra**, as direct references to their wheels
  in explosion/spacy-models 3.8.0. pip-compile then hashes them into `requirements.lock`
  like every other artefact, `--require-hashes` refuses a changed file, and a local
  `pip install -e '.[pii]'` works without a separate `spacy download`. Hatchling needs
  `allow-direct-references` for that; the project is not published to PyPI, which would
  refuse them.
- **PRESIDIO_DE_MODEL and PRESIDIO_EN_MODEL default to the `md` models**, the ones the
  extra installs. Keeping `lg` as the default would make the extra fail out of the box.
  A deployment that installed `lg` keeps it by setting the variable; the CHANGELOG says so.
- **ffprobe from Debian's `ffmpeg` package**, signed by the distribution, with
  `--no-install-recommends`. A static binary from a third party would be one more download
  to trust. The size is measured at the build and stated in the CHANGELOG.
- **3.1.0**: the image gains two abilities and two operational defaults change.

## Tasks, test first

1. `tests/test_packaging.py`, red first: the lock names presidio-analyzer,
   presidio-anonymizer, spacy and the two models the defaults name, each hashed; the
   Dockerfile installs `ffmpeg`.
2. `pyproject.toml` (extra, hatch), `app/config.py` (defaults), `requirements.lock`
   (regenerated in `python:3.13-slim-trixie` as the README describes, with `--extra pii`),
   `Dockerfile` (ffmpeg). Green.
3. Local proof on the built image: version, `ffprobe -version` as the service user,
   anonymize de and en through the API with entities found, metadata of a real WAV file
   through the API, image size before and after, memory with the models loaded.
4. `.github/workflows/publish.yml`: the smoke test anonymizes in both languages and runs
   `ffprobe` before the image can be pushed.
5. Documents: README, `docs/settings.md`, the /docs texts that say the image lacks both
   (`app/error_docs.py`, `app/schemas.py`), CHANGELOG 3.1.0 for everything since 3.0.0.
6. Release: version and compose tag 3.1.0, CI, tag, image on Docker Hub, `main`
   fast-forwarded after it, release notes; then one real job against the pulled image.

## Risks

| Risk | Mitigation |
|---|---|
| spaCy on Python 3.13 | spacy 3.8.16, thinc 8.3.13 and blis ship cp313 manylinux wheels; checked on PyPI |
| A model wheel changes under the same URL | the lock pins its sha256; the build fails instead |
| ffmpeg widens the attack surface of media parsing | ffprobe already runs with a protocol and format whitelist, as the service user, in its own process group with a deadline |
| Memory per conversion worker with a model loaded | measured in step 3 and documented |
