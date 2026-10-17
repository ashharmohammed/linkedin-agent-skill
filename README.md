# LinkedIn Agent Skill

A reusable **draft -> human approval -> publish** workflow for LinkedIn, with an official-API text publisher and durable duplicate protection.

Built for a coding agent that can read `SKILL.md` and run Python. No Publora or n8n subscription is required. **This is a skill plus a CLI helper, not a hosted service or an always-running bot.**

## What works

| Capability | Status |
|---|---|
| Post drafting and opinion-led content planning | Agent workflow |
| Personal public text-post publishing | Verified in the originating Windows setup through the official API |
| Exact-text hash and confirmed-member binding | Included |
| Approval/submission ledger and duplicate blocking | Included |
| Hidden token import with Windows DPAPI storage | Included; Windows only |
| Configurable posting days and per-date reservations | Included |
| Daily feed/comment reading | Requires a supported authenticated browser or authorized read connector supplied by the host |
| Comment and reply drafting | Included as agent instructions |
| Comment and reply publishing | **Not included**; a live test was denied by LinkedIn permissions |
| Media uploads, Page posts, DMs, reactions | Not implemented |
| Scheduling while the host app/computer is off | Not provided by this project |

Python **3.11+**, standard library only. Live credential import/publishing currently requires Windows. Drafting, staging, dry runs, and mocked tests can run on other platforms. No private profile, token, live ledger, or personal post archive is distributed.

## Install

```sh
git clone https://github.com/ashharmohammed/linkedin-agent-skill.git
cd linkedin-agent-skill
python linkedin_publisher.py --help
python test_publisher.py
```

To use it as a Codex skill, ask the built-in skill installer to install the repository root as `linkedin-agent`, then restart/reload skill discovery as appropriate for your host. Other coding agents can read this folder's `SKILL.md` directly. No global Codex settings are modified by the repository.

## 1. Configure your local workspace

```sh
python linkedin_publisher.py init --timezone UTC --post-days mon wed fri
```

Use your actual scheduler timezone, for example `America/Toronto`, and choose your own days. This creates `~/.linkedin-agent/work/settings.json`; it does not create a scheduled task. Day numbers in that local file are Monday=0 through Sunday=6. The helper checks day/date reservations; the host scheduler is responsible for interpreting the timezone and wake-up time.

Optionally copy `PROFILE.example.md` into `~/.linkedin-agent/work/PROFILE.md` and edit your public perspective, audience, writing preferences, and avoided subjects. The skill does not assume your employer or job is your desired public identity.

For a separate data root, set `LINKEDIN_AGENT_HOME` before running commands. Keep that folder outside any public checkout and use a separate root for each account. Do not point it at a Git-tracked folder.

## 2. Set up your own LinkedIn app

1. Create your own app in [LinkedIn Developers](https://www.linkedin.com/developers/apps). Follow LinkedIn's current Page association requirements; do not invent a company or use an employer's Page without authorization.
2. Enable **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**.
3. Complete member OAuth using LinkedIn's [official token generator](https://www.linkedin.com/developers/tools/oauth) or your own correctly implemented OAuth flow.
4. Request only the required identity/posting scopes: `openid profile w_member_social`. Enter the token's *actual granted* scopes at import. Email is not needed.
5. In your own interactive terminal, run:

```sh
python linkedin_publisher.py connect
python linkedin_publisher.py auth-check
```

Paste the access token only at the hidden prompt. Enter its actual expiry as Unix seconds, optionally suffixed `UTC`, or an ISO timestamp with a timezone. Confirm the account name and type the exact `urn:li:person:...` printed by the importer. A profile URL is not an author URN. Keep passwords, client secrets, and access tokens out of chat, shell arguments, screenshots, and transcripts.

The token is encrypted in `~/.linkedin-agent/work/private/token.dpapi`, bound to your Windows user. `auth-check` verifies live identity and local expiry, not write access. Scope declarations are supplied by you; only a real approved create request verifies endpoint access. The client secret is not stored by this import path. Reauthorize when expired/revoked; automatic refresh is not implemented.

## 3. Stage and inspect a draft

```sh
python linkedin_publisher.py stage LI-20261017-DEMO-v1 --file example-post.txt --kind sample --topic "Example"
python linkedin_publisher.py dry-run LI-20261017-DEMO-v1
```

This makes **zero network calls** and the sample cannot publish. Use a fresh ID/date and `--kind requested` for a real post, or `--kind scheduled` on a configured post day. Each saved version has its text, metadata, character count, and SHA-256. The final saved text is normalized to LF with no trailing newline; review that exact version, not a separate unsaved copy.

## 4. Approve and publish a real post

Only after the human reviews and approves that exact real draft:

```sh
python linkedin_publisher.py approve LI-20261017-FIRST-v1 --sha256 "REVIEWED_SHA256" --author "CONFIRMED_PERSON_URN" --evidence "Direct human approval of this exact shown version"
python linkedin_publisher.py publish LI-20261017-FIRST-v1
```

Replace the example ID/hash/URN with the real staged values. The `approve` command records consent supplied by the human/agent interaction; it is **not** an independent access-control boundary against someone who can run arbitrary local commands.

Posts are public. The publisher rechecks the confirmed account and text, records in-flight state in SQLite, and sends one request to `POST https://api.linkedin.com/rest/posts`. A successful response includes the post URL. API version `202609` is pinned from the working implementation; check LinkedIn's supported versions before changing it or when maintenance is required. There is no automatic endpoint/version fallback.

```sh
python linkedin_publisher.py status
python linkedin_publisher.py withdraw LI-20261017-FIRST-v1 --evidence "Direct human skip/revision"
```

Withdrawal blocks an unsent version; it cannot cancel a sent request or delete a published post.

## Failure handling

A timeout is an uncertain outcome. Do not rerun `publish`, use a new ID with the same text, or clear the database to force another create request. Confirm whether the exact post exists on the intended account. For a verified existing result:

```sh
python linkedin_publisher.py reconcile DRAFT_ID --post-urn "VERIFIED_SHARE_OR_UGCPOST_URN" --evidence "Verified exact text and intended author on LinkedIn"
```

Reconciliation records that result without posting. Rejected requests also remain locked for investigation. The helper does not provide a force/retry switch. Preserve the local ledger across upgrades and backups; losing it also loses local duplicate history. DPAPI backup/decryption remains bound to the same Windows account context.

## Cadence example

Ask your host agent to schedule a daily 9:00 AM check in your timezone. It can prepare replies to new meaningful comments and one relevant comment on another person's feed post, while preparing original posts only on Monday, Wednesday, and Friday. Require review before publishing, retain pending versions, and stay quiet on unchanged checks. These are configurable starter choices, not a scheduler installed by this repository.

Comment/reply publishing stays draft-only in this release. Working personal posting does not prove comment-create or member-feed-read access. A live versioned Comments API test returned a permission denial; [current Comments API documentation](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/comments-api) lists `w_member_social_feed`. Do not advertise automatic comments/replies as working with a basic posting token.

## Tests and privacy

```sh
python test_publisher.py
```

29 offline checks cover approval/hash/account binding, normalization, configurable days, duplicates, in-flight persistence, unknown results, failure locks, withdrawal, and dummy DPAPI round-tripping. HTTP is mocked and blocked in tests; they never use your saved token. The DPAPI check is skipped outside Windows. Test fixtures use dummy data under ignored `work/tests/`.

`.gitignore` excludes state, credentials, drafts, databases, transcripts, and environment files. Runtime state also defaults outside the repository. Review your own changes before publishing; an ignore file does not protect already tracked files.

## Attribution and license

Created by [ashharmohammed](https://github.com/ashharmohammed). The workflow was developed while using [Sergey Bulaev's LinkedIn skills](https://github.com/sergebulaev/linkedin-skills) for drafting guidance. This repository packages an independent official-API publisher and a focused reusable skill; it does not redistribute or claim authorship of that upstream bundle. Install it separately if you want its additional writing/analysis skills.

MIT licensed; see [LICENSE](LICENSE).

Official references: [Share on LinkedIn](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin), [OpenID Connect](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2), [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api), [API versioning](https://learn.microsoft.com/en-us/linkedin/marketing/versioning), and [LinkedIn API Terms](https://www.linkedin.com/legal/l/api-terms-of-use).
