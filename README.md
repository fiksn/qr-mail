# QR-mail

Simple tool that parses incoming mail for (Slovenian) UPN QR codes and converts them to EPC QR format.
It is meant to be used with [https://gitlab.com/simple-nixos-mailserver/nixos-mailserver](https://gitlab.com/simple-nixos-mailserver/nixos-mailserver).
For trusted users a reply is sent back. `allowedSenders` will be processed, but never replied to - that
reply goes to `adminEmail`.

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
   catchAllWorkaround = true;
};
```

## Other uses

You can still use the python tooling independently of Nix. To convert or craft payment QR codes.

## Google Workspace Fetching

Alternatively instead of postfix this tool can use Google Workspace API as well.

### Setup steps (one-time, manual)

1. Google Cloud Console
  - New project → enable Gmail API
  - IAM → Service Accounts → Create → download JSON key
2. Google Workspace Admin (admin.google.com)
  - Security → Access and data control → API controls → Domain-wide delegation
  - Add client ID (from client_id field above) with scope:
https://www.googleapis.com/auth/gmail.modify
(modify = read + label; readonly if you prefer to mark-read via a separate mechanism)
3. The dedicated Workspace account (e.g. qr@yourdomain.com) is the address the service account impersonates — it doesn't need any special permissions itself, just needs to exist and receive mail.
