# Removing committed secrets and rotating keys

Applies when `secrets/`, `.env`, or any key file has been pushed to the
repository — which happened in the first push of CitePrism Phase 4
(`secrets/export-signing.key`). Deleting the file in a new commit is **not**
enough: the key stays readable in git history and in every clone and fork.
Treat it as compromised and replace it.

`python scripts/check_no_secrets.py` fails CI while any such file is tracked.

## 1. Stop tracking the files (every repository owner does this once)

```powershell
cd <repo>
git rm -r --cached secrets .env 2>$null      # removes from git, keeps your local copies
git add .gitignore secrets/README.md
python scripts/check_no_secrets.py           # must print "Secret check passed"
git commit -m "Stop tracking secrets; ignore key material"
git push
```

Check whether `.env` was ever pushed:

```powershell
git log --all --oneline -- .env secrets
```

Any commit listed means the corresponding values were public to everyone with
read access. Continue with step 2 for each of them.

## 2. Rotate what was exposed

| Exposed item | What to do |
|---|---|
| `secrets/export-signing.key` (Ed25519) | Move the old file away, run `.\scripts\setup_phase4.ps1 -GenerateSigningKey`, and change `CITEPRISM_EXPORT_SIGNING_KEY_ID` in `.env` (for example `citeprism-export-2`) so new signatures are distinguishable. Exports signed with the old key ID must no longer be trusted as authentic; re-export anything that is shared outside the team. |
| `CITEPRISM_EXPORT_HMAC_SECRET` | Generate a new value: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `CITEPRISM_PROXY_SECRET` | Generate a new value as above and update the reverse proxy at the same time. |
| `GOOGLE_API_KEY` | Google AI Studio / Cloud console → delete the key, create a new one, restrict it to the Generative Language API. |
| `HF_API_TOKEN` | huggingface.co → Settings → Access Tokens → invalidate and create a fine-grained read token. |
| `OPENAI_API_KEY` | platform.openai.com → API keys → revoke and create a new key. |
| `SEMANTIC_SCHOLAR_API_KEY` | Request a replacement from Semantic Scholar and ask them to revoke the old key. |
| Database / S3 credentials | Change the password or access key in PostgreSQL / the object store, then in `.env`. |

Distribute new values only over a secure channel (a password manager share),
never in chat, email, an issue or a zip.

## 3. Optional: purge history

Rotation is what protects you; purging only removes the clutter. If the team
wants the old key gone from history:

```powershell
pip install git-filter-repo
git clone --mirror <repository-url> citeprism-mirror
cd citeprism-mirror
git filter-repo --invert-paths --path secrets/export-signing.key --path .env
git push --force --mirror
```

Afterwards **every collaborator must delete their clone and clone again**;
pulling into an old clone reintroduces the purged objects. Forks and GitHub's
cached views may still hold the old content for some time, which is why
rotation in step 2 is mandatory regardless.

## 4. Keep it from happening again

- `.gitignore` excludes `.env`, `.env.*` (except `env.example`), `secrets/*`
  (except its README), and `*.key`, `*.pem`, `*.p12`, `*.pfx`, SSH keys.
- Before committing: `python scripts/check_no_secrets.py --staged`.
- CI runs the same check on every push and pull request.
- Enable GitHub secret scanning and push protection
  (Settings → Code security) on the repository.
