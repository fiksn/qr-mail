# QR-mail

Simple tool that parses incoming mail for (Slovenian) UPN QR codes and converts them to EPC QR format.
It is meant to be used with [https://gitlab.com/simple-nixos-mailserver/nixos-mailserver](https://gitlab.com/simple-nixos-mailserver/nixos-mailserver) or
Google Workspace (but you still need a server to process and reply to emails periodically).

For trusted users a reply is sent back. `allowedSenders` will be processed, but never replied to - that
reply goes to `adminEmail`.

## Idea

You get invoices via email in form of PDFs or images. However if you are Slovene the QR codes are UPNs (which stands for
Univerzalni Placilni Nalog). You can scan them with your banking app to pay, however with Revolut it won't work as they don't understand this format.
The idea of this tool is that you just forward such emails (possibly automatically based on sender email) to special qr@domain.tld address that you set-up.

Then you (`adminEmail`) get a mail that is basically a forward of the original email but has QR codes extracted and replaced with EPC QR codes which
work with international banking apps. So for you the flow is exactly the same as before except that you only get an invoice with FIXED QR codes that you can
directly scan. Whether you want to keep the original mail or not is entirely up to you and how you set-up email forwarding.

First I wanted to use [UPN-EPC-QR.SI](https://upn-epc-qr.si) which works by scanning QR codes from the browser (on your phone). It works great however it is
a bit invasive to your privacy as the author sees everything. It is also an additional manual step that is cumbersome. If you already have a mail server (even
if it is not Nix based this tool is great).

I also puzzled with the idea of mobile app that would invoke Revolut with a special `intent` to prefill payment details but unfortunately due to
security reasons this is not really possible.

## Usage

Add this to `flake.nix` inputs like:
```
inputs = {
    qr-mail.url = "github:fiksn/qr-mail";
}
```
and then reference `qr-mail.nixosModules.default` in modules.

Configuration is like:
```
services.qrMail = {
   enable = true;
   myAddress = "qr@domain.tld";
   adminEmail = "admin@trusted-domain.tld";
   trustedSenders = [ "*@trusted-domain.tld" ];
   allowedSenders = [ "forwarding-noreply@google.com" ];
   eslogTrustedCertsFile = "/etc/ssl/certs/eslog-extra-trust.pem";
   catchAllWorkaround = true;
};
```

Notes:

- Outbound SMTP now verifies certificates and hostnames by default. Only disable this if you fully control the relay and cannot fix its TLS.
- Gmail mode does not deliver a forward. It inserts an artificial reply (carrying the converted QR codes) into the original conversation, and only when payment data is found. The live daemon trusts a sender only when Gmail authentication results show aligned SPF/DKIM/DMARC success for that `From:` domain. See [Google Workspace mode](#google-workspace-mode).
- eSLOG XML signatures are now accepted as `valid` only when the signer certificate chains to a trusted CA. System trust roots are used automatically, the bundled `slo-intermediates.pem` is used by default for missing intermediates, and `ESLOG_INTERMEDIATE_CERTS_FILE` overrides that default when needed. `eslogTrustedCertsFile` can add extra PEM trust anchors. Mixed CA bundles are accepted too: roots in the intermediate PEM are promoted to trust anchors automatically.
- For debugging only, set `SLOG_SKIP_CHAIN_VALIDATION=1` (or the legacy `ESLOG_SKIP_CHAIN_VALIDATION=1`) to skip certificate chain validation while still checking the XML signature and digest references.

## Other uses

You can run the Python tooling independently of the mail server, for instance to convert or craft payment QR codes.
All executable entry points live under `scripts/`.

### Getting an environment

The Python code needs three native libraries that are not installed by `pip`/`uv`:
`zbar` (for `pyzbar` QR decoding), `poppler` (for `pdf2image`), and `tesseract` (for OCR).
Pick **either** Nix **or** uv — both give you the same working environment.

**Nix** (bundles the native libraries automatically):
```bash
nix develop                       # drop into a shell with everything available
nix develop -c python3 scripts/generate_qr.py --format both   # or run a single command
```

**uv** (manages the Python side; install the native libraries with your OS package manager):
```bash
# macOS:        brew install zbar poppler tesseract
# Debian/Ubuntu: sudo apt install libzbar0 poppler-utils tesseract-ocr
uv sync                           # create .venv from pyproject.toml + uv.lock
uv run pytest                     # run the tests
uv run python scripts/generate_qr.py --format both
```

`uv sync` installs the runtime dependencies; add `--group dev` for `pytest`. Exact versions are pinned in `uv.lock`.

### Examples
```bash
python3 scripts/generate_qr.py --format both        # UPN + EPC QR PNGs
python3 scripts/generate_qr.py --format slip        # full UPN poloznica PNG (pink form + QR)
python3 scripts/generate_qr.py --format legacy_slip # legacy UPN slip with bottom OCR line
python3 scripts/generate_qr.py --format all         # UPN + EPC + poloznica
python3 scripts/generate_qr.py --format legacy_ocr_cli
python3 scripts/generate_qr.py --format slip --slip-template ./UPN-1.jpg
python3 scripts/verify_and_extract_eslog.py invoice.xml          # verify signature, print UPN fields
python3 scripts/verify_and_extract_eslog.py invoice.xml \
  | python3 scripts/generate_qr.py --format slip                 # verify + generate UPN slip
python3 scripts/debug_process.py invoice.pdf --output out.eml
ADMIN_EMAIL=admin@example.com MY_ADDRESS=qr@example.com ALLOWED_SENDERS='*@example.com' \
  python3 scripts/mail_processor.py [envelope-sender] < message.eml
```

The commands above assume an active environment (`nix develop` shell or `uv run`/an activated `.venv`).
Default slip template path is `./upn_base_empty.jpg` (bundled in repo).
Legacy OCR slip mode uses `./upn_base_legacy_ocr.jpg` by default and requires an OCR-compatible recipient reference (`SI12`).

Project layout:

- `core/` payment models, routing, and EPC/UPN conversion helpers
- `parsers/` eSLOG, ISO 20022 pain.001, ICL envelope, and text extraction logic
- `scripts/` runnable CLIs and mail-processing entry points
- `tests/` test suite

### ISO 20022 pain.001 batches

XML attachments are also parsed as ISO 20022 `pain.001` (Customer Credit
Transfer Initiation) messages. Every credit transfer in the batch becomes its
own payment with a converted EPC QR code, exactly as if each had been a separate
QR code found in the attachments. The parser is namespace-version agnostic
(pain.001.001.03/.09/.12).

When a batch produces enough payments that the generated QR codes and slips
would exceed `MAX_EMAIL_BYTES` (default 20 MB, `maxEmailBytes` in the NixOS
module), the output is split across multiple messages labelled `(part N/M)`. On
the Postfix path these are forwards (original attachments ride in the first
part); on the Gmail path they are several inserted replies.

To turn a pain.001 file into images directly (one per transaction), without any
mail:

```bash
python3 scripts/pain_to_qr.py batch.xml                       # EPC QR PNGs → ./
python3 scripts/pain_to_qr.py batch.xml -o out --format both  # EPC QR + UPN slip per tx
python3 scripts/pain_to_qr.py batch.xml --format slip --scale 8
```

Images are named `<stem>_<NN>_epc.png` / `<stem>_<NN>_slip.png`; the written
paths are printed to stdout and a per-transaction summary to stderr.

## Google Workspace mode

Instead of the Postfix pipe, qr-mail can run directly against a Gmail mailbox.
In this mode it **does not forward** mail. It periodically fetches new messages
from your allowed senders, scans each for payment data, and — only when payment
data is found — **inserts an artificial reply** into the same conversation,
carrying the converted EPC QR codes. Nothing is sent over SMTP; the reply is
injected straight into the mailbox via `users.messages.insert`. If the QR codes
exceed `MAX_EMAIL_BYTES` the reply is split into several threaded replies.

This contrasts with the Postfix path, which always forwards (even with no
payments) and re-attaches the originals.

### Authentication

Two modes are supported and auto-detected from configuration:

- **Single-user OAuth** — a normal user authorises their own mailbox once. No
  admin involvement and no domain-wide delegation. Selected when
  `GMAIL_OAUTH_TOKEN_FILE` is set (and `GMAIL_SERVICE_ACCOUNT_FILE` is not).
- **Service account + domain-wide delegation (DWD)** — a service account
  impersonates a mailbox in the workspace. Selected when
  `GMAIL_SERVICE_ACCOUNT_FILE` is set.

A single least-privilege OAuth scope, `gmail.modify`
(`https://www.googleapis.com/auth/gmail.modify`), covers reading, labelling and
inserting — it does not grant permanent deletion.

Relevant environment variables (the NixOS module sets these for you):

| variable | purpose |
|----------|---------|
| `GMAIL_IMPERSONATE_ADDRESS` | mailbox to read and insert replies into (required) |
| `GMAIL_SERVICE_ACCOUNT_FILE` | service-account JSON → DWD mode |
| `GMAIL_OAUTH_CLIENT_SECRET_FILE` | OAuth client secret → used for the one-time consent flow |
| `GMAIL_OAUTH_TOKEN_FILE` | cached, refreshable OAuth token → runtime credential for OAuth mode |
| `MY_ADDRESS` | `From:` of the inserted replies |
| `ALLOWED_SENDERS` / `TRUSTED_SENDERS` | colon-separated sender globs to act on |
| `GMAIL_POLL_INTERVAL_S` | seconds between polls (default 60) |
| `GMAIL_PROCESSED_LABEL` | label applied after handling (default `qr-mail-processed`) |
| `MAX_EMAIL_BYTES` | split replies past this size (default 20 MB) |

The Gmail search is scoped to your configured senders, so the daemon never
touches unrelated mail — important when running against your own mailbox in
single-user mode.

### Setup — single-user OAuth

1. Google Cloud Console → new project → enable the Gmail API.
2. Create an OAuth client (type *Desktop app*) and download the client-secret
   JSON.
3. Run the one-time consent flow to produce a token file:
   ```bash
   GMAIL_IMPERSONATE_ADDRESS=me@example.com \
   GMAIL_OAUTH_CLIENT_SECRET_FILE=client_secret.json \
   GMAIL_OAUTH_TOKEN_FILE=token.json \
   python3 scripts/gmail_authorize.py
   ```
   A browser opens; approve the `gmail.modify` scope. The refreshable
   token is written to `token.json`.
4. Run the daemon (or configure the NixOS module) with `GMAIL_OAUTH_TOKEN_FILE`
   pointing at that token. No further browser interaction is needed; the token
   refreshes itself.

### Setup — service account + domain-wide delegation

1. Google Cloud Console → new project → enable the Gmail API → IAM → Service
   Accounts → create → download the JSON key.
2. Google Workspace Admin (admin.google.com) → Security → Access and data
   control → API controls → Domain-wide delegation → add the service account's
   client ID with scope `https://www.googleapis.com/auth/gmail.modify`.
3. Point `GMAIL_IMPERSONATE_ADDRESS` at the mailbox to act on (e.g.
   `qr@yourdomain.com`). It only needs to exist and receive mail.

### Running

- **Live daemon** (periodic fetch + reply):
  ```bash
  python3 scripts/gmail_fetch.py
  ```
- **Backfill** (process the last N days and insert replies into past threads):
  ```bash
  # DWD: impersonate any mailbox
  GMAIL_SERVICE_ACCOUNT_FILE=key.json \
    python3 scripts/gmail_backfill.py user@example.com 1M

  # Preview matches without inserting anything
  python3 scripts/gmail_backfill.py user@example.com 1M --dry-run
  ```
  Backfill reads its allowlist from `scripts/allowed_senders.txt` (override with
  `--config`). Duration syntax: `14d`, `2w`, `1M`, `1y`.

### NixOS

Set either auth method; the module starts `qr-mail-gmail-fetch` and disables the
Postfix pipe automatically:

```nix
services.qrMail = {
  enable = true;
  myAddress = "qr@domain.tld";
  trustedSenders = [ "*@trusted-domain.tld" ];
  gmailImpersonateAddress = "qr@domain.tld";

  # DWD:
  gmailServiceAccountFile = "/run/secrets/qr-mail-service-account.json";

  # or single-user OAuth:
  # gmailOauthClientSecretFile = "/run/secrets/qr-mail-oauth-client.json";
  # gmailOauthTokenFile = "/var/lib/qr-mail/gmail-token.json";
};
```
