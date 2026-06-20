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
- Gmail polling mode no longer trusts the raw `From:` header as a sender identity. Messages are processed only when Gmail authentication results show aligned SPF/DKIM/DMARC success for that `From:` domain.
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
- `parsers/` eSLOG and text extraction logic
- `scripts/` runnable CLIs and mail-processing entry points
- `tests/` test suite

## Google Workspace Fetching

Alternatively instead of postfix this tool can use Google Workspace API as well.

### Setup steps (one-time, manual)

1. Google Cloud Console
  - New project → enable Gmail API
  - IAM → Service Accounts → Create → download JSON key
2. Google Workspace Admin (admin.google.com)
  - Security → Access and data control → API controls → Domain-wide delegation
  - Add client ID (from client_id field above) with scope:
https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/gmail.send
(modify = read + label; readonly if you prefer to mark-read via a separate mechanism)
3. The dedicated Workspace account (e.g. qr@yourdomain.com) is the address the service account impersonates — it doesn't need any special permissions itself, just needs to exist and receive mail.
4. When `gmailServiceAccountFile` is configured, outbound replies/forwards are also sent through the Gmail API automatically using the same impersonated mailbox.
